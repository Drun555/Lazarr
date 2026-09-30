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


async def test_nfo_updates_without_touching_video_and_unchanged_files(core, media, season, worker_setup):
    from xml.etree import ElementTree as ET
    from lazarr.models import Media

    media.overview = "Plot <one> & two"
    media.people = [{"Name": "Actor", "Type": "Actor", "Role": "Hero"}]
    media.community_rating = 8.5
    _, source, user = await completed_library(core, media, season, worker_setup)
    show = next(user.rglob("tvshow.nfo"))
    parsed = ET.parse(show)
    assert parsed.findtext("plot") == "Plot <one> & two"
    assert parsed.findtext("actor/role") == "Hero"
    assert parsed.findtext("ratings/rating/value") == "8.5"
    assert parsed.find("uniqueid[@type='tmdb']").text == "42"
    episode = next(user.rglob("*S01E01*.nfo"))
    assert ET.parse(episode).findtext("episode") == "1"
    stamp = show.stat().st_mtime_ns
    reconcile(core[1])
    assert show.stat().st_mtime_ns == stamp
    with core[1].session() as db:
        row = db.scalar(select(Media))
        row.metadata_json = {**row.metadata_json, "overview": "New plot"}
    reconcile(core[1])
    assert ET.parse(show).findtext("plot") == "New plot"
    assert (source / "Show.S01E01.1080p.mkv").read_bytes() == b"episode 1"


async def test_anime_reclassification_and_numbering_move_owned_files(core, media, season, worker_setup):
    from xml.etree import ElementTree as ET
    from lazarr.models import Media

    _, source, user = await completed_library(core, media, season, worker_setup)
    old = next(user.rglob("tvshow.nfo")).parent
    foreign = old / "notes.txt"
    foreign.write_text("keep")
    with core[1].session() as db:
        row = db.scalar(select(Media))
        row.metadata_json = {
            **row.metadata_json,
            "genre_ids": [16],
            "original_language": "ja",
            "episode_numbering": {"1:1": [{"season": 2, "episode": 14}]},
        }
    reconcile(core[1])
    assert not list((user / "Series").rglob("*.mkv"))
    assert not list((user / "Series").rglob("*.nfo"))
    assert foreign.read_text() == "keep"
    episode = next((user / "Anime").rglob("*S02E14*.nfo"))
    assert ET.parse(episode).findtext("season") == "2"
    assert ET.parse(episode).findtext("episode") == "14"
    assert ET.parse(episode.parent / "season.nfo").findtext("seasonnumber") == "2"
    assert episode.with_suffix(".mkv").read_bytes() == b"episode 1"
    assert (source / "Show.S01E01.1080p.mkv").is_file()


async def test_sidecar_cleanup_preserves_foreign_metadata(core, media, season, worker_setup):
    from lazarr.deletion import delete_selection

    worker, _, user = await completed_library(core, media, season, worker_setup)
    episode = next(user.rglob("*S01E01*.nfo"))
    with core[1].session() as db:
        row = db.get(ConfigEntry, MANIFEST)
        row.value = {"entries": [e for e in row.value["entries"] if e["path"] != str(episode)]}
    episode.write_text("foreign NFO")
    reconcile(core[1])
    assert episode.read_text() == "foreign NFO"
    await delete_selection(worker, 1, 1)
    assert episode.read_text() == "foreign NFO"
    assert next(user.rglob("*S01E02*.nfo")).is_file()


async def test_owned_metadata_symlink_never_overwrites_target(core, media, season, worker_setup, tmp_path):
    _, _, user = await completed_library(core, media, season, worker_setup)
    nfo = next(user.rglob("tvshow.nfo"))
    nfo.unlink()
    foreign = tmp_path / "private"
    foreign.write_text("untouched")
    nfo.symlink_to(foreign)
    with pytest.raises(ValueError, match="symlink"):
        reconcile(core[1])
    assert foreign.read_text() == "untouched"


async def test_artwork_cache_failure_and_deletion_during_fetch(core, media, season, worker_setup):
    import httpx
    from types import SimpleNamespace
    from lazarr.library_metadata import sync_artwork
    from lazarr.models import Media

    media.poster = "https://image.tmdb.org/t/p/w342/example.jpg"
    _, _, user = await completed_library(core, media, season, worker_setup)
    image = next(user.rglob("tvshow.nfo")).parent / "poster.jpg"
    jpeg = b"\xff\xd8\xfftest"
    calls = []

    def transport(request):
        calls.append(request.url)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg)

    ctx = SimpleNamespace(db=core[1], config=core[0], poster_transport=httpx.MockTransport(transport))
    await sync_artwork(ctx)
    assert image.read_bytes() == jpeg
    stamp = image.stat().st_mtime_ns
    await sync_artwork(ctx)
    assert len(calls) == 1 and image.stat().st_mtime_ns == stamp
    with core[1].session() as db:
        row = db.scalar(select(Media))
        row.metadata_json = {**row.metadata_json, "poster": "https://image.tmdb.org/t/p/w342/new.jpg"}
    reconcile(core[1])
    ctx.poster_transport = httpx.MockTransport(lambda _: httpx.Response(503))
    await sync_artwork(ctx)
    assert image.read_bytes() == jpeg
    with core[1].session() as db:
        assert db.get(ConfigEntry, "storage.status").value["metadata_errors"]

    def delete_during_fetch(request):
        with core[1].session() as db:
            row = db.scalar(select(Media))
            row.metadata_json = {**row.metadata_json, "poster": None}
        reconcile(core[1])
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=jpeg)

    ctx.poster_transport = httpx.MockTransport(delete_during_fetch)
    await sync_artwork(ctx)
    assert not image.exists()


def test_movie_nfo_and_anime_movie_stays_in_movies():
    from lazarr.library_metadata import media_nfo
    from lazarr.library import library_kind
    from lazarr.models import Media

    movie = Media(
        provider="tmdb",
        external_id="10",
        kind="movie",
        title="Movie",
        year=2020,
        metadata_json={"genre_ids": [16], "original_language": "ja", "collection": "Collection"},
    )
    assert library_kind(movie) == "movies"
    nfo = media_nfo(movie)
    assert nfo.tag == "movie" and nfo.findtext("set/name") == "Collection"
