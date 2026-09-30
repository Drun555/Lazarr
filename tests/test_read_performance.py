from test_worker import worker_setup as worker_setup
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from lazarr.library import LibraryService
from lazarr.models import MediaAsset
from lazarr.services import CreateTask


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


def test_metadata_retry_survives_service_restart(core):
    _, db, plugins, service = core
    library = LibraryService(db, plugins, service)
    assert library._reserve_refresh("media", 1)
    assert not LibraryService(db, plugins, service)._reserve_refresh("media", 1)


@pytest.mark.parametrize("detected", ["en", "und"])
async def test_subtitle_analysis_persists_and_invalidates(
    core, media, season, worker_setup, monkeypatch, detected
):
    from lazarr.preparation import prepare_subtitles
    from lazarr.subtitle_language import stored_subtitle_language

    from test_storage import completed_library

    _, source, _ = await completed_library(core, media, season, worker_setup)
    path = source / "unknown.srt"
    path.write_text("subtitle")
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
