"""Special episode placement, with persistent manual overrides and date-based defaults."""

from datetime import date
import time

from sqlalchemy import select

from lazarr.models import ConfigEntry, Episode, Media, Season


def valid_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def infer_position(air_date, catalog):
    aired = valid_date(air_date)
    if not aired:
        return {}
    timeline = sorted(
        (valid_date(ep.get("air_date")), season["number"], ep["number"])
        for season in catalog
        for ep in season["episodes"]
        if valid_date(ep.get("air_date"))
    )
    # A date alone cannot order two episodes released on the same day.
    if not timeline or any(day == aired for day, _, _ in timeline):
        return {}
    following = next((entry for entry in timeline if entry[0] > aired), None)
    previous = next((entry for entry in reversed(timeline) if entry[0] < aired), None)
    if following:
        _, season, episode = following
        if previous and previous[1] == season:
            return {"airsbefore_season": season, "airsbefore_episode": episode}
        if previous:
            return {"airsafter_season": previous[1]}
        return {"airsbefore_season": season}
    return {"airsafter_season": previous[1]}


async def refresh_catalog(service, media_id):
    """Fetch unselected seasons too: their dates are needed to place specials correctly."""
    key = f"special_catalog.{media_id}"
    with service.db.session() as db:
        media = db.get(Media, media_id)
        cached = db.get(ConfigEntry, key)
        if cached and cached.value.get("updated", 0) > time.time() - 86400:
            return
        provider_id, external_id = media.provider, media.external_id
    async with service.plugins.open(provider_id) as provider:
        item = await provider.get_media("tv", external_id)
        catalog = await fetch_catalog(provider, item)
    with service.db.session() as db:
        entry = db.get(ConfigEntry, key)
        value = {"updated": time.time(), "seasons": catalog}
        if entry:
            entry.value = value
        else:
            db.add(ConfigEntry(key=key, value=value))


async def fetch_catalog(provider, item):
    catalog = []
    for season in item.seasons:
        number = season["number"]
        if number == 0:
            continue
        info = await provider.get_season(item.id, number)
        catalog.append(
            {
                "number": info.number,
                "episodes": [{"number": ep.number, "air_date": ep.air_date} for ep in info.episodes],
            }
        )
    return catalog


def save_catalog(db, media_id, catalog):
    key = f"special_catalog.{media_id}"
    entry = db.get(ConfigEntry, key)
    value = {"updated": time.time(), "seasons": catalog}
    if entry:
        entry.value = value
    else:
        db.add(ConfigEntry(key=key, value=value))


def catalog_for(db, media_id):
    from lazarr.season_structure import insertions, local_number

    saved = db.get(ConfigEntry, f"special_catalog.{media_id}")
    positions = insertions(db, media_id)
    seasons = (
        {
            local_number(s["number"], positions): {**s, "number": local_number(s["number"], positions)}
            for s in saved.value["seasons"]
        }
        if saved
        else {}
    )
    for season in db.scalars(select(Season).where(Season.media_id == media_id, Season.number > 0)):
        episodes = list(db.scalars(select(Episode).where(Episode.season_id == season.id)))
        if episodes:
            seasons[season.number] = {
                "number": season.number,
                "episodes": [{"number": ep.number, "air_date": ep.air_date} for ep in episodes],
            }
    return sorted(seasons.values(), key=lambda s: s["number"])


def placement(db, episode, catalog):
    automatic = infer_position(episode.air_date, catalog)
    saved = db.get(ConfigEntry, f"special_position.{episode.id}")
    return {
        "mode": "manual" if saved else "auto",
        "position": saved.value if saved else automatic,
        "automatic": automatic,
    }
