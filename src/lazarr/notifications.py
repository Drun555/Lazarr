"""Durable episode notifications, queued in the worker's transaction."""

import hashlib
import time
from sqlalchemy import select

from lazarr.models import ConfigEntry, Episode, Media, Season, Task, TelegramUser

PREFIX = "telegram.notification."
DIGEST_PREFIX = "telegram.digest."
COALESCE_SECONDS = 8


def digest_id(entry):
    return hashlib.sha256(entry.key.rsplit(".", 2)[0].encode()).hexdigest()[:24]


def digest_text(items):
    """Bounded summary; each episode appears once, with its latest event."""
    rows = list(items.values())
    title = rows[0].get("media_title", "Обновления Lazarr")[:200]
    lines = [title]
    for event, label in [("selection", "Требуется выбор раздачи"), ("found", "Раздача найдена")]:
        group = [row for row in rows if row.get("event") == event]
        if not group:
            continue
        lines.append(f"\n{label}: {len(group)}")
        for row in group[:12]:
            lines.append(row.get("episode_label", row.get("text", ""))[:110])
        if len(group) > 12:
            lines.append(f"…и ещё {len(group) - 12}")
        if event == "found":
            releases = list(dict.fromkeys(row["release_title"] for row in group if row.get("release_title")))
            for title in releases[:3]:
                lines.append(f"Раздача: {title[:180]}")
    return "\n".join(lines)[:4000]


def queue_episode_notification(db, sub, event, release_title=None):
    cfg = db.get(ConfigEntry, "telegram")
    if not sub.episode_id or not cfg or not cfg.value.get("enabled"):
        return
    bot_id = cfg.value.get("bot_id")
    task = db.get(Task, sub.task_id)
    key = f"{PREFIX}{bot_id}.{task.id}.{task.created_at}.{sub.episode_id}.{event}"
    if db.get(ConfigEntry, key):
        return
    recipients = list(
        db.scalars(
            select(TelegramUser.id).where(TelegramUser.bot_id == bot_id, TelegramUser.status == "approved")
        )
    )
    if not recipients:
        return
    episode = db.get(Episode, sub.episode_id)
    season = db.get(Season, episode.season_id)
    media = db.get(Media, task.media_id)
    heading = "Найдена раздача для серии" if event == "found" else "Требуется выбор раздачи для серии"
    text = f"{heading}\n{media.title} — S{season.number:02d}E{episode.number:02d}"
    if episode.title:
        text += f"\n{episode.title}"
    if release_title:
        text += f"\nРаздача: {release_title}"
    if event == "selection":
        text += "\nОткройте задачу в Lazarr и выберите раздачу."
    db.add(
        ConfigEntry(
            key=key,
            value={
                "bot_id": bot_id,
                "task_id": task.id,
                "task_created_at": task.created_at,
                "subtask_id": sub.id,
                "episode_label": f"S{season.number:02d}E{episode.number:02d} — {episode.title or ''}",
                "media_title": media.title,
                "release_title": release_title,
                "event": event,
                "queued_at": time.time(),
                "text": text[:4096],
                "recipients": {str(identity): 0 for identity in recipients},
                "pending": True,
            },
        )
    )
