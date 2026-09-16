import json
from pathlib import Path

import pytest
from sqlalchemy import select

from conftest import candidate
from test_worker import worker_setup as worker_setup
from lazarr.library import LibraryService
from lazarr.models import CandidateDecision, ConfigEntry, Download, LibraryAsset, MediaAsset, SubtaskAsset
from lazarr.sdk import DownloadSource
from lazarr.services import CreateTask


def record_choice(worker, engine, service, db, sub_id, paths, identity):
    metadata = engine.inspect(DownloadSource(torrent=json.dumps(paths).encode()))
    from lazarr.models import Subtask

    with db.session() as session:
        request = service.request_for(session, session.get(Subtask, sub_id))
    item = candidate(provider="demo", id=identity)
    report = worker.matcher.evaluate(item, [request], metadata.files, metadata.infohash)
    release_id, _ = worker._record(item, metadata, report)
    with db.session() as session:
        return session.scalar(
            select(CandidateDecision.id).where(
                CandidateDecision.release_id == release_id,
                CandidateDecision.subtask_id == sub_id,
            )
        )


@pytest.mark.parametrize("complete", [False, True])
async def test_delete_episode_selection_preserves_other_series(core, media, season, worker_setup, complete):
    from lazarr.deletion import delete_selection
    from lazarr.models import Subtask

    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    if complete:
        engine.completed = {1, 2}
        await worker.poll()
    with db.session() as session:
        download = session.scalar(select(Download))
        root = Path(download.save_path)
    first, second = root / "Show.S01E01.1080p.mkv", root / "Show.S01E02.1080p.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    assert not (await delete_selection(worker, 1, 1))["cleanup_pending"]
    assert not first.exists() and second.read_bytes() == b"second"
    with db.session() as session:
        assert session.get(Subtask, 1).status == "removed"
        assert not session.scalar(select(SubtaskAsset).where(SubtaskAsset.subtask_id == 1))
        assert not session.scalar(select(LibraryAsset).where(LibraryAsset.episode_id == 1))
        assert not session.scalar(
            select(CandidateDecision).where(
                CandidateDecision.subtask_id == 1, CandidateDecision.action == "selected"
            )
        )
    assert not await worker.due_groups(force=True)
    await worker.run_due()
    assert not first.exists()
    await delete_selection(worker, 1, 1)  # Idempotent.
    await delete_selection(worker, 2, 1)
    assert not root.exists() and download.infohash not in engine.handles
    choice = record_choice(worker, engine, service, db, 1, ["New.S01E01.mkv"], "after-delete")
    await worker.choose(choice, 1, video_index=0)
    with db.session() as session:
        assert session.get(Subtask, 1).status == "starting"


async def test_mapping_reads_actual_tracks_of_selected_video(core, media, season, worker_setup):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from test_api import login

    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    paths = ["Show.S01E10.mkv", "Show.S01E2.mkv", "Show.S01E1.mkv", "Show.S01E10.srt", "Show.S01E2.srt"]
    first = record_choice(worker, engine, service, db, 1, paths, "mapping")
    second = record_choice(worker, engine, service, db, 2, paths, "mapping")
    await worker.choose(first, 1, video_index=2, track_indices=[4])
    await worker.choose(second, 1, video_index=1, track_indices=[3])
    with TestClient(create_app(core[0])) as client:
        login(client)
        assert client.get(f"/api/v1/candidates/{first}/selection").json()["video_index"] == 2
        other = client.get(f"/api/v1/candidates/{first}/selection?video_index=1").json()
        assert other["video_index"] == 1
        assert [t["file_index"] for t in other["tracks"]] == [3]
        assert client.get(f"/api/v1/candidates/{first}/selection?video_index=0").json() is None
        original_engine = client.app.state.ctx.engine
        try:
            client.app.state.ctx.engine = engine
            files = client.get(f"/api/v1/candidates/{first}/files").json()
            assert [f["index"] for f in files] == list(range(5))
            assert [f["episode_order"] for f in files] == [[1, 10], [1, 2], [1, 1], [1, 10], [1, 2]]
        finally:
            client.app.state.ctx.engine = original_engine


@pytest.mark.parametrize("whole_season", [False, True])
async def test_delete_library_episode_without_task(core, media, season, worker_setup, whole_season):
    from lazarr.deletion import delete_selection, delete_task, delete_season

    _, db, _, service = core
    worker, engine, _ = worker_setup
    task = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    engine.completed = {1, 2}
    await worker.poll()
    with db.session() as session:
        root = Path(session.scalar(select(Download)).save_path)
    first, second = root / "Show.S01E01.1080p.mkv", root / "Show.S01E02.1080p.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    await delete_task(worker, task, 1)
    if whole_season:
        await delete_season(worker, 1, 1, 1)
        assert not root.exists()
        return
    await delete_selection(worker, "1", 1, media_id=1)
    assert not first.exists() and second.exists()
    await delete_selection(worker, "2", 1, media_id=1)
    assert not root.exists()


def test_episode_delete_api_requires_auth_and_csrf(core, media, season):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.models import Subtask
    from test_api import login

    config, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    service.edit(1, 1, paused=True)
    with TestClient(create_app(config)) as client:
        url = "/api/v1/subtasks/1/selection"
        assert client.delete(url).status_code in {401, 403}
        login(client)
        assert client.delete(url, headers={"x-csrf-token": "wrong"}).status_code == 403
        assert client.delete(url).status_code == 200
        assert client.delete("/api/v1/libraries/media/1/episodes/1/selection").status_code == 200
        with db.session() as session:
            assert session.get(Subtask, 1).status == "removed"
        url = "/api/v1/libraries/media/1/seasons/1"
        assert client.delete(url, headers={"x-csrf-token": "wrong"}).status_code == 403
        assert client.delete(url).status_code == 200
        assert client.get("/api/v1/tasks").json() == []


@pytest.mark.parametrize("complete", [False, True])
async def test_delete_season_preserves_shared_download_and_removes_last_task(
    core, media, worker_setup, complete
):
    from lazarr.deletion import delete_season
    from lazarr.models import Subtask, Task, TaskSeason
    from test_task_seasons import season_info

    _, db, _, service = core
    worker, engine, _ = worker_setup
    payload = CreateTask(media_id="42", kind="tv", seasons=[{"season": 1}, {"season": 2}])
    service.create_from_metadata(payload, media, [season_info(1), season_info(2)], 1)
    paths = [f"Show.S{s:02}E{e:02}.mkv" for s in (1, 2) for e in (1, 2)]
    for index in range(4):
        choice = record_choice(worker, engine, service, db, index + 1, paths, "both-seasons")
        await worker.choose(choice, 1, video_index=index)
    if complete:
        engine.completed = {1, 2, 3, 4}
        await worker.poll()
    with db.session() as session:
        root = Path(session.scalar(select(Download)).save_path)
    for path in paths:
        (root / path).write_bytes(b"video")
    result = await delete_season(worker, 1, 1, 1)
    assert not result["cleanup_pending"]
    assert [p for p in paths if (root / p).exists()] == paths[2:]
    with db.session() as session:
        assert len(list(session.scalars(select(Subtask)))) == 2
        assert len(list(session.scalars(select(TaskSeason)))) == 1
        assert len(session.scalar(select(Download)).plan["bindings"]) == 2
    assert [s["season"] for s in service.list_tasks()[0]["seasons"]] == [2]
    await delete_season(worker, 1, 1, 1)  # Repeating does not affect season 2.
    await delete_season(worker, 1, 2, 1)
    assert not root.exists()
    with db.session() as session:
        assert not session.scalar(select(Task))
        assert not session.scalar(select(TaskSeason))
        assert not session.scalar(select(Subtask))
        assert not session.scalar(select(LibraryAsset))
    # Metadata remains, and adding the season back creates a usable task.
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season_info(1), 1)
    assert len(service.list_tasks()[0]["subtasks"]) == 2


@pytest.mark.parametrize("complete", [False, True])
async def test_replacement_removes_only_retired_episode_files(core, media, season, worker_setup, complete):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    if complete:
        engine.completed = {1, 2}
        await worker.poll()
    with db.session() as session:
        old = session.scalar(select(Download))
        root, old_hash = Path(old.save_path), old.infohash
    first, second = root / "Show.S01E01.1080p.mkv", root / "Show.S01E02.1080p.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    choice = record_choice(worker, engine, service, db, 1, ["New.S01E01.1080p.mkv"], "new")
    await worker.choose(choice, 1, video_index=0)
    assert not first.exists() and second.read_bytes() == b"second"
    assert old_hash in engine.handles
    with db.session() as session:
        assert len(list(session.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id == 1)))) == 1
        assert not session.scalar(select(LibraryAsset).where(LibraryAsset.episode_id == 1))
        assert len(session.get(Download, old.id).plan["bindings"]) == 1
    files = LibraryService(db, worker.plugins, service).detail(1)["episodes"][0]["files"]
    assert len(files) == 1 and files[0]["path"] == "New.S01E01.1080p.mkv"
    # Moving the last consumer removes the old torrent directory as well.
    choice = record_choice(worker, engine, service, db, 2, ["New.S01E02.1080p.mkv"], "new2")
    await worker.choose(choice, 1, video_index=0)
    assert not root.exists() and old_hash not in engine.handles


@pytest.mark.parametrize("paused", [False, True])
async def test_remap_current_torrent_deletes_old_video_and_tracks(core, media, season, worker_setup, paused):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    task_id = service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    paths = ["Show.S01E01.1080p.mkv", "Other.S01E01.1080p.mkv", "old.ru.srt", "new.ru.srt"]
    choice = record_choice(worker, engine, service, db, 1, paths, "remap")
    await worker.choose(choice, 1, video_index=0, track_indices=[2])
    engine.completed = {1}
    await worker.poll()
    with db.session() as session:
        root = Path(session.scalar(select(Download)).save_path)
    for name in paths:
        (root / name).write_bytes(name.encode())
    if paused:
        service.edit(task_id, 1, paused=True)
    await worker.choose(choice, 1, video_index=1, track_indices=[3])
    assert not (root / paths[0]).exists() and not (root / paths[2]).exists()
    assert (root / paths[1]).exists() and (root / paths[3]).exists()
    with db.session() as session:
        link = session.scalar(select(SubtaskAsset))
        assert link.pending and not link.current
        assert link.preflight["binding"]["video_index"] == 1
        assert [t["file_index"] for t in link.preflight["binding"]["tracks"]] == [3]
        assert not session.scalar(select(LibraryAsset))
        assert len(list(session.scalars(select(MediaAsset)))) == 1
    assert list(engine.paused.values()) == [paused]
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from test_api import login

    with TestClient(create_app(core[0])) as client:
        assert client.get(f"/api/v1/candidates/{choice}/selection").status_code == 401
        login(client)
        selected = client.get(f"/api/v1/candidates/{choice}/selection").json()
        assert selected["video_index"] == 1
        assert [track["file_index"] for track in selected["tracks"]] == [3]


async def test_file_cleanup_failure_retries_before_restoring_shared_torrent(
    core, media, season, worker_setup, monkeypatch
):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        old = session.scalar(select(Download))
        path = Path(old.save_path) / "Show.S01E01.1080p.mkv"
    path.write_bytes(b"old")
    original = Path.unlink

    def fail(self, *args, **kwargs):
        if self == path:
            raise PermissionError("busy")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    choice = record_choice(worker, engine, service, db, 1, ["New.S01E01.1080p.mkv"], "new")
    await worker.choose(choice, 1, video_index=0)
    assert old.infohash not in engine.handles and path.exists()
    with db.session() as session:
        assert session.scalar(select(ConfigEntry).where(ConfigEntry.key.like("cleanup.%")))
    monkeypatch.setattr(Path, "unlink", original)
    await worker.poll()
    assert not path.exists() and old.infohash in engine.handles


async def test_shared_video_is_kept_for_other_consumer(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    for identity in ("42", "43"):
        service.create_from_metadata(
            CreateTask(media_id=identity, kind="tv", season=1, episodes=[1]),
            media.model_copy(update={"id": identity}),
            season,
            1,
        )
    await worker.run_due()
    engine.completed = {1, 2}
    await worker.poll()
    with db.session() as session:
        old = session.scalar(select(Download))
        path = Path(old.save_path) / "Show.S01E01.1080p.mkv"
    path.write_bytes(b"shared")
    choice = record_choice(worker, engine, service, db, 1, ["New.S01E01.1080p.mkv"], "new")
    await worker.choose(choice, 1, video_index=0)
    assert path.read_bytes() == b"shared"
    with db.session() as session:
        assert session.scalar(select(LibraryAsset).where(LibraryAsset.media_id == 2))


async def test_invalid_mapping_preserves_selection_and_files(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        download = session.scalar(select(Download))
        choice = session.scalar(select(CandidateDecision.id))
        old_link = session.scalar(select(SubtaskAsset.id))
    path = Path(download.save_path) / "Show.S01E01.1080p.mkv"
    path.write_bytes(b"keep")
    with pytest.raises(ValueError, match="видеофайл"):
        await worker.choose(choice, 1, video_index=99)
    assert path.read_bytes() == b"keep"
    with db.session() as session:
        assert session.scalar(select(SubtaskAsset.id)) == old_link


async def test_restore_removes_legacy_history_and_keeps_current_file(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    engine.completed = {1}
    await worker.poll()
    with db.session() as session:
        current = session.scalar(select(SubtaskAsset))
        download = session.scalar(select(Download))
        root = Path(download.save_path)
        obsolete_root = root.parent / ("a" * 64)
        obsolete_root.mkdir()
        old_download = Download(
            infohash="a" * 64,
            release_id=download.release_id,
            save_path=str(obsolete_root),
            torrent_file=download.torrent_file,
            plan=download.plan,
        )
        session.add(old_download)
        session.flush()
        old_asset = MediaAsset(
            media_id=1, download_id=old_download.id, video_index=0, path="Show.S01E01.1080p.mkv"
        )
        session.add(old_asset)
        session.flush()
        session.add(
            SubtaskAsset(
                subtask_id=1,
                asset_id=old_asset.id,
                current=False,
                pending=False,
                preflight=current.preflight,
                verification=current.verification,
            )
        )
        session.add(
            LibraryAsset(
                media_id=1,
                episode_id=1,
                part_key="episode:1",
                asset_id=old_asset.id,
                preflight=current.preflight,
                verification=current.verification,
            )
        )
    (obsolete_root / "Show.S01E01.1080p.mkv").write_bytes(b"obsolete")
    current_file = root / "Show.S01E01.1080p.mkv"
    current_file.write_bytes(b"current")
    await worker.restore()
    assert not obsolete_root.exists() and current_file.read_bytes() == b"current"
    with db.session() as session:
        links = list(session.scalars(select(SubtaskAsset)))
        assert len(links) == 1 and links[0].current
        assert len(list(session.scalars(select(LibraryAsset)))) == 1


@pytest.mark.parametrize("paused", [False, True])
async def test_explicit_alternative_search_keeps_completed_selection(
    core, media, season, worker_setup, paused
):
    from lazarr.config import Requirements
    from lazarr.models import Subtask

    _, db, _, service = core
    worker, engine, demo = worker_setup
    task = service.create_from_metadata(
        CreateTask(
            media_id="42", kind="tv", season=1, episodes=[1], requirements=Requirements(max_resolution=2160)
        ),
        media,
        season,
        1,
    )
    await worker.run_due()
    engine.completed = {1}
    await worker.poll()
    if paused:
        service.edit(task, 1, paused=True)
    with db.session() as session:
        original_link = session.scalar(select(SubtaskAsset.id))
        original_download = session.scalar(select(Download.id))
    demo.quality = 2160
    await worker.search_alternatives(1)
    assert len(service.candidates(1)) == 2
    progress = worker.progress.tasks[task]
    assert progress["state"] == "finished" and progress["groups_done"] == 1
    assert any(event["stage"] == "alternatives" for event in progress["history"])
    with db.session() as session:
        assert session.scalar(select(SubtaskAsset.id)) == original_link
        assert session.scalar(select(Download.id)) == original_download
        assert session.get(Subtask, 1).status == "done"
        assert len(list(session.scalars(select(Download)))) == 1
        assert not session.scalar(select(ConfigEntry).where(ConfigEntry.key.like("cleanup.%")))
