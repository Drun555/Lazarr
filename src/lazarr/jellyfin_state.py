"""User state and query semantics shared by Jellyfin routes."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, text

from lazarr.models import ConfigEntry, LibraryAsset, Media, PlaybackProgress, PlaybackSession, Subtask


class UserDataUpdate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    IsFavorite: StrictBool | None = None
    Likes: StrictBool | None = None
    Rating: float | None = None
    Played: StrictBool | None = None
    PlaybackPositionTicks: int | None = Field(default=None, ge=0, le=2**63 - 1)
    PlayCount: int | None = Field(default=None, ge=0, le=2**31 - 1)
    LastPlayedDate: datetime | None = None


class DisplayPreferences(BaseModel):
    ViewType: str | None = None
    SortBy: str | None = "SortName"
    SortOrder: str = "Ascending"
    IndexBy: str | None = None
    RememberIndexing: bool = False
    RememberSorting: bool = False
    PrimaryImageHeight: int = Field(default=250, ge=0)
    PrimaryImageWidth: int = Field(default=250, ge=0)
    CustomPrefs: dict[str, str | None] = Field(default_factory=dict)
    ScrollDirection: str = "Horizontal"
    ShowBackdrop: bool = True
    ShowSidebar: bool = False


def parameter(params, name, default=None):
    return next((v for k, v in params.items() if k.casefold() == name.casefold()), default)


def csv_parameter(params, name):
    pairs = params.multi_items() if hasattr(params, "multi_items") else params.items()
    return [
        v.strip() for k, raw in pairs if k.casefold() == name.casefold() for v in raw.split(",") if v.strip()
    ]


def bool_parameter(params, name, default=False):
    raw = parameter(params, name)
    if raw is None:
        return default
    if raw.casefold() not in {"true", "false"}:
        raise HTTPException(400, f"Invalid {name}")
    return raw.casefold() == "true"


def number_parameter(params, name, default=None):
    value = parameter(params, name)
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"Invalid {name}") from exc
    if number < 0:
        raise HTTPException(400, f"Invalid {name}")
    return number


def page(items, params):
    from lazarr.jellyfin import query_result
    from lazarr.jellyfin_catalog import project

    result = query_result(items, number_parameter(params, "startIndex", 0), number_parameter(params, "limit"))
    result["Items"] = [project(item, params) for item in result["Items"]]
    if not bool_parameter(params, "enableTotalRecordCount", True):
        result["TotalRecordCount"] = 0
    return result


def filter_items(items, params):
    includes = set(csv_parameter(params, "includeItemTypes"))
    excludes = set(csv_parameter(params, "excludeItemTypes"))
    media_types = set(csv_parameter(params, "mediaTypes"))
    ids = set(csv_parameter(params, "ids"))
    filters = set(csv_parameter(params, "filters"))
    term = parameter(params, "searchTerm", "").casefold()
    result = []
    for item in items:
        data = item.get("UserData", {})
        if includes and item.get("Type") not in includes:
            continue
        if item.get("Type") in excludes or (media_types and item.get("MediaType") not in media_types):
            continue
        if ids and item["Id"] not in ids:
            continue
        if term and term not in item.get("Name", "").casefold():
            continue
        if parameter(params, "isFavorite") is not None and bool(data.get("IsFavorite")) != bool_parameter(
            params, "isFavorite"
        ):
            continue
        if parameter(params, "isPlayed") is not None and bool(data.get("Played")) != bool_parameter(
            params, "isPlayed"
        ):
            continue
        checks = {
            "IsFavorite": data.get("IsFavorite", False),
            "IsFavoriteOrLikes": data.get("IsFavorite", False) or data.get("Likes") is True,
            "IsLiked": data.get("Likes") is True,
            "IsDisliked": data.get("Likes") is False,
            "IsPlayed": data.get("Played", False),
            "IsUnplayed": not data.get("Played", False),
            "IsResumable": data.get("PlaybackPositionTicks", 0) > 0,
            "IsFolder": item.get("IsFolder", False),
            "IsNotFolder": not item.get("IsFolder", False),
        }
        if any(not checks[f] for f in filters if f in checks):
            continue
        result.append(item)
    from lazarr.jellyfin_catalog import advanced_filter

    return advanced_filter(result, params)


def preferences(ctx, user, identity, client, update=None):
    from lazarr.jellyfin import server_id

    try:
        identity = str(uuid.UUID(identity))
    except ValueError:
        identity = str(uuid.uuid5(uuid.UUID(server_id(ctx)), f"display:{identity}:{client}"))
    # Hash an unambiguous tuple: client and preference IDs may themselves contain dots.
    digest = hashlib.sha256(json.dumps([identity, client]).encode()).hexdigest()
    key = f"jellyfin.display.{user.id}.{digest}"
    with ctx.db.session() as db:
        if update is not None:
            db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(ConfigEntry, key)
        value = {**DisplayPreferences().model_dump(), **(row.value if row else {})}
        if update is not None:
            if update.SortOrder not in {"Ascending", "Descending"} or update.ScrollDirection not in {
                "Horizontal",
                "Vertical",
            }:
                raise HTTPException(400, "Invalid display preferences")
            value.update(update.model_dump(exclude_unset=True))
            if row:
                row.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
    return {
        **value,
        "Id": identity,
        "Client": client,
    }


def update_user_data(ctx, user, item_id, update, *, clear_likes=False):
    from lazarr.jellyfin import item_dto, object_id, playback_for_item, progress_targets

    item = item_dto(ctx, item_id, user)  # Includes playlist ACL and existence checks.
    item_id = item["Id"]
    targets = [item_id]
    if update.Played is not None and item["Type"] in {"Series", "Season"}:
        targets, _ = progress_targets(ctx, item_id)
    mapping = {
        "IsFavorite": "is_favorite",
        "Likes": "likes",
        "Rating": "rating",
        "Played": "played",
        "PlaybackPositionTicks": "position_ticks",
        "PlayCount": "play_count",
        "LastPlayedDate": "last_played_at",
    }
    with ctx.db.session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        playback_fields = {"Played", "PlaybackPositionTicks", "PlayCount", "LastPlayedDate"}
        values = update.model_dump(exclude_none=True)
        if playback_fields.intersection(values):
            sources = set(
                db.scalars(
                    select(PlaybackSession.source_id).where(
                        PlaybackSession.user_id == user.id, PlaybackSession.item_id.in_(targets)
                    )
                )
            )
            if update.Played is None:
                current = playback_for_item(ctx, item_id)
                sources &= {object_id("asset", current["asset"].id)} if current else set()
            targets = targets + list(sources)
        for target in set(targets + [item_id]):
            row = db.scalar(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id, PlaybackProgress.item_id == target
                )
            )
            if not row:
                row = PlaybackProgress(user_id=user.id, item_id=target)
                db.add(row)
            for field, value in values.items():
                if target != item_id and field not in {
                    "Played",
                    "PlaybackPositionTicks",
                    "PlayCount",
                    "LastPlayedDate",
                }:
                    continue
                if field == "LastPlayedDate":
                    value = (
                        value.replace(tzinfo=timezone.utc).timestamp()
                        if value.tzinfo is None
                        else value.timestamp()
                    )
                setattr(row, mapping[field], value)
            if clear_likes and target == item_id:
                row.likes = None
            row.updated_at = time.time()
    return item_dto(ctx, item_id, user)["UserData"]


def record_playback(ctx, user, item_id, payload, event, fallback_key):
    from lazarr.jellyfin import duration_ticks, object_id, playback_for_item
    from lazarr.jellyfin_resources import is_extra

    playable = playback_for_item(ctx, item_id, payload.get("MediaSourceId"))
    if not playable:
        raise HTTPException(404, "Playable file not found")
    link = playable["link"]
    if isinstance(link, LibraryAsset):
        item_id = (
            object_id("episode", link.episode_id) if link.episode_id else object_id("media", link.media_id)
        )
    else:
        with ctx.db.session() as db:
            task = db.get(Subtask, link.subtask_id)
            item_id = (
                object_id("episode", task.episode_id)
                if task.episode_id
                else object_id("media", playable["asset"].media_id)
            )
    key = payload.get("PlaySessionId") or payload.get("SessionId") or fallback_key
    if not isinstance(key, str) or not key or len(key) > 128:
        raise HTTPException(400, "Invalid playback session")
    raw_position = payload.get("PositionTicks")
    if raw_position is not None and (
        isinstance(raw_position, bool) or not isinstance(raw_position, int) or not 0 <= raw_position < 2**63
    ):
        raise HTTPException(400, "Invalid PositionTicks")
    now = time.time()
    source = object_id("asset", playable["asset"].id)
    part = getattr(playable["link"], "part_key", "") or item_id
    extra = is_extra(part)
    if extra:
        item_id = source
    with ctx.db.session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        history = db.scalar(
            select(PlaybackSession).where(
                PlaybackSession.user_id == user.id,
                PlaybackSession.item_id == item_id,
                PlaybackSession.session_key == key,
            )
        )
        if history and event == "start" and history.stopped_at and not payload.get("PlaySessionId"):
            history.session_key = uuid.uuid4().hex
            db.flush()
            history = None
        if history and history.stopped_at:
            return  # Retried stop or delayed progress cannot resurrect a closed session.
        if history and event == "start":
            return
        row = db.scalar(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == user.id, PlaybackProgress.item_id == item_id
            )
        )
        if row is None:
            row = PlaybackProgress(
                user_id=user.id, item_id=item_id, play_count=0, position_ticks=0, played=False
            )
            db.add(row)
        version = (
            row
            if source == item_id
            else db.scalar(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id, PlaybackProgress.item_id == source
                )
            )
        )
        if version is None:
            version = PlaybackProgress(
                user_id=user.id, item_id=source, play_count=0, position_ticks=0, played=row.played
            )
            db.add(version)
        if history is None:
            history = PlaybackSession(
                user_id=user.id,
                item_id=item_id,
                session_key=key,
                source_id=source,
                part_key=part,
                started_at=now,
                completed=False,
            )
            db.add(history)
            if not (event == "stop" and payload.get("Failed")):
                row.play_count += 1
                if version is not row:
                    version.play_count += 1
        elif history.source_id != source:
            raise HTTPException(400, "Media source does not match playback session")
        history.updated_at = now
        history.failed = bool(payload.get("Failed", False)) if event == "stop" else False
        if event == "stop":
            history.stopped_at = now
        if history.failed:
            return
        runtime = duration_ticks(playable["asset"])
        position = raw_position
        if event == "stop" and position is None:
            position = runtime or history.position_ticks or 0
        if position is not None:
            history.position_ticks = position
            complete = bool(runtime and position >= runtime * 0.9) or (
                event == "stop" and raw_position is None
            )
            if complete:
                history.completed = True
                if isinstance(link, LibraryAsset) and not extra:
                    links = list(
                        db.scalars(
                            select(LibraryAsset).where(
                                LibraryAsset.media_id == link.media_id,
                                LibraryAsset.episode_id == link.episode_id,
                            )
                        )
                    )
                    parts = {
                        candidate.part_key
                        for candidate in links
                        if candidate.verification.get("complete") and not is_extra(candidate.part_key)
                    }
                else:
                    parts = {part}
                completed_parts = set(
                    db.scalars(
                        select(PlaybackSession.part_key).where(
                            PlaybackSession.user_id == user.id,
                            PlaybackSession.item_id == item_id,
                            PlaybackSession.completed.is_(True),
                        )
                    )
                ) | {part}
                row.played = row.played or parts.issubset(completed_parts)
                row.position_ticks = 0
                version.played = True
                version.position_ticks = 0
                # Completed variants share watched state and leave Continue Watching together.
                source_ids = set(
                    db.scalars(
                        select(PlaybackSession.source_id).where(
                            PlaybackSession.user_id == user.id,
                            PlaybackSession.item_id == item_id,
                            PlaybackSession.part_key == part,
                        )
                    )
                )
                for sibling in db.scalars(
                    select(PlaybackProgress).where(
                        PlaybackProgress.user_id == user.id, PlaybackProgress.item_id.in_(source_ids)
                    )
                ):
                    sibling.played = True
                    sibling.position_ticks = 0
            elif not history.completed:
                # Keep the watched flag during rewatch; resume is independent of it.
                row.position_ticks = 0 if runtime and position < runtime * 0.05 else position
                version.position_ticks = row.position_ticks
        row.last_played_at = now
        row.updated_at = now
        version.last_played_at = now
        version.updated_at = now


def next_up(ctx, user, params, batch=None):
    from lazarr.jellyfin import episode_dto, library_ids, object_id
    from lazarr.library import library_kind

    parent = parameter(params, "parentId")
    series_id = parameter(params, "seriesId")
    from lazarr.jellyfin import user_configuration

    config = user_configuration(ctx, user, batch)
    with ctx.db.session() as db:
        series = list(db.scalars(select(Media).where(Media.kind == "tv")))
    candidates = []
    cutoff = parameter(params, "nextUpDateCutoff")
    if cutoff:
        try:
            cutoff = (
                datetime.fromisoformat(cutoff.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp()
            )
        except ValueError as exc:
            raise HTTPException(400, "Invalid nextUpDateCutoff") from exc
    for media in series:
        identity = object_id("media", media.id)
        library = library_ids(ctx)[library_kind(media)]
        if series_id and identity != series_id:
            continue
        if parent and parent not in {library, identity}:
            continue
        if not parent and not series_id and library in config.get("LatestItemsExcludes", []):
            continue
        episodes = [
            item
            for e in (batch.detail(media.id) if batch else ctx.library.detail(media.id))["episodes"]
            if e["season"] and e["season"] > 0
            for item in [episode_dto(ctx, media, e, user, batch=batch)]
            if item and item.get("PlayAccess") == "Full"
        ]
        episodes.sort(key=lambda i: (i["ParentIndexNumber"], i["IndexNumber"], i["Id"]))
        if not episodes:
            continue
        dates = [i["UserData"].get("LastPlayedDate", "") for i in episodes]
        latest = max(dates, default="")
        if not latest and not series_id:
            continue
        if cutoff and (
            not latest or datetime.fromisoformat(latest.replace("Z", "+00:00")).timestamp() < cutoff
        ):
            continue
        watched = [n for n, i in enumerate(episodes) if i["UserData"]["Played"]]
        # Continue after the furthest watched episode, not an earlier intentionally skipped one.
        start = max(watched, default=-1) + 1
        normal = next((i for i in episodes[start:] if not i["UserData"]["Played"]), None)
        if normal and (
            bool_parameter(params, "enableResumable", True) or not normal["UserData"]["PlaybackPositionTicks"]
        ):
            candidates.append((latest, normal))
        if bool_parameter(params, "enableRewatching") and latest:
            recent = max(range(len(episodes)), key=lambda n: dates[n])
            following = episodes[recent + 1] if recent + 1 < len(episodes) else None
            if (
                following
                and following["UserData"]["Played"]
                and not following["UserData"]["PlaybackPositionTicks"]
                and dates[recent] > dates[recent + 1]
            ):
                candidates.append((latest, following))
    candidates.sort(key=lambda pair: (pair[0], pair[1]["Id"]), reverse=True)
    return page([item for _, item in candidates], params)
