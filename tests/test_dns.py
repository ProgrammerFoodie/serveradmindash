import random
import socket
import struct
import threading
import unittest

from dnsd import admin_dns as dns

NAME = "admin.example.com"
IP4 = "100.64.0.1"
IP6 = "fd7a:115c:a1e0::1"


def free_port():
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as t:
            try:
                t.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


class ServerCase(unittest.TestCase):
    aaaa = None

    def setUp(self):
        dns.POLL_S = 0.1
        self.zone = dns.Zone(NAME, IP4, self.aaaa)
        self.port = free_port()
        self.stop = threading.Event()
        self.udp = dns.bind(socket.SOCK_DGRAM, "127.0.0.1", self.port)
        self.tcp = dns.bind(socket.SOCK_STREAM, "127.0.0.1", self.port)
        self.threads = [threading.Thread(target=dns.serve_udp, args=(self.udp, self.zone, self.stop), daemon=True),
                        threading.Thread(target=dns.serve_tcp, args=(self.tcp, self.zone, self.stop), daemon=True)]
        for t in self.threads:
            t.start()

    def tearDown(self):
        self.stop.set()
        for t in self.threads:
            t.join(3)
        self.udp.close()
        self.tcp.close()

    def ask(self, name, qtype, tcp=False, **kw):
        return dns.ask("127.0.0.1", self.port, name, qtype, tcp=tcp, **kw)


class AnswerTest(ServerCase):
    def test_a_record_over_udp_and_tcp(self):
        for tcp in (False, True):
            r = self.ask(NAME, dns.A, tcp=tcp)
            self.assertEqual((r["rcode"], r["qr"], r["aa"], r["rd"], r["id"]), (0, True, True, True, 0x1234))
            self.assertEqual(r["answers"], [(dns.A, 300, IP4)])

    def test_name_match_ignores_case_and_trailing_dot_but_not_subdomains(self):
        self.assertEqual(self.ask("ADMIN.Example.COM", dns.A)["answers"], [(dns.A, 300, IP4)])
        for other in ("x.admin.example.com", "example.com", "app1.example.com", "example.com", "lv"):
            r = self.ask(other, dns.A)
            self.assertEqual((r["rcode"], r["answers"], r["aa"]), (dns.REFUSED, [], False), other)

    def test_other_types_are_empty_answers_with_soa(self):
        for qtype in (16, 15, 2, 6, 65, 33):               # TXT MX NS SOA HTTPS SRV
            r = self.ask(NAME, qtype)
            self.assertEqual((r["rcode"], r["answers"], r["authority"]), (0, [], [dns.SOA]), qtype)

    def test_aaaa_is_empty_unless_configured(self):
        r = self.ask(NAME, dns.AAAA)
        self.assertEqual((r["rcode"], r["answers"]), (0, []))

    def test_any_returns_the_address(self):
        self.assertEqual(self.ask(NAME, dns.ANY)["answers"], [(dns.A, 300, IP4)])

    def test_edns_is_echoed_only_when_asked_for(self):
        self.assertTrue(self.ask(NAME, dns.A, edns=True)["has_opt"])
        self.assertFalse(self.ask(NAME, dns.A, edns=False)["has_opt"])
        self.assertTrue(self.ask("example.com", dns.A, edns=True)["has_opt"])

    def test_rd_flag_is_echoed(self):
        self.assertFalse(self.ask(NAME, dns.A, rd=False)["rd"])

    def test_question_is_echoed_byte_for_byte(self):
        query = dns.build_query("AdMiN.example.COM", dns.A)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            s.sendto(query, ("127.0.0.1", self.port))
            reply = s.recvfrom(4096)[0]
        self.assertIn(b"\x05AdMiN\x07example\x03COM\x00", reply)

    def test_tcp_serves_several_queries_per_connection(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            for i in range(3):
                q = dns.build_query(NAME, dns.A, qid=i)
                s.sendall(len(q).to_bytes(2, "big") + q)
                r = dns.parse_response(dns._recv_exact(s, int.from_bytes(dns._recv_exact(s, 2), "big")))
                self.assertEqual((r["id"], r["answers"][0][2]), (i, IP4))

    def test_oversized_tcp_length_is_dropped(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall((60000).to_bytes(2, "big") + b"x" * 10)
            try:
                self.assertEqual(s.recv(10), b"")              # server hung up (clean close)...
            except ConnectionResetError:
                pass                                           # ...or reset, because unread bytes were left over
        self.assertEqual(self.ask(NAME, dns.A)["rcode"], 0)    # and is still alive


class Ipv6Test(ServerCase):
    aaaa = IP6

    def test_aaaa_when_configured(self):
        self.assertEqual(self.ask(NAME, dns.AAAA)["answers"], [(dns.AAAA, 300, IP6)])
        self.assertEqual(len(self.ask(NAME, dns.ANY)["answers"]), 2)


class MalformedInputTest(unittest.TestCase):
    zone = dns.Zone(NAME, IP4)

    def test_rejections(self):
        q = dns.build_query(NAME, dns.A)
        response_flag = struct.pack("!H", 0x8100) + q[2:]
        self.assertIsNone(dns.respond(self.zone, q[:2] + struct.pack("!H", 0x8100) + q[4:]))   # a response: ignored
        self.assertIsNone(dns.respond(self.zone, b""))
        self.assertIsNone(dns.respond(self.zone, b"\x00" * 11))
        notimp = dns.respond(self.zone, q[:2] + struct.pack("!H", 0x2800) + q[4:])             # opcode 5 (UPDATE)
        self.assertEqual(struct.unpack_from("!H", notimp, 2)[0] & 0xF, dns.NOTIMP)
        two = q[:4] + struct.pack("!H", 2) + q[6:]
        self.assertEqual(struct.unpack_from("!H", dns.respond(self.zone, two), 2)[0] & 0xF, dns.FORMERR)
        pointer = q[:12] + b"\xc0\x0c" + struct.pack("!HH", 1, 1)                              # compression in a question
        self.assertEqual(struct.unpack_from("!H", dns.respond(self.zone, pointer), 2)[0] & 0xF, dns.FORMERR)
        runaway = q[:12] + b"\x3f" + b"a" * 20                                                 # label runs off the end
        self.assertEqual(struct.unpack_from("!H", dns.respond(self.zone, runaway), 2)[0] & 0xF, dns.FORMERR)
        self.assertIsNotNone(response_flag)

    def test_fuzz_never_raises_and_never_grows(self):
        rng = random.Random(7)
        base = dns.build_query(NAME, dns.A)
        for _ in range(20000):
            data = bytearray(base)
            for _ in range(rng.randint(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            if rng.random() < 0.3:
                data = data[:rng.randrange(len(data) + 1)]
            if rng.random() < 0.1:
                data += bytes(rng.randrange(256) for _ in range(rng.randint(0, 40)))
            reply = dns.respond(self.zone, bytes(data))
            if reply is not None:
                self.assertGreaterEqual(len(reply), 12)
                self.assertEqual(reply[:2], bytes(data[:2]))
                self.assertLess(len(reply), 160)                  # no amplification


if __name__ == "__main__":
    unittest.main()
