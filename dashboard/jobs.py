"""Background jobs: operations that take longer than one web request (restart every service, apply a config).

A job is a list of steps with a state each, kept in memory only (a restart of the dashboard forgets them; the
audit log is the permanent record). The page polls GET /api/jobs/<id> while a job runs.

Nothing put into a job may be a secret: job text is shown in the browser and is not redacted.
"""

import secrets
import threading
import time
from collections import OrderedDict

MAX_STEPS = 100
KEEP = 20
ID_LENGTH = 16          # hex characters


def _text(value, limit: int) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))[:limit]


class Job:
    def __init__(self, job_id: str, kind: str, label: str, user: str, clock=time.time):
        self.id, self.kind, self.label, self.user = job_id, kind, _text(label, 200), _text(user, 64)
        self._clock = clock
        self._lock = threading.Lock()
        self.state = "running"            # running | ok | failed
        self.detail = ""
        self.started, self.finished = clock(), None
        self.steps: list[dict] = []

    def step(self, label: str, detail: str = "") -> None:
        """Finish the current step successfully and start the next one."""
        with self._lock:
            self._close_current("ok")
            if len(self.steps) < MAX_STEPS:
                self.steps.append({"label": _text(label, 200), "state": "running", "detail": _text(detail, 500)})

    def step_ok(self, detail: str = "") -> None:
        """Mark the current step as done. Needed for the last step of a job that fails for another reason:
        finish(False) would otherwise blame the step that happened to be running."""
        with self._lock:
            self._close_current("ok", detail)

    def note(self, detail: str) -> None:
        """Set the text of the current step (for example the output of a check that failed)."""
        with self._lock:
            if self.steps:
                self.steps[-1]["detail"] = _text(detail, 500)

    def step_failed(self, detail: str = "") -> None:
        """Mark the current step as failed. The job goes on unless the caller finishes it."""
        with self._lock:
            self._close_current("failed", detail)

    def finish(self, ok: bool, detail: str = "") -> None:
        with self._lock:
            if self.state != "running":
                return
            self._close_current("ok" if ok else "failed", None if ok else detail)
            self.state = "ok" if ok else "failed"
            self.detail = _text(detail, 500)
            self.finished = self._clock()

    def _close_current(self, state: str, detail: str | None = None) -> None:
        if self.steps and self.steps[-1]["state"] == "running":
            self.steps[-1]["state"] = state
            if detail:
                self.steps[-1]["detail"] = _text(detail, 500)

    @property
    def done(self) -> bool:
        return self.state != "running"

    def to_dict(self) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "label": self.label, "started_by": self.user, "state": self.state,
                    "detail": self.detail, "started": round(self.started, 3),
                    "finished": round(self.finished, 3) if self.finished else None,
                    "steps": [dict(s) for s in self.steps]}


class Jobs:
    """The most recent jobs. Running jobs are never dropped to make room."""

    def __init__(self, keep: int = KEEP, clock=time.time):
        self.keep, self._clock = keep, clock
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, kind: str, label: str, user: str) -> Job:
        job = Job(secrets.token_hex(ID_LENGTH // 2), kind, label, user, self._clock)
        with self._lock:
            self._jobs[job.id] = job
            for old_id in [i for i, j in self._jobs.items() if j.done][:max(0, len(self._jobs) - self.keep)]:
                del self._jobs[old_id]
        return job

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return job.to_dict() if job else None
