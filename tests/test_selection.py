import json
import time

import pytest
from sqlalchemy import select
from conftest import candidate, audio_claim, request
from test_worker import worker_setup as worker_setup
from lazarr.config import Requirements
from lazarr.matcher import Matcher
from lazarr.models import ConfigEntry, ProviderConfig, Download, Release, Subtask
from lazarr.sdk import (
    MetadataItem,
    SubtaskRequest,
    Evidence,
    ProviderError,
    SearchPage,
    ProviderManifest,
    DownloadSource,
)
from lazarr.selection import reject_reason, assess_candidate
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
        "Re:Zero / Сезон: 03 / Серии: 1-8 [1080p]",
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
    assert reject_reason(candidate(title="Re:Zero / Сезон: 02 / [1080p]"), [rezero]) is None
    assert reject_reason(candidate(title="Re:Zero", external_ids={}), [rezero]) is None
    other = rezero.model_copy(update={"season": 3, "episode": 1})
    assert reject_reason(item, [other, rezero]) is None
    item.evidence = [
        Evidence(field="audio_languages", value=["ru"], source="description", scope="all_video_files")
    ]
    assert reject_reason(item, [rezero], detailed=True) is None
    item.evidence[0].complete = True
    assert reject_reason(item, [rezero], detailed=True)


def test_keyword_score_waits_for_description_and_never_blocks(rezero):
    from lazarr.config import KeywordRule

    rezero.requirements.keywords = [KeywordRule(text="Group", points=50)]
    item = candidate(title="Re:Zero (ТВ-2) [1080p]", external_ids={})
    assert reject_reason(item, [rezero]) is None
    original = assess_candidate(item, rezero, 2).total
    assert reject_reason(item, [rezero], detailed=True) is None
    item.description = "Released by GROUP"
    assert reject_reason(item, [rezero], detailed=True) is None
    assert assess_candidate(item, rezero, 2).total == original + 50


def test_season_identity_requires_title_season_and_episode_year(rezero):
    item = candidate(title="Re:Zero (ТВ-2, часть 2) [2021] [1080p]", external_ids={})
    assert Matcher().identity(item, rezero).result == "MATCH"
    assert Matcher().identity(item, rezero.model_copy(update={"air_date": None})).result == "UNKNOWN"
    assert (
        Matcher().identity(item.model_copy(update={"title": "Re:Zero (ТВ-3) [2021]"}), rezero).result
        == "MATCH"
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
    assert p["candidates_found"] == 4 and p["candidates_filtered"] == 1 and p["candidates_checked"] == 1
    assert len(engine.plans) == 1


async def test_description_filter_does_not_resolve_torrent(core, media, season, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup

    async def resolve(*args):
        pytest.fail("Rejected description must not resolve torrent")

    async def inspect(self, item):
        return item.model_copy(update={"description": "Формат: BDMV"})

    monkeypatch.setattr(demo, "resolve_download", resolve)
    monkeypatch.setattr(demo, "inspect", inspect)
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    assert worker.progress.snapshot()["candidates_filtered"] == 1
    assert worker.progress.snapshot()["candidates_checked"] == 0


async def test_search_compares_bounded_pages_even_when_already_covered(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, _, demo = worker_setup
    original_search = demo.search
    calls = []

    async def search(self, query, cursor=None):
        calls.append(cursor)
        page = await original_search(self, query, cursor)
        return page.model_copy(update={"next_cursor": "more"})

    monkeypatch.setattr(demo, "search", search)
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(max_resolution=2160),
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    assert calls == [None, "more", None, "more"]
    assert worker.progress.snapshot()["candidates_checked"] == 1


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
    assert calls == [
        ("demo", "search", None),
        ("demo", "search", "50"),
        ("demo", "search", None),
        ("demo", "search", "50"),
        ("second", "search", None),
        ("second", "search", None),
        ("demo", "inspect", "0"),
    ]
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
    assert calls == [
        ("demo", "search", None),
        ("demo", "search", "50"),
        ("demo", "search", None),
        ("demo", "search", "50"),
        ("demo", "inspect", "0"),
    ]
    assert Scheduler(worker, service).snapshot()["pending_requests"] == 1


async def test_search_expands_query_and_logs_actual_text(core, media, season, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup
    media.original_title = "日本語"
    media.title = "Не слишком ли ты сладкая, Бог соли Сато?"
    media.english_title = "Salt God Sato-san Is Too Sweet to Me"
    media.romanized_title = "Shiotaiou no Satou-san ga Ore ni dake Amai"
    media.aliases = [media.original_title, media.english_title, media.romanized_title]
    calls = []

    async def search(self, query, cursor=None):
        calls.append(query.text)
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", search)
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    assert calls == [
        "Не слишком ли ты сладкая, Бог соли Сато? 2020",
        "Не слишком ли ты сладкая, Бог соли Сато?",
        "Salt God Sato-san Is Too Sweet to Me 2020",
        "Salt God Sato-san Is Too Sweet to Me",
        "Shiotaiou no Satou-san ga Ore ni dake Amai 2020",
        "Shiotaiou no Satou-san ga Ore ni dake Amai",
    ]
    history = [v["message"] for v in worker.progress.snapshot()["history"] if v["stage"] == "search"]
    assert media.title in history[0] and media.english_title in history[2]
    assert all(media.original_title not in message for message in history)


async def test_search_stops_language_fallback_after_first_results(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, _, demo = worker_setup
    media.title = "Русское название"
    media.english_title = "English title"
    media.romanized_title = "Romaji title"
    media.aliases = [media.english_title, media.romanized_title]
    calls = []

    async def search(self, query, cursor=None):
        calls.append(query.text)
        items = (
            [candidate(provider="demo", title="English title S01E01")]
            if query.text == "English title 2020"
            else []
        )
        return SearchPage(items=items)

    monkeypatch.setattr(demo, "search", search)
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    assert calls == [
        "Русское название 2020",
        "Русское название",
        "English title 2020",
        "English title",
    ]


def test_season_and_episode_ranges_keep_partial_coverage(rezero):
    assert reject_reason(candidate(title="Re:Zero S01-S03 1080p"), [rezero]) is None
    assert (
        reject_reason(candidate(title="Re:Zero S02E01-E13 1080p"), [rezero]) is None
    )  # Keep for manual mapping.
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


def test_preference_scores_follow_requested_quality_and_languages(media):
    req = request(media, max_resolution=2160, subtitle_languages=["en"])
    low = candidate(title="Example Show (2020) 1080p")
    high = candidate(title="Example Show (2020) 2160p")
    assert assess_candidate(high, req).total - assess_candidate(low, req).total == 40
    high.evidence = [
        Evidence(field="audio_languages", value=["ru"], source="title"),
        Evidence(field="subtitle_languages", value=["en", "ja"], source="title"),
    ]
    assert assess_candidate(high, req).total - assess_candidate(low, req).total == 80
    # Repeated claims never add points, and unwanted languages add none.
    high.evidence *= 2
    assert assess_candidate(high, req).total - assess_candidate(low, req).total == 80
    unrelated = candidate(title="Unrelated 2160p", external_ids={}, evidence=high.evidence)
    assert not assess_candidate(unrelated, req).passed


def test_description_only_blocks_explicit_contradictions(media):
    req = request(media)
    item = candidate(title="Example Show", external_ids={})
    stage = assess_candidate(item, req, 2)
    assert stage.total == 20 and stage.passed
    item.description = "Формат: BDMV"
    assert "BDMV" in reject_reason(item, [req], detailed=True)
    item.description = ""
    item.external_ids = {"imdb": "another-show"}
    assert reject_reason(item, [req], detailed=True)


@pytest.mark.parametrize(
    "failure",
    ["bdmv_description", "bdmv_files", "audio_missing", "audio_unknown", "subtitles", "episode", "rejected"],
)
async def test_next_candidate_is_checked_until_all_requested_tracks_match(
    core, media, season, worker_setup, monkeypatch, failure
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    inspected, resolved = [], []
    wanted_subs = ["en"] if failure == "subtitles" else []
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(
                max_resolution=2160,
                subtitle_languages=wanted_subs,
            ),
        ),
        media,
        season,
        1,
    )

    async def search(self, query, cursor=None):
        return SearchPage(
            items=[
                candidate(provider="demo", id=key, title=f"Example Show (2020) {quality}p")
                for key, quality in [("best", 2160), ("next", 1080), ("unused", 720)]
            ]
        )

    def paths(item):
        number = 2 if item.id == "best" and failure == "episode" else 1
        quality = 2160 if item.id == "best" else 1080
        result = [f"Show.S01E0{number}.{quality}p.mkv"]
        if wanted_subs and item.id != "best":
            result.append(f"Show.S01E0{number}.{quality}p.en.srt")
        if item.id == "best" and failure == "bdmv_files":
            result.append("BDMV/STREAM/00001.m2ts")
        return result

    async def inspect(self, item):
        inspected.append(item.id)
        evidence = [
            audio_claim(
                paths(item)[0], ["en"] if item.id == "best" and failure == "audio_missing" else ["ru"]
            )
        ]
        if item.id == "best" and failure == "audio_unknown":
            evidence = []
        return item.model_copy(
            update={
                "evidence": evidence,
                "description": "Формат: BDMV" if item.id == "best" and failure == "bdmv_description" else "",
            }
        )

    async def resolve(self, item):
        resolved.append(item.id)
        return DownloadSource(torrent=json.dumps(paths(item)).encode())

    monkeypatch.setattr(demo, "search", search)
    monkeypatch.setattr(demo, "inspect", inspect)
    monkeypatch.setattr(demo, "resolve_download", resolve)
    if failure == "rejected":
        from lazarr.models import CandidateDecision

        with db.session() as session:
            req = service.request_for(session, session.get(Subtask, 1))
        item, metadata, report = await worker.evaluate(
            candidate(provider="demo", id="best", title="Example Show (2020) 2160p"), [req]
        )
        release_id, _ = worker._record(item, metadata, report)
        with db.session() as session:
            decision = session.scalar(
                select(CandidateDecision).where(CandidateDecision.release_id == release_id)
            )
            decision.action = "rejected"
        inspected.clear()
        resolved.clear()
        engine.inspect_calls = 0
    await worker.run_due()
    assert inspected == ["best", "next"]
    assert resolved == (["next"] if failure == "bdmv_description" else ["best", "next"])
    with db.session() as session:
        download = session.scalar(select(Download))
        assert session.get(Release, download.release_id).external_id == "next"
        assert session.scalar(select(Subtask)).status == "starting"
    assert len(engine.plans) == 1


async def test_quality_orders_descriptions_across_providers(core, media, season, worker_setup, monkeypatch):
    _, db, plugins, service = core
    worker, engine, demo = worker_setup
    inspected = []
    original_inspect = demo.inspect

    async def inspect(self, item):
        inspected.append(item.provider)
        return await original_inspect(self, item)

    class Second(demo):
        manifest = ProviderManifest(id="second", name="Second", kind="content", version="1.0.0")
        quality = 2160

        async def search(self, query, cursor=None):
            return SearchPage(items=[candidate(provider="second", title="Example Show (2020) 2160p")])

    monkeypatch.setattr(demo, "inspect", inspect)
    plugins.classes["second"] = Second
    with db.session() as session:
        session.add(ProviderConfig(id="second", enabled=True))
    service.create_from_metadata(
        CreateTask(
            media_id="42", kind="tv", season=1, episodes=[1], requirements=Requirements(max_resolution=2160)
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    assert inspected == ["second"]
    assert engine.inspect_calls == 1
    with db.session() as session:
        download = session.scalar(select(Download))
        assert session.get(Release, download.release_id).provider == "second"


async def test_search_stops_only_after_every_episode_is_covered(
    core, media, season, worker_setup, monkeypatch
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    inspected = []
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )

    async def search(self, query, cursor=None):
        return SearchPage(items=[candidate(provider="demo", id=str(i), seeds=10 - i) for i in [1, 2, 3]])

    async def inspect(self, item):
        inspected.append(item.id)
        return item.model_copy(update={"evidence": [audio_claim(f"Show.S01E0{item.id}.1080p.mkv")]})

    async def resolve(self, item):
        return DownloadSource(torrent=json.dumps([f"Show.S01E0{item.id}.1080p.mkv"]).encode())

    monkeypatch.setattr(demo, "search", search)
    monkeypatch.setattr(demo, "inspect", inspect)
    monkeypatch.setattr(demo, "resolve_download", resolve)
    await worker.run_due()
    assert inspected == ["1", "2"]
    assert engine.inspect_calls == 2 and len(engine.plans) == 2
    with db.session() as session:
        assert all(sub.status == "starting" for sub in session.scalars(select(Subtask)))


@pytest.mark.parametrize("alternatives", [False, True])
async def test_inspection_budget_and_manual_alternatives(
    core, media, season, worker_setup, monkeypatch, alternatives
):
    _, _, _, service = core
    worker, engine, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1] if alternatives else [3]),
        media,
        season,
        1,
    )

    async def search(self, query, cursor=None):
        return SearchPage(items=[candidate(provider="demo", id=str(i)) for i in range(20)])

    monkeypatch.setattr(demo, "search", search)
    if alternatives:
        await worker.search_alternatives(1)
    else:
        await worker.run_due()
    assert engine.inspect_calls == 12
    assert not engine.plans


def test_final_quality_points_use_matched_file_instead_of_title(media):
    from lazarr.sdk import TorrentFile

    req = request(media, max_resolution=2160)
    item = candidate(title="Example Show (2020) 2160p", evidence=[audio_claim("Show.S01E01.1080p.mkv")])
    report = Matcher().evaluate(
        item, [req], [TorrentFile(index=0, path="Show.S01E01.1080p.mkv", size=100)], "a" * 40
    )
    evaluation = report.evaluations[0]
    assert evaluation.result == "MATCH"
    title_quality = next(signal for signal in evaluation.scoring[0].signals if signal.group == "quality")
    file_quality = next(signal for signal in evaluation.scoring[2].signals if signal.group == "quality")
    assert title_quality.points == 80 and file_quality.points == 40
    assert file_quality.source == "torrent"
