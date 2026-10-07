"""Interface traffic, listening ports with owners, TCP states and Tailscale peers."""

import ipaddress
import json
import re
import socket
import struct
from collections import Counter
from datetime import datetime

from ..util import CommandError, Delta, rate, read_text, run

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
    "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}


def _net_dev() -> dict:
    out = {}
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if name == "lo":
            continue
        f = list(map(int, data.split()))
        out[name] = {"rx_bytes": f[0], "rx_packets": f[1], "rx_errors": f[2], "rx_drops": f[3],
                     "tx_bytes": f[8], "tx_packets": f[9], "tx_errors": f[10], "tx_drops": f[11]}
    return out


class NetIO:
    cadence = "fast"

    def __init__(self):
        self._delta = Delta()

    def collect(self, cfg: dict) -> dict:
        stats = _net_dev()
        prev, elapsed = self._delta.update(stats)
        ifaces = []
        for name, s in stats.items():
            try:
                up = read_text(f"/sys/class/net/{name}/operstate").strip() in ("up", "unknown")
            except OSError:
                continue                                            # the interface vanished between the two reads
            row = {"name": name, **s, "up": up,
                   "rx_Bps": None, "tx_Bps": None, "rx_pps": None, "tx_pps": None}
            p = prev.get(name) if prev else None
            if p:
                for key, counter in (("rx_Bps", "rx_bytes"), ("tx_Bps", "tx_bytes"),
                                     ("rx_pps", "rx_packets"), ("tx_pps", "tx_packets")):
                    row[key] = round(rate(s[counter], p[counter], elapsed), 1)
            ifaces.append(row)
        return {"interfaces": ifaces}


def _exposure(addr: str) -> str:
    """Who can reach a socket bound to this address (before any firewall)."""
    if addr in ("*", "0.0.0.0", "::"):
        return "all interfaces"
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return "unknown"
    if ip.is_loopback:
        return "localhost"
    if ip in TAILNET_V4 or ip in TAILNET_V6:
        return "tailnet"
    if ip.is_private or ip.is_link_local:
        return "private"
    return "public"


_SS_USER = re.compile(r'\("([^"]*)",pid=(\d+)')


def _listeners() -> list[dict]:
    """`ss -tulpnH`. Process names need root; without it the owner is blank."""
    rows, seen = [], set()
    for line in run(["ss", "-tulpnH"]).splitlines():
        f = line.split(None, 6)
        if len(f) < 5:
            continue
        proto, local = f[0], f[4]
        addr, _, port = local.rpartition(":")
        addr = addr.strip("[]").split("%", 1)[0]
        owners = sorted({(name, int(pid)) for name, pid in _SS_USER.findall(f[6] if len(f) > 6 else "")})
        key = (proto, addr, port)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"proto": proto, "address": addr, "port": int(port) if port.isdigit() else port,
                     "exposure": _exposure(addr),
                     "process": ", ".join(sorted({n for n, _ in owners})),
                     "pids": [p for _, p in owners]})
    rows.sort(key=lambda r: (r["port"] if isinstance(r["port"], int) else 0, r["proto"]))
    return rows


def _hex_ip(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    # IPv6 is four 32-bit words, each in host (little-endian) order.
    words = struct.unpack("<4I", raw)
    v6 = ipaddress.IPv6Address(struct.pack(">4I", *words))
    return str(v6.ipv4_mapped or v6)


def _tcp_connections() -> tuple[Counter, Counter]:
    states, remotes = Counter(), Counter()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = read_text(path).splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            state = TCP_STATES.get(f[3], f[3])
            states[state] += 1
            if state == "ESTABLISHED":
                ip = _hex_ip(f[2].split(":")[0])
                if not ipaddress.ip_address(ip).is_loopback:
                    remotes[ip] += 1
    return states, remotes


def _addresses() -> list[dict]:
    out = []
    for iface in json.loads(run(["ip", "-j", "addr"])):
        if iface["ifname"] == "lo":
            continue
        out.append({"name": iface["ifname"],
                    "addresses": [f'{a["local"]}/{a["prefixlen"]}' for a in iface.get("addr_info", [])
                                  if a.get("scope") != "link"]})
    return out


class Sockets:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        states, remotes = _tcp_connections()
        return {
            "addresses": _addresses(),
            "listening": _listeners(),
            "tcp_states": dict(states.most_common()),
            "top_remotes": [{"ip": ip, "connections": n} for ip, n in remotes.most_common(15)],
        }


def _ts(value: str | None) -> int | None:
    """Tailscale timestamps are RFC3339; the zero value means 'never'."""
    if not value or value.startswith("0001-"):
        return None
    try:
        return round(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


class Tailscale:
    cadence = "medium"

    def collect(self, cfg: dict) -> dict:
        try:
            st = json.loads(run(["tailscale", "status", "--json"]))
        except CommandError as e:
            return {"error": str(e)}
        me = st.get("Self", {})
        peers = []
        for p in (st.get("Peer") or {}).values():
            peers.append({
                "name": p.get("HostName", ""),
                "dns": p.get("DNSName", "").rstrip("."),
                "os": p.get("OS", ""),
                "ips": p.get("TailscaleIPs", []),
                "online": bool(p.get("Online")),
                "last_seen": _ts(p.get("LastSeen")),
                "last_handshake": _ts(p.get("LastHandshake")),
                "connection": "direct" if p.get("CurAddr") else (f"relay {p['Relay']}" if p.get("Relay") else ""),
                "rx_bytes": p.get("RxBytes", 0),
                "tx_bytes": p.get("TxBytes", 0),
                "exit_node": bool(p.get("ExitNode")),
            })
        peers.sort(key=lambda p: (not p["online"], p["name"]))
        return {
            "state": st.get("BackendState", ""),
            "version": st.get("Version", "").split("-")[0],
            "self": {"name": me.get("HostName", ""), "dns": me.get("DNSName", "").rstrip("."),
                     "ips": me.get("TailscaleIPs", []), "online": bool(me.get("Online"))},
            "magic_dns_suffix": st.get("MagicDNSSuffix", ""),
            "peers": peers,
        }
