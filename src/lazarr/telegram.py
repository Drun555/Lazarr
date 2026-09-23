"""Private-chat authorization via outgoing Telegram long polling."""

import asyncio
import json
import re
import time

import httpx
from sqlalchemy import select

from lazarr.models import ConfigEntry, TelegramUser
from lazarr.security import audit

KEY = "telegram"
REPLIES = {
    "pending": "Запрос на авторизацию отправлен",
    "approved": "Заявка одобрена",
    "blocked": "Запрос на авторизацию отклонён. Обратитесь к администратору Lazarr.",
}


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=30, message=None, *, description=""):
        self.not_modified = "message is not modified" in description.lower()
        self.message_missing = any(
            part in description.lower()
            for part in ("message to edit not found", "message can't be edited", "message can not be edited")
        )
        self.retry_after = max(1, retry_after)
        self.code = code
        self.message = message or {
            400: "Telegram отклонил запрос: неверные параметры.",
            401: "Недействительный токен бота.",
            404: "Telegram не нашёл бота. Проверьте токен: нужен полный токен от @BotFather, а не имя бота.",
            403: "Пользователь заблокировал бота или доставка запрещена.",
            409: "Бот уже используется другим процессом или настроен webhook.",
            429: "Telegram ограничил частоту запросов. Повторим позже.",
        }.get(code, "Telegram недоступен. Повторим позже.")
        super().__init__(self.message)


class TelegramService:
    def __init__(self, db, secrets, background=True):
        self.db, self.secrets, self.background = db, secrets, background
        self.tasks = []
        self.lock = asyncio.Lock()
        self.error = ""
        self.menu = None

    def config(self):
        with self.db.session() as db:
            row = db.get(ConfigEntry, KEY)
            return dict(row.value) if row else {}

    def describe(self):
        cfg = self.config()
        return {
            "enabled": cfg.get("enabled", False),
            "token_configured": bool(cfg.get("secret")),
            "bot_username": cfg.get("username", ""),
            "error": self.error,
        }

    async def call(self, token, method, **payload):
        # Never expose HTTP exceptions: Telegram URLs contain the bot token.
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                photo = payload.pop("photo_bytes", None)
                if photo is not None:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/{method}",
                        data={
                            k: json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                            for k, v in payload.items()
                        },
                        files={"photo": ("results.jpg", photo, "image/jpeg")},
                    )
                else:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/{method}", json=payload
                    )
            try:
                data = response.json()
            except ValueError:
                raise TelegramError(
                    message=f"Telegram вернул неожиданный ответ (HTTP {response.status_code})."
                ) from None
            if not data.get("ok"):
                raise TelegramError(
                    data.get("error_code"),
                    data.get("parameters", {}).get("retry_after", 30),
                    description=data.get("description", ""),
                )
            return data["result"]
        except httpx.TimeoutException:
            raise TelegramError(
                message="Истекло время ожидания ответа Telegram. Проверьте соединение сервера."
            ) from None
        except httpx.ConnectError:
            raise TelegramError(
                message="Не удалось установить защищённое соединение с Telegram. Проверьте сеть, DNS и сертификаты сервера."
            ) from None
        except httpx.ProxyError:
            raise TelegramError(
                message="Ошибка подключения к Telegram через прокси. Проверьте настройки прокси сервера."
            ) from None
        except (httpx.HTTPError, ValueError, KeyError):
            raise TelegramError() from None

    async def configure(self, enabled, token, actor):
        async with self.lock:
            cfg = self.config()
            token = token.strip() if token else self.secrets.decrypt(cfg.get("secret", "")).get("token", "")
            if token and not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                raise ValueError(
                    "Неверный формат токена. Скопируйте полный токен от @BotFather в формате 123456789:ABC… без кавычек, ссылки и префикса bot."
                )
            if enabled and not token:
                raise ValueError("Укажите токен Telegram-бота")
            if token and (
                enabled
                or not cfg.get("secret")
                or token != self.secrets.decrypt(cfg.get("secret", "")).get("token")
            ):
                bot = await self.call(token, "getMe")
                if cfg.get("bot_id") != bot["id"]:
                    cfg["offset"] = 0
                cfg.update(
                    bot_id=bot["id"],
                    username=bot.get("username", ""),
                    secret=self.secrets.encrypt({"token": token}),
                )
            await self.stop()
            latest = self.config()
            if latest.get("bot_id") == cfg.get("bot_id"):
                cfg["offset"] = latest.get("offset", 0)
            cfg["enabled"] = enabled
            with self.db.session() as db:
                row = db.get(ConfigEntry, KEY)
                if row:
                    row.value = cfg
                else:
                    db.add(ConfigEntry(key=KEY, value=cfg))
                audit(db, actor, "telegram.configure", "bot")
            self.error = ""
            await self.start()

    async def start(self):
        cfg = self.config()
        if self.background and cfg.get("enabled") and not self.tasks:
            token = self.secrets.decrypt(cfg["secret"])["token"]
            self.tasks = [
                asyncio.create_task(self.poll(token, cfg["bot_id"])),
                asyncio.create_task(self.deliver(token, cfg["bot_id"])),
            ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []

    @staticmethod
    def queue_reply(user):
        user.reply = REPLIES[user.status]
        user.reply_version += 1
        user.retry_at = 0
        user.delivery_error = ""
        dialog = dict(user.dialog or {})
        dialog.update(
            stage="menu",
            revision=dialog.get("revision", 0) + 1,
            view={"buttons": [[("Поиск", "search")]] if user.status == "approved" else []},
        )
        user.dialog = dialog

    def receive(self, bot_id, update):
        with self.db.session() as db:
            cfg = db.get(ConfigEntry, KEY)
            if (
                not cfg
                or cfg.value.get("bot_id") != bot_id
                or update["update_id"] < cfg.value.get("offset", 0)
            ):
                return
            callback = update.get("callback_query", {})
            msg = update.get("message") or callback.get("message", {})
            sender, chat = callback.get("from") or msg.get("from", {}), msg.get("chat", {})
            private = (
                chat.get("type") == "private"
                and sender.get("id") == chat.get("id")
                and not sender.get("is_bot")
            )
            user = (
                db.scalar(
                    select(TelegramUser).where(
                        TelegramUser.bot_id == bot_id, TelegramUser.user_id == sender.get("id", 0)
                    )
                )
                if private
                else None
            )
            command = msg.get("text", "").split(maxsplit=1)[0:1]
            if command and command[0].split("@")[0] == "/start" and private and not callback:
                if not user:
                    user = TelegramUser(
                        bot_id=bot_id,
                        user_id=sender["id"],
                        chat_id=chat["id"],
                        status="pending",
                        reply_version=0,
                    )
                    db.add(user)
                user.name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))[:256]
                user.username = sender.get("username", "")[:80]
                self.queue_reply(user)
                if msg.get("message_id"):
                    user.dialog = {
                        **user.dialog,
                        "garbage": [*user.dialog.get("garbage", []), msg["message_id"]][-100:],
                    }
            elif user and user.status == "approved":
                # Durable bounded inbox: command processing is independent of polling.
                user.inbox = [*(user.inbox or [])[-49:], update]
            cfg.value = {**cfg.value, "offset": update["update_id"] + 1}

    def users(self):
        bot_id = self.config().get("bot_id", 0)
        with self.db.session() as db:
            return [
                {
                    key: getattr(u, key)
                    for key in (
                        "id",
                        "user_id",
                        "name",
                        "username",
                        "status",
                        "requested_at",
                        "delivery_error",
                    )
                }
                | {"reply_pending": bool(u.reply)}
                for u in db.scalars(
                    select(TelegramUser)
                    .where(TelegramUser.bot_id == bot_id)
                    .order_by(TelegramUser.requested_at.desc())
                )
            ]

    def decide(self, identity, status, actor):
        with self.db.session() as db:
            user = db.get(TelegramUser, identity)
            if not user or user.bot_id != self.config().get("bot_id"):
                raise ValueError("Пользователь Telegram не найден")
            if user.status != status:
                user.status = status
                if status == "approved":
                    user.approved_by = actor
                user.inbox = []
                self.queue_reply(user)
                audit(db, actor, "telegram." + status, str(user.user_id))

    async def poll(self, token, bot_id):
        while True:
            try:
                updates = await self.call(
                    token,
                    "getUpdates",
                    offset=self.config().get("offset", 0),
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    self.receive(bot_id, update)
                self.error = ""
            except TelegramError as exc:
                self.error = exc.message
                await asyncio.sleep(exc.retry_after)
            except Exception:
                self.error = "Ошибка обработки Telegram. Повторим позже."
                await asyncio.sleep(30)

    async def deliver_once(self, token, bot_id):
        with self.db.session() as db:
            users = list(
                db.scalars(
                    select(TelegramUser)
                    .where(
                        TelegramUser.bot_id == bot_id,
                        TelegramUser.reply != "",
                        TelegramUser.retry_at <= time.time(),
                    )
                    .limit(20)
                )
            )
        for user in users:
            error = None
            try:
                if self.menu:
                    await self.menu.render(token, user)
                else:
                    await self.call(token, "sendMessage", chat_id=user.chat_id, text=user.reply)
            except TelegramError as exc:
                error = exc
            with self.db.session() as db:
                current = db.get(TelegramUser, user.id)
                if current and current.reply_version == user.reply_version:
                    if error:
                        current.delivery_error = error.message
                        current.retry_at = time.time() + max(30, error.retry_after)
                    else:
                        current.reply = ""
                        current.delivery_error = ""
            if error:
                break

    async def deliver_notifications_once(self, token, bot_id):
        from lazarr.notifications import PREFIX, DIGEST_PREFIX, COALESCE_SECONDS, digest_id, digest_text
        from lazarr.telegram_format import escape

        with self.db.session() as db:
            entries = list(
                db.scalars(
                    select(ConfigEntry).where(
                        ConfigEntry.key.startswith(PREFIX),
                        ConfigEntry.value["bot_id"].as_integer() == bot_id,
                        ConfigEntry.value["pending"].as_boolean().is_(True),
                    )
                )
            )
        groups = {}
        for entry in entries:
            for identity in entry.value["recipients"]:
                groups.setdefault((digest_id(entry), identity), []).append(entry)
        for (digest, identity), batch in groups.items():
            now = time.time()
            if any(e.value["recipients"][identity] > now for e in batch):
                continue
            if min(e.value.get("queued_at", 0) for e in batch) + COALESCE_SECONDS > now:
                continue
            key = DIGEST_PREFIX + digest
            with self.db.session() as db:
                user = db.get(TelegramUser, int(identity))
                allowed = user and user.bot_id == bot_id and user.status == "approved"
                saved = db.get(ConfigEntry, key)
                state = dict(saved.value) if saved else {"bot_id": bot_id, "users": {}}
                delivery = dict(state["users"].get(identity, {}))
            items = dict(delivery.get("items", {}))
            for entry in sorted(batch, key=lambda e: e.value.get("queued_at", 0)):
                items[str(entry.value.get("subtask_id", entry.key))] = {
                    k: v for k, v in entry.value.items() if k not in {"recipients", "pending"}
                }
            latest = batch[-1].value
            text = digest_text(items)
            if not any(row.get("event") for row in items.values()):
                text = escape("\n\n".join(row["text"] for row in items.values()))[:4000]
            buttons = []
            if latest.get("task_id") and any(row.get("event") == "selection" for row in items.values()):
                buttons = [[{"text": "Выбрать раздачу", "callback_data": f"notice:{digest}"}]]
            error = None
            message_id = delivery.get("message_id")
            if allowed:
                try:
                    payload = dict(
                        chat_id=user.chat_id,
                        text=text,
                        parse_mode="MarkdownV2",
                        reply_markup={"inline_keyboard": buttons},
                    )
                    if message_id and delivery.get("text") == text:
                        result = None
                    elif message_id:
                        try:
                            result = await self.call(
                                token, "editMessageText", message_id=message_id, **payload
                            )
                        except TelegramError as exc:
                            if exc.code == 400 and exc.not_modified:
                                result = None
                            elif exc.code == 400 and exc.message_missing:
                                result = await self.call(token, "sendMessage", **payload)
                            else:
                                raise
                    else:
                        result = await self.call(token, "sendMessage", **payload)
                    if isinstance(result, dict):
                        message_id = result.get("message_id", message_id)
                except TelegramError as exc:
                    error = exc
            with self.db.session() as db:
                if allowed and not error:
                    saved = db.get(ConfigEntry, key)
                    state = dict(saved.value) if saved else {"bot_id": bot_id, "users": {}}
                    state["users"] = {
                        **state["users"],
                        identity: {"items": items, "text": text, "message_id": message_id},
                    }
                    state.update(task_id=latest.get("task_id"), task_created_at=latest.get("task_created_at"))
                    if saved:
                        saved.value = state
                    else:
                        db.add(ConfigEntry(key=key, value=state))
                for entry in batch:
                    current = db.get(ConfigEntry, entry.key)
                    if not current:
                        continue
                    recipients = dict(current.value["recipients"])
                    if error:
                        recipients[identity] = time.time() + max(30, error.retry_after)
                    else:
                        recipients.pop(identity, None)
                    current.value = {**current.value, "recipients": recipients, "pending": bool(recipients)}
            if error and error.code == 429:
                return

    async def deliver(self, token, bot_id):
        while True:
            try:
                if self.menu:
                    await self.menu.tick(token, bot_id)
                await self.deliver_once(token, bot_id)
                await self.deliver_notifications_once(token, bot_id)
            except Exception:
                self.error = "Ошибка доставки Telegram. Повторим позже."
            await asyncio.sleep(2)
