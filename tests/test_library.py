import time
from sqlalchemy import select
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.library import LibraryService, library_kind
from lazarr.models import Media, SubtaskAsset, MediaAsset, Download
from lazarr.sdk import MetadataItem, ProviderError
from lazarr.services import CreateTask
from test_api import login
from test_worker import worker_setup as worker_setup


def test_classification_includes_anime_movies_but_not_japanese_live_action():
    def classify(kind, genres, countries, lang=""):
        return library_kind(
            Media(
                kind=kind,
                metadata_json={"genre_ids": genres, "origin_countries": countries, "original_language": lang},
            )
        )

    assert classify("movie", [16], ["JP"]) == "anime"
    assert classify("tv", [16], [], "ja") == "anime"
    assert classify("tv", [18], ["JP"], "ja") == "series"
    assert classify("movie", [16], ["US"], "en") == "movies"
    assert classify("tv", [], []) == "series"


async def test_shared_media_detail_tracks_release_calendar_last_search(core, media, season, worker_setup):
    _, db, plugins, service = core
    worker, engine, _ = worker_setup
    media.taxonomy_known = True
    media.genre_ids = [16]
    media.original_language = "ja"
    media.episode_numbering = {"1:1": [{"season": 2, "episode": 14}]}
    season.episodes[0].overview = "Описание серии"
    season.episodes[0].still = "https://image.tmdb.org/t/p/w342/still.jpg"
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 2
    )
    library = LibraryService(db, plugins, service)
    groups = library.list()
    assert [g["name"] for g in groups] == ["Сериалы", "Кино", "Аниме"]
    assert len(groups[2]["items"]) == 1
    identity = groups[2]["items"][0]["id"]
    before = library.detail(identity)
    assert before["task_count"] == 2 and before["last_search_at"] is None
    assert any(e["season"] == 2 and e["episode"] == 14 for e in before["episodes"])
    # Use canonical filename numbering for this synthetic worker fixture.
    with db.session() as session:
        row = session.get(Media, identity)
        row.metadata_json = {**row.metadata_json, "episode_numbering": {}}
    await worker.run_due()
    detail = library.detail(identity)
    assert detail["last_search_at"] and detail["last_search_at"] <= time.time()
    first = next(e for e in detail["episodes"] if e["episode"] == 1)
    assert first["overview"] == "Описание серии" and first["still"].endswith("/still.jpg")
    assert first["subtasks"] and first["subtasks"][0]["id"]
    assert len(first["files"]) == 1  # Shared physical asset, two users.
    file = first["files"][0]
    assert file["pending"] and not file["verified"]
    assert file["resolution"] == 1080 and file["release"]["provider"] == "demo"
    assert first["air_date"] == "2020-01-01"
    with db.session() as session:
        asset = session.get(MediaAsset, file["id"])
        download = session.get(Download, asset.download_id)
        subtask_ids = [item["id"] for item in first["subtasks"]]
        download.state = "downloading"
        download.stats = {
            "progress": 0.4,
            "download_rate": 2048,
            "bindings": {
                str(subtask_identity): {"progress": 0.25, "eta": 90}
                for subtask_identity in subtask_ids
            },
        }
        asset.probe = {
            "streams": [
                {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "rus"}}
            ]
        }
        for link in session.scalars(select(SubtaskAsset).where(SubtaskAsset.asset_id == asset.id)):
            link.current = True
            link.pending = False
            link.verification = {"complete": True}
    tile = library.list()[2]["items"][0]
    assert tile["download"] == {"state": "downloading", "progress": 0.4, "download_rate": 2048}
    first = next(e for e in library.detail(identity)["episodes"] if e["episode"] == 1)
    assert first["download"]["progress"] == 0.25 and first["download"]["eta"] == 90
    file = next(e for e in library.detail(identity)["episodes"] if e["episode"] == 1)["files"][0]
    assert file["verified"] and file["tracks"][0]["language"] == "ru" and file["tracks"][0]["codec"] == "aac"


def test_library_api_auth_movie_empty_file_and_not_found(core):
    config, db, _, service = core
    movie = MetadataItem(id="8", kind="movie", title="Movie", taxonomy_known=True, release_date="2999-01-01")
    service.create_from_metadata(CreateTask(media_id="8", kind="movie"), movie, None, 1)
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/libraries").status_code == 401
        login(client)
        groups = client.get("/api/v1/libraries").json()
        item = groups[1]["items"][0]
        detail = client.get(f"/api/v1/libraries/media/{item['id']}").json()
        assert len(detail["episodes"]) == 1
        assert detail["episodes"][0]["files"] == [] and not detail["episodes"][0]["released"]
        assert client.get("/api/v1/libraries/media/99999").status_code == 404


def test_library_media_delete_removes_shared_media_tasks_and_requires_csrf(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[2]), media, season, 2
    )
    with db.session() as session:
        identity = session.scalar(select(Media.id))
    url = f"/api/v1/libraries/media/{identity}"
    with TestClient(create_app(config)) as client:
        assert client.request("DELETE", url, json={"delete_files": False}).status_code == 401
        login(client)
        assert (
            client.request(
                "DELETE", url, json={"delete_files": False}, headers={"x-csrf-token": "wrong"}
            ).status_code
            == 403
        )
        result = client.request("DELETE", url, json={"delete_files": False})
        assert result.status_code == 200 and result.json()["tasks_deleted"] == 2
        assert all(not group["items"] for group in client.get("/api/v1/libraries").json())
        assert client.get("/api/v1/tasks").json() == []
        assert client.get(url).status_code == 404


async def test_enrichment_is_offline_tolerant_and_does_not_change_numbering(core, media, season, monkeypatch):
    _, db, plugins, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    library = LibraryService(db, plugins, service)

    async def offline(*args):
        raise ProviderError("unavailable", "offline")

    monkeypatch.setattr(plugins.classes["tmdb"], "get_media", offline)
    await library.enrich()
    assert len(library.list()[0]["items"]) == 1

    async def enriched(*args):
        return media.model_copy(update={"taxonomy_known": True, "genre_ids": [16], "original_language": "ja"})

    monkeypatch.setattr(plugins.classes["tmdb"], "get_media", enriched)
    from lazarr.models import ProviderConfig

    with db.session() as session:
        session.get(ProviderConfig, "tmdb").retry_at = 0
    await library.enrich()
    assert len(library.list()[2]["items"]) == 1
