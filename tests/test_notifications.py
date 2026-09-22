# ruff: noqa: F811 -- pytest fixtures imported from the worker test module
import pytest
from sqlalchemy import select

from lazarr.models import ConfigEntry, Subtask, TelegramUser
from lazarr.notifications import PREFIX, queue_episode_notification
from lazarr.security import SecretStore
from lazarr.services import CreateTask
from lazarr.telegram import TelegramError, TelegramService
from test_worker import worker_setup  # noqa: F401


@pytest.fixture
def notifications(core):
    config, db, _, _ = core
    with db.session() as session:
        session.add(ConfigEntry(key="telegram", value={"enabled": True, "bot_id": 123}))
        session.add_all(
            TelegramUser(bot_id=bot, user_id=i, chat_id=i, status=status)
            for i, bot, status in [(11, 123, "approved"), (12, 123, "pending"), (13, 456, "approved")]
        )
    return TelegramService(db, SecretStore(config.data_dir / "secret.key"), False)


def events(db):
    with db.session() as session:
        return list(session.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(PREFIX))))


async def test_search_and_probe_notifications(core, media, season, worker_setup, notifications):
    _, db, _, service = core
    worker, engine, _ = worker_setup
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1, 2]), media, season, 1
    )
    await worker.run_due()
    rows = events(db)
    assert len(rows) == 2
    assert all("Найдена раздача" in r.value["text"] for r in rows)
    assert all("Example Show" in r.value["text"] for r in rows)
    assert "S01E01" in rows[0].value["text"]
    assert "1080p" in rows[0].value["text"]
    await worker.run_due(force=True)
    assert len(events(db)) == 2

    async def bad_probe(*args):
        return {"ok": True, "streams": [{"codec_type": "video", "height": 480, "width": 640}]}

    worker._probe = bad_probe
    engine.completed = {1, 2}
    await worker.poll()
    assert len(events(db)) == 4
    assert sum("Требуется выбор" in r.value["text"] for r in events(db)) == 2


async def test_search_needs_selection(core, media, season, worker_setup, notifications):
    _, db, _, service = core
    worker, _, demo = worker_setup

    async def inspect(self, item):
        return item

    demo.inspect = inspect
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    await worker.run_due()
    with db.session() as session:
        assert session.get(Subtask, 1).status == "needs_selection"
    assert len(events(db)) == 1
    assert "Требуется выбор" in events(db)[0].value["text"]
    await worker.run_due(force=True)
    assert len(events(db)) == 1


async def test_durable_delivery_retry_and_authorization(core, media, season, notifications):
    _, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with db.session() as session:
        sub = session.get(Subtask, 1)
        queue_episode_notification(session, sub, "found", "Release")
        queue_episode_notification(session, sub, "found", "Release")
        user = session.scalar(select(TelegramUser).where(TelegramUser.user_id == 11))
        user.reply = "Existing menu reply"
    assert len(events(db)) == 1
    sent = []

    async def fail(*args, **kwargs):
        raise TelegramError(503)

    notifications.call = fail
    await notifications.deliver_notifications_once("fake", 123)
    assert events(db)[0].value["pending"]
    assert next(iter(events(db)[0].value["recipients"].values())) > 0
    with db.session() as session:
        row = session.get(ConfigEntry, events(db)[0].key)
        row.value = {**row.value, "recipients": {k: 0 for k in row.value["recipients"]}}
    restarted = TelegramService(db, notifications.secrets, False)

    async def send(token, method, **payload):
        sent.append(payload)

    restarted.call = send
    await restarted.deliver_notifications_once("fake", 456)
    assert sent == []
    await restarted.deliver_notifications_once("fake", 123)
    await restarted.deliver_notifications_once("fake", 123)
    assert [p["chat_id"] for p in sent] == [11]
    with db.session() as session:
        sub = session.get(Subtask, 1)
        queue_episode_notification(session, sub, "selection")
        user = session.scalar(select(TelegramUser).where(TelegramUser.user_id == 11))
        assert user.reply == "Existing menu reply"
        user.status = "blocked"
    await restarted.deliver_notifications_once("fake", 123)
    assert len(sent) == 1
    assert all(not r.value["pending"] for r in events(db))


def test_disabled_and_rolled_back_events_are_not_queued(core, media, season, notifications):
    _, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with pytest.raises(RuntimeError):
        with db.session() as session:
            queue_episode_notification(session, session.get(Subtask, 1), "found")
            raise RuntimeError("Worker transaction failed")
    assert events(db) == []
    with db.session() as session:
        cfg = session.get(ConfigEntry, "telegram")
        cfg.value = {**cfg.value, "enabled": False}
        queue_episode_notification(session, session.get(Subtask, 1), "found")
    assert events(db) == []
