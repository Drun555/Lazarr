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
    Season,
    Subtask,
    SubtaskAsset,
    Task,
    TaskSeason,
)
from lazarr.sdk import TorrentFile
from lazarr.season_mapping import related_files, releases_key
from lazarr.services import CreateTask
from test_api import login
from test_worker import FakeEngine


def files(*paths):
    return [TorrentFile(index=i, path=path, size=100, offset=i * 100) for i, path in enumerate(paths)]


def test_add_release_to_empty_manual_season_then_map_episode(core, media, season, monkeypatch):
    import json
    from lazarr.sdk import DownloadSource

    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with db.session() as session:
        manual = Season(media_id=1, number=2, title="Manual", metadata_json={"manual": True})
        session.add(manual)
        session.flush()
        manual_id = manual.id
        session.add(TaskSeason(task_id=1, season_id=manual.id, selection_key="2", whole_season=True))
        session.get(Task, 1).paused = True

    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        ctx.plugins.configure("nyaa", {}, True)

        async def inspect(self, candidate):
            return candidate.model_copy(update={"title": "Manual release"})

        async def resolve(self, candidate):
            return DownloadSource(torrent=json.dumps(["Pilot.mkv", "Pilot.ru.srt"]).encode())

        monkeypatch.setattr(ctx.plugins.classes["nyaa"], "inspect", inspect)
        monkeypatch.setattr(ctx.plugins.classes["nyaa"], "resolve_download", resolve)
        engine = FakeEngine()
        engine.close = ctx.engine.close
        monkeypatch.setattr(ctx, "engine", engine)
        monkeypatch.setattr(ctx.worker, "engine", engine)
        path = "/api/v1/tasks/1/seasons/2/mapping"
        assert client.post(path + "/releases", json={"url": "invalid"}).status_code == 422
        for _ in range(2):
            response = client.post(path + "/releases", json={"url": "https://nyaa.si/view/321"})
            assert response.status_code == 200, response.text
        release_id = response.json()["release_id"]
        snapshot = client.get(path).json()
        assert snapshot["episodes"] == []
        assert [release["id"] for release in snapshot["releases"]] == [release_id]
        assert len(snapshot["releases"][0]["files"]) == 2
        with db.session() as session:
            assert session.scalar(select(Episode).where(Episode.season_id == manual_id)) is None
            assert session.scalar(select(CandidateDecision)) is None
            assert session.scalar(select(Download)) is None
        response = client.post(path + "/episodes", json={"number": 1, "title": "Pilot"})
        assert response.status_code == 200, response.text
        subtask_id = response.json()["subtask_id"]
        ctx.worker.choose_many = AsyncMock()
        response = client.put(
            path,
            json={
                "rows": [
                    {
                        "subtask_id": subtask_id,
                        "title": "Pilot",
                        "release_id": release_id,
                        "video_index": 0,
                        "track_indices": [1],
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        ctx.worker.choose_many.assert_awaited_once()
        with db.session() as session:
            assert session.get(Subtask, subtask_id).episode_id is not None


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


def test_related_files_require_common_title_fragments_anywhere():
    root = "[Beatrice-Raws] Bakemonogatari [BDRip 1920x1080 x264 FLAC]"
    stem = "[Beatrice-Raws] Bakemonogatari 01 [BDRip 1920x1080 x264 FLAC]"
    items = files(
        f"{root}/{stem}.mkv",
        f"{root}/RUS Sound/{stem}.[SHIZA].mka",
        f"{root}/RUS Subs/[Other Group] Bakemonogatari 01 [720p].ass",
        f"{root}/Sound Vol.1.flac",
        f"{root}/RUS Sound/[Beatrice-Raws] Other Title 01 [BDRip 1920x1080 x264 FLAC].mka",
        f"{root}/RUS Subs/[Other Group] Bakemonogatari 02 [720p].ass",
        f"{root}/RUS Subs/Translation Bakemonogatari 01.ass",
    )
    assert related_files(items) == {0: [1, 2, 6]}
    # Explicit user selections still override the automatic name check.
    assert related_files(items, [{"video_index": 0, "tracks": [{"file_index": 3}]}]) == {0: [1, 2, 3, 6]}


def test_related_files_sum_separate_fragments_and_preserve_short_titles():
    assert related_files(
        files(
            "Alpha Red Omega 01.mkv",
            "Translation Alpha Blue Omega 01.ass",
            "Show.S01E01.1080p.mkv",
            "Show.S01E01.ru.mka",
            "S01E01.1080p.flac",
            "Other.S01E01.1080p.flac",
        )
    ) == {0: [1], 2: [3]}
    assert related_files(
        files(
            "[Group] Alpha 01 [1080p].mkv",
            "[Group] Algae 01 [1080p].ass",
        )
    ) == {0: []}


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


def test_mapping_api_excludes_soundtrack_from_smart_group(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    seed_release(config, db)
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        original = ctx.engine
        ctx.engine = SimpleNamespace(
            inspect=lambda _: SimpleNamespace(
                files=files(
                    "[Beatrice-Raws] Bakemonogatari 01 [1080p].mkv",
                    "RUS Subs/[Different Group] Bakemonogatari 01.ass",
                    "[Beatrice-Raws] Bakemonogatari 02 [1080p].mkv",
                    "RUS Sound/Sound Vol.1.flac",
                    "RUS Sound/[Other Group] Bakemonogatari 01.mka",
                )
            )
        )
        try:
            response = client.get("/api/v1/tasks/1/seasons/1/mapping")
            assert response.status_code == 200, response.text
            catalog = response.json()["releases"][0]["files"]
            assert catalog[0]["related"] == [1, 4]
            assert catalog[2]["related"] == []
        finally:
            ctx.engine = original


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
            ctx.worker.add_manual_task_candidate.assert_awaited_once_with(
                1, "https://nyaa.si/view/1", 1, return_release=True
            )
            assert len(client.get(path).json()["releases"]) == 1
            # Zero-match candidates can be added without selecting any episodes.
            response = client.post(path + "/releases", json={"candidate_id": 1})
            assert response.status_code == 200, response.text
            assert response.json()["release_id"] == release_id
            ctx.worker.add_manual_task_candidate.assert_awaited_once()
            assert client.post(path + "/releases", json={"candidate_id": 99999}).status_code == 422
            assert client.put(path, json={"rows": [], "pool_release_ids": [99999]}).status_code == 422
            assert client.put(path, json={"rows": [], "pool_release_ids": []}).status_code == 200
            assert client.get(path).json()["hidden_release_ids"] == [release_id]
            assert client.post(path + "/releases", json={"candidate_id": 1}).status_code == 200
            assert client.get(path).json()["hidden_release_ids"] == []
            ctx.worker.choose_many = AsyncMock()
            # Every row is checked before any selection is changed.
            valid = {"subtask_id": 1, "title": "My pilot", "release_id": release_id, "video_index": 2}
            response = client.put(path, json={"rows": [valid, {**valid, "subtask_id": 999}]})
            assert response.status_code == 422
            ctx.worker.choose_many.assert_not_called()
            response = client.put(path, json={"rows": [{**valid, "track_indices": [2]}]})
            assert response.status_code == 422
            ctx.worker.choose_many.assert_not_called()
            # A title-only edit must not restart an existing download.
            response = client.put(path, json={"rows": [{**valid, "video_index": 0, "track_indices": [1]}]})
            assert response.status_code == 200, response.text
            ctx.worker.choose_many.assert_not_called()
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
            ctx.worker.choose_many.assert_awaited_once()
            assert {
                k: v for k, v in ctx.worker.choose_many.call_args.args[0][0].items() if k != "decision_id"
            } == {"video_index": 2, "track_indices": []}
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
        ctx.worker.choose_many = AsyncMock()
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
            ctx.worker.choose_many.assert_not_called()
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
            ctx.worker.choose_many.assert_not_called()
            response = client.get("/api/v1/candidates/1/selection")
            assert response.json()["video_index"] == 1
            assert response.json()["tracks"][0]["file_index"] == 2
            # A removed legacy video can still be assigned using its own torrent snapshot.
            response = client.put(
                path, json={"rows": [{**rows[2], "release_id": identity, "video_index": 3}]}
            )
            assert response.status_code == 200, response.text
            assert {
                k: v for k, v in ctx.worker.choose_many.call_args.args[0][0].items() if k != "decision_id"
            } == {
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


def test_insert_split_episode_before_existing_number(core, media, season, monkeypatch):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        path = "/api/v1/tasks/1/seasons/1/mapping"
        before = client.get(path).json()["episodes"]
        with db.session() as session:
            release = Release(provider="nyaa", external_id="parts", revision="parts", data={"title": "Parts"})
            session.add(release)
            session.flush()
            release_id = release.id
            session.add(
                ConfigEntry(key=releases_key(session.get(Task, 1), 1), value={"releases": [release_id]})
            )
        torrent = config.data_dir / "torrents" / "parts.torrent"
        torrent.parent.mkdir(exist_ok=True)
        torrent.write_bytes(b"parts")
        monkeypatch.setattr(
            ctx.engine,
            "inspect",
            lambda _: SimpleNamespace(
                files=files("Episode1.part1.mkv", "Episode1.part2.mkv", "Episode2.mkv")
            ),
        )
        monkeypatch.setattr(ctx.worker, "choose_many", AsyncMock())
        # Explicit duplicate numbers still fail. Draft additions reserve a free
        # number before the final, simultaneous renumbering of all visible rows.
        assert client.post(path + "/episodes", json={"number": 2, "title": "Part 2"}).status_code == 422
        response = client.post(path + "/episodes", json={"title": "Part 2"})
        assert response.status_code == 200, response.text
        new_id = response.json()["subtask_id"]
        assert response.json()["number"] > max(row["number"] for row in before)
        rows = [
            {
                "subtask_id": row["subtask_id"],
                "number": row["number"] + (row["number"] >= 2),
                "title": row["title"],
            }
            for row in before
        ]
        rows.insert(1, {"subtask_id": new_id, "number": 2, "title": "Part 2"})
        for index, row in enumerate(rows[:3]):
            row.update(release_id=release_id, video_index=index)
        response = client.put(path, json={"rows": rows})
        assert response.status_code == 200, response.text
        assert ctx.worker.choose_many.await_count == 1
        assert len(ctx.worker.choose_many.call_args.args[0]) == 3
        with db.session() as session:
            for choice in ctx.worker.choose_many.call_args.args[0]:
                decision = session.get(CandidateDecision, choice["decision_id"])
                assert decision.subtask_id == rows[choice["video_index"]]["subtask_id"]
            service._upsert_season(session, 1, season)
        after = client.get(path).json()["episodes"]
        assert [(row["subtask_id"], row["number"]) for row in after] == [
            (row["subtask_id"], row["number"]) for row in rows
        ]
        # The same structure can be saved again without a number collision.
        assert client.put(path, json={"rows": rows}).status_code == 200


def test_compact_numbers_after_deleting_episode(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        path = "/api/v1/tasks/1/seasons/1/mapping"
        before = client.get(path).json()["episodes"]
        rows = [
            {"subtask_id": row["subtask_id"], "number": i + 1, "title": row["title"]}
            for i, row in enumerate(before[1:])
        ]
        payload = {"rows": rows, "deleted_subtask_ids": [before[0]["subtask_id"]]}
        response = client.put(path, json=payload)
        assert response.status_code == 200, response.text
        assert client.put(path, json=payload).status_code == 200
        with db.session() as session:
            service._upsert_season(session, 1, season)
        after = client.get(path).json()["episodes"]
        assert [(row["subtask_id"], row["number"]) for row in after] == [
            (row["subtask_id"], row["number"]) for row in rows
        ]
        # Appending never accidentally restores a hidden, deleted episode.
        added = client.post(path + "/episodes", json={"title": "New"}).json()
        assert added["subtask_id"] not in {row["subtask_id"] for row in before}


def test_season_title_survives_refresh_and_is_exposed_in_library(core, media, season):
    config, db, _, service = core
    season.title = "Название TMDB"
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        path = "/api/v1/tasks/1/seasons/1/mapping"
        assert client.get(path).json()["season_title"] == "Название TMDB"
        assert client.put(path, json={"rows": [], "season_title": "x" * 501}).status_code == 422
        response = client.put(path, json={"rows": [], "season_title": "  Моё название  "})
        assert response.status_code == 200, response.text
        with db.session() as session:
            service._upsert_season(session, 1, season)
        assert client.get(path).json()["season_title"] == "Моё название"
        detail = client.get("/api/v1/libraries/media/1").json()
        assert next(item for item in detail["seasons"] if item["number"] == 1)["title"] == "Моё название"
        # Older clients that omit the field must preserve the saved title.
        assert client.put(path, json={"rows": []}).status_code == 200
        assert client.get(path).json()["season_title"] == "Моё название"
        assert client.put(path, json={"rows": [], "season_title": ""}).status_code == 200
        with db.session() as session:
            service._upsert_season(session, 1, season)
        assert client.get(path).json()["season_title"] == ""


def test_deleted_episodes_stay_deleted_and_can_be_added_again(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        path = "/api/v1/tasks/1/seasons/1/mapping"
        original = client.get(path).json()["episodes"]
        ids = [row["subtask_id"] for row in original]
        assert client.put(path, json={"rows": [], "deleted_subtask_ids": [99999]}).status_code == 422
        assert client.get(path).json()["episodes"] == original
        response = client.put(path, json={"rows": [], "deleted_subtask_ids": ids})
        assert response.status_code == 200, response.text
        # Retrying after a lost response is harmless, including for an empty season.
        assert client.put(path, json={"rows": [], "deleted_subtask_ids": ids}).status_code == 200
        with db.session() as session:
            service._upsert_season(session, 1, season)
        assert client.get(path).json()["episodes"] == []
        assert client.get("/api/v1/libraries/media/1").json()["episodes"] == []
        response = client.post(path + "/episodes", json={"number": 1, "title": "Возвращённая серия"})
        assert response.status_code == 200, response.text
        assert response.json()["subtask_id"] == ids[0]
        assert len(client.get(path).json()["episodes"]) == 1
        assert len(client.get("/api/v1/libraries/media/1").json()["episodes"]) == 1


def test_special_positions_auto_manual_reset_and_validation(core, media, season):
    import time
    from lazarr.models import Season

    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with db.session() as session:
        session.get(Season, 1).number = 0
        session.get(Episode, 1).air_date = "2020-01-04"
        session.add(
            ConfigEntry(
                key="special_catalog.1",
                value={
                    "updated": time.time(),
                    "seasons": [
                        {
                            "number": 1,
                            "episodes": [
                                {"number": 1, "air_date": "2020-01-01"},
                                {"number": 2, "air_date": "2020-01-08"},
                            ],
                        }
                    ],
                },
            )
        )
    with TestClient(create_app(config)) as client:
        login(client)
        path = "/api/v1/tasks/1/seasons/0/mapping"
        data = client.get(path).json()
        row = data["episodes"][0]
        assert row["special_position"]["position"] == {"airsbefore_season": 1, "airsbefore_episode": 2}
        payload = {
            "subtask_id": row["subtask_id"],
            "title": row["title"],
            "special_position": {"mode": "manual", "airsafter_season": 1},
        }
        assert client.put(path, json={"rows": [payload]}).status_code == 200
        assert client.get(path).json()["episodes"][0]["special_position"]["position"] == {
            "airsafter_season": 1
        }
        payload["special_position"] = {"mode": "manual", "airsbefore_season": 1, "airsbefore_episode": 99}
        assert client.put(path, json={"rows": [payload]}).status_code == 422
        payload["special_position"] = {"mode": "manual", "airsbefore_season": 1, "airsafter_season": 1}
        assert client.put(path, json={"rows": [payload]}).status_code == 422
        payload["special_position"] = {"mode": "manual"}
        assert client.put(path, json={"rows": [payload]}).status_code == 200
        assert client.get(path).json()["episodes"][0]["special_position"]["position"] == {}
        payload["special_position"] = {"mode": "auto"}
        assert client.put(path, json={"rows": [payload]}).status_code == 200
        assert client.get(path).json()["episodes"][0]["special_position"]["position"] == {
            "airsbefore_season": 1,
            "airsbefore_episode": 2,
        }
        with db.session() as session:
            session.get(Season, 1).number = 1
        assert client.put(path.replace("/0/", "/1/"), json={"rows": [payload]}).status_code == 422
