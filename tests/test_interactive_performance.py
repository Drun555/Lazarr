"""Regression checks for work amplified by season size and background activity."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from conftest import candidate
from lazarr.library import LibraryService
from lazarr.models import CandidateDecision, Download, Subtask, SubtaskAsset
from lazarr.sdk import DownloadSource, EpisodeInfo, EvaluationReport, SeasonInfo
from lazarr.services import CreateTask
from test_read_performance import queries
from test_worker import worker_setup as worker_setup


def season_choices(core, media, worker_setup, count=24):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    season = SeasonInfo(number=1, episodes=[EpisodeInfo(id=str(n), number=n) for n in range(1, count + 1)])
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    metadata = engine.inspect(
        DownloadSource(
            torrent=json.dumps([f"Show.S01E{n:02d}.1080p.mkv" for n in range(1, count + 1)]).encode()
        )
    )
    release_id, _ = worker._record(candidate(), metadata, EvaluationReport(evaluations=[]))
    choices = []
    with db.session() as session:
        for index, sub in enumerate(session.scalars(select(Subtask).order_by(Subtask.id))):
            decision = CandidateDecision(subtask_id=sub.id, release_id=release_id, report={})
            session.add(decision)
            session.flush()
            choices.append(dict(decision_id=decision.id, video_index=index, track_indices=[]))
    return choices


async def test_season_batch_restarts_torrent_once_and_preserves_swapped_files(
    core, media, worker_setup, monkeypatch
):
    worker, engine, _ = worker_setup
    choices = season_choices(core, media, worker_setup)
    monkeypatch.setattr(engine, "add", Mock(wraps=engine.add))
    monkeypatch.setattr(engine, "remove", Mock(wraps=engine.remove))
    before = engine.inspect_calls
    await worker.choose_many(choices, 1)
    assert engine.inspect_calls - before == 1
    assert engine.add.call_count == 1
    with core[1].session() as session:
        download = session.scalar(select(Download))
        paths = [Path(download.save_path) / f["path"] for f in download.plan["files"]]
    for path in paths:
        path.write_bytes(b"preserve shared media")
    swapped = [{**choice, "video_index": len(choices) - 1 - i} for i, choice in enumerate(choices)]
    await worker.choose_many(swapped, 1)
    assert engine.remove.call_count == 1
    assert engine.add.call_count == 2
    assert all(path.read_bytes() == b"preserve shared media" for path in paths)
    assert len(next(iter(engine.plans.values())).bindings) == len(choices)
    with core[1].session() as session:
        assert len(list(session.scalars(select(SubtaskAsset)))) == len(choices)
        assert all(d.action == "selected" for d in session.scalars(select(CandidateDecision)))


async def test_invalid_batch_does_not_submit_first_episode(core, media, worker_setup):
    worker, engine, _ = worker_setup
    choices = season_choices(core, media, worker_setup, count=2)
    choices[-1]["video_index"] = 999
    with pytest.raises(ValueError, match="видеофайл"):
        await worker.choose_many(choices, 1)
    assert not engine.handles
    with core[1].session() as session:
        assert session.scalar(select(Download)) is None


async def test_manual_release_does_not_wait_for_background_search(
    core, media, season, worker_setup, monkeypatch
):
    _, db, plugins, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    monkeypatch.setattr(plugins, "manual_candidate", lambda url: candidate(provider="demo"))
    async with worker.lock:
        release_id = await asyncio.wait_for(
            worker.add_manual_task_candidate(1, "https://example.test/release", 1, return_release=True), 2
        )
    assert release_id and engine.inspect_calls == 1
    assert not engine.handles  # Adding files to the editor does not start a download.
    with db.session() as session:
        assert session.scalar(select(CandidateDecision)) is None


async def test_library_preview_does_not_touch_video_paths(core, media, worker_setup, monkeypatch):
    worker, _, _ = worker_setup
    choices = season_choices(core, media, worker_setup)
    await worker.choose_many(choices, 1)
    library = LibraryService(core[1], core[2], core[3])

    def forbidden(*args, **kwargs):
        raise AssertionError("Library preview must use persisted metadata, not resolve media paths")

    monkeypatch.setattr(Path, "resolve", forbidden)
    detail = library.detail(1)
    assert len(detail["episodes"]) == len(choices)
    assert all(part["files"] for part in detail["episodes"])


def test_library_overview_query_count_does_not_grow_with_media(core, media, season):
    _, db, plugins, service = core
    for n in range(8):
        item = media.model_copy(update={"id": str(n + 100), "title": f"Show {n}"})
        service.create_from_metadata(CreateTask(media_id=item.id, kind="tv", season=1), item, season, 1)
    with queries(db) as calls:
        result = LibraryService(db, plugins, service).list()
    assert sum(len(group["items"]) for group in result) == 8
    assert len(calls) <= 4


async def test_poll_yields_between_files_for_interactive_selection(core, media, worker_setup, monkeypatch):
    worker, engine, _ = worker_setup
    choices = season_choices(core, media, worker_setup, count=3)
    await worker.choose_many(choices, 1)
    engine.completed = {1, 2, 3}
    calls = []
    verify = worker._verify

    async def interrupt(*args, **kwargs):
        calls.append(args[2])
        result = await verify(*args, **kwargs)
        await worker.selection_lock.acquire()
        return result

    monkeypatch.setattr(worker, "_verify", interrupt)
    try:
        await worker.poll()
        assert calls == [1]
    finally:
        if worker.selection_lock.locked():
            worker.selection_lock.release()


def test_mapping_api_applies_complete_season_in_one_torrent_update(core, media, worker_setup, monkeypatch):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.models import ConfigEntry, Task
    from lazarr.season_mapping import releases_key
    from test_api import login

    choices = season_choices(core, media, worker_setup)
    config, db, _, _ = core
    _, engine, _ = worker_setup
    with db.session() as session:
        release_id = session.get(CandidateDecision, choices[0]["decision_id"]).release_id
        session.add(ConfigEntry(key=releases_key(session.get(Task, 1), 1), value={"releases": [release_id]}))
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        engine.close = ctx.engine.close
        monkeypatch.setattr(ctx, "engine", engine)
        monkeypatch.setattr(ctx.worker, "engine", engine)
        monkeypatch.setattr(engine, "add", Mock(wraps=engine.add))
        route = "/api/v1/tasks/1/seasons/1/mapping"
        snapshot = client.get(route).json()
        rows = [
            dict(subtask_id=episode["subtask_id"], title=f"Renamed {i}", release_id=release_id, video_index=i)
            for i, episode in enumerate(snapshot["episodes"])
        ]
        response = client.put(route, json={"rows": rows})
        assert response.status_code == 200, response.text
        assert engine.add.call_count == 1
        after = client.get(route).json()
        assert [row["title"] for row in after["episodes"]] == [row["title"] for row in rows]
        assert [row["binding"]["video_index"] for row in after["episodes"]] == list(range(len(rows)))
        assert client.put(route, json={"rows": rows}).status_code == 200
        assert engine.add.call_count == 1
