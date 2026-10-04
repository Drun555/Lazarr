"""Local season numbers, with provider numbers retained across manual insertions."""

from copy import deepcopy

from sqlalchemy import select

from lazarr.models import ConfigEntry, Season, Task, TaskSeason


def insertions(db, media_id):
    entry = db.get(ConfigEntry, f"season_structure.{media_id}")
    return entry.value.get("insertions", []) if entry else []


def local_number(number, positions):
    for position in positions:
        if number >= position:
            number += 1
    return number


def provider_number(number, positions):
    for position in reversed(positions):
        if number == position:
            raise ValueError("Ручной сезон не имеет номера в TMDB")
        if number > position:
            number -= 1
    return number


def local_metadata(db, media):
    data = deepcopy(media.metadata_json)
    positions = insertions(db, media.id)
    for season in data.get("seasons", []):
        season["number"] = local_number(season["number"], positions)
    numbering = {}
    for key, aliases in data.get("episode_numbering", {}).items():
        season, episode = map(int, key.split(":"))
        for alias in aliases:
            alias["season"] = local_number(alias["season"], positions)
        numbering[f"{local_number(season, positions)}:{episode}"] = aliases
    data["episode_numbering"] = numbering
    return data


def insert(db, media_id, number):
    # Descending updates avoid the unique (media_id, number) constraint. IDs and
    # all episode/file foreign keys remain unchanged.
    for season in db.scalars(
        select(Season)
        .where(Season.media_id == media_id, Season.number >= number)
        .order_by(Season.number.desc())
    ):
        season.number += 1
        db.flush()
    tasks = list(db.scalars(select(Task).where(Task.media_id == media_id)))
    for task in tasks:
        memberships = list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id)))
        # Free selection keys before assigning their shifted values.
        for membership in memberships:
            membership.selection_key = f"moving:{membership.id}"
        db.flush()
        for membership in memberships:
            if membership.numbering:
                value = dict(membership.numbering)
                if value["season"] >= number:
                    value["season"] += 1
                membership.numbering = value
                membership.selection_key = f"alt:{value['season']}"
            else:
                membership.selection_key = str(db.get(Season, membership.season_id).number)
        if task.numbering and task.numbering["season"] >= number:
            task.numbering = {**task.numbering, "season": task.numbering["season"] + 1}
        prefix = f"season_mapping.{task.id}.{task.created_at}."
        pools = list(db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(prefix))))
        for pool in sorted(pools, key=lambda entry: int(entry.key[len(prefix) :]), reverse=True):
            old_number = int(pool.key[len(prefix) :])
            if old_number >= number:
                pool.key = prefix + str(old_number + 1)
                db.flush()
    key = f"season_structure.{media_id}"
    entry = db.get(ConfigEntry, key)
    value = {"insertions": [*insertions(db, media_id), number]}
    if entry:
        entry.value = value
    else:
        db.add(ConfigEntry(key=key, value=value))
    db.flush()
