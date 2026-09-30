import re
import time
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.models import ProviderConfig, User
from lazarr.services import CreateTask


def login(client):
    response = client.get("/login")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', response.text)[1]
    result = client.post(
        "/api/v1/session",
        json={"username": "alice", "password": "a-safe-password"},
        headers={"x-csrf-token": csrf},
    )
    assert result.status_code == 200, result.text
    client.headers["x-csrf-token"] = result.json()["csrf"]


def test_auth_csrf_accounts_and_ui(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/tasks").status_code == 401
        login(client)
        page = client.get("/")
        assert page.status_code == 200 and "Название фильма, сериала или аниме" in page.text
        assert 'hx-sync="this:replace"' in page.text
        assert client.get("/static/app.js").status_code == 200
        response = client.post(
            "/api/v1/accounts", json={"username": "charlie", "password": "another-password"}
        )
        assert response.status_code == 201, response.text
        assert len(client.get("/api/v1/accounts").json()) == 3
        result = client.post(
            "/api/v1/accounts",
            json={"username": "d", "password": "another-password"},
            headers={"x-csrf-token": "bad"},
        )
        assert result.status_code == 403
        assert "password_hash" not in str(client.get("/api/v1/accounts").json())
        assert client.delete("/api/v1/session").status_code == 200
        assert client.get("/api/v1/tasks").status_code == 401


def test_theme_color_setting_persists_and_rejects_unknown_presets(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        login(client)
        settings = client.get("/api/v1/settings").json()
        assert settings["theme_color"] == "purple"
        settings["theme_color"] = "green"
        assert client.put("/api/v1/settings", json=settings).status_code == 200
        assert client.get("/api/v1/settings").json()["theme_color"] == "green"
        assert '<html lang="ru" data-accent="green">' in client.get("/").text
        assert '<html lang="ru" data-accent="green">' in client.get("/login").text
        settings["theme_color"] = "invalid"
        assert client.put("/api/v1/settings", json=settings).status_code == 422
        assert client.get("/api/v1/settings").json()["theme_color"] == "green"


def test_create_task_and_provider_secret_via_api(core, media, season, monkeypatch):
    config, db, _, _ = core
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx

        async def get_media(self, kind, media_id):
            return media

        async def get_season(self, media_id, number):
            return season

        async def search(self, query):
            return [media]

        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", get_media)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", get_season)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "search", search)
        assert client.get("/ui/search?q=Example").status_code == 200
        result = client.post("/api/v1/tasks", json={"media_id": "42", "kind": "tv", "season": 1})
        assert result.status_code == 201, result.text
        tasks = client.get("/api/v1/tasks").json()
        assert len(tasks[0]["subtasks"]) == 3
        result = client.patch(f"/api/v1/tasks/{tasks[0]['id']}", json={"paused": True})
        assert result.status_code == 200, result.text
        assert (
            client.put(
                "/api/v1/providers/tmdb", json={"enabled": True, "config": {"api_key": "secret-value"}}
            ).status_code
            == 200
        )
        assert "secret-value" not in client.get("/api/v1/providers").text
        reveal = client.post("/api/v1/providers/tmdb/secrets/api_key/reveal")
        assert reveal.status_code == 200
        assert reveal.json() == {"value": "secret-value"}
        assert reveal.headers["cache-control"] == "no-store"
        assert client.post("/api/v1/providers/tmdb/secrets/base_url/reveal").status_code == 404
        assert (
            client.post(
                "/api/v1/providers/tmdb/secrets/api_key/reveal", headers={"x-csrf-token": "bad"}
            ).status_code
            == 403
        )
        assert client.get("/openapi.json").status_code == 200


def test_subtask_search_log_requires_auth_and_is_scoped_to_its_task(core, media, season):
    config, _, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/subtasks/1/search").status_code == 401
        assert client.get("/api/v1/tasks/1/seasons/1/candidates").status_code == 401
        login(client)
        result = client.get("/api/v1/subtasks/1/search")
        assert result.status_code == 200
        assert isinstance(result.json()["history"], list)
        assert result.json()["message"]
        assert client.get("/api/v1/tasks/1/seasons/1/candidates").json() == []
        assert client.get("/api/v1/subtasks/999/search").status_code == 404


def test_manual_candidate_url_endpoint_delegates_to_worker(core, media, season, monkeypatch):
    config, _, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        if ctx.engine is None:

            class StubEngine:
                def close(self):
                    pass

            ctx.engine = StubEngine()
        received = []

        async def add_manual_candidate(subtask_id, url):
            received.append(("subtask", subtask_id, url))
            return 77

        async def add_manual_task_candidate(task_id, url, season_number=None):
            received.append(("task", task_id, season_number, url))
            return 78

        monkeypatch.setattr(ctx.worker, "add_manual_candidate", add_manual_candidate)
        monkeypatch.setattr(ctx.worker, "add_manual_task_candidate", add_manual_task_candidate)
        response = client.post(
            "/api/v1/subtasks/1/candidates/manual",
            json={"url": "https://nyaa.si/view/321"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"id": 77}
        response = client.post(
            "/api/v1/tasks/1/seasons/1/candidates/manual",
            json={"url": "https://nyaa.si/view/322"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"id": 78}
        assert received == [
            ("subtask", 1, "https://nyaa.si/view/321"),
            ("task", 1, 1, "https://nyaa.si/view/322"),
        ]


def test_permissions_are_enforced_centrally(core):
    config, db, _, _ = core
    with db.session() as session:
        session.get(User, 1).role = "user"
    with TestClient(create_app(config)) as client:
        login(client)
        assert client.get("/api/v1/accounts").status_code == 403
        assert client.get("/api/v1/settings").status_code == 403
        assert client.get("/api/v1/tasks").status_code == 200


def test_manual_provider_controls_bypass_cooldown(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        ctx.plugins.configure("rutracker", {"session_cookie": "private-session"}, True)
        with ctx.db.session() as session:
            row = session.get(ProviderConfig, "rutracker")
            row.retry_at = time.time() + 300
            row.last_error = "unavailable: temporary outage"

        response = client.post("/api/v1/providers/rutracker/authenticate", json={"values": {}})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "authenticated"


def test_delete_api_requires_auth_and_csrf(core, media, season):
    from lazarr.services import CreateTask

    config, _, _, service = core
    task = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        url = f"/api/v1/tasks/{task}"
        assert client.request("DELETE", url, json={"delete_media": True}).status_code == 401
        login(client)
        assert (
            client.request(
                "DELETE", url, json={"delete_media": True}, headers={"x-csrf-token": "wrong"}
            ).status_code
            == 403
        )
        assert client.request("DELETE", url, json={"delete_media": False}).status_code == 200
        assert client.get("/api/v1/tasks").json() == []


def test_jellyfin_api_removed_and_legacy_settings_ignored(core):
    from lazarr.config import Settings

    settings = Settings.model_validate({"jellyfin": {"audio_languages": ["ja"]}})
    assert "jellyfin" not in settings.model_dump()
    with TestClient(create_app(core[0])) as client:
        for route in ("/System/Info/Public", "/Items", "/Users/Me", "/Shows/NextUp", "/Videos/1/stream"):
            assert client.get(route).status_code == 404
        assert client.post("/Users/AuthenticateByName", json={}).status_code == 404
        login(client)
        assert "jellyfin" not in client.get("/api/v1/settings").json()
        assert "settings-jellyfin" not in client.get("/").text


def test_create_special_season_fetches_dictionary_season_catalog(core, monkeypatch):
    from lazarr.models import ConfigEntry
    from lazarr.sdk import EpisodeInfo, MetadataItem, SeasonInfo

    config, db, _, _ = core
    item = MetadataItem(
        id="46195",
        kind="tv",
        title="Specials regression",
        seasons=[{"number": n, "episode_count": 1} for n in (0, 1, 2)],
    )
    calls = []

    async def get_media(self, kind, media_id):
        return item

    async def get_season(self, media_id, number):
        calls.append(number)
        return SeasonInfo(
            number=number,
            episodes=[
                EpisodeInfo(
                    id=str(number + 10),
                    number=1,
                    title=f"Episode {number}",
                    air_date={0: "2020-06-01", 1: "2020-01-01", 2: "2021-01-01"}[number],
                )
            ],
        )

    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_media", get_media)
        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "get_season", get_season)
        response = client.post(
            "/api/v1/tasks",
            json={
                "provider": "tmdb",
                "media_id": "46195",
                "kind": "tv",
                "requirements": {
                    "audio_languages": ["ja"],
                    "subtitle_languages": ["ru"],
                    "min_resolution": 1080,
                    "max_resolution": 1080,
                    "keyword": "",
                },
                "seasons": [{"season": 0, "episodes": None}],
            },
        )
        assert response.status_code == 201, response.text
        assert calls == [0, 1, 2]
        task_id = response.json()["id"]
        mapping = client.get(f"/api/v1/tasks/{task_id}/seasons/0/mapping")
        assert mapping.status_code == 200, mapping.text
        assert mapping.json()["episodes"][0]["special_position"]["position"] == {"airsafter_season": 1}
        assert [s["number"] for s in mapping.json()["placement_seasons"]] == [1, 2]
        # Only specials are scheduled; other seasons were fetched solely for their dates.
        tasks = client.get("/api/v1/tasks").json()
        assert len(tasks[0]["subtasks"]) == 1
        with db.session() as session:
            session.get(ConfigEntry, "special_catalog.1").value = {"updated": 0, "seasons": []}
        refreshed = client.get(f"/api/v1/tasks/{task_id}/seasons/0/mapping")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["episodes"][0]["special_position"]["position"] == {"airsafter_season": 1}
        assert calls == [0, 1, 2, 1, 2]


def test_task_pause_does_not_wait_for_search_lock(core, media, season, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from lazarr.models import Task

    config, db, _, service = core
    identity = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        worker = client.app.state.ctx.worker

        class BusySearchLock:
            async def __aenter__(self):
                raise AssertionError("Pause must not acquire the search lock")

            async def __aexit__(self, *args):
                pass

        monkeypatch.setattr(worker, "lock", BusySearchLock())
        monkeypatch.setattr(worker, "cancel_media_search", Mock())
        monkeypatch.setattr(worker, "sync_consumers", AsyncMock())
        for paused in (True, False):
            response = client.patch(f"/api/v1/tasks/{identity}", json={"paused": paused})
            assert response.status_code == 200, response.text
            with db.session() as session:
                assert session.get(Task, identity).paused is paused
        worker.cancel_media_search.assert_called_once_with(identity)
        assert worker.sync_consumers.await_count == 2
