import os
import tempfile
import unittest

from dashboard import auth


class PasswordTest(unittest.TestCase):
    def test_hash_and_verify(self):
        h = auth.hash_password("correct horse battery")
        self.assertTrue(auth.verify_password("correct horse battery", h))
        self.assertFalse(auth.verify_password("Correct horse battery", h))
        self.assertFalse(auth.verify_password("x", "not$a$valid$hash"))
        self.assertFalse(auth.verify_password("x", ""))
        self.assertNotEqual(h, auth.hash_password("correct horse battery"))   # random salt


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "auth.db")

    def tearDown(self):
        self.dir.cleanup()

    def open(self, fp):
        s = auth.Sessions(self.path, fp, hours=1)
        self.addCleanup(s.close)
        return s

    def test_create_lookup_revoke(self):
        s = self.open("fp1")
        token, csrf = s.create("1.2.3.4", "agent")
        self.assertEqual(s.lookup(token)["csrf"], csrf)
        self.assertIsNone(s.lookup(token + "x"))
        self.assertIsNone(s.lookup(""))
        self.assertIsNone(s.lookup("a" * 500))
        s.revoke(token)
        self.assertIsNone(s.lookup(token))

    def test_token_is_not_stored_in_the_clear_and_file_is_private(self):
        s = auth.Sessions(self.path, "fp1", hours=1)
        token, csrf = s.create("1.2.3.4", "agent")
        s.close()
        with open(self.path, "rb") as f:
            self.assertNotIn(token.encode(), f.read())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_survives_restart_but_not_a_credential_change(self):
        s = auth.Sessions(self.path, "fp1", hours=1)
        token, _ = s.create("1.2.3.4", "agent")
        s.close()
        same = auth.Sessions(self.path, "fp1", hours=1)
        self.assertIsNotNone(same.lookup(token))
        same.close()
        changed = auth.Sessions(self.path, "fp2", hours=1)
        self.assertIsNone(changed.lookup(token))
        changed.close()

    def test_expiry(self):
        s = self.open("fp1")
        token, _ = s.create("1.2.3.4", "agent")
        s._db.execute("UPDATE sessions SET expires = 1")
        self.assertIsNone(s.lookup(token))
        self.assertEqual(s.count(), 0)

    def test_session_cap_drops_the_least_recently_used(self):
        s = self.open("fp1")
        tokens = [s.create("1.2.3.4", "a")[0] for _ in range(auth.MAX_SESSIONS + 3)]
        self.assertEqual(s.count(), auth.MAX_SESSIONS)
        self.assertIsNotNone(s.lookup(tokens[-1]))


class LimiterTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.lim = auth.LoginLimiter(per_ip=3, overall=5, window=100, clock=lambda: self.now)

    def test_per_ip_limit_and_recovery(self):
        for _ in range(3):
            self.assertEqual(self.lim.retry_after("a"), 0)
            self.lim.record_failure("a")
        wait = self.lim.retry_after("a")
        self.assertTrue(0 < wait <= 101)
        self.assertEqual(self.lim.retry_after("b"), 0)
        self.now += 101
        self.assertEqual(self.lim.retry_after("a"), 0)

    def test_overall_limit_defeats_rotating_addresses(self):
        for i in range(5):
            self.lim.record_failure(f"ip{i}")
        self.assertGreater(self.lim.retry_after("brand-new"), 0)

    def test_success_clears_that_address_only(self):
        for _ in range(3):
            self.lim.record_failure("a")
        self.lim.record_success("a")
        self.assertEqual(self.lim.retry_after("a"), 0)
        self.assertEqual(len(self.lim._all), 3)    # still counts toward the overall limit


if __name__ == "__main__":
    unittest.main()
