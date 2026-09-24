"""Resume must not stall another client or assemble the entire library."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from lazarr import jellyfin
from lazarr.app import create_app
from test_jellyfin import jellyfin_login, playable_episode
from test_jellyfin_state import items


@pytest.mark.parametrize("legacy", [False, True])
def test_resume_keeps_other_client_streaming(core, media, season, monkeypatch, legacy):
    video, _ = playable_episode(core, media, season)
    started, release = threading.Event(), threading.Event()
    with TestClient(create_app(core[0])) as client:
        first = jellyfin_login(client)
        episode = items(client)[0]
        assert (
            client.post(
                f"/UserItems/{episode['Id']}/UserData", json={"PlaybackPositionTicks": 100_000_000}
            ).status_code
            == 200
        )
        second = jellyfin_login(client)
        original = jellyfin.item_dto

        def slow_item(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(jellyfin, "item_dto", slow_item)
        route = f"/Users/{first['User']['Id']}/Items/Resume" if legacy else "/UserItems/Resume"
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(client.get, route, headers={"X-Emby-Token": first["AccessToken"]})
            try:
                assert started.wait(2)
                health = pool.submit(client.get, "/health").result(timeout=2)
                assert health.status_code == 200
                stream = pool.submit(
                    client.get,
                    f"/Videos/{episode['Id']}/stream",
                    headers={"X-Emby-Token": second["AccessToken"], "Range": "bytes=0-3"},
                ).result(timeout=2)
                assert stream.status_code == 206
                assert stream.content == video.read_bytes()[:4]
                assert not pending.done()
            finally:
                release.set()
            assert pending.result(timeout=2).status_code == 200


def test_resume_library_filter_only_builds_resumed_episode(core, media, season, monkeypatch):
    playable_episode(core, media, season)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        episode = items(client)[0]
        assert (
            client.post(
                "/Sessions/Playing/Progress",
                json={"ItemId": episode["Id"], "PlaySessionId": "resume", "PositionTicks": 100_000_000},
            ).status_code
            == 204
        )
        libraries = jellyfin.library_ids(client.app.state.ctx)
        original = jellyfin.episode_dto
        built = []

        def episode_dto(ctx, media, episode_data, *args, **kwargs):
            built.append(episode_data["id"])
            return original(ctx, media, episode_data, *args, **kwargs)

        def unexpected_media(*args, **kwargs):
            pytest.fail("Resume assembled an unrelated library media DTO")

        monkeypatch.setattr(jellyfin, "episode_dto", episode_dto)
        monkeypatch.setattr(jellyfin, "media_dto", unexpected_media)
        response = client.get("/UserItems/Resume", params={"parentId": libraries["anime"].upper()})
        assert response.status_code == 200
        assert [item["Id"] for item in response.json()["Items"]] == [episode["Id"]]
        assert built == [jellyfin.parse_object_id(episode["Id"])[1]]
        built.clear()
        for key in ("series", "movies"):
            response = client.get("/UserItems/Resume", params={"parentId": libraries[key]})
            assert response.status_code == 200
            assert response.json()["Items"] == []
        assert built == []
        assert client.get("/UserItems/Resume", params={"parentId": "bad-id"}).status_code == 404
