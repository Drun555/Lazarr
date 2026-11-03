from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import httpx
import pytest
from sqlalchemy import event, select

from lazarr.library import LibraryService
from lazarr.library_metadata import media_nfo, sidecars, sync_artwork
from lazarr.models import ConfigEntry, Episode, Media, Season
from lazarr.services import CreateTask
from lazarr.storage import MANIFEST


async def test_artwork_batch_reads_manifest_twice(core, tmp_path, monkeypatch):
    _, db, _, _ = core
    root = tmp_path / "library"
    image = tmp_path / "cached.jpg"
    image.write_bytes(b"cached artwork")
    entries = [
        {"path": str(root / f"{i}.jpg"), "root": str(root), "image": "https://image.tmdb.org/t/p/w342/a.jpg"}
        for i in range(40)
    ]
    with db.session() as session:
        session.add(ConfigEntry(key=MANIFEST, value={"entries": entries}))
    calls = []

    async def fetch(*args, **kwargs):
        calls.append(args[1])
        return image

    monkeypatch.setattr("lazarr.posters.fetch_poster", fetch)
    reads = []

    def count_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and parameters == (MANIFEST,):
            reads.append(statement)

    event.listen(db.engine, "before_cursor_execute", count_reads)
    try:
        await sync_artwork(SimpleNamespace(db=db))
    finally:
        event.remove(db.engine, "before_cursor_execute", count_reads)
    assert len(reads) == 2
    assert calls == ["a.jpg"]
    assert all(Path(e["path"]).read_bytes() == image.read_bytes() for e in entries)
    modified = {e["path"]: Path(e["path"]).stat().st_mtime_ns for e in entries}
    await sync_artwork(SimpleNamespace(db=db))
    assert all(Path(path).stat().st_mtime_ns == stamp for path, stamp in modified.items())


@pytest.mark.parametrize("change", ["remove", "replace", "delete_manifest"])
async def test_artwork_batch_rechecks_ownership_after_fetch(core, tmp_path, monkeypatch, change):
    _, db, _, _ = core
    root = tmp_path / "library"
    entries = [
        {
            "path": str(root / f"{i}.jpg"),
            "root": str(root),
            "image": f"https://image.tmdb.org/t/p/w342/{i}.jpg",
        }
        for i in range(2)
    ]
    with db.session() as session:
        session.add(ConfigEntry(key=MANIFEST, value={"entries": entries}))
    image = tmp_path / "cached.jpg"
    image.write_bytes(b"artwork")

    async def fetch(ctx, name, **kwargs):
        if name == "1.jpg":
            with db.session() as session:
                row = session.get(ConfigEntry, MANIFEST)
                if change == "delete_manifest":
                    session.delete(row)
                elif change == "remove":
                    row.value = {"entries": entries[1:]}
                else:
                    row.value = {"entries": [{**entries[0], "image": entries[1]["image"]}, entries[1]]}
        return image

    monkeypatch.setattr("lazarr.posters.fetch_poster", fetch)
    await sync_artwork(SimpleNamespace(db=db))
    assert not Path(entries[0]["path"]).exists()
    assert Path(entries[1]["path"]).exists() == (change != "delete_manifest")


async def test_artwork_batch_continues_after_fetch_and_publish_errors(core, tmp_path, monkeypatch):
    _, db, _, _ = core
    root = tmp_path / "library"
    root.mkdir()
    entries = [
        {
            "path": str(root / f"{i}.jpg"),
            "root": str(root),
            "image": f"https://image.tmdb.org/t/p/w342/{i}.jpg",
        }
        for i in range(3)
    ]
    Path(entries[0]["path"]).write_bytes(b"previous artwork")
    # Refuse to overwrite a symlink, while still publishing the remaining image.
    Path(entries[1]["path"]).symlink_to(entries[0]["path"])
    with db.session() as session:
        session.add(ConfigEntry(key=MANIFEST, value={"entries": entries}))
    image = tmp_path / "cached.jpg"
    image.write_bytes(b"new artwork")

    async def fetch(ctx, name, **kwargs):
        if name == "0.jpg":
            raise OSError("Private remote URL must not reach status")
        return image

    monkeypatch.setattr("lazarr.posters.fetch_poster", fetch)
    await sync_artwork(SimpleNamespace(db=db))
    assert Path(entries[0]["path"]).read_bytes() == b"previous artwork"
    assert Path(entries[1]["path"]).is_symlink()
    assert Path(entries[2]["path"]).read_bytes() == b"new artwork"
    with db.session() as session:
        assert session.get(ConfigEntry, "storage.status").value["metadata_errors"] == [
            "Не удалось сохранить изображение: 0.jpg",
            "Не удалось сохранить изображение: 1.jpg",
        ]


async def test_tmdb_season_metadata_survives_database_and_nfo(core):
    _, db, plugins, service = core
    plugins.configure("tmdb", {"api_key": "a" * 32}, True)
    actor = {"id": 10, "name": "Actor & Co", "character": "Hero", "order": 0, "profile_path": "/actor.jpg"}
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/season/1"):
            assert request.url.params["append_to_response"] == "credits"
            return httpx.Response(
                200,
                json={
                    "id": 81,
                    "name": "Season one",
                    "overview": "Season plot",
                    "poster_path": "/season.jpg",
                    "air_date": "2020-01-01",
                    "vote_average": 8.2,
                    "credits": {"cast": [actor]},
                    "episodes": [
                        {
                            "id": 91,
                            "episode_number": 1,
                            "name": "Pilot",
                            "overview": "Episode plot",
                            "air_date": "2020-01-01",
                            "still_path": "/still.jpg",
                            "runtime": 47,
                            "vote_average": 8.4,
                            "vote_count": 123,
                            "guest_stars": [actor, {"id": 11, "name": "Guest", "character": "Visitor"}],
                            "crew": [
                                {"id": 12, "name": "Director", "job": "Director"},
                                {"id": 13, "name": "Writer", "job": "Screenplay"},
                            ],
                        }
                    ],
                },
            )
        assert "keywords" in request.url.params["append_to_response"]
        return httpx.Response(
            200,
            json={
                "id": 42,
                "name": "Show",
                "genres": [],
                "status": "Ended",
                "last_air_date": "2020-02-01",
                "tagline": "A tagline",
                "vote_average": 8.1,
                "vote_count": 456,
                "keywords": {"results": [{"name": "mystery"}, {"name": "mystery"}]},
                "credits": {"cast": [actor]},
                "videos": {"results": [{"site": "YouTube", "type": "Trailer", "key": "abc_123-xyz"}]},
                "seasons": [
                    {
                        "id": 81,
                        "season_number": 1,
                        "name": "Season one",
                        "episode_count": 1,
                        "poster_path": "/season.jpg",
                        "overview": "Season plot",
                    }
                ],
            },
        )

    plugins.transport = httpx.MockTransport(respond)
    async with plugins.open("tmdb") as provider:
        item = await provider.get_media("tv", "42")
        info = await provider.get_season("42", 1)
    assert len(calls) == 2  # No per-episode requests.
    assert item.seasons[0]["poster"].endswith("/season.jpg")
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), item, info, 1)
    with db.session() as session:
        media = session.scalar(select(Media))
        season = session.scalar(select(Season))
        episode = session.scalar(select(Episode))
        assert season.metadata_json["poster"].endswith("/season.jpg")
        assert episode.metadata_json["runtime"] == 47
        entries = sidecars(
            media,
            episode,
            season,
            {"season": 1, "episode": 1},
            Path("/library/Show/Season 01"),
            "episode",
            Path("/library"),
        )
        xml = {Path(e["path"]).name: ET.fromstring(e["content"]) for e in entries if "content" in e}
        show = xml["tvshow.nfo"]
        assert show.findtext("tagline") == "A tagline"
        assert show.findtext("enddate") == "2020-02-01"
        assert [e.text for e in show.findall("tag")] == ["mystery"]
        assert (
            show.findtext("trailer") == "plugin://plugin.video.youtube/?action=play_video&videoid=abc_123-xyz"
        )
        assert show.findtext("ratings/rating/votes") == "456"
        season_nfo = xml["season.nfo"]
        assert season_nfo.findtext("plot") == "Season plot"
        assert season_nfo.findtext("uniqueid[@type='tmdb']") == "81"
        assert season_nfo.findtext("ratings/rating/value") == "8.2"
        ep = xml["episode.nfo"]
        assert ep.findtext("runtime") == "47"
        assert ep.findtext("ratings/rating/value") == "8.4"
        assert ep.findtext("ratings/rating/votes") == "123"
        assert ep.findtext("director") == "Director"
        assert ep.findtext("writer") == "Writer"
        assert ep.findtext("actor/thumb").endswith("/actor.jpg")
        assert ep.findtext("actor/order") == "0"
        assert [e.findtext("name") for e in ep.findall("actor")] == ["Actor & Co", "Guest"]
        assert any(e.get("image", "").endswith("/season.jpg") for e in entries)
        # Renumbered exports keep episode data but must not claim the canonical season's identity/art.
        entries = sidecars(
            media,
            episode,
            season,
            {"season": 2, "episode": 14},
            Path("/library/Show/Season 02"),
            "episode",
            Path("/library"),
        )
        node = ET.fromstring(next(e["content"] for e in entries if e["path"].endswith("season.nfo")))
        assert node.find("uniqueid") is None and node.find("plot") is None
        assert not any(e.get("image", "").endswith("/season.jpg") for e in entries)


async def test_tmdb_movie_and_missing_optional_metadata(core):
    _, _, plugins, _ = core
    async with plugins.open("tmdb") as provider:
        item = provider.item(
            {
                "id": 12,
                "title": "Film",
                "runtime": 110,
                "tagline": "Tagline",
                "belongs_to_collection": {"id": 77, "name": "Collection"},
                "keywords": {"keywords": [{"name": "space"}]},
                "credits": {
                    "crew": [
                        {"id": 3, "name": "Writer", "job": "Story"},
                        {"id": 3, "name": "Writer", "job": "Screenplay"},
                    ]
                },
            },
            "movie",
        )
        minimal = provider.item({"id": 13, "title": "Minimal", "runtime": None, "tagline": None}, "movie")
    movie = Media(
        provider="tmdb", external_id=item.id, kind="movie", title=item.title, metadata_json=item.model_dump()
    )
    node = media_nfo(movie)
    assert node.findtext("runtime") == "110"
    assert node.findtext("collectionnumber") == "77"
    assert node.findtext("set/name") == "Collection"
    assert len(node.findall("writer")) == 1
    movie.metadata_json = minimal.model_dump()
    node = media_nfo(movie)
    assert node.find("runtime") is None and node.find("ratings") is None and node.find("tagline") is None


async def test_existing_metadata_enrichment_preserves_manual_titles(core, media, season, monkeypatch):
    _, db, plugins, service = core
    media.taxonomy_known = True
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with db.session() as session:
        stored = session.scalar(select(Media))
        identity = stored.id
        old = dict(stored.metadata_json)
        old.pop("tagline")
        stored.metadata_json = old
        saved_season = session.scalar(select(Season))
        saved_season.refreshed_at = 0
        saved_season.title = "Manual season"
        episode = session.scalar(select(Episode).where(Episode.number == 1))
        episode.title = "Manual episode"
        session.add(ConfigEntry(key=f"season_title.{saved_season.id}", value={"title": saved_season.title}))
        session.add(ConfigEntry(key=f"episode_title.{episode.id}", value={"title": episode.title}))
    media.tagline = "New tagline"
    season.poster = "https://image.tmdb.org/t/p/w342/season.jpg"
    season.episodes[0].runtime = 25
    calls = []

    async def get_media(*args):
        calls.append("media")
        return media

    async def get_season(*args):
        calls.append("season")
        return season

    monkeypatch.setattr(plugins.classes["tmdb"], "get_media", get_media)
    monkeypatch.setattr(plugins.classes["tmdb"], "get_season", get_season)
    library = LibraryService(db, plugins, service)
    await library.enrich()
    await library.enrich_media(identity)
    await library.enrich()
    await library.enrich_media(identity)
    assert calls == ["media", "season"]
    with db.session() as session:
        assert session.scalar(select(Media)).metadata_json["tagline"] == "New tagline"
        assert session.scalar(select(Season)).title == "Manual season"
        episode = session.scalar(select(Episode).where(Episode.number == 1))
        assert episode.title == "Manual episode" and episode.metadata_json["runtime"] == 25
