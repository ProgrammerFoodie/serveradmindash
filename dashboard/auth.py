"""Password hashing (scrypt), persistent sessions and login throttling."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import deque

SESSION_COOKIE = "__Host-sid"   # the __Host- prefix makes browsers insist on Secure, Path=/ and no Domain
MIN_PASSWORD_LEN = 10
MAX_PASSWORD_LEN = 1024         # scrypt cost does not depend on length, but there is no reason to accept megabytes
MAX_SESSIONS = 20
DEFAULT_IDLE_MINUTES = 15       # a session with no user activity for this long must sign in again

# scrypt cost: n=2^14, r=8 needs 16 MB RAM and ~150 ms per check on this 1 vCPU box.
_N, _R, _P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def fingerprint(password_hash: str, username: str) -> str:
    """Identifies the current credentials; sessions made under other credentials stop working."""
    return hashlib.sha256(f"{username}\0{password_hash}".encode()).hexdigest()[:16]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Login sessions stored in SQLite so a restart does not log anyone out.

    Only the SHA-256 of the session token is stored, so a copy of the database cannot be
    used to impersonate a session. Changing the password or username invalidates every session.

    A session ends when it is older than `hours`, or when nothing has called touch() for `idle_minutes`.
    lookup() never counts as activity: the page polls every few seconds, and that must not keep an
    unattended browser signed in. Only requests a person makes (page load, buttons, the front end's
    input heartbeat) call touch().
    """

    def __init__(self, path, cred_fingerprint: str, hours: float, idle_minutes: float = DEFAULT_IDLE_MINUTES):
        self._lock = threading.Lock()
        self._fp = cred_fingerprint
        self.ttl = int(hours * 3600)
        self.idle = int(idle_minutes * 60)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=10, isolation_level=None)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, fp TEXT NOT NULL, "
            "created INTEGER NOT NULL, last_seen INTEGER NOT NULL, expires INTEGER NOT NULL, ip TEXT, ua TEXT)")
        os.chmod(path, 0o600)
        now = int(time.time())
        self._db.execute("DELETE FROM sessions WHERE fp != ? OR expires < ? OR last_seen <= ?",
                         (self._fp, now, now - self.idle))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create(self, ip: str, user_agent: str) -> tuple[str, str]:
        """Return (session token, CSRF token). The session token is never stored in the clear."""
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires < ? OR last_seen <= ?", (now, now - self.idle))
            self._db.execute(
                "DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions "
                "ORDER BY last_seen DESC LIMIT -1 OFFSET ?)", (MAX_SESSIONS - 1,))
            self._db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (_token_hash(token), csrf, self._fp, now, now, now + self.ttl, ip[:64], user_agent[:200]))
        return token, csrf

    def lookup(self, token: str) -> dict | None:
        """The live session for this token, or None. Read-only apart from deleting a dead session."""
        if not token or len(token) > 128:
            return None
        now = int(time.time())
        with self._lock:
            row = self._db.execute(
                "SELECT csrf, fp, created, last_seen, expires, ip FROM sessions WHERE token_hash = ?",
                (_token_hash(token),)).fetchone()
            if row is None:
                return None
            csrf, fp, created, last_seen, expires, ip = row
            if expires < now or now - last_seen >= self.idle or not hmac.compare_digest(fp, self._fp):
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
                return None
        return {"csrf": csrf, "created": created, "expires": expires, "ip": ip,
                "idle_left": self.idle - (now - last_seen)}

    def touch(self, token: str) -> None:
        """Record user activity: restart the idle clock of a session that lookup() just accepted."""
        with self._lock:
            self._db.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                             (int(time.time()), _token_hash(token)))

    def list_active(self, current_token: str = "") -> list[dict]:
        """Dashboard sign-ins that are still valid, most recently active first.

        An id is the first 12 characters of the stored token hash: enough to tell sign-ins apart, and useless for
        taking one over (the token cannot be recovered from its hash)."""
        now = int(time.time())
        mine = _token_hash(current_token) if current_token else None
        with self._lock:
            rows = self._db.execute(
                "SELECT token_hash, created, last_seen, expires, ip, ua FROM sessions "
                "WHERE fp = ? AND expires >= ? AND ? - last_seen < ? ORDER BY last_seen DESC", (self._fp, now, now, self.idle)).fetchall()
        return [{"id": h[:12], "created": created, "last_seen": last_seen, "expires": expires, "idle_left": self.idle - (now - last_seen),
                 "ip": ip or "", "ua": ua or "", "current": h == mine} for h, created, last_seen, expires, ip, ua in rows]

    @staticmethod
    def id_of(token: str) -> str:
        """The id list_active() shows for this token's sign-in."""
        return _token_hash(token)[:12]

    def revoke_by_id(self, session_id: str) -> int:
        """End the dashboard sign-in whose id (as shown by list_active) is `session_id`. Returns how many ended (0 or 1)."""
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{12}", session_id):
            return 0
        with self._lock:
            cursor = self._db.execute("DELETE FROM sessions WHERE substr(token_hash, 1, 12) = ?", (session_id,))
            return cursor.rowcount

    def revoke(self, token: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


class LoginLimiter:
    """Failed-login throttle: a small limit per client address plus a larger one overall.

    The overall limit matters because the client address comes from a header that any local
    process can forge; without it, rotating fake addresses would defeat the per-address limit.
    """

    def __init__(self, per_ip: int = 5, overall: int = 30, window: float = 900, clock=time.monotonic):
        self.per_ip, self.overall, self.window, self._clock = per_ip, overall, window, clock
        self._by_ip: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, q: deque, now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def retry_after(self, ip: str) -> int:
        """Seconds to wait before another attempt is allowed; 0 if allowed now."""
        now = self._clock()
        with self._lock:
            wait = 0.0
            for q, limit in ((self._by_ip.get(ip, deque()), self.per_ip), (self._all, self.overall)):
                self._prune(q, now)
                if len(q) >= limit:
                    wait = max(wait, q[len(q) - limit] + self.window - now)
            return int(wait) + 1 if wait > 0 else 0

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        with self._lock:
            self._by_ip.setdefault(ip, deque()).append(now)
            self._all.append(now)
            if len(self._by_ip) > 1000:   # bound memory against forged addresses
                for key in [k for k, q in self._by_ip.items() if not q or q[-1] <= now - self.window]:
                    del self._by_ip[key]

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._by_ip.pop(ip, None)
