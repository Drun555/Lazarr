import asyncio
from sqlalchemy import select, delete
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.models import ConfigEntry, Subtask, Task
from lazarr.scheduler import Scheduler
from lazarr.search import PREFIX
from lazarr.services import CreateTask
from test_worker import worker_setup as worker_setup
from test_api import login


def add(service, media, season, episodes=(1,)):
    return service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=list(episodes)), media, season, 1
    )


async def test_new_task_searches_immediately_and_only_it(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, demo = worker_setup
    old = add(service, media, season, (2,))
    with db.session() as session:
        session.execute(delete(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX)))
    new = add(service, media, season)
    settings = service.settings()
    settings.search_start = "23:59"
    service.set_settings(settings, 1)
    scheduler = Scheduler(worker, service)
    assert scheduler.snapshot()["pending_requests"] == 1
    assert await scheduler.process_queue()
    assert demo.calls == 1
    with db.session() as session:
        assert session.scalar(select(Subtask).where(Subtask.task_id == new)).status == "starting"
        assert session.scalar(select(Subtask).where(Subtask.task_id == old)).status == "queued"
    assert scheduler.snapshot()["pending_requests"] == 0
    progress = scheduler.snapshot()
    assert progress["groups_done"] == progress["groups_total"] == 1
    stages = {event["stage"] for event in progress["history"]}
    assert {
        "search",
        "results",
        "inspect",
        "resolve",
        "metadata",
        "matching",
        "download",
        "finished",
    } <= stages


async def test_queue_survives_restart_and_deduplicates_manual_requests(core, media, season, worker_setup):
    _, _, _, service = core
    worker, _, demo = worker_setup
    add(service, media, season)
    scheduler = Scheduler(worker, service)
    first = scheduler.enqueue()
    assert scheduler.enqueue() == first
    restarted = Scheduler(worker, service)
    assert restarted.snapshot()["pending_requests"] == 2
    assert await restarted.process_queue()
    assert demo.calls == 1
    assert not await restarted.process_queue()


async def test_new_task_can_arrive_during_search(core, media, season, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup
    add(service, media, season)
    scheduler = Scheduler(worker, service)
    entered, release = asyncio.Event(), asyncio.Event()
    original = demo.search

    async def slow(self, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(demo, "search", slow)
    running = asyncio.create_task(scheduler.process_queue())
    await asyncio.wait_for(entered.wait(), 2)
    snapshot = scheduler.snapshot()
    assert snapshot["running"] and snapshot["stage"] == "search"
    assert snapshot["provider"] == "Demo"
    add(service, media, season, (2,))
    release.set()
    await running
    assert scheduler.snapshot()["pending_requests"] == 1
    await scheduler.process_queue()
    assert demo.calls == 2


async def test_manual_queue_preserves_pause_and_calendar(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, demo = worker_setup
    paused = add(service, media, season)
    future = add(service, media, season, (2,))
    service.edit(paused, 1, paused=True)
    with db.session() as session:
        from lazarr.models import Episode

        sub = session.scalar(select(Subtask).where(Subtask.task_id == future))
        session.get(Episode, sub.episode_id).air_date = "2999-01-01"
    scheduler = Scheduler(worker, service)
    scheduler.enqueue()
    await scheduler.process_queue()
    assert demo.calls == 0
    with db.session() as session:
        assert session.get(Task, paused).paused
        assert session.scalar(select(Subtask).where(Subtask.task_id == future)).status == "waiting_release"


async def test_blocked_queue_stays_pending(core, media, season, worker_setup):
    _, _, plugins, service = core
    worker, _, _ = worker_setup
    add(service, media, season)
    plugins.configure("demo", {}, False)
    scheduler = Scheduler(worker, service)
    assert not await scheduler.process_queue()
    assert scheduler.snapshot()["state"] == "blocked"
    assert scheduler.snapshot()["pending_requests"] == 1
    plugins.configure("demo", {}, True)
    assert await scheduler.process_queue()


def test_run_queue_api_auth_csrf_and_create_without_waiting_for_worker(core, media, season, monkeypatch):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        assert client.post("/api/v1/search/run").status_code == 401
        login(client)
        ctx = client.app.state.ctx
        assert client.post("/api/v1/search/run", headers={"x-csrf-token": "wrong"}).status_code == 403
        assert client.post("/api/v1/search/run").status_code == 422
        ctx.plugins.configure("nyaa", {}, True)
        assert client.post("/api/v1/search/run").status_code == 202
        assert client.post("/api/v1/search/run").status_code == 202

        async def get_media(*args):
            return media

        async def get_season(*args):
            return season

        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", get_media)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", get_season)
        # A creation request must not wait for the long-running worker lock.
        client.portal.call(ctx.worker.lock.acquire)
        try:
            result = client.post("/api/v1/tasks", json={"media_id": "42", "kind": "tv", "season": 1})
            assert result.status_code == 201 and result.json()["search_queued"]
        finally:
            ctx.worker.lock.release()
        assert client.get("/api/v1/status").json()["search"]["pending_requests"] == 2


async def test_failing_provider_does_not_skip_next_enabled_provider(
    core, media, season, worker_setup, monkeypatch
):
    from lazarr.sdk import ProviderManifest, SearchPage, ProviderError
    from lazarr.models import ProviderConfig

    _, db, plugins, service = core
    worker, _, demo = worker_setup
    calls = []

    async def failed(self, *args):
        calls.append("demo")
        raise ProviderError("unavailable", "Provider HTTP 504", 60)

    monkeypatch.setattr(demo, "search", failed)

    class Second(demo):
        manifest = ProviderManifest(id="second", name="Second", kind="content", version="1.0.0")

        async def search(self, *args):
            calls.append("second")
            return SearchPage(items=[])

    plugins.classes["second"] = Second
    with db.session() as session:
        session.add(ProviderConfig(id="second", enabled=True))
    add(service, media, season)
    await worker.run_due()
    assert calls == ["demo", "second"]
    progress = worker.progress.snapshot()
    providers = {p["id"]: p for p in progress["providers"]}
    assert providers["demo"]["state"] == "error"
    assert "504" in providers["demo"]["reason"]
    assert providers["second"]["state"] == "completed"
    assert providers["second"]["requests"] == 1
    assert providers["rutracker"]["state"] == "disabled"
    assert progress["state"] == "finished" and progress["errors"] == 1


async def test_cooldown_and_disabled_provider_are_visible_without_claiming_success(
    core, media, season, worker_setup, monkeypatch
):
    import time
    from lazarr.models import ProviderConfig

    _, db, plugins, service = core
    worker, _, demo = worker_setup

    async def forbidden(*args):
        raise AssertionError("Cooldown must not issue a provider request")

    monkeypatch.setattr(demo, "search", forbidden)
    with db.session() as session:
        row = session.get(ProviderConfig, "demo")
        row.retry_at = time.time() + 60
        row.last_error = "unavailable: Provider HTTP 504"
    add(service, media, season)
    await worker.run_due()
    progress = worker.progress.snapshot()
    assert progress["state"] == "error"
    assert progress["search_requests"] == 0
    assert "не выполнен" in progress["message"]
    providers = {p["id"]: p for p in progress["providers"]}
    assert providers["demo"]["state"] == "cooldown"
    assert "504" in providers["demo"]["reason"]
    assert providers["rutracker"]["state"] == "disabled"
    assert not any(e["stage"] == "search" for e in progress["history"])
    assert any(e["stage"] == "provider_skipped" and "Rutracker" in e["message"] for e in progress["history"])
    from lazarr.sdk import ProviderError
    import pytest

    with pytest.raises(ProviderError, match="504"):
        async with plugins.open("demo"):
            pass


async def test_worker_uses_configured_provider_order(core, media, season, worker_setup, monkeypatch):
    from lazarr.sdk import ProviderManifest, SearchPage
    from lazarr.models import ProviderConfig

    _, db, plugins, service = core
    worker, _, demo = worker_setup
    calls = []

    async def search(self, *args):
        calls.append(self.manifest.id)
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", search)

    class Second(demo):
        manifest = ProviderManifest(id="second", name="Second", kind="content", version="1.0.0")

    plugins.classes["second"] = Second
    with db.session() as session:
        session.add(ProviderConfig(id="second", enabled=True))
    plugins.set_content_order(["second", "demo"])
    add(service, media, season)
    await worker.run_due()
    assert calls == ["second", "demo"]


def test_provider_order_api_permissions_validation_and_persistence(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        assert client.put("/api/v1/providers/order", json={"ids": []}).status_code == 401
        login(client)
        for identity in ["nyaa", "rutracker"]:
            assert client.put(f"/api/v1/providers/{identity}", json={"enabled": True}).status_code == 200
        assert client.put("/api/v1/providers/order", json={"ids": ["nyaa"]}).status_code == 422
        payload = {"ids": ["rutracker", "nyaa"]}
        assert (
            client.put("/api/v1/providers/order", json=payload, headers={"x-csrf-token": "wrong"}).status_code
            == 403
        )
        assert client.put("/api/v1/providers/order", json=payload).status_code == 200
        assert client.get("/api/v1/status").json()["content_providers"] == payload["ids"]
