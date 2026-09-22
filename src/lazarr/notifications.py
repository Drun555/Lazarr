"""Durable episode notifications, queued in the worker's transaction."""

from sqlalchemy import select

from lazarr.models import ConfigEntry, Episode, Media, Season, Task, TelegramUser

PREFIX = "telegram.notification."


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
                "text": text[:4096],
                "recipients": {str(identity): 0 for identity in recipients},
                "pending": True,
            },
        )
    )
