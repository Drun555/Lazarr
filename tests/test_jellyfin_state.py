"""End-to-end user state, isolation, durable playback and playlist permissions."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.jellyfin import object_id
from lazarr.models import Episode, LibraryAsset, MediaAsset, PlaybackProgress, PlaybackSession
from test_jellyfin import jellyfin_login, playable_episode


def items(client):
    return client.get("/Items", params={"Recursive": "true", "IncludeItemTypes": "Episode"}).json()["Items"]


def state(client, identity):
    response = client.get(f"/UserItems/{identity}/UserData")
    assert response.status_code == 200, response.text
    return response.json()


def test_favorites_ratings_partial_updates_and_filters(core, media, season):
    playable_episode(core, media, season)
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        alice = jellyfin_login(client)
        episode = items(client)[0]
        identity = episode["Id"]
        series = episode["SeriesId"]
        assert client.post(f"/UserFavoriteItems/{identity}").json()["IsFavorite"]
        assert client.post(f"/UserFavoriteItems/{series}").json()["IsFavorite"]
        assert client.post(f"/UserItems/{identity}/Rating", params={"likes": False}).json()["Likes"] is False
        response = client.post(
            f"/UserItems/{identity}/UserData",
            json={
                "Rating": 8.5,
                "PlayCount": 3,
                "PlaybackPositionTicks": 500_000_000,
                "LastPlayedDate": "2020-04-01T12:00:00Z",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["IsFavorite"] and data["Rating"] == 8.5 and data["PlayCount"] == 3
        assert data["LastPlayedDate"] == "2020-04-01T12:00:00Z"
        assert data["Likes"] is False
        assert state(client, series)["IsFavorite"]
        favorites = client.get("/Items", params={"Recursive": True, "Filters": "IsFavorite"}).json()["Items"]
        assert {i["Id"] for i in favorites} == {identity, series}
        assert client.get("/Items", params={"Recursive": True, "Filters": "IsLiked"}).json()["Items"] == []
        disliked = client.get("/Items", params={"Recursive": True, "Filters": "IsDisliked"}).json()["Items"]
        assert [i["Id"] for i in disliked] == [identity]
        assert client.delete(f"/UserItems/{identity}/Rating").json()["Likes"] is None
        assert state(client, identity)["Rating"] == 8.5
        assert client.post(f"/UserItems/{identity}/UserData", json={"PlayCount": -1}).status_code == 422
        assert client.post(f"/UserItems/{identity}/UserData", json={"IsFavorite": "false"}).status_code == 422
        # Compact UUIDs refer to the same state, never an independent row.
        assert client.delete(f"/UserFavoriteItems/{uuid.UUID(identity).hex}").json()["IsFavorite"] is False
        bob = jellyfin_login(client, "bob", "b-safe-password")
        assert state(client, identity)["PlayCount"] == 0
        assert state(client, identity)["Rating"] is None
        assert (
            client.post(f"/UserFavoriteItems/{identity}", params={"userId": alice["User"]["Id"]}).status_code
            == 403
        )
        assert client.post(f"/Users/{alice['User']['Id']}/FavoriteItems/{identity}").status_code == 403
        assert client.post(f"/Users/{bob['User']['Id']}/FavoriteItems/{identity}").status_code == 200
    with TestClient(create_app(config)) as client:
        jellyfin_login(client)
        assert state(client, identity)["Rating"] == 8.5
        assert state(client, series)["IsFavorite"]


def test_display_preferences_are_persistent_and_scoped(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        alice = jellyfin_login(client)
        url = "/DisplayPreferences/usersettings"
        data = {
            "ViewType": "List",
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "CustomPrefs": {"home0": "resume"},
            "ShowSidebar": True,
        }
        assert client.post(url, params={"client": "tv"}, json=data).status_code == 204
        returned_id = client.get(url, params={"client": "tv"}).json()["Id"]
        assert (
            client.post(
                f"/DisplayPreferences/{returned_id}", params={"client": "tv"}, json={"ViewType": "List"}
            ).status_code
            == 204
        )
        assert client.get(url, params={"client": "tv"}).json()["CustomPrefs"] == data["CustomPrefs"]
        assert client.get(url, params={"client": "web"}).json()["CustomPrefs"] == {}
        assert client.get("/DisplayPreferences/another", params={"client": "tv"}).json()["CustomPrefs"] == {}
        jellyfin_login(client, "bob", "b-safe-password")
        assert client.get(url, params={"client": "tv"}).json()["CustomPrefs"] == {}
        assert (
            client.post(url, params={"client": "tv", "userId": alice["User"]["Id"]}, json=data).status_code
            == 403
        )
    with TestClient(create_app(config)) as client:
        jellyfin_login(client)
        assert client.get(url, params={"client": "tv"}).json()["ViewType"] == "List"


def test_playlists_duplicates_ordering_permissions_and_restart(core, media, season):
    playable_episode(core, media, season)
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        alice = jellyfin_login(client)
        identity = items(client)[0]["Id"]
        bob = jellyfin_login(client, "bob", "b-safe-password")
        client.headers["X-Emby-Token"] = alice["AccessToken"]
        response = client.post(
            "/Playlists", json={"Name": "Вечер", "Ids": [identity, identity], "MediaType": "Video"}
        )
        assert response.status_code == 200, response.text
        playlist = response.json()["Id"]
        base = f"/Playlists/{playlist}"
        entries = client.get(base + "/Items").json()["Items"]
        assert len(entries) == 2
        first, second = [i["PlaylistItemId"] for i in entries]
        assert first != second
        assert client.post(base + f"/Items/{first}/Move/1").status_code == 204
        assert [i["PlaylistItemId"] for i in client.get(base + "/Items").json()["Items"]] == [second, first]
        assert (
            client.get(base + "/Items", params={"startIndex": 1, "limit": 1}).json()["TotalRecordCount"] == 2
        )
        client.headers["X-Emby-Token"] = bob["AccessToken"]
        for url in [base, base + "/Items", f"/Items/{playlist}"]:
            assert client.get(url).status_code == 404
        assert client.get("/Items", params={"IncludeItemTypes": "Playlist"}).json()["Items"] == []
        assert client.get("/Items", params={"ParentId": playlist}).status_code == 404
        client.headers["X-Emby-Token"] = alice["AccessToken"]
        share = base + f"/Users/{bob['User']['Id']}"
        assert client.post(share, json={"CanEdit": False}).status_code == 204
        client.headers["X-Emby-Token"] = bob["AccessToken"]
        assert client.get(base).json()["ItemIds"] == [identity, identity]
        assert client.post(base + "/Items", params={"ids": identity}).status_code == 403
        client.headers["X-Emby-Token"] = alice["AccessToken"]
        assert client.post(share, json={"CanEdit": True}).status_code == 204
        client.headers["X-Emby-Token"] = bob["AccessToken"]
        assert client.post(base, json={"Name": "Общий вечер"}).status_code == 204
        assert client.post(base, json={"IsPublic": True}).status_code == 403
        assert client.post(share, json={"CanEdit": True}).status_code == 403
        assert client.delete(f"/Items/{playlist}").status_code == 403
        assert client.delete(base + "/Items", params={"entryIds": first}).status_code == 204
        assert client.get(base + "/Items").json()["Items"][0]["PlaylistItemId"] == second
        client.headers["X-Emby-Token"] = alice["AccessToken"]
        assert client.delete(share).status_code == 204
        assert client.post(base, json={"IsPublic": True}).status_code == 204
    with TestClient(create_app(config)) as client:
        jellyfin_login(client, "bob", "b-safe-password")
        assert client.get(f"/Items/{playlist}").json()["Name"] == "Общий вечер"
        assert client.get(base + "/Items").json()["TotalRecordCount"] == 1
        assert client.post(base + "/Items", params={"ids": identity}).status_code == 403
        jellyfin_login(client)
        assert client.delete(f"/Items/{playlist}").status_code == 204
        assert client.get(base).status_code == 404
        assert client.delete(f"/Items/{identity}").status_code == 405


def test_playback_rewatch_resume_idempotency_failure_and_restart(core, media, season):
    playable_episode(core, media, season)
    config, db, _, _ = core
    with TestClient(create_app(config)) as client:
        jellyfin_login(client)
        episode = items(client)[0]
        identity = episode["Id"]
        payload = {"ItemId": identity, "PlaySessionId": "first", "PositionTicks": 0}
        for _ in range(2):
            assert client.post("/Sessions/Playing", json=payload).status_code == 204
        assert state(client, identity)["PlayCount"] == 1
        payload["PositionTicks"] = 500_000_000
        client.post("/Sessions/Playing/Progress", json=payload)
        assert client.get("/UserItems/Resume", params={"excludeActiveSessions": True}).json()["Items"] == []
        client.post("/Sessions/Playing/Stopped", json=payload)
        assert (
            len(client.get("/UserItems/Resume", params={"excludeActiveSessions": True}).json()["Items"]) == 1
        )
        assert client.get("/UserItems/Resume", params={"includeItemTypes": "Movie"}).json()["Items"] == []
        assert (
            client.get("/UserItems/Resume", params={"parentId": episode["SeasonId"]}).json()[
                "TotalRecordCount"
            ]
            == 1
        )
        assert client.get("/UserItems/Resume", params={"startIndex": -1}).status_code == 400
        payload = {"ItemId": identity, "PlaySessionId": "second", "PositionTicks": 0}
        client.post("/Sessions/Playing", json=payload)
        payload["PositionTicks"] = 1_100_000_000
        for _ in range(2):
            client.post("/Sessions/Playing/Progress", json=payload)
            client.post("/Sessions/Playing/Stopped", json=payload)
        assert state(client, identity)["Played"]
        assert state(client, identity)["PlayCount"] == 2
        assert client.get("/UserItems/Resume").json()["Items"] == []
        payload = {"ItemId": identity, "PlaySessionId": "rewatch", "PositionTicks": 0}
        client.post("/Sessions/Playing", json=payload)
        payload["PositionTicks"] = 500_000_000
        client.post("/Sessions/Playing/Progress", json=payload)
        assert state(client, identity)["Played"]  # Rewatch never erases watched history.
        assert state(client, identity)["PlayCount"] == 3
        assert len(client.get("/UserItems/Resume").json()["Items"]) == 1
        client.post(
            "/Sessions/Playing/Stopped", json={**payload, "Failed": True, "PositionTicks": 1_200_000_000}
        )
        assert state(client, identity)["PlaybackPositionTicks"] == 500_000_000
    with TestClient(create_app(config)) as client:
        jellyfin_login(client)
        client.post("/Sessions/Playing/Stopped", json=payload)
        assert state(client, identity)["PlayCount"] == 3
        with db.session() as session:
            history = list(session.scalars(select(PlaybackSession).order_by(PlaybackSession.id)))
            assert len(history) == 3
            assert history[1].completed and history[2].failed
        client.delete(f"/UserPlayedItems/{identity}")
        assert state(client, identity)["PlayCount"] == 0
        assert client.get("/UserItems/Resume").json()["Items"] == []


def add_more_episodes(core):
    _, db, _, _ = core
    with db.session() as session:
        first = session.scalar(select(LibraryAsset))
        asset = session.get(MediaAsset, first.asset_id)
        episodes = list(session.scalars(select(Episode).order_by(Episode.number)))
        for episode in episodes[1:]:
            # A distinct source per episode, even though this fixture reuses the tiny file.
            copy = MediaAsset(
                media_id=asset.media_id,
                download_id=asset.download_id,
                video_index=episode.id + 10,
                path=asset.path,
                resolution=asset.resolution,
                probe=asset.probe,
            )
            session.add(copy)
            session.flush()
            session.add(
                LibraryAsset(
                    media_id=first.media_id,
                    episode_id=episode.id,
                    part_key=f"episode:{episode.id}",
                    asset_id=copy.id,
                    preflight=first.preflight,
                    verification={"complete": True},
                )
            )


def test_next_up_order_resumable_rewatch_and_user_isolation(core, media, season):
    media.episode_numbering = {}
    playable_episode(core, media, season)
    add_more_episodes(core)
    # Remove the fixture's alternate numbering, so order is 1, 2, 3.
    from lazarr.models import Media

    with core[1].session() as session:
        row = session.scalar(select(Media))
        row.metadata_json = {**row.metadata_json, "episode_numbering": {}}
    with TestClient(create_app(core[0])) as client:
        alice = jellyfin_login(client)
        episodes = sorted(items(client), key=lambda i: i["IndexNumber"])
        assert len(episodes) == 3
        first, second, third = [i["Id"] for i in episodes]
        series = episodes[0]["SeriesId"]
        assert client.get("/Shows/NextUp").json()["Items"] == []
        assert client.get("/Shows/NextUp", params={"seriesId": series}).json()["Items"][0]["Id"] == first
        client.post(f"/UserPlayedItems/{first}", params={"datePlayed": "2020-01-01T00:00:00Z"})
        assert client.get("/Shows/NextUp").json()["Items"][0]["Id"] == second
        client.post("/Sessions/Playing/Progress", json={"ItemId": second, "PositionTicks": 500_000_000})
        assert client.get("/Shows/NextUp", params={"enableResumable": False}).json()["Items"] == []
        assert client.get("/Shows/NextUp").json()["Items"][0]["Id"] == second
        client.post(f"/UserPlayedItems/{second}", params={"datePlayed": "2020-01-02T00:00:00Z"})
        client.post(f"/UserPlayedItems/{third}", params={"datePlayed": "2020-01-03T00:00:00Z"})
        assert client.get("/Shows/NextUp").json()["Items"] == []
        client.post("/Sessions/Playing", json={"ItemId": first, "PlaySessionId": "rewatch"})
        client.post(
            "/Sessions/Playing/Stopped",
            json={"ItemId": first, "PlaySessionId": "rewatch", "PositionTicks": 1_100_000_000},
        )
        assert (
            client.get("/Shows/NextUp", params={"enableRewatching": True}).json()["Items"][0]["Id"] == second
        )
        assert (
            client.get(
                "/Shows/NextUp", params={"nextUpDateCutoff": "2099-01-01T00:00:00Z", "enableRewatching": True}
            ).json()["Items"]
            == []
        )
        jellyfin_login(client, "bob", "b-safe-password")
        assert client.get("/Shows/NextUp").json()["Items"] == []
        assert client.get("/Shows/NextUp", params={"userId": alice["User"]["Id"]}).status_code == 403


def test_version_resume_uses_exact_file_and_clears_on_completion(core, media, season):
    video, _ = playable_episode(core, media, season)
    with core[1].session() as session:
        link = session.scalar(select(LibraryAsset))
        asset = session.get(MediaAsset, link.asset_id)
        first_source = object_id("asset", asset.id)
        other_path = video.with_name("alternate.mkv")
        other_path.write_bytes(b"another version")
        second = MediaAsset(
            media_id=asset.media_id,
            download_id=asset.download_id,
            video_index=99,
            path=other_path.name,
            probe=asset.probe,
        )
        session.add(second)
        session.flush()
        second_source = object_id("asset", second.id)
        session.add(
            LibraryAsset(
                media_id=link.media_id,
                episode_id=link.episode_id,
                part_key=link.part_key,
                asset_id=second.id,
                preflight={},
                verification={"complete": True},
            )
        )
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        assert len(client.get(f"/Items/{identity}/PlaybackInfo").json()["MediaSources"]) == 2
        for source, position in [(first_source, 300_000_000), (second_source, 600_000_000)]:
            payload = {
                "ItemId": identity,
                "MediaSourceId": source,
                "PlaySessionId": source,
                "PositionTicks": position,
            }
            assert client.post("/Sessions/Playing/Progress", json=payload).status_code == 204
            assert state(client, source)["PlaybackPositionTicks"] == position
        resume = client.get("/UserItems/Resume").json()["Items"]
        assert len(resume) == 2
        assert {i["UserData"]["PlaybackPositionTicks"] for i in resume} == {300_000_000, 600_000_000}
        source = client.post(f"/Items/{identity}/PlaybackInfo", json={"MediaSourceId": first_source}).json()[
            "MediaSources"
        ][0]
        assert source["Id"] == first_source
        assert client.get(source["DirectStreamUrl"]).content == video.read_bytes()
        assert client.get(f"/Videos/{second_source}/stream").content == other_path.read_bytes()
        client.post(
            "/Sessions/Playing/Stopped",
            json={
                "ItemId": identity,
                "MediaSourceId": first_source,
                "PlaySessionId": first_source,
                "PositionTicks": 1_100_000_000,
            },
        )
        assert client.get("/UserItems/Resume").json()["Items"] == []
        assert state(client, second_source)["Played"]
        with core[1].session() as session:
            rows = list(session.scalars(select(PlaybackProgress).where(PlaybackProgress.user_id == 1)))
            assert all(row.position_ticks == 0 for row in rows)


def test_migration_preserves_existing_progress(core):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    import lazarr

    _, db, _, _ = core
    config = Config()
    config.set_main_option("script_location", str(Path(lazarr.__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", db.url)
    command.downgrade(config, "0012")
    from sqlalchemy import text

    identity = object_id("episode", 42)
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO playback_progress (user_id, item_id, position_ticks, played, play_count, last_played_at, updated_at) VALUES (1, :id, 123456, 0, 4, 100, 101)"
            ),
            {"id": identity},
        )
    db.migrate()
    with db.session() as session:
        row = session.scalar(select(PlaybackProgress))
        assert row.item_id == identity and row.position_ticks == 123456 and row.play_count == 4
        assert row.last_played_at == 100 and row.updated_at == 101
        assert row.is_favorite is False and row.rating is None and row.likes is None


def test_multipart_state_does_not_complete_another_part(core, media, season):
    video, _ = playable_episode(core, media, season)
    with core[1].session() as session:
        link = session.scalar(select(LibraryAsset))
        asset = session.get(MediaAsset, link.asset_id)
        source1 = object_id("asset", asset.id)
        part2 = MediaAsset(
            media_id=asset.media_id,
            download_id=asset.download_id,
            video_index=100,
            path=video.name,
            probe=asset.probe,
        )
        session.add(part2)
        session.flush()
        source2 = object_id("asset", part2.id)
        session.add(
            LibraryAsset(
                media_id=link.media_id,
                episode_id=link.episode_id,
                part_key="part:2",
                asset_id=part2.id,
                preflight={},
                verification={"complete": True},
            )
        )
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        client.post(
            "/Sessions/Playing/Progress",
            json={
                "ItemId": identity,
                "MediaSourceId": source2,
                "PlaySessionId": "part2",
                "PositionTicks": 400_000_000,
            },
        )
        client.post(
            "/Sessions/Playing/Stopped",
            json={
                "ItemId": identity,
                "MediaSourceId": source1,
                "PlaySessionId": "part1",
                "PositionTicks": 1_100_000_000,
            },
        )
        assert state(client, source1)["Played"]
        assert not state(client, source2)["Played"]
        assert state(client, source2)["PlaybackPositionTicks"] == 400_000_000
        assert not state(client, identity)["Played"]
        client.post(
            "/Sessions/Playing/Stopped",
            json={
                "ItemId": identity,
                "MediaSourceId": source2,
                "PlaySessionId": "part2",
                "PositionTicks": 1_100_000_000,
            },
        )
        assert state(client, identity)["Played"]


def test_userdata_position_update_applies_to_resume(core, media, season):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        client.post("/Sessions/Playing/Progress", json={"ItemId": identity, "PositionTicks": 400_000_000})
        assert client.get("/UserItems/Resume").json()["TotalRecordCount"] == 1
        assert (
            client.post(f"/UserItems/{identity}/UserData", json={"PlaybackPositionTicks": 0}).status_code
            == 200
        )
        assert client.get("/UserItems/Resume").json()["Items"] == []


def test_deleting_media_cleans_user_state_and_playlist_entries(core, media, season):
    from test_api import login
    from lazarr.models import Media, VideoPlaylist

    video, _ = playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        episode = items(client)[0]
        identity = episode["Id"]
        assert client.post(f"/UserFavoriteItems/{episode['SeasonId']}").status_code == 200
        client.post("/Sessions/Playing/Progress", json={"ItemId": identity, "PositionTicks": 400_000_000})
        playlist = client.post("/Playlists", json={"Name": "Keep the playlist", "Ids": [identity]}).json()[
            "Id"
        ]
        with core[1].session() as session:
            media_id = session.scalar(select(Media.id))
        login(client)
        result = client.request("DELETE", f"/api/v1/libraries/media/{media_id}", json={"delete_files": False})
        assert result.status_code == 200, result.text
        assert video.exists()
        assert client.get(f"/Playlists/{playlist}/Items").json()["Items"] == []
        with core[1].session() as session:
            assert list(session.scalars(select(PlaybackProgress))) == []
            assert list(session.scalars(select(PlaybackSession))) == []
            assert session.scalar(select(VideoPlaylist)).entries == []


def test_device_session_can_play_same_item_again_without_play_session_id(core, media, season):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        for _ in range(2):
            payload = {"ItemId": identity, "SessionId": "one-device", "PositionTicks": 0}
            assert client.post("/Sessions/Playing", json=payload).status_code == 204
            assert client.post("/Sessions/Playing", json=payload).status_code == 204
            payload["PositionTicks"] = 1_100_000_000
            assert client.post("/Sessions/Playing/Stopped", json=payload).status_code == 204
        assert state(client, identity)["PlayCount"] == 2
