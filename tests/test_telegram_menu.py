import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from lazarr.models import ConfigEntry, TelegramUser, Task, Subtask
from lazarr.security import SecretStore
from lazarr.telegram import TelegramService
from lazarr.telegram_menu import TelegramMenu, collage


@pytest.fixture
def menu(core, media, season, monkeypatch):
    config, db, plugins, service = core
    progress = {"running": False, "pending_requests": 1, "state": "queued"}
    ctx = SimpleNamespace(
        config=config,
        db=db,
        plugins=plugins,
        service=service,
        engine=object(),
        scheduler=SimpleNamespace(wake=asyncio.Event(), snapshot=lambda _: dict(progress)),
    )
    bot = TelegramService(db, SecretStore(config.data_dir / "secret.key"), False)
    wizard = TelegramMenu(ctx, bot)
    bot.menu = wizard
    calls = []

    async def call(token, method, **payload):
        calls.append((method, payload))
        return {"message_id": len(calls) + 100} if method in {"sendMessage", "sendPhoto"} else True

    async def search(self, query):
        return [media.model_copy(update={"id": str(i), "title": f"Example {i}"}) for i in range(8)]

    async def get_media(self, kind, identity):
        return media.model_copy(update={"id": identity, "kind": kind})

    async def get_season(self, identity, number):
        return season

    monkeypatch.setattr(plugins.classes["tmdb"], "search", search)
    monkeypatch.setattr(plugins.classes["tmdb"], "get_media", get_media)
    monkeypatch.setattr(plugins.classes["tmdb"], "get_season", get_season)
    bot.call = call
    with db.session() as session:
        session.add(ConfigEntry(key="telegram", value={"bot_id": 123, "offset": 0}))
        session.add(TelegramUser(bot_id=123, user_id=1001, chat_id=1001, status="approved", approved_by=1))

    async def event(text=None, action=None, raw=None):
        user = wizard.user(1)
        update_id = bot.config().get("offset", 0)
        update = {"update_id": update_id}
        if action:
            update["callback_query"] = {
                "id": str(update_id),
                "from": {"id": 1001},
                "message": {"message_id": user.dialog["message_id"], "chat": {"id": 1001, "type": "private"}},
                "data": f"{user.dialog['revision']}:{action}",
            }
        else:
            update["message"] = {
                "message_id": update_id + 1000,
                "from": {"id": 1001},
                "chat": {"id": 1001, "type": "private"},
                "text": text,
            }
        if raw:
            update = {**raw, "update_id": update_id}
        bot.receive(123, update)
        await wizard.tick("token", 123)
        await bot.deliver_once("token", 123)
        return update

    return wizard, bot, event, progress, calls


async def test_wizard_collage_pagination_languages_task_and_automatic_result(menu):
    wizard, bot, event, progress, calls = menu
    await event("/search")
    assert wizard.user(1).reply == ""  # delivered
    assert any(p.get("text") == "Введите наименование кино или сериала" for _, p in calls)
    await event("Example")
    photo = next(p["photo_bytes"] for method, p in calls if method == "sendPhoto")
    assert Image.open(BytesIO(photo)).size == (768, 756)
    await event(action="page:1")
    assert wizard.user(1).dialog["page"] == 1
    await event(action="media:6")
    assert wizard.user(1).dialog["stage"] == "season"
    await event(action="season:1")
    await event(action="lang:ja")
    await event(action="next")
    await event(action="lang:ru")
    old = await event(action="next")
    user = wizard.user(1)
    assert user.dialog["stage"] == "waiting"
    assert any(p.get("text") == "Задача создана. Выполняется поиск..." for _, p in calls)
    task_id = user.dialog["task"]
    with wizard.ctx.db.session() as db:
        task = db.get(Task, task_id)
        assert task.requirements["audio_languages"] == ["ru", "ja"]
        assert task.requirements["subtitle_languages"] == ["ru"]
        assert len(list(db.scalars(select(Task)))) == 1
        for sub in db.scalars(select(Subtask).where(Subtask.task_id == task_id)):
            sub.status = "downloading"
    await event(raw=old)  # stale buttons must not create or change a task
    progress.update(pending_requests=0, state="finished")
    await wizard.tick("token", 123)
    assert wizard.user(1).reply == "Раздача определена автоматически. Загрузка запущена."
    assert any(method == "deleteMessage" for method, _ in calls)
    assert any(method == "editMessageText" for method, _ in calls)


async def test_notification_opens_paginated_episode_choice_with_confirmation(
    menu, media, season, monkeypatch
):
    from lazarr.notifications import queue_episode_notification
    from lazarr.services import CreateTask

    wizard, bot, event, progress, calls = menu
    monkeypatch.setattr("lazarr.notifications.COALESCE_SECONDS", 0)
    task_id = wizard.ctx.service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1), media, season, 1
    )
    with wizard.ctx.db.session() as db:
        cfg = db.get(ConfigEntry, "telegram")
        cfg.value = {**cfg.value, "enabled": True}
        for sub in db.scalars(select(Subtask).where(Subtask.task_id == task_id)):
            sub.status = "needs_selection"
            queue_episode_notification(db, sub, "selection")
    await bot.deliver_notifications_once("token", 123)
    payload = calls[-1][1]
    notice = {
        "callback_query": {
            "id": "notice",
            "from": {"id": 1001},
            "message": {"message_id": len(calls) + 100, "chat": {"id": 1001, "type": "private"}},
            "data": payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
        }
    }
    await event(raw=notice)
    assert wizard.user(1).dialog["stage"] == "notice_episodes"
    selected = []
    choices = [
        {
            "id": i,
            "candidate": {"title": f"Release {i}", "size": 1024**3, "seeds": 10},
            "action": "",
            "report": {"binding": {"video_index": 0}, "criteria": []},
        }
        for i in range(1, 9)
    ]
    monkeypatch.setattr(wizard.ctx.service, "candidates", lambda sub: choices)

    async def choose(
        decision, actor, *, expected_subtask, task_id, pending_only, preview=False, allowed_subtasks=None
    ):
        assert pending_only and task_id
        if preview:
            return {"subtask_ids": [1, 2], "episodes": ["S01E01", "S01E02"], "selected": 2, "total": 3}
        assert allowed_subtasks == [1, 2]
        selected.append((decision, actor, expected_subtask))
        with wizard.ctx.db.session() as db:
            for sub_id in allowed_subtasks:
                db.get(Subtask, sub_id).status = "starting"
        return {"selected": 2, "total": 2, "skipped": 0}

    wizard.ctx.worker = SimpleNamespace(choose_all=choose)
    await event(action="notice-episode:1")
    assert wizard.user(1).dialog["stage"] == "notice_candidates"
    await event(action="notice-cpage:1")
    assert wizard.user(1).dialog["choice_page"] == 1
    await event(action="notice-choice:7")
    assert not selected
    assert wizard.user(1).dialog["stage"] == "notice_confirm"
    assert any("2 из 3" in p.get("text", "") and "S01E02" in p.get("text", "") for _, p in calls)
    old = await event(action="notice-confirm:7")
    assert selected == [(7, 1, 1)]
    await event(raw=old)
    assert len(selected) == 1
    await event(action="notice-back")
    buttons = wizard.user(1).dialog["view"]["buttons"]
    assert not any(action == "notice-episode:1" for row in buttons for _, action in row)
    assert not any(action == "notice-episode:2" for row in buttons for _, action in row)
    assert any(action == "notice-episode:3" for row in buttons for _, action in row)
    before = wizard.user(1).dialog
    forged = {
        "callback_query": {
            **notice["callback_query"],
            "message": {"message_id": 9999, "chat": {"id": 1001, "type": "private"}},
        }
    }
    await event(raw=forged)
    assert wizard.user(1).dialog == before


async def test_candidates_are_scoped_and_only_offered_choices_can_be_selected(menu, monkeypatch):
    wizard, bot, event, progress, calls = menu
    await event("/search")
    await event("Example")
    await event(action="media:0")
    await event(action="season:1")
    await event(action="next")
    await event(action="next")
    progress.update(pending_requests=0, state="finished")
    choices = [
        {
            "id": 44,
            "matched": 1,
            "total": 3,
            "episodes": [{"action": "evaluated"}],
            "candidate": {"title": "Release", "seeds": 10},
        }
    ]
    monkeypatch.setattr(wizard.ctx.service, "task_candidates", lambda task_id, season_number: choices)
    selected = []

    async def choose(identity, actor, season_number, task_id):
        selected.append((identity, actor, season_number, task_id))
        return {"selected": 1, "total": 3, "skipped": 2}

    wizard.ctx.worker = SimpleNamespace(choose_all=choose)
    await wizard.tick("token", 123)
    await bot.deliver_once("token", 123)
    assert wizard.user(1).dialog["stage"] == "candidates"
    await event(action="choose:999")
    assert selected == []
    await event(action="choose:44")
    assert selected == [(44, 1, 1, wizard.user(1).dialog["task"])]
    assert any("1 из 3" in p.get("text", "") for _, p in calls)


async def test_revocation_blocks_queued_commands_and_state_survives_restart(menu):
    wizard, bot, event, progress, calls = menu
    await event("/search")
    restored = TelegramMenu(wizard.ctx, bot)
    assert restored.user(1).dialog["stage"] == "query"
    bot.decide(1, "blocked", 1)
    await event("Example")
    assert not wizard.user(1).inbox
    assert "results" not in wizard.user(1).dialog


async def test_movie_skips_season_and_no_results_remains_searchable(menu, monkeypatch):
    wizard, bot, event, progress, calls = menu
    from lazarr.sdk import MetadataItem

    async def search(self, query):
        return [] if query == "missing" else [MetadataItem(id="99", kind="movie", title="Film")]

    monkeypatch.setattr(wizard.ctx.plugins.classes["tmdb"], "search", search)
    await event("/search")
    await event("missing")
    assert wizard.user(1).dialog["stage"] == "query"
    await event("Film")
    await event(action="media:0")
    assert wizard.user(1).dialog["stage"] == "audio"
    await event(action="clear")
    await event(action="next")
    await event(action="clear")
    await event(action="next")
    with wizard.ctx.db.session() as db:
        task = db.get(Task, wizard.user(1).dialog["task"])
        assert task.requirements["audio_languages"] == []
        assert len(list(db.scalars(select(Subtask).where(Subtask.task_id == task.id)))) == 1


def test_collage_uses_posters_and_falls_back_for_missing_images(tmp_path):
    path = tmp_path / "poster.png"
    Image.new("RGB", (80, 120), "red").save(path)
    output = Image.open(BytesIO(collage([path, None], 6)))
    assert output.size == (516, 384)
    assert output.getpixel((130, 200))[0] > 240


async def test_fast_language_taps_keep_working_until_next_screen(menu):
    wizard, bot, event, progress, calls = menu
    await event("/search")
    await event("Example")
    await event(action="media:0")
    await event(action="season:1")
    revision = wizard.user(1).dialog["revision"]
    message_id = wizard.user(1).dialog["message_id"]

    def press(action):
        return {
            "callback_query": {
                "id": "fast-tap",
                "from": {"id": 1001},
                "message": {"message_id": message_id, "chat": {"id": 1001, "type": "private"}},
                "data": f"{revision}:{action}",
            }
        }

    await event(raw=press("lang:ja"))
    await event(raw=press("lang:ru"))
    assert wizard.user(1).dialog["audio"] == ["ja"]
    assert any("Выбрано: Японский." in p.get("text", "") for _, p in calls)
    await event(raw=press("next"))
    assert wizard.user(1).dialog["stage"] == "subtitles"
    await event(raw=press("lang:en"))
    assert wizard.user(1).dialog["subtitles"] == []
