"""Account management: add, remove, lock, ban, change password, rename, move the home folder, end sessions.

This is the part of the dashboard that can lock people out of the server, so it is built around refusing:

* Only login users (the uid range from login.defs) can be locked, banned, renamed, moved or removed. root and
  system accounts are never touched, except that root's password may be changed.
* The last user with full sudo rights who can still log in can never be locked, banned or removed.
* Renaming or removing an account is refused while anything still depends on its name: running processes, a systemd
  service that runs as it, a supervisor program, a sudoers rule, its crontab, an sshd allow-list. The refusal says
  where, so it can be fixed first.
* Home folders must be ordinary places: no system folders, nothing that overlaps another account's home or this
  dashboard's folder, no symbolic links on the way.
* Every check reads the accounts fresh from the system, not a minute-old copy, and commands are argv lists with `--`
  before the name. A password only ever travels on stdin to chpasswd.
"""

import glob
import json
import logging
import os
import posixpath
import re
import shlex
import stat
import threading
import time
from pathlib import Path

from . import safefs
from .actions import ActionError, clean, require_confirmation
from .auth import MAX_PASSWORD_LEN, MIN_PASSWORD_LEN
from .collectors.users import load_sudoers, parse_group
from .config import ROOT
from .util import CommandError, run

log = logging.getLogger("dashboard.useradmin")

NEW_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
EXISTING_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.@-]{0,63}")
SESSION_ID = re.compile(r"[A-Za-z0-9]{1,16}")
DASHBOARD_ID = re.compile(r"[0-9a-f]{12}")
COMMAND_TIMEOUT_S = 60
MAX_COMMENT = 100
FORBIDDEN_HOME_ROOTS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc", "/root", "/run",
                        "/sbin", "/sys", "/usr", "/var", "/tmp", "/snap", "/lost+found")
HOME_EXCEPTIONS = ("/var/www",)
LOCKED_EXPIRY_DAY = 1                      # `usermod -e 1`: the day after the epoch, long past, so the account is expired
REAL_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "jfs", "reiserfs"}
MAX_BROWSE = 500
OPS = ("add", "password", "lock", "unlock", "ban", "unban", "rename", "home", "remove", "end_session", "end_dashboard")


def home_problem(path, accounts: list[dict], own: str | None = None, protect: tuple = (), forbidden: tuple = FORBIDDEN_HOME_ROOTS) -> str | None:
    """Why `path` may not be used as a home folder, or None. Pure: nothing on disk is looked at."""
    if not isinstance(path, str) or not path or len(path) > 200 or any(ord(c) < 32 or c == "\x7f" for c in path):
        return "that is not a valid folder path"
    if not path.startswith("/"):
        return "the home folder must be an absolute path (start with /)"
    if path != posixpath.normpath(path) or "//" in path:
        return "write the path in its plain form: no '..', '.', double or trailing slashes"
    if len(path.strip("/").split("/")) < 2:
        return "that folder is too close to the top of the disk: use something like /home/name"
    for top in forbidden:
        if (path == top or path.startswith(top + "/")) and not any(path == e or path.startswith(e + "/") for e in HOME_EXCEPTIONS):
            return f"{top} is a system folder"
    if path == "/home":
        return "/home itself cannot be a home folder"
    for folder in protect:
        if path == folder or path.startswith(folder + "/") or folder.startswith(path + "/"):
            return "that overlaps this dashboard's own folder"
    for other in accounts:
        home = other["home"]
        if other["name"] == own or len(home.strip("/").split("/")) < 2:
            continue
        # Removing a login user can delete their home, so no login user's home may contain or sit inside another's. System
        # accounts cannot be removed from the dashboard: living beside one (/var/www/carol next to www-data's /var/www) is
        # fine, sharing its folder exactly is not.
        nested = path.startswith(home + "/") or home.startswith(path + "/")
        if path == home or (nested and other["type"] != "system"):
            return f"that overlaps the home folder of {other['name']} ({home})"
    return None


def parse_mountinfo(text: str) -> list[dict]:
    """Mounts from /proc/self/mountinfo: [{"root", "target", "fstype", "source"}]. `root` is the folder of the filesystem that is mounted,
    so a real disk has root "/" and a bind mount of a sub-folder (what the service sandbox makes of /home) has something else."""
    unescape = lambda t: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), t)       # noqa: E731 - spaces appear as \040
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        a, b = left.split(), right.split()
        if sep and len(a) >= 5 and len(b) >= 2:
            mounts.append({"root": unescape(a[3]), "target": unescape(a[4]), "fstype": b[0], "source": b[1]})
    return mounts


def folder_name_problem(name) -> str | None:
    if not isinstance(name, str) or not name or len(name) > 64:
        return "a folder name is 1 to 64 characters"
    if name in (".", "..") or "/" in name or any(ord(c) < 32 or c == "\x7f" for c in name) or name != name.strip():
        return "a folder name cannot contain '/' or control characters, or be '.' or '..'"
    return None


def password_problem(password) -> str | None:
    if not isinstance(password, str):
        return "a password is required"
    if len(password) < MIN_PASSWORD_LEN:
        return f"the password must be at least {MIN_PASSWORD_LEN} characters"
    if len(password) > MAX_PASSWORD_LEN:
        return "that password is too long"
    if any(ord(c) < 32 or c == "\x7f" for c in password):
        return "the password cannot contain control characters or line breaks"
    return None


class LockLedger:
    """What an account's expiry date was before a lock replaced it, so Unlock can put it back.

    `usermod -e 1` is how a lock stops SSH keys as well as passwords, but it overwrites a real expiry date."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        safefs.write_private(self.path, json.dumps(data).encode())

    def get(self, name: str) -> dict | None:
        with self._lock:
            entry = self._load().get(name)
        return entry if isinstance(entry, dict) else None

    def put(self, name: str, expires_day: int | None, by: str, kept_password_lock: bool = False) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(name, {"expires_day": expires_day, "by": by, "at": time.time(),         # the first lock's value is the true one
                                   "kept_password_lock": kept_password_lock})
            self._save(data)

    def drop(self, name: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(name, None) is not None:
                self._save(data)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            data = self._load()
            if old in data:
                data[new] = data.pop(old)
                self._save(data)


class Dependents:
    """Finds what would break if an account were renamed or removed. Returns (blockers, notes): the first is refused on,
    the second is only worth knowing. A file that exists but cannot be read is a blocker: "cannot check" is not "fine"."""

    def __init__(self, cfg: dict, runner, paths: dict):
        self.cfg, self._run, self.paths = cfg, runner, paths

    def find(self, name: str, uid: int, group_too: bool, sudoers=None) -> tuple[list[str], list[str]]:
        blockers, notes = [], []
        ids = {name, str(uid)}
        groups = {name} if group_too else set()
        blockers += self._services(ids, groups)
        blockers += self._supervisor(ids)
        for mention in self._sudoers(name, sudoers, blockers):
            notes.append(f"sudoers: {mention} names it")
        if os.path.exists(os.path.join(self.paths["crontabs"], name)):
            notes.append(f"it has a crontab ({os.path.join(self.paths['crontabs'], name)})")
        for place in self._sshd(name, blockers):
            notes.append(f"sshd allows or denies it by name ({place})")
        return blockers, notes

    # -- systemd -------------------------------------------------------------------------------
    def _services(self, ids: set, groups: set) -> list[str]:
        found: dict[str, str] = {}
        folder = self.paths["systemd_dir"]
        files = sorted(glob.glob(os.path.join(folder, "*.service")) + glob.glob(os.path.join(folder, "*.service.d", "*.conf")))
        errors = []
        for path in files:
            unit = os.path.basename(path) if path.endswith(".service") else os.path.basename(os.path.dirname(path)).removesuffix(".d")
            try:
                text = Path(path).read_text(errors="replace")
            except OSError as e:
                if os.path.exists(path):                           # a dangling symlink is nothing to read, anything else is a problem
                    errors.append(f"cannot read {path} ({e.strerror}), so it cannot be checked")
                continue
            for line in text.splitlines():
                m = re.match(r"^\s*(User|Group)\s*=\s*(\S+)\s*$", line)
                if m and ((m.group(1) == "User" and m.group(2) in ids) or (m.group(1) == "Group" and m.group(2) in groups)):
                    found[unit] = f"the service {unit.removesuffix('.service')} runs as it"
        watched = [f"{str(u).removesuffix('.service')}.service" for u in self.cfg["watch"]["systemd"]]
        if watched:
            try:
                out = self._run(["systemctl", "show", "-p", "Id", "-p", "User", "-p", "Group", "--"] + watched, timeout=15)
                for block in out.strip().split("\n\n"):
                    p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                    if p.get("User") in ids or (p.get("Group") and p["Group"] in groups):
                        found.setdefault(p.get("Id", "?"), f"the service {p.get('Id', '?').removesuffix('.service')} runs as it")
            except CommandError as e:
                errors.append(f"systemd could not be asked about its services ({e})")
        return sorted(found.values()) + errors

    # -- supervisor ----------------------------------------------------------------------------
    def _supervisor(self, ids: set) -> list[str]:
        folder = self.paths["supervisor_dir"]
        files = [os.path.join(folder, "supervisord.conf")] + sorted(glob.glob(os.path.join(folder, "conf.d", "*.conf")) + glob.glob(os.path.join(folder, "conf.d", "*.ini")))
        out = []
        for path in files:
            try:
                text = Path(path).read_text(errors="replace")
            except FileNotFoundError:
                continue
            except OSError as e:
                out.append(f"cannot read {path} ({e.strerror}), so supervisor cannot be checked")
                continue
            section = None
            for line in text.splitlines():
                head = re.match(r"^\s*\[([^\]]+)\]", line)
                if head:
                    section = head.group(1)
                    continue
                m = re.match(r"^\s*user\s*=\s*(\S+)", line)
                if m and m.group(1) in ids:
                    out.append(f"the supervisor entry [{section or '?'}] in {os.path.basename(path)} runs as it")
        return out

    # -- sudoers -------------------------------------------------------------------------------
    def _sudoers(self, name: str, sudoers, blockers: list) -> list[str]:
        if sudoers is None:
            try:
                sudoers = load_sudoers(self.paths["sudoers"], self.paths["sudoers_dir"])
            except FileNotFoundError:
                return []
            except OSError as e:
                blockers.append(f"sudoers cannot be read ({e.strerror}), so it cannot be checked")
                return []
        return sudoers.mentions(name)

    # -- sshd ----------------------------------------------------------------------------------
    def _sshd(self, name: str, blockers: list) -> list[str]:
        files = [self.paths["sshd_config"]] + sorted(glob.glob(os.path.join(self.paths["sshd_dir"], "*.conf")))
        places = []
        for path in files:
            try:
                text = Path(path).read_text(errors="replace")
            except FileNotFoundError:
                continue
            except OSError as e:
                blockers.append(f"cannot read {path} ({e.strerror}), so sshd settings cannot be checked")
                continue
            for line in text.splitlines():
                try:
                    tokens = shlex.split(line, comments=True)
                except ValueError:
                    continue
                if not tokens:
                    continue
                key = tokens[0].lower()
                values = []
                if key in ("allowusers", "denyusers"):
                    values = tokens[1:]
                elif key == "match" and len(tokens) > 2 and tokens[1].lower() == "user":
                    values = tokens[2].split(",")
                if any(v.lstrip("!").split("@")[0] == name for v in values):
                    places.append(f"{os.path.basename(path)}: {tokens[0]}")
        return places


class UserAdmin:
    def __init__(self, cfg: dict, actions, scheduler, alerts, audit, sessions, data_dir, *, runner=run, paths: dict | None = None,
                 clock=time.time, protect=None, forbidden=FORBIDDEN_HOME_ROOTS, access=os.access, mountinfo="/proc/self/mountinfo",
                 trusted_uids=(0,)):
        self.cfg, self.actions, self.scheduler, self.alerts, self.audit, self.sessions = cfg, actions, scheduler, alerts, audit, sessions
        self._run, self._clock = runner, clock
        self.paths = {"group": "/etc/group", "sudoers": "/etc/sudoers", "sudoers_dir": "/etc/sudoers.d", "systemd_dir": "/etc/systemd/system",
                      "supervisor_dir": "/etc/supervisor", "crontabs": "/var/spool/cron/crontabs", "sshd_config": "/etc/ssh/sshd_config",
                      "sshd_dir": "/etc/ssh/sshd_config.d", **(paths or {})}
        self.protect = tuple(protect) if protect is not None else (str(ROOT),)
        self.forbidden = tuple(forbidden)
        self._access, self.mountinfo = access, mountinfo
        self.trusted_uids = frozenset(trusted_uids)          # who may own the folders above a home: nobody else can be allowed to swap it
        self.ledger = LockLedger(Path(data_dir) / "locks.json")
        self.dependents = Dependents(cfg, runner, self.paths)

    def register(self) -> None:
        handlers = {"add": self._add, "password": self._password, "lock": self._lock, "unlock": self._unlock, "ban": self._ban,
                    "unban": self._unban, "rename": self._rename, "home": self._home, "remove": self._remove,
                    "end_session": self._end_session, "end_dashboard": self._end_dashboard}
        for op, handler in handlers.items():
            self.actions.register(f"users.{op}", handler, lambda body, op=op: {"action": f"users.{op}", "target": self._target(op, body)})

    @staticmethod
    def _target(op: str, body: dict) -> str:
        """What the audit log and Telegram say the action was about: names and paths, never passwords or keys."""
        text = lambda key: clean(body.get(key, "") if isinstance(body.get(key, ""), str) else "?", 80)   # noqa: E731
        if op == "rename":
            return f"{text('name')} -> {text('new_name')}"
        if op == "home":
            return f"{text('name')} -> {text('path')}"
        if op == "end_session":
            return f"session {text('id')}"
        if op == "end_dashboard":
            return f"dashboard sign-in {text('id')}"
        return text("name")

    # ---- reading the system --------------------------------------------------------------------

    def _snapshot(self) -> dict:
        data = self.scheduler.refresh("users")
        if not isinstance(data, dict) or "error" in data or "users" not in data:
            detail = data.get("error") if isinstance(data, dict) else "no answer"
            raise ActionError(502, f"cannot read the account list: {detail}")
        return data

    def _account(self, data: dict, name, kinds: tuple = ("login",)) -> dict:
        if not isinstance(name, str) or not EXISTING_NAME.fullmatch(name):
            raise ActionError(400, "a user name is required")
        acct = next((u for u in data["users"] if u["name"] == name), None)
        if acct is None:
            raise ActionError(404, f"there is no user called {name}")
        if acct["type"] not in kinds:
            if acct["type"] == "root":
                raise ActionError(403, "root is never changed from the dashboard (only its password)")
            raise ActionError(403, f"{name} is a system account: the dashboard only manages login users")
        return acct

    def _groups(self) -> dict:
        try:
            return parse_group(Path(self.paths["group"]).read_text(errors="replace"))
        except OSError as e:
            raise ActionError(502, f"cannot read the group list ({e.strerror})") from None

    @staticmethod
    def _is_locked(acct: dict) -> bool:
        """Some lock is on: the password is locked or the account has expired."""
        return acct["password"] == "locked" or bool(acct["expired"])

    @staticmethod
    def _is_blocked(acct: dict) -> bool:
        """Nothing can log in as this account. Only an expiry date stops SSH keys: a locked password alone (`passwd -l`) does not."""
        return bool(acct["expired"])

    def _guard_last_admin(self, data: dict, acct: dict, verb: str) -> None:
        if acct["sudo"] != "full":
            return
        if data.get("sudo_source") != "sudoers":
            raise ActionError(502, "cannot tell who has sudo rights (sudoers is not readable), so this is refused")
        others = [u for u in data["users"] if u["type"] == "login" and u["name"] != acct["name"] and u["sudo"] == "full" and u["can_login"]]
        if not others:
            raise ActionError(403, f"{acct['name']} is the last user with full sudo rights who can log in: {verb} would leave nobody to run this server")

    def _command(self, args: list, what: str, **kw) -> str:
        try:
            return self._run(args, timeout=COMMAND_TIMEOUT_S, **kw)
        except CommandError as e:
            detail = str(e)
            if kw.get("secret"):
                detail = detail.replace(kw["secret"], "***")        # util.run does this too; a message is never trusted to be clean
            raise ActionError(502, f"{what} failed: {detail}") from None

    def _busy(self, data: dict, acct: dict, groups: dict, strict: bool) -> tuple[list[str], list[str]]:
        """(blockers, notes) for an account that is about to lose its name or its existence."""
        blockers = []
        if acct["processes"]:
            blockers.append(f"{acct['processes']} process{'es are' if acct['processes'] != 1 else ' is'} running as it (ban it to end its sessions, stop its services)")
        private = groups.get(acct["name"], {}).get("gid") == acct["gid"]
        found, notes = self.dependents.find(acct["name"], acct["uid"], private)
        if strict:
            return blockers + found + [f"{n}" for n in notes], []
        return blockers + found, notes

    # ---- looking before acting, and afterwards -----------------------------------------------

    def _need_writable(self, folder: str, doing: str) -> None:
        """Refuse before anything is changed if the dashboard's service cannot write in `folder` (a read-only mount, for one)."""
        if not self._access(folder, os.W_OK | os.X_OK):
            raise ActionError(409, f"the dashboard cannot write in {folder}, so it cannot {doing}. That folder is read-only to the dashboard's "
                                   "service: add its disk to ReadWritePaths in the service unit and restart it (see the README), or choose another folder")

    def _fresh_account(self, name: str) -> dict | None:
        try:
            return next((u for u in self._snapshot()["users"] if u["name"] == name), None)
        except ActionError:
            return None

    def _after_failed_home_change(self, name: str, old_home: str, new_home: str, reason: str) -> ActionError:
        """`usermod -d NEW -m` writes the new path to /etc/passwd BEFORE it moves anything, so "failed" can mean "the account now points at nothing".
        Look at what is true now and, if the account points at a folder that is not there while the old one is, put it back."""
        fresh = self._fresh_account(name)
        if fresh is None:
            return ActionError(502, f"{reason}; the account could not be read afterwards, so check its home folder yourself ({old_home} or {new_home})")
        now = fresh["home"]
        if now == old_home:
            return ActionError(502, f"{reason}; nothing was changed")
        if now != new_home:
            return ActionError(502, f"{reason}; the home folder of {name} is now {now}, which is neither {old_home} nor {new_home}: check it")
        old_there, new_there = os.path.isdir(old_home), os.path.isdir(new_home)
        if new_there:
            return ActionError(502, f"{reason}; the account now uses {new_home}" + (f", and {old_home} is still there as well: check what is left in it" if old_there else ""))
        if not old_there:
            return ActionError(502, f"{reason}; neither {old_home} nor {new_home} exists: look for the files before doing anything else")
        try:
            self._run(["usermod", "-d", old_home, "--", name], timeout=COMMAND_TIMEOUT_S)
        except CommandError as e:
            log.error("could not put the home folder of %s back to %s: %s", name, old_home, e)
            return ActionError(502, f"{reason}; the account points at {new_home}, which does not exist, and putting it back failed too ({e}): "
                                    f"run `usermod -d {old_home} {name}` as root")
        self.scheduler.refresh("users")
        return ActionError(502, f"{reason}; nothing was moved and the account was put back to {old_home}")

    def _after_failed_add(self, name: str, home: str, reason: str, existed: bool = False) -> ActionError:
        """useradd can fail after it has already created the account (for instance when the home folder cannot be made).
        `existed`: the name was already taken when useradd started (someone else made it in the meantime), so that account
        is not ours to remove."""
        if existed or self._fresh_account(name) is None:
            return ActionError(502, reason)
        left = f" The folder {home} was created and left in place." if os.path.lexists(home) else ""
        try:
            self._run(["userdel", "--", name], timeout=COMMAND_TIMEOUT_S)
        except CommandError as e:
            log.error("could not remove the half-created account %s: %s", name, e)
            return ActionError(502, f"{reason}; the account {name} was half created and removing it failed too ({e}): remove it with Remove, or `userdel {name}` as root.{left}")
        self.scheduler.refresh("users")
        return ActionError(502, f"{reason}; the half-created account was removed again.{left}")

    def _after_failed_remove(self, name: str, home: str, delete_home: bool, reason: str) -> ActionError:
        if self._fresh_account(name) is not None:
            return ActionError(502, f"{reason}; the account is still there")
        self.ledger.drop(name)
        self.scheduler.refresh("users")
        folder = f", but its home folder {home} was not deleted: remove it by hand if you want it gone" if delete_home else ""
        return ActionError(502, f"{reason}; the account is gone{folder}")

    # ---- add ---------------------------------------------------------------------------------

    def _new_name(self, data: dict, name, groups: dict) -> str:
        if not isinstance(name, str) or not NEW_NAME.fullmatch(name):
            raise ActionError(400, "a user name is 1 to 32 characters: lower-case letters, digits, '_' and '-', starting with a letter or '_'")
        if any(u["name"] == name for u in data["users"]):
            raise ActionError(409, f"there is already a user called {name}")
        if name in groups:
            raise ActionError(409, f"there is already a group called {name}")
        return name

    @staticmethod
    def _comment(value) -> str:
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > MAX_COMMENT or any(c in value for c in ":,\\") or any(ord(c) < 32 or c == "\x7f" for c in value):
            raise ActionError(400, f"the full name can be up to {MAX_COMMENT} characters and cannot contain ':' ',' '\\' or line breaks")
        return value.strip()

    def _parent_problem(self, path: str) -> str | None:
        parent = posixpath.dirname(path)
        if not os.path.isdir(parent):
            return f"the folder {parent} does not exist"
        if os.path.realpath(parent) != parent:
            return f"{parent} is, or lies behind, a symbolic link"
        # Whoever can write into a folder above the home can swap the home for their own (a .bashrc that runs when the new user logs
        # in), so every folder on the way up must belong to root and not be writable by anyone else.
        current = parent
        while True:
            try:
                st = os.lstat(current)
            except OSError as e:
                return f"cannot check {current} ({e.strerror})"
            if st.st_uid not in self.trusted_uids:
                return f"{current} belongs to another user (uid {st.st_uid}), who could replace the new home folder; use a folder that belongs to root"
            if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:      # a sticky folder (/tmp) only lets people replace their own entries
                return f"{current} can be written to by users other than its owner, who could replace the new home folder; tighten its permissions or choose another folder"
            if current == "/":
                return None
            current = posixpath.dirname(current)

    def _add(self, body: dict, who: dict) -> dict:
        data, groups = self._snapshot(), self._groups()
        name = self._new_name(data, body.get("name"), groups)
        shell = body.get("shell", "/bin/bash")
        if not isinstance(shell, str) or shell not in data.get("shells", []):
            raise ActionError(400, "that shell is not listed in /etc/shells")
        comment = self._comment(body.get("comment"))
        home = body.get("home") or f"/home/{name}"
        problem = home_problem(home, data["users"], protect=self.protect, forbidden=self.forbidden) or self._parent_problem(home)
        if problem:
            raise ActionError(400, problem)
        if os.path.lexists(home):
            raise ActionError(409, f"{home} already exists; choose another folder (nothing is taken over or overwritten)")
        self._need_writable(posixpath.dirname(home), "create the home folder")
        sudo, must_change = body.get("sudo", False), body.get("must_change", False)
        if not isinstance(sudo, bool) or not isinstance(must_change, bool):
            raise ActionError(400, "sudo and must_change must be true or false")
        if sudo and "sudo" not in groups:
            raise ActionError(400, "there is no 'sudo' group on this server")
        password = body.get("password")
        if password not in (None, ""):
            problem = password_problem(password)
            if problem:
                raise ActionError(400, problem)
        else:
            password = None
        existed = self._fresh_account(name) is not None
        args = ["useradd", "-m", "-U", "-d", home, "-s", shell, "-c", comment] + (["-G", "sudo"] if sudo else []) + ["--", name]
        try:
            self._command(args, f"creating {name}")
        except ActionError as e:
            raise self._after_failed_add(name, home, e.message, existed or "exit 9:" in e.message) from None      # useradd exit 9: the name was already taken, by someone else
        steps = [f"created {name} with home {home}"]
        if password is not None:
            try:
                self._command(["chpasswd"], f"setting the password of {name}", stdin=f"{name}:{password}\n", secret=password)
                if must_change:
                    self._command(["chage", "-d", "0", "--", name], f"requiring {name} to change the password")
                steps.append("password set" + (", to be changed at first login" if must_change else ""))
            except ActionError as e:
                self.scheduler.refresh("users")
                raise ActionError(502, f"{name} was created, but the password was not set ({e.message}); use Change password") from None
        else:
            steps.append("no password yet: they cannot log in until you give them a password or an SSH key")
        if sudo:
            steps.append("in the sudo group")
        self.scheduler.refresh("users")
        return {"ok": True, "detail": "; ".join(steps)}

    # ---- password ----------------------------------------------------------------------------

    def _password(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        acct = self._account(data, body.get("name"), kinds=("root", "login"))
        problem = password_problem(body.get("password"))
        if problem:
            raise ActionError(400, problem)
        must_change = body.get("must_change", False)
        if not isinstance(must_change, bool):
            raise ActionError(400, "must_change must be true or false")
        name, password = acct["name"], body["password"]
        self._command(["chpasswd"], f"setting the password of {name}", stdin=f"{name}:{password}\n", secret=password)
        if must_change:
            self._command(["chage", "-d", "0", "--", name], f"requiring {name} to change the password")
        self.scheduler.refresh("users")
        locked = " It is still locked or expired, so it cannot log in yet." if self._is_locked(acct) else ""
        return {"ok": True, "detail": f"password of {name} changed" + (", to be changed at the next login" if must_change else "") + "." + locked}

    # ---- lock, unlock, ban, unban ------------------------------------------------------------

    def _lock_account(self, data: dict, acct: dict, who: dict, verb: str) -> str:
        name = acct["name"]
        if self._is_blocked(acct):
            return "already locked"
        self._guard_last_admin(data, acct, verb)
        previous = acct["expires"] // 86400 if acct["expires"] else None
        # A password that was already locked before us (passwd -l) is not ours to undo again on Unlock.
        self.ledger.put(name, previous, who["user"], kept_password_lock=acct["password"] == "locked")
        args = ["usermod"] + (["-L"] if acct["password"] in ("set", "empty") else []) + ["-e", str(LOCKED_EXPIRY_DAY), "--", name]
        try:
            self._command(args, f"locking {name}")
        except ActionError:
            self.ledger.drop(name)
            raise
        return "locked"

    def _lock(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        acct = self._account(data, body.get("name"))
        if self._is_blocked(acct):
            raise ActionError(409, f"{acct['name']} is already locked")
        self._lock_account(data, acct, who, "locking it")
        self.scheduler.refresh("users")
        return {"ok": True, "detail": f"{acct['name']} is locked: no new logins (password or SSH key). Sessions that are open stay open."}

    def _ban(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        acct = self._account(data, body.get("name"))
        require_confirmation(body, acct["name"])
        state = self._lock_account(data, acct, who, "banning it")
        ended = self._end_user_sessions(acct["name"], any(x["user"] == acct["name"] for x in data.get("sessions", [])))
        self.scheduler.refresh("users")
        return {"ok": True, "detail": f"{acct['name']} is banned ({state}); {ended}"}

    def _end_user_sessions(self, name: str, had_sessions: bool) -> str:
        try:
            self._run(["loginctl", "terminate-user", "--", name], timeout=COMMAND_TIMEOUT_S, ok_codes=(0, 1))     # 1: "not logged in"
        except CommandError as e:
            raise ActionError(502, f"the account is locked, but ending its sessions failed: {e}") from None
        return "its sessions are ended" if had_sessions else "it had no open sessions"

    def _unlock_account(self, acct: dict) -> str:
        """Give the account back its old expiry date (or none) and un-switch its password. Returns a note for the person."""
        name = acct["name"]
        entry = self.ledger.get(name)
        restore = entry["expires_day"] if entry and isinstance(entry.get("expires_day"), int) else None
        keep_pw_lock = bool(entry and entry.get("kept_password_lock"))
        args = ["usermod"] + (["-U"] if acct["password"] == "locked" and not keep_pw_lock else [])
        if acct["expired"] or entry:                                  # never touch a real expiry date of an account we did not lock
            args += ["-e", str(restore) if restore is not None else ""]
        args += ["--", name]
        self._command(args, f"unlocking {name}")
        self.ledger.drop(name)
        note = ""
        if restore is not None and restore * 86400 <= self._clock():
            note += " Its original expiry date has already passed, so it is still expired."
        if acct["password"] == "none":
            note += " It has no password: it can only log in with an SSH key."
        return note

    def _unlock(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        acct = self._account(data, body.get("name"))
        if not self._is_locked(acct):
            raise ActionError(409, f"{acct['name']} is not locked")
        note = self._unlock_account(acct)
        self.scheduler.refresh("users")
        return {"ok": True, "detail": f"{acct['name']} is unlocked.{note}"}

    def _unban(self, body: dict, who: dict) -> dict:
        return self._unlock(body, who)

    # ---- sessions ----------------------------------------------------------------------------

    def _end_session(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        session_id = body.get("id")
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ActionError(400, "a session id is required")
        session = next((s for s in data.get("sessions", []) if s["id"] == session_id), None)
        if session is None:
            raise ActionError(404, "that session no longer exists")
        self._command(["loginctl", "terminate-session", "--", session_id], f"ending session {session_id}")
        self.scheduler.refresh("users")
        return {"ok": True, "detail": f"session {session_id} of {session['user']} is ended"}

    def _end_dashboard(self, body: dict, who: dict) -> dict:
        session_id = body.get("id")
        if not isinstance(session_id, str) or not DASHBOARD_ID.fullmatch(session_id):
            raise ActionError(400, "a sign-in id is required")
        if session_id == body.get("current_id"):
            raise ActionError(400, "that is this browser: use Sign out instead")
        if not self.sessions.revoke_by_id(session_id):
            raise ActionError(404, "that dashboard sign-in no longer exists")
        return {"ok": True, "detail": "that dashboard sign-in is ended"}

    # ---- rename ------------------------------------------------------------------------------

    def _rename(self, body: dict, who: dict) -> dict:
        data, groups = self._snapshot(), self._groups()
        acct = self._account(data, body.get("name"))
        old, old_home = acct["name"], acct["home"]
        require_confirmation(body, old)
        new = self._new_name(data, body.get("new_name"), groups)
        rename_home = body.get("rename_home", False)
        if not isinstance(rename_home, bool):
            raise ActionError(400, "rename_home must be true or false")
        blockers, _ = self._busy(data, acct, groups, strict=True)
        if blockers:
            raise ActionError(409, f"{old} cannot be renamed yet: " + "; ".join(blockers))
        new_home = None
        if rename_home:
            if posixpath.basename(acct["home"]) != old:
                raise ActionError(400, f"its home folder ({acct['home']}) is not named after the user, so it is not renamed")
            new_home = posixpath.join(posixpath.dirname(acct["home"]), new)
            problem = home_problem(acct["home"], data["users"], own=old, protect=self.protect, forbidden=self.forbidden) or home_problem(new_home, data["users"], own=old, protect=self.protect, forbidden=self.forbidden)
            if problem:
                raise ActionError(400, f"the home folder cannot be renamed: {problem}")
            if os.path.lexists(new_home):
                raise ActionError(409, f"{new_home} already exists")
            self._need_writable(posixpath.dirname(acct["home"]), "rename the home folder")
        private = groups.get(old, {}).get("gid") == acct["gid"]
        self._command(["usermod", "-l", new, "--", old], f"renaming {old}")
        steps = [f"{old} is now {new}"]
        if private:
            try:
                self._command(["groupmod", "-n", new, "--", old], "renaming its group")
                steps.append("its group is renamed too")
            except ActionError as e:
                try:
                    self._run(["usermod", "-l", old, "--", new], timeout=COMMAND_TIMEOUT_S)
                except CommandError:
                    log.error("could not undo the rename of %s after the group rename failed", old)
                    raise ActionError(502, f"{e.message}; the account is now called {new} but its group is still {old}") from None
                raise ActionError(502, f"{e.message}; the account name was put back to {old}") from None
        self.ledger.rename(old, new)
        if new_home:
            try:
                self._command(["usermod", "-d", new_home, "-m", "--", new], "moving the home folder")
                steps.append(f"home folder moved to {new_home}")
            except ActionError as e:
                raise self._after_failed_home_change(new, old_home, new_home, f"{old} is now {new}, but {e.message}") from None
        self.scheduler.refresh("users")
        return {"ok": True, "detail": "; ".join(steps)}

    # ---- home folder -------------------------------------------------------------------------

    def _home(self, body: dict, who: dict) -> dict:
        data = self._snapshot()
        acct = self._account(data, body.get("name"))
        name, old_home = acct["name"], acct["home"]
        require_confirmation(body, name)
        path, move = body.get("path"), body.get("move", False)
        if not isinstance(move, bool):
            raise ActionError(400, "move must be true or false")
        problem = home_problem(path, data["users"], own=name, protect=self.protect, forbidden=self.forbidden) or self._parent_problem(path)
        if problem:
            raise ActionError(400, problem)
        if path == acct["home"]:
            raise ActionError(400, "that is already its home folder")
        created = False
        if move:
            if acct["processes"]:
                raise ActionError(409, f"{acct['processes']} process(es) are running as {name}: end its sessions and stop its services before moving the folder")
            problem = home_problem(acct["home"], data["users"], own=name, protect=self.protect, forbidden=self.forbidden)
            if problem or not os.path.isdir(acct["home"]) or os.path.islink(acct["home"]):
                raise ActionError(400, f"its current home folder ({acct['home']}) is not an ordinary folder that can be moved" + (f": {problem}" if problem else ""))
            if os.path.lexists(path):
                raise ActionError(409, f"{path} already exists; moving needs a folder that does not exist yet")
            self._need_writable(posixpath.dirname(path), "create the new home folder")
            self._need_writable(posixpath.dirname(acct["home"]), "move the old home folder away")
            args = ["usermod", "-d", path, "-m", "--", name]
        else:
            if os.path.lexists(path):
                st = os.lstat(path)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != acct["uid"]:
                    raise ActionError(409, f"{path} exists but is not a folder owned by {name}")
            else:
                self._need_writable(posixpath.dirname(path), "create the new home folder")
                self._create_home(path, acct)
                created = True
            args = ["usermod", "-d", path, "--", name]
        try:
            self._command(args, f"changing the home folder of {name}")
        except ActionError as e:
            if created:
                try:
                    os.rmdir(path)                                  # we made it a moment ago, so it is empty
                except OSError:
                    log.warning("could not remove the folder %s created for %s", path, name)
            raise self._after_failed_home_change(name, old_home, path, e.message) from None
        self.scheduler.refresh("users")
        how = "moved there" if move else "created" if created else "now used"
        return {"ok": True, "detail": f"the home folder of {name} is {path} ({how})"}

    @staticmethod
    def _create_home(path: str, acct: dict) -> None:
        parent = os.open(posixpath.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.close(safefs.ensure_dir(parent, posixpath.basename(path), acct["uid"], acct["gid"], 0o750))
        except safefs.UnsafePath as e:
            raise ActionError(409, f"the folder cannot be created safely: {e}") from None
        except OSError as e:
            raise ActionError(502, f"the folder cannot be created: {e.strerror}") from None
        finally:
            os.close(parent)

    # ---- remove ------------------------------------------------------------------------------

    def _remove(self, body: dict, who: dict) -> dict:
        data, groups = self._snapshot(), self._groups()
        acct = self._account(data, body.get("name"))
        name = acct["name"]
        require_confirmation(body, name)
        delete_home = body.get("delete_home", False)
        if not isinstance(delete_home, bool):
            raise ActionError(400, "delete_home must be true or false")
        self._guard_last_admin(data, acct, "removing it")
        blockers, notes = self._busy(data, acct, groups, strict=False)
        if blockers:
            raise ActionError(409, f"{name} cannot be removed yet: " + "; ".join(blockers))
        if delete_home:
            problem = home_problem(acct["home"], data["users"], own=name, protect=self.protect, forbidden=self.forbidden)
            if problem:
                raise ActionError(409, f"its home folder ({acct['home']}) is not deleted automatically: {problem}")
            if os.path.lexists(acct["home"]):
                st = os.lstat(acct["home"])
                if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != acct["uid"]:
                    raise ActionError(409, f"{acct['home']} is not a folder owned by {name}, so it is not deleted")
                self._need_writable(posixpath.dirname(acct["home"]), "delete the home folder")
        try:
            self._command(["userdel"] + (["-r"] if delete_home else []) + ["--", name], f"removing {name}")
        except ActionError as e:
            raise self._after_failed_remove(name, acct["home"], delete_home, e.message) from None
        self.ledger.drop(name)
        self.scheduler.refresh("users")
        detail = f"{name} is removed" + (f", with its home folder {acct['home']}" if delete_home else f"; its home folder {acct['home']} is kept")
        if notes:
            detail += ". Left behind: " + "; ".join(notes)
        return {"ok": True, "detail": detail}

    # ---- the folder browser ------------------------------------------------------------------

    def _accounts_for_browsing(self) -> list[dict]:
        _, data = self.scheduler.get("users")                  # a minute old at most: the action itself checks again
        return (data or {}).get("users", [])

    def places(self) -> list[dict]:
        """Where homes usually live: /home and every real disk, with free space and whether the dashboard may write there."""
        try:
            mounts = parse_mountinfo(Path(self.mountinfo).read_text())
        except OSError:
            mounts = []
        wanted = {"/home": None}
        for m in mounts:
            target = m["target"]
            if m["root"] == "/" and m["fstype"] in REAL_FILESYSTEMS and target != "/" and not any(target == t or target.startswith(t + "/") for t in self.forbidden):
                wanted[target] = m["source"]
        out = []
        for target in sorted(wanted, key=lambda t: (t != "/home", t)):
            if not os.path.isdir(target):
                continue
            device = wanted[target] or next((m["source"] for m in sorted(mounts, key=lambda m: -len(m["target"]))
                                             if target == m["target"] or target.startswith(m["target"].rstrip("/") + "/")), "")
            try:
                st = os.statvfs(target)
                free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
            except OSError:
                free = total = None
            out.append({"path": target, "device": device, "free": free, "total": total, "writable": bool(self._access(target, os.W_OK | os.X_OK))})
        return out

    def _plain_dir(self, path) -> str:
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 300 or any(ord(c) < 32 or c == "\x7f" for c in path):
            raise ActionError(400, "a folder path starting with / is required")
        if path != posixpath.normpath(path):
            raise ActionError(400, "write the path in its plain form: no '..', '.', double or trailing slashes")
        if not os.path.isdir(path):
            raise ActionError(404, f"{path} is not a folder")
        if os.path.realpath(path) != path:
            raise ActionError(400, f"{path} is, or lies behind, a symbolic link")
        return path

    def _enter_problem(self, path: str, users: list[dict], own: str | None) -> str | None:
        """Why the browser will not open `path` at all (as opposed to not offering it as a choice)."""
        if path == "/":
            return None
        for top in self.forbidden:
            if (path == top or path.startswith(top + "/")) and not any(path == e or path.startswith(e + "/") for e in HOME_EXCEPTIONS):
                return f"{top} is a system folder"
        for folder in self.protect:
            if path == folder or path.startswith(folder + "/"):
                return "the dashboard's own folder"
        for u in users:
            home = u["home"]
            if u["name"] != own and u["type"] != "system" and len(home.strip("/").split("/")) >= 2 and (path == home or path.startswith(home + "/")):
                return f"the home folder of {u['name']}"
        return None

    def browse(self, path=None, hidden: bool = False, own: str | None = None, name=None) -> dict:
        """A folder listing for the home folder picker. Folders only, nothing behind a symbolic link, system places and other people's
        homes greyed out with the reason. With `name`, also whether `path/name` would be an acceptable home folder."""
        places = self.places()
        if not path:
            path = places[0]["path"] if places else "/"
        path = self._plain_dir(path)
        users = self._accounts_for_browsing()
        problem = self._enter_problem(path, users, own)
        if problem:
            raise ActionError(403, f"{path} cannot be opened: {problem}")
        try:
            with os.scandir(path) as listing:
                found = list(listing)
        except OSError as e:
            raise ActionError(403, f"{path} cannot be read ({e.strerror})") from None
        folders = []
        for entry in found:
            if entry.name.startswith(".") and not hidden:
                continue
            try:
                link = entry.is_symlink()
                if not (entry.is_dir(follow_symlinks=True) if link else entry.is_dir(follow_symlinks=False)):
                    continue
            except OSError:
                continue
            full = posixpath.join(path, entry.name) if path != "/" else "/" + entry.name
            if link:
                folders.append({"name": entry.name, "path": full, "kind": "link", "enterable": False, "selectable": False, "reason": "a symbolic link"})
                continue
            enter = self._enter_problem(full, users, own)
            choose = None if enter else home_problem(full, users, own=own, protect=self.protect, forbidden=self.forbidden)
            folders.append({"name": entry.name, "path": full, "kind": "folder", "enterable": enter is None, "selectable": enter is None and choose is None,
                            "reason": enter or choose or ""})
        folders.sort(key=lambda f: f["name"].lower())
        out = {"path": path, "parent": posixpath.dirname(path) if path != "/" else None, "places": places, "folders": folders[:MAX_BROWSE],
               "truncated": len(folders) > MAX_BROWSE, "writable": bool(self._access(path, os.W_OK | os.X_OK)),
               "selectable": home_problem(path, users, own=own, protect=self.protect, forbidden=self.forbidden) is None}
        out["reason"] = "" if out["selectable"] else home_problem(path, users, own=own, protect=self.protect, forbidden=self.forbidden)
        if name is not None:
            bad = folder_name_problem(name)
            target = posixpath.join(path, name) if not bad else None
            why = bad or home_problem(target, users, own=own, protect=self.protect, forbidden=self.forbidden)
            out["choice"] = {"path": target, "ok": why is None, "reason": why or "", "exists": bool(target) and os.path.lexists(target)}
        return out
