from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import uuid
import time

from PySide6.QtCore import QObject, Signal


@dataclass
class JobRecord:
    name: str
    category: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "Queued"
    completed: int = 0
    total: int = 0
    detail: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    @property
    def percent(self) -> int:
        return int(self.completed * 100 / self.total) if self.total else 0


class JobManager(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[JobRecord] = []
        self._last_update_emit = 0.0

    def create(self, name: str, category: str, total: int = 0) -> JobRecord:
        job = JobRecord(name=name, category=category, total=total, status="Running")
        self.jobs.insert(0, job)
        del self.jobs[200:]
        self.changed.emit()
        return job

    def update(self, job: JobRecord, completed: int, total: int, detail: str = "") -> None:
        job.completed = completed
        job.total = total
        job.detail = detail
        job.status = "Running"
        now = time.monotonic()
        if completed >= total or now - self._last_update_emit >= 0.10:
            self._last_update_emit = now
            self.changed.emit()

    def finish(self, job: JobRecord, status: str = "Completed", detail: str = "") -> None:
        job.status = status
        job.detail = detail or job.detail
        job.finished_at = datetime.now()
        if job.total and status == "Completed":
            job.completed = job.total
        self.changed.emit()
