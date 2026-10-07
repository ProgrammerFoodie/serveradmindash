import http.client
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest

from dashboard import auth, config
from dashboard.history import History
from dashboard.jobs import Jobs
from dashboard.server import App, HttpError, make_server

PASSWORD = "correct horse battery"
_HASH = None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeScheduler:
    def __init__(self):
        self.touched = []
        self.data = {"cpu": {"busy": 5.0}, "supervisor": {"programs": [{"name": "grp:proc_00"}]}}

    def get(self, name):
        return (time.time(), self.data[name]) if name in self.data else (None, None)

    def touch(self, name):
        self.touched.append(name)

    def snapshot(self):
        return dict(self.data)


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok, self.texts = ok, []

    def send_now(self, text):
        self.texts.append(text)
        return (True, None) if self.ok else (False, "chat not found")


class FakeAlerts:
    server_name = "test <host>"

    def __init__(self, notifier=None):
        self.notifier = notifier or FakeNotifier()
        self.active = [{"id": "disk:/", "level": "crit", "title": "Disk / 97% full", "detail": "", "since": 1, "muted": False}]
        self.muted, self.unmuted, self.logins, self.events = [], [], [], []

    def snapshot(self):
        return self.active

    def status(self):
        return {"configured": True, "sent": 3, "last_error": None}

    def mute(self, alert_id, minutes=None):
        self.muted.append((alert_id, minutes))

    def unmute(self, alert_id):
        self.unmuted.append(alert_id)

    def dashboard_login(self, ip, user_agent):
        self.logins.append(ip)

    def event(self, key, text, level="info", cooldown_s=900):
        self.events.append((key, level))
        return True


class FakeActions:
    protected = {"ssh", "nginx"}

    def __init__(self):
        self.calls, self.error = [], None
        self.jobs = Jobs()
        self.audit = type("A", (), {"tail": staticmethod(lambda limit: [{"action": "service.restart", "limit": limit}])})()

    def perform(self, kind, body, who):
        self.calls.append((kind, body, who))
        if self.error:
            raise self.error
        return {"ok": True, "detail": "done"}


class ServerTest(unittest.TestCase):
    max_connections = 24
    limiter = None
    alerts = None
    actions = None
    power = None

    @classmethod
    def setUpClass(cls):
        global _HASH
        logging.disable(logging.CRITICAL)        # failed-login warnings are expected here
        _HASH = _HASH or auth.hash_password(PASSWORD)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.port = free_port()
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        cfg["listen"] = f"127.0.0.1:{self.port}"
        cfg["auth"].update(username="admin", password_hash=_HASH)
        self.sched = FakeScheduler()
        self.history = History(os.path.join(self.tmp.name, "h.db"))
        self.sessions = auth.Sessions(os.path.join(self.tmp.name, "a.db"), "fp", hours=1)
        self.app = App(cfg, self.sched, self.history, self.sessions, self.limiter, alerts=self.alerts, actions=self.actions, power=self.power)
        self.server = make_server(self.app, self.max_connections)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.history.close()
        self.sessions.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, token=None, csrf=None, json_body=True):
        h = dict(headers or {})
        if token:
            h["Cookie"] = f"__Host-sid={token}"
        if csrf:
            h["X-CSRF-Token"] = csrf
        data = None
        if body is not None:
            data = json.dumps(body) if json_body else body
            h.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return r.status, r, parsed

    def login(self, ip="10.0.0.1", password=PASSWORD, username="admin"):
        return self.req("POST", "/login", {"username": username, "password": password}, {"X-Real-IP": ip})

    def signed_in(self):
        status, r, body = self.login()
        self.assertEqual(status, 200)
        cookie = r.getheader("Set-Cookie")
        return cookie.split(";")[0].split("=", 1)[1], body["csrf"]


class AccessControlTest(ServerTest):
    def test_public_routes_and_headers(self):
        status, r, _ = self.req("GET", "/login")
        self.assertEqual(status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertNotIn("Python", r.getheader("Server") or "")
        self.assertEqual(self.req("GET", "/healthz")[2], {"ok": True})
        self.assertEqual(self.req("GET", "/static/app.css")[0], 200)

    def test_everything_else_needs_a_session(self):
        status, r, _ = self.req("GET", "/")
        self.assertEqual((status, r.getheader("Location")), (302, "/login"))
        for path in ("/api/session", "/api/live", "/api/history?metrics=x", "/api/metrics", "/api/logs?kind=systemd"):
            self.assertEqual(self.req("GET", path)[0], 401, path)
        self.assertEqual(self.req("POST", "/logout", {}, csrf="anything")[0], 401)

    def test_bogus_cookie_is_rejected(self):
        self.assertEqual(self.req("GET", "/api/session", token="nope")[0], 401)
        self.assertEqual(self.req("GET", "/api/session", headers={"Cookie": "__Host-sid"})[0], 401)
        self.assertEqual(self.req("GET", "/api/session", headers={"Cookie": '__Host-sid="; broken'})[0], 401)

    def test_static_cannot_escape_its_folder(self):
        for path in ("/static/../config.example.json", "/static/..%2fconfig.example.json",
                     "/static/%2e%2e/config.example.json", "/static//etc/passwd", "/static/nothing.js",
                     "/static/login.html%00.css", "/static/"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_wrong_host_header_is_refused(self):
        self.assertEqual(self.req("GET", "/login", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.req("GET", "/healthz", headers={"Host": f"127.0.0.1:{self.port + 1}"})[0], 421)
        self.assertEqual(self.req("GET", "/login", headers={"Host": "admin.example.com"})[0], 200)

    def test_unknown_routes_and_methods(self):
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", "/api/nope", token=token)[0], 404)
        self.assertEqual(self.req("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.req("PUT", "/login", {})[0], 501)


class LoginTest(ServerTest):
    def test_success_sets_a_locked_down_cookie(self):
        status, r, body = self.login()
        self.assertEqual(status, 200)
        cookie = r.getheader("Set-Cookie")
        for part in ("__Host-sid=", "Path=/", "Secure", "HttpOnly", "SameSite=Lax", "Max-Age=3600"):
            self.assertIn(part, cookie)
        self.assertNotIn("Domain", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        status, _, session = self.req("GET", "/api/session", token=token)
        self.assertEqual((status, session["user"], session["csrf"]), (200, "admin", body["csrf"]))
        self.assertEqual(self.req("GET", "/", token=token)[0], 200)
        self.assertEqual(self.req("GET", "/login", token=token)[1].getheader("Location"), "/")

    def test_wrong_username_and_wrong_password_look_identical(self):
        a = self.login(password="wrong password")
        b = self.login(username="root", password=PASSWORD)
        self.assertEqual((a[0], a[2]), (b[0], b[2]))
        self.assertEqual(a[0], 401)
        self.assertIsNone(a[1].getheader("Set-Cookie"))

    def test_bad_requests(self):
        self.assertEqual(self.req("POST", "/login", "not json", json_body=False)[0], 400)
        self.assertEqual(self.req("POST", "/login", [1, 2])[0], 400)
        self.assertEqual(self.req("POST", "/login", {"username": 1, "password": 2})[0], 400)
        self.assertEqual(self.req("POST", "/login", {"username": "admin", "password": "x" * 2000})[0], 400)
        self.assertEqual(self.req("POST", "/login", "u=admin&p=x", {"Content-Type": "text/plain"}, json_body=False)[0], 415)
        self.assertEqual(self.req("POST", "/login", "x" * 20000, json_body=False)[0], 413)

    def test_cross_origin_login_is_refused(self):
        h = {"X-Real-IP": "10.0.0.1"}
        body = {"username": "admin", "password": PASSWORD}
        self.assertEqual(self.req("POST", "/login", body, {**h, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/login", body, {**h, "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.req("POST", "/login", body, {**h, "Origin": "https://admin.example.com"})[0], 200)

    def test_lockout_applies_even_to_the_right_password(self):
        for _ in range(5):
            self.assertEqual(self.login(password="nope")[0], 401)
        status, r, _ = self.login(password="nope")
        self.assertEqual(status, 429)
        self.assertGreater(int(r.getheader("Retry-After")), 0)
        self.assertEqual(self.login()[0], 429)                       # correct password, still locked
        self.assertEqual(self.login(ip="10.0.0.2")[0], 200)          # another address is unaffected

    def test_failed_and_successful_logins_are_recorded(self):
        self.login(password="nope")
        self.login()
        kinds = {e["kind"] for e in self.history.events()}
        self.assertEqual(kinds, {"login", "login_failed"})


class OverallLimitTest(ServerTest):
    limiter = auth.LoginLimiter(per_ip=5, overall=8)

    def test_rotating_forged_addresses_do_not_escape_the_limit(self):
        for i in range(8):
            self.assertEqual(self.login(ip=f"10.0.1.{i}", password="nope")[0], 401)
        self.assertEqual(self.login(ip="10.0.9.9")[0], 429)


class CsrfTest(ServerTest):
    def test_logout_needs_the_csrf_token(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.req("POST", "/logout", {}, token=token)[0], 403)
        self.assertEqual(self.req("POST", "/logout", {}, token=token, csrf="wrong")[0], 403)
        self.assertEqual(self.req("POST", "/logout", {}, headers={"Origin": "https://evil.example"}, token=token, csrf=csrf)[0], 403)
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 200)      # still signed in
        status, r, _ = self.req("POST", "/logout", {}, token=token, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 401)     # session is really gone


class IdleTimeoutTest(ServerTest):
    def age(self, seconds):
        self.sessions._db.execute("UPDATE sessions SET last_seen = last_seen - ?", (seconds,))

    def test_session_reports_the_limit(self):
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", "/api/session", token=token)[2]["idle_s"], 15 * 60)

    def test_inactive_session_is_rejected_everywhere(self):
        token, csrf = self.signed_in()
        self.age(16 * 60)
        self.assertEqual(self.req("GET", "/api/live?tab=overview", token=token)[0], 401)
        self.assertEqual(self.req("POST", "/api/session/ping", {}, token=token, csrf=csrf)[0], 401)
        status, r, _ = self.req("GET", "/", token=token)
        self.assertEqual((status, r.getheader("Location")), (302, "/login"))

    def test_polling_does_not_keep_a_session_alive(self):
        token, _ = self.signed_in()
        self.age(14 * 60)
        for path in ("/api/live?tab=overview", "/api/session", "/api/alerts"):
            self.assertEqual(self.req("GET", path, token=token)[0], 200, path)
        self.age(2 * 60)                                   # 16 minutes since the person last did anything
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 401)

    def test_ping_keeps_it_alive_and_needs_csrf(self):
        token, csrf = self.signed_in()
        self.age(14 * 60)
        self.assertEqual(self.req("POST", "/api/session/ping", {}, token=token)[0], 403)      # no CSRF token: no activity
        status, _, body = self.req("POST", "/api/session/ping", {}, token=token, csrf=csrf)
        self.assertEqual((status, body["ok"], body["idle_left"]), (200, True, 15 * 60))
        self.age(10 * 60)
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 200)                # clock restarted by the ping

    def test_opening_the_page_and_pressing_buttons_count_as_activity(self):
        token, csrf = self.signed_in()
        self.age(14 * 60)
        self.assertEqual(self.req("GET", "/", token=token)[0], 200)
        self.age(14 * 60)
        self.assertEqual(self.req("POST", "/api/alerts/unmute", {}, token=token, csrf=csrf)[0], 400)   # rejected input, but a person pressed it
        self.age(14 * 60)
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 200)

    def test_a_forged_post_cannot_extend_a_session(self):
        token, _ = self.signed_in()
        self.age(14 * 60)
        self.req("POST", "/api/session/ping", {}, token=token, csrf="wrong")
        self.req("POST", "/api/session/ping", {}, headers={"Origin": "https://evil.example"}, token=token, csrf="wrong")
        self.age(2 * 60)
        self.assertEqual(self.req("GET", "/api/session", token=token)[0], 401)


class ApiTest(ServerTest):
    def test_live(self):
        token, _ = self.signed_in()
        status, _, body = self.req("GET", "/api/live?tab=overview", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["sections"]["cpu"]["data"], {"busy": 5.0})
        self.assertIsNone(body["sections"]["memory"])                                # not collected yet
        self.assertIsInstance(body["alerts"], list)
        self.assertEqual(self.req("GET", "/api/live?tab=bogus", token=token)[0], 400)
        self.assertEqual(self.sched.touched, [])
        self.req("GET", "/api/live?tab=processes", token=token)
        self.assertEqual(self.sched.touched, ["processes"])

    def test_history(self):
        token, _ = self.signed_in()
        now = int(time.time())
        for i in range(60):
            self.history.record(now - 600 + i * 10 - now % 5, {"cpu.busy": float(i)})
        self.history.flush()
        status, _, body = self.req("GET", "/api/history?metrics=cpu.busy,nope&range=1h", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["series"]["cpu.busy"]), 60)
        self.assertEqual(body["series"]["nope"], [])
        self.assertEqual(self.req("GET", "/api/history?range=1h", token=token)[0], 400)
        self.assertEqual(self.req("GET", "/api/history?metrics=cpu.busy&range=99y", token=token)[0], 400)
        self.assertEqual(self.req("GET", "/api/history?metrics=" + "a" * 200, token=token)[0], 400)
        self.assertEqual(self.req("GET", "/api/metrics", token=token)[2], {"metrics": ["cpu.busy"]})

    def test_logs_only_for_known_units(self):
        token, _ = self.signed_in()
        for q in ("kind=systemd&name=not-watched", "kind=systemd&name=..%2f..%2fetc", "kind=systemd&name=-n",
                  "kind=supervisor&name=other", "kind=other&name=nginx", "kind=systemd"):
            self.assertIn(self.req("GET", "/api/logs?" + q, token=token)[0], (400, 404), q)


class AlertApiTest(ServerTest):
    def setUp(self):
        self.alerts = FakeAlerts()
        super().setUp()

    def test_everything_needs_a_session(self):
        self.assertEqual(self.req("GET", "/api/alerts")[0], 401)
        for path in ("/api/alerts/mute", "/api/alerts/unmute", "/api/telegram/test"):
            self.assertEqual(self.req("POST", path, {"id": "disk:/"}, csrf="x")[0], 401, path)

    def test_state_changes_need_the_csrf_token(self):
        token, csrf = self.signed_in()
        for path in ("/api/alerts/mute", "/api/alerts/unmute", "/api/telegram/test"):
            self.assertEqual(self.req("POST", path, {"id": "disk:/"}, token=token)[0], 403, path)
            self.assertEqual(self.req("POST", path, {"id": "disk:/"}, token=token, csrf="wrong")[0], 403, path)
        self.assertEqual((self.alerts.muted, self.alerts.unmuted, self.alerts.notifier.texts), ([], [], []))

    def test_list(self):
        token, _ = self.signed_in()
        self.history.add_event("alert", "disk:/", "crit", "Disk / 97% full")
        self.history.add_event("login", "10.0.0.1", "info", "signed in")
        status, _, body = self.req("GET", "/api/alerts", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["alerts"][0]["id"], "disk:/")
        self.assertEqual([e["kind"] for e in body["events"]], ["alert"])         # only alert-related events
        self.assertTrue(body["telegram"]["configured"])
        self.assertEqual(self.req("GET", "/api/live?tab=overview", token=token)[2]["alerts"][0]["id"], "disk:/")

    def test_mute_validates_and_acts(self):
        token, csrf = self.signed_in()
        post = lambda body: self.req("POST", "/api/alerts/mute", body, token=token, csrf=csrf)[0]
        self.assertEqual(post({"id": "disk:/", "minutes": 60}), 200)
        self.assertEqual(post({"id": "disk:/"}), 200)                                        # until it clears
        self.assertEqual(self.alerts.muted, [("disk:/", 60), ("disk:/", None)])
        for bad in ({}, {"id": ""}, {"id": 5}, {"id": "disk:/", "minutes": 0}, {"id": "disk:/", "minutes": 99999},
                    {"id": "disk:/", "minutes": "60"}, {"id": "disk:/", "minutes": True}):
            self.assertEqual(post(bad), 400, bad)
        self.assertEqual(post({"id": "not-active", "minutes": 5}), 404)
        self.assertIn("muted", {e["message"].split()[0] for e in self.history.events(kinds=("action",))})

    def test_unmute(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.req("POST", "/api/alerts/unmute", {"id": "disk:/"}, token=token, csrf=csrf)[0], 200)
        self.assertEqual(self.alerts.unmuted, ["disk:/"])
        self.assertEqual(self.req("POST", "/api/alerts/unmute", {}, token=token, csrf=csrf)[0], 400)

    def test_telegram_test_reports_success_and_failure(self):
        token, csrf = self.signed_in()
        status, _, body = self.req("POST", "/api/telegram/test", {}, token=token, csrf=csrf)
        self.assertEqual((status, body), (200, {"ok": True, "error": None}))
        self.assertIn("test &lt;host&gt;", self.alerts.notifier.texts[0])                  # the name is escaped for Telegram's HTML
        self.alerts.notifier.ok = False
        self.assertEqual(self.req("POST", "/api/telegram/test", {}, token=token, csrf=csrf)[2], {"ok": False, "error": "chat not found"})

    def test_sign_in_and_lockout_reach_the_alert_manager(self):
        self.login(ip="10.0.0.9")
        self.assertEqual(self.alerts.logins, ["10.0.0.9"])
        for _ in range(5):
            self.login(ip="10.0.0.5", password="nope")
        self.assertEqual([k for k, _ in self.alerts.events], ["lockout:10.0.0.5"])         # reported when the lockout starts


class ActionApiTest(ServerTest):
    def setUp(self):
        self.actions = FakeActions()
        super().setUp()

    BODY = {"kind": "systemd", "name": "smbd", "op": "restart"}

    def test_everything_needs_a_session_and_the_csrf_token(self):
        for path in ("/api/action/service", "/api/action/process"):
            self.assertEqual(self.req("POST", path, self.BODY, csrf="x")[0], 401, path)
        self.assertEqual(self.req("GET", "/api/audit")[0], 401)
        token, csrf = self.signed_in()
        for path in ("/api/action/service", "/api/action/process"):
            self.assertEqual(self.req("POST", path, self.BODY, token=token)[0], 403, path)
            self.assertEqual(self.req("POST", path, self.BODY, token=token, csrf="wrong")[0], 403, path)
            self.assertEqual(self.req("POST", path, self.BODY, headers={"Origin": "https://evil.example"}, token=token, csrf=csrf)[0], 403, path)
            self.assertEqual(self.req("POST", path, self.BODY, headers={"Sec-Fetch-Site": "cross-site"}, token=token, csrf=csrf)[0], 403, path)
        self.assertEqual(self.actions.calls, [], "nothing reached the action code")

    def test_a_valid_request_reaches_actions_with_the_user_and_real_address(self):
        token, csrf = self.signed_in()
        status, _, body = self.req("POST", "/api/action/service", self.BODY, headers={"X-Real-IP": "100.64.0.2"}, token=token, csrf=csrf)
        self.assertEqual((status, body), (200, {"ok": True, "detail": "done"}))
        kind, sent, who = self.actions.calls[0]
        self.assertEqual((kind, sent, who), ("service", self.BODY, {"user": "admin", "ip": "100.64.0.2"}))
        self.req("POST", "/api/action/process", {"pid": 5, "signal": "TERM", "start_ticks": 1}, token=token, csrf=csrf)
        self.assertEqual(self.actions.calls[1][0], "process")

    def test_refusals_come_back_with_their_status_and_message(self):
        from dashboard.actions import ActionError
        token, csrf = self.signed_in()
        for status in (400, 403, 404, 409, 429, 500, 502):
            self.actions.error = ActionError(status, f"reason {status}")
            code, _, body = self.req("POST", "/api/action/service", self.BODY, token=token, csrf=csrf)
            self.assertEqual((code, body), (status, {"error": f"reason {status}"}))

    def test_non_json_and_oversized_bodies_are_rejected_before_acting(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.req("POST", "/api/action/service", "x=1", {"Content-Type": "text/plain"}, token=token, csrf=csrf, json_body=False)[0], 415)
        self.assertEqual(self.req("POST", "/api/action/service", "x" * 20000, token=token, csrf=csrf, json_body=False)[0], 413)
        self.assertEqual(self.req("POST", "/api/action/service", [1], token=token, csrf=csrf)[0], 400)
        self.assertEqual(self.actions.calls, [])

    def test_session_reports_what_is_protected_and_the_audit_log_is_readable(self):
        token, _ = self.signed_in()
        s = self.req("GET", "/api/session", token=token)[2]
        self.assertEqual((s["actions"], s["protected"]), (True, ["nginx", "ssh"]))
        self.assertEqual(self.req("GET", "/api/audit?limit=7", token=token)[2]["entries"][0]["limit"], 7)
        self.assertEqual(self.req("GET", "/api/audit?limit=99999", token=token)[2]["entries"][0]["limit"], 200)


class UserActionsApiTest(ServerTest):
    """The account actions through the real server, with the real Actions and UserAdmin over a fake system."""

    def setUp(self):
        from dashboard.actions import Actions
        from dashboard.audit import Audit
        from dashboard.useradmin import UserAdmin

        super().setUp()
        self.ran = []
        self.data = {"users": [
            {"name": "root", "uid": 0, "gid": 0, "type": "root", "sudo": "full", "home": "/root", "password": "set", "expired": False, "expires": None,
             "processes": 1, "can_login": True},
            {"name": "alice", "uid": 1000, "gid": 1000, "type": "login", "sudo": "full", "home": "/home/alice", "password": "set", "expired": False,
             "expires": None, "processes": 0, "can_login": True},
            {"name": "bob", "uid": 1001, "gid": 1001, "type": "login", "sudo": None, "home": "/home/bob", "password": "set", "expired": False,
             "expires": None, "processes": 0, "can_login": True}],
            "sessions": [{"id": "5", "user": "bob"}], "sudo_source": "sudoers", "shells": ["/bin/bash"]}

        class Sched:
            def refresh(inner, name):
                return self.data

            def get(inner, name):
                return 1.0, self.data

        def runner(args, timeout=5.0, ok_codes=(0,), stdin=None, secret=None):
            self.ran.append((args, stdin))
            return ""

        cfg = self.app.cfg
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.audit = Audit(os.path.join(self.work.name, "audit.jsonl"))
        self.app.actions = Actions(cfg, Sched(), None, self.audit, runner=runner)
        self.app.useradmin = UserAdmin(cfg, self.app.actions, Sched(), None, self.audit, self.sessions, self.work.name, runner=runner,
                                       protect=(), forbidden=("/etc", "/usr", "/root"))
        self.app.useradmin.register()

    OPS = ("add", "password", "lock", "unlock", "ban", "unban", "rename", "home", "remove", "end-session", "end-dashboard")

    def test_everything_needs_a_session_and_the_csrf_token(self):
        for op in self.OPS:
            self.assertEqual(self.req("POST", f"/api/users/{op}", {"name": "bob"}, csrf="x")[0], 401, op)
        token, csrf = self.signed_in()
        for op in self.OPS:
            self.assertEqual(self.req("POST", f"/api/users/{op}", {"name": "bob"}, token=token)[0], 403, op)
            self.assertEqual(self.req("POST", f"/api/users/{op}", {"name": "bob"}, token=token, csrf="wrong")[0], 403, op)
        self.assertEqual(self.ran, [])

    def test_a_switched_off_tool_does_not_exist(self):
        token, csrf = self.signed_in()
        self.app.admin = dict(self.app.admin, users=False)
        for op in self.OPS:
            self.assertEqual(self.req("POST", f"/api/users/{op}", {"name": "bob"}, token=token, csrf=csrf)[0], 404, op)
        self.assertEqual(self.ran, [])

    def test_unknown_operations_are_404_and_get_is_not_an_action(self):
        token, csrf = self.signed_in()
        for op in ("nothing", "", "../x", "LOCK", "lock/extra"):
            self.assertEqual(self.req("POST", f"/api/users/{op}", {}, token=token, csrf=csrf)[0], 404, op)
        self.assertNotEqual(self.req("GET", "/api/users/lock", token=token)[0], 200)

    def test_a_lock_goes_through_and_a_refusal_comes_back_with_its_reason(self):
        token, csrf = self.signed_in()
        status, _, body = self.req("POST", "/api/users/lock", {"name": "bob"}, token=token, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(self.ran, [(["usermod", "-L", "-e", "1", "--", "bob"], None)])
        status, _, body = self.req("POST", "/api/users/lock", {"name": "root"}, token=token, csrf=csrf)
        self.assertEqual((status, "never changed" in body["error"]), (403, True))
        status, _, body = self.req("POST", "/api/users/lock", {"name": "alice"}, token=token, csrf=csrf)    # the only other admin is root, who does not count
        self.assertEqual((status, "last user with full sudo" in body["error"]), (403, True))
        self.assertEqual(len(self.ran), 1)

    def test_a_password_travels_only_in_the_request_and_on_stdin(self):
        token, csrf = self.signed_in()
        secret = "an-entirely-new-password-123"
        status, response, body = self.req("POST", "/api/users/password", {"name": "bob", "password": secret}, token=token, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertEqual(self.ran, [(["chpasswd"], f"bob:{secret}\n")])
        self.assertNotIn(secret, json.dumps(body))
        self.assertNotIn(secret, json.dumps(self.audit.tail(10)))

    def test_the_server_not_the_page_decides_which_sign_in_is_yours(self):
        token_a, csrf_a = self.signed_in()
        status, response, _ = self.login(ip="10.0.0.9")
        self.assertEqual(status, 200)
        token_b = response.getheader("Set-Cookie").split(";")[0].split("=", 1)[1]
        mine = self.sessions.id_of(token_a)
        theirs = self.sessions.id_of(token_b)
        status, _, body = self.req("POST", "/api/users/end-dashboard", {"id": mine, "current_id": "000000000000"}, token=token_a, csrf=csrf_a)
        self.assertEqual((status, "this browser" in body["error"]), (400, True))
        self.assertEqual(self.req("GET", "/api/session", token=token_a)[0], 200)
        status, _, _ = self.req("POST", "/api/users/end-dashboard", {"id": theirs, "current_id": theirs}, token=token_a, csrf=csrf_a)
        self.assertEqual(status, 200)                                                    # a forged current_id does not protect anyone else
        self.assertEqual(self.req("GET", "/api/session", token=token_b)[0], 401)

    def tree(self):
        base = os.path.join(self.work.name, "disk")
        for name in ("projects", ".secret", "alice"):
            os.makedirs(os.path.join(base, name))
        self.data["users"].append({"name": "carol", "uid": 1002, "gid": 1002, "type": "login", "sudo": None, "home": os.path.join(base, "alice"),
                                   "password": "set", "expired": False, "expires": None, "processes": 0, "can_login": True})
        return base

    def test_the_folder_browser_needs_a_session_and_the_switch(self):
        base = self.tree()
        self.assertEqual(self.req("GET", f"/api/folders?path={base}")[0], 401)
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", f"/api/folders?path={base}", token=token)[0], 200)
        self.app.admin = dict(self.app.admin, users=False)
        self.assertEqual(self.req("GET", f"/api/folders?path={base}", token=token)[0], 404)
        self.app.admin = dict(self.app.admin, users=True)
        useradmin, self.app.useradmin = self.app.useradmin, None
        self.assertEqual(self.req("GET", f"/api/folders?path={base}", token=token)[0], 404)
        self.app.useradmin = useradmin

    def test_the_folder_browser_lists_folders_and_explains_what_is_greyed_out(self):
        base = self.tree()
        token, _ = self.signed_in()
        status, _, body = self.req("GET", f"/api/folders?path={base}", token=token)
        self.assertEqual(status, 200)
        self.assertEqual([f["name"] for f in body["folders"]], ["alice", "projects"])               # the hidden one is left out
        by = {f["name"]: f for f in body["folders"]}
        self.assertEqual((by["projects"]["selectable"], by["alice"]["enterable"], by["alice"]["reason"]), (True, False, "the home folder of carol"))
        self.assertEqual(self.req("GET", f"/api/folders?path={base}&hidden=1", token=token)[2]["folders"][0]["name"], ".secret")
        self.assertIn("places", body)
        self.assertNotIn("choice", body)

    def test_a_chosen_name_is_checked_in_place(self):
        base = self.tree()
        token, _ = self.signed_in()
        body = self.req("GET", f"/api/folders?path={base}&name=bob", token=token)[2]
        self.assertEqual(body["choice"], {"path": os.path.join(base, "bob"), "ok": True, "reason": "", "exists": False})
        self.assertFalse(self.req("GET", f"/api/folders?path={base}&name=a%2Fb", token=token)[2]["choice"]["ok"])

    def test_the_folder_browser_refuses_what_it_should(self):
        base = self.tree()
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", "/api/folders?path=/etc", token=token)[0], 403)
        self.assertEqual(self.req("GET", f"/api/folders?path={base}/alice", token=token)[0], 403)
        self.assertEqual(self.req("GET", "/api/folders?path=relative", token=token)[0], 400)
        self.assertEqual(self.req("GET", f"/api/folders?path={base}/nope", token=token)[0], 404)
        self.assertEqual(self.req("GET", f"/api/folders?path={base}/../disk", token=token)[0], 400)
        self.assertEqual(self.req("GET", f"/api/folders?path={base}/alice&for=carol", token=token)[0], 200)      # the owner may look

    def test_ending_a_system_session_and_the_audit_trail(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.req("POST", "/api/users/end-session", {"id": "5"}, token=token, csrf=csrf)[0], 200)
        self.assertEqual(self.ran[-1][0], ["loginctl", "terminate-session", "--", "5"])
        self.assertEqual(self.req("POST", "/api/users/end-session", {"id": "99"}, token=token, csrf=csrf)[0], 404)
        actions = [(e["action"], e["ok"]) for e in self.audit.tail(10)]
        self.assertEqual(actions, [("users.end_session", False), ("users.end_session", True)])


class UsersTabTest(ServerTest):
    def setUp(self):
        super().setUp()
        self.sched.data["users"] = {"users": [{"name": "root", "uid": 0}], "sessions": [], "notes": {}, "sudo_source": "sudoers"}

    def test_it_needs_a_session(self):
        self.assertEqual(self.req("GET", "/api/live?tab=users")[0], 401)

    def test_the_users_data_and_the_dashboard_sign_ins_arrive_together(self):
        token, _ = self.signed_in()
        other = self.req("POST", "/login", {"username": "admin", "password": PASSWORD}, {"X-Real-IP": "10.0.0.2", "User-Agent": "Other browser"})
        self.assertEqual(other[0], 200)
        status, response, body = self.req("GET", "/api/live?tab=users", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["sections"]["users"]["data"]["users"][0]["name"], "root")
        sign_ins = body["dashboard_sessions"]
        self.assertEqual(len(sign_ins), 2)
        self.assertEqual([s["current"] for s in sign_ins].count(True), 1)
        mine = next(s for s in sign_ins if s["current"])
        self.assertEqual(mine["ip"], "10.0.0.1")
        raw = json.dumps(body)
        self.assertNotIn(token, raw)
        for s in sign_ins:
            self.assertEqual(len(s["id"]), 12)

    def test_the_other_tabs_do_not_carry_the_sign_in_list(self):
        token, _ = self.signed_in()
        self.assertNotIn("dashboard_sessions", self.req("GET", "/api/live?tab=overview", token=token)[2])

    def test_a_switched_off_tab_does_not_exist(self):
        token, _ = self.signed_in()
        self.app.admin = dict(self.app.admin, users=False)
        self.assertEqual(self.req("GET", "/api/live?tab=users", token=token)[0], 404)
        self.assertEqual(self.req("GET", "/api/live?tab=overview", token=token)[0], 200)


class PowerApiTest(ServerTest):
    """Reboot, cancel and restart-all through the real server, with the real Actions and Power and a fake system underneath."""

    def setUp(self):
        from dashboard.actions import Actions
        from dashboard.audit import Audit
        from dashboard.power import Power

        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.ran = []
        self.scheduled = os.path.join(self.work.name, "scheduled")
        self.boot = os.path.join(self.work.name, "boot_id")
        with open(self.boot, "w") as f:
            f.write("boot-1")

        def runner(args, timeout=5.0, ok_codes=(0,)):
            self.ran.append(args)
            return "active\n" if args[:2] == ["systemctl", "is-active"] else ""

        cfg = json.loads(config.EXAMPLE_PATH.read_text())

        class Sched:
            def get(self, name):
                return 1.0, {"watched": [{"name": u.removesuffix(".service"), "exists": True} for u in cfg["watch"]["systemd"]]}

            def refresh(self, name):
                return {}

        self.actions = Actions(cfg, Sched(), None, Audit(os.path.join(self.work.name, "audit.jsonl")), runner=runner)
        self.power = Power(cfg, self.actions, Sched(), None, self.actions.audit, self.work.name, runner=runner, hostname="myhost",
                           scheduled_path=self.scheduled, boot_id_path=self.boot, defer=lambda fn, s: None)
        self.power.register()
        super().setUp()

    def post(self, path, body, token, csrf):
        return self.req("POST", path, body, token=token, csrf=csrf)

    def test_everything_needs_a_session_and_the_csrf_token(self):
        for path in ("/api/power/reboot", "/api/power/cancel", "/api/power/restart-all"):
            self.assertEqual(self.req("POST", path, {"confirm": "myhost"}, csrf="x")[0], 401, path)
        self.assertEqual(self.req("GET", "/api/power")[0], 401)
        token, csrf = self.signed_in()
        for path in ("/api/power/reboot", "/api/power/cancel", "/api/power/restart-all"):
            self.assertEqual(self.req("POST", path, {"confirm": "myhost"}, token=token)[0], 403, path)
            self.assertEqual(self.req("POST", path, {"confirm": "myhost"}, token=token, csrf="wrong")[0], 403, path)
        self.assertEqual(self.ran, [])

    def test_a_switched_off_tool_does_not_exist(self):
        token, csrf = self.signed_in()
        self.app.admin = dict(self.app.admin, power=False)
        for path in ("/api/power/reboot", "/api/power/cancel", "/api/power/restart-all"):
            self.assertEqual(self.post(path, {"confirm": "myhost"}, token, csrf)[0], 404, path)
        self.assertEqual(self.req("GET", "/api/power", token=token)[0], 404)
        self.assertEqual(self.ran, [])

    def test_describe_lists_the_plan_and_what_is_never_restarted(self):
        token, _ = self.signed_in()
        status, _, body = self.req("GET", "/api/power", token=token)
        self.assertEqual(status, 200)
        self.assertEqual((body["hostname"], body["delays"], body["scheduled"], body["job"]), ("myhost", [0, 60], None, None))
        self.assertTrue(body["restart"]["units"])
        for never in ("server-dashboard", "admin-dns", "tailscaled"):
            self.assertNotIn(never, body["restart"]["units"])

    def test_reboot_needs_the_typed_hostname(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.post("/api/power/reboot", {"confirm": "wrong", "delay": 60}, token, csrf)[0], 400)
        self.assertEqual(self.post("/api/power/reboot", {"delay": 60}, token, csrf)[0], 400)
        self.assertEqual(self.ran, [])
        status, _, body = self.post("/api/power/reboot", {"confirm": "myhost", "delay": 60}, token, csrf)
        self.assertEqual(status, 200)
        self.assertEqual(self.ran, [["shutdown", "-r", "+1", "Reboot from the admin dashboard by admin"]])
        self.assertIn("1 minute", body["detail"])

    def test_cancel_only_when_something_is_scheduled(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.post("/api/power/cancel", {}, token, csrf)[0], 409)
        with open(self.scheduled, "w") as f:
            f.write(f"USEC={int((time.time() + 40) * 1_000_000)}\nMODE=reboot\n")
        self.assertEqual(self.post("/api/power/cancel", {}, token, csrf)[0], 200)
        self.assertEqual(self.ran, [["shutdown", "-c"]])

    def test_live_data_carries_a_scheduled_shutdown_to_every_page(self):
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", "/api/live?tab=overview", token=token)[2]["power"], {"scheduled": None})
        with open(self.scheduled, "w") as f:
            f.write(f"USEC={int((time.time() + 40) * 1_000_000)}\nMODE=reboot\nWALL_MESSAGE=soon\n")
        scheduled = self.req("GET", "/api/live?tab=network", token=token)[2]["power"]["scheduled"]
        self.assertEqual((scheduled["mode"], scheduled["message"]), ("reboot", "soon"))
        self.assertTrue(38 <= scheduled["in_s"] <= 40)

    def test_restart_all_runs_as_a_job_that_the_page_can_follow(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.post("/api/power/restart-all", {"confirm": "nope"}, token, csrf)[0], 400)
        status, _, body = self.post("/api/power/restart-all", {"confirm": "myhost"}, token, csrf)
        self.assertEqual(status, 200)
        job = None
        for _ in range(200):
            job = self.req("GET", f"/api/jobs/{body['job']}", token=token)[2]
            if job["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "ok")
        self.assertEqual(job["started_by"], "admin")
        self.assertTrue(job["steps"] and all(s["state"] == "ok" for s in job["steps"]))
        restarted = [a[3] for a in self.ran if a[:2] == ["systemctl", "restart"]]
        self.assertEqual(restarted, [f"{s['label'].split()[1]}.service" for s in job["steps"]])


class JobApiTest(ServerTest):
    def setUp(self):
        self.actions = FakeActions()
        super().setUp()

    def test_a_job_can_be_read_with_a_session_only(self):
        job = self.actions.jobs.create("demo", "demo thing", "tester")
        job.step("first")
        self.assertEqual(self.req("GET", f"/api/jobs/{job.id}")[0], 401)
        token, _ = self.signed_in()
        status, _, body = self.req("GET", f"/api/jobs/{job.id}", token=token)
        self.assertEqual(status, 200)
        self.assertEqual((body["state"], body["steps"][0]["label"], body["started_by"]), ("running", "first", "tester"))
        job.finish(True, "ok")
        self.assertEqual(self.req("GET", f"/api/jobs/{job.id}", token=token)[2]["state"], "ok")

    def test_unknown_or_malformed_ids_are_404(self):
        token, _ = self.signed_in()
        for job_id in ("0" * 16, "../etc/passwd", "ABCDEF0123456789", "a" * 17, "", "%00"):
            self.assertEqual(self.req("GET", f"/api/jobs/{job_id}", token=token)[0], 404, job_id)


class JobApiWithoutActionsTest(ServerTest):
    def test_jobs_do_not_exist_without_actions(self):
        token, _ = self.signed_in()
        self.assertEqual(self.req("GET", "/api/jobs/" + "0" * 16, token=token)[0], 404)


class AdminSwitchTest(ServerTest):
    def test_session_reports_every_switch_and_the_example_config_has_them_on(self):
        token, _ = self.signed_in()
        admin = self.req("GET", "/api/session", token=token)[2]["admin"]
        self.assertEqual(sorted(admin), sorted(config.ADMIN_SWITCHES))
        self.assertTrue(all(admin.values()))

    def test_a_switched_off_tool_is_a_404_and_an_unknown_one_too(self):
        self.app.admin = {"power": True, "users": False}
        self.app.require_feature("power")
        for name in ("users", "never_heard_of"):
            with self.assertRaises(HttpError) as raised:
                self.app.require_feature(name)
            self.assertEqual((raised.exception.status, raised.exception.message), (404, "not found"))

    def test_a_config_without_the_block_has_everything_off(self):
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        del cfg["admin"]
        app = App(cfg, self.sched, self.history, self.sessions)
        self.assertFalse(any(app.admin.values()))
        with self.assertRaises(HttpError):
            app.require_feature("power")


class ActionsDisabledTest(ServerTest):
    def test_without_actions_the_endpoints_say_so(self):
        token, csrf = self.signed_in()
        self.assertEqual(self.req("POST", "/api/action/service", {}, token=token, csrf=csrf)[0], 404)
        self.assertEqual(self.req("GET", "/api/session", token=token)[2]["actions"], False)


class LoadSheddingTest(ServerTest):
    max_connections = 1

    def test_connections_beyond_the_cap_are_dropped(self):
        busy = socket.create_connection(("127.0.0.1", self.port))      # holds the only slot
        try:
            time.sleep(0.3)
            extra = socket.create_connection(("127.0.0.1", self.port))
            extra.settimeout(3)
            self.assertEqual(extra.recv(1), b"")
            extra.close()
        finally:
            busy.close()
        time.sleep(0.3)
        self.assertEqual(self.req("GET", "/healthz")[0], 200)           # the slot was released


if __name__ == "__main__":
    unittest.main()
