from pathlib import Path

from sqlalchemy import select

from test_worker import worker_setup as worker_setup
from lazarr.models import Download, LibraryAsset, Subtask, SubtaskAsset
from lazarr.services import CreateTask
from lazarr.storage import reconcile


async def test_playable_download_published_before_completion(core, media, season, worker_setup, monkeypatch):
    _, database, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with database.session() as db:
        source = Path(db.scalar(select(Download)).save_path)
    original = source / "Show.S01E01.1080p.mkv"
    original.write_bytes(b"buffered episode")
    user = source.parent.parent / "user"
    snapshot = engine.snapshot

    def buffered(infohash):
        stats = snapshot(infohash)
        for binding in stats["bindings"].values():
            binding.update(buffer_ready=True, downloaded=50, progress=0.5)
        return stats

    monkeypatch.setattr(engine, "snapshot", buffered)
    await worker.poll()
    with database.session() as db:
        assert db.scalar(select(Subtask)).status == "ready"
        link = db.scalar(select(SubtaskAsset))
        assert link.pending and not link.current
        assert db.scalar(select(LibraryAsset)) is None
    exported = next(user.rglob("*.mkv"))
    assert exported.is_symlink()
    assert exported.read_bytes() == b"buffered episode"
    assert list(user.rglob("*.nfo"))
    exported.unlink()
    reconcile(database)
    assert exported.is_file()

    # The same link exposes newly downloaded bytes and survives completion.
    original.write_bytes(b"complete episode")
    monkeypatch.setattr(engine, "snapshot", snapshot)
    engine.completed = {1}
    await worker.poll()
    with database.session() as db:
        assert db.scalar(select(Subtask)).status == "done"
    assert list(user.rglob("*.mkv")) == [exported]
    assert exported.read_bytes() == b"complete episode"


async def test_unselected_buffered_download_removed(core, media, season, worker_setup, monkeypatch):
    _, database, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with database.session() as db:
        source = Path(db.scalar(select(Download)).save_path)
    (source / "Show.S01E01.1080p.mkv").write_bytes(b"partial")
    snapshot = engine.snapshot

    def buffered(infohash):
        stats = snapshot(infohash)
        for binding in stats["bindings"].values():
            binding["buffer_ready"] = True
        return stats

    monkeypatch.setattr(engine, "snapshot", buffered)
    await worker.poll()
    user = source.parent.parent / "user"
    assert list(user.rglob("*.mkv"))
    with database.session() as db:
        db.scalar(select(SubtaskAsset)).pending = False
        db.scalar(select(Subtask)).status = "needs_selection"
    reconcile(database)
    assert not list(user.rglob("*.mkv"))
    assert not list(user.rglob("*.nfo"))
