from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from lazarr.sdk import ReleaseCalendarProvider, ReleaseDate


class TMDBCalendar(ReleaseCalendarProvider):
    async def release_date(self, media, episode=None):
        value = episode.air_date if episode else media.release_date
        return ReleaseDate(
            value=value, precision="datetime" if value and "T" in value else "date" if value else "unknown"
        )


def released(value: str | None, timezone_name: str, now: datetime | None = None) -> bool:
    if not value:
        return True
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(timezone_name) if isinstance(timezone_name, str) else timezone_name
    if "T" in value:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=tz)
    else:
        stamp = datetime.combine(date.fromisoformat(value) + timedelta(days=1), time.min, tz)
    return now >= stamp


def next_search_start(start, tz, now=None):
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    day = local.date() + timedelta(days=local.strftime("%H:%M") >= start)
    return datetime.combine(day, time.fromisoformat(start), tz).timestamp()
