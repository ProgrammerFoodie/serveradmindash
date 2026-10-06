import json
import logging
import os
import tempfile
import unittest

from dashboard import config
from dashboard.alerts import TelegramError
from dashboard.alertmanager import AlertManager, Notifier, fmt_duration

BASE = json.loads(config.EXAMPLE_PATH.read_text())


def setUpModule():
    logging.disable(logging.CRITICAL)            # warnings about bad data and failed sends are expected here


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeNotifier:
    def __init__(self, enabled=True):
        self.enabled, self.sent = enabled, []

    def send(self, text):
        if not self.enabled:
            return False
        self.sent.append(text)
        return True

    def status(self):
        return {}


def cfg(**alerts):
    c = json.loads(json.dumps(BASE))
    c["alerts"].update(alerts)
    c["public_host"] = "admin.example.test"
    return c


def disk(pct, mount="/"):
    return {"disks": {"mounts": [{"mount": mount, "used_pct": pct, "inodes_pct": 1.0}]}}


def cpu(busy):
    return {"cpu": {"total": {"busy": busy}, "load_per_core": 0.1, "load": [0.1, 0, 0], "cores": 1}}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class ManagerCase(unittest.TestCase):
    def make(self, notifier=None, path=None, **alerts):
        self.clock = Clock()
        self.notifier = notifier or FakeNotifier()
        self.mgr = AlertManager(cfg(**alerts), self.notifier, state_path=path, clock=self.clock)
        return self.mgr

    def at(self, dt, sections):
        self.clock.t = 1_000_000.0 + dt
        self.mgr.update(sections, self.clock.t)
        return self.notifier.sent


class LifecycleTest(ManagerCase):
    def test_immediate_alert_then_repeat_then_recovery(self):
        self.make()
        sent = self.at(0, disk(97))
        self.assertEqual(len(sent), 1)
        self.assertIn("🔴", sent[0]); self.assertIn("Disk / 97% full", sent[0]); self.assertNotIn(" for 0 s", sent[0]); self.assertIn("CRITICAL", sent[0])
        self.at(600, disk(97))
        self.assertEqual(len(sent), 1, "no repeat inside the repeat window")
        self.at(3600, disk(97))
        self.assertEqual(len(sent), 2)
        self.assertIn("Still CRITICAL for 1 h", sent[1])
        self.at(3610, disk(50))
        self.assertEqual(len(sent), 2, "not resolved yet: it must stay clear for 30 s")
        self.at(3645, disk(50))
        self.assertEqual(len(sent), 3)
        self.assertIn("✅", sent[2]); self.assertIn("recovered", sent[2]); self.assertIn("was CRITICAL for 1 h", sent[2])
        self.assertEqual(self.mgr.snapshot(), [])

    def test_a_condition_shorter_than_for_s_never_alerts(self):
        self.make()
        for t in range(0, 250, 10):
            self.assertEqual(self.at(t, cpu(99)), [], t)            # cpu_pct for_s is 300
        self.at(260, cpu(10))
        self.at(400, cpu(99))
        for t in range(410, 700, 10):
            self.at(t, cpu(99))
        self.assertEqual(len(self.notifier.sent), 0, "the timer restarted when the spike ended")
        self.at(710, cpu(99))
        self.assertEqual(len(self.notifier.sent), 1)                # 300 s of continuous high CPU

    def test_hysteresis_keeps_an_active_alert_until_clearly_ok(self):
        self.make()
        self.at(0, disk(91))                                         # warn at 90
        self.assertEqual(self.mgr.snapshot()[0]["level"], "warn")
        self.at(10, disk(89.5)); self.at(50, disk(89.5))
        self.assertEqual(len(self.mgr.snapshot()), 1, "89.5 is inside the 1-point margin")
        self.at(60, disk(88.9)); self.at(100, disk(88.9))
        self.assertEqual(self.mgr.snapshot(), [])

    def test_escalation_is_announced_deescalation_is_quiet(self):
        self.make()
        self.at(0, disk(91))
        self.assertEqual(len(self.notifier.sent), 1)
        self.at(5, disk(96))
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("Now CRITICAL", self.notifier.sent[1])
        self.at(10, disk(91))
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.mgr.snapshot()[0]["level"], "warn")

    def test_warnings_repeat_four_times_less_often(self):
        self.make()
        self.at(0, disk(91))
        self.at(3600, disk(91))
        self.assertEqual(len(self.notifier.sent), 1)
        self.at(4 * 3600, disk(91))
        self.assertEqual(len(self.notifier.sent), 2)

    def test_stopped_service_alerts_after_30_seconds_but_only_if_enabled(self):
        self.make()
        unit = lambda name, enabled: {"name": name, "unit": name + ".service", "exists": True, "enabled": enabled,
                                      "active": "inactive", "result": "success"}
        s = {"systemd": {"watched": [unit("app1", "enabled"), unit("app2", "disabled")], "failed": []}}
        self.at(0, s); self.at(20, s)
        self.assertEqual(self.notifier.sent, [])
        self.at(31, s)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("app1 is inactive", self.notifier.sent[0]); self.assertIn("🟠", self.notifier.sent[0])
        self.assertNotIn("app2", "".join(self.notifier.sent))


class MuteAndRobustnessTest(ManagerCase):
    def test_timed_mute_silences_then_expires(self):
        self.make()
        self.mgr.mute("disk:/", minutes=30)
        self.at(0, disk(97)); self.at(100, disk(97))
        self.assertEqual(self.notifier.sent, [])
        self.assertTrue(self.mgr.snapshot()[0]["muted"])
        self.at(31 * 60, disk(97))
        self.assertEqual(len(self.notifier.sent), 1, "the mute ended, the alert is still there")

    def test_mute_until_clear_ends_when_the_alert_ends_and_sends_no_recovery(self):
        self.make()
        self.mgr.mute("disk:/", until_clear=True)
        self.at(0, disk(97)); self.at(10, disk(50)); self.at(50, disk(50))
        self.assertEqual(self.notifier.sent, [])
        self.assertFalse(self.mgr.is_muted("disk:/"))
        self.at(100, disk(97))
        self.assertEqual(len(self.notifier.sent), 1, "a new problem later is not covered by the old mute")

    def test_an_until_clear_mute_for_an_alert_that_never_fired_expires(self):
        self.make()
        self.mgr.mute("unit:smbd", until_clear=True)
        self.at(0, disk(10))
        self.assertTrue(self.mgr.is_muted("unit:smbd"))
        self.at(700, disk(10))
        self.assertFalse(self.mgr.is_muted("unit:smbd"))

    def test_nothing_is_marked_sent_while_telegram_is_off(self):
        self.make(notifier=FakeNotifier(enabled=False))
        self.at(0, disk(97)); self.at(100, disk(97))
        self.assertFalse(self.mgr.snapshot()[0]["notified"])
        self.notifier.enabled = True                                 # configured later
        self.at(200, disk(97))
        self.assertEqual(len(self.notifier.sent), 1)

    def test_a_failed_collector_holds_alerts_instead_of_clearing_them(self):
        self.make()
        self.at(0, disk(97))
        for t in (10, 100, 1000):
            self.at(t, {"disks": {"error": "boom"}})
        self.assertEqual(len(self.mgr.snapshot()), 1)
        self.assertEqual(len(self.notifier.sent), 1, "no false recovery")

    def test_unexpected_data_never_raises(self):
        self.make()
        self.at(0, {"cpu": "nonsense", "disks": None, "systemd": {"watched": 5}})
        self.assertEqual(self.mgr.snapshot(), [])

    def test_messages_are_html_escaped(self):
        self.make()
        s = {"supervisor": {"programs": [{"name": "<b>x&y</b>", "state": "FATAL", "detail": "<script>"}]}}
        self.at(0, s); self.at(40, s)
        text = self.notifier.sent[0]
        self.assertIn("&lt;b&gt;x&amp;y&lt;/b&gt;", text); self.assertNotIn("<script>", text)

    def test_a_burst_is_one_digest_message(self):
        self.make()
        s = {"disks": {"mounts": [{"mount": f"/m{i}", "used_pct": 97, "inodes_pct": 1} for i in range(12)]}}
        self.at(0, s)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("12 new alerts", self.notifier.sent[0]); self.assertIn("and 4 more", self.notifier.sent[0])
        self.assertLess(len(self.notifier.sent[0]), 4000)

    def test_state_survives_a_restart_without_resending(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            self.make(path=path)
            self.at(0, disk(97)); self.mgr.save(force=True)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            first = AlertManager(cfg(), FakeNotifier(), state_path=path, clock=self.clock)
            first.notifier = notifier2 = FakeNotifier()
            first.update(disk(97), self.clock.t + 60)
            self.assertEqual(notifier2.sent, [], "restarting the dashboard must not repeat the message")
            self.assertEqual(len(first.snapshot()), 1)
            first.update(disk(10), self.clock.t + 100); first.update(disk(10), self.clock.t + 140)
            self.assertEqual(len(notifier2.sent), 1)
            self.assertIn("recovered", notifier2.sent[0])


class EventsTest(ManagerCase):
    def accepted(self, *entries):
        return {"ssh_auth": {"recent_accepted": [{"time": t, "user": u, "ip": ip, "method": "publickey"} for t, u, ip in reversed(entries)]},
                "logins": {"history": [{"from": "9.9.9.9"}], "sessions": []}}

    def test_first_run_learns_silently_then_reports_only_new_addresses(self):
        self.make()
        self.at(0, self.accepted((10, "root", "1.1.1.1")))
        self.assertEqual(self.notifier.sent, [])
        self.at(5, self.accepted((10, "root", "1.1.1.1"), (20, "root", "1.1.1.1")))     # known address again
        self.at(10, self.accepted((10, "root", "1.1.1.1"), (20, "root", "1.1.1.1"), (30, "bob", "2.2.2.2")))
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("new SSH login", self.notifier.sent[0]); self.assertIn("2.2.2.2", self.notifier.sent[0])
        self.at(15, self.accepted((10, "root", "1.1.1.1"), (30, "bob", "2.2.2.2"), (40, "bob", "2.2.2.2")))
        self.assertEqual(len(self.notifier.sent), 1, "an address is only new once")
        self.assertIn("9.9.9.9", self.mgr._known["ssh"], "login history was used to learn known addresses")

    def test_logins_can_be_switched_off(self):
        self.make(notify_logins=False)
        self.at(0, self.accepted((10, "root", "1.1.1.1")))
        self.at(5, self.accepted((10, "root", "1.1.1.1"), (20, "x", "3.3.3.3")))
        self.assertEqual(self.notifier.sent, [])

    def test_dashboard_login_reports_a_new_address_once(self):
        self.make()
        self.mgr.dashboard_login("100.1.1.1", "Mozilla/5.0 <Mac>")
        self.mgr.dashboard_login("100.1.1.1", "Mozilla/5.0")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("&lt;Mac&gt;", self.notifier.sent[0])

    def test_one_off_events_have_a_cooldown_per_key(self):
        self.make()
        self.assertTrue(self.mgr.event("lockout:1.2.3.4", "locked out", "warn"))
        self.assertFalse(self.mgr.event("lockout:1.2.3.4", "locked out", "warn"))
        self.assertTrue(self.mgr.event("lockout:5.6.7.8", "locked out", "warn"))
        self.clock.t += 901
        self.assertTrue(self.mgr.event("lockout:1.2.3.4", "locked out", "warn"))

    def test_ban_spike(self):
        self.make()
        jail = lambda total: {"fail2ban": {"jails": [{"total_banned": total}]}}
        self.at(0, jail(10)); self.at(60, jail(12))
        self.assertEqual(self.notifier.sent, [])
        self.at(120, jail(16))
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("banned 6 addresses", self.notifier.sent[0])


class NotifierTest(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = []

        def flaky(text):
            calls.append(text)
            if len(calls) < 3:
                raise TelegramError("network error")

        n = Notifier(cfg() | {"telegram": {"bot_token": "t", "chat_id": "1", "enabled": True}}, send=flaky, delays=(0, 0, 0, 0))
        n._deliver("hello")
        self.assertEqual((len(calls), n.sent, n.dropped), (3, 1, 0))
        self.assertIsNone(n.last_error)

    def test_gives_up_after_the_last_attempt(self):
        def broken(text):
            raise TelegramError("chat not found")

        n = Notifier(cfg() | {"telegram": {"bot_token": "t", "chat_id": "1", "enabled": True}}, send=broken, delays=(0, 0))
        n._deliver("hello")
        self.assertEqual((n.sent, n.dropped, n.last_error), (0, 1, "chat not found"))

    def test_unconfigured_notifier_is_a_silent_no_op(self):
        n = Notifier(cfg())
        self.assertFalse(n.enabled)
        self.assertFalse(n.send("x"))
        ok, error = n.send_now("x")
        self.assertFalse(ok); self.assertIn("not configured", error)

    def test_send_now_reports_the_error(self):
        def broken(text):
            raise TelegramError("Unauthorized")

        n = Notifier(cfg() | {"telegram": {"bot_token": "t", "chat_id": "1", "enabled": True}}, send=broken)
        self.assertEqual(n.send_now("x"), (False, "Unauthorized"))

    def test_a_full_queue_drops_instead_of_blocking(self):
        n = Notifier(cfg() | {"telegram": {"bot_token": "t", "chat_id": "1", "enabled": True}}, send=lambda t: None)
        results = [n.send(str(i)) for i in range(60)]                # the worker thread is not started, so nothing drains
        self.assertEqual((results.count(True), n.dropped), (50, 10))


class FormatTest(unittest.TestCase):
    def test_durations(self):
        self.assertEqual([fmt_duration(x) for x in (5, 59, 60, 3599, 3600, 5400, 86400, 100000)],
                         ["5 s", "59 s", "1 min", "59 min", "1 h 0 min", "1 h 30 min", "1 d 0 h", "1 d 3 h"])


if __name__ == "__main__":
    unittest.main()
