import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path

from dashboard import config, power
from dashboard.actions import ActionError, Actions
from dashboard.audit import Audit
from dashboard.power import Power
from dashboard.util import CommandError

WHO = {"user": "tester", "ip": "10.0.0.1"}
HOST = "myhost"
WATCHED = ["nginx", "app1", "smbd", "redis-server", "supervisor", "ssh", "tailscaled", "fail2ban",
           "server-dashboard", "admin-dns", "cron", "app2.service"]


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeScheduler:
    def __init__(self, log):
        self.log = log
        self.installed = {}

    def get(self, name):
        watched = [{"name": n.removesuffix(".service"), "exists": self.installed.get(n.removesuffix(".service"), True)} for n in WATCHED]
        return 1.0, {"watched": watched}

    def refresh(self, name):
        self.log.append(("refresh", name))
        return {}


class FakeNotifier:
    def __init__(self, log):
        self.log, self.sent = log, []

    def drain(self, timeout=8.0):
        self.log.append(("drain",))
        return True

    def send(self, text):
        self.sent.append(text)
        return True


class FakeAlerts:
    server_name = HOST

    def __init__(self, log):
        self.notifier, self.events = FakeNotifier(log), []

    def event(self, key, text, level="info", cooldown_s=900):
        self.events.append((level, text))
        return True

    def mute(self, *args, **kwargs):
        pass


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.scheduled_file, self.boot_file = root / "scheduled", root / "boot_id"
        self.boot_file.write_text("boot-1\n")
        self.data = root / "data"
        self.data.mkdir()
        self.log, self.now = [], 1_700_000_000.0
        self.responses, self.broken = {}, {}
        self.deferred = []
        self.cfg = json.loads(config.EXAMPLE_PATH.read_text())
        self.cfg["watch"]["systemd"] = list(WATCHED)
        self.audit = Audit(root / "audit.jsonl")
        self.alerts = FakeAlerts(self.log)
        self.sched = FakeScheduler(self.log)
        self.build()

    def build(self, cfg=None):
        if cfg is not None:
            self.cfg = cfg
        self.actions = Actions(self.cfg, self.sched, self.alerts, self.audit, runner=self.runner, clock=lambda: self.now)
        self.power = Power(self.cfg, self.actions, self.sched, self.alerts, self.audit, self.data, runner=self.runner,
                           clock=lambda: self.now, sleep=self.sleep, defer=lambda fn, s: self.deferred.append((fn, s)),
                           scheduled_path=self.scheduled_file, boot_id_path=self.boot_file, hostname=HOST)
        self.power.register()

    def runner(self, args, timeout=5.0, ok_codes=(0,)):
        self.log.append(("run", args))
        error = self.broken.get(tuple(args))
        if error:
            raise CommandError(error)
        out = self.responses.get(tuple(args), "")
        return out.pop(0) if isinstance(out, list) else out

    def sleep(self, seconds):
        self.log.append(("sleep", seconds))
        self.now += seconds

    def commands(self):
        return [c[1] for c in self.log if c[0] == "run"]

    def schedule(self, in_s=60, mode="reboot", message="hello"):
        self.scheduled_file.write_text(f"USEC={int((self.now + in_s) * 1_000_000)}\nWARN_WALL=1\nMODE={mode}\nWALL_MESSAGE={message}\n")

    def pending(self):
        path = self.data / "reboot_pending.json"
        return json.loads(path.read_text()) if path.exists() else None

    def entries(self):
        return self.audit.tail(50)

    def wait_job(self, job):
        for _ in range(300):
            if job.done and not self.actions._lock.locked():
                return job.to_dict()
            threading.Event().wait(0.02)
        self.fail("job did not finish")


class ScheduledTest(Case):
    def test_nothing_scheduled(self):
        self.assertIsNone(self.power.scheduled())
        self.assertEqual(self.power.live(), {"scheduled": None})

    def test_a_scheduled_reboot_is_read_from_the_logind_file(self):
        self.schedule(60, "reboot", "Reboot from the admin dashboard by tester")
        s = self.power.scheduled()
        self.assertEqual((s["mode"], s["label"], s["in_s"], s["at"]), ("reboot", "reboot", 60, self.now + 60))
        self.assertEqual(s["message"], "Reboot from the admin dashboard by tester")
        self.assertIsNone(s["requested_by"])

    def test_other_modes_have_readable_labels_and_a_past_time_is_zero(self):
        self.schedule(-5, "poweroff")
        s = self.power.scheduled()
        self.assertEqual((s["label"], s["in_s"]), ("power off", 0))
        self.schedule(30, "something-new")
        self.assertEqual(self.power.scheduled()["label"], "something-new")

    def test_garbage_is_nothing_scheduled(self):
        for text in ("", "MODE=reboot\n", "USEC=abc\nMODE=reboot\n", "\x00\x01", "USEC=\n"):
            self.scheduled_file.write_text(text)
            self.assertIsNone(self.power.scheduled(), repr(text))

    def test_the_wall_message_is_one_clean_line(self):
        self.schedule(60, "reboot", "a\x1b[31m" + "b" * 500)
        message = self.power.scheduled()["message"]
        self.assertNotIn("\x1b", message)
        self.assertLessEqual(len(message), 200)


class RebootTest(Case):
    def test_the_hostname_must_be_typed_and_nothing_runs_otherwise(self):
        for body in ({}, {"confirm": ""}, {"confirm": "MYHOST"}, {"confirm": "other"}, {"confirm": None}, {"delay": 60}):
            with self.assertRaises(ActionError) as raised:
                self.actions.perform("power.reboot", body, WHO)
            self.assertEqual(raised.exception.status, 400, body)
        self.assertEqual(self.commands(), [])
        self.assertIsNone(self.pending())

    def test_only_zero_or_sixty_seconds(self):
        for delay in (30, 1, 61, -1, "60", True, False, None, 60.5, [60]):
            with self.assertRaises(ActionError, msg=repr(delay)) as raised:
                self.actions.perform("power.reboot", {"confirm": HOST, "delay": delay}, WHO)
            self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.commands(), [])

    def test_a_reboot_in_one_minute(self):
        result = self.actions.perform("power.reboot", {"confirm": HOST, "delay": 60}, WHO)
        self.assertEqual(self.commands(), [["shutdown", "-r", "+1", "Reboot from the admin dashboard by tester"]])
        self.assertFalse(result.get("reconnect"))
        note = self.pending()
        self.assertEqual((note["by"], note["ip"], note["delay"], note["boot_id"], note["requested"]), ("tester", "10.0.0.1", 60, "boot-1", self.now))
        self.assertEqual(os.stat(self.data / "reboot_pending.json").st_mode & 0o777, 0o600)
        entry = self.entries()[0]
        self.assertEqual((entry["action"], entry["ok"], entry["user"]), ("power.reboot", True, "tester"))
        self.assertIn("power.reboot", self.alerts.events[-1][1])
        self.assertEqual(self.deferred, [])

    def test_the_delay_defaults_to_a_minute(self):
        self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.assertEqual(self.commands()[0][:3], ["shutdown", "-r", "+1"])

    def test_the_user_name_in_the_wall_message_is_cleaned(self):
        self.actions.perform("power.reboot", {"confirm": HOST}, {"user": "ev\nil\x1b[0m" + "x" * 100, "ip": "1.2.3.4"})
        message = self.commands()[0][3]
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)
        self.assertLess(len(message), 80)

    def test_an_immediate_reboot_waits_for_the_answer_and_for_telegram(self):
        result = self.actions.perform("power.reboot", {"confirm": HOST, "delay": 0}, WHO)
        self.assertTrue(result["reconnect"] and result["rebooting"])
        self.assertEqual(self.commands(), [])                                  # nothing yet: the page gets its answer first
        (fn, seconds), = self.deferred
        self.assertEqual(seconds, power.NOW_GRACE_S)
        fn()
        self.assertEqual([c for c in self.log if c[0] in ("drain", "run")],
                         [("drain",), ("run", ["shutdown", "-r", "now", "Reboot from the admin dashboard by tester"])])
        self.assertEqual(self.pending()["delay"], 0)

    def test_an_immediate_reboot_that_fails_to_start_is_reported_and_forgotten(self):
        self.broken[("shutdown", "-r", "now", "Reboot from the admin dashboard by tester")] = "shutdown: exit 1: Failed"
        self.actions.perform("power.reboot", {"confirm": HOST, "delay": 0}, WHO)
        self.deferred[0][0]()
        self.assertIsNone(self.pending())
        self.assertEqual(self.alerts.events[-1][0], "warn")
        self.assertIn("did not start", self.alerts.events[-1][1])

    def test_a_failed_schedule_is_a_502_and_leaves_no_note(self):
        self.broken[("shutdown", "-r", "+1", "Reboot from the admin dashboard by tester")] = "shutdown: not installed"
        with self.assertRaises(ActionError) as raised:
            self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.assertEqual(raised.exception.status, 502)
        self.assertIsNone(self.pending())
        self.assertEqual((self.entries()[0]["ok"], self.alerts.events[-1][0]), (False, "warn"))

    def test_a_second_reboot_is_refused_while_one_is_scheduled(self):
        self.schedule(30, "reboot")
        with self.assertRaises(ActionError) as raised:
            self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.commands(), [])

    def test_the_requester_is_shown_for_a_reboot_scheduled_here(self):
        self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.schedule(60, "reboot")
        self.assertEqual(self.power.scheduled()["requested_by"], "tester")


class CancelTest(Case):
    def test_nothing_to_cancel(self):
        with self.assertRaises(ActionError) as raised:
            self.actions.perform("power.cancel", {}, WHO)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.commands(), [])

    def test_cancel_runs_shutdown_c_and_forgets_the_note(self):
        self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.schedule(50, "reboot")
        result = self.actions.perform("power.cancel", {}, WHO)
        self.assertEqual(self.commands()[-1], ["shutdown", "-c"])
        self.assertIn("cancelled", result["detail"])
        self.assertIsNone(self.pending())
        self.assertEqual(self.entries()[0]["action"], "power.cancel")

    def test_a_shutdown_scheduled_elsewhere_can_be_cancelled_too(self):
        self.schedule(50, "poweroff")
        self.actions.perform("power.cancel", {}, WHO)
        self.assertEqual(self.commands(), [["shutdown", "-c"]])

    def test_a_failed_cancel_keeps_the_note_and_says_so(self):
        self.actions.perform("power.reboot", {"confirm": HOST}, WHO)
        self.schedule(50, "reboot")
        self.broken[("shutdown", "-c")] = "shutdown: exit 1: no"
        with self.assertRaises(ActionError) as raised:
            self.actions.perform("power.cancel", {}, WHO)
        self.assertEqual(raised.exception.status, 502)
        self.assertIsNotNone(self.pending())


class OnStartTest(Case):
    def note(self, **overrides):
        note = {"by": "tester", "ip": "10.0.0.1", "requested": self.now - 100, "delay": 60, "boot_id": "boot-0"}
        note.update(overrides)
        (self.data / "reboot_pending.json").write_text(json.dumps(note))

    def test_a_reboot_we_asked_for_is_reported_once(self):
        self.note()
        self.power.on_start()
        message = self.alerts.notifier.sent[0]
        self.assertIn("back up", message)
        self.assertIn("tester", message)
        self.assertIn("1 min after", message)
        self.assertEqual((self.entries()[0]["action"], self.entries()[0]["ok"], self.entries()[0]["user"]), ("power.boot", True, "system"))
        self.assertIsNone(self.pending())
        self.power.on_start()
        self.assertEqual(len(self.alerts.notifier.sent), 1)

    def test_a_note_from_long_ago_is_dropped_silently(self):
        self.note(requested=self.now - power.BACK_UP_WINDOW_S - 1)
        self.power.on_start()
        self.assertEqual((self.alerts.notifier.sent, self.entries()), ([], []))
        self.assertIsNone(self.pending())

    def test_same_boot_the_note_is_kept_while_the_countdown_may_still_run_and_dropped_when_stale(self):
        self.note(boot_id="boot-1", requested=self.now - 30)
        self.power.on_start()
        self.assertIsNotNone(self.pending())
        self.note(boot_id="boot-1", requested=self.now - power.STALE_PENDING_S - 1)
        self.power.on_start()
        self.assertIsNone(self.pending())
        self.assertEqual(self.alerts.notifier.sent, [])

    def test_a_broken_note_is_removed(self):
        for text in ("{not json", "[]", "42", json.dumps({"by": "x"}), json.dumps({"by": "x", "boot_id": "b", "requested": "soon"}),
                     json.dumps({"by": 1, "boot_id": "b", "requested": 1}), json.dumps({"by": "x", "boot_id": "b", "requested": True})):
            (self.data / "reboot_pending.json").write_text(text)
            self.power.on_start()
            self.assertIsNone(self.pending(), text)
        self.assertEqual(self.alerts.notifier.sent, [])

    def test_without_a_note_nothing_happens(self):
        self.power.on_start()
        self.assertEqual((self.alerts.notifier.sent, self.entries()), ([], []))

    def test_the_user_name_is_escaped_in_the_telegram_message(self):
        self.note(by="<b>x</b>")
        self.power.on_start()
        self.assertNotIn("<b>x</b>", self.alerts.notifier.sent[0])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", self.alerts.notifier.sent[0])


class PlanTest(Case):
    def test_data_stores_first_applications_next_supporting_services_after_and_nginx_last(self):
        plan = self.power.restart_plan()
        self.assertEqual(plan["units"], ["redis-server", "app1", "supervisor", "app2", "smbd", "ssh", "fail2ban", "cron", "nginx"])
        self.assertEqual(sorted(plan["excluded"]), ["admin-dns", "server-dashboard", "tailscaled"])
        self.assertEqual(plan["missing"], [])

    def test_the_dashboard_its_dns_and_tailscale_are_never_in_the_list(self):
        for unit in ("server-dashboard", "admin-dns", "tailscaled"):
            self.assertNotIn(unit, self.power.restart_plan()["units"])

    def test_a_configured_order_is_used_as_given(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["admin"] = {"restart_order": ["nginx", "app2.service", "redis-server"]}
        self.build(cfg)
        self.assertEqual(self.power.restart_plan()["units"], ["nginx", "app2", "redis-server"])

    def test_an_excluded_unit_in_a_hand_edited_order_is_still_skipped(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["admin"] = {"restart_order": ["tailscaled", "nginx", "server-dashboard"]}
        self.build(cfg)
        plan = self.power.restart_plan()
        self.assertEqual(plan["units"], ["nginx"])
        self.assertIn("tailscaled", plan["excluded"])

    def test_an_empty_configured_order_means_the_built_in_one(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["admin"] = {"restart_order": []}
        self.build(cfg)
        self.assertEqual(self.power.restart_plan()["units"][0], "redis-server")

    def test_units_that_are_not_installed_are_listed_but_not_restarted(self):
        self.sched.installed["smbd"] = False
        plan = self.power.restart_plan()
        self.assertNotIn("smbd", plan["units"])
        self.assertEqual(plan["missing"], ["smbd"])

    def test_describe_has_what_the_dialogs_need(self):
        d = self.power.describe()
        self.assertEqual((d["hostname"], d["delays"], d["scheduled"], d["job"]), (HOST, [0, 60], None, None))
        self.assertEqual(d["restart"], self.power.restart_plan())


class RestartAllTest(Case):
    def start(self, **body):
        return self.power.start_restart_all({"confirm": HOST, **body}, WHO)

    def test_the_hostname_must_be_typed(self):
        for body in ({}, {"confirm": "nope"}):
            with self.assertRaises(ActionError) as raised:
                self.power.start_restart_all(body, WHO)
            self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.actions._lock.locked())

    def test_nothing_to_restart_is_a_400(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["watch"]["systemd"] = ["tailscaled", "server-dashboard"]
        self.build(cfg)
        with self.assertRaises(ActionError) as raised:
            self.start()
        self.assertEqual(raised.exception.status, 400)
        self.assertFalse(self.actions._lock.locked())

    def test_every_unit_is_restarted_in_order_and_checked(self):
        for unit in WATCHED:
            self.responses[("systemctl", "is-active", "--", f"{unit.removesuffix('.service')}.service")] = "active\n"
        d = self.wait_job(self.start())
        plan = self.power.restart_plan()["units"]
        expected = [c for unit in plan for c in (["systemctl", "restart", "--", f"{unit}.service"], ["systemctl", "is-active", "--", f"{unit}.service"])]
        self.assertEqual(self.commands(), expected)
        self.assertEqual((d["state"], d["detail"]), ("ok", f"{len(plan)} restarted"))
        self.assertEqual([s["label"] for s in d["steps"]], [f"restart {u}" for u in plan])
        self.assertTrue(all(s["state"] == "ok" for s in d["steps"]))
        self.assertIn(("refresh", "systemd"), self.log)
        entry = self.entries()[0]
        self.assertEqual((entry["action"], entry["ok"], entry["detail"]), ("power.restart-all", True, f"{len(plan)} restarted"))
        for never in ("tailscaled", "server-dashboard", "admin-dns"):
            self.assertFalse(any(never in " ".join(c) for c in self.commands()))

    def test_a_failure_is_recorded_and_the_job_carries_on(self):
        self.responses[("systemctl", "is-active", "--", "app1.service")] = "active\n"
        self.broken[("systemctl", "restart", "--", "redis-server.service")] = "systemctl: exit 1: Job failed"
        self.responses[("systemctl", "is-active", "--", "smbd.service")] = "failed\n"
        for unit in self.power.restart_plan()["units"]:
            self.responses.setdefault(("systemctl", "is-active", "--", f"{unit}.service"), "active\n")
        d = self.wait_job(self.start())
        self.assertEqual(d["state"], "failed")
        self.assertIn("2 failed (redis-server, smbd)", d["detail"])
        states = {s["label"]: s for s in d["steps"]}
        self.assertEqual(states["restart redis-server"]["state"], "failed")
        self.assertIn("Job failed", states["restart redis-server"]["detail"])
        self.assertEqual(states["restart smbd"]["state"], "failed")
        self.assertIn("is failed after the restart", states["restart smbd"]["detail"])
        self.assertEqual(states["restart nginx"]["state"], "ok")                   # it went on to the end
        self.assertEqual(self.entries()[0]["ok"], False)
        self.assertEqual(self.alerts.events[-1][0], "warn")

    def test_a_service_that_is_still_starting_gets_time(self):
        self.responses[("systemctl", "is-active", "--", "app1.service")] = ["activating\n", "activating\n", "active\n"]
        for unit in self.power.restart_plan()["units"]:
            self.responses.setdefault(("systemctl", "is-active", "--", f"{unit}.service"), "active\n")
        d = self.wait_job(self.start())
        self.assertEqual(d["state"], "ok")
        self.assertEqual([c for c in self.log if c[0] == "sleep"], [("sleep", 1), ("sleep", 1)])

    def test_a_service_that_never_finishes_starting_is_a_failure_after_the_time_limit(self):
        self.responses[("systemctl", "is-active", "--", "app1.service")] = "activating\n"
        for unit in self.power.restart_plan()["units"]:
            self.responses.setdefault(("systemctl", "is-active", "--", f"{unit}.service"), "active\n")
        d = self.wait_job(self.start())
        self.assertEqual(d["state"], "failed")
        self.assertIn("app1 is activating after the restart", str(d["steps"]))
        self.assertGreaterEqual(len([c for c in self.log if c[0] == "sleep"]), power.ACTIVE_WAIT_S)

    def test_only_one_thing_at_a_time(self):
        gate, started = threading.Event(), threading.Event()
        real = self.runner

        def blocking(args, timeout=5.0, ok_codes=(0,)):
            if args[:2] == ["systemctl", "restart"]:
                started.set()
                gate.wait(5)
            return real(args, timeout, ok_codes)

        self.power._run = blocking
        for unit in self.power.restart_plan()["units"]:
            self.responses[("systemctl", "is-active", "--", f"{unit}.service")] = "active\n"
        job = self.start()
        self.assertTrue(started.wait(5))
        self.assertEqual(self.power.describe()["job"], job.id)
        for attempt in (lambda: self.start(), lambda: self.actions.perform("power.reboot", {"confirm": HOST}, WHO)):
            with self.assertRaises(ActionError) as raised:
                attempt()
            self.assertEqual(raised.exception.status, 409)
        gate.set()
        self.wait_job(job)
        self.assertIsNone(self.power.describe()["job"])


class DrainTest(unittest.TestCase):
    def test_drain_waits_for_the_queue_and_gives_up_after_the_timeout(self):
        from dashboard.alertmanager import Notifier
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        cfg["telegram"].update(bot_token="x", chat_id="1", enabled=True)
        gate, sent = threading.Event(), []

        def slow_send(text):
            gate.wait(5)
            sent.append(text)

        notifier = Notifier(cfg, send=slow_send, delays=(0,))
        self.assertTrue(notifier.drain(0.1))                                   # nothing queued, and not started
        notifier.start()
        self.addCleanup(notifier.stop)
        self.assertTrue(notifier.drain(0.1))
        notifier.send("one")
        notifier.send("two")
        self.assertFalse(notifier.drain(0.2))                                  # stuck sending: gives up in time
        gate.set()
        self.assertTrue(notifier.drain(3))
        self.assertEqual(sent, ["one", "two"])

    def test_drain_without_telegram_returns_at_once(self):
        from dashboard.alertmanager import Notifier
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        self.assertTrue(Notifier(cfg).drain(5))


if __name__ == "__main__":
    unittest.main()
