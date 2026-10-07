import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path

from dashboard import auth, config
from dashboard.actions import ActionError, Actions
from dashboard.audit import Audit
from dashboard.useradmin import UserAdmin, home_problem, password_problem
from dashboard.util import CommandError

ME, MY_GID = os.getuid(), os.getgid()
WHO = {"user": "tester", "ip": "10.0.0.1"}
SECRET = "correct-horse-battery-STAPLE"
NOW = 1_700_000_000.0


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def acct(name, uid, **over):
    base = {"name": name, "uid": uid, "gid": uid, "type": "login", "comment": "", "home": f"/data/homes/{name}", "shell": "/bin/bash",
            "groups": [name], "primary_group": name, "sudo": None, "sudo_via": [], "nopasswd": False, "password": "set",
            "password_changed": None, "must_change": False, "password_expires": None, "password_expired": False, "expires": None,
            "expired": False, "last_login": None, "processes": 0, "can_login": True, "blockers": [], "keys": 1, "keys_error": None,
            "home_exists": True, "home_owner": name, "home_mode": "0750"}
    base.update(over)
    return base


def snapshot():
    return {"users": [acct("root", 0, type="root", sudo="full", home="/root"),
                      acct("alice", 1000, sudo="full"),
                      acct("bob", 1001),
                      acct("carol", 1002, sudo="full"),
                      acct("daemon", 1, type="system", password="none", home="/usr/sbin", shell="/usr/sbin/nologin", can_login=False)],
            "sessions": [{"id": "77", "user": "bob", "uid": 1001, "service": "sshd", "from": "10.1.1.1"}],
            "sudo_source": "sudoers", "shells": ["/bin/sh", "/bin/bash"], "has_sudo_group": True, "notes": {}}


class FakeScheduler:
    def __init__(self, case):
        self.case = case

    def refresh(self, name):
        self.case.refreshes += 1
        return self.case.data


class FakeAlerts:
    server_name = "myhost"

    def __init__(self):
        self.events = []

    def event(self, key, text, level="info", cooldown_s=900):
        self.events.append((level, text))
        return True

    def mute(self, *a, **k):
        pass


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = Path(self.tmp.name)
        self.root = r
        for folder in ("sudoers.d", "systemd", "supervisor/conf.d", "crontabs", "sshd.d", "homes", "data"):
            (r / folder).mkdir(parents=True)
        (r / "group").write_text("root:x:0:\nsudo:x:27:alice,carol\nalice:x:1000:\nbob:x:1001:\ncarol:x:1002:\ndaemon:x:1:\n")
        (r / "sudoers").write_text("root ALL=(ALL) ALL\n%sudo ALL=(ALL:ALL) ALL\n")
        self.data = snapshot()
        self.refreshes = 0
        self.calls, self.broken = [], []
        self.alerts = FakeAlerts()
        self.audit = Audit(r / "audit.jsonl")
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        cfg["watch"]["systemd"] = []
        self.cfg = cfg
        self.actions = Actions(cfg, FakeScheduler(self), self.alerts, self.audit, runner=lambda *a, **k: "")
        self.sessions = auth.Sessions(r / "auth.db", "fp", hours=1)
        self.addCleanup(self.sessions.close)
        self.paths = {"group": str(r / "group"), "sudoers": str(r / "sudoers"), "sudoers_dir": str(r / "sudoers.d"), "systemd_dir": str(r / "systemd"),
                      "supervisor_dir": str(r / "supervisor"), "crontabs": str(r / "crontabs"), "sshd_config": str(r / "sshd_config"),
                      "sshd_dir": str(r / "sshd.d")}
        self.systemctl_show = ""
        self.admin = UserAdmin(cfg, self.actions, FakeScheduler(self), self.alerts, self.audit, self.sessions, r / "data",
                               runner=self.runner, paths=self.paths, clock=lambda: NOW, protect=(str(r / "dashboard"),), forbidden=("/etc", "/usr", "/root"))
        self.admin.register()

    def runner(self, args, timeout=5.0, ok_codes=(0,), stdin=None, secret=None):
        self.calls.append({"args": list(args), "stdin": stdin, "secret": secret, "ok_codes": ok_codes})
        for prefix, message in self.broken:
            if args[:len(prefix)] == prefix:
                raise CommandError(message)
        if args[:2] == ["systemctl", "show"]:
            return self.systemctl_show
        return ""

    def do(self, op, **body):
        return self.actions.perform(f"users.{op}", body, WHO)

    def refused(self, op, status, **body):
        with self.assertRaises(ActionError) as raised:
            self.do(op, **body)
        self.assertEqual(raised.exception.status, status, raised.exception.message)
        return raised.exception.message

    def argv(self):
        return [c["args"] for c in self.calls]

    def entries(self):
        return self.audit.tail(50)

    def user(self, name):
        return next(u for u in self.data["users"] if u["name"] == name)


class AddTest(Case):
    GOOD = {"name": "dave", "shell": "/bin/bash", "comment": "Dave D", "home": None}

    def home(self, name="dave"):
        return str(self.root / "homes" / name)

    def add(self, **over):
        body = {"name": "dave", "shell": "/bin/bash", "comment": "Dave D", "home": self.home(), **over}
        return self.do("add", **body)

    def test_creates_the_account_with_exactly_these_arguments(self):
        result = self.add()
        self.assertEqual(self.argv(), [["useradd", "-m", "-U", "-d", self.home(), "-s", "/bin/bash", "-c", "Dave D", "--", "dave"]])
        self.assertIn("created dave", result["detail"])
        self.assertIn("they cannot log in until", result["detail"])                          # no password and no key yet
        self.assertGreaterEqual(self.refreshes, 2)                                             # the list is refreshed afterwards

    def test_without_a_home_the_default_is_under_home(self):
        self.do("add", name="dave", shell="/bin/bash")
        self.assertEqual(self.argv()[0], ["useradd", "-m", "-U", "-d", "/home/dave", "-s", "/bin/bash", "-c", "", "--", "dave"])

    def test_sudo_adds_the_group(self):
        self.add(sudo=True)
        self.assertEqual(self.argv()[0], ["useradd", "-m", "-U", "-d", self.home(), "-s", "/bin/bash", "-c", "Dave D", "-G", "sudo", "--", "dave"])

    def test_the_password_goes_to_chpasswd_on_stdin_only(self):
        result = self.add(password=SECRET)
        self.assertEqual(self.argv()[1], ["chpasswd"])
        self.assertEqual((self.calls[1]["stdin"], self.calls[1]["secret"]), (f"dave:{SECRET}\n", SECRET))
        self.assertTrue(all(SECRET not in " ".join(a) for a in self.argv()))                   # never on a command line
        self.assertIn("password set", result["detail"])
        self.assertNotIn(SECRET, json.dumps(self.entries()) + str(self.alerts.events) + json.dumps(result))

    def test_must_change_forces_a_new_password_at_first_login(self):
        self.add(password=SECRET, must_change=True)
        self.assertEqual(self.argv()[2], ["chage", "-d", "0", "--", "dave"])

    def test_bad_names_are_refused_and_nothing_runs(self):
        for name in ("Dave", "1dave", "-dave", "d" * 33, "da ve", "da:ve", "da$", "dave\n", "", None, 5, ["dave"], "root", "bob", "sudo", "da.ve", "../x"):
            self.refused("add", 400 if name not in ("root", "bob", "sudo") else 409, **{**self.GOOD, "name": name, "home": self.home()})
        self.assertEqual(self.calls, [])

    def test_a_name_that_is_a_group_is_refused(self):
        self.assertIn("group", self.refused("add", 409, name="sudo", shell="/bin/bash"))
        self.assertIn("user", self.refused("add", 409, name="alice", shell="/bin/bash"))

    def test_a_shell_must_be_listed(self):
        for shell in ("/bin/zsh", "/usr/sbin/nologin", "bash", "", None, 5):
            self.refused("add", 400, name="dave", shell=shell, home=self.home())
        self.assertEqual(self.calls, [])

    def test_the_full_name_cannot_smuggle_in_fields(self):
        for comment in ("a:b", "a,b", "a\\b", "line\nbreak", "tab\there", "x" * 101, 5, ["x"]):
            self.refused("add", 400, **{**self.GOOD, "comment": comment, "home": self.home()})
        self.add(comment="")                                                                      # empty and missing are fine
        self.assertEqual(len(self.calls), 1)

    def test_homes_must_be_ordinary_places_that_do_not_exist_yet(self):
        for home in ("relative/path", "/etc/dave", "/usr/dave", "/root/dave", "/data/homes/bob/dave", "/data/homes", str(self.root / "missing" / "dave"),
                     str(self.root / "homes" / ".." / "dave"), "/x", "//data//dave", "a\nb"):
            self.refused("add", 400, **{**self.GOOD, "home": home})
        Path(self.home()).mkdir()
        self.assertIn("already exists", self.refused("add", 409, **{**self.GOOD, "home": self.home()}))
        self.assertEqual(self.calls, [])

    def test_a_home_behind_a_symlink_is_refused(self):
        target = self.root / "elsewhere"
        target.mkdir()
        os.symlink(target, self.root / "link")
        self.assertIn("symbolic link", self.refused("add", 400, **{**self.GOOD, "home": str(self.root / "link" / "dave")}))

    def test_passwords_are_checked_before_anything_is_created(self):
        for password in ("short", "x" * 1025, "has\nnewline-in-it", "nul\x00byte-in-it", 12345678901, ["x" * 12]):
            self.refused("add", 400, **{**self.GOOD, "home": self.home(), "password": password})
        self.assertEqual(self.calls, [])

    def test_sudo_needs_a_sudo_group_and_flags_must_be_booleans(self):
        (self.root / "group").write_text("root:x:0:\nalice:x:1000:\n")
        self.refused("add", 400, **{**self.GOOD, "home": self.home(), "sudo": True})
        self.refused("add", 400, **{**self.GOOD, "home": self.home(), "sudo": "yes"})
        self.refused("add", 400, **{**self.GOOD, "home": self.home(), "must_change": 1})
        self.assertEqual(self.calls, [])

    def test_useradd_failing_is_a_502(self):
        self.broken = [(["useradd"], "useradd: exit 9: user exists")]
        self.assertIn("useradd: exit 9", self.refused("add", 502, **{**self.GOOD, "home": self.home()}))

    def test_a_password_failure_after_creation_says_so_and_never_shows_the_password(self):
        self.broken = [(["chpasswd"], f"chpasswd: exit 1: bad {SECRET}")]
        message = self.refused("add", 502, **{**self.GOOD, "home": self.home(), "password": SECRET})
        self.assertIn("dave was created, but the password was not set", message)
        self.assertNotIn(SECRET, message + json.dumps(self.entries()) + str(self.alerts.events))
        self.assertEqual([a[0] for a in self.argv()], ["useradd", "chpasswd"])                    # no rollback: nothing is deleted
        self.assertIn("setting the password", message)

    def test_the_audit_entry_names_the_account_and_not_the_password(self):
        self.add(password=SECRET)
        e = self.entries()[0]
        self.assertEqual((e["action"], e["target"], e["ok"]), ("users.add", "dave", True))
        self.assertNotIn(SECRET, json.dumps(e))


class PasswordTest(Case):
    def test_root_and_login_users_may_get_a_new_password_via_stdin(self):
        for name in ("root", "bob"):
            self.calls.clear()
            result = self.do("password", name=name, password=SECRET)
            self.assertEqual(self.argv(), [["chpasswd"]])
            self.assertEqual((self.calls[0]["stdin"], self.calls[0]["secret"]), (f"{name}:{SECRET}\n", SECRET))
            self.assertIn(f"password of {name} changed", result["detail"])

    def test_system_accounts_and_unknown_names_are_refused(self):
        self.refused("password", 403, name="daemon", password=SECRET)
        self.refused("password", 404, name="nobody-here", password=SECRET)
        self.refused("password", 400, name="-x", password=SECRET)
        self.refused("password", 400, name=None, password=SECRET)
        self.assertEqual(self.calls, [])

    def test_weak_or_odd_passwords_are_refused(self):
        for password in (None, "", "short", "x" * 2000, "new\nline-password", 7):
            self.refused("password", 400, name="bob", password=password)
        self.assertEqual(self.calls, [])

    def test_must_change_and_a_note_for_a_locked_account(self):
        self.user("bob").update(expired=True)
        result = self.do("password", name="bob", password=SECRET, must_change=True)
        self.assertEqual(self.argv()[1], ["chage", "-d", "0", "--", "bob"])
        self.assertIn("still locked or expired", result["detail"])
        self.refused("password", 400, name="bob", password=SECRET, must_change="yes")

    def test_failure_hides_the_password(self):
        self.broken = [(["chpasswd"], f"chpasswd: exit 1: {SECRET} rejected")]
        message = self.refused("password", 502, name="bob", password=SECRET)
        self.assertEqual(self.entries()[0]["ok"], False)
        self.assertNotIn(SECRET, json.dumps(self.entries()) + str(self.alerts.events))
        self.assertIn("chpasswd", message)

    def test_password_problem_function(self):
        self.assertIsNone(password_problem("0123456789"))
        self.assertIsNotNone(password_problem("012345678"))


class LockTest(Case):
    def test_lock_uses_L_and_expires_the_account(self):
        self.do("lock", name="bob")
        self.assertEqual(self.argv(), [["usermod", "-L", "-e", "1", "--", "bob"]])

    def test_an_account_without_a_password_is_only_expired(self):
        self.user("bob").update(password="none")
        self.do("lock", name="bob")
        self.assertEqual(self.argv(), [["usermod", "-e", "1", "--", "bob"]])
        self.calls.clear()
        self.user("carol").update(password="empty", sudo=None)
        self.do("lock", name="carol")
        self.assertEqual(self.argv(), [["usermod", "-L", "-e", "1", "--", "carol"]])

    def test_protected_accounts_are_refused(self):
        self.refused("lock", 403, name="root")
        self.refused("lock", 403, name="daemon")
        self.refused("lock", 404, name="ghost")
        self.assertEqual(self.calls, [])

    def test_an_account_that_is_already_locked_is_a_409(self):
        for state in ({"password": "locked"}, {"expired": True}):
            self.user("bob").update(**state)
            self.refused("lock", 409, name="bob")
        self.assertEqual(self.calls, [])

    def test_the_old_expiry_date_is_remembered_for_unlock(self):
        self.user("bob").update(expires=19900 * 86400)
        self.do("lock", name="bob")
        self.assertEqual(self.admin.ledger.get("bob")["expires_day"], 19900)
        self.assertEqual(oct(os.stat(self.root / "data" / "locks.json").st_mode & 0o777), "0o600")

    def test_a_failed_lock_leaves_no_ledger_entry(self):
        self.broken = [(["usermod"], "usermod: exit 1: busy")]
        self.refused("lock", 502, name="bob")
        self.assertIsNone(self.admin.ledger.get("bob"))

    def test_the_last_user_with_full_sudo_cannot_be_locked(self):
        self.user("carol").update(sudo=None)                       # alice is now the only one with full sudo
        message = self.refused("lock", 403, name="alice")
        self.assertIn("last user with full sudo rights", message)
        self.assertEqual(self.calls, [])

    def test_a_second_admin_must_be_able_to_log_in_to_count(self):
        for broken in ({"can_login": False}, {"sudo": "limited"}, {"sudo": None}, {"type": "system"}, {"type": "root"}):
            self.setUp()
            self.user("carol").update(**broken)
            self.refused("lock", 403, name="alice")
        self.setUp()
        self.do("lock", name="alice")                                # carol is a proper second admin
        self.assertEqual(self.argv(), [["usermod", "-L", "-e", "1", "--", "alice"]])

    def test_the_guard_does_not_apply_to_people_without_full_sudo(self):
        self.user("alice").update(sudo=None)
        self.user("carol").update(sudo=None)
        self.do("lock", name="bob")
        self.assertEqual(len(self.calls), 1)

    def test_without_readable_sudoers_a_sudo_user_is_not_locked_blindly(self):
        self.data["sudo_source"] = "groups"
        self.assertIn("sudoers is not readable", self.refused("lock", 502, name="alice"))
        self.do("lock", name="bob")                                  # someone without sudo is fine

    def test_the_account_list_failing_to_load_stops_everything(self):
        self.data = {"error": "OSError: boom"}
        self.assertIn("cannot read the account list", self.refused("lock", 502, name="bob"))
        self.assertEqual(self.calls, [])


class UnlockTest(Case):
    def test_unlock_restores_the_original_expiry_and_un_switches_the_password(self):
        self.user("bob").update(expires=19900 * 86400)
        self.do("lock", name="bob")
        self.user("bob").update(password="locked", expired=True, expires=86400)
        self.calls.clear()
        result = self.do("unlock", name="bob")
        self.assertEqual(self.argv(), [["usermod", "-U", "-e", "19900", "--", "bob"]])
        self.assertIsNone(self.admin.ledger.get("bob"))
        self.assertIn("is unlocked", result["detail"])

    def test_without_a_remembered_date_the_expiry_is_cleared(self):
        self.user("bob").update(expired=True, expires=86400)
        self.do("unlock", name="bob")
        self.assertEqual(self.argv(), [["usermod", "-e", "", "--", "bob"]])

    def test_u_is_used_only_when_there_is_a_real_hash_behind_the_lock(self):
        self.user("bob").update(expired=True, password="none")
        result = self.do("unlock", name="bob")
        self.assertNotIn("-U", self.argv()[0])                       # "-U" on "!" would leave an empty password
        self.assertIn("no password", result["detail"])

    def test_an_original_expiry_in_the_past_is_said_out_loud(self):
        self.admin.ledger.put("bob", 100, "x")
        self.user("bob").update(expired=True, expires=86400)
        self.assertIn("still expired", self.do("unlock", name="bob")["detail"])

    def test_not_locked_is_a_409_and_protected_accounts_are_refused(self):
        self.refused("unlock", 409, name="bob")
        self.refused("unlock", 403, name="root")
        self.refused("unlock", 403, name="daemon")
        self.assertEqual(self.calls, [])

    def test_unban_is_the_same_thing(self):
        self.user("bob").update(password="locked", expired=True)
        self.do("unban", name="bob")
        self.assertEqual(self.argv(), [["usermod", "-U", "-e", "", "--", "bob"]])
        self.assertEqual(self.entries()[0]["action"], "users.unban")


class BanTest(Case):
    def test_ban_needs_the_name_typed(self):
        for confirm in (None, "", "BOB", "alice", 5):
            self.refused("ban", 400, name="bob", **({} if confirm is None else {"confirm": confirm}))
        self.assertEqual(self.calls, [])

    def test_ban_locks_and_ends_the_sessions(self):
        result = self.do("ban", name="bob", confirm="bob")
        self.assertEqual(self.argv(), [["usermod", "-L", "-e", "1", "--", "bob"], ["loginctl", "terminate-user", "--", "bob"]])
        self.assertEqual(self.calls[1]["ok_codes"], (0, 1))
        self.assertIn("its sessions are ended", result["detail"])

    def test_ban_says_when_there_were_no_sessions(self):
        self.data["sessions"] = []
        self.assertIn("no open sessions", self.do("ban", name="bob", confirm="bob")["detail"])

    def test_banning_the_last_admin_is_refused(self):
        self.user("carol").update(sudo=None)
        self.refused("ban", 403, name="alice", confirm="alice")
        self.assertEqual(self.calls, [])

    def test_banning_an_account_that_is_already_locked_still_ends_its_sessions(self):
        self.user("alice").update(expired=True, password="locked")
        self.user("carol").update(sudo=None)                         # even though alice would be the "last admin", nothing more is lost
        self.do("ban", name="alice", confirm="alice")
        self.assertEqual(self.argv(), [["loginctl", "terminate-user", "--", "alice"]])

    def test_a_failure_to_end_sessions_is_reported_after_the_lock(self):
        self.broken = [(["loginctl"], "loginctl: exit 2: failed")]
        self.assertIn("locked, but ending its sessions failed", self.refused("ban", 502, name="bob", confirm="bob"))
        self.assertEqual(self.argv()[0][0], "usermod")


class SessionsTest(Case):
    def test_a_system_session_is_ended_by_id(self):
        result = self.do("end_session", id="77")
        self.assertEqual(self.argv(), [["loginctl", "terminate-session", "--", "77"]])
        self.assertIn("session 77 of bob", result["detail"])

    def test_unknown_or_malformed_ids_are_refused(self):
        self.refused("end_session", 404, id="99")
        for bad in ("", "7 7", "-1", "7;rm", "x" * 17, None, 77, ["77"]):
            self.refused("end_session", 400, id=bad)
        self.assertEqual(self.calls, [])

    def test_a_dashboard_sign_in_is_ended_by_id(self):
        token, _ = self.sessions.create("2.2.2.2", "Other browser")
        sign_in = self.sessions.list_active()[0]["id"]
        result = self.do("end_dashboard", id=sign_in)
        self.assertIn("ended", result["detail"])
        self.assertIsNone(self.sessions.lookup(token))
        self.assertEqual(self.calls, [])                              # no system command involved

    def test_the_current_browser_cannot_be_ended_from_here(self):
        token, _ = self.sessions.create("2.2.2.2", "Me")
        mine = self.sessions.id_of(token)
        self.refused("end_dashboard", 400, id=mine, current_id=mine)
        self.assertIsNotNone(self.sessions.lookup(token))

    def test_unknown_or_malformed_dashboard_ids(self):
        self.refused("end_dashboard", 404, id="0" * 12)
        for bad in ("", "xyz", "0" * 11, "0" * 13, "ABCDEF012345", None, 5):
            self.refused("end_dashboard", 400, id=bad)


class RenameTest(Case):
    def setUp(self):
        super().setUp()
        self.user("bob")["home"] = str(self.root / "homes" / "bob")
        (self.root / "homes" / "bob").mkdir()

    def rename(self, **over):
        return self.do("rename", **{"name": "bob", "new_name": "robert", "confirm": "bob", **over})

    def test_renames_the_account_and_its_private_group(self):
        result = self.rename()
        self.assertEqual(self.argv(), [["usermod", "-l", "robert", "--", "bob"], ["groupmod", "-n", "robert", "--", "bob"]])
        self.assertIn("bob is now robert", result["detail"])
        self.assertIn("its group is renamed too", result["detail"])

    def test_a_group_that_is_not_its_private_one_is_left_alone(self):
        (self.root / "group").write_text("root:x:0:\nbob:x:5555:\nalice:x:1000:\n")                 # a group called bob, but not bob's own
        self.rename()
        self.assertEqual(self.argv(), [["usermod", "-l", "robert", "--", "bob"]])

    def test_the_name_must_be_typed(self):
        for confirm in (None, "", "Bob", "robert", 5):
            self.refused("rename", 400, name="bob", new_name="robert", **({} if confirm is None else {"confirm": confirm}))
        self.assertEqual(self.calls, [])

    def test_the_new_name_follows_the_same_rules_as_a_new_account(self):
        for new in ("Robert", "1x", "-x", "r r", "", None, 5, "x" * 33):
            self.refused("rename", 400, new_name=new, name="bob", confirm="bob")
        self.refused("rename", 409, name="bob", new_name="alice", confirm="bob")                # taken by a user
        self.refused("rename", 409, name="bob", new_name="sudo", confirm="bob")                 # taken by a group
        self.assertEqual(self.calls, [])

    def test_only_login_users_can_be_renamed(self):
        self.refused("rename", 403, name="root", new_name="boss", confirm="root")
        self.refused("rename", 403, name="daemon", new_name="x", confirm="daemon")
        self.refused("rename", 404, name="ghost", new_name="x", confirm="ghost")
        self.assertEqual(self.calls, [])

    def test_running_processes_block_it(self):
        self.user("bob")["processes"] = 3
        self.assertIn("3 processes are running as it", self.refused("rename", 409, name="bob", new_name="robert", confirm="bob"))
        self.user("bob")["processes"] = 1
        self.assertIn("1 process is running as it", self.refused("rename", 409, name="bob", new_name="robert", confirm="bob"))
        self.assertEqual(self.calls, [])

    def test_every_kind_of_dependent_blocks_it_and_says_where(self):
        cases = {
            "service": (lambda: (self.root / "systemd" / "app.service").write_text("[Service]\nUser=bob\nExecStart=/x\n"), "the service app runs as it"),
            "service by uid": (lambda: (self.root / "systemd" / "app.service").write_text("[Service]\nUser=1001\n"), "the service app runs as it"),
            "service group": (lambda: (self.root / "systemd" / "app.service").write_text("[Service]\nGroup=bob\n"), "the service app runs as it"),
            "drop-in": (lambda: ((self.root / "systemd" / "web.service.d").mkdir(), (self.root / "systemd" / "web.service.d" / "x.conf").write_text("[Service]\nUser=bob\n")),
                        "the service web runs as it"),
            "supervisor": (lambda: (self.root / "supervisor" / "conf.d" / "q.conf").write_text("[program:queue]\ncommand=x\nuser=bob\n"), "[program:queue] in q.conf"),
            "sudoers": (lambda: (self.root / "sudoers.d" / "bobrule").write_text("bob ALL=(ALL) NOPASSWD: ALL\n"), "sudoers.d/bobrule names it"),
            "sudoers alias": (lambda: (self.root / "sudoers").write_text("User_Alias OPS = alice, bob\n"), "OPS (User_Alias) names it"),
            "crontab": (lambda: (self.root / "crontabs" / "bob").write_text("* * * * * x\n"), "it has a crontab"),
            "sshd allow": (lambda: (self.root / "sshd_config").write_text("PermitRootLogin no\nAllowUsers alice bob\n"), "sshd allows or denies it by name"),
            "sshd match": (lambda: (self.root / "sshd_config").write_text("Match User carol,bob\n  X11Forwarding no\n"), "sshd allows or denies it by name"),
            "sshd drop-in": (lambda: (self.root / "sshd.d" / "10.conf").write_text("DenyUsers bob@10.0.0.*\n"), "sshd allows or denies it by name"),
        }
        for name, (make, expected) in cases.items():
            self.setUp()
            make()
            message = self.refused("rename", 409, name="bob", new_name="robert", confirm="bob")
            self.assertIn(expected, message, name)
            self.assertEqual(self.calls, [], name)

    def test_systemd_is_asked_about_the_watched_units_too(self):
        self.cfg["watch"]["systemd"] = ["nginx", "other.service"]
        self.systemctl_show = "Id=nginx.service\nUser=bob\nGroup=\n\nId=other.service\nUser=\nGroup=\n"
        message = self.refused("rename", 409, name="bob", new_name="robert", confirm="bob")
        self.assertIn("the service nginx runs as it", message)
        self.assertEqual(self.argv(), [["systemctl", "show", "-p", "Id", "-p", "User", "-p", "Group", "--", "nginx.service", "other.service"]])

    def test_unrelated_names_do_not_block(self):
        (self.root / "systemd" / "app.service").write_text("[Service]\nUser=bobby\nGroup=bobcats\n")
        (self.root / "sudoers.d" / "x").write_text("bobby ALL=(ALL) ALL\n")
        (self.root / "sshd_config").write_text("AllowUsers bobby alice\n")
        (self.root / "crontabs" / "bobby").write_text("x\n")
        self.rename()
        self.assertEqual(self.argv()[0], ["usermod", "-l", "robert", "--", "bob"])

    def test_something_that_cannot_be_read_is_a_blocker_not_a_pass(self):
        broken = self.root / "systemd" / "locked.service"
        broken.write_text("[Service]\nUser=nobody\n")
        os.chmod(broken, 0)
        self.addCleanup(os.chmod, broken, 0o600)
        if os.access(broken, os.R_OK):
            self.skipTest("running as root: permissions do not apply")
        self.assertIn("cannot read", self.refused("rename", 409, name="bob", new_name="robert", confirm="bob"))
        self.assertEqual(self.calls, [])

    def test_the_home_folder_can_be_renamed_with_it(self):
        self.rename(rename_home=True)
        self.assertEqual(self.argv()[-1], ["usermod", "-d", str(self.root / "homes" / "robert"), "-m", "--", "robert"])

    def test_the_home_is_renamed_only_when_it_is_named_after_the_user(self):
        self.user("bob")["home"] = str(self.root / "homes" / "something-else")
        self.assertIn("not named after the user", self.refused("rename", 400, name="bob", new_name="robert", confirm="bob", rename_home=True))
        (self.root / "homes" / "robert").mkdir()
        self.user("bob")["home"] = str(self.root / "homes" / "bob")
        self.assertIn("already exists", self.refused("rename", 409, name="bob", new_name="robert", confirm="bob", rename_home=True))
        self.refused("rename", 400, name="bob", new_name="robert", confirm="bob", rename_home="yes")
        self.assertEqual(self.calls, [])

    def test_a_failing_group_rename_puts_the_account_name_back(self):
        self.broken = [(["groupmod"], "groupmod: exit 10: cannot lock")]
        message = self.refused("rename", 502, name="bob", new_name="robert", confirm="bob")
        self.assertIn("put back to bob", message)
        self.assertEqual(self.argv()[-1], ["usermod", "-l", "bob", "--", "robert"])

    def test_if_putting_it_back_fails_too_the_message_says_what_state_it_is_in(self):
        self.broken = [(["groupmod"], "groupmod: exit 10: x"), (["usermod", "-l", "bob"], "usermod: exit 1: y")]
        message = self.refused("rename", 502, name="bob", new_name="robert", confirm="bob")
        self.assertIn("account is now called robert but its group is still bob", message)

    def test_a_failed_home_move_is_reported_as_a_partial_success(self):
        self.broken = [(["usermod", "-d"], "usermod: exit 12: cannot move")]
        message = self.refused("rename", 502, name="bob", new_name="robert", confirm="bob", rename_home=True)
        self.assertIn("bob is now robert, but the home folder was not moved", message)

    def test_a_remembered_expiry_follows_the_new_name(self):
        self.admin.ledger.put("bob", 19900, "x")
        self.rename()
        self.assertIsNone(self.admin.ledger.get("bob"))
        self.assertEqual(self.admin.ledger.get("robert")["expires_day"], 19900)

    def test_the_audit_target_shows_both_names(self):
        self.rename()
        self.assertEqual(self.entries()[0]["target"], "bob -> robert")


class HomeTest(Case):
    def setUp(self):
        super().setUp()
        self.user("bob").update(uid=ME, gid=MY_GID, home=str(self.root / "homes" / "bob"))
        (self.root / "homes" / "bob").mkdir()

    def new(self, name="newplace"):
        return str(self.root / "homes" / name)

    def change(self, **over):
        return self.do("home", **{"name": "bob", "path": self.new(), "confirm": "bob", **over})

    def test_a_new_folder_is_created_for_the_user_and_set_as_home(self):
        result = self.change()
        self.assertEqual(self.argv(), [["usermod", "-d", self.new(), "--", "bob"]])
        self.assertEqual(oct(os.stat(self.new()).st_mode & 0o777), "0o750")
        self.assertEqual(os.stat(self.new()).st_uid, ME)
        self.assertIn("created", result["detail"])

    def test_the_name_must_be_typed(self):
        for confirm in (None, "", "BOB", 5):
            self.refused("home", 400, name="bob", path=self.new(), **({} if confirm is None else {"confirm": confirm}))
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(self.new()))

    def test_unsuitable_paths_are_refused_and_nothing_is_created(self):
        for path in ("relative", "/etc/x", "/usr/x/y", "/root/bob", "/data/homes/alice/x", "/x", "", None, 5, str(self.root / "missing" / "x"),
                     str(self.root / "homes" / ".." / "x"), "/data//x"):
            self.refused("home", 400, name="bob", path=path, confirm="bob")
        self.assertEqual(self.calls, [])

    def test_it_must_be_a_change(self):
        self.refused("home", 400, name="bob", path=self.user("bob")["home"], confirm="bob")

    def test_an_existing_folder_of_the_user_is_just_used(self):
        Path(self.new()).mkdir()
        self.assertIn("now used", self.change()["detail"])
        self.assertEqual(self.argv(), [["usermod", "-d", self.new(), "--", "bob"]])

    def test_a_folder_that_is_not_theirs_is_refused(self):
        Path(self.new()).mkdir()
        self.user("bob")["uid"] = ME + 5000
        self.assertIn("not a folder owned by bob", self.refused("home", 409, name="bob", path=self.new(), confirm="bob"))
        self.user("bob")["uid"] = ME
        Path(self.new("afile")).write_text("x")
        self.refused("home", 409, name="bob", path=self.new("afile"), confirm="bob")
        os.symlink(self.root, self.new("alink"))
        self.refused("home", 409, name="bob", path=self.new("alink"), confirm="bob")
        self.assertEqual(self.calls, [])

    def test_a_folder_created_for_a_command_that_fails_is_removed_again(self):
        self.broken = [(["usermod"], "usermod: exit 1: nope")]
        self.refused("home", 502, name="bob", path=self.new(), confirm="bob")
        self.assertFalse(os.path.exists(self.new()))
        Path(self.new("kept")).mkdir()
        self.refused("home", 502, name="bob", path=self.new("kept"), confirm="bob")
        self.assertTrue(os.path.exists(self.new("kept")))                                         # one that was already there stays

    def test_moving_runs_usermod_with_m(self):
        result = self.change(move=True)
        self.assertEqual(self.argv(), [["usermod", "-d", self.new(), "-m", "--", "bob"]])
        self.assertIn("moved there", result["detail"])
        self.assertFalse(os.path.exists(self.new()))                                              # usermod makes it, not us

    def test_moving_is_refused_while_processes_run_or_the_target_exists_or_the_source_is_odd(self):
        self.user("bob")["processes"] = 2
        self.assertIn("2 process", self.refused("home", 409, name="bob", path=self.new(), confirm="bob", move=True))
        self.user("bob")["processes"] = 0
        Path(self.new()).mkdir()
        self.assertIn("already exists", self.refused("home", 409, name="bob", path=self.new(), confirm="bob", move=True))
        os.rmdir(self.new())
        self.user("bob")["home"] = "/usr/lib/x"
        self.refused("home", 400, name="bob", path=self.new(), confirm="bob", move=True)
        self.user("bob")["home"] = str(self.root / "homes" / "gone")
        self.refused("home", 400, name="bob", path=self.new(), confirm="bob", move=True)
        os.symlink(self.root, self.root / "homes" / "linkhome")
        self.user("bob")["home"] = str(self.root / "homes" / "linkhome")
        self.refused("home", 400, name="bob", path=self.new(), confirm="bob", move=True)
        self.assertEqual(self.calls, [])

    def test_protected_accounts_and_odd_flags(self):
        self.refused("home", 403, name="root", path=self.new(), confirm="root")
        self.refused("home", 403, name="daemon", path=self.new(), confirm="daemon")
        self.refused("home", 400, name="bob", path=self.new(), confirm="bob", move="yes")


class RemoveTest(Case):
    def setUp(self):
        super().setUp()
        self.user("bob").update(uid=ME, gid=MY_GID, home=str(self.root / "homes" / "bob"))
        (self.root / "homes" / "bob").mkdir()

    def remove(self, **over):
        return self.do("remove", **{"name": "bob", "confirm": "bob", **over})

    def test_removes_the_account_and_keeps_the_home_by_default(self):
        result = self.remove()
        self.assertEqual(self.argv(), [["userdel", "--", "bob"]])
        self.assertIn("is kept", result["detail"])

    def test_deleting_the_home_too_adds_r(self):
        result = self.remove(delete_home=True)
        self.assertEqual(self.argv(), [["userdel", "-r", "--", "bob"]])
        self.assertIn("with its home folder", result["detail"])

    def test_the_name_must_be_typed(self):
        for confirm in (None, "", "BOB", "alice", 5):
            self.refused("remove", 400, name="bob", **({} if confirm is None else {"confirm": confirm}))
        self.assertEqual(self.calls, [])

    def test_protected_accounts_and_flags(self):
        self.refused("remove", 403, name="root", confirm="root")
        self.refused("remove", 403, name="daemon", confirm="daemon")
        self.refused("remove", 404, name="ghost", confirm="ghost")
        self.refused("remove", 400, name="bob", confirm="bob", delete_home="yes")
        self.assertEqual(self.calls, [])

    def test_the_last_admin_cannot_be_removed(self):
        self.user("carol").update(sudo=None)
        self.refused("remove", 403, name="alice", confirm="alice")
        self.assertEqual(self.calls, [])
        self.user("carol").update(sudo="full")
        self.do("remove", name="alice", confirm="alice")                                          # carol remains
        self.assertEqual(self.argv(), [["userdel", "--", "alice"]])

    def test_processes_services_and_supervisor_block_it(self):
        self.user("bob")["processes"] = 4
        self.assertIn("4 processes", self.refused("remove", 409, name="bob", confirm="bob"))
        self.user("bob")["processes"] = 0
        (self.root / "systemd" / "app.service").write_text("[Service]\nUser=bob\n")
        self.assertIn("the service app runs as it", self.refused("remove", 409, name="bob", confirm="bob"))
        (self.root / "systemd" / "app.service").unlink()
        (self.root / "supervisor" / "conf.d" / "q.conf").write_text("[program:queue]\nuser=bob\n")
        self.assertIn("supervisor entry [program:queue]", self.refused("remove", 409, name="bob", confirm="bob"))
        self.assertEqual(self.calls, [])

    def test_sudoers_crontab_and_sshd_mentions_are_left_behind_with_a_note(self):
        (self.root / "sudoers.d" / "bob").write_text("bob ALL=(ALL) ALL\n")
        (self.root / "crontabs" / "bob").write_text("x\n")
        (self.root / "sshd_config").write_text("AllowUsers bob\n")
        result = self.remove()
        self.assertEqual(self.argv(), [["userdel", "--", "bob"]])
        for expected in ("Left behind", "sudoers.d/bob names it", "it has a crontab", "sshd allows or denies it by name"):
            self.assertIn(expected, result["detail"])

    def test_the_home_is_deleted_only_when_it_is_an_ordinary_folder_of_that_user(self):
        for home, kind in (("/usr/lib/x", "system folder"), ("/data/homes/alice", "overlaps the home folder of alice"), ("/x", "too close")):
            self.user("bob")["home"] = home
            self.assertIn("not deleted automatically", self.refused("remove", 409, name="bob", confirm="bob", delete_home=True), kind)
        self.user("bob")["home"] = str(self.root / "homes" / "bob")
        self.user("bob")["uid"] = ME + 5000
        self.assertIn("not a folder owned by bob", self.refused("remove", 409, name="bob", confirm="bob", delete_home=True))
        self.user("bob")["uid"] = ME
        os.rmdir(self.root / "homes" / "bob")
        os.symlink(self.root, self.root / "homes" / "bob")
        self.assertIn("not a folder owned by bob", self.refused("remove", 409, name="bob", confirm="bob", delete_home=True))
        self.assertEqual(self.calls, [])

    def test_a_home_that_is_already_gone_is_fine(self):
        os.rmdir(self.root / "homes" / "bob")
        self.remove(delete_home=True)
        self.assertEqual(self.argv(), [["userdel", "-r", "--", "bob"]])

    def test_the_remembered_expiry_is_forgotten_and_a_failure_is_a_502(self):
        self.admin.ledger.put("bob", 5, "x")
        self.broken = [(["userdel"], "userdel: exit 8: user is logged in")]
        self.assertIn("userdel: exit 8", self.refused("remove", 502, name="bob", confirm="bob"))
        self.assertIsNotNone(self.admin.ledger.get("bob"))
        self.broken = []
        self.remove()
        self.assertIsNone(self.admin.ledger.get("bob"))


class HomeProblemTest(unittest.TestCase):
    OTHERS = [acct("alice", 1000, home="/home/alice"), acct("bob", 1001, home="/home/bob"), acct("daemon", 1, type="system", home="/nonexistent"),
              acct("www", 33, type="system", home="/var/www")]

    def check(self, path, own=None, protect=("/mnt/Extra20/admin",)):
        return home_problem(path, self.OTHERS, own=own, protect=protect)

    def test_ordinary_places_are_fine(self):
        for path in ("/home/carol", "/srv/people/carol", "/mnt/disk2/homes/carol", "/opt/carol", "/var/www/carol", "/data/x"):
            self.assertIsNone(self.check(path), path)

    def test_system_places_are_not(self):
        for path in ("/", "/home", "/etc", "/etc/x", "/usr/local/x", "/var/lib/x", "/root", "/root/x", "/tmp/x", "/bin/x", "/lib64/x", "/boot/x", "/dev/shm/x",
                     "/proc/1", "/run/user/1", "/sys/x", "/snap/x", "/var", "/var/log/x"):
            self.assertIsNotNone(self.check(path), path)

    def test_the_path_must_be_in_its_plain_form(self):
        for path in ("home/x", "./x", "/home/x/", "/home/../etc/x", "/home//x", "/home/./x", "", None, 7, "/home/x\ny", "/home/x\x00", "/" + "a/" * 150 + "b"):
            self.assertIsNotNone(self.check(path), repr(path))

    def test_it_cannot_overlap_another_accounts_home(self):
        self.assertIn("alice", self.check("/home/alice"))
        self.assertIn("alice", self.check("/home/alice/sub"))
        self.assertIn("bob", self.check("/home"[:0] + "/home/bob/deeper/still"))
        self.assertIsNone(self.check("/home/alice", own="alice"))                                 # its own is fine
        self.assertIsNone(self.check("/home/aliceX"))                                              # a prefix of the text is not an overlap

    def test_it_cannot_contain_another_accounts_home(self):
        self.OTHERS.append(acct("dan", 1004, home="/srv/people/dan"))
        try:
            self.assertIn("dan", self.check("/srv/people"))
        finally:
            self.OTHERS.pop()

    def test_a_system_accounts_home_may_be_shared_beside_but_never_exactly(self):
        self.assertIsNone(self.check("/var/www/carol"))                      # inside www-data's /var/www: allowed, as the plan says
        self.assertIn("www", self.check("/var/www"))                           # the same folder: not
        self.assertIsNone(self.check("/var/www/carol/sub"))

    def test_the_dashboards_own_folder_is_off_limits(self):
        for path in ("/mnt/Extra20/admin", "/mnt/Extra20/admin/data", "/mnt/Extra20/admin/data/x"):
            self.assertIn("dashboard", self.check(path), path)
        self.assertIsNone(self.check("/mnt/Extra20/other"))


class LedgerAndGuardTest(Case):
    def test_the_first_lock_wins_and_a_broken_file_is_ignored(self):
        self.admin.ledger.put("bob", 100, "a")
        self.admin.ledger.put("bob", 1, "b")
        self.assertEqual(self.admin.ledger.get("bob")["expires_day"], 100)
        (self.root / "data" / "locks.json").write_text("{not json")
        self.assertIsNone(self.admin.ledger.get("bob"))
        (self.root / "data" / "locks.json").write_text("[1, 2]")
        self.assertIsNone(self.admin.ledger.get("bob"))
        self.admin.ledger.put("carol", None, "a")
        self.assertIsNone(self.admin.ledger.get("carol")["expires_day"])
        self.admin.ledger.drop("nobody")

    def test_every_action_is_rate_limited_and_serialised_by_the_shared_lock(self):
        gate, started = threading.Event(), threading.Event()
        real = self.runner

        def slow(args, timeout=5.0, ok_codes=(0,), stdin=None, secret=None):
            started.set()
            gate.wait(5)
            return real(args, timeout, ok_codes, stdin, secret)

        self.admin._run = slow
        results = []
        thread = threading.Thread(target=lambda: results.append(self.do("lock", name="bob")))
        thread.start()
        self.assertTrue(started.wait(5))
        self.refused("lock", 409, name="carol")                                 # a second action while one is running
        gate.set()
        thread.join(5)
        self.assertEqual(len(results), 1)

    def test_nothing_the_audit_log_or_telegram_says_contains_a_password(self):
        self.do("password", name="bob", password=SECRET)
        self.refused("password", 400, name="bob", password="short")
        self.assertNotIn(SECRET, json.dumps(self.entries()) + str(self.alerts.events))
        for e in self.entries():
            self.assertEqual(e["target"], "bob")

    def test_every_op_is_registered_with_a_label_that_survives_hostile_input(self):
        from dashboard.useradmin import OPS
        hostile = {"name": ["x"], "new_name": 5, "path": None, "id": {"a": 1}}
        for op in OPS:
            label = self.actions._labels[f"users.{op}"](hostile)
            self.assertEqual(label["action"], f"users.{op}")
            self.assertIsInstance(label["target"], str)
        self.assertEqual(self.admin._target("rename", {"name": "a\nb", "new_name": "c" * 200}).count("\n"), 0)
        self.assertLessEqual(len(self.admin._target("home", {"name": "x" * 500, "path": "y" * 500})), 200)

if __name__ == "__main__":
    unittest.main()
