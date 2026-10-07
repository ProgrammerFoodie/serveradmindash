"""Reboot the server, cancel a reboot, and restart every watched service in a sensible order.

* A reboot is `shutdown -r +1` (a minute to change your mind) or `shutdown -r now`. What is scheduled is read
  from the file logind keeps for it (/run/systemd/shutdown/scheduled), so a reboot scheduled over SSH shows up
  here too, and cancelling works whoever scheduled it.
* The dashboard remembers that it asked for a reboot (data/reboot_pending.json, with the boot id). When it
  starts and finds a different boot id, the server did reboot, and Telegram hears that it is back.
* "Restart all services" is a background job. It restarts the watched systemd units one by one, data stores
  first and nginx last, and carries on after a failure. The dashboard, its DNS responder and tailscaled are
  never in it: restarting them would kill the job or cut the only way in.
* Every one of these needs the hostname typed, checked here again on the server.
"""

import html
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

from . import safefs
from .actions import SERVICE_TIMEOUT_S, ActionError, clean, require_confirmation
from .alertmanager import fmt_duration
from .config import RESTART_ALL_EXCLUDED
from .util import CommandError, run

log = logging.getLogger("dashboard.power")

SCHEDULED_FILE = "/run/systemd/shutdown/scheduled"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
DELAYS = (0, 60)                    # seconds: now, or a minute during which it can be cancelled
NOW_GRACE_S = 1.5                   # an immediate reboot waits this long so the page gets its answer first
DRAIN_S = 8.0                       # ... and for the Telegram message about it to leave
BACK_UP_WINDOW_S = 900              # a reboot older than this is not reported as "back up" (the request was long ago)
STALE_PENDING_S = 600               # a note about a reboot that never happened is dropped after this
ACTIVE_WAIT_S = 30                  # how long a restarted service gets to become active again

DATA_STORES = ("redis-server", "redis", "mysql", "mariadb", "postgresql", "memcached", "mongod")
SUPPORTING = ("cron", "chrony", "chronyd", "fail2ban", "smbd", "nmbd", "ssh", "sshd")
LAST = ("nginx",)
MODES = {"reboot": "reboot", "kexec": "reboot (kexec)", "poweroff": "power off", "halt": "halt"}


def _rank(unit: str) -> int:
    if unit in DATA_STORES:
        return 0
    if unit in LAST:
        return 3
    return 2 if unit in SUPPORTING else 1                 # everything else is an application (supervisor included)


class Power:
    def __init__(self, cfg: dict, actions, scheduler, alerts, audit, data_dir, *, runner=run, clock=time.time,
                 sleep=time.sleep, defer=None, scheduled_path=SCHEDULED_FILE, boot_id_path=BOOT_ID_FILE, hostname=None):
        self.cfg, self.actions, self.scheduler, self.alerts, self.audit = cfg, actions, scheduler, alerts, audit
        self._run, self._clock, self._sleep = runner, clock, sleep
        self._defer = defer or self._timer
        self._scheduled_path, self._boot_id_path = scheduled_path, boot_id_path
        self._pending_path = Path(data_dir) / "reboot_pending.json"
        self._hostname = hostname
        self._job_id: str | None = None

    @staticmethod
    def _timer(fn, seconds: float) -> None:
        timer = threading.Timer(seconds, fn)
        timer.daemon = True
        timer.start()

    @property
    def hostname(self) -> str:
        if self._hostname:
            return self._hostname
        return self.alerts.server_name if self.alerts else socket.gethostname()

    def register(self) -> None:
        self.actions.register("power.reboot", self._reboot,
                              lambda body: {"action": "power.reboot", "target": f"{self.hostname}, delay {clean(body.get('delay', 60), 8)} s"})
        self.actions.register("power.cancel", self._cancel, lambda body: {"action": "power.cancel", "target": self.hostname})
        self.actions.register("power.restart-all", None, lambda body: {"action": "power.restart-all", "target": self.hostname})

    # ---- what is going on ------------------------------------------------------------------

    def scheduled(self) -> dict | None:
        """The pending shutdown or reboot, if any: {"mode", "label", "at", "in_s", "message", "requested_by"}."""
        try:
            with open(self._scheduled_path, encoding="utf-8") as f:
                text = f.read(4096)
        except OSError:
            return None
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        try:
            at = int(fields["USEC"]) / 1_000_000
        except (KeyError, ValueError):
            return None
        mode = fields.get("MODE", "")
        pending = self._read_pending()
        return {"mode": mode, "label": MODES.get(mode, mode or "shutdown"), "at": round(at, 1),
                "in_s": max(0, round(at - self._clock())), "message": clean(fields.get("WALL_MESSAGE", ""), 200),
                "requested_by": pending["by"] if pending and mode in ("reboot", "kexec") else None}

    def live(self) -> dict:
        """What every page shows next to the alerts."""
        return {"scheduled": self.scheduled()}

    def describe(self) -> dict:
        """For the Power card and the confirmation dialogs."""
        job = self.actions.jobs.get(self._job_id) if self._job_id else None
        return {"hostname": self.hostname, "scheduled": self.scheduled(), "delays": list(DELAYS), "restart": self.restart_plan(),
                "job": job["id"] if job and job["state"] == "running" else None}

    def restart_plan(self) -> dict:
        """{"units": restarted in this order, "excluded": never restarted, "missing": configured but not installed}."""
        watched = list(dict.fromkeys(str(u).removesuffix(".service") for u in self.cfg["watch"]["systemd"]))
        configured = [str(u).removesuffix(".service") for u in (self.cfg.get("admin", {}).get("restart_order") or [])]
        order = configured or sorted(watched, key=_rank)           # sorted() is stable: config order is kept within a group
        _, data = self.scheduler.get("systemd")
        installed = {u["name"]: u["exists"] for u in (data or {}).get("watched", [])}
        units, excluded, missing = [], [], []
        for unit in order:
            if unit in RESTART_ALL_EXCLUDED:
                excluded.append(unit)
            elif installed.get(unit) is False:
                missing.append(unit)
            else:
                units.append(unit)
        excluded += [u for u in watched if u in RESTART_ALL_EXCLUDED and u not in excluded]
        return {"units": units, "excluded": excluded, "missing": missing}

    # ---- reboot and cancel -----------------------------------------------------------------

    def _reboot(self, body: dict, who: dict) -> dict:
        require_confirmation(body, self.hostname)
        delay = body.get("delay", 60)
        if isinstance(delay, bool) or delay not in DELAYS:
            raise ActionError(400, "delay must be 0 or 60 seconds")
        if self.scheduled():
            raise ActionError(409, "a shutdown or reboot is already scheduled; cancel it first")
        message = f"Reboot from the admin dashboard by {clean(who['user'], 32)}"
        self._write_pending(who, delay)
        if delay == 0:
            self._defer(lambda: self._reboot_now(message), NOW_GRACE_S)
            return {"ok": True, "reconnect": True, "rebooting": True, "detail": "rebooting now; this page reloads when the server is back"}
        try:
            self._run(["shutdown", "-r", "+1", message], timeout=15)
        except CommandError as e:
            self._clear_pending()
            raise ActionError(502, f"could not schedule the reboot: {e}") from None
        return {"ok": True, "detail": "reboot in 1 minute; cancel it from any page until then"}

    def _reboot_now(self, message: str) -> None:
        if self.alerts:
            self.alerts.notifier.drain(DRAIN_S)                    # the message about this reboot goes out first
        try:
            self._run(["shutdown", "-r", "now", message], timeout=15)
        except CommandError as e:
            log.error("immediate reboot failed: %s", e)
            self._clear_pending()
            if self.alerts:
                self.alerts.event("power:failed", f"the reboot did not start: {e}", "warn", cooldown_s=0)

    def _cancel(self, body: dict, who: dict) -> dict:
        if not self.scheduled():
            raise ActionError(409, "no reboot or shutdown is scheduled")
        try:
            self._run(["shutdown", "-c"], timeout=15)
        except CommandError as e:
            raise ActionError(502, f"could not cancel: {e}") from None
        self._clear_pending()
        return {"ok": True, "detail": "the scheduled shutdown is cancelled"}

    # ---- the "back up" message ---------------------------------------------------------------

    def on_start(self) -> None:
        """Call once when the dashboard starts: report a reboot it asked for, and tidy notes that are out of date."""
        pending = self._read_pending()
        if not pending:
            self._clear_pending()                                  # a corrupt note is worse than none
            return
        now = self._clock()
        age = now - pending["requested"]
        if pending["boot_id"] == self._boot_id():
            if age > STALE_PENDING_S:                              # same boot, long ago: the reboot never happened
                self._clear_pending()
            return
        self._clear_pending()
        if age > BACK_UP_WINDOW_S:
            return
        host = self.hostname
        self.audit.record("system", "-", "power.boot", host, True, "done", f"back up {int(age)} s after the reboot requested by {pending['by']}")
        if self.alerts:
            self.alerts.notifier.send(f"✅ <b>{html.escape(host)}</b>: back up, {html.escape(fmt_duration(age))} after the reboot "
                                      f"requested by {html.escape(pending['by'])}")

    # ---- restart all -----------------------------------------------------------------------

    def start_restart_all(self, body: dict, who: dict):
        require_confirmation(body, self.hostname)
        if not self.restart_plan()["units"]:
            raise ActionError(400, "there are no services to restart")
        job = self.actions.run_job("power.restart-all", body, who, self._restart_all)
        self._job_id = job.id
        return job

    def _restart_all(self, job) -> str:
        units = self.restart_plan()["units"]
        ok, failed = [], []
        for unit in units:
            job.step(f"restart {unit}")
            try:
                self._run(["systemctl", "restart", "--", f"{unit}.service"], timeout=SERVICE_TIMEOUT_S)
                problem = self._wait_active(unit)
            except CommandError as e:
                problem = f"systemctl restart failed: {e}"
            if problem:
                job.step_failed(problem)
                failed.append(unit)
            else:
                job.step_ok()
                ok.append(unit)
        self.scheduler.refresh("systemd")
        summary = f"{len(ok)} restarted" + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
        if failed:
            raise ActionError(502, summary)
        return summary

    def _wait_active(self, unit: str) -> str | None:
        """None once the unit is active, else what is wrong with it."""
        deadline = self._clock() + ACTIVE_WAIT_S
        while True:
            state = self._run(["systemctl", "is-active", "--", f"{unit}.service"], timeout=10, ok_codes=(0, 3)).strip()
            if state == "active":
                return None
            if state not in ("activating", "reloading") or self._clock() >= deadline:
                return f"{unit} is {state or 'not running'} after the restart"
            self._sleep(1)

    # ---- the note about a reboot we asked for ------------------------------------------------

    def _boot_id(self) -> str:
        try:
            with open(self._boot_id_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _write_pending(self, who: dict, delay: int) -> None:
        note = {"by": clean(who["user"], 64), "ip": clean(who["ip"], 64), "requested": self._clock(), "delay": delay, "boot_id": self._boot_id()}
        try:
            safefs.write_private(self._pending_path, json.dumps(note).encode())
        except OSError:
            log.exception("could not write %s", self._pending_path)       # the reboot still goes ahead; only the "back up" message is lost

    def _read_pending(self) -> dict | None:
        try:
            note = json.loads(self._pending_path.read_text())
            if (isinstance(note.get("by"), str) and isinstance(note.get("boot_id"), str)
                    and isinstance(note.get("requested"), (int, float)) and not isinstance(note["requested"], bool)):
                return note
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def _clear_pending(self) -> None:
        try:
            self._pending_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.exception("could not remove %s", self._pending_path)
