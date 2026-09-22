import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lazarr.app import create_app
from lazarr.models import ConfigEntry, TelegramUser, User
from lazarr.security import SecretStore
from lazarr.telegram import TelegramService, TelegramError, REPLIES
from test_api import login


def start_update(identity=1, user_id=5000000001, chat_type="private"):
    return {
        "update_id": identity,
        "message": {
            "text": "/start",
            "from": {"id": user_id, "first_name": "<Alice>", "username": "alice"},
            "chat": {"id": user_id, "type": chat_type},
        },
    }


@pytest.fixture
def telegram(core):
    config, db, _, _ = core
    service = TelegramService(db, SecretStore(config.data_dir / "secret.key"), False)
    with db.session() as session:
        session.add(ConfigEntry(key="telegram", value={"bot_id": 123, "offset": 0}))
    return service


async def test_start_decision_block_restore_and_retry(telegram):
    sent = []

    async def call(token, method, **payload):
        sent.append(payload)
        return True

    telegram.call = call
    telegram.receive(123, start_update())
    telegram.receive(123, start_update())
    assert len(telegram.users()) == 1
    assert telegram.users()[0]["status"] == "pending"
    await telegram.deliver_once("secret", 123)
    assert sent[-1]["text"] == REPLIES["pending"]
    identity = telegram.users()[0]["id"]
    telegram.decide(identity, "blocked", 1)
    telegram.receive(123, start_update(2))
    assert telegram.users()[0]["status"] == "blocked"
    telegram.decide(identity, "approved", 1)

    async def fail(*args, **kwargs):
        raise TelegramError(403)

    telegram.call = fail
    await telegram.deliver_once("secret", 123)
    assert telegram.users()[0]["reply_pending"]
    assert telegram.users()[0]["delivery_error"]
    # /start after unblocking the bot retries delivery immediately.
    telegram.receive(123, start_update(3))
    telegram.call = call
    await telegram.deliver_once("secret", 123)
    assert sent[-1]["text"] == REPLIES["approved"]
    assert not telegram.users()[0]["reply_pending"]
    assert telegram.config()["offset"] == 4
    telegram.receive(123, start_update(4, 55, "group"))
    assert len(telegram.users()) == 1
    restarted = TelegramService(telegram.db, telegram.secrets, False)
    assert restarted.users()[0]["status"] == "approved"
    assert restarted.config()["offset"] == 5


async def test_delivery_does_not_erase_new_decision(telegram):
    telegram.receive(123, start_update())
    identity = telegram.users()[0]["id"]

    async def call(*args, **kwargs):
        telegram.decide(identity, "approved", 1)
        return True

    telegram.call = call
    await telegram.deliver_once("secret", 123)
    assert telegram.users()[0]["reply_pending"]
    with telegram.db.session() as db:
        assert db.get(TelegramUser, identity).reply == REPLIES["approved"]


def test_api_security_secret_preservation_and_bot_isolation(core, monkeypatch):
    config, db, _, _ = core
    bot_id = 123

    async def call(*args, **kwargs):
        return {"id": bot_id, "username": "lazarr_bot"}

    monkeypatch.setattr(TelegramService, "call", call)
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/telegram/users").status_code == 401
        login(client)
        assert client.put("/api/v1/telegram", json={"enabled": True}).status_code == 422
        assert (
            client.put("/api/v1/telegram", json={"enabled": True, "token": "123:secret"}).status_code == 200
        )
        assert "123:secret" not in client.get("/api/v1/telegram").text
        with db.session() as session:
            assert "123:secret" not in str(session.get(ConfigEntry, "telegram").value)
        assert client.put("/api/v1/telegram", json={"enabled": False}).status_code == 200
        assert client.get("/api/v1/telegram").json()["token_configured"]
        tg = client.app.state.ctx.telegram
        tg.receive(123, start_update())
        identity = tg.users()[0]["id"]
        url = f"/api/v1/telegram/users/{identity}"
        assert (
            client.patch(url, json={"status": "approved"}, headers={"x-csrf-token": "bad"}).status_code == 403
        )
        assert client.patch(url, json={"status": "approved"}).status_code == 200
        bot_id = 456
        assert client.put("/api/v1/telegram", json={"enabled": True, "token": "456:other"}).status_code == 200
        assert client.get("/api/v1/telegram/users").json() == []
        assert client.patch(url, json={"status": "approved"}).status_code == 422
        with db.session() as session:
            session.scalar(select(User).where(User.username == "alice")).role = "user"
        assert client.get("/api/v1/telegram").status_code == 403


async def test_background_polling_delivery_and_shutdown(telegram):
    delivered = asyncio.Event()
    polled = False

    async def call(token, method, **payload):
        nonlocal polled
        if method == "getUpdates":
            if not polled:
                polled = True
                return [start_update()]
            await asyncio.Event().wait()
        if method == "sendMessage":
            delivered.set()
            return True

    telegram.call = call
    telegram.background = True
    with telegram.db.session() as db:
        row = db.get(ConfigEntry, "telegram")
        row.value = {**row.value, "enabled": True, "secret": telegram.secrets.encrypt({"token": "secret"})}
    await telegram.start()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=5)
        assert telegram.users()[0]["status"] == "pending"
        assert telegram.config()["offset"] == 2
    finally:
        tasks = list(telegram.tasks)
        await telegram.stop()
    assert all(task.done() for task in tasks)
    assert telegram.tasks == []


async def test_transport_errors_do_not_expose_token(telegram, monkeypatch):
    async def post(self, url, **kwargs):
        raise httpx.ConnectError(url)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(TelegramError) as failure:
        await telegram.call("private-token", "getMe")
    assert "private-token" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.parametrize(
    "code, expected", [(401, "Недействительный токен"), (404, "не нашёл бота"), (400, "неверные параметры")]
)
async def test_telegram_api_errors_are_specific(telegram, monkeypatch, code, expected):
    async def post(self, url, **kwargs):
        return httpx.Response(
            code, json={"ok": False, "error_code": code, "description": "untrusted private-token"}
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(TelegramError, match=expected) as failure:
        await telegram.call("private-token", "getMe")
    assert "private-token" not in str(failure.value)


async def test_invalid_token_format_does_not_send_request(telegram):
    async def call(*args, **kwargs):
        pytest.fail("Invalid tokens must not be sent")

    telegram.call = call
    with pytest.raises(ValueError, match="Неверный формат токена"):
        await telegram.configure(True, "@example_bot", 1)
