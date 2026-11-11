"""A provider-owned file format exercises the SDK without a production adapter."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from conftest import audio_claim, candidate
from lazarr.config import Requirements
from lazarr.models import (
    CandidateDecision,
    ConfigEntry,
    Download,
    ProviderConfig,
    Release,
    Subtask,
    SubtaskAsset,
)
from lazarr.sdk import DownloadSource, OfflineStatus, ProviderError, ProviderManifest, SearchPage
from lazarr.services import CreateTask
from lazarr.storage import describe, storage_root
from test_worker import worker_setup as worker_setup


@pytest.fixture
def offline_setup(core, worker_setup):
    _, _, plugins, service = core
    worker, engine, original = worker_setup
    events = []

    class Offline(original):
        manifest = ProviderManifest(
            id="demo",
            name="Offline Demo",
            kind="content",
            version="1",
            sdk=">=1.9,<2",
            offlineCapability=True,
        )
        page_size = 2
        magnet_only = False
        online = [{"id": "shared", "paths": ["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]}]

        def catalog(self):
            for directory in self.ctx.offline_directories:
                path = directory / "index.json"
                if path.is_file():
                    return json.loads(path.read_text())
            return None

        async def offline_status(self):
            events.append(("status", self.ctx.offline))
            catalog = self.catalog()
            return (
                OfflineStatus(available=True, year_from=catalog.get("year_from"), year_to=catalog["year_to"])
                if catalog
                else OfflineStatus(message="index.json не найден")
            )

        async def search(self, query, cursor=None, *, offline=False):
            assert self.ctx.offline == offline
            events.append(("search", offline, tuple(query.episodes), query.text, cursor))
            items = self.catalog()["items"] if offline else self.online
            start = int(cursor or 0)
            part = items[start : start + self.page_size]
            return SearchPage(
                items=[
                    candidate(
                        provider="demo",
                        id=item["id"],
                        title=item.get("title", "Example Show (2020) 1080p"),
                        seeds=item.get("seeds", 1),
                    )
                    for item in part
                ],
                next_cursor=str(start + self.page_size) if start + self.page_size < len(items) else None,
            )

        async def inspect(self, item, *, offline=False):
            assert self.ctx.offline == offline
            events.append(("inspect", offline, item.id))
            rows = self.catalog()["items"] if offline else self.online
            row = next(row for row in rows if row["id"] == item.id)
            return item.model_copy(
                update={
                    "description": row.get("description", ""),
                    "evidence": [audio_claim(path, row.get("audio", ["ru"])) for path in row["paths"]],
                }
            )

        async def resolve_download(self, item, *, offline=False):
            assert self.ctx.offline == offline
            events.append(("resolve", offline, item.id))
            if offline and self.magnet_only:
                return DownloadSource(magnet="magnet:?xt=urn:btih:" + "a" * 40)
            rows = self.catalog()["items"] if offline else self.online
            row = next(row for row in rows if row["id"] == item.id)
            return DownloadSource(torrent=json.dumps(row["paths"]).encode())

    plugins.classes["demo"] = Offline
    plugins.prepare_offline_storage(service.settings())
    path = plugins.offline_directories("demo")[0] / "index.json"

    def write(items=None, **coverage):
        path.write_text(
            json.dumps({"year_to": 2025, **coverage, "items": items if items is not None else Offline.online})
        )

    write()
    return SimpleNamespace(
        worker=worker, engine=engine, provider=Offline, path=path, write=write, events=events
    )


def create(core, media, season, episodes=None, **requirements):
    return core[3].create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=season.number,
            episodes=episodes or [1],
            requirements=Requirements(**requirements),
        ),
        media,
        season,
        1,
    )


def test_offline_manifest_and_year_boundaries():
    assert ProviderManifest(
        id="demo", name="Demo", kind="content", version="1", offlineCapability=True
    ).offline_capability
    status = OfflineStatus(available=True, year_from=2000, year_to=2025)
    assert status.covers(2000) and status.covers(2025)
    assert not any(status.covers(year) for year in (None, 1999, 2026))
    assert not OfflineStatus().covers(2020)
    for kwargs in ({"available": True}, {"year_from": 2025, "year_to": 2020}):
        with pytest.raises(ValidationError):
            OfflineStatus(**kwargs)


async def test_complete_offline_pass_skips_all_online_methods(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season, [1, 2])
    await env.worker.run_due()
    assert env.events[0] == ("status", True)
    assert [e[:2] for e in env.events if e[0] in {"inspect", "resolve"}] == [
        ("inspect", True),
        ("resolve", True),
    ]
    assert all(e[1] for e in env.events)
    assert env.engine.inspect_calls == 1
    with core[1].session() as session:
        assert len(list(session.scalars(select(SubtaskAsset)))) == 2
        assert session.scalar(select(Release)).data["offline"] is True


@pytest.mark.parametrize("case", ["absent", "empty", "corrupt", "wrong_episode", "audio"])
async def test_offline_miss_falls_back_to_online_without_suppressing_same_topic(
    core, media, season, offline_setup, case
):
    env = offline_setup
    create(core, media, season)
    if case == "absent":
        env.path.unlink()
    elif case == "empty":
        env.write([])
    elif case == "corrupt":
        env.path.write_text("broken catalog")
    elif case == "wrong_episode":
        env.write([{"id": "shared", "paths": ["Show.S01E02.1080p.mkv"]}])
    elif case == "audio":
        env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"], "audio": ["en"]}])
    await env.worker.run_due()
    assert any(e[0] == "search" and not e[1] for e in env.events)
    assert env.events[-2:] == [("inspect", False, "shared"), ("resolve", False, "shared")]
    with core[1].session() as session:
        assert session.scalar(select(Download)) is not None
        assert session.scalar(select(Release)).data["offline"] is False
        assert session.get(ProviderConfig, "demo").retry_at == 0


@pytest.mark.parametrize("metadata_available", [True, False])
async def test_offline_magnet_can_fetch_metadata_before_online_fallback(
    core, media, season, offline_setup, monkeypatch, metadata_available
):
    env = offline_setup
    create(core, media, season)
    env.provider.magnet_only = True
    original_inspect = env.engine.inspect
    sources = []

    def inspect(source):
        sources.append(source)
        if source.magnet:
            assert not any(e[0] == "search" and not e[1] for e in env.events)
            if not metadata_available:
                raise TimeoutError("Magnet metadata unavailable")
            source = DownloadSource(torrent=json.dumps(env.provider.online[0]["paths"]).encode())
        return original_inspect(source)

    monkeypatch.setattr(env.engine, "inspect", inspect)
    await env.worker.run_due()
    assert sources[0].magnet
    assert any(e[0] == "search" and not e[1] for e in env.events) != metadata_available
    assert env.engine.plans
    with core[1].session() as session:
        assert session.scalar(select(Release)).data["offline"] == metadata_available


async def test_partial_offline_coverage_keeps_found_episode_and_online_refreshes_same_topic(
    core, media, season, offline_setup
):
    env = offline_setup
    create(core, media, season, [1, 2])
    env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"]}])
    await env.worker.run_due()
    assert [(e[0], e[1]) for e in env.events if e[0] == "inspect"] == [("inspect", True), ("inspect", False)]
    assert len(env.engine.plans) == 2
    assert sorted(b.subtask_id for plan in env.engine.plans.values() for b in plan.bindings) == [1, 2]


@pytest.mark.parametrize("year,eligible", [(2024, True), (2025, True), (2026, False), (None, False)])
async def test_season_episode_year_controls_offline_eligibility(
    core, media, season, offline_setup, year, eligible
):
    env = offline_setup
    media.year = 2000  # The original series year must not hide a newer season.
    for episode in season.episodes:
        episode.air_date = f"{year}-01-01" if year else None
    create(core, media, season)
    await env.worker._run_group([1])
    assert any(e[0] == "search" and e[1] for e in env.events) == eligible
    assert any(e[0] == "search" and not e[1] for e in env.events) != eligible


async def test_catalog_only_searches_episodes_within_its_years(core, media, season, offline_setup):
    env = offline_setup
    season.episodes[0].air_date = "2024-01-01"
    season.episodes[1].air_date = "2026-01-01"
    create(core, media, season, [1, 2])
    env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"]}])
    await env.worker._run_group([1, 2])
    assert all(e[2] == (1,) for e in env.events if e[0] == "search" and e[1])
    assert any(e[0] == "search" and not e[1] for e in env.events)


async def test_offline_exhausts_more_than_three_pages_and_twelve_candidates(
    core, media, season, offline_setup
):
    env = offline_setup
    create(core, media, season)
    env.write(
        [
            {"id": str(i), "seeds": 100 - i, "paths": [f"Show.S01E0{1 if i == 14 else 2}.1080p.mkv"]}
            for i in range(15)
        ]
    )
    await env.worker.run_due()
    assert env.engine.inspect_calls == 15
    assert all(e[1] for e in env.events)
    assert env.engine.plans


async def test_offline_session_ignores_online_cooldown_and_never_changes_auth(
    core, media, season, offline_setup, monkeypatch
):
    env = offline_setup
    _, db, plugins, _ = core
    create(core, media, season)
    with db.session() as session:
        row = session.get(ProviderConfig, "demo")
        row.retry_at = time.time() + 3600
        row.last_error = "rate_limited: online pause"
        row.session_state = plugins.secrets.encrypt({"authenticated": True})
        row.secrets = plugins.secrets.encrypt({"password": "local-test-password"})
        before = (row.retry_at, row.last_error, row.session_state)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text="metadata")

    monkeypatch.setattr(plugins, "transport", httpx.MockTransport(respond))
    async with plugins.open("demo", offline=True) as provider:
        assert provider.ctx.state == {"authenticated": True}
        assert provider.ctx.config["password"] == "local-test-password"
        provider.ctx.state["authenticated"] = False
        for operation in (provider.ctx.request, provider.ctx.http.request):
            response = await operation("GET", "https://example.invalid")
            assert response.text == "metadata"
    assert len(requests) == 2
    await env.worker.run_due()
    assert env.engine.plans and all(e[1] for e in env.events)
    with db.session() as session:
        row = session.get(ProviderConfig, "demo")
        assert (row.retry_at, row.last_error, row.session_state) == before
        assert session.get(ConfigEntry, "provider.backoff.demo") is None


async def test_offline_error_does_not_create_online_backoff(core, offline_setup):
    _, db, plugins, _ = core
    with pytest.raises(ProviderError):
        async with plugins.open("demo", offline=True):
            raise ProviderError("unavailable", "bad local files", 3600)
    with db.session() as session:
        row = session.get(ProviderConfig, "demo")
        assert row.retry_at == 0 and row.last_error is None


def test_offline_directories_follow_storage_roots_and_provider_capabilities(core, offline_setup, tmp_path):
    _, _, plugins, service = core
    settings = service.settings()
    expected = storage_root(settings.movie_path) / "offline" / "demo"
    assert expected.is_dir() and plugins.offline_directories("demo") == (expected,)
    assert plugins.offline_directories("nyaa") == ()
    settings.series_path = str(tmp_path / "another-disk")
    service.set_settings(settings, 1)
    assert set(plugins.offline_directories("demo")) == {expected, tmp_path / "another-disk/offline/demo"}
    assert (tmp_path / "another-disk/offline/demo").is_dir()
    assert offline_setup.path.is_file()  # Updating settings never moves/removes user files.
    assert all("offline" in root for root in describe(core[1], settings)["roots"])
    description = next(p for p in plugins.describe() if p["id"] == "demo")
    assert description["offline_capability"] and len(description["offline_directories"]) == 2


async def test_disabled_offline_provider_is_not_probed(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season)
    core[2].configure("demo", {}, False)
    await env.worker.run_due()
    assert env.events == [] and not env.engine.plans


async def test_manual_alternatives_keep_both_modes(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season)
    await env.worker.search_alternatives(1)
    assert ("inspect", True, "shared") in env.events
    assert ("inspect", False, "shared") in env.events
    assert not env.engine.plans


async def test_offline_pass_precedes_other_online_providers(core, media, season, offline_setup):
    env = offline_setup
    _, db, plugins, _ = core
    create(core, media, season)

    class Other(env.provider):
        manifest = ProviderManifest(id="other", name="Other", kind="content", version="1")

        async def search(self, query, cursor=None):
            pytest.fail("Online providers must wait for the whole offline pass")

    plugins.classes["other"] = Other
    with db.session() as session:
        session.add(ProviderConfig(id="other", enabled=True))
    plugins.set_content_order(["other", "demo"])
    await env.worker.run_due()
    assert env.engine.plans and all(e[1] for e in env.events)


async def test_offline_pass_precedes_refresh_of_completed_topics(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season, [1, 2])
    env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"]}])
    await env.worker._run_group([1])
    with core[1].session() as session:
        link = session.scalar(select(SubtaskAsset))
        link.current, link.pending = True, False
        session.get(Subtask, 1).status = "done"
    env.write()
    env.events.clear()
    await env.worker._run_group([2])
    assert env.engine.plans and all(e[1] for e in env.events)
    assert sum(e[0] == "inspect" for e in env.events) == 1


async def test_rejected_local_topic_cannot_stop_online_fallback(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season)
    with core[1].session() as session:
        release = Release(
            provider="demo",
            external_id="shared",
            revision="old",
            data=candidate(provider="demo", id="shared").model_dump(),
        )
        session.add(release)
        session.flush()
        session.add(CandidateDecision(subtask_id=1, release_id=release.id, action="rejected", report={}))
    env.provider.online = [{"id": "alternative", "paths": ["Show.S01E01.1080p.mkv"]}]
    await env.worker.run_due()
    assert env.events[-2:] == [("inspect", False, "alternative"), ("resolve", False, "alternative")]
    with core[1].session() as session:
        download = session.scalar(select(Download))
        assert session.get(Release, download.release_id).external_id == "alternative"


async def test_missing_requested_subtitles_requires_online_fallback(core, media, season, offline_setup):
    env = offline_setup
    create(core, media, season, subtitle_languages=["en"])
    env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"]}])
    env.provider.online = [{"id": "shared", "paths": ["Show.S01E01.1080p.mkv", "Show.S01E01.1080p.en.srt"]}]
    await env.worker.run_due()
    assert env.events[-2:] == [("inspect", False, "shared"), ("resolve", False, "shared")]
    assert all(not b.missing_subtitle_languages for plan in env.engine.plans.values() for b in plan.bindings)


async def test_movie_year_uses_offline_catalog(core, media, offline_setup):
    env = offline_setup
    media.kind, media.year = "movie", 2024
    core[3].create_from_metadata(CreateTask(media_id="42", kind="movie"), media, None, 1)
    env.write([{"id": "shared", "paths": ["Example.Show.2024.1080p.mkv"]}])
    await env.worker._run_group([1])
    assert env.engine.plans and all(e[1] for e in env.events)


async def test_offline_candidate_report_reads_local_description(core, media, season, offline_setup):
    from lazarr.mapping_report import collect_report

    env = offline_setup
    create(core, media, season)
    env.write([{"id": "shared", "paths": ["Show.S01E01.1080p.mkv"], "description": "local description"}])
    await env.worker.run_due()
    with core[1].session() as session:
        release = session.scalar(select(Release))
        item = candidate(**{k: v for k, v in release.data.items() if k != "provider"}, provider="demo")
        req = core[3].request_for(session, session.get(Subtask, 1))
        snapshot = {"id": release.id, "revision": release.revision, "files": release.files}
    env.events.clear()
    ctx = SimpleNamespace(plugins=core[2])
    report = await collect_report(ctx, item, [req], [], snapshot)
    assert report["release"]["description_source"] == "offline"
    assert env.events == [("inspect", True, "shared")]


def test_offline_status_api_exposes_installed_coverage_without_online_requests(core, offline_setup):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from test_api import login

    env = offline_setup
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx
        ctx.plugins.classes["demo"] = env.provider
        response = client.get("/api/v1/providers/demo/offline")
        assert response.status_code == 200, response.text
        assert response.json()["available"] and response.json()["year_to"] == 2025
        assert response.json()["directories"] == [str(env.path.parent)]
        assert client.get("/api/v1/providers/nyaa/offline").status_code == 404
        assert all(event[1] for event in env.events)
        storage = client.get("/api/v1/storage").json()
        assert str(env.path.parent.parent) in [root["offline"] for root in storage["roots"]]
        env.path.unlink()
        response = client.get("/api/v1/providers/demo/offline")
        assert response.status_code == 200 and not response.json()["available"]
