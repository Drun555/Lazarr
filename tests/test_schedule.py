from datetime import datetime, timezone
from unittest.mock import AsyncMock
from lazarr.scheduler import Scheduler
from lazarr.models import ConfigEntry


async def test_daily_pass_once_restart_and_next_day(core, monkeypatch):
    _, db, _, service = core
    monkeypatch.setenv("TZ", "Europe/Saratov")
    settings = service.settings()
    settings.search_start = "10:00"
    service.set_settings(settings, 1)
    service.refresh_seasons = AsyncMock()
    worker = AsyncMock()
    worker.run_due.return_value = True
    scheduler = Scheduler(worker, service)

    def now(day, hour):
        return datetime(2026, 9, day, hour, tzinfo=timezone.utc)

    await scheduler.search_tick(now(12, 5))
    worker.run_due.assert_not_awaited()
    await scheduler.search_tick(now(12, 6))
    await Scheduler(worker, service).search_tick(now(12, 19))
    assert worker.run_due.await_count == 1
    await scheduler.search_tick(now(13, 6))
    assert worker.run_due.await_count == 2
    with db.session() as session:
        assert session.get(ConfigEntry, "scheduler.daily").value["completed"] == "2026-09-13@10:00"


async def test_busy_worker_does_not_complete_pass(core, monkeypatch):
    _, _, _, service = core
    monkeypatch.setenv("TZ", "UTC")
    service.refresh_seasons = AsyncMock()
    worker = AsyncMock()
    worker.run_due.side_effect = [False, RuntimeError("interrupted"), True]
    scheduler = Scheduler(worker, service)
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    await scheduler.search_tick(now)
    import pytest

    with pytest.raises(RuntimeError):
        await scheduler.search_tick(now)
    await Scheduler(worker, service).search_tick(now)
    assert worker.run_due.await_count == 3


async def test_unfinished_pass_resumes_before_next_daily_start(core, monkeypatch):
    _, db, _, service = core
    monkeypatch.setenv("TZ", "UTC")
    settings = service.settings()
    settings.search_start = "22:00"
    service.set_settings(settings, 1)
    with db.session() as session:
        session.add(ConfigEntry(key="scheduler.daily", value={"started": "2026-09-11@22:00"}))
    worker = AsyncMock()
    worker.run_due.return_value = True
    service.refresh_seasons = AsyncMock()
    await Scheduler(worker, service).search_tick(datetime(2026, 9, 12, 2, tzinfo=timezone.utc))
    worker.run_due.assert_awaited_once()
    with db.session() as session:
        assert session.get(ConfigEntry, "scheduler.daily").value["completed"] == "2026-09-11@22:00"


def test_moving_daily_start_resets_previous_due_time(core, media, season):
    from sqlalchemy import select
    from lazarr.models import Subtask
    from lazarr.services import CreateTask

    _, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with db.session() as session:
        for sub in session.scalars(select(Subtask)):
            sub.next_search_at = 2**40
    settings = service.settings()
    settings.search_start = "09:00"
    service.set_settings(settings, 1)
    with db.session() as session:
        assert all(s.next_search_at == 0 for s in session.scalars(select(Subtask)))
