from sqlalchemy import select, func
import pytest
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.models import Task, TaskSeason, Season, Subtask
from lazarr.sdk import SeasonInfo, EpisodeInfo
from lazarr.services import CreateTask
from lazarr.worker import Worker
from test_api import login
from test_worker import FakeEngine


def season_info(number, count=2):
    return SeasonInfo(
        number=number,
        episodes=[
            EpisodeInfo(id=f"{number}:{n}", number=n, title=f"S{number}E{n}", air_date="2020-01-01")
            for n in range(1, count + 1)
        ],
    )


def test_migration_preserves_legacy_season_selection(core, media, season):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    _, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[2]), media, season, 1
    )
    before = service.list_tasks()
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "src/lazarr/migrations"))
    config.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(config, "0008")
    db.migrate()
    assert service.list_tasks() == before


async def test_multi_season_requests_refresh_and_pause(core, media, monkeypatch):
    config, db, plugins, service = core
    identity = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", seasons=[{"season": 1}, {"season": 2, "episodes": [1]}]),
        media,
        [season_info(1), season_info(2)],
        1,
    )
    worker = Worker(db, plugins, service, FakeEngine(), config)
    assert not service.list_tasks()[0]["completed"]
    groups = await worker.due_groups()
    assert sorted(map(len, groups)) == [1, 2]
    with db.session() as session:
        requests = [service.request_for(session, sub) for sub in session.scalars(select(Subtask))]
        assert {(r.season, r.episode) for r in requests} == {(1, 1), (1, 2), (2, 1)}
        for season in session.scalars(select(Season)):
            season.refreshed_at = 0
        for sub in session.scalars(select(Subtask)):
            sub.status = "done"
    assert service.list_tasks()[0]["completed"]
    assert await worker.due_groups(force=True) == []

    async def get_season(self, media_id, number):
        return season_info(number, 3)

    monkeypatch.setattr(plugins.classes["tmdb"], "get_season", get_season)
    await service.refresh_seasons()
    task = service.list_tasks()[0]
    assert not task["completed"]
    assert [len(group) for group in await worker.due_groups(force=True)] == [1]
    assert task["id"] == identity and task["season"] is None
    assert [s["season"] for s in task["seasons"]] == [1, 2]
    assert [(s["season"], s["episode"]) for s in task["subtasks"]] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    service.edit(identity, 1, paused=True)
    assert await worker.due_groups(force=True) == []
    from lazarr.deletion import delete_task

    await delete_task(worker, identity, 1)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(TaskSeason)) == 0


def test_multi_season_api_and_atomic_validation(core, media, monkeypatch):
    config, db, plugins, service = core

    async def get_media(self, *args):
        return media

    async def get_season(self, media_id, number):
        return season_info(number)

    with TestClient(create_app(config)) as client:
        login(client)
        provider = client.app.state.ctx.plugins.classes["tmdb"]
        monkeypatch.setattr(provider, "get_media", get_media)
        monkeypatch.setattr(provider, "get_season", get_season)
        payload = {"media_id": "42", "kind": "tv", "seasons": [{"season": 1}, {"season": 2}]}
        assert client.post("/api/v1/tasks", json=payload).status_code == 201
        task = client.get("/api/v1/tasks").json()[0]
        assert len(task["seasons"]) == 2 and len(task["subtasks"]) == 4
        for invalid in [
            {**payload, "season": 1},
            {**payload, "kind": "movie"},
            {**payload, "seasons": []},
            {**payload, "seasons": [{"season": 1}, {"season": 1}]},
            {**payload, "seasons": [{"season": 1}, {"season": 2, "episodes": [99]}]},
        ]:
            assert client.post("/api/v1/tasks", json=invalid).status_code == 422
        assert len(client.get("/api/v1/tasks").json()) == 1
        assert (
            client.request("DELETE", f"/api/v1/libraries/media/{task['media_id']}", json={}).status_code
            == 200
        )
        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(TaskSeason)) == 0


@pytest.mark.parametrize("canonical", [False, True])
async def test_delete_displayed_season_preserves_other_numbering(core, canonical):
    from test_numbering import numbered_media, canonical_season
    from lazarr.deletion import delete_season

    config, db, plugins, service = core
    media = numbered_media()
    media.episode_numbering.update(
        {
            f"1:{n}": [{"season": 3, "episode": n - 50, "source": "tmdb:episode_group:test"}]
            for n in range(51, 67)
        }
    )
    selections = (
        [{"season": 1}]
        if canonical
        else [{"season": 2, "numbering_season": 2}, {"season": 3, "numbering_season": 3}]
    )
    service.create_from_metadata(
        CreateTask(media_id=media.id, kind="tv", seasons=selections), media, canonical_season(), 1
    )
    worker = Worker(db, plugins, service, FakeEngine(), config)
    await delete_season(worker, 1, 2, 1)
    with db.session() as session:
        remaining = list(session.scalars(select(Subtask)))
        assert remaining
        from lazarr.models import Episode

        assert all(not 26 <= session.get(Episode, sub.episode_id).number <= 50 for sub in remaining)
    await delete_season(worker, 1, 3, 1)
    if canonical:
        await delete_season(worker, 1, 1, 1)
    assert service.list_tasks() == []


async def test_two_alternate_seasons_share_canonical_season(core, monkeypatch):
    from test_numbering import numbered_media, canonical_season

    _, db, plugins, service = core
    media = numbered_media()
    media.episode_numbering.update(
        {
            f"1:{n}": [{"season": 3, "episode": n - 50, "source": "tmdb:episode_group:test"}]
            for n in range(51, 67)
        }
    )
    payload = CreateTask(
        media_id=media.id,
        kind="tv",
        seasons=[
            {"season": 2, "numbering_season": 2},
            {"season": 3, "numbering_season": 3},
        ],
    )
    identity = service.create_from_metadata(payload, media, canonical_season(), 1)
    task = service.list_tasks()[0]
    assert len(task["subtasks"]) == 41
    assert {(s["season"], s["canonical_season"]) for s in task["seasons"]} == {(2, 1), (3, 1)}
    assert [(s["season"], s["episode"]) for s in task["subtasks"]][-1] == (3, 16)
    with pytest.raises(ValueError, match="одни и те же"):
        service.create_from_metadata(
            CreateTask(
                media_id=media.id,
                kind="tv",
                seasons=[
                    {"season": 1},
                    {"season": 2, "numbering_season": 2},
                ],
            ),
            media,
            canonical_season(),
            1,
        )

    async def get_media(self, *args):
        return media

    async def get_season(self, *args):
        return canonical_season()

    monkeypatch.setattr(plugins.classes["tmdb"], "get_media", get_media)
    monkeypatch.setattr(plugins.classes["tmdb"], "get_season", get_season)
    with db.session() as session:
        session.scalar(select(Season)).refreshed_at = 0
    await service.refresh_seasons()
    assert len(service.list_tasks()[0]["subtasks"]) == 41
    with db.session() as session:
        assert session.get(Task, identity) is not None
