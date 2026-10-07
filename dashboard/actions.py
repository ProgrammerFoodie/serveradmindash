"""Things the dashboard may DO: start, stop and restart services, and signal processes.

This is the dangerous part of the program, so it is built around refusing:

* Services are allow-listed. Only units named in config.watch.systemd and supervisor programs that
  currently exist can be touched, and names are never passed through a shell or as options.
* Protected services (ssh, tailscaled, nginx, the dashboard and its DNS responder by default) can be
  restarted but never stopped, because stopping them would cut off the way back in.
* A process is identified by its PID *and* its exact start tick, then signalled through a pidfd, so
  a PID that was recycled between "looked at the table" and "clicked kill" can never be hit.
* PID 1, kernel threads, the dashboard and everything that started it, core system daemons, and any
  process that belongs to a protected service are refused.
* Every attempt, allowed or refused, goes to the audit log, and to Telegram when it matters.
"""

import logging
import os
import re
import select
import signal
import threading
import time
from collections import deque

from .collectors.processes import PF_KTHREAD, unit_of
from .jobs import Job, Jobs
from .util import CommandError, read_text, run

log = logging.getLogger("dashboard.actions")

SERVICE_OPS = ("start", "stop", "restart")
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")           # used with fullmatch; cannot start with "-", so never an option
SELF_UNIT = "server-dashboard"
ALWAYS_PROTECTED = {SELF_UNIT, "admin-dns"}
SYSTEM_COMMS = {"init", "sshd", "tailscaled", "dbus-daemon"}
SERVICE_TIMEOUT_S = 60
EXIT_WAIT_S = 2.0
MAX_ACTIONS_PER_MINUTE = 20


class ActionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def clean(value, limit: int = 100) -> str:
    """One printable line of at most `limit` characters, for audit entries and Telegram messages."""
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


def require_confirmation(body: dict, expected: str) -> None:
    """The typed-confirmation check for dangerous actions. The page asks the person to type `expected`
    (a host or user name); the server checks it again, because the page is not to be trusted."""
    if not isinstance(body.get("confirm"), str) or body["confirm"].strip() != expected:
        raise ActionError(400, f"confirmation does not match: type {expected}")


def read_identity(pid: int) -> dict:
    """What /proc says about a process right now. Raises ProcessLookupError if it is gone."""
    try:
        stat = read_text(f"/proc/{pid}/stat")
        cgroup = read_text(f"/proc/{pid}/cgroup")
    except (FileNotFoundError, ProcessLookupError):
        raise ProcessLookupError(pid) from None
    comm = stat[stat.index("(") + 1:stat.rindex(")")]
    f = stat[stat.rindex(")") + 2:].split()                  # f[0] is field 3 (state)
    return {"pid": pid, "comm": comm, "ppid": int(f[1]), "flags": int(f[6]), "start_ticks": int(f[19]), "unit": unit_of(cgroup)}


def own_pids() -> set[int]:
    """This process and every ancestor: signalling any of them would stop the dashboard."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            pid = read_identity(pid)["ppid"]
        except ProcessLookupError:
            break
    return pids


class Actions:
    def __init__(self, cfg: dict, scheduler, alerts, audit, runner=run, clock=time.time, jobs: Jobs | None = None):
        self.cfg, self.scheduler, self.alerts, self.audit = cfg, scheduler, alerts, audit
        self._run, self._clock = runner, clock
        self.jobs = jobs or Jobs(clock=clock)
        self._handlers = {"service": self._service, "process": self._process}
        self._labels = {}                                    # kind -> function(body) -> {"action", "target"}; services and processes use _label
        self.protected = set(cfg["watch"]["protected"]) | ALWAYS_PROTECTED
        self._lock = threading.Lock()
        self._recent: deque = deque()

    # ---- entry point -----------------------------------------------------------------------

    def register(self, kind: str, handler, label) -> None:
        """Add a kind of action. `handler(body)` returns {"detail": text, ...} or raises ActionError;
        `label(body)` returns {"action", "target"} for the audit log, built only from values that are safe to print
        (never a password, key or file content). Everything registered gets the lock, the rate limit, the audit
        entry and the Telegram message that services and processes get."""
        if kind in self._handlers:
            raise ValueError(f"action kind already registered: {kind}")
        self._handlers[kind], self._labels[kind] = handler, label

    def _label_for(self, kind: str, body: dict) -> dict:
        try:
            label = self._labels[kind](body) if kind in self._labels else self._label(kind, body)
            return {"action": clean(label["action"], 64), "target": clean(label["target"], 200)}
        except Exception:  # noqa: BLE001 - a broken label must never stop the action from being audited
            log.exception("label for %s failed", kind)
            return {"action": clean(kind, 64), "target": ""}

    def run_job(self, kind: str, body: dict, who: dict, work) -> Job:
        """Start a long operation in the background and return its Job at once.

        `work(job)` runs on its own thread and reports progress with job.step(...). It may return a detail
        string, or raise ActionError. The one-action-at-a-time lock is held until it finishes, so nothing
        else can be started meanwhile (and a second job is refused with 409).
        """
        if not self._lock.acquire(blocking=False):
            raise ActionError(409, "another action is still running; wait a moment")
        try:
            self._rate_limit()
            label = self._label_for(kind, body)
            job = self.jobs.create(kind, f"{label['action']} {label['target']}".strip(), who["user"])
        except BaseException:
            self._lock.release()
            raise

        def runner():
            ok, outcome, status = False, "internal error", 500
            try:
                outcome = work(job) or "done"
                ok, status = True, 200
            except ActionError as e:
                outcome, status = e.message, e.status
            except Exception as e:  # noqa: BLE001 - an unexpected bug must still be audited and reported cleanly
                log.exception("job %s crashed", label)
                outcome = f"internal error: {type(e).__name__}"
            finally:
                job.finish(ok, outcome)
                self._report(who, label, ok=ok, outcome=outcome, status=status)
                self._lock.release()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()
        return job

    def perform(self, kind: str, body: dict, who: dict) -> dict:
        """Run one action. `who` is {"user", "ip"}. Raises ActionError for anything refused or failed."""
        if not self._lock.acquire(blocking=False):
            raise ActionError(409, "another action is still running; wait a moment")
        try:
            self._rate_limit()
            handler = self._handlers.get(kind)
            if handler is None:
                raise ActionError(400, "unknown action")
            label = self._label_for(kind, body)
            try:
                result = handler(body)
            except ActionError as e:
                self._report(who, label, ok=False, outcome=e.message, status=e.status)
                raise
            except Exception as e:  # noqa: BLE001 - an unexpected bug must still be audited and reported cleanly
                log.exception("action %s crashed", label)
                self._report(who, label, ok=False, outcome=f"internal error: {type(e).__name__}", status=500)
                raise ActionError(500, "internal error") from None
            self._report(who, label, ok=True, outcome=result["detail"], status=200)
            return result
        finally:
            self._lock.release()

    def _rate_limit(self) -> None:
        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= MAX_ACTIONS_PER_MINUTE:
            raise ActionError(429, "too many actions in a minute")
        self._recent.append(now)

    @staticmethod
    def _label(kind: str, body: dict) -> dict:
        """Short description used for the audit log, built only from values that are safe to print."""
        if kind == "service":
            return {"action": f"service.{str(body.get('op'))[:16]}", "target": f"{str(body.get('kind'))[:16]}:{str(body.get('name'))[:100]}"}
        return {"action": f"process.{str(body.get('signal'))[:16]}", "target": f"pid:{str(body.get('pid'))[:12]} {str(body.get('name') or '')[:60]}".strip()}

    def _report(self, who: dict, label: dict, ok: bool, outcome: str, status: int) -> None:
        self.audit.record(who["user"], who["ip"], label["action"], label["target"], ok, "done" if ok else f"refused ({status})", outcome)
        if not self.alerts:
            return
        if ok:
            text, level = f"{who['user']} did {label['action']} on {label['target']}: {outcome} (from {who['ip']})", "info"
        elif status in (403, 500, 502):                     # refused or failed: worth a message; typos and stale tables are not
            text, level = f"refused or failed: {label['action']} on {label['target']}: {outcome} (from {who['ip']})", "warn"
        else:
            return
        self.alerts.event(f"action:{time.time_ns()}", text, level, cooldown_s=0)

    # ---- services --------------------------------------------------------------------------

    def _service(self, body: dict) -> dict:
        kind, name, op = body.get("kind"), body.get("name"), body.get("op")
        if not all(isinstance(v, str) for v in (kind, name, op)) or not NAME_OK.fullmatch(name):
            raise ActionError(400, "kind, name and op are required")
        if op not in SERVICE_OPS:
            raise ActionError(400, f"op must be one of {', '.join(SERVICE_OPS)}")
        if kind == "systemd":
            return self._systemd(name, op)
        if kind == "supervisor":
            return self._supervisor(name, op)
        raise ActionError(400, "kind must be systemd or supervisor")

    def _systemd(self, name: str, op: str) -> dict:
        allowed = {u.removesuffix(".service") for u in self.cfg["watch"]["systemd"]}
        if name not in allowed:
            raise ActionError(404, "that service is not on the watch list")
        _, data = self.scheduler.get("systemd")
        info = next((u for u in (data or {}).get("watched", []) if u["name"] == name), None)
        if info is None or not info["exists"]:
            raise ActionError(404, "that service is not installed")
        if op == "stop" and name in self.protected:
            raise ActionError(403, f"{name} is protected: stopping it would cut you off. Restart it instead.")
        if op == "start" and name == SELF_UNIT:
            raise ActionError(400, "the dashboard is already running")
        if op == "stop" and self.alerts:
            self.alerts.mute(f"unit:{name}", until_clear=True)        # a stop you asked for must not page you
        # The dashboard restarting itself cannot wait for the result: it is the thing being restarted.
        args = ["systemctl", op] + (["--no-block"] if name == SELF_UNIT else []) + ["--", f"{name}.service"]
        try:
            self._run(args, timeout=SERVICE_TIMEOUT_S)
        except CommandError as e:
            raise ActionError(502, f"systemctl {op} {name} failed: {e}") from None
        if name == SELF_UNIT:
            return {"ok": True, "reconnect": True, "detail": "restart scheduled; the dashboard comes back in a few seconds"}
        state = self.scheduler.refresh("systemd")
        now = next((u for u in state.get("watched", []) if u["name"] == name), None) if isinstance(state, dict) else None
        return {"ok": True, "detail": f"{op} {name}: now {now['active']} ({now['sub']})" if now else f"{op} {name}: done"}

    def _supervisor(self, name: str, op: str) -> dict:
        _, data = self.scheduler.get("supervisor")
        if name not in {p["name"] for p in (data or {}).get("programs", [])}:
            raise ActionError(404, "that supervisor program does not exist")
        if op == "stop" and self.alerts:
            self.alerts.mute(f"supervisor:{name}", until_clear=True)
        try:
            out = self._run(["supervisorctl", op, name], timeout=SERVICE_TIMEOUT_S)
        except CommandError as e:
            raise ActionError(502, f"supervisorctl {op} {name} failed: {e}") from None
        first = out.strip().splitlines()[-1] if out.strip() else ""
        if "ERROR" in out or "error:" in out:
            raise ActionError(502, f"supervisorctl {op} {name}: {first or 'error'}")
        self.scheduler.refresh("supervisor")
        return {"ok": True, "detail": f"{op} {name}: {first or 'done'}"}

    # ---- processes -------------------------------------------------------------------------

    def protected_reason(self, info: dict) -> str | None:
        """Why this process must not be signalled, or None if it may be."""
        if info["pid"] == 1:
            return "PID 1 (init) is never touched"
        if info["flags"] & PF_KTHREAD or info["pid"] == 2 or info["ppid"] == 2:
            return "that is a kernel thread"
        if info["pid"] in own_pids():
            return "that is the dashboard itself (or the program that started it)"
        comm = info["comm"]
        if comm.startswith("systemd") or comm in SYSTEM_COMMS:
            return f"{comm} is a core system process"
        unit = info["unit"]
        if unit.endswith(".service") and unit[:-len(".service")] in self.protected:
            return f"it belongs to the protected service {unit[:-len('.service')]}; restart that service from the Services tab instead"
        return None

    def _process(self, body: dict) -> dict:
        pid, sig, ticks, expect = body.get("pid"), body.get("signal"), body.get("start_ticks"), body.get("name")
        is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)    # noqa: E731
        if not is_int(pid) or not is_int(ticks) or ticks < 0 or not isinstance(sig, str):
            raise ActionError(400, "pid, signal and start_ticks are required")
        if sig not in SIGNALS:
            raise ActionError(400, "signal must be TERM or KILL")
        if expect is not None and not isinstance(expect, str):
            raise ActionError(400, "name must be text")
        if pid == 1:
            raise ActionError(403, "PID 1 (init) is never touched")
        if not 2 <= pid <= 4_194_304:
            raise ActionError(400, "pid out of range")
        try:
            fd = os.pidfd_open(pid)                                      # from here on we hold *this* process, not "whoever has the PID"
        except ProcessLookupError:
            raise ActionError(404, "no such process (it may already have exited)") from None
        except OSError as e:
            raise ActionError(502, f"cannot open the process: {e.strerror}") from None
        try:
            try:
                info = read_identity(pid)
            except ProcessLookupError:
                raise ActionError(404, "no such process (it may already have exited)") from None
            if info["start_ticks"] != ticks:
                raise ActionError(409, "that PID now belongs to a different process; refresh the table and look again")
            if expect is not None and info["comm"] != expect:
                raise ActionError(409, "the process name does not match; refresh the table and look again")
            reason = self.protected_reason(info)
            if reason:
                raise ActionError(403, f"refused: {reason}")
            try:
                signal.pidfd_send_signal(fd, SIGNALS[sig])
            except ProcessLookupError:
                return {"ok": True, "exited": True, "detail": f"{info['comm']} (PID {pid}) had already exited"}
            except PermissionError:
                raise ActionError(403, "the operating system refused the signal") from None
            exited = bool(select.select([fd], [], [], EXIT_WAIT_S)[0])      # a pidfd becomes readable when the process ends
        finally:
            os.close(fd)
        return {"ok": True, "exited": exited,
                "detail": f"sent SIG{sig} to {info['comm']} (PID {pid}): {'it exited' if exited else 'it is still running'}"}
