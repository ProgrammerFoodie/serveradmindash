import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        return []
    return [ROOT / f for f in out.stdout.split() if (ROOT / f).is_file()]


class ServiceUnitTest(unittest.TestCase):
    """The dashboard runs as root, so how far its sandbox is open is worth pinning down."""

    @staticmethod
    def settings() -> dict:
        lines = (DEPLOY / "server-dashboard.service").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))

    def test_the_sandbox_is_open_only_where_the_admin_tools_write(self):
        s = self.settings()
        self.assertEqual((s["ProtectSystem"], s["ProtectHome"], s["NoNewPrivileges"], s["PrivateTmp"]), ("strict", "no", "yes", "yes"))
        writable = sorted(p.lstrip("-") for p in s["ReadWritePaths"].split())
        self.assertEqual(writable, ["/etc", "/home", "/mnt/Extra20/admin/data", "/root", "/var/mail", "/var/spool/cron"])

    def test_paths_that_may_not_exist_are_optional(self):
        # without the "-" prefix systemd refuses to start the service on a machine that has no /var/mail
        for path in self.settings()["ReadWritePaths"].split():
            if path.lstrip("-") in ("/var/mail", "/var/spool/cron"):
                self.assertTrue(path.startswith("-"), path)

    def test_the_install_script_restarts_the_service_so_changes_apply(self):
        script = (DEPLOY / "install-admin.sh").read_text()
        self.assertIn("systemctl restart server-dashboard", script)
        self.assertNotIn("enable --now server-dashboard", script)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env, self.devices = Path(self.tmp.name, "local.env"), Path(self.tmp.name, "devices.txt")
        self.env.write_text("# comment\nDOMAIN=admin.example.org\nTAILSCALE_IP=100.64.9.9\n")
        self.devices.write_text("# who may open it\n100.64.9.1   # laptop\nfd7a:115c:a1e0::5  # laptop (IPv6)\n\n100.64.9.2\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_render(self, *args):
        return subprocess.run(["python3", str(DEPLOY / "render.py"), *args, "--env", str(self.env), "--devices", str(self.devices)],
                              capture_output=True, text=True)

    def test_every_template_renders_completely(self):
        for template in sorted(DEPLOY.glob("*.template")):
            run = self.run_render(str(template))
            self.assertEqual(run.returncode, 0, f"{template.name}: {run.stderr}")
            self.assertNotRegex(run.stdout, r"@[A-Z_]+@", template.name)
            self.assertIn("admin.example.org", run.stdout, template.name)

    def test_values_land_where_they_belong(self):
        nginx = self.run_render(str(DEPLOY / "nginx-admin.conf.template")).stdout
        self.assertIn("server_name admin.example.org;", nginx)
        self.assertIn("/etc/letsencrypt/live/admin.example.org/fullchain.pem", nginx)
        self.assertRegex(nginx, r"100\.64\.9\.1\s+1;\s+# laptop")
        self.assertRegex(nginx, r"fd7a:115c:a1e0::5\s+1;\s+# laptop \(IPv6\)")
        self.assertRegex(nginx, r"100\.64\.9\.2\s+1;")
        self.assertRegex(nginx, r"default\s+0;")                                  # everybody else stays out
        unit = self.run_render(str(DEPLOY / "admin-dns.service.template")).stdout
        self.assertIn("--name admin.example.org --bind 100.64.9.9 --a 100.64.9.9", unit)

    def test_get(self):
        self.assertEqual(self.run_render("--get", "DOMAIN").stdout.strip(), "admin.example.org")
        self.assertNotEqual(self.run_render("--get", "NOPE").returncode, 0)

    def test_bad_values_are_refused_before_anything_is_installed(self):
        template = str(DEPLOY / "nginx-admin.conf.template")
        bad_envs = ["DOMAIN=admin.example.org; rm -rf /\nTAILSCALE_IP=100.64.9.9", "DOMAIN=admin example.org\nTAILSCALE_IP=100.64.9.9",
                    "DOMAIN=admin.example.org\nTAILSCALE_IP=not-an-ip", "DOMAIN=admin.example.org\nTAILSCALE_IP=fd7a::1",
                    "DOMAIN=admin.example.org", "DOMAIN=localhost\nTAILSCALE_IP=100.64.9.9", "nonsense"]
        for text in bad_envs:
            self.env.write_text(text)
            self.assertNotEqual(self.run_render(template).returncode, 0, text)
        self.env.write_text("DOMAIN=admin.example.org\nTAILSCALE_IP=100.64.9.9\n")
        for text in ("", "# nothing but a comment\n", "not-an-ip # x\n", "100.64.9.1; allow all\n", "100.64.9.1 1;\n"):
            self.devices.write_text(text)
            self.assertNotEqual(self.run_render(template).returncode, 0, repr(text))

    def test_a_device_label_cannot_smuggle_in_nginx_syntax(self):
        self.devices.write_text("100.64.9.1   # laptop; } server { listen 80; $x \\\n")
        out = self.run_render(str(DEPLOY / "nginx-admin.conf.template")).stdout
        line = next(l for l in out.splitlines() if "100.64.9.1" in l)
        self.assertNotRegex(line.split("#", 1)[1], r"[;{}$\\]")

    def test_missing_private_files_give_a_clear_message(self):
        self.env.unlink()
        run = self.run_render(str(DEPLOY / "nginx-admin.conf.template"))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("local.env", run.stderr)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_the_install_script_can_show_what_it_would_install(self):
        out = Path(self.tmp.name, "out")
        env = {**os.environ, "DASHBOARD_LOCAL_ENV": str(self.env), "DASHBOARD_ALLOWED_DEVICES": str(self.devices)}
        run = subprocess.run(["bash", str(DEPLOY / "install-admin.sh"), "render", str(out)], capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["admin-dns.service", "nginx-admin-http.conf", "nginx-admin.conf"])
        self.assertIn("admin.example.org", (out / "nginx-admin.conf").read_text())

    def test_the_install_script_is_valid_shell(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(DEPLOY / "install-admin.sh")]).returncode, 0)


class NothingPrivateInTheRepositoryTest(unittest.TestCase):
    """This repository is pushed to GitHub: nothing about the real deployment may be committed."""

    def private_values(self) -> set[str]:
        values = set()
        env = DEPLOY / "local.env"
        if env.is_file():
            for line in env.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and not line.startswith("#") and value.strip().strip("\"'"):
                    values.add(value.strip().strip("\"'"))
        devices = DEPLOY / "allowed-devices.txt"
        if devices.is_file():
            for line in devices.read_text().splitlines():
                body = line.partition("#")[0].strip()                  # addresses are private; labels like "laptop" are just words
                if body:
                    values.add(body)
        values.discard("")
        # the second-level domain (for example example.org) is as private as the full name
        for v in list(values):
            if v.count(".") >= 2 and re.fullmatch(r"[A-Za-z0-9.-]+", v) and not re.fullmatch(r"[0-9.]+", v):
                values.add(".".join(v.split(".")[-2:]))
        return values

    def test_none_of_this_servers_private_values_is_in_a_tracked_file(self):
        values = self.private_values()
        if not values:
            self.skipTest("no private deployment files on this machine, nothing to compare against")
        hits = []
        for path in tracked_files():
            if path == Path(__file__).resolve():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            hits += [f"{path.relative_to(ROOT)}: {v}" for v in values if v in text]
        self.assertEqual(hits, [], "private deployment values found in tracked files")

    def test_private_files_are_not_tracked(self):
        tracked = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
        for forbidden in ("config.json", "deploy/local.env", "deploy/allowed-devices.txt"):
            self.assertNotIn(forbidden, tracked)
        self.assertFalse([t for t in tracked if t.startswith("data/")])

    def test_no_credentials_or_personal_addresses_in_tracked_files(self):
        patterns = {"Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{34,}\b",
                    "private key": r"BEGIN [A-Z ]*PRIVATE KEY",
                    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{30,}",
                    "e-mail address": r"[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply\.github\.com|localhost)[A-Za-z0-9-]+\.[A-Za-z]{2,}"}
        for path in tracked_files():
            if path == Path(__file__).resolve():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for name, pattern in patterns.items():
                for m in re.finditer(pattern, text):
                    if name == "e-mail address" and re.match(r"(git|root|ubuntu|claude)@", m.group(0)):
                        continue                                              # ssh user@host forms, not addresses
                    self.fail(f"{name} in {path.relative_to(ROOT)}: {m.group(0)[:20]}…")


if __name__ == "__main__":
    unittest.main()
