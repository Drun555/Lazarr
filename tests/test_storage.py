import os
from pathlib import Path

import pytest
from sqlalchemy import select

from test_worker import worker_setup as worker_setup
from lazarr.models import ConfigEntry, Download, LibraryAsset, MediaAsset
from lazarr.services import CreateTask
from lazarr.storage import MANIFEST, check_links, component, migrate_sources, reconcile, source_directory


async def completed_library(core, media, season, worker_setup):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        download = session.scalar(select(Download))
    root = Path(download.save_path)
    for number in [1, 2]:
        (root / f"Show.S01E0{number}.1080p.mkv").write_bytes(f"episode {number}".encode())
    engine.completed = {1, 2}
    await worker.poll()
    return worker, root, root.parent.parent / "user"


async def test_publish_repair_and_delete_one_episode(core, media, season, worker_setup):
    from lazarr.deletion import delete_selection

    worker, source, user = await completed_library(core, media, season, worker_setup)
    links = sorted(user.rglob("*.mkv"))
    assert len(links) == 2
    assert all(p.is_symlink() and not os.path.isabs(os.readlink(p)) for p in links)
    assert "Season 01" in str(links[0]) and "S01E01 - Episode 1" in links[0].name
    links[0].unlink()
    reconcile(core[1])
    assert links[0].read_bytes() == b"episode 1"
    links[0].unlink()
    links[0].symlink_to("wrong-target")
    reconcile(core[1])
    assert links[0].read_bytes() == b"episode 1"
    await delete_selection(worker, 1, 1)
    assert not links[0].is_symlink()
    assert links[1].read_bytes() == b"episode 2"
    assert (source / "Show.S01E02.1080p.mkv").exists()


async def test_missing_source_removes_link_then_recovers(core, media, season, worker_setup):
    _, source, user = await completed_library(core, media, season, worker_setup)
    target = source / "Show.S01E01.1080p.mkv"
    link = next(user.rglob("*S01E01*.mkv"))
    target.unlink()
    reconcile(core[1])
    assert not link.is_symlink()
    target.write_bytes(b"restored")
    reconcile(core[1])
    assert link.read_bytes() == b"restored"


async def test_no_links_for_incomplete_files(core, media, season, worker_setup):
    _, db, _, service = core
    worker, _, _ = worker_setup
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    await worker.run_due()
    with db.session() as session:
        source = Path(session.scalar(select(Download)).save_path)
    (source / "Show.S01E01.1080p.mkv").write_bytes(b"partial")
    await worker.poll()
    assert not list((source.parent.parent / "user").rglob("*.mkv"))


async def test_first_version_wins_and_sidecars_are_adjacent(core, media, season, worker_setup):
    _, source, user = await completed_library(core, media, season, worker_setup)
    (source / "second.mkv").write_bytes(b"second release")
    (source / "ru.srt").write_text("subtitle")
    with core[1].session() as db:
        library = db.scalar(select(LibraryAsset).order_by(LibraryAsset.id))
        asset = db.get(MediaAsset, library.asset_id)
        library.preflight = {
            "binding": {
                "tracks": [
                    {"file_index": 7, "path": "ru.srt", "kind": "subtitle", "language": "ru", "forced": True}
                ]
            }
        }
        second = MediaAsset(
            media_id=asset.media_id, download_id=asset.download_id, video_index=9, path="second.mkv"
        )
        db.add(second)
        db.flush()
        db.add(
            LibraryAsset(
                media_id=library.media_id,
                episode_id=library.episode_id,
                part_key=library.part_key,
                asset_id=second.id,
                verification={"complete": True},
            )
        )
    reconcile(core[1])
    link = next(user.rglob("*S01E01*.mkv"))
    assert link.read_bytes() == b"episode 1"
    subtitle = next(user.rglob("*.srt"))
    assert subtitle.parent == link.parent and ".ru.forced." in subtitle.name
    assert subtitle.read_text() == "subtitle"


async def test_foreign_file_or_link_never_claimed(core, media, season, worker_setup, tmp_path):
    _, _, user = await completed_library(core, media, season, worker_setup)
    link = next(user.rglob("*S01E01*.mkv"))
    with core[1].session() as db:
        db.delete(db.get(ConfigEntry, MANIFEST))
    link.unlink()
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"mine")
    link.symlink_to(foreign)
    reconcile(core[1])
    reconcile(core[1])
    assert link.resolve() == foreign
    with core[1].session() as db:
        assert str(link) not in {e["path"] for e in db.get(ConfigEntry, MANIFEST).value["entries"]}


async def test_migration_resumes_after_rename_before_db_commit(
    core, media, season, worker_setup, monkeypatch
):
    _, source, user = await completed_library(core, media, season, worker_setup)
    old = source.parent.parent / "series" / source.name
    old.parent.mkdir()
    source.rename(old)
    with core[1].session() as db:
        download = db.scalar(select(Download))
        download.save_path = str(old)
        identity = download.id
        db.add(
            ConfigEntry(
                key="cleanup.test",
                value={
                    "directories": [],
                    "resume_files": [],
                    "files": [{"root": str(old), "path": "unused.txt"}],
                },
            )
        )
    rename = Path.rename

    def crash(path, destination):
        rename(path, destination)
        raise OSError("simulated crash after rename")

    monkeypatch.setattr(Path, "rename", crash)
    with pytest.raises(OSError, match="simulated crash"):
        migrate_sources(core[1])
    assert not old.exists() and source.is_dir()
    monkeypatch.setattr(Path, "rename", rename)
    migrate_sources(core[1])
    migrate_sources(core[1])
    with core[1].session() as db:
        assert db.get(Download, identity).save_path == str(source)
        assert db.get(ConfigEntry, f"storage.move.{identity}") is None
        assert db.get(ConfigEntry, "cleanup.test").value["files"][0]["root"] == str(source)
    reconcile(core[1])
    assert all(p.is_file() for p in user.rglob("*.mkv"))


async def test_migration_collision_keeps_original(core, media, season, worker_setup):
    _, source, _ = await completed_library(core, media, season, worker_setup)
    old = source.parent.parent / "series" / source.name
    old.mkdir(parents=True)
    (old / "original").write_text("keep")
    with core[1].session() as db:
        db.scalar(select(Download)).save_path = str(old)
    with pytest.raises(FileExistsError):
        migrate_sources(core[1])
    assert (old / "original").read_text() == "keep"


async def test_directory_symlink_cannot_escape_user_root(core, media, season, worker_setup, tmp_path):
    _, _, user = await completed_library(core, media, season, worker_setup)
    series = user / "Series"
    moved = tmp_path / "elsewhere"
    series.rename(moved)
    series.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        reconcile(core[1])
    assert list(moved.rglob("*.mkv"))


def test_unsupported_filesystem_reports_actionable_error(tmp_path, monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError("operation not supported")

    monkeypatch.setattr(Path, "symlink_to", unsupported)
    with pytest.raises(OSError, match="Docker Desktop/WSL2"):
        check_links(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["CON", "con.txt", "LPT1", "nul", "a:b/c\\d?*<>|", "x" * 300, " . "])
def test_portable_components(name):
    value = component(name)
    assert len(value.encode()) <= 111
    assert not any(c in value for c in '<>:"/\\|?*')
    assert not value.endswith((".", " "))
    assert value.casefold().split(".")[0] not in {"con", "lpt1", "nul"}


def test_legacy_roots_share_source(tmp_path):
    assert source_directory(tmp_path / "movies", "abc") == tmp_path / "source" / "abc"
    assert source_directory(tmp_path / "series", "abc") == tmp_path / "source" / "abc"


async def test_publication_retries_after_filesystem_failure(core, media, season, worker_setup, monkeypatch):
    _, _, user = await completed_library(core, media, season, worker_setup)
    link = next(user.rglob("*S01E01*.mkv"))
    link.unlink()
    original = Path.symlink_to

    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "symlink_to", unavailable)
    with pytest.raises(OSError):
        reconcile(core[1])
    with core[1].session() as db:
        assert db.get(ConfigEntry, "storage.status").value["links_error"]
    monkeypatch.setattr(Path, "symlink_to", original)
    reconcile(core[1])
    assert link.read_bytes() == b"episode 1"


def test_storage_status_is_authenticated_and_reports_errors(core):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.storage import set_status
    from test_api import login

    set_status(core[1], "migration_error", "Source volume unavailable")
    with TestClient(create_app(core[0])) as client:
        assert client.get("/api/v1/storage").status_code == 401
        login(client)
        response = client.get("/api/v1/storage")
        assert response.status_code == 200
        status = response.json()
        assert status["migration_error"] == "Source volume unavailable"
        assert Path(status["roots"][0]["source"]).name == "source"
        assert Path(status["roots"][0]["user"]).name == "user"
