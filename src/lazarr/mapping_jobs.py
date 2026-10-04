"""Durable, serial application of complete season-mapping drafts."""

import asyncio
from contextvars import ContextVar
import hashlib
import json
import logging
import time
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from lazarr.models import ConfigEntry, Season, Task

current_job = ContextVar("mapping_job", default=None)
PREFIX = "mapping_job."
log = logging.getLogger(__name__)


class MappingJobs:
    def __init__(self, ctx, apply):
        self.ctx, self.apply = ctx, apply
        self.wake = asyncio.Event()
        self.accept_lock = asyncio.Lock()
        self.closing = False
        self.runner = None

    def entries(self, db):
        return list(db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX))))

    def public(self, job, *, draft=False):
        keys = (
            "id",
            "state",
            "created_at",
            "started_at",
            "finished_at",
            "detail",
            "completed",
            "total",
            "task_id",
            "season_number",
        )
        result = {key: job.get(key) for key in keys}
        result.update(kind="season-mapping", lane="mapping")
        if draft:
            result["payload"] = job["payload"]
        return result

    def enqueue(self, task_id, season_number, payload, user_id):
        identity = str(payload.request_id or uuid4())
        data = payload.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        with self.ctx.db.session() as db:
            existing = db.get(ConfigEntry, PREFIX + identity)
            if existing:
                job = existing.value
                if (job["task_id"], job["season_number"], job["owner_id"], job["digest"]) != (
                    task_id,
                    season_number,
                    user_id,
                    digest,
                ):
                    raise HTTPException(409, "Этот идентификатор сохранения уже использован")
                return self.public(job)
            task = db.get(Task, task_id)
            season = (
                db.scalar(
                    select(Season).where(Season.media_id == task.media_id, Season.number == season_number)
                )
                if task
                else None
            )
            if not season:
                raise HTTPException(404, "Сезон задачи не найден")
            jobs = [entry.value for entry in self.entries(db)]
            if any(job["task_id"] == task_id and job["state"] in {"queued", "running"} for job in jobs):
                raise HTTPException(
                    409, "Сопоставление этой задачи уже применяется. Дождитесь завершения в «Процессах»."
                )
            if sum(job["state"] in {"queued", "running"} for job in jobs) >= 64:
                raise HTTPException(503, "Очередь сопоставлений заполнена")
            job = dict(
                id=identity,
                task_id=task_id,
                task_created_at=task.created_at,
                season_id=season.id,
                season_number=season_number,
                owner_id=user_id,
                payload=data,
                digest=digest,
                state="queued",
                completed=0,
                total=len(payload.rows) + len(set(payload.deleted_subtask_ids)),
                detail="План сохранён. Ожидание применения",
                created_at=time.time(),
                started_at=None,
                finished_at=None,
            )
            db.add(ConfigEntry(key=PREFIX + identity, value=job))
            return self.public(job)

    def snapshot(self):
        with self.ctx.db.session() as db:
            jobs = sorted(
                (entry.value for entry in self.entries(db)), key=lambda j: j["created_at"], reverse=True
            )
            active = [j for j in jobs if j["state"] in {"queued", "running"}]
            history = [j for j in jobs if j["state"] not in {"queued", "running"}][:30]
            return [self.public(j) for j in active + history]

    def latest(self, task_id, season_number):
        with self.ctx.db.session() as db:
            task = db.get(Task, task_id)
            jobs = [
                entry.value
                for entry in self.entries(db)
                if task
                and entry.value["task_id"] == task_id
                and entry.value["task_created_at"] == task.created_at
                and entry.value["season_number"] == season_number
            ]
            return self.public(max(jobs, key=lambda j: j["created_at"]), draft=True) if jobs else None

    def retry(self, identity):
        with self.ctx.db.session() as db:
            entry = db.get(ConfigEntry, PREFIX + identity)
            if not entry:
                raise HTTPException(404, "Сохранение не найдено")
            job = entry.value
            if job["state"] != "failed":
                return self.public(job)
            if any(
                other.value["task_id"] == job["task_id"]
                and other.value["id"] != identity
                and (
                    other.value["created_at"] > job["created_at"]
                    or other.value["state"] in {"queued", "running"}
                )
                for other in self.entries(db)
            ):
                raise HTTPException(409, "Есть более новое сопоставление. Откройте редактор заново.")
            entry.value = {
                **job,
                "state": "queued",
                "finished_at": None,
                "detail": "Повторное применение сохранённого плана",
            }
            return self.public(entry.value)

    def update(self, identity, **values):
        with self.ctx.db.session() as db:
            entry = db.get(ConfigEntry, PREFIX + identity)
            entry.value = {**entry.value, **values}

    async def progress(self, detail, completed=None):
        identity = current_job.get()
        if identity:
            values = {"detail": detail}
            if completed is not None:
                values["completed"] = completed
            await asyncio.to_thread(self.update, identity, **values)

    def persist_payload(self, db, payload):
        """Commit newly assigned subtask IDs atomically with the new episodes."""
        identity = current_job.get()
        if identity:
            entry = db.get(ConfigEntry, PREFIX + identity)
            entry.value = {**entry.value, "payload": payload.model_dump(mode="json")}

    async def start(self):
        def recover():
            with self.ctx.db.session() as db:
                for entry in self.entries(db):
                    if entry.value["state"] == "running":
                        entry.value = {
                            **entry.value,
                            "state": "queued",
                            "detail": "Возобновление после перезапуска",
                        }

        await asyncio.to_thread(recover)
        self.runner = asyncio.create_task(self.run())

    def next_job(self):
        with self.ctx.db.session() as db:
            jobs = [entry.value for entry in self.entries(db) if entry.value["state"] == "queued"]
            return min(jobs, key=lambda j: j["created_at"]) if jobs else None

    async def run(self):
        while not self.closing:
            self.wake.clear()
            job = await asyncio.to_thread(self.next_job)
            if job is None:
                await self.wake.wait()
                continue
            token = current_job.set(job["id"])
            try:
                with self.ctx.db.session() as db:
                    task, season = db.get(Task, job["task_id"]), db.get(Season, job["season_id"])
                    if (
                        not task
                        or task.created_at != job["task_created_at"]
                        or not season
                        or season.number != job["season_number"]
                    ):
                        raise ValueError("Структура задачи изменилась; откройте сопоставление заново")
                await asyncio.to_thread(
                    self.update, job["id"], state="running", started_at=time.time(), finished_at=None
                )
                await self.apply(job)
                await asyncio.to_thread(
                    self.update,
                    job["id"],
                    state="completed",
                    finished_at=time.time(),
                    completed=job["total"],
                    detail="Сопоставление применено",
                )
            except asyncio.CancelledError:
                raise  # A restart resumes the durable plan, not an empty draft.
            except Exception as error:
                log.exception("Mapping job %s failed", job["id"])
                detail = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Не удалось применить сопоставление. Повторите сохранённый план."
                )
                await asyncio.to_thread(
                    self.update, job["id"], state="failed", finished_at=time.time(), detail=detail[:1000]
                )
            finally:
                current_job.reset(token)

    async def close(self):
        self.closing = True
        self.wake.set()
        if self.runner:
            await self.runner
