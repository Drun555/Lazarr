"""Notification entry point and paginated, episode-scoped release selection."""

import asyncio
from sqlalchemy import select

from lazarr.models import ConfigEntry, Task, Subtask, Episode, Season, Media
from lazarr.notifications import DIGEST_PREFIX
from lazarr.telegram_format import bold, escape, quote

PAGE = 6


class NotificationSelection:
    def __init__(self, menu):
        self.menu = menu
        self.ctx = menu.ctx

    async def open(self, identity, callback):
        user = self.menu.user(identity)
        digest = callback.get("data", "").removeprefix("notice:")
        with self.ctx.db.session() as db:
            row = db.get(ConfigEntry, DIGEST_PREFIX + digest)
            if not row or row.value.get("bot_id") != user.bot_id:
                return
            sent = row.value.get("users", {}).get(str(identity), {})
            message = callback.get("message", {})
            if (
                not sent.get("message_id")
                or message.get("message_id") != sent["message_id"]
                or message.get("chat", {}).get("id") != user.chat_id
            ):
                return
            dialog = {
                "stage": "notice_episodes",
                "task": row.value.get("task_id"),
                "task_created_at": row.value.get("task_created_at"),
                "episode_page": 0,
            }
        # One dedicated selection screen near the notification, not an edit of a
        # potentially distant search wizard. Subsequent navigation edits this screen.
        with self.ctx.db.session() as db:
            from lazarr.models import TelegramUser

            current = db.get(TelegramUser, identity)
            old = current.dialog or {}
            current.dialog = {
                **old,
                "message_id": None,
                "sent_kind": "text",
                "garbage": [*old.get("garbage", []), *([old["message_id"]] if old.get("message_id") else [])][
                    -100:
                ],
            }
        self.episodes(identity, dialog)

    def scope(self, identity, dialog):
        self.menu.actor(identity)
        with self.ctx.db.session() as db:
            task = db.get(Task, dialog["task"])
            if not task or task.created_at != dialog.get("task_created_at"):
                raise ValueError("Эта задача уже удалена. Откройте актуальное уведомление.")
            if task.paused:
                raise ValueError("Задача на паузе. Возобновите её в Lazarr.")
            title = db.get(Media, task.media_id).title
            rows = list(
                db.execute(
                    select(Subtask, Episode, Season)
                    .join(Episode, Episode.id == Subtask.episode_id)
                    .join(Season, Season.id == Episode.season_id)
                    .where(Subtask.task_id == task.id, Subtask.status == "needs_selection")
                    .order_by(Season.number, Episode.number, Subtask.id)
                )
            )
        return title, rows

    def episodes(self, identity, dialog):
        title, episodes = self.scope(identity, dialog)
        page = max(0, min(dialog.get("episode_page", 0), (len(episodes) - 1) // PAGE))
        dialog.update(stage="notice_episodes", episode_page=page)
        rows = [
            [
                (
                    f"S{s.number:02d}E{e.number:02d} · {e.title or 'Выбрать раздачу'}"[:64],
                    f"notice-episode:{sub.id}",
                )
            ]
            for sub, e, s in episodes[page * PAGE : (page + 1) * PAGE]
        ]
        nav = []
        if page:
            nav.append(("←", f"notice-page:{page - 1}"))
        if (page + 1) * PAGE < len(episodes):
            nav.append(("→", f"notice-page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Обновить", "notice-back"), ("Поиск", "search")])
        text = (
            f"{bold(title[:200])}\n\n{escape(f'Выберите серию · страница {page + 1}/{max(1, (len(episodes) + PAGE - 1) // PAGE)}')}"
            if episodes
            else f"{bold(title[:200])}\n\n{escape('Нет серий, требующих выбора. Возможно, раздача уже выбрана.')}"
        )
        self.menu.save(identity, dialog, text, rows, markdown=True)

    async def choices(self, identity, dialog, confirm=None):
        title, episodes = self.scope(identity, dialog)
        current = next((row for row in episodes if row[0].id == dialog.get("subtask")), None)
        if not current:
            self.episodes(identity, dialog)
            return None
        _, episode, season = current
        choices = await asyncio.to_thread(self.ctx.service.candidates, dialog["subtask"])
        choices = [
            c
            for c in choices
            if c["action"] not in {"rejected", "selected"}
            and not c.get("episode_missing")
            and c.get("report", {}).get("binding")
        ]
        heading = f"{title[:160]} · S{season.number:02d}E{episode.number:02d}"
        if confirm is not None:
            choice = next((c for c in choices if c["id"] == confirm), None)
            if not choice:
                await self.choices(identity, dialog)
                return None
            candidate = choice["candidate"]
            warnings = [
                c.get("reason") or c.get("field", "")
                for c in choice["report"].get("criteria", [])
                if c.get("result") in {"MISMATCH", "UNKNOWN"}
            ]
            if self.ctx.engine is None:
                raise ValueError("Движок загрузок недоступен")
            coverage = await self.ctx.worker.choose_all(
                confirm,
                self.menu.actor(identity),
                task_id=dialog["task"],
                pending_only=True,
                expected_subtask=dialog["subtask"],
                preview=True,
            )
            self.menu.actor(identity)
            dialog.update(stage="notice_confirm", decision=confirm, selection_ids=coverage["subtask_ids"])
            labels = ", ".join(coverage["episodes"][:30])
            if len(coverage["episodes"]) > 30:
                labels += f" и ещё {len(coverage['episodes']) - 30}"
            coverage_text = (
                "Раздача будет подключена к подходящим сериям этой задачи: "
                f"{coverage['selected']} из {coverage['total']}."
            )
            text = (
                f"{bold(heading)}\n\n{quote(candidate.get('title', 'Раздача')[:800])}\n\n"
                f"{escape(coverage_text)}\n"
                f"{quote(labels)}\n{escape('Уже выбранные и загруженные серии не изменятся.')}"
            )
            if warnings:
                text += f"\n{bold('Внимание:')} {escape('; '.join(str(w)[:150] for w in warnings[:6]))}"
            text += f"\n{bold('Подтвердить выбор?')}"
            self.menu.save(
                identity,
                dialog,
                text,
                [
                    [("Подтвердить загрузку", f"notice-confirm:{confirm}")],
                    [("← К раздачам", "notice-choices")],
                ],
                markdown=True,
            )
            return choice
        page = max(0, min(dialog.get("choice_page", 0), (len(choices) - 1) // PAGE))
        dialog.update(stage="notice_candidates", choice_page=page)
        lines = [bold(heading), escape("Выберите раздачу:")]
        rows = []
        for i, choice in enumerate(choices[page * PAGE : (page + 1) * PAGE], page * PAGE + 1):
            c = choice["candidate"]
            size = f"{c['size'] / 1024**3:.1f} ГиБ" if c.get("size") else "Размер неизвестен"
            lines.append(
                quote(
                    f"{i}. {c.get('title', 'Раздача')[:260]}\n"
                    f"{c.get('provider', '')[:40]} · Сиды: {c.get('seeds') or 0} · {size}"
                )
            )
            rows.append([(f"{i}. {c.get('title', 'Раздача')[:54]}", f"notice-choice:{choice['id']}")])
        if not choices:
            lines.append(
                escape("Нет вариантов с определённым видеофайлом. Для ручного сопоставления откройте Lazarr.")
            )
        nav = []
        if page:
            nav.append(("←", f"notice-cpage:{page - 1}"))
        if (page + 1) * PAGE < len(choices):
            nav.append(("→", f"notice-cpage:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("Обновить", "notice-choices"), ("← К сериям", "notice-back")])
        self.menu.save(identity, dialog, "\n\n".join(lines), rows, markdown=True)

    async def handle(self, identity, dialog, action):
        if action == "notice-back":
            self.episodes(identity, dialog)
        elif action.startswith("notice-page:"):
            dialog["episode_page"] = int(action.split(":")[1])
            self.episodes(identity, dialog)
        elif action.startswith("notice-episode:"):
            dialog.update(subtask=int(action.split(":")[1]), choice_page=0)
            await self.choices(identity, dialog)
        elif action == "notice-choices" or action.startswith("notice-cpage:"):
            if ":" in action:
                dialog["choice_page"] = int(action.split(":")[1])
            await self.choices(identity, dialog)
        elif action.startswith("notice-choice:"):
            await self.choices(identity, dialog, int(action.split(":")[1]))
        elif action.startswith("notice-confirm:") and dialog.get("stage") == "notice_confirm":
            decision = int(action.split(":")[1])
            if decision != dialog.get("decision"):
                return
            self.scope(identity, dialog)
            actor = self.menu.actor(identity)
            if self.ctx.engine is None:
                raise ValueError("Движок загрузок недоступен")
            result = await self.ctx.worker.choose_all(
                decision,
                actor,
                task_id=dialog["task"],
                pending_only=True,
                expected_subtask=dialog["subtask"],
                allowed_subtasks=dialog.get("selection_ids", []),
            )
            dialog["stage"] = "notice_done"
            self.menu.save(
                identity,
                dialog,
                f"Раздача выбрана. Подключено серий: {result['selected']}. Остальные серии продолжат ждать выбора.",
                [[("Выбрать для других серий", "notice-back"), ("Поиск", "search")]],
            )
