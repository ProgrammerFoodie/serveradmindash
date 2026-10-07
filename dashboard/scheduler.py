"""Runs collectors on their cadence, keeps the latest result of each and records history.

Two threads: "fast" samples every 5 s on a fixed grid (CPU, memory, IO, network; these feed the
charts), and "background" does everything slower plus the history flush and maintenance.
Keeping them apart means a slow `apt list` or `systemctl` call never delays a 5 s sample.
"""

import logging
import threading
import time

from . import collectors
from .history import History, extract_metrics

log = logging.getLogger("dashboard.scheduler")

FAST_S = 5
MEDIUM_S = 60
SLOW_S = 6 * 3600
SLOW_START_DELAY_S = 30        # `apt list` briefly needs ~80 MB; do not stack it on top of startup
VACUUM_EVERY_S = 24 * 3600
ON_DEMAND = {"processes"}      # only collected while someone is looking at them
DEMAND_WINDOW_S = 30


class Scheduler:
    def __init__(self, cfg: dict, history: History, instances: dict | None = None, alerts=None):
        self.cfg = cfg
        self.history = history
        self.alerts = alerts
        self.collectors = instances if instances is not None else collectors.create(cfg)
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._demand: dict[str, float] = {}

    # ---- access --------------------------------------------------------------------------

    def get(self, name: str) -> tuple[float | None, dict | None]:
        """Latest (collection time, result) of a collector, or (None, None) if it has not run yet."""
        with self._lock:
            return self._data.get(name, (None, None))

    def snapshot(self) -> dict[str, dict]:
        """Latest result of every collector that has run, by name."""
        with self._lock:
            return {n: d for n, (_t, d) in self._data.items()}

    def touch(self, name: str) -> None:
        """Mark an on-demand collector as wanted, so it keeps running for the next half minute."""
        self._demand[name] = time.monotonic()

    def refresh(self, name: str) -> dict:
        """Run one collector right now (after an action, or from a refresh button)."""
        return self._run(name)

    def names(self, cadence: str) -> list[str]:
        return [n for n, c in self.collectors.items() if c.cadence == cadence]

    # ---- running -------------------------------------------------------------------------

    def _run(self, name: str) -> dict:
        data = collectors.run(self.collectors[name], self.cfg)
        with self._lock:
            self._data[name] = (time.time(), data)
        if "error" in data:
            log.debug("collector %s: %s", name, data["error"])
        return data

    def _wanted(self, name: str) -> bool:
        if name not in ON_DEMAND:
            return True
        return time.monotonic() - self._demand.get(name, float("-inf")) < DEMAND_WINDOW_S

    def _fast_loop(self) -> None:
        names = self.names("fast")
        while not self._stop.is_set():
            # Next tick on the 5 s wall-clock grid; a stalled tick is skipped, not replayed.
            tick = (int(time.time()) // FAST_S + 1) * FAST_S
            if self._stop.wait(max(0.0, tick - time.time())):
                return
            for name in names:
                if self._wanted(name):
                    self._run(name)
            with self._lock:
                snapshot = {n: d for n, (_t, d) in self._data.items()}
            try:
                self.history.record(tick, extract_metrics(snapshot))
            except Exception:  # noqa: BLE001 - history problems must never stop sampling
                log.exception("recording history failed")
            if self.alerts:
                try:
                    self.alerts.update(snapshot, tick)
                except Exception:  # noqa: BLE001 - a bug in alerting must not stop sampling either
                    log.exception("alert evaluation failed")

    def _background_loop(self) -> None:
        started = time.monotonic()
        every = {n: MEDIUM_S for n in self.names("medium")} | {n: SLOW_S for n in self.names("slow")}
        due = {n: 0.0 for n in self.names("medium")} | {n: started + SLOW_START_DELAY_S for n in self.names("slow")}
        next_flush = started + MEDIUM_S
        last_vacuum = started
        while not self._stop.is_set():
            now = time.monotonic()
            for name, at in due.items():
                if self._stop.is_set():
                    return
                if at <= now:
                    self._run(name)
                    due[name] = time.monotonic() + every[name]
            if time.monotonic() >= next_flush:
                next_flush = time.monotonic() + MEDIUM_S
                self._flush_and_maintain(vacuum=time.monotonic() - last_vacuum >= VACUUM_EVERY_S)
                if time.monotonic() - last_vacuum >= VACUUM_EVERY_S:
                    last_vacuum = time.monotonic()
            self._stop.wait(1.0)

    def _flush_and_maintain(self, vacuum: bool = False) -> None:
        try:
            self.history.flush()
            self.history.maintain(vacuum=vacuum)
        except Exception:  # noqa: BLE001 - keep sampling; samples stay buffered for the next attempt
            log.exception("history flush/maintenance failed")

    # ---- lifecycle -----------------------------------------------------------------------

    def start(self) -> None:
        for target, name in ((self._fast_loop, "fast"), (self._background_loop, "background")):
            t = threading.Thread(target=target, name=f"scheduler-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)
        self._flush_and_maintain()
        if self.alerts:
            self.alerts.stop()
