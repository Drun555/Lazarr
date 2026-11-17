import time
import asyncio
import logging
from datetime import datetime, timezone
from lazarr.models import ConfigEntry, Subtask, SubtaskAsset
from sqlalchemy import select
from lazarr.search import PREFIX, enqueue

log = logging.getLogger(__name__)


class Scheduler:
    """One daily search pass; a started pass has no closing time."""

    def __init__(self, worker, service):
        self.worker, self.service = worker, service
        self.tasks = []
        self.wake = asyncio.Event()
        self.queue_lock = asyncio.Lock()
        self.automatic_task = None
        self.automatic_stop_requested = False

    def settings_changed(self):
        if not self.service.settings().automatic_search_enabled and self.automatic_task:
            self.automatic_stop_requested = True
            self.automatic_task.cancel()
        self.wake.set()

    async def _run_automatic(self, operation, *args, **kwargs):
        if not self.service.settings().automatic_search_enabled:
            return False
        task = self.automatic_task = asyncio.create_task(operation(*args, **kwargs))
        self.automatic_stop_requested = False
        try:
            return await task
        except asyncio.CancelledError:
            if not self.automatic_stop_requested or asyncio.current_task().cancelling():
                raise
            return False
        finally:
            self.automatic_task = None
            self.automatic_stop_requested = False

    async def start(self):
        await self.worker.restore()
        self.tasks = [
            asyncio.create_task(self._search_loop()),
            asyncio.create_task(self._download_loop()),
        ]

    async def search_tick(self, now=None):
        settings = self.service.settings()
        if not settings.automatic_search_enabled:
            return
        local = (now or datetime.now(timezone.utc)).astimezone(settings.timezone)
        with self.service.db.session() as db:
            row = db.get(ConfigEntry, "scheduler.daily")
            previous = row.value if row else {}
            unfinished = previous.get("started") and previous.get("started") != previous.get("completed")
            if unfinished:
                key = previous["started"]
            else:
                if local.strftime("%H:%M") < settings.search_start:
                    return
                key = f"{local.date()}@{settings.search_start}"
                if previous.get("completed") == key:
                    return
            if not row:
                row = ConfigEntry(key="scheduler.daily", value={})
                db.add(row)
            row.value = {**row.value, "started": key}
        if not await self._run_automatic(self._daily_search):
            return
        with self.service.db.session() as db:
            row = db.get(ConfigEntry, "scheduler.daily")
            row.value = {**row.value, "completed": key}

    async def _daily_search(self):
        await self.service.refresh_seasons()
        return await self.worker.run_due()

    def enqueue(self, task_id=None):
        with self.service.db.session() as db:
            key = enqueue(db, task_id, manual=True)
        self.wake.set()
        return key

    def retry_now(self, task_id=None):
        """Reset provider pauses and make matching deferred searches runnable now."""
        self.worker.plugins.reset_content_cooldowns()
        with self.service.db.session() as db:
            entries = db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX)))
            for row in entries:
                if task_id is not None and row.value.get("task_id") != task_id:
                    continue
                if row.value.get("not_before", 0) > 0:
                    row.value = {**row.value, "not_before": 0}
        return self.enqueue(task_id)

    def discard_satisfied(self):
        """Remove queued searches whose episodes already have a download."""
        with self.service.db.session() as db:
            assigned = (
                select(SubtaskAsset.id)
                .where(
                    SubtaskAsset.subtask_id == Subtask.id,
                    SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True),
                )
                .exists()
            )
            outstanding = set(
                db.scalars(
                    select(Subtask.task_id).where(~assigned, Subtask.status.not_in(("done", "removed")))
                )
            )
            for row in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX))):
                task_id = row.value.get("task_id")
                if task_id is None and outstanding or task_id in outstanding:
                    continue
                db.delete(row)

    def snapshot(self, task_id=None):
        automatic_enabled = self.service.settings().automatic_search_enabled
        with self.service.db.session() as db:
            entries = list(db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX))))
            if not automatic_enabled:
                entries = [row for row in entries if row.value.get("manual", False)]
            if task_id is not None:
                entries = [r for r in entries if r.value.get("task_id") in (None, task_id)]
            pending = len(entries)
            next_attempt = min((r.value.get("not_before", 0) for r in entries), default=0)
        if task_id is None:
            result = self.worker.progress.snapshot()
        else:
            from copy import deepcopy
            from lazarr.search import SearchProgress

            result = deepcopy(self.worker.progress.tasks.get(task_id, SearchProgress().snapshot()))
            if result["state"] in {"finished", "error"}:
                entries = [
                    row
                    for row in entries
                    if row.value.get("not_before", 0) > 0
                    or row.value.get("created_at", 0) > result.get("started_at", 0)
                ]
                pending = len(entries)
                next_attempt = min((r.value.get("not_before", 0) for r in entries), default=0)
        if not result.get("providers"):
            result["providers"] = self.worker.plugins.search_status()
        result["pending_requests"] = pending
        result["next_attempt_at"] = next_attempt
        result["automatic_search_enabled"] = automatic_enabled
        if (
            not automatic_enabled
            and not pending
            and not result["running"]
            and result["state"] in {"idle", "queued"}
        ):
            result.update(state="paused", message="Автоматический поиск выключен")
        if pending and next_attempt > time.time() and not result["running"]:
            result.update(state="queued", message="Повтор поиска запланирован после паузы провайдера")
        if pending and not result["running"] and result["state"] not in {"blocked", "error"}:
            if next_attempt <= time.time():
                result.update(state="queued", message="Поиск поставлен в очередь")
        if task_id is not None and pending and not result["running"]:
            if self.worker.engine is None or not self.worker.plugins.available("content"):
                result.update(
                    state="blocked",
                    message="Движок загрузок недоступен"
                    if self.worker.engine is None
                    else "Включите провайдеры контента в настройках",
                )
        return result

    async def process_queue(self):
        async with self.queue_lock:
            with self.service.db.session() as db:
                entries = list(db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX))))
            entries = [row for row in entries if row.value.get("not_before", 0) <= time.time()]
            manual = [row for row in entries if row.value.get("manual", False)]
            if manual:
                # Explicit requests cover only their tasks; unrelated automatic
                # work remains separately cancellable through the settings switch.
                manual_ids = {row.value["task_id"] for row in manual}
                entries = [row for row in entries if None in manual_ids or row.value["task_id"] in manual_ids]
            elif not self.service.settings().automatic_search_enabled:
                return False
            if not entries:
                return False
            if self.worker.engine is None or not self.worker.plugins.available("content"):
                self.worker.progress.record(
                    "blocked",
                    "Включите провайдеры контента в настройках"
                    if self.worker.engine
                    else "libtorrent недоступен",
                    state="blocked",
                    running=False,
                )
                return False
            task_ids = (
                None
                if any(row.value["task_id"] is None for row in entries)
                else [row.value["task_id"] for row in entries]
            )
            provider_filter = (
                {row.value["provider"] for row in entries}
                if all(row.value.get("provider") for row in entries)
                else None
            )
            operation = self.worker.run_due if manual else self._run_automatic
            args = () if manual else (self.worker.run_due,)
            if not await operation(*args, task_ids=task_ids, force=True, provider_filter=provider_filter):
                return False
            with self.service.db.session() as db:
                for entry in entries:
                    current = db.get(ConfigEntry, entry.key)
                    if current and current.value == entry.value:
                        db.delete(current)
            return True

    async def _search_loop(self):
        while True:
            self.wake.clear()
            try:
                processed = await self.process_queue()
                if not processed and not self.snapshot()["pending_requests"]:
                    await self.search_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Search scheduler failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass

    async def _download_loop(self):
        while True:
            try:
                await self.worker.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Download monitor failed")
            await asyncio.sleep(2)

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.worker.engine is not None:
            await asyncio.to_thread(self.worker.engine.close)
