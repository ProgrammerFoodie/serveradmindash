import unittest

from dashboard.collectors.logs import firewall_block


class FirewallLineTest(unittest.TestCase):
    def test_fields_are_extracted(self):
        line = ("[UFW BLOCK] IN=eth0 OUT= MAC=22:00:c4 SRC=198.51.100.7 DST=203.0.113.10 LEN=40 "
                "PROTO=TCP SPT=41632 DPT=22 WINDOW=1024 RES=0x00 SYN URGP=0")
        self.assertEqual(firewall_block(line), {"SRC": "198.51.100.7", "PROTO": "TCP", "DPT": "22"})

    def test_other_lines_are_not_firewall_lines(self):
        self.assertIsNone(firewall_block("ssh: error: kex_exchange_identification: read: Connection reset"))
        self.assertIsNone(firewall_block(""))

    def test_a_truncated_line_still_works(self):
        self.assertEqual(firewall_block("[UFW BLOCK] IN=eth0 SRC=1.2.3.4 DST=5.6.7.8 PROTO=UDP"), {"SRC": "1.2.3.4", "PROTO": "UDP"})


class ReadOnlyMountTest(unittest.TestCase):
    SAMPLE = (
        "25 1 8:0 / / rw,relatime shared:1 - ext4 /dev/sda rw,errors=remount-ro\n"
        "30 25 8:32 / /mnt/Extra20 rw,relatime shared:2 - ext4 /dev/sdc rw\n"
        "41 25 7:1 / /snap/core ro,nodev,relatime - squashfs /dev/loop1 ro\n"
        "52 25 8:48 / /mnt/with\\040space rw,relatime - ext4 /dev/sdd ro,errors=continue\n"
        "garbage line\n")

    def test_options_of_the_mount_and_of_the_filesystem_both_count(self):
        from dashboard.collectors.disks import parse_readonly
        self.assertEqual(parse_readonly(self.SAMPLE), {
            "/": False, "/mnt/Extra20": False, "/snap/core": True, "/mnt/with space": True})


if __name__ == "__main__":
    unittest.main()


class SshAuthParserTest(unittest.TestCase):
    def parse(self, message):
        from dashboard.collectors.security import SshAuth
        return SshAuth(path="/nonexistent")._parse(f"2026-10-07T10:00:00+00:00 host sshd-session[123]: {message}")

    def test_ordinary_lines(self):
        self.assertEqual(self.parse("Failed password for root from 198.51.100.7 port 5555 ssh2")[1:], ("failed", "root", "198.51.100.7", "password"))
        self.assertEqual(self.parse("Failed password for invalid user bob from 198.51.100.7 port 5555 ssh2")[1:], ("failed", "bob", "198.51.100.7", "password"))
        self.assertEqual(self.parse("Invalid user bob from 198.51.100.7 port 5555")[1:], ("invalid_user", "bob", "198.51.100.7", ""))
        self.assertEqual(self.parse("Accepted publickey for alice from 100.64.0.10 port 4000 ssh2: ED25519 SHA256:abc")[1:], ("accepted", "alice", "100.64.0.10", "publickey"))

    def test_a_user_name_cannot_forge_the_address(self):
        forged = "x from 203.0.113.9 port 1"
        got = self.parse(f"Invalid user {forged} from 198.51.100.7 port 5555")
        self.assertEqual((got[2], got[3]), (forged, "198.51.100.7"))
        got = self.parse(f"Failed password for invalid user {forged} from 198.51.100.7 port 5555 ssh2")
        self.assertEqual((got[2], got[3]), (forged, "198.51.100.7"))

    def test_an_empty_user_name_is_still_counted_and_long_names_are_cut(self):
        self.assertEqual(self.parse("Invalid user  from 198.51.100.7 port 5555")[2:4], ("", "198.51.100.7"))
        self.assertEqual(len(self.parse("Invalid user " + "a" * 500 + " from 198.51.100.7 port 5555")[2]), 64)

    def test_unrelated_lines_are_ignored(self):
        self.assertIsNone(self.parse("Connection closed by 198.51.100.7 port 5555 [preauth]"))


class RobustnessTest(unittest.TestCase):
    def test_pressure_without_psi_is_empty_not_an_error(self):
        from dashboard import util
        real = util.read_text
        util.read_text = lambda path: (_ for _ in ()).throw(FileNotFoundError(path))
        try:
            self.assertEqual(util.pressure("cpu"), {})
        finally:
            util.read_text = real

    def test_the_journal_read_is_bounded_with_and_without_a_cursor(self):
        from dashboard.collectors import logs
        seen = []
        real = logs.run
        logs.run = lambda args, timeout=5: seen.append(args) or '{"__CURSOR":"c1","__REALTIME_TIMESTAMP":"1","PRIORITY":"x"}\n{"__CURSOR":"c2","__REALTIME_TIMESTAMP":"2000000","PRIORITY":"3","MESSAGE":"ok"}\n'
        try:
            j = logs.Journal()
            j.collect({}); j.collect({})
        finally:
            logs.run = real
        self.assertTrue(all(f"-n{logs.Journal.MAX_ENTRIES}" in a for a in seen))
        self.assertIn("--after-cursor=c2", seen[1])                       # the bad-priority entry did not drop the good one after it
