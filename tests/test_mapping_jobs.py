import asyncio
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from lazarr.app import create_app
from lazarr.models import ConfigEntry, Episode
from lazarr.services import CreateTask
from test_api import login


def wait_job(client, identity, state):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        jobs = client.get("/api/v1/background-tasks").json()["items"]
        job = next((job for job in jobs if job["id"] == identity), None)
        if job and job["state"] == state:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Expected {state}, got {job}")


def test_durable_acceptance_is_fast_idempotent_and_observable(core, media, season, monkeypatch):
    config, db, _, service = core
    season = season.model_copy(update={"episodes": season.episodes[:2]})
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    release = threading.Event()
    with TestClient(create_app(config)) as client:
        login(client)
        jobs = client.app.state.ctx.mapping_jobs
        apply = jobs.apply

        async def blocked(job):
            await jobs.progress("Ожидание диска", 0)
            while not release.is_set():
                await asyncio.sleep(0.01)
            await apply(job)

        monkeypatch.setattr(jobs, "apply", blocked)
        path = "/api/v1/tasks/1/seasons/1/mapping"
        payload = {
            "background": True,
            "request_id": str(uuid4()),
            "rows": [
                {"subtask_id": 1, "number": 1, "title": "Part 1"},
                {"subtask_id": -1, "number": 2, "title": "Part 2"},
                {"subtask_id": 2, "number": 3, "title": "Next"},
            ],
        }
        try:
            response = client.put(path, json=payload)
            assert response.status_code == 202, response.text
            identity = response.json()["job"]["id"]
            assert not release.is_set()
            assert client.put(path, json=payload).json()["job"]["id"] == identity
            assert client.put(path, json={**payload, "request_id": str(uuid4())}).status_code == 409
            wait_job(client, identity, "running")
            assert client.get("/health").json() == {"ok": True}
            snapshot = client.get(path).json()
            assert snapshot["job"]["payload"]["rows"] == [
                {
                    **row,
                    "special_position": None,
                    "release_id": None,
                    "video_index": None,
                    "track_indices": [],
                }
                for row in payload["rows"]
            ]
            with db.session() as session:
                assert (
                    session.get(ConfigEntry, "mapping_job." + identity).value["payload"]["rows"][1][
                        "subtask_id"
                    ]
                    == -1
                )
                assert session.scalar(select(func.count()).select_from(Episode)) == 2
        finally:
            release.set()
        job = wait_job(client, identity, "completed")
        assert job["completed"] == job["total"] == 3
        assert [row["title"] for row in client.get(path).json()["episodes"]] == ["Part 1", "Part 2", "Next"]
        # A lost acceptance response must not schedule the same draft twice.
        assert client.put(path, json=payload).json()["job"]["state"] == "completed"
        assert len(client.get(path).json()["episodes"]) == 3


def test_failure_and_restart_resume_saved_plan_without_duplicate_episodes(core, media, season, monkeypatch):
    config, db, _, service = core
    season = season.model_copy(update={"episodes": season.episodes[:2]})
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    path = "/api/v1/tasks/1/seasons/1/mapping"
    from lazarr import storage

    original = storage.reconcile

    def fail(_):
        raise ValueError("Диск временно недоступен")

    with TestClient(create_app(config)) as client:
        login(client)
        monkeypatch.setattr(storage, "reconcile", fail)
        payload = {
            "background": True,
            "request_id": str(uuid4()),
            "rows": [{"subtask_id": -1, "number": 3, "title": "New"}],
        }
        identity = client.put(path, json=payload).json()["job"]["id"]
        failure = wait_job(client, identity, "failed")
        assert "Диск временно недоступен" in failure["detail"]
        with db.session() as session:
            saved = session.get(ConfigEntry, "mapping_job." + identity)
            assert saved.value["payload"]["rows"][0]["subtask_id"] > 0
            assert session.scalar(select(func.count()).select_from(Episode)) == 3
        monkeypatch.setattr(storage, "reconcile", original)
        assert client.post(f"/api/v1/mapping-jobs/{identity}/retry").status_code == 200
        wait_job(client, identity, "completed")
    # Simulate a process interrupted after writing the structure but before its
    # final completion checkpoint. Startup replays the durable, normalized IDs.
    with db.session() as session:
        saved = session.get(ConfigEntry, "mapping_job." + identity)
        saved.value = {**saved.value, "state": "running"}
    with TestClient(create_app(config)) as client:
        login(client)
        wait_job(client, identity, "completed")
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(Episode)) == 3


def test_invalid_draft_fails_before_creating_series(core, media, season):
    config, db, _, service = core
    season = season.model_copy(update={"episodes": season.episodes[:2]})
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        response = client.put(
            "/api/v1/tasks/1/seasons/1/mapping",
            json={"background": True, "rows": [{"subtask_id": -1, "number": 2, "title": "Conflict"}]},
        )
        assert response.status_code == 202
        wait_job(client, response.json()["job"]["id"], "failed")
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(Episode)) == 2
