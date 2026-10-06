import json
import unittest

from dashboard import config
from dashboard.alerts import evaluate

CFG = json.loads(config.EXAMPLE_PATH.read_text())


def ids(alerts, level=None):
    return {a["id"] for a in alerts if level is None or a["level"] == level}


class EvaluateTest(unittest.TestCase):
    def test_quiet_system_has_no_alerts(self):
        s = {"cpu": {"total": {"busy": 5.0}, "load_per_core": 0.2, "load": [0.2, 0.2, 0.2], "cores": 1},
             "memory": {"available_pct": 40.0, "swap_used_pct": 10.0, "oom_kills_new": 0, "oom_kills_total": 0},
             "disks": {"mounts": [{"mount": "/", "used_pct": 50.0, "inodes_pct": 10.0}]}}
        self.assertEqual(evaluate(CFG, s), [])

    def test_thresholds_in_both_directions(self):
        s = {"cpu": {"total": {"busy": 96.0}, "load_per_core": 2.5, "load": [2.5, 1, 1], "cores": 1},
             "memory": {"available_pct": 10.0, "swap_used_pct": 95.0, "oom_kills_new": 1, "oom_kills_total": 3},
             "disks": {"mounts": [{"mount": "/", "used_pct": 92.0, "inodes_pct": 99.0}]},
             "ssl": {"certificates": [{"name": "a", "days_left": 3.0}, {"name": "b", "days_left": 10.0}, {"name": "c", "days_left": 60.0}]}}
        a = evaluate(CFG, s)
        self.assertEqual(ids(a, "crit"), {"cpu", "swap", "oom", "inodes:/", "ssl:a"})
        self.assertEqual(ids(a, "warn"), {"load", "mem", "disk:/", "ssl:b"})
        self.assertEqual([x["level"] for x in a], sorted((x["level"] for x in a), key=lambda l: {"crit": 0, "warn": 1, "info": 2}[l]))

    def test_units(self):
        unit = lambda name, enabled, active, result="success", exists=True: {
            "name": name, "unit": name + ".service", "exists": exists, "enabled": enabled, "active": active, "result": result}
        s = {"systemd": {"watched": [
            unit("running", "enabled", "active"),
            unit("stopped-on-purpose", "enabled", "inactive"),           # app1 during maintenance
            unit("crashed", "enabled", "failed", "exit-code"),
            unit("disabled", "disabled", "inactive"),                     # app2
            unit("not-installed", "", "inactive", exists=False)],
            "failed": [{"unit": "crashed.service", "description": ""}, {"unit": "grub.service", "description": "x"}]}}
        a = evaluate(CFG, s)
        self.assertEqual(ids(a, "warn"), {"unit:stopped-on-purpose", "failed:grub.service"})
        self.assertEqual(ids(a, "crit"), {"unit:crashed"})
        self.assertNotIn("failed:crashed.service", ids(a))                # already reported as a unit

    def test_supervisor_tailscale_updates(self):
        s = {"supervisor": {"programs": [{"name": "w", "state": "FATAL", "detail": "x"}, {"name": "ok", "state": "RUNNING"}]},
             "tailscale": {"state": "Stopped"},
             "updates": {"security_count": 2, "reboot_required": True, "reboot_packages": ["linux-image"]}}
        a = evaluate(CFG, s)
        self.assertEqual(ids(a, "crit"), {"supervisor:w", "tailscale"})
        self.assertEqual(ids(a, "warn"), {"updates:security"})
        self.assertEqual(ids(a, "info"), {"reboot"})

    def test_failed_or_missing_sections_are_ignored(self):
        s = {"cpu": {"error": "boom"}, "memory": None, "disks": {"mounts": [{"mount": "/", "error": "gone"}]}}
        self.assertEqual(evaluate(CFG, s), [])

    def test_one_malformed_section_does_not_hide_the_others(self):
        s = {"cpu": {"unexpected": "shape"},                       # would raise KeyError
             "memory": {"available_pct": 3.0, "swap_used_pct": 1.0, "oom_kills_new": 0, "oom_kills_total": 0},
             "tailscale": ["not", "a", "dict"]}
        with self.assertLogs("dashboard.alerts", level="WARNING"):
            a = evaluate(CFG, s)
        self.assertEqual(ids(a), {"mem"})


if __name__ == "__main__":
    unittest.main()
