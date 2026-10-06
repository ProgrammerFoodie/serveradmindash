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
from dashboard.server import App, make_server

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
        self.app = App(cfg, self.sched, self.history, self.sessions, self.limiter, alerts=self.alerts, actions=self.actions)
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
