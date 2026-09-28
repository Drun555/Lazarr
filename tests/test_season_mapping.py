from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.models import (
    CandidateDecision,
    ConfigEntry,
    Download,
    Episode,
    MediaAsset,
    Release,
    SubtaskAsset,
    Task,
)
from lazarr.sdk import TorrentFile
from lazarr.season_mapping import related_files, releases_key
from lazarr.services import CreateTask
from test_api import login


def files(*paths):
    return [TorrentFile(index=i, path=path, size=100, offset=i * 100) for i, path in enumerate(paths)]


def test_related_files_without_episode_numbers_and_ambiguity():
    items = files("Pilot.mkv", "Audio/Pilot.ru.flac", "Subs/Pilot.en.srt", "Finale.mkv", "Finale.ru.ass")
    assert related_files(items) == {0: [1, 2], 3: [4]}
    assert related_files(
        files("Pilot.mkv", "mystery.srt"), [{"video_index": 0, "tracks": [{"file_index": 1}]}]
    ) == {0: [1]}
    assert related_files(files("1080p/Show.S01E01.mkv", "720p/Show.S01E01.mkv", "Show.S01E01.ru.srt")) == {
        0: [],
        1: [],
    }


def seed_release(config, db):
    with db.session() as session:
        release = Release(
            provider="nyaa", external_id="test", revision="mapping-test", data={"title": "Release"}
        )
        session.add(release)
        session.flush()
        identity = release.id
        session.add(CandidateDecision(subtask_id=1, release_id=identity, report={}))
        session.add(ConfigEntry(key=releases_key(session.get(Task, 1), 1), value={"releases": [identity]}))
        download = Download(
            release_id=identity,
            infohash="mapping-test",
            save_path="/tmp/mapping-test",
            torrent_file="mapping-test.torrent",
            plan={},
            state="paused",
        )
        session.add(download)
        session.flush()
        for subtask_id, index in [(1, 0), (2, 2)]:
            asset = MediaAsset(media_id=1, download_id=download.id, video_index=index, path=f"{index}.mkv")
            session.add(asset)
            session.flush()
            session.add(
                SubtaskAsset(
                    subtask_id=subtask_id,
                    asset_id=asset.id,
                    current=True,
                    pending=False,
                    preflight={
                        "binding": {"video_index": index, "tracks": [{"file_index": 1}] if index == 0 else []}
                    },
                )
            )
    torrent = config.data_dir / "torrents" / "mapping-test.torrent"
    torrent.parent.mkdir(exist_ok=True)
    torrent.write_bytes(b"test")
    return identity


def test_season_editor_bindings_validation_titles_and_manual_episodes(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    release_id = seed_release(config, db)
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        original = ctx.engine
        ctx.engine = SimpleNamespace(
            inspect=lambda _: SimpleNamespace(files=files("Pilot.mkv", "Pilot.ru.srt", "Finale.mkv"))
        )
        try:
            path = "/api/v1/tasks/1/seasons/1/mapping"
            response = client.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            assert len(data["releases"]) == 1  # Both episode subtasks share this release.
            assert data["episodes"][0]["binding"]["video_index"] == 0
            assert data["releases"][0]["files"][0]["related"] == [1]
            ctx.worker.add_manual_task_candidate = AsyncMock(return_value=1)
            assert client.post(path + "/releases", json={"url": "https://nyaa.si/view/1"}).status_code == 200
            ctx.worker.add_manual_task_candidate.assert_awaited_once_with(1, "https://nyaa.si/view/1", 1)
            assert len(client.get(path).json()["releases"]) == 1
            ctx.worker.choose = AsyncMock()
            # Every row is checked before any selection is changed.
            valid = {"subtask_id": 1, "title": "My pilot", "release_id": release_id, "video_index": 2}
            response = client.put(path, json={"rows": [valid, {**valid, "subtask_id": 999}]})
            assert response.status_code == 422
            ctx.worker.choose.assert_not_called()
            response = client.put(path, json={"rows": [{**valid, "track_indices": [2]}]})
            assert response.status_code == 422
            ctx.worker.choose.assert_not_called()
            # A title-only edit must not restart an existing download.
            response = client.put(path, json={"rows": [{**valid, "video_index": 0, "track_indices": [1]}]})
            assert response.status_code == 200, response.text
            ctx.worker.choose.assert_not_called()
            with db.session() as session:
                service._upsert_season(session, 1, season)
                assert session.get(Episode, 1).title == "My pilot"
            response = client.post(path + "/episodes", json={"number": 4, "title": "Bonus"})
            assert response.status_code == 200, response.text
            assert (
                client.post(path + "/episodes", json={"number": 4, "title": "Duplicate"}).status_code == 422
            )
            assert client.get(path).json()["episodes"][-1]["number"] == 4
            assert client.post(path + "/episodes", json={"number": 5, "title": "   "}).status_code == 422
            with db.session() as session:
                assert session.scalar(select(Episode).where(Episode.number == 5)) is None
            response = client.put(path, json={"rows": [valid]})
            assert response.status_code == 200, response.text
            ctx.worker.choose.assert_awaited_once()
            assert ctx.worker.choose.call_args.kwargs == {"video_index": 2, "track_indices": []}
        finally:
            ctx.engine = original


def test_updated_release_mapping_keeps_old_download_and_translates_indices(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    identity = seed_release(config, db)
    old_files = [file.model_dump() for file in files("Pilot.mkv", "Pilot.ru.srt", "Finale.mkv")]
    new_files = files("New.mkv", "Pilot.mkv", "Pilot.ru.srt")
    with db.session() as session:
        release = session.get(Release, identity)
        release.revision = "updated"
        release.files = [file.model_dump() for file in new_files]
        download = session.scalar(select(Download))
        download.plan = {"files": old_files}
    (config.data_dir / "torrents/updated.torrent").write_bytes(b"new")
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        original = ctx.engine
        ctx.engine = SimpleNamespace(inspect=lambda _: SimpleNamespace(files=new_files))
        ctx.worker.choose = AsyncMock()
        try:
            path = "/api/v1/tasks/1/seasons/1/mapping"
            result = client.get(path).json()
            assert len(result["releases"]) == 1
            assert len(result["releases"][0]["files"]) == 4
            assert result["episodes"][0]["binding"]["video_index"] == 1
            assert result["episodes"][0]["binding"]["tracks"][0]["file_index"] == 2
            assert result["episodes"][1]["binding"]["video_index"] == 3
            assert result["releases"][0]["files"][3]["legacy"] is True
            response = client.put(path, json={"rows": [], "revisions": {identity: "mapping-test"}})
            assert response.status_code == 422
            ctx.worker.choose.assert_not_called()
            # Merely opening and saving must not replace or redownload old files.
            rows = [
                {
                    "subtask_id": episode["subtask_id"],
                    "title": episode["title"],
                    "release_id": episode["release_id"],
                    "video_index": (episode["binding"] or {}).get("video_index"),
                    "track_indices": [
                        track["file_index"] for track in (episode["binding"] or {}).get("tracks", [])
                    ],
                }
                for episode in result["episodes"]
            ]
            response = client.put(path, json={"rows": rows})
            assert response.status_code == 200, response.text
            ctx.worker.choose.assert_not_called()
            response = client.get("/api/v1/candidates/1/selection")
            assert response.json()["video_index"] == 1
            assert response.json()["tracks"][0]["file_index"] == 2
            # A removed legacy video can still be assigned using its own torrent snapshot.
            response = client.put(
                path, json={"rows": [{**rows[2], "release_id": identity, "video_index": 3}]}
            )
            assert response.status_code == 200, response.text
            assert ctx.worker.choose.call_args.kwargs == {
                "video_index": 2,
                "track_indices": [],
                "revision": "mapping-test",
            }
        finally:
            ctx.engine = original


def test_episode_numbers_swap_and_survive_metadata_refresh(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        path = "/api/v1/tasks/1/seasons/1/mapping"
        rows = [
            {"subtask_id": item["subtask_id"], "number": item["number"], "title": item["title"]}
            for item in client.get(path).json()["episodes"]
        ]
        rows[0]["number"] = rows[1]["number"]
        response = client.put(path, json={"rows": rows})
        assert response.status_code == 422
        assert client.get(path).json()["episodes"][0]["number"] == 1
        rows[1]["number"] = 1
        response = client.put(path, json={"rows": rows})
        assert response.status_code == 200, response.text
        with db.session() as session:
            service._upsert_season(session, 1, season)
            assert session.get(Episode, 1).number == 2
            assert session.get(Episode, 2).number == 1
            assert session.get(Episode, 1).title == season.episodes[0].title
            assert session.get(Episode, 2).title == season.episodes[1].title
        saved = {row["subtask_id"]: row["number"] for row in client.get(path).json()["episodes"]}
        assert saved[rows[0]["subtask_id"]] == 2
        assert saved[rows[1]["subtask_id"]] == 1
