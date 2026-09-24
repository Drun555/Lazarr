"""Playback retries must not receive a method the client has rejected."""

import pytest
from fastapi.testclient import TestClient

from lazarr.app import create_app
from test_jellyfin import jellyfin_login, playable_episode
from test_jellyfin_state import items


@pytest.mark.parametrize("query", [False, True])
def test_transcode_only_retry_reports_no_compatible_stream(core, media, season, query):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        flags = {"EnableDirectPlay": False, "EnableDirectStream": False, "EnableTranscoding": True}
        result = client.post(
            f"/Items/{identity}/PlaybackInfo",
            params=flags if query else None,
            json={"EnableDirectPlay": True, "EnableDirectStream": True} if query else flags,
        )
        assert result.status_code == 200
        body = result.json()
        assert body["ErrorCode"] == "NoCompatibleStream"
        for source in body["MediaSources"]:
            assert source["SupportsDirectPlay"] is False
            assert source["SupportsDirectStream"] is False
            assert source["SupportsTranscoding"] is False
            assert "TranscodingUrl" not in source


def test_direct_stream_retry_has_a_working_session_authorized_url(core, media, season):
    video, _ = playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        result = client.post(
            f"/Items/{identity}/PlaybackInfo",
            json={"EnableDirectPlay": False, "EnableDirectStream": True},
        )
        assert result.status_code == 200
        body = result.json()
        assert "ErrorCode" not in body
        source = body["MediaSources"][0]
        assert source["SupportsDirectPlay"] is False
        assert source["SupportsDirectStream"] is True
        assert source["SupportsTranscoding"] is False
        client.headers.pop("X-Emby-Token")
        stream = client.get(source["TranscodingUrl"], headers={"Range": "bytes=0-3"})
        assert stream.status_code == 206
        assert stream.content == video.read_bytes()[:4]


def test_query_can_enable_direct_play_over_body_and_null_uses_default(core, media, season):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        identity = items(client)[0]["Id"]
        for params, body in [
            ({"enableDirectPlay": "true"}, {"EnableDirectPlay": False, "EnableDirectStream": False}),
            ({}, {"EnableDirectPlay": None}),
        ]:
            result = client.post(f"/Items/{identity}/PlaybackInfo", params=params, json=body)
            assert result.status_code == 200
            assert "ErrorCode" not in result.json()
            assert result.json()["MediaSources"][0]["SupportsDirectPlay"] is True
