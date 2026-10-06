#!/usr/bin/env python3
"""Tiny authoritative DNS responder for a single name, used for Tailscale split DNS.

Public DNS points admin.example.com at the server's public address (that is what lets Let's
Encrypt validate the name). Tailscale devices are told to ask THIS responder for that one name
instead, and get the server's Tailscale address, so their traffic goes through the VPN.

It is deliberately small and does as little as possible:
  * answers A (and AAAA, only if an IPv6 address is configured) for exactly one name;
  * any other type for that name gets an empty answer with an SOA record (negative caching);
  * every other name gets REFUSED, there is no recursion and no forwarding;
  * never answers a packet that is itself a response, so it cannot be used for reflection loops.

    python3 admin_dns.py --bind 100.64.0.1 --a 100.64.0.1      # serve
    python3 admin_dns.py --test 100.64.0.1 --a 100.64.0.1      # ask a running server
"""

import argparse
import ipaddress
import logging
import signal
import socket
import struct
import sys
import threading
import time

log = logging.getLogger("admin-dns")

A, SOA, AAAA, OPT, ANY = 1, 6, 28, 41, 255
IN = 1
NOERROR, FORMERR, NOTIMP, REFUSED = 0, 1, 4, 5
TTL = 300             # A/AAAA: Tailscale addresses are stable
NEGATIVE_TTL = 60     # how long resolvers may cache "no such record"
EDNS_PAYLOAD = 1232   # advertised UDP size; every answer here is far smaller
MAX_PACKET = 4096
TCP_SLOTS = 16
POLL_S = 1.0           # how often the serving loops look at the stop flag
IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)   # lets us bind before tailscale0 exists at boot


def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.rstrip(".").split(".")) + b"\0"


class Zone:
    def __init__(self, name: str, a: str, aaaa: str | None = None):
        self.name = name.lower().rstrip(".")
        self.labels = tuple(label.encode("ascii") for label in self.name.split("."))
        self.a = ipaddress.IPv4Address(a).packed
        self.aaaa = ipaddress.IPv6Address(aaaa).packed if aaaa else None
        # SOA only exists so that "no data" answers can be cached; its values carry no meaning.
        self.soa = (encode_name(self.name) + encode_name("hostmaster." + self.name)
                    + struct.pack("!5I", 1, 3600, 600, 86400, NEGATIVE_TTL))


class FormatError(Exception):
    pass


def _read_labels(data: bytes, off: int) -> tuple[tuple[bytes, ...], int]:
    """Read an uncompressed name. Compression pointers are not valid inside a question."""
    labels, total = [], 0
    while True:
        if off >= len(data):
            raise FormatError("name runs past the packet")
        n = data[off]
        off += 1
        if n == 0:
            return tuple(labels), off
        if n & 0xC0 or off + n > len(data):
            raise FormatError("bad label")
        total += n + 1
        if total > 255:
            raise FormatError("name too long")
        labels.append(data[off:off + n].lower())
        off += n


def _skip_name(data: bytes, off: int) -> int:
    """Skip a (possibly compressed) name in a resource record."""
    while True:
        if off >= len(data):
            raise FormatError("name runs past the packet")
        n = data[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:
            return off + 2
        if n & 0xC0:
            raise FormatError("bad label")
        off += n + 1


def _has_opt(data: bytes, off: int, count: int) -> bool:
    """True if the additional section carries an EDNS0 OPT record; malformed means no."""
    try:
        for _ in range(count):
            off = _skip_name(data, off)
            rtype, _cls, _ttl, rdlen = struct.unpack_from("!HHIH", data, off)
            if rtype == OPT:
                return True
            off += 10 + rdlen
    except (FormatError, struct.error):
        pass
    return False


def respond(zone: Zone, data: bytes) -> bytes | None:
    """Build the reply to one query packet, or None if it should be ignored."""
    if len(data) < 12:
        return None
    qid, flags, qdcount, ancount, nscount, arcount = struct.unpack_from("!6H", data)
    if flags & 0x8000:
        return None                                    # a response: never answer those
    opcode, rd = (flags >> 11) & 0xF, flags & 0x0100

    def header(rcode, qd=0, an=0, ns=0, ar=0, aa=False):
        return struct.pack("!6H", qid, 0x8000 | (opcode << 11) | (0x0400 if aa else 0) | rd | rcode, qd, an, ns, ar)

    if opcode != 0:
        return header(NOTIMP)
    if qdcount != 1:
        return header(FORMERR)
    try:
        labels, off = _read_labels(data, 12)
        qtype, qclass = struct.unpack_from("!HH", data, off)
    except (FormatError, struct.error):
        return header(FORMERR)
    question = data[12:off + 4]                         # echoed byte for byte (keeps 0x20 case randomisation)
    edns = _has_opt(data, off + 4, ancount + nscount + arcount)
    opt = b"\0" + struct.pack("!HHIH", OPT, EDNS_PAYLOAD, 0, 0) if edns else b""

    if labels != zone.labels or qclass not in (IN, 255):
        return header(REFUSED, qd=1, ar=1 if edns else 0) + question + opt

    answers, count = b"", 0
    if qtype in (A, ANY):
        answers += b"\xc0\x0c" + struct.pack("!HHIH", A, IN, TTL, 4) + zone.a
        count += 1
    if zone.aaaa and qtype in (AAAA, ANY):
        answers += b"\xc0\x0c" + struct.pack("!HHIH", AAAA, IN, TTL, 16) + zone.aaaa
        count += 1
    authority, ns = b"", 0
    if not count:                                       # the name exists, this type does not: NODATA
        authority = b"\xc0\x0c" + struct.pack("!HHIH", SOA, IN, NEGATIVE_TTL, len(zone.soa)) + zone.soa
        ns = 1
    return header(NOERROR, qd=1, an=count, ns=ns, ar=1 if edns else 0, aa=True) + question + answers + authority + opt


# ---- network loops -----------------------------------------------------------------------------

_last_refused_log = 0.0


def _note_refused(data: bytes, addr) -> None:
    """Log refused names, at most once every 10 seconds (the name is attacker-controlled text)."""
    global _last_refused_log
    now = time.monotonic()
    if now - _last_refused_log < 10:
        return
    _last_refused_log = now
    try:
        labels, _ = _read_labels(data, 12)
        name = b".".join(labels)[:100].decode("ascii", "replace")
    except FormatError:
        name = "<malformed>"
    log.info("refused %r from %s", name, addr[0])


def _answer(zone: Zone, data: bytes, addr) -> bytes | None:
    reply = respond(zone, data)
    if reply and (struct.unpack_from("!H", reply, 2)[0] & 0xF) == REFUSED:
        _note_refused(data, addr)
    return reply


def serve_udp(sock: socket.socket, zone: Zone, stop: threading.Event) -> None:
    sock.settimeout(POLL_S)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(MAX_PACKET)
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            time.sleep(0.1)
            continue
        try:
            reply = _answer(zone, data, addr)
            if reply:
                sock.sendto(reply, addr)
        except OSError:
            pass                                        # the client went away; nothing to do


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def _handle_tcp(conn: socket.socket, addr, zone: Zone, slots: threading.BoundedSemaphore) -> None:
    try:
        conn.settimeout(5)
        for _ in range(16):                             # a few queries per connection, then hang up
            length = int.from_bytes(_recv_exact(conn, 2), "big")
            if not 12 <= length <= MAX_PACKET:
                return
            reply = _answer(zone, _recv_exact(conn, length), addr)
            if reply is None:
                return
            conn.sendall(len(reply).to_bytes(2, "big") + reply)
    except (OSError, EOFError):
        pass
    finally:
        conn.close()
        slots.release()


def serve_tcp(sock: socket.socket, zone: Zone, stop: threading.Event) -> None:
    sock.settimeout(POLL_S)
    slots = threading.BoundedSemaphore(TCP_SLOTS)
    while not stop.is_set():
        try:
            conn, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                return
            time.sleep(0.1)
            continue
        if not slots.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(target=_handle_tcp, args=(conn, addr, zone, slots), daemon=True).start()


def bind(kind: int, host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, kind)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_IP, IP_FREEBIND, 1)
    sock.bind((host, port))
    if kind == socket.SOCK_STREAM:
        sock.listen(16)          # from here on connections queue up, even before the serving thread runs
    return sock


# ---- client side (used by --test and the unit tests) ------------------------------------------

def build_query(name: str, qtype: int, qid: int = 0x1234, edns: bool = True, rd: bool = True) -> bytes:
    packet = struct.pack("!6H", qid, 0x0100 if rd else 0, 1, 0, 0, 1 if edns else 0)
    packet += encode_name(name) + struct.pack("!HH", qtype, IN)
    if edns:
        packet += b"\0" + struct.pack("!HHIH", OPT, EDNS_PAYLOAD, 0, 0)
    return packet


def parse_response(data: bytes) -> dict:
    qid, flags, qd, an, ns, ar = struct.unpack_from("!6H", data)
    off = 12
    for _ in range(qd):
        _, off = _read_labels(data, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        off = _skip_name(data, off)
        rtype, _cls, ttl, rdlen = struct.unpack_from("!HHIH", data, off)
        rdata = data[off + 10:off + 10 + rdlen]
        records.append((rtype, ttl, rdata))
        off += 10 + rdlen
    answers = records[:an]
    return {
        "id": qid, "flags": flags, "rcode": flags & 0xF, "aa": bool(flags & 0x0400), "rd": bool(flags & 0x0100),
        "qr": bool(flags & 0x8000), "questions": qd,
        "answers": [(t, ttl, str(ipaddress.ip_address(r)) if t in (A, AAAA) else r) for t, ttl, r in answers],
        "authority": [r[0] for r in records[an:an + ns]],
        "has_opt": any(r[0] == OPT for r in records[an + ns:]),
    }


def ask(host: str, port: int, name: str, qtype: int, tcp: bool = False, timeout: float = 3.0, **kw) -> dict:
    query = build_query(name, qtype, **kw)
    if tcp:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(len(query).to_bytes(2, "big") + query)
            return parse_response(_recv_exact(s, int.from_bytes(_recv_exact(s, 2), "big")))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query, (host, port))
        return parse_response(s.recvfrom(MAX_PACKET)[0])


def run_test(target: str, name: str, expect_a: str) -> int:
    host, _, port = target.partition(":")
    port = int(port or 53)
    ok = True
    for tcp in (False, True):
        label = "TCP" if tcp else "UDP"
        try:
            r = ask(host, port, name, A, tcp=tcp)
        except (OSError, EOFError, struct.error) as e:
            print(f"{label}: no usable answer from {host}:{port}: {e}")
            ok = False
            continue
        got = [a[2] for a in r["answers"] if a[0] == A]
        good = r["rcode"] == NOERROR and got == [expect_a]
        ok &= good
        print(f"{label}: {name} A -> {got or 'nothing'} (rcode {r['rcode']}) {'OK' if good else 'UNEXPECTED'}")
    other = ask(host, port, "example.com", A)
    refused = other["rcode"] == REFUSED
    ok &= refused
    print(f"other names: rcode {other['rcode']} {'OK (refused)' if refused else 'UNEXPECTED'}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--name", required=True, help="the one host name to answer for, for example admin.example.com")
    p.add_argument("--a", required=True, help="IPv4 address to answer with (the server's Tailscale address)")
    p.add_argument("--aaaa", help="optional IPv6 answer; leave unset so every client uses IPv4")
    p.add_argument("--bind", help="address to listen on (the server's Tailscale address)")
    p.add_argument("--port", type=int, default=53)
    p.add_argument("--test", metavar="HOST[:PORT]", help="query a running responder and check its answers")
    args = p.parse_args()
    if args.test:
        return run_test(args.test, args.name, args.a)
    if not args.bind:
        p.error("--bind is required to serve")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    zone = Zone(args.name, args.a, args.aaaa)
    stop = threading.Event()
    udp, tcp = bind(socket.SOCK_DGRAM, args.bind, args.port), bind(socket.SOCK_STREAM, args.bind, args.port)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    threads = [threading.Thread(target=serve_udp, args=(udp, zone, stop), daemon=True),
               threading.Thread(target=serve_tcp, args=(tcp, zone, stop), daemon=True)]
    for t in threads:
        t.start()
    log.info("answering %s -> %s%s on %s:%d (udp+tcp)", zone.name, args.a,
             f" / {args.aaaa}" if args.aaaa else "", args.bind, args.port)
    stop.wait()
    for t in threads:
        t.join(timeout=3)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
