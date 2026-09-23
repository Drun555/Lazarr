from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from lazarr.app import create_app
from lazarr.jellyfin import JellyfinDtoBatch, item_dto, object_id
from lazarr.library import LibraryService
from lazarr.models import Episode, Media, MediaAsset, LibraryAsset, User
from lazarr.services import CreateTask
from test_jellyfin import playable_episode, jellyfin_login


@contextmanager
def queries(db):
    calls = []

    def before(*args):
        calls.append(args[2])

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield calls
    finally:
        event.remove(db.engine, "before_cursor_execute", before)


def test_tasks_use_constant_queries_and_can_scope_media(core, media, season):
    _, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    second = media.model_copy(update={"id": "43", "title": "Second"})
    service.create_from_metadata(CreateTask(media_id="43", kind="tv", season=1), second, season, 1)
    with queries(db) as calls:
        rows = service.list_tasks()
    assert len(rows) == 2 and len(calls) <= 7
    with queries(db) as calls:
        selected = service.list_tasks(rows[0]["media_id"])
    assert selected == [rows[0]] and len(calls) <= 7


def test_episode_only_reads_its_own_paths_and_no_version_view(core, media, season, monkeypatch):
    playable_episode(core, media, season)
    config, db, plugins, service = core
    with db.session() as session:
        episode_id = session.scalar(select(Episode.id).order_by(Episode.id))
        user = session.get(User, 1)
        # A foreign asset must never be considered by a scoped episode request.
        other = Media(provider="tmdb", external_id="other", kind="movie", title="Other", metadata_json={})
        session.add(other)
        session.flush()
        original = session.scalar(select(MediaAsset))
        foreign = MediaAsset(
            media_id=other.id, download_id=original.download_id, video_index=99, path="foreign.mkv", probe={}
        )
        session.add(foreign)
        session.flush()
        session.add(
            LibraryAsset(
                media_id=other.id,
                asset_id=foreign.id,
                part_key="movie",
                preflight={},
                verification={"complete": True},
            )
        )
    library = LibraryService(db, plugins, service)
    ctx = SimpleNamespace(config=config, db=db, library=library, service=service)

    def forbidden(*args, **kwargs):
        raise AssertionError("Jellyfin DTO must not build the WebUI file/version view")

    monkeypatch.setattr(library, "_version", forbidden)
    from lazarr import jellyfin

    original_path = jellyfin.playable_path
    paths = []

    def checked(download, relative):
        paths.append(relative)
        return original_path(download, relative)

    monkeypatch.setattr(jellyfin, "playable_path", checked)
    item = item_dto(ctx, object_id("episode", episode_id), user, JellyfinDtoBatch(ctx, user))
    assert item["Type"] == "Episode"
    assert "foreign.mkv" not in paths


def test_empty_nextup_does_not_build_details_and_get_does_not_enrich(core, media, season, monkeypatch):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)

        def forbidden(*args, **kwargs):
            raise AssertionError("Unexpected work on empty history")

        monkeypatch.setattr(client.app.state.ctx.library, "detail", forbidden)
        monkeypatch.setattr(client.app.state.ctx.library, "enrich", forbidden)
        response = client.get("/Shows/NextUp")
        assert response.status_code == 200
        assert response.json()["Items"] == []
        response = client.get("/Items", params={"IncludeItemTypes": "Series", "Limit": 0})
        assert response.status_code == 200
        assert response.json()["Items"] == []
        assert response.json()["TotalRecordCount"] == 1


def test_items_hydrate_only_page(core, media, season, monkeypatch):
    _, _, _, service = core
    for i in range(5):
        item = media.model_copy(update={"id": str(i), "title": f"Series {i}"})
        service.create_from_metadata(CreateTask(media_id=str(i), kind="tv", season=1), item, season, 1)
    from lazarr import jellyfin

    original = jellyfin.media_dto
    calls = []

    def counted(ctx, media, *args, **kwargs):
        calls.append(media.id)
        return original(ctx, media, *args, **kwargs)

    monkeypatch.setattr(jellyfin, "media_dto", counted)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        response = client.get(
            "/Items", params={"IncludeItemTypes": "Series", "SortBy": "SortName", "StartIndex": 2, "Limit": 1}
        )
        assert response.status_code == 200
        assert response.json()["TotalRecordCount"] == 5
        assert response.json()["Items"][0]["Name"] == "Series 2"
        assert len(calls) == 1


def test_metadata_retry_survives_service_restart(core):
    _, db, plugins, service = core
    library = LibraryService(db, plugins, service)
    assert library._reserve_refresh("media", 1)
    assert not LibraryService(db, plugins, service)._reserve_refresh("media", 1)


@pytest.mark.parametrize("detected", ["en", "und"])
def test_subtitle_analysis_persists_and_invalidates(core, media, season, monkeypatch, detected):
    from lazarr.preparation import prepare_subtitles
    from lazarr.subtitle_language import stored_subtitle_language

    _, path = playable_episode(core, media, season)
    db = core[1]
    with db.session() as session:
        asset = session.scalar(select(MediaAsset))
        asset_id = asset.id
        asset.tracks = [{"kind": "subtitle", "language": "und", "path": path.name}]
    calls = []

    def detect(*args):
        calls.append(args)
        return detected

    monkeypatch.setattr("lazarr.preparation.detect_subtitle_language", detect)
    ctx = SimpleNamespace(db=db)
    prepare_subtitles(ctx, asset_id)
    prepare_subtitles(ctx, asset_id)
    assert len(calls) == 1
    with db.session() as session:
        asset = session.get(MediaAsset, asset_id)
        assert stored_subtitle_language(asset, path) == detected
    path.write_text("changed content", encoding="utf-8")
    assert stored_subtitle_language(asset, path) == "und"
    prepare_subtitles(ctx, asset_id)
    assert len(calls) == 2
