"""Accounts and who is signed in: /etc/passwd, group, shadow, sudoers, lastlog and logind.

Password hashes are read only to learn whether a password is set, locked or missing. The hash itself is dropped
inside the parser and never leaves it; a test checks that no hash appears anywhere in the output.

Everything that needs root (shadow, sudoers) degrades to a partial answer with a note instead of failing, so
`python3 -m dashboard check users` is still useful as a normal user.
"""

import os
import re
import struct
import time
from pathlib import Path

from .. import safefs
from ..util import CommandError, run
from .security import read_wtmp

DAY = 86400
NO_LOGIN_SHELLS = {"nologin", "false", "sync", "shutdown", "halt"}
SUDO_GROUPS = ("sudo", "admin", "wheel")          # what is assumed when sudoers cannot be read
LASTLOG = struct.Struct("<i32s256s")              # glibc struct lastlog on 64-bit Linux: time, tty line, host
MAX_USERS = 1000
MAX_INCLUDE_DEPTH = 5
SESSION_PROPS = ("Id", "Name", "User", "Remote", "RemoteHost", "Service", "TTY", "Type", "Class", "State", "Leader",
                 "IdleHint", "IdleSinceHint", "TimestampMonotonic", "Scope")


# ---- /etc/passwd and /etc/group -----------------------------------------------------------------------

def parse_passwd(text: str) -> list[dict]:
    users = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 7 or not parts[0] or parts[0][0] in "#+-" or not parts[2].isdigit() or not parts[3].isdigit():
            continue
        name, _x, uid, gid, comment, home, shell = parts
        users.append({"name": name, "uid": int(uid), "gid": int(gid), "comment": comment.split(",")[0], "home": home,
                      "shell": shell or "/bin/sh"})                       # an empty shell field means /bin/sh
    return users


def parse_group(text: str) -> dict[str, dict]:
    groups = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) != 4 or not parts[0] or parts[0][0] in "#+-" or not parts[2].isdigit():
            continue
        groups[parts[0]] = {"gid": int(parts[2]), "members": [m for m in parts[3].split(",") if m]}
    return groups


# ---- /etc/shadow --------------------------------------------------------------------------------------

def password_state(field: str) -> str:
    """"set", "locked" (a password exists but is switched off), "none" (no password can be used) or "empty" (anyone may log in)."""
    if field == "":
        return "empty"
    if field.startswith("!"):
        rest = field.lstrip("!")
        return "locked" if rest.startswith("$") or len(rest) >= 13 else "none"
    if field.startswith("*"):
        return "none"
    return "set" if field.startswith("$") or len(field) >= 13 else "none"


def _days(value: str) -> int | None:
    return int(value) if value.lstrip("-").isdigit() else None


def parse_shadow(text: str, today: int) -> dict[str, dict]:
    """Per account: only what can be known without the hash. `today` is days since 1970."""
    out = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 8 or not parts[0] or parts[0][0] in "#+-":
            continue
        name, field = parts[0], parts[1]
        changed, max_days, expire = _days(parts[2]), _days(parts[4]), _days(parts[7])
        if max_days is not None and max_days >= 99999:
            max_days = None                                    # "never expires"
        expires_at = (changed + max_days) * DAY if changed and max_days is not None else None
        out[name] = {
            "password": password_state(field),
            "password_changed": changed * DAY if changed else None,
            "must_change": changed == 0,
            "password_max_days": max_days,
            "password_expires": expires_at,
            "password_expired": expires_at is not None and expires_at <= today * DAY,
            "expires": expire * DAY if expire is not None else None,
            "expired": expire is not None and expire <= today,
        }
    return out


# ---- sudoers ------------------------------------------------------------------------------------------

class Sudoers:
    """Just enough of the sudoers grammar to answer "may this user use sudo, and without a password?":
    user specs (names, %groups, #uids, ALL, !negations), User_Alias, and @include / @includedir."""

    def __init__(self):
        self.aliases: dict[str, list[str]] = {}
        self.rules: list[dict] = []
        self.includes: list[tuple[str, bool]] = []            # (path, is_directory)

    def add_text(self, text: str, label: str) -> None:
        text = re.sub(r"\\\n", " ", text)
        for raw in text.splitlines():
            line = raw.strip()
            m = re.match(r"^[#@]include(dir)?\s+(\S+)", line)
            if m:
                self.includes.append((m.group(2), bool(m.group(1))))
                continue
            line = re.split(r"\s#(?!\d)", line, maxsplit=1)[0].strip()
            if not line or (line.startswith("#") and not re.match(r"^#\d", line)):         # "#1000 ALL=..." is a user spec, not a comment
                continue
            first = line.split(None, 1)[0]
            if first == "Defaults" or first.startswith("Defaults:") or first.startswith("Defaults@") or first.startswith("Defaults!"):
                continue
            if first in ("Runas_Alias", "Host_Alias", "Cmnd_Alias"):
                continue
            if first == "User_Alias":
                for chunk in line[len("User_Alias"):].split(":"):
                    name, _, members = chunk.partition("=")
                    if name.strip() and members.strip():
                        self.aliases[name.strip()] = [t.strip() for t in members.split(",") if t.strip()]
                continue
            self._add_rule(line, label)

    def _add_rule(self, line: str, label: str) -> None:
        left, eq, right = line.partition("=")
        if not eq:
            return
        pieces = left.rsplit(None, 1)
        if len(pieces) != 2:
            return                                               # no host list: not a user spec
        users = [t.strip() for t in pieces[0].split(",") if t.strip()]
        right = re.sub(r"^\s*\([^)]*\)", "", right)
        commands = [re.sub(r"^(\w+:\s*)+", "", c.strip()) for c in right.split(",")]
        self.rules.append({"users": users, "label": label, "nopasswd": "NOPASSWD:" in right, "full": "ALL" in commands})

    def _matches(self, token: str, name: str, uid: int, groups: set[str], gids: set[int], depth: int = 0) -> bool:
        if token == "ALL":
            return True
        if token in self.aliases:
            return depth < 8 and self._list_matches(self.aliases[token], name, uid, groups, gids, depth + 1)
        if token.startswith("%#"):
            return token[2:].isdigit() and int(token[2:]) in gids
        if token.startswith("%"):
            return token[1:] in groups
        if token.startswith("#"):
            return token[1:].isdigit() and int(token[1:]) == uid
        if token.startswith("+"):
            return False                                         # netgroups are not supported
        return token == name

    def _list_matches(self, tokens, name, uid, groups, gids, depth=0) -> bool:
        matched = False
        for token in tokens:                                     # as in sudo: the last entry that matches decides
            negated = token.startswith("!")
            if self._matches(token.lstrip("!"), name, uid, groups, gids, depth):
                matched = not negated
        return matched

    def for_user(self, name: str, uid: int, groups: set[str], gids: set[int]) -> list[dict]:
        return [r for r in self.rules if self._list_matches(r["users"], name, uid, groups, gids)]


def load_sudoers(main: str, directory: str, depth: int = 0) -> Sudoers:
    """Read sudoers and the files it includes. Raises OSError if the main file cannot be read."""
    sudoers = Sudoers()
    sudoers.add_text(Path(main).read_text(errors="replace"), "sudoers")
    paths = []
    if Path(directory).is_dir():
        paths += [p for p in sorted(Path(directory).iterdir()) if "." not in p.name and not p.name.endswith("~") and p.is_file()]
    for path in paths:
        sudoers.add_text(path.read_text(errors="replace"), f"sudoers.d/{path.name}")
    seen = {str(Path(directory))}
    for include, is_dir in list(sudoers.includes):
        target = Path(include) if include.startswith("/") else Path(main).parent / include
        if str(target) in seen or depth >= MAX_INCLUDE_DEPTH:
            continue
        seen.add(str(target))
        try:
            files = [p for p in sorted(target.iterdir()) if "." not in p.name and p.is_file()] if is_dir else [target]
            for path in files:
                sudoers.add_text(path.read_text(errors="replace"), f"include {path.name}")
        except OSError:
            continue
    return sudoers


# ---- lastlog, keys, processes -------------------------------------------------------------------------

def read_lastlog(path: str, uid: int) -> dict | None:
    """The last login of a uid from /var/log/lastlog (records are indexed by uid), or None if it never logged in."""
    with open(path, "rb") as f:
        f.seek(uid * LASTLOG.size)
        raw = f.read(LASTLOG.size)
    if len(raw) < LASTLOG.size:
        return None
    seconds, line, host = LASTLOG.unpack(raw)
    if seconds <= 0:
        return None
    text = lambda b: b.split(b"\0", 1)[0].decode("utf-8", "replace")      # noqa: E731
    return {"time": seconds, "from": text(host), "tty": text(line)}


def authorized_key_count(home: str, uid: int) -> int | None:
    """Keys in ~/.ssh/authorized_keys, or None when it cannot be looked at safely. A line that is not blank or a
    comment counts as a key; phase 17 replaces this with the real parser."""
    data = safefs.read_in_home(home, uid, ".ssh/authorized_keys")
    if data is None:
        return 0
    return sum(1 for line in data.decode("utf-8", "replace").splitlines() if line.strip() and not line.lstrip().startswith("#"))


def count_processes(proc: str = "/proc") -> dict[int, int]:
    counts: dict[int, int] = {}
    try:
        with os.scandir(proc) as entries:
            for entry in entries:
                if entry.name.isdigit():
                    try:
                        uid = entry.stat().st_uid
                    except OSError:
                        continue                                  # it exited while we looked
                    counts[uid] = counts.get(uid, 0) + 1
    except OSError:
        pass
    return counts


# ---- logind sessions ----------------------------------------------------------------------------------

def read_sessions(runner=run, cgroup: str = "/sys/fs/cgroup", clock=time.time, monotonic=time.monotonic) -> list[dict]:
    """People signed in right now (class "user": the systemd manager sessions are not people)."""
    ids = [line.split()[0] for line in runner(["loginctl", "list-sessions", "--no-legend"]).splitlines() if line.strip()]
    if not ids:
        return []
    args = ["loginctl", "show-session"] + [a for p in SESSION_PROPS for a in ("-p", p)] + ids
    offset = clock() - monotonic()
    sessions = []
    for block in runner(args).strip().split("\n\n"):
        p = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if p.get("Class") != "user":
            continue
        started, idle_since = p.get("TimestampMonotonic", ""), p.get("IdleSinceHint", "")
        uid = int(p["User"]) if p.get("User", "").isdigit() else None
        sessions.append({
            "id": p.get("Id"), "user": p.get("Name"), "uid": uid, "service": p.get("Service") or "",
            "tty": p.get("TTY") or "", "type": p.get("Type") or "", "state": p.get("State") or "",
            "from": p.get("RemoteHost") or ("local" if p.get("Remote") == "no" else ""),
            "leader": int(p["Leader"]) if p.get("Leader", "").isdigit() else None,
            "idle": p.get("IdleHint") == "yes",
            "idle_since": round(int(idle_since) / 1e6) if idle_since.isdigit() and int(idle_since) else None,
            "since": round(offset + int(started) / 1e6) if started.isdigit() and int(started) else None,
            "processes": _scope_processes(cgroup, uid, p.get("Scope", "")),
        })
    sessions.sort(key=lambda s: s["since"] or 0, reverse=True)
    return sessions


def _scope_processes(cgroup: str, uid: int | None, scope: str) -> int | None:
    if uid is None or not re.fullmatch(r"[\w.@-]+\.scope", scope or ""):
        return None
    try:
        return len(Path(cgroup, "user.slice", f"user-{uid}.slice", scope, "cgroup.procs").read_text().split())
    except OSError:
        return None


# ---- the collector ------------------------------------------------------------------------------------

class Users:
    cadence = "medium"

    def __init__(self, passwd="/etc/passwd", group="/etc/group", shadow="/etc/shadow", sudoers="/etc/sudoers",
                 sudoers_dir="/etc/sudoers.d", lastlog="/var/log/lastlog", login_defs="/etc/login.defs", proc="/proc",
                 cgroup="/sys/fs/cgroup", runner=run, clock=time.time):
        self.paths = {"passwd": passwd, "group": group, "shadow": shadow, "sudoers": sudoers, "sudoers_dir": sudoers_dir,
                      "lastlog": lastlog, "login_defs": login_defs, "proc": proc, "cgroup": cgroup}
        self._run, self._clock = runner, clock

    def _uid_range(self) -> tuple[int, int]:
        lo, hi = 1000, 60000
        try:
            for line in Path(self.paths["login_defs"]).read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    if parts[0] == "UID_MIN":
                        lo = int(parts[1])
                    elif parts[0] == "UID_MAX":
                        hi = int(parts[1])
        except OSError:
            pass
        return lo, hi

    def collect(self, cfg: dict) -> dict:
        now = self._clock()
        out: dict = {"notes": {}}
        accounts = parse_passwd(Path(self.paths["passwd"]).read_text(errors="replace"))[:MAX_USERS]
        groups = parse_group(Path(self.paths["group"]).read_text(errors="replace"))
        gid_name = {g["gid"]: n for n, g in groups.items()}
        uid_min, uid_max = self._uid_range()
        names = {a["uid"]: a["name"] for a in reversed(accounts)}

        shadow: dict = {}
        try:
            shadow = parse_shadow(Path(self.paths["shadow"]).read_text(errors="replace"), int(now // DAY))
        except OSError as e:
            out["notes"]["shadow"] = f"password and expiry details need root ({e.strerror})"

        sudoers, sudo_source = None, "sudoers"
        try:
            sudoers = load_sudoers(self.paths["sudoers"], self.paths["sudoers_dir"])
        except OSError as e:
            sudo_source = "groups"
            out["notes"]["sudo"] = f"sudoers is not readable ({e.strerror}); sudo rights are guessed from the groups {', '.join(SUDO_GROUPS)}"
        out["sudo_source"] = sudo_source

        lastlog_ok, newest_by_user = True, {}
        try:
            read_lastlog(self.paths["lastlog"], 0)
        except OSError:
            lastlog_ok = False
            try:
                for rec in read_wtmp(limit=2000):                      # newest first: keep the first record per user
                    if rec["type"] == "login":
                        newest_by_user.setdefault(rec["user"], {"time": rec["time"], "from": rec["from"], "tty": rec["tty"]})
            except OSError:
                pass
            out["notes"]["lastlog"] = "lastlog is not readable; last logins come from the login history"
        processes = count_processes(self.paths["proc"])

        users = []
        for a in accounts:
            name, uid = a["name"], a["uid"]
            member_of = {gid_name[a["gid"]]} if a["gid"] in gid_name else set()
            member_of |= {n for n, g in groups.items() if name in g["members"]}
            gids = {groups[n]["gid"] for n in member_of} | {a["gid"]}
            kind = "root" if uid == 0 else "login" if uid_min <= uid <= uid_max else "system"

            sudo, via, nopasswd = None, [], False
            if sudoers is not None:
                rules = sudoers.for_user(name, uid, member_of, gids)
                if rules:
                    sudo = "full" if any(r["full"] for r in rules) else "limited"
                    via = list(dict.fromkeys(r["label"] for r in rules))
                    nopasswd = any(r["nopasswd"] for r in rules)
            elif member_of & set(SUDO_GROUPS):
                sudo, via = "full", [f"group {g}" for g in SUDO_GROUPS if g in member_of]

            info = shadow.get(name, {})
            last = (read_lastlog(self.paths["lastlog"], uid) if lastlog_ok else newest_by_user.get(name))
            user = {
                **a, "type": kind, "primary_group": gid_name.get(a["gid"], str(a["gid"])),
                "groups": sorted(member_of), "sudo": sudo, "sudo_via": via, "nopasswd": nopasswd,
                "password": info.get("password", "unknown"), "password_changed": info.get("password_changed"),
                "must_change": info.get("must_change", False), "password_expires": info.get("password_expires"),
                "password_expired": info.get("password_expired", False), "expires": info.get("expires"),
                "expired": info.get("expired", False), "last_login": last, "processes": processes.get(uid, 0),
            }
            user.update(self._home(a["home"], uid, names))
            keys, keys_error = None, None
            real_shell = os.path.basename(a["shell"]) not in NO_LOGIN_SHELLS and bool(a["shell"])
            if kind != "system" and real_shell:
                try:
                    keys = authorized_key_count(a["home"], uid)
                except safefs.UnsafePath as e:
                    keys_error = str(e)
                except OSError as e:
                    keys_error = e.strerror
            user["keys"], user["keys_error"] = keys, keys_error
            user["blockers"] = self._blockers(user, real_shell)
            user["can_login"] = not user["blockers"]
            users.append(user)

        order = {"root": 0, "login": 1, "system": 2}
        users.sort(key=lambda u: (order[u["type"]], u["uid"]))
        out["users"] = users
        out["uid_range"] = [uid_min, uid_max]
        try:
            out["sessions"] = read_sessions(self._run, self.paths["cgroup"], self._clock)
        except CommandError as e:
            out["sessions"], out["notes"]["sessions"] = [], str(e)
        return out

    @staticmethod
    def _home(home: str, uid: int, names: dict) -> dict:
        try:
            st = os.stat(home)
        except OSError:
            return {"home_exists": False, "home_owner": None, "home_mode": None}
        return {"home_exists": True, "home_owner": names.get(st.st_uid, str(st.st_uid)), "home_mode": format(st.st_mode & 0o7777, "04o"),
                "home_owner_ok": st.st_uid == uid}

    @staticmethod
    def _blockers(user: dict, real_shell: bool) -> list[str]:
        blockers = []
        if not real_shell:
            blockers.append("its shell does not allow logins")
        if user["expired"]:
            blockers.append("the account has expired")
        if user["password"] in ("locked", "none") and user["keys"] == 0:
            blockers.append("no usable password and no SSH key")
        return blockers
