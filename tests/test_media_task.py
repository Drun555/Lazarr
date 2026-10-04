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
from lazarr.models import (
    Task,
    TaskSeason,
    Subtask,
    SubtaskAsset,
    CandidateDecision,
    Download,
    AuditEvent,
    Season,
)
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
        assert not two["running"]
        assert [event["stage"] for event in two["history"]] == ["prepare"]
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
        # The current ORM model includes columns absent from revision 0009.
        sub = session.execute(text("SELECT id, episode_id, part_key FROM subtasks")).one()
        newsub_id = session.execute(
            text(
                "INSERT INTO subtasks (task_id, episode_id, part_key, status, next_search_at, "
                "lease_until, attempts, missing_subtitle_languages) "
                "VALUES (:task, :episode, :part, 'queued', 0, 0, 0, '[]') RETURNING id"
            ),
            {"task": newer.id, "episode": sub.episode_id, "part": sub.part_key},
        ).scalar_one()
        link = session.scalar(select(SubtaskAsset))
        session.add(
            SubtaskAsset(
                subtask_id=newsub_id,
                asset_id=link.asset_id,
                pending=True,
                preflight=deepcopy(link.preflight),
                verification={},
            )
        )
        decision = session.scalar(select(CandidateDecision))
        session.add(
            CandidateDecision(subtask_id=newsub_id, release_id=decision.release_id, report=decision.report)
        )
        download = session.scalar(select(Download))
        plan = deepcopy(download.plan)
        plan["bindings"].append({**plan["bindings"][0], "subtask_id": newsub_id})
        download.plan = plan
        keep_task, keep_sub, old_sub = newer.id, newsub_id, sub.id
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


def test_edit_task_replaces_seasons_atomically_and_can_restore_selection(core, media, monkeypatch):
    config, db, _, service = core
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", seasons=[{"season": 1}, {"season": 2}]),
        media,
        [season_info(1), season_info(2)],
        1,
    )
    original_ids = {s["id"] for s in service.list_tasks()[0]["subtasks"]}
    with TestClient(create_app(config)) as client:
        login(client)

        async def get_media(*args):
            return media

        async def get_season(self, external_id, number):
            return season_info(number)

        provider = client.app.state.ctx.plugins.classes["tmdb"]
        monkeypatch.setattr(provider, "get_media", get_media)
        monkeypatch.setattr(provider, "get_season", get_season)
        url = f"/api/v1/tasks/{task_id}"
        requirements = Requirements(min_resolution=1080, max_resolution=2160).model_dump()
        assert (
            client.patch(
                url, json={"seasons": [{"season": 2, "episodes": [1]}], "requirements": requirements}
            ).status_code
            == 200
        )
        result = client.get("/api/v1/tasks").json()[0]
        assert [s["season"] for s in result["seasons"]] == [2]
        assert not result["seasons"][0]["whole_season"]
        assert len(result["subtasks"]) == 1 and result["subtasks"][0]["episode"] == 1
        assert result["requirements"] == requirements
        assert (
            client.patch(
                url,
                json={
                    "seasons": [{"season": 1, "episodes": [999]}],
                    "requirements": Requirements().model_dump(),
                },
            ).status_code
            == 422
        )
        assert client.get("/api/v1/tasks").json()[0] == result
        assert client.patch(url, json={"seasons": []}).status_code == 422
        assert client.patch(url, json={"seasons": [{"season": 1}, {"season": 2}]}).status_code == 200
        restored = client.get("/api/v1/tasks").json()[0]
        assert {s["id"] for s in restored["subtasks"]} == original_ids
        assert all(s["status"] != "removed" for s in restored["subtasks"])


def test_edit_task_adds_manual_season_missing_from_metadata(core, media, season, monkeypatch):
    config, db, _, service = core
    task_id = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)

        async def get_media(*args):
            return media

        async def get_season(self, external_id, number):
            assert number == 1
            return season

        provider = client.app.state.ctx.plugins.classes["tmdb"]
        monkeypatch.setattr(provider, "get_media", get_media)
        monkeypatch.setattr(provider, "get_season", get_season)
        url = f"/api/v1/tasks/{task_id}"
        payload = {
            "seasons": [
                {"season": 1},
                {"season": 3, "title": "Новая арка", "manual": True},
            ]
        }
        response = client.patch(url, json=payload)
        assert response.status_code == 200, response.text
        task = client.get("/api/v1/tasks").json()[0]
        custom = next(item for item in task["seasons"] if item["season"] == 3)
        assert custom["manual"] is True and custom["title"] == "Новая арка"
        mapping_url = f"{url}/seasons/3/mapping"
        mapping = client.get(mapping_url)
        assert mapping.status_code == 200 and mapping.json()["episodes"] == []
        assert client.post(mapping_url + "/episodes", json={"number": 1, "title": "Пилот"}).status_code == 200
        assert client.patch(url, json=payload).status_code == 200
        assert [item["number"] for item in client.get(mapping_url).json()["episodes"]] == [1]
        assert (
            client.patch(
                url,
                json={"seasons": [{"season": 1}, {"season": 4, "manual": True, "episodes": [1]}]},
            ).status_code
            == 422
        )
        with_empty = {"seasons": payload["seasons"] + [{"season": 4, "title": "Пустой", "manual": True}]}
        assert client.patch(url, json=with_empty).status_code == 200
        assert client.patch(url, json=payload).status_code == 200
    with db.session() as session:
        assert session.scalar(select(Season).where(Season.number == 3)) is not None
        assert session.scalar(select(Season).where(Season.number == 4)) is None


def test_manual_insertion_shifts_loaded_and_catalog_seasons_without_losing_identity(core, media, monkeypatch):
    from lazarr.models import Episode, ConfigEntry

    config, db, _, service = core
    media.seasons = [{"number": n, "title": f"TMDB {n}", "episode_count": 2} for n in range(1, 7)]
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=5), media, season_info(5), 1
    )
    with db.session() as session:
        original = session.scalar(select(Season))
        original_id = original.id
        episode_ids = list(session.scalars(select(Episode.id)))
        task = session.get(Task, task_id)
        pool_prefix = f"season_mapping.{task.id}.{task.created_at}."
        session.add(ConfigEntry(key=pool_prefix + "5", value={"releases": [42]}))
    with TestClient(create_app(config)) as client:
        login(client)
        requested = []

        async def get_media(*args):
            return media

        async def get_season(self, external_id, number):
            requested.append(number)
            assert number in range(1, 7)
            return season_info(number)

        ctx = client.app.state.ctx
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", get_media)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", get_season)
        url = f"/api/v1/tasks/{task_id}"
        response = client.patch(
            url, json={"seasons": [{"season": 5}, {"season": 5, "manual": True, "title": "Вставка"}]}
        )
        assert response.status_code == 200, response.text
        with db.session() as session:
            assert session.get(Season, original_id).number == 6
            assert list(session.scalars(select(Episode.id))) == episode_ids
            assert session.get(ConfigEntry, pool_prefix + "6").value == {"releases": [42]}
        detail = client.get("/api/v1/libraries/media/1").json()
        assert [s["number"] for s in detail["seasons"]] == list(range(1, 8))
        assert next(s for s in detail["seasons"] if s["number"] == 5)["title"] == "Вставка"
        assert {e["season"] for e in detail["episodes"]} == {6}
        task = detail["task"]
        manual = next(s for s in task["seasons"] if s["manual"])
        # Reopening/saving the editor must not insert again or fetch TMDB season 6 for local 6.
        response = client.patch(
            url,
            json={
                "seasons": [
                    {"season": 6},
                    {"season": 5, "manual": True, "title": "Вставка", "season_id": manual["season_id"]},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert requested == [5, 5]
        assert client.get("/api/v1/libraries/media/1/seasons/5/episodes").status_code == 200
        assert requested == [5, 5]
        # Previously unloaded TMDB season 6 now lives at local season 7.
        response = client.post("/api/v1/libraries/media/1/seasons", json={"season": 7})
        assert response.status_code == 200, response.text
        assert requested[-1] == 6
        detail = client.get("/api/v1/libraries/media/1").json()
        assert {e["season"] for e in detail["episodes"]} == {6, 7}
        with db.session() as session:
            service._upsert_season(session, 1, season_info(5, 3))
            assert session.get(Season, original_id).number == 6
            assert session.scalar(select(Season).where(Season.number == 5)).title == "Вставка"


def test_edit_seasons_keeps_downloaded_files_and_restores_completed_parts(core, media, season, tmp_path):
    from lazarr.services import SeasonSelection
    from lazarr.models import LibraryAsset, MediaAsset, Release

    _, db, _, service = core
    task_id = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    path = tmp_path / "Show.S01E01.1080p.mkv"
    path.write_bytes(b"keep this file")
    with db.session() as session:
        task = session.get(Task, task_id)
        release = Release(provider="demo", external_id="completed", data={})
        session.add(release)
        session.flush()
        download = Download(
            release_id=release.id, infohash="completed", save_path=str(tmp_path), torrent_file="", plan={}
        )
        session.add(download)
        session.flush()
        for index, sub in enumerate(session.scalars(select(Subtask).where(Subtask.task_id == task_id))):
            sub.status = "done"
            asset = MediaAsset(
                media_id=task.media_id, download_id=download.id, video_index=index, path=path.name
            )
            session.add(asset)
            session.flush()
            session.add(
                SubtaskAsset(subtask_id=sub.id, asset_id=asset.id, current=True, pending=False, preflight={})
            )
            session.add(
                LibraryAsset(
                    media_id=task.media_id,
                    episode_id=sub.episode_id,
                    part_key=sub.part_key,
                    asset_id=asset.id,
                )
            )
        session.flush()
        asset_ids = set(session.scalars(select(LibraryAsset.id)))
    service.edit(task_id, 1, selections=[(SeasonSelection(season=1, episodes=[2]), {}, season, {2})])
    assert path.read_bytes() == b"keep this file"
    assert service.list_tasks()[0]["completed"]
    with db.session() as session:
        assert set(session.scalars(select(LibraryAsset.id))) == asset_ids
        assert session.get(Subtask, 1).status == "removed"
    service.edit(task_id, 1, selections=[(SeasonSelection(season=1), {}, season, {1, 2})])
    assert all(sub["status"] == "done" for sub in service.list_tasks()[0]["subtasks"])
    service.edit(task_id, 1, selections=[(SeasonSelection(season=1, episodes=[2]), {}, season, {2})])
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    assert len(service.list_tasks()[0]["subtasks"]) == len(season.episodes)
    assert service.list_tasks()[0]["completed"]
