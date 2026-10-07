import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dashboard import collectors, config, safefs
from dashboard.collectors import users
from dashboard.util import CommandError

ME = os.getuid()
NOW = 1_700_000_000.0                       # day 19675
TODAY = int(NOW // 86400)

PASSWD = """\
# a comment
root:x:0:0:root:/root:/bin/bash
daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin
alice:x:1000:1000:Alice A,,,:/home/alice:/bin/bash
bob:x:1001:1001::/home/bob:/bin/bash
carol:x:1002:1002:Carol:/home/carol:/usr/sbin/nologin
dave:x:1003:1003::/home/dave:/bin/sh
erin:x:1004:1004::/nonexistent:
www-data:x:33:33:www-data:/var/www:/usr/sbin/nologin
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
+@nis::::::
broken line
x:x:notanumber:1:::/bin/sh
"""

GROUP = """\
root:x:0:
daemon:x:1:
sudo:x:27:alice
admin:x:1100:bob
devs:x:1200:carol,dave
alice:x:1000:
bob:x:1001:
carol:x:1002:
dave:x:1003:
erin:x:1004:
www-data:x:33:
"""

# The hashes are made-up, but they look like real ones: none of these strings may ever appear in the output.
SECRETS = ["ROOTSECRETHASH", "ALICESECRETHASH", "BOBSECRETHASH", "ERINSECRETHASH", "rootsalt", "alicesalt"]
SHADOW = f"""\
root:$6$rootsalt$ROOTSECRETHASH:19000:0:99999:7:::
daemon:*:19000:0:99999:7:::
alice:$y$alicesalt$ALICESECRETHASH:19000:0:99999:7:::
bob:!$6$bobsalt$BOBSECRETHASH:19000:0:99999:7:::
carol:*:19000:0:99999:7:::
dave:!:19000:0:99999:7::1:
erin::19000:0:99999:7:::
www-data:*:19000:0:99999:7:::
nobody:*:19000:0:99999:7:::
"""

SUDOERS = """\
Defaults env_reset
Defaults:alice !authenticate
User_Alias OPS = bob, %devs        # who operates things
Cmnd_Alias LS = /bin/ls
root ALL=(ALL:ALL) ALL
%sudo ALL=(ALL:ALL) ALL
OPS ALL=(root) /usr/bin/systemctl, \\
    /bin/ls
@includedir /etc/sudoers.d
"""


class ParseTest(unittest.TestCase):
    def test_passwd_skips_comments_nis_and_broken_lines(self):
        accounts = users.parse_passwd(PASSWD)
        self.assertEqual([a["name"] for a in accounts], ["root", "daemon", "alice", "bob", "carol", "dave", "erin", "www-data", "nobody"])
        alice = accounts[2]
        self.assertEqual((alice["uid"], alice["gid"], alice["comment"], alice["home"], alice["shell"]), (1000, 1000, "Alice A", "/home/alice", "/bin/bash"))
        self.assertEqual(accounts[6]["shell"], "/bin/sh")                          # an empty shell field means /bin/sh

    def test_group_parsing(self):
        groups = users.parse_group(GROUP + "+nis:::\nbroken\n#x:x:1:\nbad:x:nan:\n")
        self.assertEqual(groups["devs"], {"gid": 1200, "members": ["carol", "dave"]})
        self.assertEqual(groups["root"]["members"], [])
        self.assertNotIn("bad", groups)

    def test_password_states(self):
        cases = {"": "empty", "*": "none", "!": "none", "!!": "none", "*LK*": "none", "x": "none",
                 "$6$salt$abcdef": "set", "$y$j9T$salt$hash": "set", "abcdefghijklm": "set", "short": "none",
                 "!$6$salt$abcdef": "locked", "!!$6$salt$abcdef": "locked", "!abcdefghijklm": "locked"}
        for field, expected in cases.items():
            self.assertEqual(users.password_state(field), expected, repr(field))

    def test_shadow_keeps_only_derived_facts(self):
        parsed = users.parse_shadow(SHADOW, TODAY)
        self.assertEqual(parsed["alice"]["password"], "set")
        self.assertEqual(parsed["bob"]["password"], "locked")
        self.assertEqual(parsed["carol"]["password"], "none")
        self.assertEqual(parsed["erin"]["password"], "empty")
        self.assertEqual(parsed["dave"]["expired"], True)                          # expiry day 1 is long past
        self.assertIsNone(parsed["alice"]["password_expires"])                     # 99999 means never
        dump = json.dumps(parsed)
        for secret in SECRETS:
            self.assertNotIn(secret, dump)

    def test_shadow_dates(self):
        line = f"u:$6$s$h:{TODAY - 100}:0:90:7:::\nv:$6$s$h:0:0:99999:7::{TODAY + 5}:\nw:$6$s$h:{TODAY - 3}:0:99999:7::{TODAY}:\nbad\n"
        p = users.parse_shadow(line, TODAY)
        self.assertEqual((p["u"]["password_expired"], p["u"]["password_expires"]), (True, (TODAY - 10) * 86400))
        self.assertEqual((p["v"]["must_change"], p["v"]["password_changed"], p["v"]["expired"], p["v"]["expires"]), (True, None, False, (TODAY + 5) * 86400))
        self.assertTrue(p["w"]["expired"])                                         # the expiry day itself counts
        self.assertNotIn("bad", p)


class SudoersTest(unittest.TestCase):
    def check(self, text, name="alice", uid=1000, groups=(), gids=()):
        s = users.Sudoers()
        s.add_text(text, "sudoers")
        return s.for_user(name, uid, set(groups), set(gids))

    def test_a_user_a_group_a_uid_and_all(self):
        self.assertEqual(len(self.check("alice ALL=(ALL) ALL")), 1)
        self.assertEqual(self.check("bob ALL=(ALL) ALL"), [])
        self.assertEqual(len(self.check("%sudo ALL=(ALL:ALL) ALL", groups=["sudo"])), 1)
        self.assertEqual(self.check("%sudo ALL=(ALL:ALL) ALL", groups=["other"]), [])
        self.assertEqual(len(self.check("#1000 ALL=(ALL) ALL")), 1)
        self.assertEqual(len(self.check("%#1200 ALL=(ALL) ALL", gids=[1200])), 1)
        self.assertEqual(len(self.check("ALL ALL=(ALL) ALL")), 1)
        self.assertEqual(self.check("+netgroup ALL=(ALL) ALL"), [])

    def test_full_or_limited_and_nopasswd(self):
        full, = self.check("alice ALL=(ALL) NOPASSWD: ALL")
        self.assertEqual((full["full"], full["nopasswd"]), (True, True))
        limited, = self.check("alice ALL=(root) /usr/bin/systemctl, /bin/ls")
        self.assertEqual((limited["full"], limited["nopasswd"]), (False, False))
        tagged, = self.check("alice ALL=(ALL) NOPASSWD: SETENV: ALL")
        self.assertEqual((tagged["full"], tagged["nopasswd"]), (True, True))
        listed, = self.check("alice ALL=(ALL) /bin/ls, ALL")
        self.assertTrue(listed["full"])

    def test_lists_negation_and_last_match_wins(self):
        self.assertEqual(len(self.check("bob, alice ALL=(ALL) ALL")), 1)
        self.assertEqual(self.check("ALL, !alice ALL=(ALL) ALL"), [])
        self.assertEqual(len(self.check("!alice, ALL ALL=(ALL) ALL")), 1)

    def test_aliases_including_nested_and_cyclic_ones(self):
        text = "User_Alias INNER = alice\nUser_Alias OUTER = INNER, bob\nOUTER ALL=(ALL) ALL"
        self.assertEqual(len(self.check(text)), 1)
        self.assertEqual(len(self.check("User_Alias G = %devs\nG ALL=(ALL) ALL", groups=["devs"])), 1)
        self.assertEqual(self.check("User_Alias A = B\nUser_Alias B = A\nA ALL=(ALL) ALL"), [])      # terminates
        self.assertEqual(len(self.check("User_Alias A = alice : B = bob\nB ALL=(ALL) ALL", name="bob")), 1)

    def test_things_that_are_not_user_specs_are_ignored(self):
        text = "Defaults env_reset\nDefaults:alice !authenticate\nCmnd_Alias X = /bin/ls\nHost_Alias H = a\nRunas_Alias R = b\n# alice ALL=(ALL) ALL\n\n"
        self.assertEqual(self.check(text), [])
        self.assertEqual(self.check("alice"), [])                                  # no host list, no commands

    def test_comments_continuations_and_includes(self):
        s = users.Sudoers()
        s.add_text("alice ALL=(ALL) \\\n  ALL   # trailing comment\n@includedir /etc/sudoers.d\n#include /etc/other\n", "sudoers")
        self.assertEqual(s.includes, [("/etc/sudoers.d", True), ("/etc/other", False)])
        self.assertEqual(len(s.for_user("alice", 1000, set(), set())), 1)

    def test_load_reads_the_directory_and_skips_files_sudo_skips(self):
        with tempfile.TemporaryDirectory() as d:
            main, extra = Path(d, "sudoers"), Path(d, "sudoers.d")
            extra.mkdir()
            main.write_text("root ALL=(ALL) ALL\n@includedir " + str(extra) + "\n")
            (extra / "alice").write_text("alice ALL=(ALL) NOPASSWD: ALL\n")
            (extra / "README").write_text("# nothing\n")
            (extra / "old.bak").write_text("mallory ALL=(ALL) ALL\n")            # a dot in the name: sudo ignores it
            (extra / "tilde~").write_text("mallory ALL=(ALL) ALL\n")
            s = users.load_sudoers(str(main), str(extra))
            self.assertEqual([r["users"] for r in s.rules], [["root"], ["alice"]])
            self.assertEqual(s.rules[1]["label"], "sudoers.d/alice")
            self.assertEqual(s.for_user("mallory", 9, set(), set()), [])
            with self.assertRaises(OSError):
                users.load_sudoers(str(Path(d, "missing")), str(extra))

    def test_an_include_outside_the_default_directory_is_followed_once(self):
        with tempfile.TemporaryDirectory() as d:
            other = Path(d, "other.d")
            other.mkdir()
            (other / "frank").write_text("frank ALL=(ALL) ALL\n")
            main = Path(d, "sudoers")
            main.write_text(f"@includedir {other}\n@includedir {other}\n")
            s = users.load_sudoers(str(main), str(Path(d, "none.d")))
            self.assertEqual([r["users"] for r in s.rules], [["frank"]])


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_lastlog_records_are_indexed_by_uid(self):
        record = lambda t, line, host: struct.pack("<i32s256s", t, line, host)       # noqa: E731
        blank = record(0, b"", b"")
        data = blank * 2 + record(1_699_999_000, b"pts/0", b"10.1.1.1") + blank + record(5, b"tty1", b"")
        path = self.root / "lastlog"
        path.write_bytes(data)
        self.assertIsNone(users.read_lastlog(str(path), 0))                          # never
        self.assertEqual(users.read_lastlog(str(path), 2), {"time": 1_699_999_000, "from": "10.1.1.1", "tty": "pts/0"})
        self.assertIsNone(users.read_lastlog(str(path), 3))
        self.assertEqual(users.read_lastlog(str(path), 4)["from"], "")
        self.assertIsNone(users.read_lastlog(str(path), 99))                         # past the end of the file
        with self.assertRaises(OSError):
            users.read_lastlog(str(self.root / "missing"), 0)

    def test_lastlog_survives_garbage_text(self):
        path = self.root / "lastlog"
        path.write_bytes(struct.pack("<i32s256s", 7, b"\xff\xfe" + b"x" * 30, b"\xc3(" + b"y" * 200))
        rec = users.read_lastlog(str(path), 0)
        self.assertEqual(rec["time"], 7)
        self.assertIsInstance(rec["from"], str)

    def test_authorized_key_count(self):
        home = self.root / "home"
        (home / ".ssh").mkdir(parents=True, mode=0o700)
        os.chmod(home, 0o750)
        keys = home / ".ssh" / "authorized_keys"
        keys.write_text("# a comment\n\nssh-ed25519 AAAA one\n   \ncommand=\"x\" ssh-rsa BBBB two\n  # indented comment\n")
        os.chmod(keys, 0o600)
        self.assertEqual(users.authorized_key_count(str(home), ME), 2)
        keys.unlink()
        self.assertEqual(users.authorized_key_count(str(home), ME), 0)
        (home / ".ssh").rename(home / ".ssh-real")
        os.symlink(self.root, home / ".ssh")
        with self.assertRaises(safefs.UnsafePath):
            users.authorized_key_count(str(home), ME)

    def test_process_counts_by_owner(self):
        proc = self.root / "proc"
        for name in ("1", "20", "300", "self", "net", "cpuinfo"):
            (proc / name).mkdir(parents=True)
        self.assertEqual(users.count_processes(str(proc)), {ME: 3})
        self.assertEqual(users.count_processes(str(self.root / "missing")), {})


LIST = "1039 0 root - 52901 user - no -\n4 1001 claude - 1900 user - no -\n5 1001 claude - 1908 manager - no -\n"
SHOW = (
    "Id=1039\nName=root\nUser=0\nService=sshd\nTTY=\nType=tty\nClass=user\nState=active\nLeader=52901\nRemote=yes\nRemoteHost=10.9.9.9\n"
    "IdleHint=yes\nIdleSinceHint=1699999000000000\nTimestampMonotonic=100000000\nScope=session-1039.scope\n\n"
    "Id=4\nName=claude\nUser=1001\nService=login\nTTY=tty1\nType=tty\nClass=user\nState=closing\nLeader=1900\nRemote=no\nRemoteHost=\n"
    "IdleHint=no\nIdleSinceHint=0\nTimestampMonotonic=50000000\nScope=session-4.scope\n\n"
    "Id=5\nName=claude\nUser=1001\nService=\nTTY=\nType=unspecified\nClass=manager\nState=active\nLeader=1908\nRemote=no\nRemoteHost=\n"
    "IdleHint=no\nIdleSinceHint=0\nTimestampMonotonic=1\nScope=\n"
)


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        scope = Path(self.tmp.name, "user.slice", "user-0.slice", "session-1039.scope")
        scope.mkdir(parents=True)
        (scope / "cgroup.procs").write_text("52901\n52950\n52951\n")
        self.calls = []

    def runner(self, args, timeout=5.0, ok_codes=(0,)):
        self.calls.append(args)
        return LIST if args[1] == "list-sessions" else SHOW

    def read(self):
        return users.read_sessions(self.runner, self.tmp.name, clock=lambda: 1_700_000_000.0, monotonic=lambda: 1_000.0)

    def test_only_people_are_listed_newest_first_with_idle_and_process_counts(self):
        sessions = self.read()
        self.assertEqual([s["id"] for s in sessions], ["1039", "4"])                 # the "manager" session is not a person
        root = sessions[0]
        self.assertEqual((root["user"], root["uid"], root["service"], root["from"], root["state"]), ("root", 0, "sshd", "10.9.9.9", "active"))
        self.assertEqual((root["idle"], root["idle_since"], root["processes"], root["leader"]), (True, 1_699_999_000, 3, 52901))
        self.assertEqual(root["since"], round(1_700_000_000 - 1_000 + 100))
        local = sessions[1]
        self.assertEqual((local["from"], local["tty"], local["idle"], local["idle_since"], local["processes"]), ("local", "tty1", False, None, None))

    def test_one_loginctl_call_for_all_sessions(self):
        self.read()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][:2], ["loginctl", "show-session"])
        self.assertTrue(set(self.calls[1][-3:]) == {"1039", "4", "5"})

    def test_no_sessions(self):
        self.assertEqual(users.read_sessions(lambda *a, **k: "", self.tmp.name), [])

    def test_a_scope_name_cannot_point_outside_the_cgroup_folder(self):
        decoy = Path(self.tmp.name, "user.slice", "decoy.scope")                     # reachable as "../decoy.scope" from the user's slice
        decoy.mkdir()
        (decoy / "cgroup.procs").write_text("1\n2\n3\n")
        self.assertEqual(users._scope_processes(self.tmp.name, 0, "session-1039.scope"), 3)       # the real one works
        for scope in ("../decoy.scope", "../../etc/passwd", "a/b.scope", "", "x", "session-1039.scope/../../decoy.scope"):
            self.assertIsNone(users._scope_processes(self.tmp.name, 0, scope), scope)


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = Path(self.tmp.name)
        self.files = {"passwd": r / "passwd", "group": r / "group", "shadow": r / "shadow", "sudoers": r / "sudoers", "sudoers_dir": r / "sudoers.d",
                      "lastlog": r / "lastlog", "login_defs": r / "login.defs", "proc": r / "proc", "cgroup": r / "cgroup"}
        self.files["passwd"].write_text(PASSWD)
        self.files["group"].write_text(GROUP)
        self.files["shadow"].write_text(SHADOW)
        self.files["sudoers"].write_text(SUDOERS)
        self.files["sudoers_dir"].mkdir()
        (self.files["sudoers_dir"] / "dave-nopasswd").write_text("dave ALL=(ALL) NOPASSWD: ALL\n")
        (self.files["sudoers_dir"] / "negate").write_text("%devs, !dave ALL=(ALL) ALL\n")
        (self.files["sudoers_dir"] / "ignored.bak").write_text("erin ALL=(ALL) ALL\n")
        rec = lambda t, line, host: struct.pack("<i32s256s", t, line, host)           # noqa: E731
        blank = rec(0, b"", b"")
        self.files["lastlog"].write_bytes(blank * 1000 + rec(1_699_999_000, b"pts/0", b"10.1.1.1") + blank)
        self.files["login_defs"].write_text("# defs\nUID_MIN 1000\nUID_MAX 60000\nOTHER x\n")
        self.files["proc"].mkdir()
        (self.files["proc"] / "1").mkdir()
        self.files["cgroup"].mkdir()
        self.sessions = LIST, SHOW

    def collector(self, **overrides):
        paths = {k: str(v) for k, v in self.files.items()}
        paths.update(overrides)
        return users.Users(runner=lambda args, timeout=5.0, ok_codes=(0,): self.sessions[0 if args[1] == "list-sessions" else 1],
                           clock=lambda: NOW, **paths)

    def collect(self, **overrides):
        keys = {0: 1, 1000: 2, 1001: 0, 1004: 0}                      # by uid; dave (1003) has a booby-trapped home

        def counter(home, uid):
            if uid == 1003:
                raise safefs.UnsafePath("'.ssh' is a symbolic link or not a folder")
            return keys[uid]

        with mock.patch.object(users, "authorized_key_count", side_effect=counter):
            return self.collector(**overrides).collect({})

    def by_name(self, out):
        return {u["name"]: u for u in out["users"]}

    def test_the_nine_accounts_in_a_sensible_order_with_the_right_types(self):
        out = self.collect()
        self.assertEqual([(u["name"], u["type"]) for u in out["users"]], [
            ("root", "root"), ("alice", "login"), ("bob", "login"), ("carol", "login"), ("dave", "login"), ("erin", "login"),
            ("daemon", "system"), ("www-data", "system"), ("nobody", "system")])
        self.assertEqual(out["uid_range"], [1000, 60000])
        self.assertEqual(out["notes"], {})
        self.assertEqual(out["sudo_source"], "sudoers")

    def test_groups(self):
        u = self.by_name(self.collect())
        self.assertEqual(u["alice"]["groups"], ["alice", "sudo"])
        self.assertEqual((u["alice"]["primary_group"], u["bob"]["groups"]), ("alice", ["admin", "bob"]))
        self.assertEqual(u["dave"]["groups"], ["dave", "devs"])

    def test_sudo_rights_from_groups_aliases_and_files(self):
        u = self.by_name(self.collect())
        self.assertEqual((u["root"]["sudo"], u["root"]["nopasswd"]), ("full", False))
        self.assertEqual((u["alice"]["sudo"], u["alice"]["sudo_via"]), ("full", ["sudoers"]))
        self.assertEqual((u["bob"]["sudo"], u["bob"]["nopasswd"]), ("limited", False))                  # through the OPS alias, only some commands
        self.assertEqual(u["carol"]["sudo"], "full")                                                    # %devs in sudoers.d/negate
        self.assertEqual(u["carol"]["sudo_via"], ["sudoers", "sudoers.d/negate"])
        self.assertEqual((u["dave"]["sudo"], u["dave"]["nopasswd"]), ("full", True))
        self.assertEqual(u["dave"]["sudo_via"], ["sudoers", "sudoers.d/dave-nopasswd"])
        self.assertIsNone(u["erin"]["sudo"])                                                            # her rule is in a file sudo ignores
        self.assertTrue(all(x["sudo"] is None for x in u.values() if x["type"] == "system"))

    def test_passwords_expiry_and_login_ability(self):
        u = self.by_name(self.collect())
        self.assertEqual([u[n]["password"] for n in ("alice", "bob", "carol", "dave", "erin")], ["set", "locked", "none", "none", "empty"])
        self.assertTrue(u["alice"]["can_login"])
        self.assertTrue(u["erin"]["can_login"])                                                         # an empty password is a hole, not a blocker
        self.assertEqual(u["carol"]["blockers"], ["its shell does not allow logins"])
        self.assertEqual(u["dave"]["blockers"], ["the account has expired"])
        self.assertFalse(u["dave"]["can_login"])

    def test_a_locked_account_without_keys_cannot_log_in(self):
        u = self.by_name(self.collect())
        self.assertEqual(u["bob"]["keys"], 0)
        self.assertEqual(u["bob"]["blockers"], ["no usable password and no SSH key"])
        self.assertFalse(u["bob"]["can_login"])

    def test_keys_only_for_people_with_a_real_shell_and_unsafe_homes_are_reported(self):
        u = self.by_name(self.collect())
        self.assertEqual((u["alice"]["keys"], u["alice"]["keys_error"]), (2, None))
        self.assertEqual((u["dave"]["keys"], u["dave"]["keys_error"]), (None, "'.ssh' is a symbolic link or not a folder"))
        self.assertIsNone(u["carol"]["keys"])                                                           # nologin shell: not looked at
        self.assertIsNone(u["daemon"]["keys"])                                                          # system account: not looked at

    def test_last_login_and_home(self):
        u = self.by_name(self.collect())
        self.assertEqual(u["alice"]["last_login"], {"time": 1_699_999_000, "from": "10.1.1.1", "tty": "pts/0"})
        self.assertIsNone(u["bob"]["last_login"])
        self.assertEqual((u["alice"]["home_exists"], u["alice"]["home_owner"], u["alice"]["home_mode"]), (False, None, None))   # /home/alice does not exist here
        self.assertTrue(u["root"]["home_exists"] or not Path("/root").exists())
        self.assertEqual(u["nobody"]["home_exists"], False)                                              # /nonexistent

    def test_no_password_hash_ever_leaves_the_parser(self):
        dump = json.dumps(self.collect())
        for secret in SECRETS:
            self.assertNotIn(secret, dump)
        self.assertNotIn("$6$", dump)
        self.assertNotIn("$y$", dump)

    def test_without_root_the_answer_is_partial_but_still_useful(self):
        self.files["shadow"].unlink()
        self.files["sudoers"].unlink()
        out = self.collect()
        self.assertIn("shadow", out["notes"])
        self.assertIn("sudo", out["notes"])
        self.assertEqual(out["sudo_source"], "groups")
        u = self.by_name(out)
        self.assertEqual(u["alice"]["password"], "unknown")
        self.assertEqual((u["alice"]["sudo"], u["alice"]["sudo_via"]), ("full", ["group sudo"]))        # guessed from the group
        self.assertEqual((u["bob"]["sudo"], u["bob"]["sudo_via"]), ("full", ["group admin"]))           # the "admin" group counts too
        self.assertIsNone(u["carol"]["sudo"])
        self.assertTrue(u["bob"]["can_login"])                                                          # without the shadow file nothing says otherwise

    def test_a_missing_lastlog_falls_back_to_the_login_history(self):
        self.files["lastlog"].unlink()
        history = [{"type": "login", "user": "alice", "from": "9.9.9.9", "tty": "pts/3", "time": 1_699_000_000, "end": None},
                   {"type": "reboot", "user": "", "from": "", "tty": "", "time": 1_698_000_000, "end": None},
                   {"type": "login", "user": "alice", "from": "8.8.8.8", "tty": "pts/1", "time": 1_690_000_000, "end": None}]
        with mock.patch.object(users, "read_wtmp", return_value=history):
            out = self.collect()
        self.assertIn("lastlog", out["notes"])
        self.assertEqual(self.by_name(out)["alice"]["last_login"], {"time": 1_699_000_000, "from": "9.9.9.9", "tty": "pts/3"})   # the newest one

    def test_login_defs_sets_the_range_that_makes_a_login_user(self):
        self.files["login_defs"].write_text("UID_MIN 1002\nUID_MAX 1003\n")
        u = self.by_name(self.collect())
        self.assertEqual([u[n]["type"] for n in ("alice", "bob", "carol", "dave", "erin")], ["system", "system", "login", "login", "system"])
        self.files["login_defs"].unlink()
        self.assertEqual(self.collect()["uid_range"], [1000, 60000])

    def test_sessions_come_along_and_a_logind_failure_is_a_note(self):
        out = self.collect()
        self.assertEqual([s["id"] for s in out["sessions"]], ["1039", "4"])
        out2 = users.Users(runner=mock.Mock(side_effect=CommandError("loginctl: not installed")), clock=lambda: NOW,
                           **{k: str(v) for k, v in self.files.items()}).collect({})
        self.assertEqual(out2["sessions"], [])
        self.assertEqual(out2["notes"]["sessions"], "loginctl: not installed")

    def test_the_whole_answer_is_plain_json(self):
        json.dumps(self.collect())


class RegistryTest(unittest.TestCase):
    def test_the_collector_exists_only_when_the_tool_is_switched_on(self):
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        self.assertIn("users", collectors.create(cfg))
        self.assertIn("users", collectors.create())                                  # no config (tests, development): everything
        cfg["admin"]["users"] = False
        self.assertNotIn("users", collectors.create(cfg))
        del cfg["admin"]
        self.assertNotIn("users", collectors.create(cfg))

    def test_the_real_collector_runs_here_without_raising(self):
        cfg = json.loads(config.EXAMPLE_PATH.read_text())
        out = collectors.run(collectors.create(cfg)["users"], cfg)
        self.assertNotIn("error", out)
        self.assertTrue(any(u["name"] == "root" for u in out["users"]))
        json.dumps(out)


if __name__ == "__main__":
    unittest.main()
