"""HTTP layer: login, sessions, JSON API and static files.

The app listens on loopback only; nginx is the way in. Because other local services can also
reach 127.0.0.1, nothing here trusts the network position: every data route needs a session,
every POST needs a CSRF token and a matching Origin, and the Host header must be ours.
"""

import hmac
import html
import ipaddress
import json
import logging
import re
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, config
from .actions import ActionError
from .alerts import evaluate
from .auth import (MAX_PASSWORD_LEN, SESSION_COOKIE, LoginLimiter, Sessions, hash_password, verify_password)
from .collectors.logs import unit_logs
from .config import ROOT
from .util import CommandError

log = logging.getLogger("dashboard.http")

STATIC_DIR = ROOT / "static"
MAX_BODY = 8192
MAX_CONNECTIONS = 24
MAX_METRICS = 40

# Which collector results each tab needs.
TABS = {
    "overview": ["system", "cpu", "memory", "disk_io", "net_io", "disks"],
    "processes": ["processes", "cgroups"],
    "services": ["systemd", "supervisor", "cgroups"],
    "network": ["net_io", "sockets", "tailscale"],
    "security": ["fail2ban", "logins", "ssh_auth", "updates"],
    "logs": ["journal", "nginx", "ssl"],
}
RANGES = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".woff2": "font/woff2", ".json": "application/json"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        self.status, self.message, self.headers = status, message, headers or {}


class App:
    """Everything a request handler needs."""

    def __init__(self, cfg, scheduler, history, sessions: Sessions, limiter: LoginLimiter | None = None, alerts=None, actions=None):
        self.cfg, self.scheduler, self.history, self.sessions = cfg, scheduler, history, sessions
        self.admin = config.admin_switches(cfg)          # which admin tools are on; fixed for the life of the process
        self.alerts, self.actions = alerts, actions
        self.limiter = limiter or LoginLimiter()
        host, _, port = cfg["listen"].rpartition(":")
        self.public_host = cfg["public_host"].lower()
        self.allowed_hosts = {self.public_host, f"{host}:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"https://{self.public_host}", f"http://{host}:{port}", f"http://localhost:{port}"}
        # Verifying against this when the username is wrong keeps both failures equally slow.
        self.dummy_hash = hash_password("not-the-password")
        self.started = time.time()

    def require_feature(self, name: str) -> None:
        """404 (not 403) for an admin tool that is switched off, so a switched-off tool is indistinguishable from a missing one."""
        if not self.admin.get(name):
            raise HttpError(404, "not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address, app: App, max_connections: int = MAX_CONNECTIONS):
        self.app = app
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        # Each connection is a thread, and this host has under 1 GB of RAM: shed load instead of queueing.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "dashboard"
    sys_version = ""
    timeout = 15                       # a client that stalls cannot hold a thread for long

    # ---- plumbing ------------------------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, fmt, *args):  # nginx keeps the access log; errors go through `log`
        log.debug("%s %s", self.address_string(), fmt % args)

    def client_ip(self) -> str:
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Real-IP", "")
        try:
            if ipaddress.ip_address(peer).is_loopback:
                return str(ipaddress.ip_address(forwarded.strip()))
        except ValueError:
            pass
        return peer

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None) -> None:
        self._send(status, json.dumps(obj, separators=(",", ":"), default=str).encode(),
                   "application/json", headers)

    def _redirect(self, location: str) -> None:
        self._send(302, b"", "text/plain", {"Location": location})

    def _read_body(self) -> bytes:
        """Read the request body up front so an early error response never leaves unread bytes behind."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "bad Content-Length") from None
        if length < 0 or length > MAX_BODY:
            raise HttpError(413, "request body too large")
        return self.rfile.read(length) if length else b""

    def _read_json(self) -> dict:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise HttpError(415, "Content-Type must be application/json")
        try:
            body = json.loads(self._body)
        except ValueError:
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "expected a JSON object")
        return body

    # ---- guards --------------------------------------------------------------------------

    def _check_host(self) -> None:
        if self.headers.get("Host", "").lower() not in self.app.allowed_hosts:
            raise HttpError(421, "unknown host")        # also blocks DNS-rebinding attempts

    def _check_origin(self) -> None:
        """POSTs must come from our own page: same Origin, never a cross-site request."""
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.app.allowed_origins:
            raise HttpError(403, "cross-origin request refused")
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            raise HttpError(403, "cross-site request refused")

    def _token(self) -> str:
        try:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return jar[SESSION_COOKIE].value if SESSION_COOKIE in jar else ""

    def _session(self) -> dict | None:
        token = self._token()
        session = self.app.sessions.lookup(token)
        if session:
            session["token"] = token
        return session

    def _need_session(self, post: bool = False) -> dict:
        """The signed-in session. A POST is always something a person did, so it also counts as
        activity for the idle timeout; GETs (the page's own polling) do not."""
        session = self._session()
        if session is None:
            raise HttpError(401, "not signed in")
        if post:
            self._check_origin()
            sent = self.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, session["csrf"]):
                raise HttpError(403, "missing or wrong CSRF token")
            self.app.sessions.touch(session["token"])
            session["idle_left"] = self.app.sessions.idle
        return session

    # ---- dispatch ------------------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._check_host()
            self._body = self._read_body() if method == "POST" else b""
            url = urlsplit(self.path)
            path, qs = url.path, parse_qs(url.query)
            route = (method, path)
            if route == ("GET", "/healthz"):
                return self._json(200, {"ok": True})
            if route == ("GET", "/login"):
                return self._page("login.html", redirect_if_signed_in=True)
            if route == ("POST", "/login"):
                return self._login()
            if method == "GET" and path.startswith("/static/"):
                return self._static(unquote(path[len("/static/"):]))
            if route == ("GET", "/"):
                session = self._session()
                if session is None:
                    return self._redirect("/login")
                self.app.sessions.touch(session["token"])      # opening or reloading the page is activity
                return self._page("index.html")
            if route == ("POST", "/logout"):
                return self._logout()
            if route == ("POST", "/api/session/ping"):
                return self._json(200, {"ok": True, "idle_left": self._need_session(post=True)["idle_left"]})
            if route == ("POST", "/api/alerts/mute"):
                return self._alert_mute()
            if route == ("POST", "/api/alerts/unmute"):
                return self._alert_unmute()
            if route == ("POST", "/api/telegram/test"):
                return self._telegram_test()
            if route == ("POST", "/api/action/service"):
                return self._action("service")
            if route == ("POST", "/api/action/process"):
                return self._action("process")
            if method == "GET" and path.startswith("/api/"):
                return self._api(path, qs)
            raise HttpError(404, "not found")
        except HttpError as e:
            self._json(e.status, {"error": e.message}, e.headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - never leak internals to the client
            log.exception("unhandled error on %s %s", method, self.path.split("?")[0])
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---- pages and static files ----------------------------------------------------------

    def _page(self, name: str, redirect_if_signed_in: bool = False) -> None:
        if redirect_if_signed_in and self._session():
            return self._redirect("/")
        self._send(200, (STATIC_DIR / name).read_bytes(), CONTENT_TYPES[".html"])

    def _static(self, rel: str) -> None:
        if "\0" in rel:
            raise HttpError(404, "not found")
        target = (STATIC_DIR / rel).resolve()
        ctype = CONTENT_TYPES.get(target.suffix)
        if STATIC_DIR.resolve() not in target.parents or not target.is_file() or not ctype:
            raise HttpError(404, "not found")
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    # ---- auth ----------------------------------------------------------------------------

    def _login(self) -> None:
        app, ip = self.app, self.client_ip()
        self._check_origin()
        wait = app.limiter.retry_after(ip)
        if wait:
            raise HttpError(429, "too many failed attempts, try again later", {"Retry-After": str(wait)})
        body = self._read_json()
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > MAX_PASSWORD_LEN:
            raise HttpError(400, "username and password are required")

        auth = app.cfg["auth"]
        user_ok = hmac.compare_digest(username.encode(), auth["username"].encode())
        pass_ok = verify_password(password, auth["password_hash"] if user_ok else app.dummy_hash)
        if not (user_ok and pass_ok):
            app.limiter.record_failure(ip)
            # Log whether the username matched, never the text: people paste passwords into the wrong box.
            log.warning("failed login from %s (username %s)", ip, "matched" if user_ok else "did not match")
            app.history.add_event("login_failed", ip, "warn", f"failed login from {ip}")
            if app.alerts and app.limiter.retry_after(ip):
                app.alerts.event(f"lockout:{ip}", f"dashboard sign-in locked after repeated failures from {ip}", "warn", cooldown_s=3600)
            raise HttpError(401, "wrong username or password")

        app.limiter.record_success(ip)
        token, csrf = app.sessions.create(ip, self.headers.get("User-Agent", ""))
        app.history.add_event("login", ip, "info", f"signed in from {ip}")
        log.info("login from %s", ip)
        if app.alerts:
            app.alerts.dashboard_login(ip, self.headers.get("User-Agent", ""))
        cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={app.sessions.ttl}; Secure; HttpOnly; SameSite=Lax"
        self._json(200, {"ok": True, "csrf": csrf}, {"Set-Cookie": cookie})

    def _logout(self) -> None:
        session = self._need_session(post=True)
        self.app.sessions.revoke(session["token"])
        expired = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax"
        self._json(200, {"ok": True}, {"Set-Cookie": expired})

    # ---- alerts and Telegram ---------------------------------------------------------------

    def _alert_mute(self) -> None:
        self._need_session(post=True)
        body, app = self._read_json(), self.app
        alert_id, minutes = body.get("id"), body.get("minutes")
        if not isinstance(alert_id, str) or not alert_id or len(alert_id) > 200:
            raise HttpError(400, "id is required")
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= 7 * 24 * 60):
            raise HttpError(400, "minutes must be between 1 and 10080, or omitted to mute until the alert clears")
        if not app.alerts or alert_id not in {a["id"] for a in app.alerts.snapshot()}:
            raise HttpError(404, "no such active alert")
        app.alerts.mute(alert_id, minutes)
        app.history.add_event("action", alert_id, "info", f"muted {'until it clears' if minutes is None else f'for {int(minutes)} min'}")
        self._json(200, {"ok": True, "alerts": app.alerts.snapshot()})

    def _alert_unmute(self) -> None:
        self._need_session(post=True)
        body, app = self._read_json(), self.app
        alert_id = body.get("id")
        if not isinstance(alert_id, str) or not alert_id or not app.alerts:
            raise HttpError(400, "id is required")
        app.alerts.unmute(alert_id)
        self._json(200, {"ok": True, "alerts": app.alerts.snapshot()})

    def _telegram_test(self) -> None:
        self._need_session(post=True)
        app = self.app
        if not app.alerts:
            raise HttpError(404, "alerts are not enabled")
        ok, error = app.alerts.notifier.send_now(f"✅ <b>{html.escape(app.alerts.server_name)}</b>: test alert from the admin dashboard")
        app.history.add_event("action", "telegram-test", "info" if ok else "warn", "test alert sent" if ok else f"test alert failed: {error}")
        self._json(200, {"ok": ok, "error": error})

    # ---- actions ---------------------------------------------------------------------------

    def _action(self, kind: str) -> None:
        """Start, stop, restart a service or signal a process. All the safety rules live in actions.py."""
        self._need_session(post=True)
        body, app = self._read_json(), self.app
        if not app.actions:
            raise HttpError(404, "actions are not enabled")
        try:
            result = app.actions.perform(kind, body, {"user": app.cfg["auth"]["username"], "ip": self.client_ip()})
        except ActionError as e:
            raise HttpError(e.status, e.message) from None
        self._json(200, result)

    # ---- API -----------------------------------------------------------------------------

    def _api(self, path: str, qs: dict) -> None:
        session = self._need_session()
        app = self.app
        one = lambda key, default="": qs.get(key, [default])[0]   # noqa: E731

        if path == "/api/session":
            return self._json(200, {"user": app.cfg["auth"]["username"], "csrf": session["csrf"],
                                    "version": __version__, "host": app.public_host,
                                    "expires": session["expires"], "server_time": round(time.time()),
                                    "idle_s": app.sessions.idle,
                                    "thresholds": app.cfg["thresholds"],
                                    "actions": app.actions is not None, "admin": app.admin,
                                    "protected": sorted(app.actions.protected) if app.actions else []})

        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/"):]
            job = app.actions.jobs.get(job_id) if app.actions and re.fullmatch(r"[0-9a-f]{16}", job_id) else None
            if job is None:
                raise HttpError(404, "no such job")
            return self._json(200, job)

        if path == "/api/live":
            tab = one("tab", "overview")
            if tab not in TABS:
                raise HttpError(400, f"unknown tab; use one of {', '.join(TABS)}")
            if tab == "processes":
                app.scheduler.touch("processes")
            sections = {}
            for name in TABS[tab]:
                t, data = app.scheduler.get(name)
                sections[name] = None if t is None else {"t": round(t, 1), "data": data}
            return self._json(200, {"tab": tab, "now": round(time.time(), 1), "sections": sections,
                                    "alerts": app.alerts.snapshot() if app.alerts else evaluate(app.cfg, app.scheduler.snapshot())})

        if path == "/api/history":
            metrics = [m for m in one("metrics").split(",") if m][:MAX_METRICS]
            if not metrics or any(len(m) > 80 for m in metrics):
                raise HttpError(400, "metrics is required (comma separated, up to 80 characters each)")
            range_s = RANGES.get(one("range", "1h"))
            if range_s is None:
                raise HttpError(400, f"range must be one of {', '.join(RANGES)}")
            result = app.history.query(metrics, range_s)
            result["events"] = app.history.events(since=result["end"] - range_s)
            return self._json(200, result)

        if path == "/api/alerts":
            kinds = ("alert", "alert_resolved", "event")
            return self._json(200, {"alerts": app.alerts.snapshot() if app.alerts else [],
                                    "events": app.history.events(since=time.time() - 7 * 86400, limit=100, kinds=kinds),
                                    "telegram": app.alerts.status() if app.alerts else {"configured": False}})

        if path == "/api/audit":
            limit = int(one("limit", "50")) if one("limit", "50").isdigit() else 50
            return self._json(200, {"entries": app.actions.audit.tail(max(1, min(limit, 200))) if app.actions else []})

        if path == "/api/metrics":
            return self._json(200, {"metrics": app.history.metric_names()})

        if path == "/api/logs":
            return self._unit_logs(one("kind"), one("name"), one("lines", "200"))

        raise HttpError(404, "not found")

    def _unit_logs(self, kind: str, name: str, lines: str) -> None:
        """Last log lines of a watched systemd unit or a known supervisor program (never arbitrary names)."""
        app = self.app
        if kind == "systemd":
            allowed = {u.removesuffix(".service") for u in app.cfg["watch"]["systemd"]}
            name = name.removesuffix(".service")
        elif kind == "supervisor":
            _t, data = app.scheduler.get("supervisor")
            allowed = {p["name"] for p in (data or {}).get("programs", [])}
        else:
            raise HttpError(400, "kind must be systemd or supervisor")
        if name not in allowed:
            raise HttpError(404, "unknown unit or program")
        try:
            out = unit_logs(kind, name, int(lines) if lines.isdigit() else 200)
        except CommandError as e:
            raise HttpError(502, str(e)) from None
        self._json(200, {"kind": kind, "name": name, "lines": out})


def make_server(app: App, max_connections: int = MAX_CONNECTIONS) -> Server:
    host, _, port = app.cfg["listen"].rpartition(":")
    return Server((host, int(port)), app, max_connections)
