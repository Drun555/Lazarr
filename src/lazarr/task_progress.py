"""Small, atomic progress records shared by the server and standalone CLI jobs."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
from pathlib import Path
import time
import uuid

task_directory = ContextVar("background_task_directory", default=None)
PUBLIC_FIELDS = (
    "id",
    "kind",
    "lane",
    "state",
    "created_at",
    "started_at",
    "updated_at",
    "finished_at",
    "detail",
    "phase",
    "progress",
    "completed_bytes",
    "total_bytes",
    "scanned",
    "indexed",
)


class TaskProgress:
    def __init__(self, path, kind, detail, total_bytes):
        self.path = path
        self.last_write = 0.0
        now = time.time()
        self.value = dict(
            schema=1,
            id=uuid.uuid4().hex,
            kind=kind,
            lane="catalog",
            state="running",
            created_at=now,
            started_at=now,
            updated_at=now,
            finished_at=None,
            detail=detail,
            phase="Подготовка",
            progress=0.0,
            completed_bytes=0,
            total_bytes=total_bytes,
            scanned=0,
            indexed=0,
        )

    def update(self, *, force=False, **values):
        previous = self.value["completed_bytes"]
        self.value.update(values)
        self.value["completed_bytes"] = min(
            self.value["total_bytes"], max(previous, self.value["completed_bytes"])
        )
        total = max(1, self.value["total_bytes"])
        self.value["progress"] = (
            1.0
            if self.value["state"] == "completed"
            else max(0.0, min(0.999, self.value["completed_bytes"] / total))
        )
        now = time.monotonic()
        if not force and now - self.last_write < 1:
            return
        self.value["updated_at"] = time.time()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)
        self.last_write = now


@contextmanager
def registered_task(directory, key, *, kind, detail, total_bytes):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(str(key).encode()).hexdigest()
    path = directory / f"{identity}.json"
    # The held lock is also a cross-process liveness signal: a killed CLI cannot
    # leave a task displayed as running forever. No PID namespace assumptions.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        task = TaskProgress(path, kind, detail, total_bytes)
        task.update(force=True)
        try:
            yield task
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
            task.update(
                force=True,
                state="cancelled" if cancelled else "failed",
                finished_at=time.time(),
                phase="Остановлено" if cancelled else "Ошибка подготовки каталога",
            )
            raise
        else:
            task.update(force=True, state="completed", phase="Готово", finished_at=time.time())


def _read(path):
    if path.stat().st_size > 65536:
        raise ValueError("Oversized progress record")
    row = json.loads(path.read_text(encoding="utf-8"))
    if row.get("schema") != 1 or not isinstance(row.get("updated_at"), (int, float)):
        raise ValueError("Invalid progress record")
    return row


def persisted_tasks(directory):
    rows = []
    for path in Path(directory).glob("*.json"):
        try:
            row = _read(path)
            if row["state"] == "running":
                try:
                    with path.with_suffix(".lock").open("r") as lock:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            row = _read(path)  # The writer may just have completed.
                            if row["state"] == "running":
                                row.update(
                                    state="interrupted",
                                    phase="Индексация прервана",
                                    finished_at=row["updated_at"],
                                )
                except FileNotFoundError:
                    row.update(
                        state="interrupted", phase="Индексация прервана", finished_at=row["updated_at"]
                    )
            rows.append({key: row[key] for key in PUBLIC_FIELDS})
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return [r for r in rows if r["state"] == "running"] + [r for r in rows if r["state"] != "running"][:30]
