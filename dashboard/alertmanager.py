"""Alert state machine and Telegram delivery.

alerts.evaluate() says what is wrong *right now*. This module remembers: how long a condition has
lasted, whether a person has been told, when to repeat, when it is over, and what has been muted.
State survives a restart (data/alert_state.json), so restarting the dashboard does not re-send
everything.

Telegram messages go through a background queue with retries, so a slow or unreachable Telegram
can never delay data collection or a web request.
"""

import html
import json
import logging
import os
import queue
import tempfile
import threading
import time
from collections import deque

from .alerts import TelegramError, evaluate, section_of, send_telegram

log = logging.getLogger("dashboard.alerts")

RANK = {None: 0, "info": 1, "warn": 2, "crit": 3}
LABEL = {"crit": "CRITICAL", "warn": "WARNING", "info": "NOTICE"}
EMOJI = {"crit": "🔴", "warn": "🟠", "info": "🔵"}
RECOVER_S = 30            # an alert must be gone this long before it counts as resolved
SAVE_EVERY_S = 10
MAX_KNOWN = 500
BAN_SPIKE = 5             # this many new fail2ban bans within BAN_WINDOW_S is worth a message
BAN_WINDOW_S = 600
STALE_MUTE_S = 600        # an until-clear mute whose alert has not appeared by now is dropped
DIGEST_OVER = 4           # more new messages than this in one pass are sent as a single digest


def fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    if s >= 86400:
        return f"{s // 86400} d {s % 86400 // 3600} h"
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s} s"


class Notifier:
    """Sends Telegram messages from a background thread, retrying with growing delays."""

    RETRY_DELAYS = (0, 5, 30, 120)

    def __init__(self, cfg: dict, send=None, delays: tuple | None = None):
        tg = cfg["telegram"]
        self.enabled = bool(tg["enabled"] and tg["bot_token"] and str(tg["chat_id"]))
        self._send = send or (lambda text: send_telegram(cfg, text))
        self._delays = self.RETRY_DELAYS if delays is None else delays
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.dropped = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None

    def status(self) -> dict:
        return {"configured": self.enabled, "sent": self.sent, "dropped": self.dropped,
                "last_ok": self.last_ok, "last_error": self.last_error}

    def start(self) -> None:
        if self.enabled and not self._thread:
            self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._thread.start()
        elif not self.enabled:
            log.info("Telegram is not configured: alerts are shown on the dashboard only")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def send(self, text: str) -> bool:
        """Queue a message. Never blocks; returns False if Telegram is off or the queue is full."""
        if not self.enabled:
            return False
        try:
            self._queue.put_nowait(text)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def send_now(self, text: str) -> tuple[bool, str | None]:
        """Send immediately and report the outcome (for the "send test alert" button)."""
        if not self.enabled:
            return False, "Telegram is not configured: set telegram.bot_token and telegram.chat_id in config.json and restart"
        try:
            self._send(text)
        except TelegramError as e:
            self.last_error = str(e)
            return False, str(e)
        self.sent += 1
        self.last_ok, self.last_error = time.time(), None
        return True, None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self._deliver(text)

    def _deliver(self, text: str) -> None:
        for attempt, delay in enumerate(self._delays):
            if delay and self._stop.wait(delay):
                return
            try:
                self._send(text)
            except TelegramError as e:
                self.last_error = str(e)
                log.warning("Telegram send failed (attempt %d of %d): %s", attempt + 1, len(self._delays), e)
                continue
            self.sent += 1
            self.last_ok, self.last_error = time.time(), None
            return
        self.dropped += 1
        log.error("Telegram message dropped after %d attempts", len(self._delays))


class AlertManager:
    def __init__(self, cfg: dict, notifier: Notifier, history=None, state_path=None, clock=time.time):
        self.cfg, self.notifier, self.history, self.clock = cfg, notifier, history, clock
        self.state_path = state_path
        self._lock = threading.RLock()
        self._alerts: dict[str, dict] = {}       # per alert id: confirmed level, timers, what was sent
        self._mutes: dict[str, dict] = {}        # per alert id: {"until": ts | None, "until_clear": bool}
        self._known: dict[str, set] = {"ssh": set(), "dashboard": set()}
        self._events_sent: dict[str, float] = {}  # cooldowns for one-off messages
        self._seen_logins: set = set()
        self._ssh_ready = False
        self._bans: deque = deque()
        self._dirty = False
        self._last_save = 0.0
        self._have_known = False
        self._load()

    # ---- configuration helpers -------------------------------------------------------------

    @property
    def server_name(self) -> str:
        return self.cfg["alerts"].get("server_name") or self.cfg["public_host"]

    def _repeat_s(self, level: str) -> float:
        base = self.cfg["alerts"]["repeat_minutes"] * 60
        return {"crit": base, "warn": base * 4, "info": 86400}[level]

    # ---- persistence -----------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        for alert_id, st in (raw.get("alerts") or {}).items():
            if isinstance(st, dict) and st.get("level") in RANK:
                self._alerts[alert_id] = {**self._new(), **st}
        self._mutes = {k: v for k, v in (raw.get("mutes") or {}).items() if isinstance(v, dict)}
        known = raw.get("known")
        if isinstance(known, dict):
            self._have_known = "ssh" in known
            for kind in self._known:
                self._known[kind] = set(known.get(kind) or [])

    def save(self, force: bool = False) -> None:
        if not self.state_path:
            return
        with self._lock:
            now = self.clock()
            if not (self._dirty and (force or now - self._last_save >= SAVE_EVERY_S)):
                return
            data = {
                "alerts": {k: {f: v for f, v in st.items() if f in ("level", "since", "notified_level", "last_notified")}
                           for k, st in self._alerts.items() if st["level"]},
                "mutes": self._mutes,
                "known": {kind: sorted(ips)[-MAX_KNOWN:] for kind, ips in self._known.items()},
            }
            directory = os.path.dirname(self.state_path)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".alert-state-", suffix=".tmp")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self.state_path)
            except OSError:
                log.exception("could not save alert state")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            self._dirty = False
            self._last_save = now

    @staticmethod
    def _new() -> dict:
        return {"level": None, "since": None, "seen_level": None, "seen_since": None, "clear_since": None,
                "notified_level": None, "last_notified": None, "title": "", "detail": "", "for_s": 0}

    # ---- the state machine -----------------------------------------------------------------

    def update(self, sections: dict, now: float | None = None) -> None:
        """Feed the latest collector results. Call every few seconds; it is cheap."""
        now = self.clock() if now is None else now
        with self._lock:
            skipped: set = set()
            active = {k: st["level"] for k, st in self._alerts.items() if st["level"]}
            instant = {a["id"]: a for a in evaluate(self.cfg, sections, active, skipped)}
            messages: list[str] = []

            for alert_id, a in instant.items():
                st = self._alerts.setdefault(alert_id, self._new())
                st.update(title=a["title"], detail=a["detail"], for_s=a["for_s"], clear_since=None)
                if st["seen_level"] != a["level"]:
                    st["seen_level"], st["seen_since"] = a["level"], now
                if now - st["seen_since"] >= a["for_s"] and st["level"] != a["level"]:
                    previous = st["level"]
                    st["level"] = a["level"]
                    self._dirty = True
                    if previous is None:
                        st["since"] = now
                    if RANK[a["level"]] > RANK[previous]:
                        self._record("alert", alert_id, a["level"], a["title"])
                if st["level"] and not self._muted(alert_id, now):
                    text = self._due_message(alert_id, st, now)
                    if text:
                        messages.append(text)

            for alert_id in list(self._alerts):
                if alert_id in instant:
                    continue
                st = self._alerts[alert_id]
                section = section_of(alert_id)
                if section in skipped:                       # no data is not good news: hold the state
                    continue
                if st["level"] is None:                      # was only pending
                    del self._alerts[alert_id]
                    continue
                if st["clear_since"] is None:
                    st["clear_since"] = now
                if now - st["clear_since"] >= RECOVER_S:
                    text = None if self._muted(alert_id, now) else self._recovery_message(alert_id, st, now)
                    if text:
                        messages.append(text)
                    self._record("alert_resolved", alert_id, "info", f"{st['title']} (resolved)")
                    if self._mutes.get(alert_id, {}).get("until_clear"):
                        del self._mutes[alert_id]
                    del self._alerts[alert_id]
                    self._dirty = True

            for alert_id, mute in list(self._mutes.items()):           # a mute for an alert that never fired must not linger
                if mute.get("until_clear") and mute.get("until") is None and alert_id not in self._alerts \
                        and now - mute.get("created", now) > STALE_MUTE_S:
                    del self._mutes[alert_id]
                    self._dirty = True

            self._check_logins(sections, now, messages)
            self._check_bans(sections, now, messages)
            self._send(messages)
            self.save()

    def _muted(self, alert_id: str, now: float) -> bool:
        mute = self._mutes.get(alert_id)
        if not mute:
            return False
        if mute.get("until") is not None and mute["until"] <= now:
            del self._mutes[alert_id]
            self._dirty = True
            return False
        return True

    def _due_message(self, alert_id: str, st: dict, now: float) -> str | None:
        """Text to send for an active alert if a message is due (first time, escalation or repeat)."""
        if not self.notifier.enabled:
            return None                                       # not marked as sent: it fires once Telegram is configured
        level = st["level"]
        first = st["notified_level"] is None
        escalated = RANK[level] > RANK[st["notified_level"]]
        repeat = not first and not escalated and now - st["last_notified"] >= self._repeat_s(level)
        if not (first or escalated or repeat):
            return None
        st["notified_level"], st["last_notified"] = level, now
        self._dirty = True
        elapsed = now - (st["since"] or now)
        head = "⬆️ Now" if escalated and not first else "⏰ Still" if repeat else ""
        duration = f" for {fmt_duration(elapsed)}" if elapsed >= 10 else ""        # "for 0 s" on a brand-new alert is noise
        return self._format(level, st["title"], st["detail"], f"{head} {LABEL[level]}{duration}".strip())

    def _recovery_message(self, alert_id: str, st: dict, now: float) -> str | None:
        if not (self.cfg["alerts"]["notify_recovery"] and self.notifier.enabled):
            return None
        if st["notified_level"] in (None, "info"):            # nobody was told, or it was only a notice
            return None
        return (f"✅ <b>{html.escape(self.server_name)}</b>: recovered\n{html.escape(st['title'])}\n"
                f"<i>was {LABEL[st['notified_level']]} for {fmt_duration(now - (st['since'] or now))}</i>")

    def _format(self, level: str, title: str, detail: str, footer: str) -> str:
        link = f'<a href="https://{html.escape(self.cfg["public_host"])}/#overview">open dashboard</a>'
        lines = [f"{EMOJI[level]} <b>{html.escape(self.server_name)}</b>: {html.escape(title)}"]
        if detail:
            lines.append(html.escape(detail))
        lines.append(f"<i>{html.escape(footer)}</i> · {link}")
        return "\n".join(lines)

    def _send(self, messages: list[str]) -> None:
        if not messages:
            return
        if len(messages) > DIGEST_OVER:                      # a burst (for example after a reboot): one message, not twenty
            shown, extra = messages[:8], len(messages) - 8   # each message is short, so 8 stay far below Telegram's 4096-character limit
            tail = f"\n\n…and {extra} more, see the dashboard" if extra > 0 else ""
            messages = [f"📋 <b>{html.escape(self.server_name)}</b>: {len(messages)} new alerts\n\n" + "\n\n".join(shown) + tail]
        for text in messages:
            self.notifier.send(text)

    def _record(self, kind: str, alert_id: str, level: str, message: str) -> None:
        if self.history:
            try:
                self.history.add_event(kind, alert_id, level, message)
            except Exception:  # noqa: BLE001 - the event log must never break alerting
                log.exception("could not record alert event")

    # ---- one-off events --------------------------------------------------------------------

    def event(self, key: str, text: str, level: str = "info", cooldown_s: float = 900) -> bool:
        """A single notification that is not a state (a lockout, a dashboard action). Rate-limited per key."""
        with self._lock:
            now = self.clock()
            if now - self._events_sent.get(key, -1e18) < cooldown_s:
                return False
            self._events_sent[key] = now
            if len(self._events_sent) > 500:
                self._events_sent = {k: t for k, t in self._events_sent.items() if now - t < 3600}
            self._record("event", key, level, text)
            return self.notifier.send(f"{EMOJI.get(level, '🔵')} <b>{html.escape(self.server_name)}</b>: {html.escape(text)}")

    def _check_logins(self, sections: dict, now: float, messages: list[str]) -> None:
        """Tell the owner about SSH logins from an address that has never logged in before."""
        ssh = sections.get("ssh_auth")
        if not isinstance(ssh, dict) or "error" in ssh:
            return
        accepted = list(reversed(ssh.get("recent_accepted", [])))        # oldest first
        if not self._ssh_ready:
            logins = sections.get("logins") or {}
            if not self._have_known:                                      # first run: learn the history silently
                for entry in logins.get("history", []):
                    if entry.get("from"):
                        self._known["ssh"].add(entry["from"])
                for entry in logins.get("sessions", []):
                    if entry.get("from"):
                        self._known["ssh"].add(entry["from"])
                for entry in accepted:
                    self._known["ssh"].add(entry["ip"])
                self._have_known = True
                self._dirty = True
                self._seen_logins = {(e["time"], e["user"], e["ip"]) for e in accepted}
                self._ssh_ready = True
                return
            self._ssh_ready = True                                        # later runs: logins made while we were down count
        for entry in accepted:
            key = (entry["time"], entry["user"], entry["ip"])
            if key in self._seen_logins:
                continue
            self._seen_logins.add(key)
            if entry["ip"] not in self._known["ssh"]:
                self._known["ssh"].add(entry["ip"])
                self._dirty = True
                if self.cfg["alerts"]["notify_logins"] and self.notifier.enabled:
                    method = f" ({html.escape(entry['method'])})" if entry.get("method") else ""
                    messages.append(f"🔑 <b>{html.escape(self.server_name)}</b>: new SSH login\n"
                                    f"<b>{html.escape(entry['user'])}</b> from <code>{html.escape(entry['ip'])}</code>{method}")
        if len(self._seen_logins) > 2000:
            self._seen_logins = {(e["time"], e["user"], e["ip"]) for e in accepted}

    def dashboard_login(self, ip: str, user_agent: str) -> None:
        """Called after a successful dashboard sign-in; a new address is reported once."""
        with self._lock:
            if ip in self._known["dashboard"]:
                return
            self._known["dashboard"].add(ip)
            self._dirty = True
            if self.cfg["alerts"]["notify_logins"]:
                self.notifier.send(f"🔑 <b>{html.escape(self.server_name)}</b>: new device signed in to the dashboard\n"
                                   f"<code>{html.escape(ip)}</code> · {html.escape(user_agent[:80])}")
            self.save(force=True)

    def _check_bans(self, sections: dict, now: float, messages: list[str]) -> None:
        f2b = sections.get("fail2ban")
        if not isinstance(f2b, dict) or "error" in f2b:
            return
        total = sum(j["total_banned"] for j in f2b["jails"])
        if not self._bans or self._bans[-1][1] != total:
            self._bans.append((now, total))
        while self._bans and now - self._bans[0][0] > BAN_WINDOW_S:
            self._bans.popleft()
        if self._bans and total - self._bans[0][1] >= BAN_SPIKE and self.notifier.enabled:
            if now - self._events_sent.get("ban-spike", -1e18) >= 3600:
                self._events_sent["ban-spike"] = now
                messages.append(f"🛡️ <b>{html.escape(self.server_name)}</b>: fail2ban banned "
                                f"{total - self._bans[0][1]} addresses in {fmt_duration(now - self._bans[0][0])}")

    # ---- views and controls ----------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        """Confirmed alerts for the dashboard, worst first."""
        with self._lock:
            now = self.clock()
            out = []
            for alert_id, st in self._alerts.items():
                if not st["level"]:
                    continue
                mute = self._mutes.get(alert_id) if self._muted(alert_id, now) else None
                out.append({"id": alert_id, "level": st["level"], "title": st["title"], "detail": st["detail"],
                            "since": st["since"], "muted": bool(mute),
                            "muted_until": mute.get("until") if mute else None,
                            "muted_until_clear": bool(mute and mute.get("until_clear")),
                            "notified": st["notified_level"] is not None})
            out.sort(key=lambda a: (-RANK[a["level"]], a["id"]))
            return out

    def mute(self, alert_id: str, minutes: float | None = None, until_clear: bool = False) -> None:
        """Stop messages for an alert for `minutes`, or until it clears (until_clear). It stays visible."""
        with self._lock:
            until = None if minutes is None else self.clock() + minutes * 60
            self._mutes[alert_id] = {"until": until, "until_clear": until_clear or minutes is None, "created": self.clock()}
            self._dirty = True
            self.save(force=True)

    def unmute(self, alert_id: str) -> None:
        with self._lock:
            if self._mutes.pop(alert_id, None) is not None:
                self._dirty = True
                self.save(force=True)

    def is_muted(self, alert_id: str) -> bool:
        with self._lock:
            return self._muted(alert_id, self.clock())

    def status(self) -> dict:
        return self.notifier.status()

    def stop(self) -> None:
        self.save(force=True)
