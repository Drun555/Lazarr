import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.models import ConfigEntry, Subtask
from lazarr.scheduler import Scheduler
from lazarr.search import PREFIX, defer
from lazarr.services import CreateTask
from test_api import login
from test_worker import worker_setup as worker_setup


def automatic(service, enabled):
    settings = service.settings()
    settings.automatic_search_enabled = enabled
    service.set_settings(settings, 1)


def add(service, media, season):
    return service.create_from_metadata(
        CreateTask(media_id=media.id, kind="tv", season=1, episodes=[1]), media, season, 1
    )


async def test_disabled_daily_pass_does_not_start_or_resume(core):
    _, db, _, service = core
    automatic(service, False)
    worker = AsyncMock()
    service.refresh_seasons = AsyncMock()
    with db.session() as session:
        session.add(ConfigEntry(key="scheduler.daily", value={"started": "2026-09-11@00:00"}))
    await Scheduler(worker, service).search_tick(datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
    worker.run_due.assert_not_awaited()
    service.refresh_seasons.assert_not_awaited()
    with db.session() as session:
        assert "completed" not in session.get(ConfigEntry, "scheduler.daily").value


async def test_disabled_automatic_requests_survive_restart_and_resume_only_when_enabled(
    core, media, season, worker_setup
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    automatic(service, False)
    task_id = add(service, media, season)
    service.edit(task_id, 1, paused=True)
    service.edit(task_id, 1, paused=False)
    with db.session() as session:
        defer(session, [task_id], "demo", time.time() - 1)
    scheduler = Scheduler(worker, service)
    assert not await scheduler.process_queue()
    assert not await Scheduler(worker, service).process_queue()
    assert scheduler.snapshot()["pending_requests"] == 0
    assert scheduler.snapshot()["state"] == "paused"
    assert not demo.calls and not engine.plans
    automatic(service, True)
    assert await scheduler.process_queue()
    assert demo.calls and engine.plans


async def test_manual_request_while_disabled_searches_only_requested_task(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    automatic(service, False)
    wanted = add(service, media, season)
    other = add(service, media.model_copy(update={"id": "43"}), season)
    scheduler = Scheduler(worker, service)
    scheduler.retry_now(wanted)
    assert scheduler.snapshot()["pending_requests"] == 1
    assert await scheduler.process_queue()
    assert engine.plans
    with db.session() as session:
        assert session.scalar(select(Subtask).where(Subtask.task_id == other)).status == "queued"
    assert not await scheduler.process_queue()
    assert scheduler.snapshot()["pending_requests"] == 0


async def test_manual_search_does_not_automatically_retry_after_provider_pause(
    core, media, season, worker_setup, monkeypatch
):
    from lazarr.sdk import ProviderError

    _, db, _, service = core
    worker, _, demo = worker_setup
    automatic(service, False)
    task_id = add(service, media, season)
    scheduler = Scheduler(worker, service)

    async def unavailable(self, *args, **kwargs):
        raise ProviderError("unavailable", "temporarily unavailable", 30)

    monkeypatch.setattr(demo, "search", unavailable)
    scheduler.retry_now(task_id)
    assert await scheduler.process_queue()
    with db.session() as session:
        retries = list(
            session.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX + "retry.")))
        )
        assert retries
        for row in retries:
            row.value = {**row.value, "not_before": 0}
    assert not await Scheduler(worker, service).process_queue()
    assert scheduler.snapshot()["pending_requests"] == 0


@pytest.mark.parametrize("source", ["daily", "queue", "manual"])
async def test_turning_off_cancels_automatic_search_but_preserves_manual_search(
    core, media, season, worker_setup, monkeypatch, source
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    task_id = add(service, media, season)
    service.refresh_seasons = AsyncMock()
    scheduler = Scheduler(worker, service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = demo.search

    async def slow(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(demo, "search", slow)
    if source == "manual":
        scheduler.enqueue(task_id)
    operation = scheduler.search_tick if source == "daily" else scheduler.process_queue
    running = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        automatic(service, False)
        scheduler.settings_changed()
        if source == "manual":
            assert not running.done()
            release.set()
        await asyncio.wait_for(running, 3)
        assert bool(engine.plans) == (source == "manual")
        assert not worker.progress.snapshot()["running"]
        with db.session() as session:
            assert all(sub.lease_until == 0 for sub in session.scalars(select(Subtask)))
        if source != "manual":
            assert scheduler.snapshot()["state"] == "paused"
            assert not await scheduler.process_queue()
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_settings_api_persists_switch_and_wakes_scheduler(core, media, season, monkeypatch):
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx
        assert client.get("/api/v1/settings").json()["automatic_search_enabled"] is True
        changed = []
        original = ctx.scheduler.settings_changed

        def settings_changed():
            changed.append(True)
            original()

        monkeypatch.setattr(ctx.scheduler, "settings_changed", settings_changed)
        settings = client.get("/api/v1/settings").json()
        settings["automatic_search_enabled"] = False
        assert client.put("/api/v1/settings", json=settings).status_code == 200
        assert client.get("/api/v1/settings").json()["automatic_search_enabled"] is False
        assert changed == [True]
        assert ctx.scheduler.snapshot()["automatic_search_enabled"] is False
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", AsyncMock(return_value=media))
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", AsyncMock(return_value=season))
        created = client.post("/api/v1/tasks", json={"media_id": media.id, "kind": "tv", "season": 1})
        assert created.status_code == 201, created.text
        assert created.json()["search_queued"] is False
        assert client.portal.call(ctx.scheduler.process_queue) is False
        assert ctx.scheduler.snapshot()["pending_requests"] == 0
        settings["automatic_search_enabled"] = True
        assert client.put("/api/v1/settings", json=settings).status_code == 200
        assert ctx.scheduler.snapshot()["pending_requests"] == 1
