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
