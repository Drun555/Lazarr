from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.jellyfin import object_id
from lazarr.library import LibraryService
from lazarr.models import Download, Media, MediaAsset, ProviderConfig
from lazarr.sdk import ProviderContext, ProviderError, SubtitleCandidate, SubtitleFile, SubtitleRequest
from lazarr.subtitles import SubtitleService
from test_jellyfin import jellyfin_login, playable_episode


async def test_opensubtitles_plugin_uses_episode_identity_key_and_download_link():
    from lazarr.bundled.opensubtitles import Plugin

    requests = []

    def transport(request):
        requests.append(request)
        if request.url.path.endswith("/subtitles"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "attributes": {
                                "language": "ru",
                                "download_count": 12,
                                "ratings": 7.5,
                                "files": [{"file_id": 9, "file_name": "episode.ru.srt"}],
                            }
                        }
                    ]
                },
            )
        if request.url.path.endswith("/download"):
            return httpx.Response(
                200, json={"link": "https://download.example/subtitle", "file_name": "episode.ru.srt"}
            )
        return httpx.Response(200, content=b"subtitle text")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        plugin = Plugin(
            ProviderContext(
                {"api_key": "secret", "base_url": "https://api.opensubtitles.test/api/v1"}, {}, client
            )
        )
        candidates = await plugin.search(
            SubtitleRequest(
                media_kind="episode",
                title="Show",
                season=2,
                episode=14,
                languages=["ru"],
                external_ids={"tmdb": "123"},
            )
        )
        downloaded = await plugin.download(candidates[0])

    assert requests[0].headers["api-key"] == "secret"
    assert requests[0].url.params["tmdb_id"] == "123"
    assert requests[0].url.params["season_number"] == "2"
    assert requests[0].url.params["episode_number"] == "14"
    assert downloaded.content == b"subtitle text" and downloaded.filename == "episode.ru.srt"


async def test_podnapisi_searches_alias_and_extracts_anonymous_zip_download():
    from lazarr.bundled.podnapisi import Plugin

    requests = []
    archive = BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("Re.Zero.S02E14.ru.srt", b"1\n00:00:00,000 --> 00:00:01,000\nTest\n")

    def transport(request):
        requests.append(request)
        if request.url.path.endswith("/search/advanced"):
            if request.url.params["keywords"] == "Локализованное название":
                return httpx.Response(200, json={"page": 1, "all_pages": 1, "data": []})
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "all_pages": 1,
                    "data": [
                        {
                            "id": "abc1",
                            "language": "ru",
                            "flags": ["high_definition"],
                            "releases": ["Re.Zero.S02E14.1080p.WEB-DL"],
                            "custom_releases": [],
                            "stats": {"downloads": 321},
                            "movie": {
                                "type": "tv-series",
                                "episode_info": {"season": 2, "episode": 14},
                            },
                        }
                    ],
                },
            )
        return httpx.Response(200, content=archive.getvalue())

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        plugin = Plugin(ProviderContext({"base_url": "https://podnapisi.test"}, {}, client))
        candidates = await plugin.search(
            SubtitleRequest(
                media_kind="episode",
                title="Локализованное название",
                aliases=["Re:Zero"],
                year=2020,
                season=2,
                episode=14,
                languages=["ru"],
            )
        )
        downloaded = await plugin.download(candidates[0])

    search_requests = [request for request in requests if request.url.path.endswith("/search/advanced")]
    assert [request.url.params["keywords"] for request in search_requests] == [
        "Локализованное название",
        "Re:Zero",
    ]
    assert search_requests[-1].url.params.get_list("movie_type") == ["tv-series", "mini-series"]
    assert search_requests[-1].url.params["seasons"] == "2"
    assert search_requests[-1].url.params["episodes"] == "14"
    assert candidates[0].downloads == 321 and candidates[0].language == "ru"
    assert requests[-1].url.params["container"] == "zip"
    assert downloaded.filename == "Re.Zero.S02E14.ru.srt"
    assert downloaded.content.endswith(b"Test\n")


async def test_downloads_missing_subtitle_registers_library_and_jellyfin(core, media, season, monkeypatch):
    playable_episode(core, media, season)
    config, db, plugins, service = core
    with db.session() as session:
        media_id = session.scalar(select(Media.id))
        row = session.get(ProviderConfig, "opensubtitles")
        row.enabled = True

    seen = []

    async def search(provider, query):
        seen.append(query)
        return [
            SubtitleCandidate(
                id="55",
                language="eng",
                filename="Example.Show.S02E14.srt",
                downloads=100,
                rating=8.5,
            )
        ]

    async def download(provider, candidate):
        return SubtitleFile(content=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n", filename=candidate.filename)

    monkeypatch.setattr(plugins.classes["opensubtitles"], "search", search)
    monkeypatch.setattr(plugins.classes["opensubtitles"], "download", download)
    result = await SubtitleService(db, plugins, service).download(media_id, ["en"], 1)
    assert len(result["downloaded"]) == 1 and result["errors"] == []
    assert seen[0].season == 2 and seen[0].episode == 14
    assert seen[0].external_ids["tmdb"] == "1"
    skipped = await SubtitleService(db, plugins, service).download(media_id, ["ru"], 1)
    assert skipped["skipped"] == 1 and len(seen) == 1
    subtitle_path = result["downloaded"][0]["path"]
    with db.session() as session:
        asset = session.scalar(select(MediaAsset))
        assert asset.tracks[0]["language"] == "en" and asset.tracks[0]["source"] == "opensubtitles"
        root = Path(session.get(Download, asset.download_id).save_path)
        result_path = root / subtitle_path
        assert result_path.is_file()

    detail = LibraryService(db, plugins, service).detail(media_id)
    playable_detail = next(episode for episode in detail["episodes"] if episode["files"])
    tracks = playable_detail["files"][0]["tracks"]
    assert any(track["language"] == "en" and track["source"] == "opensubtitles" for track in tracks)

    with TestClient(create_app(config)) as client:
        jellyfin_login(client)
        episode_id = object_id("episode", playable_detail["id"])
        streams = client.post(f"/Items/{episode_id}/PlaybackInfo", json={}).json()["MediaSources"][0][
            "MediaStreams"
        ]
        subtitle = next(stream for stream in streams if stream["Language"] == "eng")
        assert subtitle["IsExternal"] is True
        assert client.get(subtitle["DeliveryUrl"]).content == result_path.read_bytes()


async def test_subtitle_service_falls_back_to_next_enabled_provider(core, media, season, monkeypatch):
    playable_episode(core, media, season)
    _, db, plugins, service = core
    with db.session() as session:
        media_id = session.scalar(select(Media.id))
        session.get(ProviderConfig, "opensubtitles").enabled = True
        session.get(ProviderConfig, "podnapisi").enabled = True

    async def unavailable(provider, query):
        raise ProviderError("configuration", "API key отсутствует")

    async def search(provider, query):
        return [SubtitleCandidate(id="pod-1", language="en", filename="episode.srt", downloads=10)]

    async def download(provider, candidate):
        return SubtitleFile(content=b"Podnapisi subtitle", filename=candidate.filename)

    monkeypatch.setattr(plugins, "available", lambda kind=None: ["opensubtitles", "podnapisi"])
    monkeypatch.setattr(plugins.classes["opensubtitles"], "search", unavailable)
    monkeypatch.setattr(plugins.classes["podnapisi"], "search", search)
    monkeypatch.setattr(plugins.classes["podnapisi"], "download", download)
    result = await SubtitleService(db, plugins, service).download(media_id, ["en"], 1)

    assert result["errors"] == [] and result["not_found"] == []
    assert result["downloaded"][0]["provider"] == "podnapisi"
    with db.session() as session:
        asset = session.scalar(select(MediaAsset))
        track = next(value for value in asset.tracks if value["language"] == "en")
        assert track["source"] == "podnapisi"
        assert ".podnapisi.en.srt" in track["path"]


def test_subtitle_api_requires_login_and_existing_media(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        assert client.post("/api/v1/libraries/media/1/subtitles", json={}).status_code == 401
        from test_api import login

        login(client)
        assert client.post("/api/v1/libraries/media/999/subtitles", json={}).status_code == 404
