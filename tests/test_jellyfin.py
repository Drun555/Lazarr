from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.config import Requirements
from lazarr.models import Download, LibraryAsset, Media, MediaAsset, Release, Subtask, SubtaskAsset, Task
from lazarr.services import CreateTask


def playable_episode(core, media, season):
    config, db, _, service = core
    media.taxonomy_known = True
    media.genre_ids = [16]
    media.original_language = "ja"
    media.episode_numbering = {"1:1": [{"season": 2, "episode": 14}]}
    season.episodes[0].overview = "Описание первой серии"
    season.episodes[0].still = "https://image.tmdb.org/t/p/w342/episode-still.jpg"
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    settings = service.settings()
    settings.defaults = Requirements(audio_languages=["ja", "ru"], subtitle_languages=["ru"])
    service.set_settings(settings, 1)
    infohash = "a" * 40
    root = Path(settings.series_path) / infohash
    root.mkdir(parents=True)
    video = root / "episode.mkv"
    video.write_bytes(b"0123456789")
    subtitle = root / "episode.ru.ass"
    subtitle.write_text("[Script Info]\nTitle: test\n", encoding="utf-8")
    with db.session() as session:
        row = session.scalar(select(Media))
        row.metadata_json = {
            **row.metadata_json,
            "taxonomy_known": True,
            "genre_ids": [16],
            "original_language": "ja",
            "episode_numbering": media.episode_numbering,
            "overview": "Описание",
            "poster": "https://image.tmdb.org/t/p/w342/example.jpg",
        }
        subtask = session.scalar(select(Subtask))
        release = Release(
            provider="demo", external_id="1", revision="1", data={"title": "Release", "url": ""}
        )
        session.add(release)
        session.flush()
        download = Download(
            infohash=infohash,
            release_id=release.id,
            save_path=str(root.resolve()),
            torrent_file=str(config.data_dir / "torrents" / f"{infohash}.torrent"),
            state="seeding",
            plan={"infohash": infohash, "files": [], "bindings": []},
        )
        session.add(download)
        session.flush()
        asset = MediaAsset(
            media_id=row.id,
            download_id=download.id,
            video_index=0,
            path=video.name,
            resolution=1080,
            probe={
                "format": {"duration": "120.5"},
                "streams": [
                    {
                        "index": 0,
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                    },
                    {
                        "index": 1,
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "channels": "2",
                        "sample_rate": "48000",
                        "bit_rate": "192000",
                        "tags": {"language": "ru"},
                    },
                    {
                        "index": 2,
                        "codec_type": "audio",
                        "codec_name": "truehd",
                        "channels": 2,
                        "tags": {"language": "ja"},
                    },
                ],
            },
        )
        session.add(asset)
        session.flush()
        session.add(
            SubtaskAsset(
                subtask_id=subtask.id,
                asset_id=asset.id,
                current=True,
                pending=False,
                verification={"complete": True},
                preflight={
                    "binding": {
                        "tracks": [
                            {
                                "kind": "subtitle",
                                "language": "ru",
                                "file_index": 1,
                                "path": subtitle.name,
                            }
                        ]
                    }
                },
            )
        )
        session.add(
            LibraryAsset(
                media_id=row.id,
                episode_id=subtask.episode_id,
                part_key=f"episode:{subtask.episode_id}",
                asset_id=asset.id,
                preflight={
                    "binding": {
                        "tracks": [
                            {
                                "kind": "subtitle",
                                "language": "ru",
                                "file_index": 1,
                                "path": subtitle.name,
                            }
                        ]
                    }
                },
                verification={"complete": True},
            )
        )
    return video, subtitle


def jellyfin_login(client, username="alice", password="a-safe-password"):
    response = client.post("/Users/AuthenticateByName", json={"Username": username, "Pw": password})
    assert response.status_code == 200, response.text
    payload = response.json()
    client.headers["X-Emby-Token"] = payload["AccessToken"]
    return payload


def test_jellyfin_auth_libraries_navigation_and_read_only_api(core, media, season):
    playable_episode(core, media, season)
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        public = client.get("/System/Info/Public")
        assert public.status_code == 200 and public.json()["ProductName"] == "Lazarr"
        assert client.get("/UserViews").status_code == 401
        assert (
            client.post("/Users/AuthenticateByName", json={"Username": "alice", "Pw": "wrong"}).status_code
            == 401
        )
        auth = jellyfin_login(client)
        assert client.get("/Users/Me").json()["Id"] == auth["User"]["Id"]
        views = client.get("/UserViews", params={"userId": auth["User"]["Id"]}).json()["Items"]
        assert [item["Name"] for item in views] == ["Сериалы", "Кино", "Аниме"]
        anime = next(item for item in views if item["Name"] == "Аниме")
        series = client.get("/Items", params={"ParentId": anime["Id"]}).json()["Items"]
        assert len(series) == 1 and series[0]["Type"] == "Series"
        seasons = client.get(f"/Shows/{series[0]['Id']}/Seasons").json()["Items"]
        assert [item["IndexNumber"] for item in seasons] == [2]
        children = client.get(
            "/Items", params={"ParentId": seasons[0]["Id"], "IncludeItemTypes": "Episode"}
        ).json()["Items"]
        assert len(children) == 1 and children[0]["IndexNumber"] == 14
        recursive = client.get(
            "/Items", params={"ParentId": series[0]["Id"], "Recursive": True, "IncludeItemTypes": "Episode"}
        ).json()["Items"]
        assert [item["Id"] for item in recursive] == [item["Id"] for item in children]
        episodes = client.get(
            f"/Shows/{series[0]['Id']}/Episodes",
            params=[
                ("userId", auth["User"]["Id"]),
                ("fields", "Overview"),
                ("fields", "CanDownload"),
                ("fields", "ParentId"),
                ("season", "2"),
                ("seasonId", next(s["Id"] for s in seasons if s["IndexNumber"] == 2)),
            ],
        ).json()["Items"]
        assert len(episodes) == 1 and episodes[0]["IndexNumber"] == 14
        assert episodes[0]["Name"] == "Episode 1"
        assert episodes[0]["Overview"] == "Описание первой серии"
        assert episodes[0]["ImageTags"]["Primary"]
        audio = next(stream for stream in episodes[0]["MediaStreams"] if stream["Type"] == "Audio")
        assert audio["Channels"] == 2
        assert audio["SampleRate"] == 48_000
        assert audio["BitRate"] == 192_000
        assert client.delete(f"/Items/{episodes[0]['Id']}").status_code == 405
        assert client.post("/Sessions/Logout").status_code == 204
        assert client.get("/UserViews").status_code == 401


def test_jellyfin_direct_play_range_languages_and_external_subtitles(core, media, season):
    video, subtitle = playable_episode(core, media, season)
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        auth = jellyfin_login(client)
        anime = next(
            item
            for item in client.get("/UserViews", params={"userId": auth["User"]["Id"]}).json()["Items"]
            if item["Name"] == "Аниме"
        )
        series = client.get("/Items", params={"ParentId": anime["Id"]}).json()["Items"][0]
        episode = client.get(f"/Shows/{series['Id']}/Episodes", params={"Season": 2}).json()["Items"][0]
        playback = client.post(f"/Items/{episode['Id']}/PlaybackInfo", json={}).json()
        source = playback["MediaSources"][0]
        assert source["Id"] == episode["Id"]
        assert source["SupportsDirectPlay"] is True
        assert source["SupportsDirectStream"] is True
        assert source["DirectStreamUrl"].startswith(f"/Videos/{episode['Id']}/stream")
        assert source["SupportsTranscoding"] is False and "TranscodingUrl" not in source
        streams = source["MediaStreams"]
        assert (
            next(s for s in streams if s["Index"] == source["DefaultAudioStreamIndex"])["Language"] == "jpn"
        )
        selected_subtitle = next(s for s in streams if s["Index"] == source["DefaultSubtitleStreamIndex"])
        assert selected_subtitle["Language"] == "rus" and selected_subtitle["IsExternal"]
        client.app.state.ctx.poster_transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b"\xff\xd8\xffepisode", headers={"content-type": "image/jpeg"}
            )
        )
        preview = client.get(f"/Items/{episode['Id']}/Images/Primary")
        assert preview.status_code == 200 and preview.content == b"\xff\xd8\xffepisode"
        response = client.get(f"/Videos/{source['Id']}/stream", headers={"Range": "bytes=2-5"})
        assert response.status_code == 206 and response.content == video.read_bytes()[2:6]
        assert client.get(f"/Videos/{episode['Id']}/stream.mp4").status_code == 415
        response = client.get(selected_subtitle["DeliveryUrl"])
        assert response.status_code == 200 and response.content == subtitle.read_bytes()


async def test_jellyfin_keeps_verified_episode_after_task_is_deleted(core, media, season):
    from lazarr.deletion import delete_task

    playable_episode(core, media, season)
    config, db, _, _ = core
    with db.session() as session:
        task_id = session.scalar(select(Task.id))
    with TestClient(create_app(config)) as client:
        await delete_task(client.app.state.ctx.worker, task_id, 1)
        auth = jellyfin_login(client)
        anime = next(
            item
            for item in client.get("/UserViews", params={"userId": auth["User"]["Id"]}).json()["Items"]
            if item["Name"] == "Аниме"
        )
        series = client.get("/Items", params={"ParentId": anime["Id"]}).json()["Items"][0]
        seasons = client.get(f"/Shows/{series['Id']}/Seasons").json()["Items"]
        episodes = client.get(f"/Shows/{series['Id']}/Episodes", params={"Season": 2}).json()["Items"]
        assert [item["IndexNumber"] for item in seasons] == [2]
        assert len(episodes) == 1
        assert client.get(f"/Videos/{episodes[0]['Id']}/stream").content == b"0123456789"
    with db.session() as session:
        assert session.scalar(select(Task.id)) is None
        assert session.scalar(select(LibraryAsset.id)) is not None


def test_jellyfin_tracks_progress_per_user_and_returns_resume_items(core, media, season):
    playable_episode(core, media, season)
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        alice = jellyfin_login(client)
        anime = next(
            item
            for item in client.get("/UserViews", params={"userId": alice["User"]["Id"]}).json()["Items"]
            if item["Name"] == "Аниме"
        )
        series = client.get("/Items", params={"ParentId": anime["Id"]}).json()["Items"][0]
        episode = client.get(f"/Shows/{series['Id']}/Episodes", params={"Season": 2}).json()["Items"][0]
        item_id = episode["Id"]
        assert client.get(f"/UserItems/{item_id}/UserData").json()["PlaybackPositionTicks"] == 0

        assert (
            client.post(
                "/Sessions/Playing/Progress",
                json={"ItemId": item_id, "PositionTicks": 500_000_000},
            ).status_code
            == 204
        )
        progress = client.get(f"/UserItems/{item_id}/UserData").json()
        assert progress["PlaybackPositionTicks"] == 500_000_000 and not progress["Played"]
        resumed = client.get("/UserItems/Resume").json()["Items"]
        assert [item["Id"] for item in resumed] == [item_id]

        bob = jellyfin_login(client, "bob", "b-safe-password")
        assert bob["User"]["Id"] != alice["User"]["Id"]
        assert client.get(f"/UserItems/{item_id}/UserData").json()["PlaybackPositionTicks"] == 0

        client.headers["X-Emby-Token"] = alice["AccessToken"]
        client.post(
            "/Sessions/Playing/Stopped",
            json={"ItemId": item_id, "PositionTicks": 1_100_000_000},
        )
        completed = client.get(f"/Items/{item_id}").json()["UserData"]
        assert completed["Played"] and completed["PlayCount"] == 1
        assert completed["PlaybackPositionTicks"] == 0
        assert client.get("/UserItems/Resume").json()["Items"] == []
