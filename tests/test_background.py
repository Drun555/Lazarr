import asyncio
import threading

import pytest
from fastapi import HTTPException

from lazarr.background import BackgroundTasks


@pytest.mark.parametrize("fail", [False, True])
async def test_metadata_jobs_describe_real_work_and_skip_empty_checks(core, media, season, monkeypatch, fail):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from lazarr.library import LibraryService
    from lazarr.models import Media, Season

    _, database, plugins, service = core
    library = LibraryService(database, plugins, service)
    queue = BackgroundTasks()
    calls = []

    async def get_media(*args):
        active = queue.snapshot(1)["items"][0]
        assert active["state"] == "running"
        assert "Example Show" in active["detail"] and "Карточка" in active["detail"]
        calls.append("media")
        if fail:
            raise ValueError("private provider error")
        return media.model_copy(update={"taxonomy_known": True})

    async def get_season(*args):
        active = queue.snapshot(1)["items"][0]
        assert active["state"] == "running"
        assert active["detail"] == "Example Show · Сезон 1: данные эпизодов"
        calls.append("season")
        if fail:
            raise ValueError("private provider error")
        return season

    @asynccontextmanager
    async def provider(*args):
        yield SimpleNamespace(get_media=get_media, get_season=get_season)

    monkeypatch.setattr(plugins, "available", lambda kind: ["demo"])
    monkeypatch.setattr(plugins, "open", provider)
    try:
        await library.enrich(background=True, observe=queue.observe)
        assert queue.snapshot(1)["items"] == []
        with database.session() as db:
            row = Media(provider="demo", external_id="42", kind="tv", title="Example Show", metadata_json={})
            db.add(row)
            db.flush()
            identity = row.id
            db.add(Season(media_id=identity, number=1, title="Season 1", refreshed_at=0))
        for _ in range(2):
            await library.enrich(background=True, observe=queue.observe)
            await library.enrich_media(identity, background=True, observe=queue.observe)
        rows = queue.snapshot(1)["items"]
        assert calls == ["media", "season"]
        assert len(rows) == 2
        assert all(row["state"] == ("failed" if fail else "completed") for row in rows)
        assert "private" not in str(rows)
    finally:
        await queue.close()


def test_processes_show_waiting_episodes_and_use_scoped_batch_choice(core, media, season, monkeypatch):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.models import Subtask
    from lazarr.services import CreateTask
    from test_api import login

    config, database, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with database.session() as db:
        db.get(Subtask, 1).status = "needs_selection"
        db.get(Subtask, 2).status = "done"
    calls = []

    async def choose(identity, actor, **kwargs):
        calls.append((identity, actor, kwargs))
        return {"selected": 1, "total": 1, "episodes": ["S01E01"], "subtask_ids": [1]}

    with TestClient(create_app(config)) as client:
        assert (
            client.post("/api/v1/subtasks/1/candidates/7/choice-pending", json={"preview": True}).status_code
            == 401
        )
        headers = login(client)
        rows = client.get("/api/v1/background-tasks").json()["selection"]
        assert len(rows) == 1 and rows[0]["id"] == 1 and rows[0]["title"] == media.title
        monkeypatch.setattr(client.app.state.ctx.worker, "choose_all", choose)
        client.app.state.ctx.engine = object()
        result = client.post(
            "/api/v1/subtasks/1/candidates/7/choice-pending", json={"preview": True}, headers=headers
        )
        assert result.status_code == 200
        assert calls[-1][2] == dict(
            pending_only=True, expected_subtask=1, preview=True, allowed_subtasks=None
        )
        assert (
            client.post(
                "/api/v1/subtasks/1/candidates/7/choice-pending", json={"subtask_ids": [1]}, headers=headers
            ).status_code
            == 200
        )
        assert calls[-1][2]["allowed_subtasks"] == [1]
        client.app.state.ctx.engine = None


def test_download_activity_in_tasks_is_compact_and_excludes_finished(core):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.models import Download, Release
    from test_api import login

    with core[1].session() as db:
        release = Release(provider="demo", external_id="activity", revision="1", data={"title": "Release"})
        db.add(release)
        db.flush()
        for index, (state, complete) in enumerate(
            [
                ("starting", False),
                ("downloading", False),
                ("paused", False),
                ("error", False),
                ("seeding", True),
                ("stopped", True),
                ("replaced", False),
                ("paused", True),
            ]
        ):
            db.add(
                Download(
                    infohash=f"{index:040x}",
                    release_id=release.id,
                    save_path="/private",
                    torrent_file="/private.torrent",
                    plan={},
                    state=state,
                    stats={"progress": 0.42, "download_rate": 2048, "eta": 120, "complete": complete},
                )
            )
    with TestClient(create_app(core[0])) as client:
        assert client.get("/api/v1/background-tasks").status_code == 401
        login(client)
        response = client.get("/api/v1/background-tasks")
        assert response.status_code == 200
        rows = response.json()["downloads"]
        assert [row["state"] for row in rows] == ["starting", "downloading", "paused", "error"]
        assert rows[1]["progress"] == 0.42 and rows[1]["download_rate"] == 2048
        assert "private" not in response.text and "infohash" not in response.text
        assert response.json()["items"] == []


async def test_cached_resources_bypass_busy_media_lane_and_invalidate(tmp_path):
    from types import SimpleNamespace
    from lazarr.jellyfin_resources import cached_async

    queue = BackgroundTasks()
    ctx = SimpleNamespace(background_tasks=queue, config=SimpleNamespace(data_dir=tmp_path))
    source = tmp_path / "source"
    source.write_bytes(b"one")
    calls = []

    def generate(path):
        calls.append(1)
        path.write_bytes(source.read_bytes())

    started, release = threading.Event(), threading.Event()

    def busy():
        started.set()
        assert release.wait(5)

    pending = None
    try:
        first = await cached_async(ctx, [source], ["image"], "bin", generate)
        pending = asyncio.create_task(queue.run("trickplay", busy))
        assert await asyncio.to_thread(started.wait, 2)
        warm = await asyncio.wait_for(cached_async(ctx, [source], ["image"], "bin", generate), 1)
        assert warm == first and len(calls) == 1
        release.set()
        await pending
        source.write_bytes(b"changed")
        changed = await cached_async(ctx, [source], ["image"], "bin", generate)
        assert changed != first and changed.read_bytes() == b"changed"
        assert len(calls) == 2
    finally:
        release.set()
        if pending:
            await pending
        await queue.close()


def test_next_up_keeps_health_and_tasks_responsive(core, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from test_api import login
    from test_jellyfin import jellyfin_login

    started, release = threading.Event(), threading.Event()

    def next_up(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"Items": [], "TotalRecordCount": 0}

    monkeypatch.setattr("lazarr.jellyfin_state.next_up", next_up)
    with TestClient(create_app(core[0])) as client:
        assert client.get("/api/v1/background-tasks").status_code == 401
        login(client)
        token = jellyfin_login(client)["AccessToken"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.get, "/Shows/NextUp", headers={"X-Emby-Token": token})
            try:
                assert started.wait(2)
                assert client.get("/health").status_code == 200
                response = client.get("/api/v1/background-tasks")
                assert response.status_code == 200
                assert response.json()["items"][0]["kind"] == "next-up"
                assert response.json()["items"][0]["state"] == "running"
            finally:
                release.set()
            assert future.result().status_code == 200


async def test_queue_is_bounded_observable_and_lanes_are_independent():
    queue = BackgroundTasks(capacity=2)
    started, release = threading.Event(), threading.Event()

    def work():
        started.set()
        assert release.wait(5)
        return threading.current_thread().name

    first = asyncio.create_task(queue.run("trickplay", work))
    second = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await queue.run("next-up", lambda: 42, lane="catalog", owner_id=1) == 42
        second = asyncio.create_task(queue.run("chapter", lambda: 12))
        await asyncio.sleep(0)
        rows = queue.snapshot(1)["items"]
        assert [row["state"] for row in rows[:2]] == ["running", "queued"]
        assert not any(row["kind"] == "next-up" for row in queue.snapshot(2)["items"])
        with pytest.raises(HTTPException) as exc:
            await queue.run("probe", lambda: 0)
        assert exc.value.status_code == 503
    finally:
        release.set()
        assert (await first).startswith("lazarr-media")
        if second:
            assert await second == 12
        await queue.close()
    assert all(row["state"] == "completed" for row in queue.snapshot(1)["items"])


async def test_singleflight_survives_waiter_cancellation_and_errors_are_safe():
    queue = BackgroundTasks()
    started, release = threading.Event(), threading.Event()
    calls = []

    def work():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return 7

    first = asyncio.create_task(queue.run("image", work, key="same"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(queue.run("image", work, key="same"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == 7
        assert len(calls) == 1

        def fail():
            raise ValueError("/private/path?token=secret")

        with pytest.raises(ValueError):
            await queue.run("probe", fail)
        snapshot = queue.snapshot(1)
        assert snapshot["items"][0]["state"] == "failed"
        assert "private" not in str(snapshot) and "secret" not in str(snapshot)
    finally:
        release.set()
        await queue.close()
    with pytest.raises(HTTPException):
        await queue.run("image", lambda: 1)
