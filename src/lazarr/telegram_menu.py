"""Persistent, private-chat media wizard using Lazarr's existing task pipeline."""

import asyncio
from copy import deepcopy
from io import BytesIO
import math
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps
from sqlalchemy import select

from lazarr.config import Requirements
from lazarr.languages import LABELS
from lazarr.models import TelegramUser, User, Task, Subtask, Episode, Season
from lazarr.posters import fetch_poster
from lazarr.sdk import ProviderError
from lazarr.security import audit, permitted
from lazarr.services import CreateTask
from lazarr.telegram import TelegramError

PAGE = 6


def collage(paths, first=0):
    """Numbered poster grid; text labels live in the matching Telegram buttons."""
    width, height, gap = 240, 360, 12
    columns = min(3, len(paths))
    canvas = Image.new(
        "RGB",
        (columns * (width + gap) + gap, math.ceil(len(paths) / columns) * (height + gap) + gap),
        "#151923",
    )
    font = ImageFont.load_default(size=30)
    for index, path in enumerate(paths):
        x, y = gap + index % columns * (width + gap), gap + index // columns * (height + gap)
        tile = Image.new("RGB", (width, height), "#32394b")
        if path:
            try:
                with Image.open(path) as source:
                    if source.width * source.height <= 20_000_000:
                        tile = ImageOps.fit(source.convert("RGB"), (width, height))
            except (OSError, ValueError, Image.DecompressionBombError):
                pass
        canvas.paste(tile, (x, y))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((x + 8, y + 8, x + 65, y + 54), radius=8, fill="#151923")
        draw.text((x + 19, y + 12), str(first + index + 1), font=font, fill="white")
    output = BytesIO()
    canvas.save(output, "JPEG", quality=85)
    return output.getvalue()


class TelegramMenu:
    def __init__(self, ctx, bot):
        self.ctx, self.bot = ctx, bot
        self.commands_set = None

    def user(self, identity):
        with self.ctx.db.session() as db:
            return db.get(TelegramUser, identity)

    def actor(self, identity):
        with self.ctx.db.session() as db:
            user = db.get(TelegramUser, identity)
            actor = db.get(User, user.approved_by) if user and user.approved_by else None
            if not user or user.status != "approved" or not actor or not permitted(actor, "tasks"):
                raise ValueError("Доступ не разрешён. Обратитесь к администратору Lazarr.")
            return actor.id

    def save(self, identity, dialog, text=None, buttons=None, posters=None):
        with self.ctx.db.session() as db:
            user = db.get(TelegramUser, identity)
            if not user or user.status != "approved":
                return
            # Rendering records message IDs independently; retain the newest IDs.
            for key in ("message_id", "sent_kind", "garbage"):
                if key in (user.dialog or {}):
                    dialog[key] = user.dialog[key]
            if text is not None:
                current = user.dialog or {}
                # A language picker is one screen, even while its checkmarks change.
                # Keep its callbacks valid when the user taps faster than Telegram redraws.
                same_picker = dialog.get("stage") in ("audio", "subtitles") and dialog.get(
                    "stage"
                ) == current.get("stage")
                dialog["revision"] = current.get("revision", 0) + (0 if same_picker else 1)
                dialog["view"] = {"buttons": buttons or [], "posters": posters or []}
                user.reply = text
                user.reply_version += 1
                user.retry_at = 0
                user.delivery_error = ""
            user.dialog = deepcopy(dialog)

    async def delete(self, token, chat_id, message_id):
        if not message_id:
            return True
        try:
            await self.bot.call(token, "deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except TelegramError as exc:
            # Telegram cannot delete messages older than 48h; don't prevent navigation.
            if exc.code in (400, 403):
                return True
            return False

    async def render(self, token, user):
        if self.user(user.id).reply_version != user.reply_version:
            return
        dialog = deepcopy(user.dialog or {})
        revision = dialog.get("revision", 0)
        view = dialog.get("view", {})
        keyboard = {
            "inline_keyboard": [
                [{"text": label[:64], "callback_data": f"{revision}:{action}"} for label, action in row]
                for row in view.get("buttons", [])
            ]
        }
        posters = view.get("posters", [])
        old_message = dialog.get("message_id")
        if old_message and dialog.get("sent_kind") == "text" and not posters:
            try:
                result = await self.bot.call(
                    token,
                    "editMessageText",
                    chat_id=user.chat_id,
                    message_id=old_message,
                    text=user.reply,
                    reply_markup=keyboard,
                )
            except TelegramError as exc:
                if exc.code != 400:
                    raise
                result = await self.bot.call(
                    token, "sendMessage", chat_id=user.chat_id, text=user.reply, reply_markup=keyboard
                )
        elif posters:

            async def poster(item):
                url = item.get("poster") or ""
                if not url:
                    return None
                try:
                    return await fetch_poster(self.ctx, url.rsplit("/", 1)[-1])
                except (ProviderError, ValueError):
                    return None

            paths = await asyncio.gather(*(poster(item) for item in posters))
            photo = await asyncio.to_thread(collage, paths, dialog.get("page", 0) * PAGE)
            if self.user(user.id).reply_version != user.reply_version:
                return
            result = await self.bot.call(
                token,
                "sendPhoto",
                chat_id=user.chat_id,
                photo_bytes=photo,
                caption=user.reply[:1024],
                reply_markup=keyboard,
            )
        else:
            result = await self.bot.call(
                token, "sendMessage", chat_id=user.chat_id, text=user.reply, reply_markup=keyboard
            )
        message_id = result.get("message_id", old_message) if isinstance(result, dict) else old_message
        garbage = list(dialog.get("garbage", []))
        if old_message and old_message != message_id:
            garbage.append(old_message)
        # Commit the new ID before deleting anything, including across restarts.
        with self.ctx.db.session() as db:
            current = db.get(TelegramUser, user.id)
            current.dialog = {
                **current.dialog,
                "message_id": message_id,
                "sent_kind": "photo" if posters else "text",
                "garbage": list(dict.fromkeys(garbage))[-100:],
            }
        remaining = []
        for identity in dict.fromkeys(garbage):
            if identity != message_id and not await self.delete(token, user.chat_id, identity):
                remaining.append(identity)
        with self.ctx.db.session() as db:
            current = db.get(TelegramUser, user.id)
            current.dialog = {**current.dialog, "garbage": remaining}

    def show_results(self, identity, dialog):
        results = dialog["results"]
        start = dialog.get("page", 0) * PAGE
        items = results[start : start + PAGE]
        buttons = [
            [
                (
                    f"{start + i + 1}. {item['title']} ({item.get('year') or '—'}) · {'Кино' if item['kind'] == 'movie' else 'Сериал'}",
                    f"media:{start + i}",
                )
            ]
            for i, item in enumerate(items)
        ]
        navigation = []
        if start:
            navigation.append(("←", f"page:{dialog['page'] - 1}"))
        if start + PAGE < len(results):
            navigation.append(("→", f"page:{dialog['page'] + 1}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([("Новый поиск", "search")])
        dialog["stage"] = "results"
        self.save(identity, dialog, "Выберите кино или сериал", buttons, items)

    def languages(self, identity, dialog, stage):
        dialog["stage"] = stage
        selected = dialog.get(stage, [])
        selection = ", ".join(LABELS.get(code, code) for code in selected) or "не выбраны"
        buttons = [
            (f"{'✓ ' if code in selected else ''}{name}", f"lang:{code}") for code, name in LABELS.items()
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.extend(
            [
                [("Без ограничений" if stage == "audio" else "Без субтитров", "clear")],
                [("Далее" if stage == "audio" else "Создать задачу", "next")],
                [("Отмена", "search")],
            ]
        )
        self.save(
            identity,
            dialog,
            (
                f"Выберите языки озвучки.\nВыбрано: {selection}.\nМожно выбрать несколько. Все выбранные языки обязательны.\nЗатем нажмите «Далее»."
                if stage == "audio"
                else f"Выберите языки субтитров.\nВыбрано: {selection}.\nИх отсутствие не блокирует загрузку.\nЗатем нажмите «Создать задачу»."
            ),
            rows,
        )

    async def handle(self, token, identity, update):
        self.actor(identity)
        user = self.user(identity)
        dialog = deepcopy(user.dialog or {})
        callback = update.get("callback_query")
        if callback:
            try:
                await self.bot.call(token, "answerCallbackQuery", callback_query_id=callback["id"])
            except TelegramError:
                pass
            if callback.get("data", "").startswith("notice:"):
                from lazarr.telegram_selection import NotificationSelection

                await NotificationSelection(self).open(identity, callback)
                return
            raw = callback.get("data", "").split(":", 1)
            if (
                len(raw) != 2
                or raw[0] != str(dialog.get("revision", 0))
                or callback.get("message", {}).get("message_id") != dialog.get("message_id")
            ):
                return
            action = raw[1]
            allowed = {a for row in dialog.get("view", {}).get("buttons", []) for _, a in row}
            if action not in allowed:
                return
        else:
            message = update.get("message", {})
            text = message.get("text", "").strip()
            message_id = message.get("message_id")
            if message_id and not await self.delete(token, user.chat_id, message_id):
                with self.ctx.db.session() as db:
                    current = db.get(TelegramUser, identity)
                    current.dialog = {
                        **current.dialog,
                        "garbage": [*current.dialog.get("garbage", []), message_id][-100:],
                    }
            if text.split("@")[0] in ("/search", "/cancel", "Поиск"):
                action = "search"
            elif dialog.get("stage") == "query":
                if not 2 <= len(text) <= 200:
                    self.save(
                        identity, dialog, "Введите название от 2 до 200 символов", [[("Отмена", "search")]]
                    )
                    return
                async with self.ctx.plugins.open("tmdb") as provider:
                    results = await provider.search(text)
                self.actor(identity)
                dialog.update(results=[item.model_dump(mode="json") for item in results[:60]], page=0)
                if not results:
                    self.save(
                        identity,
                        dialog,
                        "Ничего не найдено. Введите другое название",
                        [[("Новый поиск", "search")]],
                    )
                else:
                    self.show_results(identity, dialog)
                return
            else:
                action = "search"
        stage = dialog.get("stage")
        if action.startswith("notice-"):
            from lazarr.telegram_selection import NotificationSelection

            await NotificationSelection(self).handle(identity, dialog, action)
            return
        if action == "search":
            self.save(identity, {"stage": "query"}, "Введите наименование кино или сериала")
        elif action.startswith("page:") and stage == "results":
            page = int(action.split(":")[1])
            if 0 <= page < math.ceil(len(dialog["results"]) / PAGE):
                dialog["page"] = page
                self.show_results(identity, dialog)
        elif action.startswith("media:") and stage == "results":
            item = dialog["results"][int(action.split(":")[1])]
            async with self.ctx.plugins.open("tmdb") as provider:
                media = await provider.get_media(item["kind"], item["id"])
            self.actor(identity)
            defaults = self.ctx.service.settings().defaults
            dialog.update(
                media=media.model_dump(mode="json"),
                audio=defaults.audio_languages,
                subtitles=defaults.subtitle_languages,
            )
            if media.kind == "tv":
                seasons = [s for s in media.seasons if s.get("number") is not None]
                dialog.update(stage="season", seasons=seasons, season_page=0)
                self.seasons(identity, dialog)
            else:
                dialog.pop("season", None)
                self.languages(identity, dialog, "audio")
        elif action.startswith("seasonpage:") and stage == "season":
            dialog["season_page"] = int(action.split(":")[1])
            self.seasons(identity, dialog)
        elif action.startswith("season:") and stage == "season":
            dialog["season"] = int(action.split(":")[1])
            self.languages(identity, dialog, "audio")
        elif stage in ("audio", "subtitles"):
            if action.startswith("lang:"):
                code = action.split(":")[1]
                selected = list(dialog.get(stage, []))
                if code in selected:
                    selected.remove(code)
                else:
                    selected.append(code)
                dialog[stage] = selected
                self.languages(identity, dialog, stage)
            elif action == "clear":
                dialog[stage] = []
                self.languages(identity, dialog, stage)
            elif action == "next":
                if stage == "audio":
                    self.languages(identity, dialog, "subtitles")
                else:
                    await self.create(identity, dialog)
        elif action.startswith("choose:") and stage == "candidates":
            choice = int(action.split(":")[1])
            if choice not in dialog.get("choices", []):
                return
            actor = self.actor(identity)
            if self.ctx.engine is None:
                raise ValueError("Движок загрузок недоступен")
            result = await self.ctx.worker.choose_all(choice, actor, dialog.get("season"), dialog["task"])
            dialog["stage"] = "done"
            self.save(
                identity,
                dialog,
                f"Раздача выбрана. Загрузка запущена для {result['selected']} из {result['total']} эпизодов."
                if result["skipped"]
                else "Раздача выбрана. Загрузка запущена.",
                [[("Поиск", "search")]],
            )
        elif action == "refresh" and stage in ("waiting", "candidates", "done"):
            dialog["stage"] = "waiting"
            dialog.pop("last_status", None)
            self.save(identity, dialog)
            await self.monitor(identity)

    def seasons(self, identity, dialog):
        page = dialog.get("season_page", 0)
        seasons = dialog["seasons"]
        rows = [
            [(s.get("title") or f"Сезон {s['number']}", f"season:{s['number']}")]
            for s in seasons[page * 12 : page * 12 + 12]
        ]
        nav = []
        if page:
            nav.append(("←", f"seasonpage:{page - 1}"))
        if (page + 1) * 12 < len(seasons):
            nav.append(("→", f"seasonpage:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Новый поиск", "search")])
        self.save(
            identity, dialog, "Выберите сезон" if seasons else "У этого сериала пока нет сезонов в TMDB", rows
        )

    async def create(self, identity, dialog):
        media = dialog["media"]
        requirements = self.ctx.service.settings().defaults.model_copy(
            update={
                "audio_languages": dialog.get("audio", []),
                "subtitle_languages": dialog.get("subtitles", []),
            }
        )
        payload = CreateTask(
            media_id=media["id"],
            kind=media["kind"],
            season=dialog.get("season"),
            requirements=Requirements.model_validate(requirements.model_dump()),
        )
        async with self.ctx.plugins.open("tmdb") as provider:
            item = await provider.get_media(payload.kind, payload.media_id)
            season = (
                await provider.get_season(payload.media_id, payload.season) if payload.kind == "tv" else None
            )
        # Recheck approval after network I/O, immediately before mutation.
        actor = self.actor(identity)
        task_id = self.ctx.service.create_from_metadata(payload, item, season, actor)
        with self.ctx.db.session() as db:
            audit(
                db,
                actor,
                "telegram.task.create",
                str(task_id),
                {"telegram_user_id": self.user(identity).user_id},
            )
        dialog.update(stage="waiting", task=task_id, created_at=time.time())
        self.save(
            identity,
            dialog,
            "Задача создана. Выполняется поиск...",
            [[("Обновить", "refresh"), ("Новый поиск", "search")]],
        )
        self.ctx.scheduler.wake.set()

    async def monitor(self, identity):
        user = self.user(identity)
        dialog = deepcopy(user.dialog or {})
        if dialog.get("stage") != "waiting":
            return
        self.actor(identity)
        task_id = dialog["task"]
        with self.ctx.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                dialog["stage"] = "done"
                self.save(identity, dialog, "Задача удалена", [[("Поиск", "search")]])
                return
            query = select(Subtask).where(Subtask.task_id == task_id)
            if dialog.get("season") is not None:
                query = query.join(Episode).join(Season).where(Season.number == dialog["season"])
            subtasks = list(db.scalars(query))
            statuses = [s.status for s in subtasks]
            selected = sum(s in {"starting", "downloading", "ready", "done", "seeding"} for s in statuses)
            paused = task.paused
        snapshot = self.ctx.scheduler.snapshot(task_id)
        if statuses and selected == len(statuses):
            dialog["stage"] = "done"
            self.save(
                identity,
                dialog,
                "Раздача определена автоматически. Загрузка запущена."
                if any(s != "done" for s in statuses)
                else "Все выбранные эпизоды уже загружены.",
                [[("Поиск", "search")]],
            )
            return
        if snapshot.get("running"):
            return
        pending = snapshot.get("pending_requests")
        deferred = snapshot.get("next_attempt_at", 0) > time.time()
        if pending and not deferred and snapshot.get("state") not in {"blocked", "error"} and not paused:
            return
        choices = [
            c
            for c in self.ctx.service.task_candidates(task_id, dialog.get("season"))
            if c["matched"] > 0 and any(e["action"] != "rejected" for e in c["episodes"])
        ]
        if choices:
            choices = choices[:6]
            dialog.update(stage="candidates", choices=[c["id"] for c in choices])
            lines = [
                f"Автоматически выбраны раздачи для {selected} из {len(statuses)} эпизодов."
                if selected
                else "Автоматически выбрать раздачу не удалось.",
                "Наиболее подходящие варианты:",
            ]
            rows = []
            for i, c in enumerate(choices, 1):
                candidate = c["candidate"]
                lines.append(
                    f"{i}. {candidate.get('title', 'Раздача')[:260]}\nПокрытие: {c['matched']}/{c['total']} · Сиды: {candidate.get('seeds') or 0} · {candidate.get('provider', '')}"
                )
                rows.append([(f"{i}. {candidate.get('title', 'Раздача')[:54]}", f"choose:{c['id']}")])
            rows.extend([[("Обновить", "refresh"), ("Новый поиск", "search")]])
            self.save(identity, dialog, "\n\n".join(lines), rows)
        else:
            message = (
                "Задача на паузе. Возобновите её в Lazarr."
                if paused
                else "Раздачи пока не найдены. Lazarr продолжит поиск по расписанию."
            )
            if snapshot.get("state") in {"blocked", "error"} or deferred:
                message = (
                    snapshot.get("message") or "Поиск временно недоступен. Проверьте провайдеры в Lazarr."
                )
            if selected:
                message = (
                    f"Автоматически выбраны раздачи для {selected} из {len(statuses)} эпизодов.\n" + message
                )
            if dialog.get("last_status") != message:
                dialog["last_status"] = message
                self.save(identity, dialog, message, [[("Обновить", "refresh"), ("Новый поиск", "search")]])

    async def tick(self, token, bot_id):
        if self.commands_set != bot_id:
            try:
                await self.bot.call(
                    token, "setMyCommands", commands=[{"command": "search", "description": "Поиск"}]
                )
                self.commands_set = bot_id
            except TelegramError:
                pass
        with self.ctx.db.session() as db:
            users = list(
                db.scalars(
                    select(TelegramUser).where(
                        TelegramUser.bot_id == bot_id, TelegramUser.status == "approved"
                    )
                )
            )
        for user in users:
            if user.inbox:
                update = user.inbox[0]
                try:
                    await self.handle(token, user.id, update)
                except TelegramError:
                    # Leave the durable event for retry without exposing credentials.
                    continue
                except (ValueError, ProviderError) as exc:
                    dialog = deepcopy(self.user(user.id).dialog)
                    self.save(user.id, dialog, str(exc)[:1000], [[("Повторить поиск", "search")]])
                except Exception:
                    dialog = deepcopy(self.user(user.id).dialog)
                    self.save(
                        user.id,
                        dialog,
                        "Не удалось выполнить действие. Повторите поиск.",
                        [[("Поиск", "search")]],
                    )
                with self.ctx.db.session() as db:
                    current = db.get(TelegramUser, user.id)
                    current.inbox = [u for u in current.inbox if u["update_id"] != update["update_id"]]
            else:
                try:
                    await self.monitor(user.id)
                except (ValueError, ProviderError):
                    pass
