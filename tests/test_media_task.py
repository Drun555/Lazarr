import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, func, text
from sqlalchemy.exc import IntegrityError

from lazarr.app import create_app
from lazarr.config import Requirements
from lazarr.models import Task, TaskSeason, Subtask, SubtaskAsset, CandidateDecision, Download, AuditEvent
from lazarr.scheduler import Scheduler
from lazarr.services import CreateTask
from test_api import login
from test_task_seasons import season_info
from test_worker import worker_setup as worker_setup


def test_add_season_api_reuses_task_preserves_requirements_and_checks_access(core, media, monkeypatch):
    config, db, _, service = core
    task_id = service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            requirements=Requirements(audio_languages=["ja"], subtitle_languages=["ru"]),
        ),
        media,
        season_info(1),
        1,
    )
    task = service.list_tasks()[0]
    with TestClient(create_app(config)) as client:
        url = f"/api/v1/libraries/media/{task['media_id']}/seasons"
        assert client.post(url, json={"season": 2}).status_code == 401
        login(client)
        assert client.post(url, json={"season": 2}, headers={"x-csrf-token": "wrong"}).status_code == 403

        async def get_media(*args):
            return media

        async def get_season(self, external_id, number):
            return season_info(number)

        provider = client.app.state.ctx.plugins.classes["tmdb"]
        monkeypatch.setattr(provider, "get_media", get_media)
        monkeypatch.setattr(provider, "get_season", get_season)
        for _ in range(2):
            response = client.post(url, json={"season": 2})
            assert response.status_code == 200 and response.json()["id"] == task_id
        result = client.get("/api/v1/tasks").json()
        assert len(result) == 1 and len(result[0]["subtasks"]) == 4
        assert result[0]["requirements"] == task["requirements"]
        client.app.state.ctx.library.enrich_media = lambda *_: asyncio.sleep(0)
        detail = client.get(f"/api/v1/libraries/media/{task['media_id']}").json()
        assert detail["task"]["id"] == task_id and "search" in detail
        assert client.post(f"/api/v1/tasks/{task_id}/search").status_code == 202
        assert client.post("/api/v1/tasks/999/search").status_code == 404
        assert client.patch(f"/api/v1/tasks/{task_id}", json={"paused": True}).status_code == 200
        assert client.post(f"/api/v1/tasks/{task_id}/search").status_code == 422
    with pytest.raises(IntegrityError), db.session() as session:
        session.add(
            Task(media_id=task["media_id"], created_by=1, updated_by=1, requirements=task["requirements"])
        )


async def test_task_progress_is_scoped_and_keeps_all_season_groups(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, _, demo = worker_setup
    first = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    second = service.create_from_metadata(
        CreateTask(media_id="43", kind="tv", seasons=[{"season": 1}, {"season": 2}]),
        media.model_copy(update={"id": "43"}),
        [season_info(1), season_info(2)],
        1,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = demo.search

    async def slow(self, query, cursor=None):
        if query.media.id == "42":
            entered.set()
            await release.wait()
        return await original(self, query, cursor)

    monkeypatch.setattr(demo, "search", slow)
    scheduler = Scheduler(worker, service)
    running = asyncio.create_task(worker.run_due(force=True))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        one, two = scheduler.snapshot(first), scheduler.snapshot(second)
        assert one["running"] and one["stage"] == "search"
        assert not two["running"] and two["history"] == []
        assert two["groups_total"] == 2
    finally:
        release.set()
        await running
    one, two = scheduler.snapshot(first), scheduler.snapshot(second)
    assert one["groups_done"] == one["groups_total"] == 1
    assert two["groups_done"] == two["groups_total"] == 2
    assert one["candidates_checked"] < scheduler.snapshot()["candidates_checked"]
    assert not one["running"] and not two["running"]


async def test_merge_migration_preserves_download_links_and_remaps_ids(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    first = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "src/lazarr/migrations"))
    config.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(config, "0009")
    with db.session() as session:
        old = session.get(Task, first)
        newer = Task(
            media_id=old.media_id,
            created_by=2,
            updated_by=2,
            requirements=Requirements(max_resolution=2160).model_dump(),
            updated_at=old.updated_at + 1,
        )
        session.add(newer)
        session.flush()
        membership = session.scalar(select(TaskSeason))
        session.add(
            TaskSeason(
                task_id=newer.id,
                season_id=membership.season_id,
                selection_key=membership.selection_key,
                whole_season=True,
            )
        )
        sub = session.scalar(select(Subtask))
        newsub = Subtask(task_id=newer.id, episode_id=sub.episode_id, part_key=sub.part_key)
        session.add(newsub)
        session.flush()
        link = session.scalar(select(SubtaskAsset))
        session.add(
            SubtaskAsset(
                subtask_id=newsub.id,
                asset_id=link.asset_id,
                pending=True,
                preflight=deepcopy(link.preflight),
                verification={},
            )
        )
        decision = session.scalar(select(CandidateDecision))
        session.add(
            CandidateDecision(subtask_id=newsub.id, release_id=decision.release_id, report=decision.report)
        )
        download = session.scalar(select(Download))
        plan = deepcopy(download.plan)
        plan["bindings"].append({**plan["bindings"][0], "subtask_id": newsub.id})
        download.plan = plan
        keep_task, keep_sub, old_sub = newer.id, newsub.id, sub.id
    db.migrate()
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.get(Task, keep_task).requirements["max_resolution"] == 2160
        assert session.get(Subtask, old_sub) is None
        assert session.scalar(select(TaskSeason)).whole_season
        assert session.scalar(select(func.count()).select_from(SubtaskAsset)) == 1
        assert session.scalar(select(SubtaskAsset)).subtask_id == keep_sub
        assert session.scalar(select(CandidateDecision)).subtask_id == keep_sub
        bindings = session.scalar(select(Download)).plan["bindings"]
        assert [b["subtask_id"] for b in bindings] == [keep_sub]
        assert (
            len(
                session.scalar(select(AuditEvent).where(AuditEvent.action == "task.merge")).details[
                    "previous_tasks"
                ]
            )
            == 2
        )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
