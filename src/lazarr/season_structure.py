"""Local season numbers, with provider numbers retained across manual insertions."""

from copy import deepcopy

from sqlalchemy import select

from lazarr.models import ConfigEntry, Episode, Media, Season, Task, TaskSeason


def insertions(db, media_id):
    entry = db.get(ConfigEntry, f"season_structure.{media_id}")
    return entry.value.get("insertions", []) if entry else []


def local_number(number, positions):
    for position in positions:
        if isinstance(position, dict):
            number = position.get(str(number), number)
            continue
        if number >= position:
            number += 1
    return number


def provider_number(number, positions):
    for position in reversed(positions):
        if isinstance(position, dict):
            number = next((int(old) for old, new in position.items() if new == number), number)
            continue
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


def ensure_idle(db, media_id):
    task_ids = set(db.scalars(select(Task.id).where(Task.media_id == media_id)))
    if any(
        entry.value.get("task_id") in task_ids and entry.value.get("state") in {"queued", "running"}
        for entry in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith("mapping_job.")))
    ):
        raise ValueError("Дождитесь применения сопоставления в «Процессах», затем измените порядок сезонов")


def reorder(db, media_id, order):
    """Permute stable season IDs and provider numbering, including unloaded seasons."""
    media = db.get(Media, media_id)
    seasons = list(db.scalars(select(Season).where(Season.media_id == media_id)))
    known = {item["number"] for item in local_metadata(db, media).get("seasons", [])}
    known.update(season.number for season in seasons)
    if len(order) != len(set(order)) or set(order) != known:
        raise ValueError("Список сезонов изменился. Откройте редактор заново.")
    if 0 in known and order[0] != 0:
        raise ValueError("Спецматериалы должны оставаться сезоном 0")
    numbers = {old: index + (0 if 0 in known else 1) for index, old in enumerate(order)}
    if all(old == new for old, new in numbers.items()):
        return
    tasks = list(db.scalars(select(Task).where(Task.media_id == media_id)))
    ensure_idle(db, media_id)
    original = {season.id: season.number for season in seasons}
    for season in seasons:
        season.number = -season.id
    db.flush()
    for season in seasons:
        season.number = numbers[original[season.id]]
    for task in tasks:
        memberships = list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id)))
        for membership in memberships:
            membership.selection_key = f"moving:{membership.id}"
        db.flush()
        for membership in memberships:
            if membership.numbering:
                value = dict(membership.numbering)
                value["season"] = numbers.get(value["season"], value["season"])
                membership.numbering = value
                membership.selection_key = f"alt:{value['season']}"
            else:
                membership.selection_key = str(db.get(Season, membership.season_id).number)
        if task.numbering:
            task.numbering = {
                **task.numbering,
                "season": numbers.get(task.numbering["season"], task.numbering["season"]),
            }
        prefix = f"season_mapping.{task.id}.{task.created_at}."
        pools = list(db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(prefix))))
        keys = {
            pool.key: prefix + str(numbers.get(int(pool.key[len(prefix) :]), int(pool.key[len(prefix) :])))
            for pool in pools
        }
        originals = [(pool, pool.key) for pool in pools]
        for index, (pool, _) in enumerate(originals):
            pool.key = prefix + f"moving:{index}"
        db.flush()
        for pool, old in originals:
            pool.key = keys[old]
    for episode_id in db.scalars(select(Episode.id).join(Season).where(Season.media_id == media_id)):
        position = db.get(ConfigEntry, f"special_position.{episode_id}")
        if position:
            position.value = {
                key: numbers.get(value, value) if key in {"airsbefore_season", "airsafter_season"} else value
                for key, value in position.value.items()
            }
    key = f"season_structure.{media_id}"
    entry = db.get(ConfigEntry, key)
    value = {"insertions": [*insertions(db, media_id), {str(old): new for old, new in numbers.items()}]}
    if entry:
        entry.value = value
    else:
        db.add(ConfigEntry(key=key, value=value))
    db.flush()
