import re
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.models import User


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
        assert client.get("/openapi.json").status_code == 200


def test_permissions_are_enforced_centrally(core):
    config, db, _, _ = core
    with db.session() as session:
        session.get(User, 1).role = "user"
    with TestClient(create_app(config)) as client:
        login(client)
        assert client.get("/api/v1/accounts").status_code == 403
        assert client.get("/api/v1/settings").status_code == 403
        assert client.get("/api/v1/tasks").status_code == 200


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
