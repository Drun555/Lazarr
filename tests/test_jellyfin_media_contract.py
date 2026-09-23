"""Catalog and playback responses must satisfy the strict Kotlin SDK models."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.models import MediaAsset
from test_jellyfin import jellyfin_login, playable_episode
from test_jellyfin_login_contract import assert_model


def assert_media_source(source):
    assert_model(source, "MediaSourceInfo")
    for stream in source.get("MediaStreams", []):
        assert_model(stream, "MediaStream")
    for attachment in source.get("MediaAttachments", []):
        assert_model(attachment, "MediaAttachment")


def assert_item(item):
    assert_model(item, "BaseItemDto")
    if item.get("UserData"):
        assert_model(item["UserData"], "UserItemDataDto")
    for source in item.get("MediaSources", []):
        assert_media_source(source)
    for stream in item.get("MediaStreams", []):
        assert_model(stream, "MediaStream")
    for chapter in item.get("Chapters", []):
        assert_model(chapter, "ChapterInfo")


def test_catalog_and_playback_satisfy_kotlin_sdk(core, media, season):
    playable_episode(core, media, season)
    with core[1].session() as db:
        asset = db.scalar(select(MediaAsset))
        probe = dict(asset.probe)
        probe["streams"][0]["field_order"] = "tt"
        probe["streams"][1]["disposition"] = {"hearing_impaired": 1}
        probe["chapters"] = [{"start_time": "0", "end_time": "10", "tags": {"title": "Intro"}}]
        asset.probe = probe
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        response = client.get("/Items", params={"Recursive": True})
        assert response.status_code == 200
        assert_model(response.json(), "BaseItemDtoQueryResult")
        for item in response.json()["Items"]:
            assert_item(item)
        episode = next(i for i in response.json()["Items"] if i["Type"] == "Episode")
        detail = client.get(f"/Items/{episode['Id']}")
        assert detail.status_code == 200
        assert_item(detail.json())
        playback = client.post(f"/Items/{episode['Id']}/PlaybackInfo", json={})
        assert playback.status_code == 200
        assert_model(playback.json(), "PlaybackInfoResponse")
        for source in playback.json()["MediaSources"]:
            assert_media_source(source)
            assert source["HasSegments"] is True
            streams = source["MediaStreams"]
            assert next(s for s in streams if s["Type"] == "Video")["IsInterlaced"] is True
            assert any(s["IsHearingImpaired"] for s in streams if s["Type"] == "Audio")
            assert any(s["IsExternal"] for s in streams if s["Type"] == "Subtitle")
