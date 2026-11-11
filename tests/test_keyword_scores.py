"""Weighted phrases stay preferences across storage, scoring and search."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from conftest import audio_claim, candidate, request
from lazarr.app import create_app
from lazarr.config import Requirements
from lazarr.matcher import Matcher
from lazarr.models import ConfigEntry, Download, Release, Subtask, Task
from lazarr.sdk import SearchPage, TorrentFile
from lazarr.selection import assess_candidate, reject_reason
from lazarr.services import CreateTask
from test_api import login
from test_worker import worker_setup as worker_setup


def test_legacy_phrase_becomes_one_weighted_rule():
    requirements = Requirements(keyword=" Studio ")
    assert requirements.model_dump()["keywords"] == [{"text": "Studio", "points": 50}]
    assert "keyword" not in requirements.model_dump()
    assert Requirements(keyword="").keywords == []
    assert Requirements(keyword="Studio", keywords=[]).keywords == []


@pytest.mark.parametrize(
    "rules",
    [
        [{"text": " "}],
        [{"text": "x" * 201}],
        [{"text": "Studio"}, {"text": " studio "}],
        [{"text": "Studio", "points": 10001}],
        [{"text": "Studio", "points": -10001}],
        [{"text": "Studio", "points": 2.5}],
        [{"text": "Studio", "points": True}],
        [{"text": str(i)} for i in range(51)],
    ],
)
def test_invalid_keyword_rules_are_rejected(rules):
    with pytest.raises(ValidationError):
        Requirements(keywords=rules)


def test_keywords_score_once_in_title_or_description_without_blocking(media):
    req = request(
        media,
        keywords=[
            {"text": "Studio", "points": 50},
            {"text": "raw", "points": -1000},
            {"text": "missing", "points": 9999},
        ],
    )
    item = candidate(title="Example Show 2020 1080p STUDIO studio", description="Studio RAW raw")
    plain = request(media)
    for stage, delta in [(1, 50), (2, -950)]:
        assessed = assess_candidate(item, req, stage)
        assert assessed.total == assess_candidate(item, plain, stage).total + delta
        assert assessed.passed
        assert reject_reason(item, [req], detailed=stage == 2) is None
    signals = [s for s in assess_candidate(item, req, 2).signals if s.group == "keyword"]
    assert [(s.points, s.source) for s in signals] == [(50, "title"), (-1000, "description")]
    path = "Show.S01E01.1080p.mkv"
    item.evidence = [audio_claim(path)]
    report = Matcher().evaluate(item, [req], [TorrentFile(index=0, path=path, size=100)], "a" * 40)
    assert report.plan is not None
    final = report.evaluations[0]
    assert final.result == "MATCH" and final.score < 0
    assert not any(c.field == "keyword" for c in final.criteria)
    assert sum(s.points for s in final.scoring[-1].signals if s.group == "keyword") == -950
    # Preferences cannot compensate for an explicit identity conflict.
    item.external_ids = {"imdb": "another"}
    assert reject_reason(item, [req])


async def test_keyword_weights_set_description_order_and_early_choice(
    core, media, season, worker_setup, monkeypatch
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    inspected = []
    original_inspect = demo.inspect
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(
                keywords=[{"text": "Preferred", "points": 100}, {"text": "Avoid", "points": -500}]
            ),
        ),
        media,
        season,
        1,
    )

    async def search(self, query, cursor=None):
        return SearchPage(
            items=[
                candidate(provider="demo", id=word, title=f"Example Show 2020 1080p {word}", seeds=seeds)
                for word, seeds in [("Avoid", 10000), ("Ordinary", 500), ("Preferred", 1)]
            ]
        )

    async def inspect(self, item):
        inspected.append(item.id)
        return await original_inspect(self, item)

    monkeypatch.setattr(demo, "search", search)
    monkeypatch.setattr(demo, "inspect", inspect)
    await worker.run_due()
    assert inspected == ["Preferred"] and engine.inspect_calls == 1
    with db.session() as session:
        download = session.scalar(select(Download))
        assert session.get(Release, download.release_id).external_id == "Preferred"


def test_keyword_rules_create_edit_and_defaults_via_api(core, media, season, monkeypatch):
    config, _, _, _ = core
    rules = [{"text": "Studio", "points": 60}, {"text": "CAMRip", "points": -100}]
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx

        async def get_media(self, kind, media_id):
            return media

        async def get_season(self, media_id, number):
            return season

        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", get_media)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", get_season)
        result = client.post(
            "/api/v1/tasks",
            json={"media_id": "42", "kind": "tv", "season": 1, "requirements": {"keywords": rules}},
        )
        assert result.status_code == 201, result.text
        identity = result.json()["id"]
        assert client.get("/api/v1/tasks").json()[0]["requirements"]["keywords"] == rules
        rules[0]["points"] = -30
        result = client.patch(f"/api/v1/tasks/{identity}", json={"requirements": {"keywords": rules}})
        assert result.status_code == 200, result.text
        assert client.get("/api/v1/tasks").json()[0]["requirements"]["keywords"] == rules
        invalid = client.patch(
            f"/api/v1/tasks/{identity}", json={"requirements": {"keywords": [{"text": " "}]}}
        )
        assert invalid.status_code == 422
        assert client.get("/api/v1/tasks").json()[0]["requirements"]["keywords"] == rules
        settings = client.get("/api/v1/settings").json()
        settings["defaults"]["keywords"] = rules
        assert client.put("/api/v1/settings", json=settings).status_code == 200
        assert client.get("/api/v1/settings").json()["defaults"]["keywords"] == rules
        assert (
            client.patch(f"/api/v1/tasks/{identity}", json={"requirements": {"keywords": []}}).status_code
            == 200
        )
        assert client.get("/api/v1/tasks").json()[0]["requirements"]["keywords"] == []


def test_migration_preserves_legacy_phrases_tasks_and_defaults(core, media, season):
    _, db, _, service = core
    identity = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "src/lazarr/migrations"))
    config.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(config, "0016")
    with db.session() as session:
        task = session.get(Task, identity)
        legacy = dict(task.requirements)
        legacy.pop("keywords")
        legacy["keyword"] = " Original Studio "
        task.requirements = legacy
        settings = session.get(ConfigEntry, "app")
        settings.value = {**settings.value, "defaults": {**legacy, "keyword": "Default Studio"}}
    db.migrate()
    with db.session() as session:
        task = session.get(Task, identity)
        assert task.requirements["keywords"] == [{"text": "Original Studio", "points": 50}]
        assert "keyword" not in task.requirements
        assert task.requirements["audio_languages"] == legacy["audio_languages"]
        assert task.paused is False
        assert (
            service.request_for(session, session.scalar(select(Subtask))).requirements.model_dump()
            == task.requirements
        )
    assert service.settings().defaults.model_dump()["keywords"] == [{"text": "Default Studio", "points": 50}]
