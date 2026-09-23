import re

import httpx
from fastapi.testclient import TestClient

from lazarr.app import create_app
from lazarr.config import RuntimeConfig
from lazarr.models import ConfigEntry, ProviderConfig
from lazarr.sdk import AuthResult, ProviderError
from lazarr.telegram import TelegramError
from test_api import login


def bootstrap(client):
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/setup").text)[1]
    result = client.post(
        "/api/v1/setup",
        headers={"x-csrf-token": csrf},
        json={
            "username": "admin",
            "password": "long-password",
            "password_confirm": "long-password",
        },
    )
    assert result.status_code == 201
    client.headers["x-csrf-token"] = result.json()["csrf"]


def test_bootstrap_resume_complete_and_access(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZARR_TRAWL_URL", "http://configured-trawl:8191")
    with TestClient(create_app(RuntimeConfig(tmp_path, background=False))) as client:
        assert client.post("/api/v1/onboarding/complete").status_code == 401
        bootstrap(client)
        assert client.get("/", follow_redirects=False).headers["location"] == "/onboarding"
        page = client.get("/onboarding")
        assert "http://configured-trawl:8191" in page.text
        assert "onboarding.js" in page.text
        assert client.post("/api/v1/onboarding/complete", headers={"x-csrf-token": "bad"}).status_code == 403
        assert client.post("/api/v1/onboarding/progress", json={"current": 2, "skip": 1}).status_code == 200
        assert '"current": 2' in client.get("/onboarding").text
        assert client.post("/api/v1/onboarding/complete").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 200
        assert client.get("/onboarding", follow_redirects=False).headers["location"] == "/"


def test_existing_users_not_forced_into_onboarding(core):
    with TestClient(create_app(core[0])) as client:
        login(client)
        assert client.get("/", follow_redirects=False).status_code == 200
        with client.app.state.ctx.db.session() as db:
            from lazarr.models import User

            db.get(User, 1).role = "viewer"
        assert client.get("/onboarding").status_code == 403
        assert client.post("/api/v1/onboarding/trawl", json={"url": "http://trawl:8191"}).status_code == 403


def test_trawl_checks_before_save_and_preserves_providers(core):
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx
        ctx.plugins.configure("rutracker", {"username": "original", "trawl_url": "http://old:8191"}, False)
        ctx.plugins.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "bad"}))
        assert client.post("/api/v1/onboarding/trawl", json={"url": "http://new:8191"}).status_code == 502
        with ctx.db.session() as db:
            assert db.get(ProviderConfig, "rutracker").config["trawl_url"] == "http://old:8191"
        seen = []

        def healthy(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"status": "ok"})

        ctx.plugins.transport = httpx.MockTransport(healthy)
        assert client.post("/api/v1/onboarding/trawl", json={"url": "http://new:8191/"}).status_code == 200
        assert seen == ["http://new:8191/health"]
        for provider in ctx.plugins.describe():
            if provider["id"] in {"rutracker", "kinozal"}:
                assert provider["config"]["trawl_url"] == "http://new:8191"
                assert not provider["enabled"]
        assert ctx.plugins.secret("rutracker", "username") == "original"
        with ctx.db.session() as db:
            assert db.get(ConfigEntry, "onboarding.1").value["current"] == 1
        for url in ["file:///etc/passwd", "http://user:pass@host", "http://host?secret=123"]:
            assert client.post("/api/v1/onboarding/trawl", json={"url": url}).status_code == 422


def test_tmdb_only_saves_validated_key(core, monkeypatch):
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx
        old, new = "a" * 32, "b" * 32
        ctx.plugins.configure("tmdb", {"api_key": old}, True)

        async def reject(self):
            assert self.ctx.config["api_key"] == new
            raise ProviderError("auth_required", "Ключ не принят")

        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "healthcheck", reject)
        assert client.post("/api/v1/onboarding/tmdb", json={"api_key": new}).status_code >= 400
        assert ctx.plugins.secret("tmdb", "api_key") == old

        async def accept(self):
            assert self.ctx.config["api_key"] == new
            return {"ok": True}

        monkeypatch.setattr(ctx.plugins.classes["tmdb"], "healthcheck", accept)
        result = client.post("/api/v1/onboarding/tmdb", json={"api_key": new})
        assert result.status_code == 200 and new not in result.text
        assert ctx.plugins.secret("tmdb", "api_key") == new
        assert new not in client.get("/onboarding").text


def test_rutracker_challenge_not_saved_then_success_keeps_session(core, monkeypatch):
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx

        attempts = []

        async def authenticate(self, values):
            attempts.append(values)
            assert values["username"] == "test-user"
            if len(attempts) == 1:
                self.ctx.state["challenge_tokens"] = {"id": "challenge"}
                return AuthResult(status="challenge", message="Введите CAPTCHA")
            assert "challenge_tokens" not in self.ctx.state
            self.ctx.state["authenticated"] = True
            return AuthResult(status="authenticated")

        monkeypatch.setattr(ctx.plugins.classes["rutracker"], "authenticate", authenticate)
        payload = {"username": "test-user", "password": "test-password"}
        result = client.post("/api/v1/onboarding/rutracker", json=payload)
        assert result.json()["status"] == "challenge"
        assert ctx.plugins.secret("rutracker", "password") is None
        result = client.post("/api/v1/onboarding/rutracker", json=payload)
        assert result.json()["status"] == "authenticated"
        assert ctx.plugins.secret("rutracker", "password") == "test-password"
        with ctx.db.session() as db:
            row = db.get(ProviderConfig, "rutracker")
            assert row.enabled
            assert ctx.secret_store.decrypt(row.session_state)["authenticated"] is True


def test_telegram_failure_not_marked_success(core, monkeypatch):
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx

        async def reject(*args):
            raise TelegramError("Недействительный токен")

        monkeypatch.setattr(ctx.telegram, "configure", reject)
        assert client.post("/api/v1/onboarding/telegram", json={"token": "123:abc"}).status_code == 502
        with ctx.db.session() as db:
            assert db.get(ConfigEntry, "onboarding.1") is None
        calls = []

        async def accept(*args):
            calls.append(args)

        monkeypatch.setattr(ctx.telegram, "configure", accept)
        assert client.post("/api/v1/onboarding/telegram", json={"token": "123:abc"}).status_code == 200
        assert calls == [(True, "123:abc", 1)]
