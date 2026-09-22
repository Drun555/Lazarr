import asyncio
import hashlib
import json
from pathlib import Path
from sqlalchemy import select, func
import pytest
from lazarr.models import (
    ProviderConfig,
    Subtask,
    Download,
    MediaAsset,
    SubtaskAsset,
    LibraryAsset,
    CandidateDecision,
    Episode,
)
from lazarr.sdk import (
    ContentProvider,
    ProviderManifest,
    SearchPage,
    DownloadSource,
    TorrentFile,
    SeasonInfo,
    EpisodeInfo,
)
from lazarr.services import CreateTask
from lazarr.config import Requirements
from lazarr.torrent import TorrentMetadata
from lazarr.worker import Worker
from conftest import candidate, audio_claim


class FakeEngine:
    def contains(self, infohash):
        return infohash in self.handles

    def __init__(self):
        self.handles = {}
        self.plans = {}
        self.paused = {}
        self.completed = set()
        self.uploaded = 0
        self.downloaded = 100
        self.inspect_calls = 0

    def inspect(self, source):
        self.inspect_calls += 1
        data = json.loads(source.torrent)
        files = [TorrentFile(index=i, path=path, size=100, offset=i * 100) for i, path in enumerate(data)]
        return TorrentMetadata(hashlib.sha256(source.torrent).hexdigest(), files, source.torrent)

    def add(self, torrent, save_path, plan, paused=False, counters=None):
        self.handles[plan.infohash] = object()
        self.plans[plan.infohash] = plan
        self.paused[plan.infohash] = paused
        Path(save_path).mkdir(parents=True, exist_ok=True)
        return plan.infohash

    def update_plan(self, infohash, plan):
        self.plans[infohash] = plan

    def pause(self, infohash):
        self.paused[infohash] = True

    def remove(self, infohash):
        self.handles.pop(infohash, None)
        self.plans.pop(infohash, None)
        self.paused.pop(infohash, None)

    def resume(self, infohash):
        self.paused[infohash] = False

    def checkpoint(self):
        pass

    def snapshot(self, infohash):
        plan = self.plans[infohash]
        bindings = {
            str(b.subtask_id): {
                "progress": 1 if b.subtask_id in self.completed else 0,
                "complete": b.subtask_id in self.completed,
                "buffer_ready": b.subtask_id in self.completed,
                "downloaded": 100 if b.subtask_id in self.completed else 0,
                "total": 100,
                "eta": None,
            }
            for b in plan.bindings
        }
        complete = bool(bindings) and all(b["complete"] for b in bindings.values())
        return {
            "progress": 1 if complete else 0,
            "complete": complete,
            "uploaded": self.uploaded,
            "downloaded": self.downloaded,
            "bindings": bindings,
            "paused": self.paused[infohash],
            "error": None,
            "download_rate": 0,
            "upload_rate": 0,
            "eta": None,
        }


@pytest.fixture
def worker_setup(core, media):
    config, db, plugins, service = core

    class Demo(ContentProvider):
        manifest = ProviderManifest(id="demo", name="Demo", kind="content", version="1.0.0")
        calls = 0
        quality = 1080

        async def search(self, query, cursor=None):
            type(self).calls += 1
            return SearchPage(
                items=[
                    candidate(
                        provider="demo", id=str(self.quality), title=f"Example Show (2020) {self.quality}p"
                    )
                ]
            )

        async def inspect(self, item):
            paths = [f"Show.S01E0{i}.{self.quality}p.mkv" for i in [1, 2]]
            return item.model_copy(update={"evidence": [audio_claim(path) for path in paths]})

        async def resolve_download(self, item):
            return DownloadSource(
                torrent=json.dumps([f"Show.S01E0{i}.{self.quality}p.mkv" for i in [1, 2]]).encode()
            )

    plugins.classes["demo"] = Demo
    with db.session() as session:
        session.add(ProviderConfig(id="demo", enabled=True))
    engine = FakeEngine()
    worker = Worker(db, plugins, service, engine, config)

    async def probe(path, complete):
        quality = 2160 if "2160" in str(path) else 1080
        return {
            "ok": True,
            "streams": [
                {"codec_type": "video", "width": 3840 if quality == 2160 else 1920, "height": quality},
                {"codec_type": "audio", "tags": {"language": "rus"}},
            ],
        }

    worker._probe = probe
    return worker, engine, Demo


async def test_new_episode_candidates_include_season_release(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[2, 3]), media, season, 1
    )
    with db.session() as session:
        ids = dict(session.execute(select(Episode.number, Subtask.id).join(Subtask)).all())
    for number in [2, 3]:
        choices = service.candidates(ids[number])
        assert len(choices) == 1
        assert choices[0]["used_in_season"] == [1]
        assert choices[0]["episode_missing"] is (number == 3)
        assert len(service.candidates(ids[number])) == 1
    with db.session() as session:
        session.delete(session.scalar(select(SubtaskAsset)))
    assert service.candidates(ids[2])[0]["used_in_season"] == []


async def test_new_search_clears_previous_error_when_it_starts(
    core, media, season, worker_setup, monkeypatch
):
    _, db, _, service = core
    worker, _, demo = worker_setup
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with db.session() as session:
        subtask = session.scalar(select(Subtask).where(Subtask.task_id == task_id))
        subtask.last_error = "unavailable: previous provider failure"

    started = asyncio.Event()
    resume = asyncio.Event()

    async def waiting_search(self, query, cursor=None):
        started.set()
        await resume.wait()
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", waiting_search)
    search = asyncio.create_task(worker.run_due())
    await started.wait()
    with db.session() as session:
        subtask = session.scalar(select(Subtask).where(Subtask.task_id == task_id))
        assert subtask.status == "searching"
        assert subtask.last_error is None
    resume.set()
    await search


async def test_grouped_subtasks_share_one_download(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 2
    )
    await worker.run_due()
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Download)) == 1
        assert session.scalar(select(func.count()).select_from(MediaAsset)) == 2
        assert session.scalar(select(func.count()).select_from(SubtaskAsset)) == 2
        assert len(list(session.scalars(select(Subtask).where(Subtask.status == "starting")))) == 2
    assert demo.calls == 1
    # An immediate retry must not duplicate active work.
    await worker.run_due()
    assert demo.calls == 1
    engine.completed = {1, 2, 3}
    await worker.poll()
    with db.session() as session:
        assert all(s.status == "done" for s in session.scalars(select(Subtask)))
        assert all(link.current for link in session.scalars(select(SubtaskAsset)))
        assert session.scalar(select(func.count()).select_from(LibraryAsset)) == 2


async def test_search_uses_requested_season_year_and_stops_on_match(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, _, demo = worker_setup
    for episode in season.episodes:
        episode.air_date = "2022-01-01"
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    queries = []

    async def search(self, query, cursor=None):
        queries.append(query.text)
        return SearchPage(
            items=[candidate(provider="demo", id="season-year", title="Example Show (2022) 1080p")]
        )

    monkeypatch.setattr(demo, "search", search)
    await worker.run_due()
    assert queries == ["Example Show 2022"]


async def test_search_falls_back_without_year_when_no_release_matches(
    core, media, season, worker_setup, monkeypatch
):
    _, _, _, service = core
    worker, _, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    queries = []

    async def search(self, query, cursor=None):
        queries.append(query.text)
        items = (
            []
            if query.text.endswith("2020")
            else [candidate(provider="demo", id="fallback", title="Example Show (2020) 1080p")]
        )
        return SearchPage(items=items)

    monkeypatch.setattr(demo, "search", search)
    await worker.run_due()
    assert queries == ["Example Show 2020", "Example Show"]


async def test_movie_search_uses_movie_year_then_falls_back(core, media, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup
    movie = media.model_copy(update={"kind": "movie", "year": 1999})
    service.create_from_metadata(CreateTask(media_id="42", kind="movie"), movie, None, 1)
    queries = []

    async def search(self, query, cursor=None):
        queries.append(query.text)
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", search)
    await worker.run_due()
    assert queries == ["Example Show 1999", "Example Show"]


async def test_search_without_known_year_uses_plain_title_once(core, media, worker_setup, monkeypatch):
    _, _, _, service = core
    worker, _, demo = worker_setup
    movie = media.model_copy(update={"kind": "movie", "year": None})
    service.create_from_metadata(CreateTask(media_id="42", kind="movie"), movie, None, 1)
    queries = []

    async def search(self, query, cursor=None):
        queries.append(query.text)
        return SearchPage(items=[])

    monkeypatch.setattr(demo, "search", search)
    await worker.run_due()
    assert queries == ["Example Show"]


async def test_shared_download_survives_one_task_pause_and_stops_at_ratio(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    first = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    service.create_from_metadata(
        CreateTask(media_id="43", kind="tv", season=1, episodes=[1]),
        media.model_copy(update={"id": "43"}),
        season,
        2,
    )
    await worker.run_due()
    service.edit(first, 1, paused=True)
    await worker.sync_consumers()
    infohash = next(iter(engine.handles))
    assert not engine.paused[infohash]
    assert [b.subtask_id for b in engine.plans[infohash].bindings] == [2]
    engine.completed = {1, 2}
    engine.uploaded = 100
    await worker.poll()
    assert engine.paused[infohash]
    with db.session() as session:
        assert session.scalar(select(Download)).state == "stopped"


async def test_wrong_actual_audio_does_not_promote_version(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()

    async def bad_probe(path, complete):
        return {
            "ok": True,
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio", "tags": {"language": "eng"}},
            ],
        }

    worker._probe = bad_probe
    engine.completed = {1}
    await worker.poll()
    with db.session() as session:
        assert not session.scalar(select(SubtaskAsset)).current
        assert session.get(Subtask, 1).status == "needs_selection"
        assert session.scalar(select(CandidateDecision)).action == "rejected"


async def test_completed_tasks_do_not_search_or_upgrade(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    service.create_from_metadata(
        CreateTask(
            media_id="42", kind="tv", season=1, episodes=[1], requirements=Requirements(max_resolution=2160)
        ),
        media,
        season,
        1,
    )
    service.create_from_metadata(
        CreateTask(
            media_id="42", kind="tv", season=1, episodes=[1], requirements=Requirements(max_resolution=1080)
        ),
        media.model_copy(update={"id": "43"}),
        season,
        2,
    )
    await worker.run_due()
    engine.completed = {1, 2}
    await worker.poll()
    old_hash = next(iter(engine.handles))
    search_calls = demo.calls
    demo.quality = 2160
    assert all(task["completed"] for task in service.list_tasks())
    assert await worker.due_groups(force=True) == []
    with db.session() as session:
        session.get(Subtask, 1).next_search_at = 0
    await worker.run_due()
    await worker.run_due(force=True)
    await worker.poll()
    assert demo.calls == search_calls
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Download)) == 1
        current = session.scalar(
            select(MediaAsset)
            .join(SubtaskAsset)
            .where(SubtaskAsset.subtask_id == 1, SubtaskAsset.current.is_(True))
        )
        assert current.resolution == 1080
    assert not engine.paused[old_hash]
    # Editing requirements must not invalidate completed files or restart acquisition.
    service.edit(2, 2, requirements=Requirements(max_resolution=2160))
    await worker.sync_consumers()
    assert not engine.paused[old_hash]
    assert await worker.due_groups(force=True) == []
    assert all(task["completed"] for task in service.list_tasks())


async def test_alternative_search_records_quality_and_language_mismatches(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, demo = worker_setup
    demo.quality = 2160
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(audio_languages=["en"], max_resolution=1080),
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    with db.session() as session:
        assert session.scalar(select(CandidateDecision)) is None
    await worker.search_alternatives(1)
    choice = service.candidates(1)[0]
    criteria = {item["field"]: item["result"] for item in choice["report"]["criteria"]}
    assert criteria["resolution"] == "MISMATCH"
    assert criteria["audio"] == "MISMATCH"


async def test_bluray_bdmv_candidates_are_never_recorded(core, media, season, worker_setup, monkeypatch):
    _, db, _, service = core
    worker, _, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )

    async def bluray_download(self, item):
        return DownloadSource(torrent=json.dumps(["Movie/BDMV/STREAM/00001.m2ts"]).encode())

    monkeypatch.setattr(demo, "resolve_download", bluray_download)
    await worker.search_alternatives(1)
    with db.session() as session:
        assert session.scalar(select(CandidateDecision)) is None
        assert session.scalar(select(Download)) is None
    assert any("BDMV" in item["message"] for item in worker.progress.snapshot()["history"])


async def test_manual_url_is_inspected_and_saved_for_selection(
    core, media, season, worker_setup, monkeypatch
):
    _, _, plugins, service = core
    worker, _, _ = worker_setup
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    plugins.configure("nyaa", {}, True)
    provider = plugins.classes["nyaa"]

    async def inspect(self, item):
        return item.model_copy(
            update={
                "title": "Example Show (2020) 1080p",
                "evidence": [audio_claim("Show.S01E01.1080p.mkv")],
            }
        )

    async def resolve_download(self, item):
        return DownloadSource(torrent=json.dumps(["Show.S01E01.1080p.mkv"]).encode())

    monkeypatch.setattr(provider, "inspect", inspect)
    monkeypatch.setattr(provider, "resolve_download", resolve_download)
    decision_id = await worker.add_manual_candidate(1, "https://nyaa.si/view/321")
    choices = service.candidates(1)
    assert choices[0]["id"] == decision_id
    assert choices[0]["candidate"]["id"] == "321"
    await worker.add_manual_task_candidate(task_id, "https://nyaa.si/view/322", season_number=1)
    task_choice = next(
        choice for choice in service.task_candidates(task_id, 1) if choice["candidate"]["id"] == "322"
    )
    assert task_choice["total"] == 2
    assert {episode["subtask_id"] for episode in task_choice["episodes"]} == {1, 2}


async def test_future_dates_block_but_unknown_dates_search(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    with db.session() as session:
        session.get(Episode, 1).air_date = "2999-01-01"
        session.get(Episode, 2).air_date = None
    await worker.run_due()
    with db.session() as session:
        assert session.get(Subtask, 1).status == "waiting_release"
        assert session.get(Subtask, 2).status == "starting"
        plan = session.scalar(select(Download)).plan
        assert [b["subtask_id"] for b in plan["bindings"]] == [2]


async def test_restore_recreates_pending_download_without_duplication(core, media, season, worker_setup):
    _, db, plugins, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()

    class RestoredEngine(FakeEngine):
        def add(self, torrent, save_path, plan, paused=False, counters=None):
            self.uploaded = counters["uploaded"]
            self.downloaded = counters["downloaded"]
            return super().add(torrent, save_path, plan, paused)

    with db.session() as session:
        row = session.scalar(select(Download))
        row.uploaded = 75
        row.downloaded = 100
    restored = RestoredEngine()
    second = Worker(db, plugins, service, restored, worker.config)
    await second.restore()
    assert len(restored.handles) == 1 and restored.uploaded == 75
    await second.run_due()
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Download)) == 1


async def test_reselecting_current_candidate_is_idempotent(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    engine.completed = {1}
    await worker.poll()
    with db.session() as session:
        decision_id = session.scalar(select(CandidateDecision.id))
    await worker.choose(decision_id, 1)
    await worker.poll()
    with db.session() as session:
        link = session.scalar(select(SubtaskAsset))
        assert link.current and not link.pending
        assert session.get(Subtask, 1).status == "done"


async def test_manual_choice_interrupts_active_provider_search(
    core, media, season, worker_setup, monkeypatch
):
    _, db, _, service = core
    worker, engine, demo = worker_setup
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    torrent = json.dumps(["Show.S01E01.1080p.mkv"]).encode()
    metadata = engine.inspect(DownloadSource(torrent=torrent))
    item = candidate(provider="demo", id="manual-interrupt", evidence=[])
    with db.session() as session:
        sub = session.scalar(select(Subtask).where(Subtask.task_id == task_id))
        request = service.request_for(session, sub)
    report = worker.matcher.evaluate(item, [request], metadata.files, metadata.infohash)
    worker._record(item, metadata, report)
    choice = service.task_candidates(task_id)[0]["id"]
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def slow_search(self, *args, **kwargs):
        entered.set()
        await hold.wait()

    monkeypatch.setattr(demo, "search", slow_search)
    running = asyncio.create_task(worker.run_due(force=True))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(worker.choose(choice, 1), 2)
    await asyncio.wait_for(running, 2)
    assert not hold.is_set()
    with db.session() as session:
        assert session.scalar(select(SubtaskAsset).where(SubtaskAsset.subtask_id == sub.id)).pending


async def test_manual_choice_can_apply_one_release_to_all_matching_episodes(
    core, media, season, worker_setup
):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2, 3]),
        media,
        season,
        1,
    )
    torrent = json.dumps(["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]).encode()
    metadata = engine.inspect(DownloadSource(torrent=torrent))
    item = candidate(provider="demo", id="manual-batch", evidence=[])
    with db.session() as session:
        requests = [
            service.request_for(session, sub)
            for sub in session.scalars(select(Subtask).where(Subtask.task_id == task_id))
        ]
    report = worker.matcher.evaluate(item, requests, metadata.files, metadata.infohash)
    worker._record(item, metadata, report)
    choices = service.task_candidates(task_id)
    assert len(choices) == 1
    assert choices[0]["matched"] == 2 and choices[0]["total"] == 3

    result = await worker.choose_all(choices[0]["id"], 1)

    assert result == {"selected": 2, "total": 3, "skipped": 1}
    plan = engine.plans[metadata.infohash]
    assert [binding.subtask_id for binding in plan.bindings] == [1, 2]
    assert [binding.video_path for binding in plan.bindings] == [
        "Show.S01E01.1080p.mkv",
        "Show.S01E02.1080p.mkv",
    ]
    with db.session() as session:
        assert session.get(Subtask, 1).status == "starting"
        assert session.get(Subtask, 2).status == "starting"
        assert session.get(Subtask, 3).status == "queued"
        selected = list(
            session.scalars(select(CandidateDecision).where(CandidateDecision.action == "selected"))
        )
        assert len(selected) == 2


async def test_manual_choice_can_be_scoped_to_one_season(core, media, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    seasons = [
        SeasonInfo(
            number=number,
            episodes=[
                EpisodeInfo(
                    id=f"{number}:{episode}",
                    number=episode,
                    title=f"S{number}E{episode}",
                    air_date="2020-01-01",
                )
                for episode in ([1] if number == 1 else [1, 2])
            ],
        )
        for number in [1, 2]
    ]
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", seasons=[{"season": 1}, {"season": 2}]),
        media,
        seasons,
        1,
    )
    torrent = json.dumps(
        ["Example.Show.S01E01.1080p.mkv", "Example.Show.S02E01.1080p.mkv", "Example.Show.S02E02.1080p.mkv"]
    ).encode()
    metadata = engine.inspect(DownloadSource(torrent=torrent))
    item = candidate(provider="demo", id="season-batch", evidence=[])
    with db.session() as session:
        requests = [
            service.request_for(session, sub)
            for sub in session.scalars(select(Subtask).where(Subtask.task_id == task_id))
        ]
    worker._record(item, metadata, worker.matcher.evaluate(item, requests, metadata.files, metadata.infohash))

    choices = service.task_candidates(task_id, 2)
    assert len(choices) == 1
    assert choices[0]["matched"] == choices[0]["total"] == 2
    result = await worker.choose_all(choices[0]["id"], 1, season_number=2, task_id=task_id)

    assert result == {"selected": 2, "total": 2, "skipped": 0}
    assert [binding.subtask_id for binding in engine.plans[metadata.infohash].bindings] == [2, 3]
    with db.session() as session:
        assert session.get(Subtask, 1).status == "queued"
        assert session.get(Subtask, 2).status == "starting"
        assert session.get(Subtask, 3).status == "starting"


async def test_delete_task_preserves_shared_download_and_removes_last_consumer(
    core, media, season, worker_setup
):
    from lazarr.deletion import delete_task
    from lazarr.models import Task, Media

    _, db, _, service = core
    worker, engine, _ = worker_setup
    engine.remove = lambda h: engine.handles.pop(h, None)
    first = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    second = service.create_from_metadata(
        CreateTask(media_id="43", kind="tv", season=1, episodes=[1]),
        media.model_copy(update={"id": "43"}),
        season,
        2,
    )
    await worker.run_due()
    with db.session() as session:
        download = session.scalar(select(Download))
        root = Path(download.save_path)
    payload = root / "media.mkv"
    payload.write_bytes(b"test media")
    result = await delete_task(worker, first, 1, True)
    assert result["shared_downloads_kept"] == 1
    assert payload.exists() and engine.contains(download.infohash)
    assert len(engine.plans[download.infohash].bindings) == 1
    result = await delete_task(worker, second, 2, True)
    assert not result["cleanup_pending"]
    assert not root.exists() and not engine.contains(download.infohash)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(Download)) == 0
        assert session.scalar(select(func.count()).select_from(Media)) == 2


async def test_delete_task_without_media_keeps_files(core, media, season, worker_setup):
    from lazarr.deletion import delete_task

    _, db, _, service = core
    worker, engine, _ = worker_setup
    task = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        download = session.scalar(select(Download))
    path = Path(download.save_path) / "keep.mkv"
    path.write_bytes(b"keep")
    await delete_task(worker, task, 1)
    assert path.exists() and engine.paused[download.infohash]
    with db.session() as session:
        download = session.get(Download, download.id)
        assert not download.plan["bindings"]


async def test_delete_media_removes_download_record_but_preserves_files_by_default(
    core, media, season, worker_setup
):
    from lazarr.deletion import delete_media
    from lazarr.models import Media, Task

    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        identity = session.scalar(select(Media.id))
        download = session.scalar(select(Download))
        root = Path(download.save_path)
        infohash = download.infohash
    payload = root / "keep.mkv"
    payload.write_bytes(b"keep")

    result = await delete_media(worker, identity, 1)

    assert result["tasks_deleted"] == 1
    assert payload.exists() and not engine.contains(infohash)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(Media)) == 0
        assert session.scalar(select(func.count()).select_from(Task)) == 0
        assert session.scalar(select(func.count()).select_from(Download)) == 0


async def test_deletion_rejects_unsafe_paths(core, media, season, worker_setup, tmp_path):
    from lazarr.deletion import delete_task
    from lazarr.models import Task

    _, db, _, service = core
    worker, engine, _ = worker_setup
    task = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        download = session.scalar(select(Download))
        download.save_path = str(tmp_path)
    with pytest.raises(ValueError, match="путь"):
        await delete_task(worker, task, 1, True)
    with db.session() as session:
        assert session.get(Task, task) is not None
    assert tmp_path.exists()


async def test_failed_cleanup_retries_after_restart(core, media, season, worker_setup, monkeypatch):
    from lazarr.deletion import delete_task, cleanup
    import lazarr.deletion as deletion

    _, db, _, service = core
    worker, engine, _ = worker_setup
    engine.remove = lambda h: engine.handles.pop(h, None)
    task = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        root = Path(session.scalar(select(Download)).save_path)
    original = deletion.shutil.rmtree

    def fail(path):
        raise PermissionError("busy")

    monkeypatch.setattr(deletion.shutil, "rmtree", fail)
    assert (await delete_task(worker, task, 1, True))["cleanup_pending"]
    assert root.exists()
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    assert not cleanup(db)
    assert not root.exists()


async def test_cancelled_group_remains_due(core, media, season, worker_setup, monkeypatch):
    import asyncio

    _, db, _, service = core
    worker, _, demo = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(demo, "search", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_due()
    with db.session() as session:
        sub = session.scalar(select(Subtask))
        assert sub.next_search_at == 0 and sub.lease_until == 0


async def test_description_language_does_not_replace_unknown_probe(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        link = session.scalar(select(SubtaskAsset))
        report = dict(link.preflight)
        report["binding"] = {
            **report["binding"],
            "tracks": [
                {
                    "kind": "audio",
                    "language": "ru",
                    "language_source": "description",
                    "file_index": 1,
                    "path": "external.mka",
                    "embedded": False,
                }
            ],
        }
        link.preflight = report
        asset = session.get(MediaAsset, link.asset_id)
        download = session.get(Download, asset.download_id)

    async def probe(path, complete):
        return {
            "ok": True,
            "streams": [{"codec_type": "audio"}]
            if str(path).endswith(".mka")
            else [
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio", "tags": {"language": "jpn"}},
            ],
        }

    worker._probe = probe
    await worker._verify(link.id, asset.id, link.subtask_id, download.save_path, {"complete": True})
    with db.session() as session:
        link = session.get(SubtaskAsset, link.id)
        assert not link.current
        assert next(c for c in link.verification["criteria"] if c["field"] == "audio")["result"] == "UNKNOWN"


async def test_unknown_external_subtitle_language_is_detected_and_saved(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, _ = worker_setup
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1],
            requirements=Requirements(subtitle_languages=["ru"]),
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    with db.session() as session:
        link = session.scalar(select(SubtaskAsset))
        asset = session.get(MediaAsset, link.asset_id)
        download = session.get(Download, asset.download_id)
        subtitle = Path(download.save_path) / "episode.srt"
        text = (
            "Мы вместе идём на станцию. Поезд прибудет через несколько минут. "
            "Пожалуйста, возьми билет и подожди рядом со входом. Наши друзья уже идут сюда. "
            "Они нашли свободные места для всех и скоро мы отправимся в путешествие. "
        ) * 3
        subtitle.write_text("1\n00:00:01,000 --> 00:00:10,000\n" + text + "\n", encoding="utf-8")
        link.preflight = {
            **link.preflight,
            "binding": {
                **link.preflight["binding"],
                "tracks": [{"kind": "subtitle", "language": "und", "file_index": 1, "path": subtitle.name}],
            },
        }
        ids = link.id, asset.id, link.subtask_id
    await worker._verify(*ids, download.save_path, {"complete": True})
    with db.session() as session:
        link = session.get(SubtaskAsset, ids[0])
        assert link.preflight["binding"]["tracks"][0]["language"] == "ru"
        assert link.preflight["binding"]["tracks"][0]["language_source"] == "content"
        assert session.get(Subtask, ids[2]).missing_subtitle_languages == []
