import asyncio
import threading

import pytest
from fastapi import HTTPException

from lazarr.background import BackgroundTasks


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
