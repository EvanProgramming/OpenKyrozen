"""Durable local scheduler used by the OpenKyrozen Gateway."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from event_store import EventStore, utc_now


class JobScheduler:
    """Polls SQLite and claims due jobs exactly once per process tick."""

    def __init__(self, store: EventStore, *, workspace_id: str = "default", user_id: str = "local",
                 poll_seconds: float = 2.0):
        self.store = store
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.poll_seconds = max(0.25, poll_seconds)
        self._callbacks: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def register_callback(self, job_type: str, callback: Callable[[dict[str, Any]], Any]) -> None:
        self._callbacks[job_type] = callback

    def schedule_every(self, name: str, interval_seconds: float, *, payload: dict[str, Any] | None = None,
                       job_id: str | None = None, delay_seconds: float | None = None) -> str:
        interval_seconds = max(1.0, float(interval_seconds))
        job_id = job_id or f"job_{uuid.uuid4().hex}"
        next_run = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds if delay_seconds is not None else interval_seconds)
        return self.store.upsert_schedule(job_id, name, interval_seconds=interval_seconds,
                                           next_run_at=next_run.isoformat(), payload=payload,
                                           workspace_id=self.workspace_id, user_id=self.user_id)

    def schedule_once(self, name: str, run_at: str, *, payload: dict[str, Any] | None = None,
                      job_id: str | None = None) -> str:
        datetime.fromisoformat(run_at)
        job_id = job_id or f"job_{uuid.uuid4().hex}"
        return self.store.upsert_schedule(job_id, name, run_at=run_at, next_run_at=run_at,
                                           payload=payload, workspace_id=self.workspace_id, user_id=self.user_id)

    def list_jobs(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        return self.store.list_schedules(workspace_id=self.workspace_id, enabled=enabled, user_id=self.user_id)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="openkyrozen-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def tick(self) -> int:
        now = utc_now()
        jobs = self.store.claim_due_schedules(now=now, workspace_id=self.workspace_id, user_id=self.user_id)
        for job in jobs:
            job_type = str(job.get("payload", {}).get("type", "default"))
            callback = self._callbacks.get(job_type)
            if callback is None:
                self.store.append_event("scheduler.unhandled", {"job_id": job["id"], "type": job_type},
                                        user_id=self.user_id, workspace_id=self.workspace_id)
                continue
            try:
                callback(job)
                self.store.append_event("scheduler.completed", {"job_id": job["id"], "type": job_type},
                                        user_id=self.user_id, workspace_id=self.workspace_id)
            except Exception as exc:
                self.store.append_event("scheduler.failed", {"job_id": job["id"], "type": job_type, "error": str(exc)[:1000]},
                                        user_id=self.user_id, workspace_id=self.workspace_id)
        return len(jobs)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            self.tick()
