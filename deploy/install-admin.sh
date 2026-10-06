#!/usr/bin/env bash
# Installs the admin dashboard's web address: the nginx site, its certificate, the dashboard service and
# the split-DNS responder. Your domain, Tailscale address and allowed devices come from two private files
# next to this script (see local.env.example and allowed-devices.example.txt); nothing about your
# deployment is in the repository.
#
# Run as root, one stage at a time and check each result:
#
#   deploy/install-admin.sh render DIR     # write the filled-in files to DIR for review (no root needed, installs nothing)
#   deploy/install-admin.sh http           # port 80 site, so the certificate can be issued
#   deploy/install-admin.sh cert           # request the Let's Encrypt certificate
#   deploy/install-admin.sh https          # the real site (443, only the listed Tailscale devices)
#   deploy/install-admin.sh service        # install and start the dashboard itself
#   deploy/install-admin.sh dns            # install and start the split-DNS responder
#
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export DASHBOARD_LOCAL_ENV=${DASHBOARD_LOCAL_ENV:-$HERE/local.env}
export DASHBOARD_ALLOWED_DEVICES=${DASHBOARD_ALLOWED_DEVICES:-$HERE/allowed-devices.txt}
AVAIL=/etc/nginx/sites-available/admin
ENABLED=/etc/nginx/sites-enabled/admin
SNIPPET=/etc/nginx/snippets/admin-proxy.conf
WEBROOT=/var/www/letsencrypt

die() { echo "ERROR: $*" >&2; exit 1; }
render() { python3 "$HERE/render.py" "$1"; }                    # prints the filled-in template, or fails with a clear message
value()  { python3 "$HERE/render.py" --get "$1"; }

# Put a site file in place, test the WHOLE nginx configuration, and reload only if the test passes.
# If it fails, the previous state is restored, so a mistake here cannot take the other sites down.
install_site() {
    local src=$1 backup="" had_link=0
    if [[ -e $AVAIL ]]; then backup=$(mktemp); cp -p "$AVAIL" "$backup"; fi
    if [[ -L $ENABLED ]]; then had_link=1; fi
    install -m 644 "$src" "$AVAIL"
    ln -sfn "$AVAIL" "$ENABLED"
    if nginx -t; then
        systemctl reload nginx
        echo "nginx reloaded."
    else
        echo "nginx -t failed; restoring the previous state." >&2
        if [[ -n $backup ]]; then install -m 644 "$backup" "$AVAIL"; else rm -f "$AVAIL"; fi
        if (( ! had_link )); then rm -f "$ENABLED"; fi
        [[ -z $backup ]] || rm -f "$backup"
        exit 1
    fi
    [[ -z $backup ]] || rm -f "$backup"
}

stage=${1:-}
if [[ $stage == render ]]; then                                   # read-only: no root needed
    out=${2:?usage: $0 render DIR}
    mkdir -p "$out"
    render "$HERE/nginx-admin-http.conf.template" > "$out/nginx-admin-http.conf"
    render "$HERE/nginx-admin.conf.template"      > "$out/nginx-admin.conf"
    render "$HERE/admin-dns.service.template"     > "$out/admin-dns.service"
    echo "Filled-in files written to $out"
    exit 0
fi

[[ $EUID -eq 0 ]] || die "run as root: sudo $0 ${stage:-<stage>}"
DOMAIN=$(value DOMAIN)
TAILSCALE_IP=$(value TAILSCALE_IP)
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem

case "$stage" in
http)
    install -d -m 755 "$WEBROOT"
    tmp=$(mktemp); render "$HERE/nginx-admin-http.conf.template" > "$tmp"
    install_site "$tmp"; rm -f "$tmp"
    echo
    echo "Check (expect 'HTTP/1.1 404', which proves the new site answers):"
    echo "  curl -sI http://$DOMAIN/.well-known/acme-challenge/x"
    ;;
cert)
    [[ -L $ENABLED ]] || die "run the http stage first"
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --dry-run
    certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --deploy-hook "systemctl reload nginx"
    ;;
https)
    [[ -r $CERT ]] || die "no certificate yet; run the cert stage first"
    install -m 644 "$HERE/nginx-admin-proxy.conf" "$SNIPPET"
    tmp=$(mktemp); render "$HERE/nginx-admin.conf.template" > "$tmp"
    install_site "$tmp"; rm -f "$tmp"
    echo
    echo "This server is not on the allow-list, so the connection should be closed without any page (curl error 52, 56 or 92):"
    echo "  curl -sk https://$DOMAIN/ --resolve $DOMAIN:443:127.0.0.1"
    ;;
service)
    python3 - "$HERE/../config.json" <<'PY' || die "set the login first: python3 -m dashboard set-password"
import json, sys
sys.exit(0 if json.load(open(sys.argv[1]))["auth"]["password_hash"] else 1)
PY
    install -m 644 "$HERE/server-dashboard.service" /etc/systemd/system/server-dashboard.service
    systemctl daemon-reload
    systemctl enable --now server-dashboard
    sleep 3
    systemctl --no-pager --lines=8 status server-dashboard || true
    echo
    curl -fsS http://127.0.0.1:9100/healthz && echo
    ;;
dns)
    render "$HERE/admin-dns.service.template" > /etc/systemd/system/admin-dns.service
    chmod 644 /etc/systemd/system/admin-dns.service
    systemctl daemon-reload
    systemctl enable admin-dns
    systemctl restart admin-dns
    sleep 2
    systemctl --no-pager --lines=5 status admin-dns || true
    echo
    python3 "$HERE/../dnsd/admin_dns.py" --name "$DOMAIN" --test "$TAILSCALE_IP" --a "$TAILSCALE_IP"
    ;;
*)
    die "usage: $0 render DIR | http | cert | https | service | dns"
    ;;
esac
