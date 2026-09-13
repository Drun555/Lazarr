import re
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from sqlalchemy import select
from lazarr.app import create_app
from lazarr.config import RuntimeConfig
from lazarr.models import User, AuditEvent
from lazarr.security import verify_password


def setup_csrf(client):
    page = client.get("/setup")
    return re.search(r'name="csrf-token" content="([^"]+)"', page.text)[1]


def test_first_run_creates_session_and_closes_bootstrap(tmp_path):
    with TestClient(create_app(RuntimeConfig(tmp_path, background=False))) as client:
        assert str(client.get("/").url).endswith("/setup")
        csrf = setup_csrf(client)
        payload = {"username": " admin ", "password": "long-password", "password_confirm": "long-password"}
        assert client.post("/api/v1/setup", json=payload).status_code == 403
        headers = {"x-csrf-token": csrf}
        assert (
            client.post(
                "/api/v1/setup", json={**payload, "password_confirm": "other-password"}, headers=headers
            ).status_code
            == 422
        )
        result = client.post("/api/v1/setup", json=payload, headers=headers)
        assert result.status_code == 201
        assert client.get("/api/v1/accounts").json()[0]["username"] == "admin"
        with client.app.state.ctx.db.session() as db:
            user = db.scalar(select(User))
            assert user.role == "admin" and verify_password(user.password_hash, payload["password"])
            assert db.scalar(select(AuditEvent)).action == "account.bootstrap"
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/login").text)[1]
        assert client.post("/api/v1/setup", json=payload, headers={"x-csrf-token": csrf}).status_code == 409
        assert str(client.get("/setup").url).endswith("/login")


def test_concurrent_bootstrap_has_exactly_one_winner(tmp_path):
    with TestClient(create_app(RuntimeConfig(tmp_path, background=False))) as client:
        token = setup_csrf(client)

        def create(name):
            return client.post(
                "/api/v1/setup",
                json={"username": name, "password": "long-password", "password_confirm": "long-password"},
                headers={"x-csrf-token": token, "cookie": f"lazarr_login_csrf={token}"},
            ).status_code

        with ThreadPoolExecutor(2) as pool:
            codes = list(pool.map(create, ["one", "two"]))
        assert sorted(codes) == [201, 409]
        with client.app.state.ctx.db.session() as db:
            assert len(list(db.scalars(select(User)))) == 1
