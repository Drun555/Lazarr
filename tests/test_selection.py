import json
import time

import pytest
from sqlalchemy import select
from conftest import candidate, audio_claim
from test_worker import worker_setup as worker_setup
from lazarr.config import Requirements
from lazarr.matcher import Matcher
from lazarr.models import ConfigEntry, ProviderConfig
from lazarr.sdk import (
    MetadataItem,
    SubtaskRequest,
    Evidence,
    ProviderError,
    SearchPage,
    ProviderManifest,
    DownloadSource,
)
from lazarr.selection import reject_reason, can_improve
from lazarr.services import CreateTask
from lazarr.scheduler import Scheduler
from lazarr.search import PREFIX


@pytest.fixture
def rezero():
    media = MetadataItem(
        id="65942",
        kind="tv",
        title="Re:Zero",
        year=2016,
        episode_numbering={"1:39": [{"season": 2, "episode": 14}]},
    )
    return SubtaskRequest(
        id=1,
        media=media,
        season=1,
        episode=39,
        air_date="2021-01-06",
        requirements=Requirements(
            audio_languages=["ja"], subtitle_languages=["ru"], min_resolution=1080, max_resolution=2160
        ),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Re:Zero (ТВ-1) [1080p]",
        "Re:Zero 3rd Season [1080p]",
        "Re:Zero [Movie] [1080p]",
        "Re:Zero (ТВ-2, часть 2) [720p]",
        "Re:Zero [manga]",
        "Re:Zero [PDF, RUS]",
        "(OST) Re:Zero FLAC tracks lossless",
        "Re:Zero (ТВ-2) [RUS(Dub)] [2020, ААС]",
        "[DL] Re:Zero [P] [ENG] [Scene]",
    ],
)
def test_explicit_contradictions_are_filtered_before_network(rezero, title):
    assert reject_reason(candidate(title=title, external_ids={}), [rezero])


def test_unknown_claims_partial_coverage_and_anime_numbering_are_kept(rezero):
    item = candidate(title="Re:Zero (ТВ-2, часть 2) [2021] [1080p]", external_ids={})
    assert reject_reason(item, [rezero]) is None
    assert reject_reason(candidate(title="Re:Zero", external_ids={}), [rezero]) is None
    other = rezero.model_copy(update={"season": 3, "episode": 1})
    assert reject_reason(item, [other, rezero]) is None
    item.evidence = [
        Evidence(field="audio_languages", value=["ru"], source="description", scope="all_video_files")
    ]
    assert reject_reason(item, [rezero], detailed=True) is None
    item.evidence[0].complete = True
    assert reject_reason(item, [rezero], detailed=True)


def test_keyword_waits_for_description_and_subtitles_never_exclude(rezero):
    rezero.requirements.keyword = "Group"
    item = candidate(title="Re:Zero (ТВ-2) [1080p]", external_ids={})
    assert reject_reason(item, [rezero]) is None
    assert reject_reason(item, [rezero], detailed=True)
    item.description = "Released by GROUP"
    assert reject_reason(item, [rezero], detailed=True) is None


def test_season_identity_requires_title_season_and_episode_year(rezero):
    item = candidate(title="Re:Zero (ТВ-2, часть 2) [2021] [1080p]", external_ids={})
    assert Matcher().identity(item, rezero).result == "MATCH"
    assert Matcher().identity(item, rezero.model_copy(update={"air_date": None})).result == "UNKNOWN"
    assert (
        Matcher().identity(item.model_copy(update={"title": "Re:Zero (ТВ-3) [2021]"}), rezero).result
        == "UNKNOWN"
    )
    assert (
        Matcher().identity(item.model_copy(update={"title": "Unrelated (ТВ-2) [2021]"}), rezero).result
        == "UNKNOWN"
    )
    rezero.media.external_ids = {"imdb": "tt1"}
    assert (
        Matcher().identity(item.model_copy(update={"external_ids": {"imdb": "tt2"}}), rezero).result
        == "MISMATCH"
    )


def test_coverage_prunes_equal_quality_but_keeps_upgrades_and_unknown(rezero):
    item = candidate(title="Re:Zero [1080p]")
    assert not can_improve(item, [rezero], {rezero.id: 1080})
    assert can_improve(item.model_copy(update={"title": "Re:Zero [2160p]"}), [rezero], {rezero.id: 1080})
    assert can_improve(item.model_copy(update={"title": "Re:Zero"}), [rezero], {rezero.id: 1080})
    assert can_improve(item, [rezero, rezero.model_copy(update={"id": 2})], {rezero.id: 1080})


async def test_worker_filters_before_inspect_and_resolve_and_orders_candidates(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, engine, demo = worker_setup
    inspected, resolved = [], []

    async def search(self, *args):
        return SearchPage(
            items=[
                candidate(provider="demo", id=str(i), title=t)
                for i, t in enumerate(
                    [
                        "Example Show [manga]",
                        "Example Show (2020) 1080p",
                        "Example Show (2020) 2160p",
                        "Example Show (TV-3) 2160p",
                    ]
                )
            ]
        )

    async def inspect(self, item):
        inspected.append(item.id)
        return item.model_copy(update={"evidence": [audio_claim("Show.S01E01.2160p.mkv")]})

    async def resolve(self, item):
        resolved.append(item.id)
        return DownloadSource(torrent=json.dumps(["Show.S01E01.2160p.mkv"]).encode())

    monkeypatch.setattr(demo, "search", search)
    monkeypatch.setattr(demo, "inspect", inspect)
    monkeypatch.setattr(demo, "resolve_download", resolve)
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(audio_languages=["ru"], max_resolution=2160),
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    assert inspected == resolved == ["2"]
    p = worker.progress.snapshot()
    assert p["candidates_found"] == 4 and p["candidates_filtered"] == 3 and p["candidates_checked"] == 1
    assert len(engine.plans) == 1


async def test_description_filter_does_not_resolve_torrent(core, media, season, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup

    async def resolve(*args):
        pytest.fail("Rejected description must not resolve torrent")

    monkeypatch.setattr(demo, "resolve_download", resolve)
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(keyword="missing phrase"),
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    assert worker.progress.snapshot()["candidates_filtered"] == 1
    assert worker.progress.snapshot()["candidates_checked"] == 0


async def test_cooldown_stops_candidate_loop_and_pagination_and_retries_only_failed_provider(
    core, media, season, worker_setup, monkeypatch
):
    _, db, plugins, service = core
    worker, _, demo = worker_setup
    calls = []

    async def search(self, query, cursor=None):
        calls.append((self.manifest.id, "search", cursor))
        return SearchPage(
            items=[candidate(provider=self.manifest.id, id=str(i)) for i in range(3)], next_cursor="50"
        )

    async def inspect(self, item):
        calls.append((self.manifest.id, "inspect", item.id))
        raise ProviderError("unavailable", "HTTP 504", 60)

    monkeypatch.setattr(demo, "search", search)
    monkeypatch.setattr(demo, "inspect", inspect)

    class Second(demo):
        manifest = ProviderManifest(id="second", name="Second", kind="content", version="1.0.0")

        async def search(self, *args):
            calls.append(("second", "search", None))
            return SearchPage(items=[])

    plugins.classes["second"] = Second
    with db.session() as session:
        session.add(ProviderConfig(id="second", enabled=True))
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    scheduler = Scheduler(worker, service)
    await scheduler.process_queue()
    assert calls == [("demo", "search", None), ("demo", "inspect", "0"), ("second", "search", None)]
    p = worker.progress.snapshot()
    assert p["candidates_failed"] == 1 and p["candidates_deferred"] == 2 and p["candidates_checked"] == 0
    assert not await scheduler.process_queue()
    with db.session() as session:
        row = session.scalar(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX + "retry.")))
        assert row.value["provider"] == "demo"
        assert row.value["not_before"] > time.time()
        row.value = {**row.value, "not_before": 0}
        session.get(ProviderConfig, "demo").retry_at = 0
    calls.clear()
    await Scheduler(worker, service).process_queue()
    assert calls == [("demo", "search", None), ("demo", "inspect", "0")]
    assert Scheduler(worker, service).snapshot()["pending_requests"] == 1


async def test_search_expands_query_and_logs_actual_text(core, media, season, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup
    media.original_title = "日本語"
    media.title = "Re:ZERO – Жизнь с нуля"
    media.aliases = ["ReZero", "Re:Zero", "Re Zero", "Re Zero Empezar de cero en un mundo diferente"]
    calls = []

    async def search(self, query, cursor=None):
        calls.append(query.text)
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", search)
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    assert calls == ["Re:Zero", "Re Zero", "ReZero"]
    history = [v["message"] for v in worker.progress.snapshot()["history"] if v["stage"] == "search"]
    assert "Re:Zero" in history[0] and "Re Zero" in history[1]


def test_season_and_episode_ranges_keep_partial_coverage(rezero):
    assert reject_reason(candidate(title="Re:Zero S01-S03 1080p"), [rezero]) is None
    assert reject_reason(candidate(title="Re:Zero S02E01-E13 1080p"), [rezero])
    assert reject_reason(candidate(title="Re:Zero S02E14-E25 1080p"), [rezero]) is None


def test_conflicting_complete_audio_claims_remain_unknown(rezero):
    item = candidate(
        title="Re:Zero (ТВ-2) 1080p",
        external_ids={},
        evidence=[
            Evidence(
                field="audio_languages",
                value=[code],
                source="description",
                scope="all_video_files",
                complete=True,
            )
            for code in ["ru", "ja"]
        ],
    )
    assert reject_reason(item, [rezero], detailed=True) is None
