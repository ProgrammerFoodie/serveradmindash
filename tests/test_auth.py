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

    def age(self, s, seconds):
        """Pretend the last activity was `seconds` ago."""
        s._db.execute("UPDATE sessions SET last_seen = last_seen - ?", (seconds,))

    def test_idle_session_must_sign_in_again(self):
        s = auth.Sessions(self.path, "fp1", hours=1, idle_minutes=15)
        self.addCleanup(s.close)
        token, _ = s.create("1.2.3.4", "agent")
        self.age(s, 14 * 60)
        self.assertIsNotNone(s.lookup(token))              # 14 minutes of silence: still signed in
        self.age(s, 2 * 60)
        self.assertIsNone(s.lookup(token))                 # 16 minutes: gone
        self.assertEqual(s.count(), 0)

    def test_lookup_is_not_activity_but_touch_is(self):
        s = auth.Sessions(self.path, "fp1", hours=1, idle_minutes=15)
        self.addCleanup(s.close)
        token, _ = s.create("1.2.3.4", "agent")
        self.age(s, 10 * 60)
        for _ in range(3):
            s.lookup(token)                                # the page polling must not reset the clock
        left = s.lookup(token)["idle_left"]
        self.assertTrue(289 <= left <= 300, left)
        s.touch(token)
        self.assertTrue(s.lookup(token)["idle_left"] >= 899)
        self.age(s, 10 * 60)
        self.assertIsNotNone(s.lookup(token))              # 10 min after the touch: alive

    def test_idle_sessions_are_dropped_at_startup_and_on_new_login(self):
        s = auth.Sessions(self.path, "fp1", hours=1, idle_minutes=15)
        old, _ = s.create("1.2.3.4", "agent")
        self.age(s, 20 * 60)
        s.close()
        again = auth.Sessions(self.path, "fp1", hours=1, idle_minutes=15)
        self.addCleanup(again.close)
        self.assertEqual(again.count(), 0)                 # a restart does not revive an abandoned session
        fresh, _ = again.create("1.2.3.4", "agent")
        self.age(again, 20 * 60)
        again.create("1.2.3.4", "agent")
        self.assertEqual(again.count(), 1)
        self.assertIsNone(again.lookup(fresh))

    def test_list_active_shows_valid_sign_ins_without_anything_that_could_take_one_over(self):
        s = self.open("fp1")
        old, csrf_old = s.create("1.1.1.1", "Agent one")
        mine, csrf_mine = s.create("2.2.2.2", "Agent two")
        self.age(s, 100)
        s._db.execute("UPDATE sessions SET last_seen = last_seen + 90 WHERE ip = '2.2.2.2'")             # mine was active more recently
        rows = s.list_active(mine)
        self.assertEqual([(r["ip"], r["ua"], r["current"]) for r in rows], [("2.2.2.2", "Agent two", True), ("1.1.1.1", "Agent one", False)])
        for row in rows:
            self.assertRegex(row["id"], r"^[0-9a-f]{12}$")
            self.assertTrue(0 < row["idle_left"] <= s.idle)
        dump = str(rows)
        for secret in (old, mine, csrf_old, csrf_mine, auth._token_hash(mine), auth._token_hash(old)):
            self.assertNotIn(secret, dump)
        self.assertEqual(len({r["id"] for r in rows}), 2)
        self.assertEqual([r["current"] for r in s.list_active()], [False, False])                       # nobody is "you" without a token
        self.assertEqual([r["current"] for r in s.list_active("not-a-token")], [False, False])

    def test_list_active_leaves_out_idle_expired_and_foreign_sign_ins(self):
        s = self.open("fp1")
        idle, _ = s.create("1.1.1.1", "idle")
        gone, _ = s.create("2.2.2.2", "expired")
        fresh, _ = s.create("3.3.3.3", "fresh")
        s._db.execute("UPDATE sessions SET last_seen = last_seen - ? WHERE ip = '1.1.1.1'", (s.idle + 5,))
        s._db.execute("UPDATE sessions SET expires = 1 WHERE ip = '2.2.2.2'")
        self.assertEqual([r["ip"] for r in s.list_active(fresh)], ["3.3.3.3"])
        s._db.execute("UPDATE sessions SET fp = 'other' WHERE ip = '3.3.3.3'")
        self.assertEqual(s.list_active(fresh), [])

    def test_default_idle_limit_is_fifteen_minutes(self):
        self.assertEqual(self.open("fp1").idle, 15 * 60)

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
