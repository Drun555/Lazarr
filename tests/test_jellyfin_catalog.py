from fastapi.testclient import TestClient
from sqlalchemy import event, select

from lazarr.app import create_app
from lazarr.jellyfin import object_id, user_object_id
from lazarr.jellyfin_catalog import named_id
from lazarr.models import LibraryAsset, Media
from test_jellyfin import jellyfin_login, playable_episode
from test_jellyfin_state import add_more_episodes


def test_catalog_filters_discovery_projection_and_scope(core, media, season):
    playable_episode(core, media, season)
    with core[1].session() as db:
        row = db.scalar(select(Media))
        row.metadata_json = {
            **row.metadata_json,
            "genres": ["Drama", "Adventure"],
            "studios": ["Studio A"],
            "people": [{"Name": "An Actor", "Type": "Actor"}],
            "community_rating": 8.5,
        }
        series_id = object_id("media", row.id)
    with TestClient(create_app(core[0])) as client:
        auth = jellyfin_login(client)
        query = {
            "recursive": "true",
            "includeItemTypes": "Episode",
            "genres": "Comedy|Drama",
            "audioLanguages": "ja",
            "minWidth": 1920,
            "hasSubtitles": "true",
            "minCommunityRating": 8,
            "fields": "Genres,MediaStreams",
            "enableUserData": "false",
            "enableImages": "false",
        }
        response = client.get("/Items", params=query)
        assert response.status_code == 200, response.text
        assert response.json()["TotalRecordCount"] == 1
        episode = response.json()["Items"][0]
        assert "MediaStreams" in episode and "MediaSources" not in episode and "Path" not in episode
        assert "UserData" not in episode and "ImageTags" not in episode
        assert client.get("/Items", params={**query, "minWidth": "nan"}).status_code == 400
        assert client.get("/Items", params={**query, "audioLanguages": "de"}).json()["Items"] == []
        assert client.get("/Items", params={"ids": episode["Id"]}).json()["Items"][0]["Id"] == episode["Id"]
        genres = client.get("/Genres").json()["Items"]
        genre = next(g for g in genres if g["Name"] == "Drama")
        assert genre["Id"] == named_id("Genre", "Drama")
        assert client.get(f"/Items/{genre['Id']}").json()["Name"] == "Drama"
        assert (
            client.get("/Items", params={"parentId": genre["Id"], "includeItemTypes": "Series"}).json()[
                "Items"
            ][0]["Id"]
            == series_id
        )
        assert client.get("/Persons").json()["Items"][0]["Name"] == "An Actor"
        assert client.get("/Studios").json()["Items"][0]["Name"] == "Studio A"
        assert client.get("/Years").json()["TotalRecordCount"] >= 1
        assert client.get("/Items/Counts").json()["EpisodeCount"] == 1
        filters = client.get("/Items/Filters2").json()
        assert genre["Id"] in [g["Id"] for g in filters["Genres"]]
        assert "jpn" in [v["Value"] for v in filters["AudioLanguages"]]
        hints = client.get("/Search/Hints", params={"searchTerm": "Actor", "includeMedia": "false"}).json()
        assert hints["SearchHints"][0]["Type"] == "Person"
        ancestors = client.get(f"/Items/{episode['Id']}/Ancestors").json()
        assert [i["Type"] for i in ancestors] == ["Season", "Series", "CollectionFolder"]
        bob_id = user_object_id(client.app.state.ctx, 2)
        for route in (
            "/Items/Counts",
            "/Genres",
            "/Search/Hints",
            f"/Items/{episode['Id']}/Ancestors",
            "/Shows/Upcoming",
        ):
            assert client.get(route, params={"userId": bob_id}).status_code == 403
        assert client.get("/Items/Filters", params={"userId": auth["User"]["Id"]}).status_code == 200


def test_latest_multisort_suggestions_and_similarity(core, media, season):
    playable_episode(core, media, season)
    with core[1].session() as db:
        db.scalar(select(LibraryAsset)).created_at = 2000
        for name, year, rating, genres in [
            ("Z newest", 2023, 9, ["Drama"]),
            ("A older", 2022, 9, ["Drama"]),
            ("B unrelated", 2021, 4, ["Comedy"]),
        ]:
            db.add(
                Media(
                    provider="demo",
                    external_id=name,
                    kind="movie",
                    title=name,
                    year=year,
                    metadata_json={
                        "genres": genres,
                        "community_rating": rating,
                        "taxonomy_known": True,
                        "backdrop": None,
                        "people": [],
                    },
                )
            )
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        rows = client.get(
            "/Items",
            params={
                "recursive": "true",
                "includeItemTypes": "Movie",
                "sortBy": "CommunityRating,Name",
                "sortOrder": "Descending,Ascending",
            },
        ).json()["Items"]
        assert [i["Name"] for i in rows] == ["A older", "Z newest", "B unrelated"]
        latest = client.get("/Items/Latest", params={"groupItems": "false"}).json()
        assert latest[0]["Type"] == "Episode"
        assert client.get("/Items/Latest").json()[0]["Type"] == "Series"
        baseline = rows[0]
        similar = client.get(f"/Items/{baseline['Id']}/Similar").json()["Items"]
        assert [i["Name"] for i in similar] == ["Z newest"]
        assert client.post(f"/UserFavoriteItems/{baseline['Id']}").status_code == 200
        recs = client.get("/Movies/Recommendations").json()
        assert recs[0]["RecommendationType"] == "SimilarToLikedItem"
        assert client.get("/Items/Suggestions", params={"limit": 1}).json()["TotalRecordCount"] == 4
        assert client.get("/Items", params={"recursive": "true", "sortOrder": "Wrong"}).status_code == 400


def test_media_lists_filter_before_dto_work_and_batch_queries(core, media, season, monkeypatch):
    template = season.episodes[0]
    season.episodes = [
        template.model_copy(update={"id": str(number), "number": number, "title": f"Episode {number}"})
        for number in range(1, 81)
    ]
    media.seasons[0]["episode_count"] = len(season.episodes)
    playable_episode(core, media, season)
    add_more_episodes(core)

    import lazarr.jellyfin as jellyfin

    original_episode_dto = jellyfin.episode_dto
    episode_dto_calls = 0

    def counted_episode_dto(*args, **kwargs):
        nonlocal episode_dto_calls
        episode_dto_calls += 1
        return original_episode_dto(*args, **kwargs)

    monkeypatch.setattr(jellyfin, "episode_dto", counted_episode_dto)
    with TestClient(create_app(core[0])) as client:
        jellyfin_login(client)
        anime = next(item for item in client.get("/UserViews").json()["Items"] if item["Name"] == "Аниме")

        series = client.get(
            "/Items",
            params={
                "parentId": anime["Id"],
                "recursive": "true",
                "includeItemTypes": "Series",
            },
        )
        assert series.status_code == 200
        assert [item["Type"] for item in series.json()["Items"]] == ["Series"]
        assert episode_dto_calls == 0

        latest = client.get("/Items/Latest", params={"parentId": anime["Id"], "limit": 1})
        assert latest.status_code == 200 and latest.json()[0]["Type"] == "Series"
        assert episode_dto_calls == 0

        statements = []

        def record_query(*args):
            statements.append(args[2])

        engine = client.app.state.ctx.db.engine
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            episodes = client.get(
                "/Items",
                params={
                    "parentId": anime["Id"],
                    "recursive": "true",
                    "includeItemTypes": "Episode",
                },
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert episodes.status_code == 200
        assert len(episodes.json()["Items"]) == 80
        assert episode_dto_calls == 80
        assert len(statements) < 30, f"expected batched reads, got {len(statements)} SQL statements"
