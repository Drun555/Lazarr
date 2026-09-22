"""Read-only Jellyfin-compatible API backed by Lazarr's verified media."""

import asyncio
import hashlib
import json
import mimetypes
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from sqlalchemy import select, delete, text

from lazarr import jellyfin_catalog, jellyfin_playlists, jellyfin_resources, jellyfin_state
from lazarr.jellyfin_state import (
    DisplayPreferences,
    UserDataUpdate,
    bool_parameter,
    csv_parameter,
    filter_items,
    number_parameter,
    page,
    parameter,
)

from lazarr.library import LIBRARIES, library_kind
from lazarr.languages import CODES, language, language_name
from lazarr.matcher import classify_external_subtitles
from lazarr.models import (
    Download,
    Episode,
    ConfigEntry,
    LoginSession,
    LibraryAsset,
    Media,
    MediaAsset,
    PlaybackProgress,
    PlaybackSession,
    Season,
    Subtask,
    SubtaskAsset,
    Task,
    User,
)
from lazarr.posters import fetch_poster
from lazarr.security import audit, hash_token, new_session, verify_password
from lazarr.subtitle_language import detect_subtitle_language


API_VERSION = "12.0.0"
TICKS_PER_SECOND = 10_000_000
KINDS = {"media": 1, "season": 2, "episode": 3, "asset": 4, "user": 5, "playlist": 6}
KIND_NAMES = {value: key for key, value in KINDS.items()}
LIBRARY_COLLECTIONS = {"series": "tvshows", "movies": "movies", "anime": "tvshows"}
VISIBLE_DOWNLOAD_STATES = {"starting", "downloading", "ready"}


class JellyfinDtoBatch:
    """Request-scoped cache for the data shared by Jellyfin DTOs."""

    def __init__(self, ctx, user=None):
        self.ctx = ctx
        self.user_id = user.id if user else None
        self._details = {}
        self._progress = None
        self._playables = None
        self._playables_by_asset = None
        self._media_metadata = None
        self._settings = None
        self._selections = None
        self._episode_media = None
        self._user_configuration = None

    def detail(self, media_id):
        if media_id not in self._details:
            self._details[media_id] = self.ctx.library.detail(media_id)
        return self._details[media_id]

    def progress(self, item_id):
        if self._progress is None:
            if self.user_id is None:
                self._progress = {}
            else:
                with self.ctx.db.session() as db:
                    self._progress = {
                        row.item_id: row
                        for row in db.scalars(
                            select(PlaybackProgress).where(PlaybackProgress.user_id == self.user_id)
                        )
                    }
        return self._progress.get(item_id)

    def progress_rows(self, item_ids):
        result = {}
        for identity in item_ids:
            row = self.progress(identity)
            if row:
                result[identity] = row
        return result

    def settings(self):
        if self._settings is None:
            self._settings = self.ctx.service.settings()
        return self._settings

    def stored_user_configuration(self):
        if self._user_configuration is None:
            if self.user_id is None:
                self._user_configuration = {}
            else:
                with self.ctx.db.session() as db:
                    row = db.get(ConfigEntry, f"jellyfin.user.{self.user_id}")
                    self._user_configuration = dict(row.value) if row else {}
        return self._user_configuration

    def media_id_for_item(self, item_id):
        kind, identity, _ = parse_object_id(item_id)
        if kind in {"media", "season"}:
            return identity
        if kind == "episode":
            if self._episode_media is None:
                with self.ctx.db.session() as db:
                    self._episode_media = {
                        episode_id: media_id
                        for episode_id, media_id in db.execute(
                            select(Episode.id, Season.media_id).join(Season, Season.id == Episode.season_id)
                        )
                    }
            return self._episode_media.get(identity)
        if kind == "asset":
            playable = self.playable(kind, identity)
            return playable["asset"].media_id if playable else None
        return None

    def playback_selection(self, item_id):
        media_id = self.media_id_for_item(item_id)
        if media_id is None or self.user_id is None:
            return {}
        if self._selections is None:
            prefix = f"jellyfin.selection.{self.user_id}."
            with self.ctx.db.session() as db:
                self._selections = {
                    int(row.key.removeprefix(prefix)): dict(row.value)
                    for row in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith(prefix)))
                }
        return self._selections.get(media_id, {})

    def media_metadata(self, media_id):
        if self._media_metadata is None:
            self._media_metadata = {}
            with self.ctx.db.session() as db:
                for identity, created_at, part_key, asset_id in db.execute(
                    select(
                        LibraryAsset.media_id,
                        LibraryAsset.created_at,
                        LibraryAsset.part_key,
                        LibraryAsset.asset_id,
                    ).order_by(LibraryAsset.created_at)
                ):
                    row = self._media_metadata.setdefault(identity, {"added": None, "extras": []})
                    row["added"] = row["added"] or created_at
                    row["extras"].append((part_key, asset_id))
                for identity, created_at in db.execute(select(Task.media_id, Task.created_at)):
                    row = self._media_metadata.setdefault(identity, {"added": None, "extras": []})
                    row["added"] = row["added"] or created_at
        return self._media_metadata.get(media_id, {"added": None, "extras": []})

    def _load_playables(self):
        if self._playables is not None:
            return
        self._playables = {}
        self._playables_by_asset = {}
        with self.ctx.db.session() as db:
            stored = db.execute(
                select(LibraryAsset, MediaAsset, Download)
                .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
                .join(Download, Download.id == MediaAsset.download_id)
                .order_by(MediaAsset.id.desc())
            )
            for link, asset, download in stored:
                if not link.verification.get("complete"):
                    continue
                path = playable_path(download, asset.path)
                if not path:
                    continue
                playable = {"asset": asset, "download": download, "link": link, "path": path}
                self._playables_by_asset.setdefault(asset.id, playable)
                if jellyfin_resources.is_extra(link.part_key) or link.part_key.startswith("part:"):
                    continue
                key = ("episode", link.episode_id) if link.episode_id else ("media", link.media_id)
                self._playables.setdefault(key, playable)

            partial = db.execute(
                select(SubtaskAsset, Subtask, MediaAsset, Download)
                .join(Subtask, Subtask.id == SubtaskAsset.subtask_id)
                .join(MediaAsset, MediaAsset.id == SubtaskAsset.asset_id)
                .join(Download, Download.id == MediaAsset.download_id)
                .where(Subtask.status == "ready")
                .order_by(SubtaskAsset.current.desc(), SubtaskAsset.id.desc())
            )
            for link, subtask, asset, download in partial:
                binding = (download.stats or {}).get("bindings", {}).get(str(subtask.id), {})
                path = playable_path(download, asset.path)
                if not binding.get("buffer_ready") or not path:
                    continue
                playable = {"asset": asset, "download": download, "link": link, "path": path}
                self._playables_by_asset.setdefault(asset.id, playable)
                key = ("episode", subtask.episode_id) if subtask.episode_id else ("media", asset.media_id)
                self._playables.setdefault(key, playable)

    def playable(self, kind, identity, source_id=None):
        self._load_playables()
        asset_id = None
        if source_id and source_id != object_id(kind, identity):
            source_kind, asset_id, _ = parse_object_id(source_id)
            if source_kind != "asset":
                return None
        if kind == "asset":
            asset_id = identity
        if asset_id is not None:
            candidate = self._playables_by_asset.get(asset_id)
            if not candidate:
                return None
            link = candidate["link"]
            if kind == "episode":
                linked = getattr(link, "episode_id", None)
                if linked is None and isinstance(link, SubtaskAsset):
                    with self.ctx.db.session() as db:
                        linked = db.get(Subtask, link.subtask_id).episode_id
                if linked != identity:
                    return None
            elif kind == "media" and candidate["asset"].media_id != identity:
                return None
            return candidate
        return self._playables.get((kind, identity))

    def versions(self, item_id, current):
        self._load_playables()
        kind, identity, _ = parse_object_id(item_id)
        if kind == "asset" or not isinstance(current["link"], LibraryAsset):
            return [current]
        part_key = current["link"].part_key
        candidates = []
        for playable in self._playables_by_asset.values():
            link = playable["link"]
            if not isinstance(link, LibraryAsset) or link.part_key != part_key:
                continue
            if jellyfin_resources.is_extra(link.part_key):
                continue
            if kind == "episode" and link.episode_id != identity:
                continue
            if kind == "media" and (link.media_id != identity or link.episode_id is not None):
                continue
            candidates.append(playable)
        candidates.sort(key=lambda value: value["asset"].id, reverse=True)
        return [current] + [value for value in candidates if value["asset"].id != current["asset"].id]


def is_fladder(request):
    header = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization", "")
    return bool(re.search(r'\bClient=["\']?Fladder(?:["\',\s]|$)', header, re.I))


def object_id(kind, identity, secondary=0):
    value = (KINDS[kind] << 120) | (int(identity) << 32) | int(secondary)
    return str(uuid.UUID(int=value))


def parse_object_id(value):
    try:
        number = uuid.UUID(str(value)).int
    except (ValueError, AttributeError) as exc:
        raise HTTPException(404, "Item not found") from exc
    kind = KIND_NAMES.get(number >> 120)
    if not kind:
        raise HTTPException(404, "Item not found")
    return kind, (number >> 32) & ((1 << 88) - 1), number & 0xFFFFFFFF


def server_id(ctx):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"lazarr:{ctx.config.data_dir.resolve()}").hex


def user_object_id(ctx, identity):
    # Swiftfin keys saved users globally by User.Id rather than by (ServerId, User.Id).
    # Keep the local database identity reversible while namespacing its low bits per server.
    return object_id("user", identity, int(server_id(ctx)[:8], 16))


def library_ids(ctx):
    namespace = uuid.UUID(server_id(ctx))
    return {key: str(uuid.uuid5(namespace, f"library:{key}")) for key, _ in LIBRARIES}


def iso_date(value):
    return f"{value}T00:00:00.0000000Z" if value else None


def query_result(items, start=0, limit=None):
    total = len(items)
    end = total if limit is None else start + max(0, limit)
    return {"Items": items[start:end], "TotalRecordCount": total, "StartIndex": start}


def jellyfin_language(value):
    normalized = language(value)
    if normalized == "und":
        return "und"
    return next((code for code in CODES[normalized].split() if len(code) == 3), normalized)


def user_configuration(ctx, user, batch=None):
    preferences = (batch.settings() if batch else ctx.service.settings()).jellyfin
    configuration = {
        "AudioLanguagePreference": jellyfin_language(preferences.audio_languages[0])
        if preferences.audio_languages
        else None,
        "SubtitleLanguagePreference": jellyfin_language(preferences.subtitle_languages[0])
        if preferences.subtitle_languages
        else None,
        "PlayDefaultAudioTrack": True,
        "SubtitleMode": "Default" if preferences.subtitle_languages else "None",
        "RememberAudioSelections": False,
        "RememberSubtitleSelections": False,
        "EnableNextEpisodeAutoPlay": True,
    }
    if batch is not None and batch.user_id == user.id:
        configuration.update(batch.stored_user_configuration())
    else:
        with ctx.db.session() as db:
            stored = db.get(ConfigEntry, f"jellyfin.user.{user.id}")
            if stored:
                configuration.update(stored.value)
    return configuration


def user_dto(ctx, user):
    return {
        "Name": user.username,
        "ServerId": server_id(ctx),
        "ServerName": "Lazarr",
        "Id": user_object_id(ctx, user.id),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "EnableAutoLogin": False,
        "Configuration": user_configuration(ctx, user),
        "Policy": {
            "IsAdministrator": user.role == "admin",
            "IsHidden": True,
            "IsDisabled": not user.active,
            "EnableRemoteAccess": True,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": False,
            "EnableVideoPlaybackTranscoding": False,
            "EnablePlaybackRemuxing": False,
            "EnableContentDeletion": False,
            "EnableContentDownloading": False,
            "EnableAllDevices": True,
            "EnableAllFolders": True,
            "AuthenticationProviderId": "Lazarr",
            "PasswordResetProviderId": "Lazarr",
        },
    }


def image_tag(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


def poster_tag(media):
    return image_tag(media.metadata_json.get("poster"))


def backdrop_tag(media):
    return image_tag(media.metadata_json.get("backdrop"))


def provider_ids(media):
    values = {str(k).capitalize(): str(v) for k, v in media.metadata_json.get("external_ids", {}).items()}
    values.setdefault(media.provider.capitalize(), str(media.external_id))
    return values


def user_data(ctx, user, item_id, runtime_ticks=0, batch=None):
    if user is None:
        return {
            "ItemId": item_id,
            "Key": item_id,
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
        }
    if batch is not None and batch.user_id == user.id:
        row = batch.progress(item_id)
    else:
        with ctx.db.session() as db:
            row = db.scalar(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id, PlaybackProgress.item_id == item_id
                )
            )
    position = row.position_ticks if row else 0
    result = {
        "ItemId": item_id,
        "Key": item_id,
        "PlaybackPositionTicks": position,
        "PlayCount": row.play_count if row else 0,
        "IsFavorite": bool(row and row.is_favorite),
        "Likes": row.likes if row else None,
        "Rating": row.rating if row else None,
        "Played": bool(row and row.played),
    }
    if row and row.last_played_at:
        result["LastPlayedDate"] = (
            datetime.fromtimestamp(row.last_played_at, timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if runtime_ticks:
        result["PlayedPercentage"] = min(100, position * 100 / runtime_ticks)
    return result


def visible_episode_item_ids(ctx, media_id, season_number=None, batch=None):
    return [
        object_id("episode", episode["id"])
        for episode in (batch.detail(media_id) if batch else ctx.library.detail(media_id))["episodes"]
        if (season_number is None or episode["season"] == season_number)
        and episode_is_visible(ctx, episode, batch)
    ]


def grouped_user_data(ctx, user, item_id, episode_ids, batch=None):
    result = user_data(ctx, user, item_id, batch=batch)
    if user is None:
        return result
    if batch is not None and batch.user_id == user.id:
        rows = batch.progress_rows(episode_ids)
    else:
        with ctx.db.session() as db:
            rows = {
                row.item_id: row
                for row in db.scalars(
                    select(PlaybackProgress).where(
                        PlaybackProgress.user_id == user.id,
                        PlaybackProgress.item_id.in_(episode_ids),
                    )
                )
            }
    played = sum(bool(rows.get(identity) and rows[identity].played) for identity in episode_ids)
    result.update(
        {
            "Played": bool(episode_ids) and played == len(episode_ids),
            "PlayCount": (
                min(rows.get(identity).play_count if rows.get(identity) else 0 for identity in episode_ids)
                if episode_ids
                else 0
            ),
            "UnplayedItemCount": len(episode_ids) - played,
            "PlayedPercentage": played * 100 / len(episode_ids) if episode_ids else 0,
        }
    )
    last_played = max((row.last_played_at for row in rows.values() if row.last_played_at), default=None)
    if last_played:
        result["LastPlayedDate"] = (
            datetime.fromtimestamp(last_played, timezone.utc).isoformat().replace("+00:00", "Z")
        )
    return result


def media_dto(ctx, media, user=None, batch=None):
    image = poster_tag(media)
    backdrop = backdrop_tag(media)
    kind = "Movie" if media.kind == "movie" else "Series"
    data = media.metadata_json
    result = {
        "Name": media.title,
        **jellyfin_catalog.metadata_fields(ctx, media, batch),
        "OriginalTitle": data.get("original_title"),
        "ServerId": server_id(ctx),
        "Id": object_id("media", media.id),
        "Etag": hashlib.sha256(repr((media.title, media.year, data)).encode()).hexdigest()[:16],
        "SourceType": "Library",
        "CanDelete": False,
        "CanDownload": False,
        "PlayAccess": "Full",
        "Overview": data.get("overview"),
        "Genres": data.get("genres", []),
        "ProductionYear": media.year,
        "PremiereDate": iso_date(data.get("release_date") or data.get("first_air_date")),
        "ProviderIds": provider_ids(media),
        "IsFolder": media.kind != "movie",
        "Type": kind,
        "MediaType": "Video" if media.kind == "movie" else "Unknown",
        "LocationType": "FileSystem",
        "ImageTags": {"Primary": image} if image else {},
        "BackdropImageTags": [backdrop] if backdrop else [],
        "UserData": {},
    }
    playable = playable_asset(ctx, "media", media.id, batch=batch) if media.kind == "movie" else None
    if playable:
        result.update(playable_item_fields(playable, result["Id"]))
        source = media_source(ctx, playable, result["Id"], user, batch=batch)
        result["MediaSources"] = [source] + [
            media_source(
                ctx,
                version,
                result["Id"],
                user,
                source_id=object_id("asset", version["asset"].id),
                batch=batch,
            )
            for version in playback_versions(ctx, result["Id"], playable, batch)[1:]
        ]
        result["MediaSourceCount"] = len(result["MediaSources"])
        result["MediaStreams"] = source["MediaStreams"]
        result["HasSubtitles"] = any(s["Type"] == "Subtitle" for s in source["MediaStreams"])
    if media.kind == "tv":
        result["UserData"] = grouped_user_data(
            ctx,
            user,
            result["Id"],
            visible_episode_item_ids(ctx, media.id, batch=batch),
            batch,
        )
    else:
        result["UserData"] = user_data(ctx, user, result["Id"], result.get("RunTimeTicks", 0), batch)
    return result


def library_dto(ctx, key, name, user=None, batch=None):
    identity = library_ids(ctx)[key]
    return {
        "Name": name,
        "ServerId": server_id(ctx),
        "Id": identity,
        "Etag": identity.replace("-", "")[:16],
        "SourceType": "Library",
        "CanDelete": False,
        "CanDownload": False,
        "IsFolder": True,
        "Type": "CollectionFolder",
        "CollectionType": LIBRARY_COLLECTIONS[key],
        "LocationType": "FileSystem",
        "ImageTags": {},
        "BackdropImageTags": [],
        "UserData": user_data(ctx, user, identity, batch=batch),
    }


def season_dto(ctx, media, number, user=None, batch=None):
    count = sum(
        1
        for episode in (batch.detail(media.id) if batch else ctx.library.detail(media.id))["episodes"]
        if episode["season"] == number and episode_is_visible(ctx, episode, batch)
    )
    backdrop = backdrop_tag(media)
    identity = object_id("season", media.id, number)
    return {
        "Name": f"Сезон {number}",
        "ServerId": server_id(ctx),
        "Id": identity,
        "SeriesName": media.title,
        "SeriesId": object_id("media", media.id),
        "ParentId": object_id("media", media.id),
        "IndexNumber": number,
        "IsFolder": True,
        "Type": "Season",
        "ChildCount": count,
        "RecursiveItemCount": count,
        "ParentPrimaryImageItemId": object_id("media", media.id),
        "ParentPrimaryImageTag": poster_tag(media),
        "ParentBackdropItemId": object_id("media", media.id),
        "ParentBackdropImageTags": [backdrop] if backdrop else [],
        "LocationType": "FileSystem",
        "ImageTags": {},
        "BackdropImageTags": [backdrop] if backdrop else [],
        "SeriesPrimaryImageTag": poster_tag(media),
        "UserData": grouped_user_data(
            ctx,
            user,
            identity,
            visible_episode_item_ids(ctx, media.id, number, batch),
            batch,
        ),
    }


def playable_path(download, relative):
    if not isinstance(relative, str) or not relative:
        return None
    root = Path(download.save_path).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def available_seasons(ctx, media, batch=None):
    detail = batch.detail(media.id) if batch else ctx.library.detail(media.id)
    return sorted(
        {
            e["season"]
            for e in detail["episodes"]
            if e["season"] is not None and episode_is_visible(ctx, e, batch)
        }
    )


def playable_asset(ctx, kind, identity, source_id=None, batch=None):
    if batch is not None:
        return batch.playable(kind, identity, source_id)
    asset_id = None
    if source_id and source_id != object_id(kind, identity):
        source_kind, asset_id, _ = parse_object_id(source_id)
        if source_kind != "asset":
            return None
    with ctx.db.session() as db:
        statement = (
            select(LibraryAsset, MediaAsset, Download)
            .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
            .join(Download, Download.id == MediaAsset.download_id)
            .order_by(MediaAsset.id.desc())
        )
        if kind == "asset":
            statement = statement.where(MediaAsset.id == identity)
        elif kind == "episode":
            statement = statement.where(LibraryAsset.episode_id == identity)
        else:
            statement = statement.where(
                LibraryAsset.media_id == identity,
                LibraryAsset.episode_id.is_(None),
            )
        if asset_id is not None:
            statement = statement.where(MediaAsset.id == asset_id)
        for link, asset, download in db.execute(statement):
            if kind != "asset" and jellyfin_resources.is_extra(link.part_key):
                continue
            if kind != "asset" and asset_id is None and link.part_key.startswith("part:"):
                continue
            if not link.verification.get("complete"):
                continue
            path = playable_path(download, asset.path)
            if path:
                return {
                    "asset": asset,
                    "download": download,
                    "link": link,
                    "path": path,
                }
        partial = (
            select(SubtaskAsset, Subtask, MediaAsset, Download)
            .join(Subtask, Subtask.id == SubtaskAsset.subtask_id)
            .join(MediaAsset, MediaAsset.id == SubtaskAsset.asset_id)
            .join(Download, Download.id == MediaAsset.download_id)
            .where(Subtask.status == "ready")
            .order_by(SubtaskAsset.current.desc(), SubtaskAsset.id.desc())
        )
        if kind == "asset":
            partial = partial.where(MediaAsset.id == identity)
        elif kind == "episode":
            partial = partial.where(Subtask.episode_id == identity)
        elif kind == "media":
            partial = partial.join(Task, Task.id == Subtask.task_id).where(
                Task.media_id == identity, Subtask.episode_id.is_(None)
            )
        else:
            return None
        if asset_id is not None:
            partial = partial.where(MediaAsset.id == asset_id)
        for link, subtask, asset, download in db.execute(partial):
            binding = (download.stats or {}).get("bindings", {}).get(str(subtask.id), {})
            path = playable_path(download, asset.path)
            if binding.get("buffer_ready") and path:
                return {
                    "asset": asset,
                    "download": download,
                    "link": link,
                    "path": path,
                }
    return None


def episode_is_visible(ctx, episode_data, batch=None):
    return bool(VISIBLE_DOWNLOAD_STATES.intersection(episode_data.get("statuses", []))) or bool(
        playable_asset(ctx, "episode", episode_data["id"], batch=batch)
    )


def duration_ticks(asset):
    try:
        return int(float(asset.probe.get("format", {}).get("duration", 0)) * TICKS_PER_SECOND)
    except (TypeError, ValueError):
        return 0


def integer(value):
    """Return an ffprobe value as a JSON number accepted by Jellyfin clients."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def language_rank(value, priorities):
    try:
        return priorities.index(value)
    except ValueError:
        return len(priorities) + 1


def stream_display_title(language_value, title, codec):
    normalized = language(language_value)
    parts = [language_name(normalized)] if normalized != "und" else []
    if title and title.casefold() not in {part.casefold() for part in parts}:
        parts.append(title)
    if codec:
        parts.append(codec.upper())
    return " — ".join(parts) or "Не определён"


def media_identity_for_item(ctx, item_id):
    kind, identity, _ = parse_object_id(item_id)
    if kind in {"media", "season"}:
        return identity
    if kind == "episode":
        with ctx.db.session() as db:
            episode = db.get(Episode, identity)
            season = db.get(Season, episode.season_id) if episode else None
            return season.media_id if season else None
    if kind == "asset":
        with ctx.db.session() as db:
            asset = db.get(MediaAsset, identity)
            return asset.media_id if asset else None
    return None


def playback_selection(ctx, user, item_id, batch=None):
    if batch is not None and (user is None or batch.user_id == user.id):
        return batch.playback_selection(item_id)
    media_id = media_identity_for_item(ctx, item_id)
    if media_id is None or user is None:
        return {}
    with ctx.db.session() as db:
        row = db.get(ConfigEntry, f"jellyfin.selection.{user.id}.{media_id}")
        return dict(row.value) if row else {}


def stream_selector(stream, streams):
    signature = {
        "language": stream.get("Language"),
        "title": stream.get("Title"),
        "codec": stream.get("Codec"),
        "external": bool(stream.get("IsExternal")),
        "forced": bool(stream.get("IsForced")),
    }
    matching = [candidate for candidate in streams if candidate.get("Type") == stream.get("Type")]
    signature["ordinal"] = matching.index(stream)
    return signature


def selected_stream(streams, stream_type, selector):
    if not selector:
        return None
    candidates = [stream for stream in streams if stream["Type"] == stream_type]
    exact = [
        stream
        for stream in candidates
        if stream.get("Language") == selector.get("language")
        and stream.get("Title") == selector.get("title")
        and stream.get("Codec") == selector.get("codec")
        and bool(stream.get("IsExternal")) == selector.get("external")
        and bool(stream.get("IsForced")) == selector.get("forced")
    ]
    if exact:
        return exact[0]
    language_matches = [stream for stream in candidates if stream.get("Language") == selector.get("language")]
    if language_matches:
        return language_matches[0]
    ordinal = selector.get("ordinal")
    return candidates[ordinal] if isinstance(ordinal, int) and 0 <= ordinal < len(candidates) else None


def media_streams(
    ctx,
    playable,
    item_id,
    media_source_id,
    user=None,
    play_session_id=None,
    subtitle_compatibility=False,
    batch=None,
):
    asset, link, download = playable["asset"], playable["link"], playable["download"]
    settings = batch.settings() if batch else ctx.service.settings()
    preferences = settings.jellyfin
    configuration = user_configuration(ctx, user, batch) if user else {}
    audio_priorities = list(
        dict.fromkeys(
            [
                jellyfin_language(configuration.get("AudioLanguagePreference")),
                *[jellyfin_language(value) for value in preferences.audio_languages],
            ]
        )
    )
    subtitle_priorities = list(
        dict.fromkeys(
            [
                jellyfin_language(configuration.get("SubtitleLanguagePreference")),
                *[jellyfin_language(value) for value in preferences.subtitle_languages],
            ]
        )
    )
    internal_streams = []
    for raw in asset.probe.get("streams", []):
        stream_type = raw.get("codec_type")
        if stream_type not in {"video", "audio", "subtitle"}:
            continue
        tags = raw.get("tags", {})
        stream_language = tags.get("language", "und")
        if stream_type == "subtitle" and language(stream_language) == "und" and raw.get("index") is not None:
            stream_language = raw.get("detected_language") or detect_subtitle_language(
                playable["path"], raw.get("index"), raw.get("codec_name")
            )
        entry = {
            "Codec": raw.get("codec_name"),
            "Language": jellyfin_language(stream_language),
            "Title": tags.get("title"),
            "DisplayTitle": stream_display_title(stream_language, tags.get("title"), raw.get("codec_name")),
            "Type": stream_type.capitalize(),
            "Index": (integer(raw.get("index")) if raw.get("index") is not None else len(internal_streams)),
            "IsExternal": False,
            "IsDefault": bool(raw.get("disposition", {}).get("default")),
            "IsForced": bool(raw.get("disposition", {}).get("forced")),
            "Width": integer(raw.get("width")),
            "Height": integer(raw.get("height")),
            "Channels": integer(raw.get("channels")),
            "ChannelLayout": raw.get("channel_layout"),
            "SampleRate": integer(raw.get("sample_rate")),
            "BitRate": integer(raw.get("bit_rate")),
        }
        internal_streams.append(entry)
    external_tracks = list((link.preflight.get("binding") or {}).get("tracks", []))
    external_tracks.extend(asset.tracks or [])
    external_tracks = classify_external_subtitles(
        [dict(track) for track in external_tracks], download.plan.get("files", [])
    )
    seen_external = set()
    external_streams = []
    for track in external_tracks:
        relative = track.get("path")
        # A direct HTTP video response cannot combine a separate audio file.
        # External subtitles are independent resources supported by Jellyfin clients.
        if track.get("kind") != "subtitle" or not relative:
            continue
        signature = relative
        if signature in seen_external:
            continue
        seen_external.add(signature)
        path = playable_path(download, relative)
        if not path:
            continue
        track_language = track.get("language", "und")
        if language(track_language) == "und":
            track_language = detect_subtitle_language(path)
        source_suffix = path.suffix.lower().lstrip(".")
        suffix = "srt" if subtitle_compatibility and source_suffix in {"ass", "ssa"} else source_suffix
        entry = {
            "Codec": suffix,
            "Language": jellyfin_language(track_language),
            "Title": track.get("title"),
            "DisplayTitle": stream_display_title(track_language, track.get("title"), suffix),
            "Type": track["kind"].capitalize(),
            "Index": len(external_streams),
            "IsExternal": True,
            "IsDefault": False,
            "IsForced": bool(track.get("forced")),
            "Path": str(path),
        }
        entry.update(
            {
                "DeliveryMethod": "External",
                "IsExternalUrl": False,
                "IsTextSubtitleStream": suffix in {"srt", "ass", "ssa", "vtt"},
                "SupportsExternalStream": True,
            }
        )
        external_streams.append(entry)

    # Swiftfin 1.6 expects Jellyfin sidecars to occupy the first public
    # stream indexes and offsets embedded container indexes by their count.
    # Current Swiftfin maps tracks by media type and accepts the same layout.
    external_count = len(external_streams)
    for stream in internal_streams:
        stream["Index"] += external_count
        if stream["Type"] == "Subtitle":
            text_subtitle = stream["Codec"] in jellyfin_resources.TEXT_CODECS
            stream.update(
                IsTextSubtitleStream=text_subtitle,
                SupportsExternalStream=text_subtitle or stream["Codec"] == "hdmv_pgs_subtitle",
            )
            if stream["SupportsExternalStream"]:
                stream.update(DeliveryMethod="External", IsExternalUrl=False)
    for stream in external_streams + internal_streams:
        if stream["Type"] != "Subtitle" or not stream.get("SupportsExternalStream"):
            continue
        index = stream["Index"]
        suffix = (
            stream["Codec"]
            if stream["IsExternal"]
            else ("ass" if stream["Codec"] in {"ass", "ssa"} and not subtitle_compatibility else "srt")
        )
        if stream["Codec"] == "hdmv_pgs_subtitle":
            suffix = "sup"
        delivery_url = f"/Videos/{item_id}/{media_source_id}/Subtitles/{index}/0/Stream.{suffix}"
        if play_session_id:
            delivery_url += f"?playSessionId={play_session_id}"
        stream["DeliveryUrl"] = delivery_url
    streams = external_streams + internal_streams
    for path_value, info in asset.probe.get("external_audio", {}).items():
        path = playable_path(download, path_value)
        if not path or info.get("stamp") != f"{path.stat().st_mtime_ns}:{path.stat().st_size}":
            continue
        raw = info["stream"]
        streams.append(
            {
                "Type": "Audio",
                "Index": max((s["Index"] for s in streams), default=-1) + 1,
                "Codec": raw.get("codec_name"),
                "Language": jellyfin_language(info["language"]),
                "Title": info.get("title"),
                "DisplayTitle": stream_display_title(
                    info["language"], info.get("title"), raw.get("codec_name")
                ),
                "IsExternal": True,
                "IsDefault": False,
                "IsForced": False,
                "Path": str(path),
                "Channels": integer(raw.get("channels")),
                "SampleRate": integer(raw.get("sample_rate")),
            }
        )
    audio = sorted(
        (s for s in streams if s["Type"] == "Audio"),
        key=lambda s: (
            language_rank(s["Language"], audio_priorities),
            s["Index"],
        ),
    )
    subtitles = sorted(
        (s for s in streams if s["Type"] == "Subtitle"),
        key=lambda s: (
            language_rank(s["Language"], subtitle_priorities),
            bool(s["IsForced"]) if settings.prefer_full_subtitles else False,
            bool(s["IsExternal"]),
            s["Index"],
        ),
    )
    for stream in streams:
        if stream["Type"] in {"Audio", "Subtitle"}:
            stream["IsDefault"] = False
    if audio:
        audio[0]["IsDefault"] = True
    subtitles_enabled = configuration.get("SubtitleMode", "Default") != "None"
    subtitles_requested = any(value != "und" for value in subtitle_priorities)
    if subtitles and subtitles_requested and subtitles_enabled:
        subtitles[0]["IsDefault"] = True
    return (
        streams,
        audio[0]["Index"] if audio else None,
        subtitles[0]["Index"] if subtitles and subtitles_requested and subtitles_enabled else None,
    )


def media_source(
    ctx,
    playable,
    item_id,
    user=None,
    play_session_id=None,
    subtitle_compatibility=False,
    source_id=None,
    batch=None,
):
    asset, path = playable["asset"], playable["path"]
    # Fladder requests /Videos/{MediaSource.Id}/stream. Keeping the source id
    # equal to the public item id makes that URL resolve without exposing a
    # filesystem path or requiring the client to know Lazarr's asset ids.
    source_id = source_id or item_id
    streams, audio_index, subtitle_index = media_streams(
        ctx,
        playable,
        item_id,
        source_id,
        user,
        play_session_id,
        subtitle_compatibility,
        batch,
    )
    remembered = playback_selection(ctx, user, item_id, batch)
    if "audio" in remembered:
        selected_audio = selected_stream(streams, "Audio", remembered["audio"])
        if selected_audio:
            audio_index = selected_audio["Index"]
    if "subtitle" in remembered:
        selected_subtitle = selected_stream(streams, "Subtitle", remembered["subtitle"])
        subtitle_index = selected_subtitle["Index"] if selected_subtitle else None
    for stream in streams:
        if stream["Type"] == "Audio":
            stream["IsDefault"] = stream["Index"] == audio_index
        elif stream["Type"] == "Subtitle":
            stream["IsDefault"] = stream["Index"] == subtitle_index
    duration = duration_ticks(asset)
    external_audio = any(
        s["Type"] == "Audio" and s["Index"] == audio_index and s["IsExternal"] for s in streams
    )
    return {
        "Protocol": "File",
        "Id": source_id,
        "Path": str(path),
        "Type": "Default",
        "Container": "mkv" if external_audio else path.suffix.lower().lstrip("."),
        "Size": path.stat().st_size,
        "Name": path.name,
        "IsRemote": False,
        "ETag": f"{path.stat().st_mtime_ns:x}-{path.stat().st_size:x}",
        "RunTimeTicks": duration,
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": not external_audio,
        "SupportsProbing": False,
        "VideoType": "VideoFile",
        "MediaStreams": streams,
        "MediaAttachments": jellyfin_resources.attachments(playable, item_id, source_id, play_session_id),
        "DefaultAudioStreamIndex": audio_index,
        "DefaultSubtitleStreamIndex": subtitle_index,
        "RequiredHttpHeaders": {},
        "DirectStreamUrl": f"/Videos/{item_id}/stream{'.mkv' if external_audio else ''}?Static=true&mediaSourceId={source_id}"
        + (f"&audioStreamIndex={audio_index}" if external_audio else ""),
    }


def playable_item_fields(playable, item_id):
    path, asset = playable["path"], playable["asset"]
    return {
        **jellyfin_resources.resource_fields(playable, item_id),
        "DateCreated": datetime.fromtimestamp(playable["link"].created_at, timezone.utc).isoformat()
        if isinstance(playable["link"], LibraryAsset)
        else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "CanDownload": True,
        "PlayAccess": "Full",
        "Container": path.suffix.lower().lstrip("."),
        "RunTimeTicks": duration_ticks(asset),
        "VideoType": "VideoFile",
        "IsFolder": False,
        "Path": str(path),
        "MediaSourceCount": 1,
        "Width": integer(
            next(
                (s.get("width") for s in asset.probe.get("streams", []) if s.get("codec_type") == "video"),
                None,
            )
        ),
        "Height": integer(asset.resolution),
    }


def episode_dto(ctx, media, episode_data, user=None, *, include_missing=False, batch=None):
    playable = playable_asset(ctx, "episode", episode_data["id"], batch=batch)
    if (
        not include_missing
        and not playable
        and not VISIBLE_DOWNLOAD_STATES.intersection(episode_data.get("statuses", []))
    ):
        return None
    season_number = episode_data["season"]
    still = episode_data.get("still")
    still_tag = image_tag(still)
    episode_number = episode_data["episode"]
    title = episode_data["title"] or f"Серия {episode_number}"
    backdrop = backdrop_tag(media)
    result = {
        **jellyfin_catalog.metadata_fields(ctx, media, batch),
        "Name": title,
        "SortName": title.casefold(),
        "ServerId": server_id(ctx),
        "Id": object_id("episode", episode_data["id"]),
        "SeriesName": media.title,
        "SeriesId": object_id("media", media.id),
        "SeasonName": f"Сезон {season_number}",
        "SeasonId": object_id("season", media.id, season_number),
        "ParentId": object_id("season", media.id, season_number),
        "IndexNumber": episode_number,
        "ParentIndexNumber": season_number,
        "PremiereDate": iso_date(episode_data["air_date"]),
        "ProductionYear": int(episode_data["air_date"][:4]) if episode_data["air_date"] else None,
        "IsFolder": False,
        "Type": "Episode",
        "MediaType": "Video",
        "LocationType": "FileSystem",
        "Overview": episode_data.get("overview") or "",
        "ImageTags": {"Primary": still_tag} if still_tag else {},
        "PrimaryImageAspectRatio": 16 / 9 if still_tag else None,
        "SeriesPrimaryImageTag": poster_tag(media),
        "ParentBackdropItemId": object_id("media", media.id),
        "ParentBackdropImageTags": [backdrop] if backdrop else [],
        "CanDownload": False,
        "PlayAccess": "None",
        "MediaSourceCount": 0,
        "MediaSources": [],
        "MediaStreams": [],
        "HasSubtitles": False,
        "UserData": user_data(ctx, user, object_id("episode", episode_data["id"]), batch=batch),
    }
    if playable:
        result.update(playable_item_fields(playable, result["Id"]))
        source = media_source(ctx, playable, result["Id"], user, batch=batch)
        result["MediaSources"] = [source] + [
            media_source(
                ctx,
                version,
                result["Id"],
                user,
                source_id=object_id("asset", version["asset"].id),
                batch=batch,
            )
            for version in playback_versions(ctx, result["Id"], playable, batch)[1:]
        ]
        result["MediaSourceCount"] = len(result["MediaSources"])
        result["MediaStreams"] = source["MediaStreams"]
        result["HasSubtitles"] = any(s["Type"] == "Subtitle" for s in source["MediaStreams"])
        result["UserData"] = user_data(ctx, user, result["Id"], result["RunTimeTicks"], batch)
    return result


def item_dto(ctx, item_id, user=None, batch=None):
    try:
        item_id = str(uuid.UUID(item_id))
    except ValueError as exc:
        raise HTTPException(404, "Item not found") from exc
    if item_id in library_ids(ctx).values():
        key = next(key for key, value in library_ids(ctx).items() if value == item_id)
        return library_dto(ctx, key, dict(LIBRARIES)[key], user, batch)
    if uuid.UUID(item_id).version == 5:
        with ctx.db.session() as db:
            rows = [media_dto(ctx, m, user, batch) for m in db.scalars(select(Media))]
        for kind in ("Genre", "Person", "Studio", "Year", "BoxSet"):
            match = next((i for i in jellyfin_catalog.entities(rows, kind) if i["Id"] == item_id), None)
            if match:
                match["UserData"] = user_data(ctx, user, match["Id"], batch=batch)
                return match
    kind, identity, secondary = parse_object_id(item_id)
    if kind == "playlist":
        return jellyfin_playlists.playlist_item(ctx, item_id, user)
    if kind == "asset":
        playable = playable_asset(ctx, kind, identity, batch=batch)
        if not playable:
            raise HTTPException(404, "Media source not found")
        link = playable["link"]
        if isinstance(link, LibraryAsset):
            parent = (
                object_id("episode", link.episode_id)
                if link.episode_id
                else object_id("media", link.media_id)
            )
        else:
            with ctx.db.session() as db:
                task = db.get(Subtask, link.subtask_id)
                parent = (
                    object_id("episode", task.episode_id)
                    if task.episode_id
                    else object_id("media", playable["asset"].media_id)
                )
        result = item_dto(ctx, parent, user, batch)
        result.update(playable_item_fields(playable, item_id))
        result["Id"] = item_id
        result["MediaSources"] = [media_source(ctx, playable, item_id, user, batch=batch)]
        result["MediaStreams"] = result["MediaSources"][0]["MediaStreams"]
        result["UserData"] = user_data(ctx, user, item_id, result.get("RunTimeTicks", 0), batch)
        if isinstance(link, LibraryAsset) and jellyfin_resources.is_extra(link.part_key):
            result["Type"] = "Trailer" if link.part_key.startswith("trailer:") else "Video"
            result["MediaType"] = "Video"
            result["Name"] = playable["path"].stem
            result["ParentId"] = parent
        return result
    with ctx.db.session() as db:
        if kind == "media":
            media = db.get(Media, identity)
            if media:
                return media_dto(ctx, media, user, batch)
        elif kind == "season":
            media = db.get(Media, identity)
            if media:
                return season_dto(ctx, media, secondary, user, batch)
        elif kind == "episode":
            episode = db.get(Episode, identity)
            season = db.get(Season, episode.season_id) if episode else None
            media = db.get(Media, season.media_id) if season else None
            if media:
                detail = batch.detail(media.id) if batch else ctx.library.detail(media.id)
                data = next((e for e in detail["episodes"] if e["id"] == identity), None)
                if data:
                    item = episode_dto(ctx, media, data, user, include_missing=True, batch=batch)
                    if item:
                        return item
    raise HTTPException(404, "Item not found")


def progress_targets(ctx, item_id):
    kind, identity, secondary = parse_object_id(item_id)
    if kind == "asset":
        playable = playback_for_item(ctx, item_id)
        if playable:
            link = playable["link"]
            if isinstance(link, LibraryAsset):
                if jellyfin_resources.is_extra(link.part_key):
                    return [item_id], False
                parent = (
                    object_id("episode", link.episode_id)
                    if link.episode_id
                    else object_id("media", link.media_id)
                )
                return [item_id, parent], False
            return [item_id], False
    with ctx.db.session() as db:
        if kind == "media":
            media = db.get(Media, identity)
            if not media:
                raise HTTPException(404, "Item not found")
            if media.kind != "tv":
                return [item_id], False
            targets = visible_episode_item_ids(ctx, media.id)
            if not targets:
                raise HTTPException(404, "No visible episodes found")
            return targets, True
        if kind == "season":
            media = db.get(Media, identity)
            if not media or media.kind != "tv":
                raise HTTPException(404, "Season not found")
            targets = visible_episode_item_ids(ctx, media.id, secondary)
            if not targets:
                raise HTTPException(404, "Season not found")
            return targets, True
        if kind == "episode":
            episode = db.get(Episode, identity)
            season = db.get(Season, episode.season_id) if episode else None
            media = db.get(Media, season.media_id) if season else None
            if media:
                data = next(
                    (item for item in ctx.library.detail(media.id)["episodes"] if item["id"] == identity),
                    None,
                )
                if data and episode_is_visible(ctx, data):
                    return [item_id], False
    raise HTTPException(404, "Item not found")


def progress_user_data(ctx, user, item_id, runtime_ticks=0):
    kind, identity, secondary = parse_object_id(item_id)
    if kind == "season":
        return grouped_user_data(ctx, user, item_id, visible_episode_item_ids(ctx, identity, secondary))
    if kind == "media":
        with ctx.db.session() as db:
            media = db.get(Media, identity)
        if media and media.kind == "tv":
            return grouped_user_data(ctx, user, item_id, visible_episode_item_ids(ctx, identity))
    return user_data(ctx, user, item_id, runtime_ticks)


def save_progress(ctx, user, item_id, position_ticks=None, played=None, touch=True, date_played=None):
    item_id = item_dto(ctx, item_id, user)["Id"]
    kind, identity, _ = parse_object_id(item_id)
    targets, grouped = progress_targets(ctx, item_id)
    if grouped and played is None:
        raise HTTPException(400, "Folder playback progress requires Played")
    playable = (
        playable_asset(ctx, kind, identity) if not grouped and kind in {"media", "episode", "asset"} else None
    )
    runtime = duration_ticks(playable["asset"]) if playable else 0
    position = max(0, int(position_ticks or 0)) if position_ticks is not None else None
    completed = played
    if completed is None and position is not None and runtime:
        completed = position >= runtime * 0.9
    now = time.time()
    with ctx.db.session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows = {
            row.item_id: row
            for row in db.scalars(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id,
                    PlaybackProgress.item_id.in_(targets),
                )
            )
        }
        for target in targets:
            row = rows.get(target)
            if row is None:
                row = PlaybackProgress(user_id=user.id, item_id=target)
                db.add(row)
            was_played = row.played
            if completed is True:
                row.position_ticks = 0
            elif position is not None:
                row.position_ticks = position
            if completed is not None:
                row.played = bool(completed)
                if completed and not was_played:
                    row.play_count = (row.play_count or 0) + 1
                if completed is False and not touch:
                    row.play_count = 0
                    row.last_played_at = None
            if touch:
                row.last_played_at = date_played.timestamp() if date_played else now
            row.updated_at = now
        if played is not None:
            history = list(
                db.scalars(
                    select(PlaybackSession).where(
                        PlaybackSession.user_id == user.id, PlaybackSession.item_id.in_(targets)
                    )
                )
            )
            sources = {h.source_id for h in history}
            for version in db.scalars(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id, PlaybackProgress.item_id.in_(sources)
                )
            ):
                version.played = played
                version.position_ticks = 0
                if not played:
                    version.play_count = 0
                    version.last_played_at = None
            for session in history:
                if session.stopped_at is None:
                    session.stopped_at = now
    return progress_user_data(ctx, user, item_id, runtime)


def save_playback_selection(ctx, user, item_id, payload):
    fields = {
        "audio": ("AudioStreamIndex", "audioStreamIndex"),
        "subtitle": ("SubtitleStreamIndex", "subtitleStreamIndex"),
    }
    supplied = {
        kind: next((payload[key] for key in keys if key in payload), None)
        for kind, keys in fields.items()
        if any(key in payload for key in keys)
    }
    if not supplied:
        return
    media_id = media_identity_for_item(ctx, item_id)
    playable = playback_for_item(ctx, item_id)
    if media_id is None or not playable:
        return
    streams = media_source(ctx, playable, item_id, user)["MediaStreams"]
    key = f"jellyfin.selection.{user.id}.{media_id}"
    with ctx.db.session() as db:
        row = db.get(ConfigEntry, key)
        value = dict(row.value) if row else {}
        for kind, raw_index in supplied.items():
            try:
                index = int(raw_index) if raw_index is not None else -1
            except (TypeError, ValueError):
                continue
            stream_type = kind.capitalize()
            stream = next(
                (
                    candidate
                    for candidate in streams
                    if candidate["Type"] == stream_type and candidate["Index"] == index
                ),
                None,
            )
            if stream:
                value[kind] = stream_selector(stream, streams)
            elif kind == "subtitle" and index == -1:
                value[kind] = None
        if row:
            row.value = value
        else:
            db.add(ConfigEntry(key=key, value=value))


def playback_for_item(ctx, item_id, source_id=None):
    kind, identity, _ = parse_object_id(item_id)
    return playable_asset(ctx, kind, identity, source_id) if kind in {"media", "episode", "asset"} else None


def playback_versions(ctx, item_id, current, batch=None):
    """Expose verified versions while preserving the existing single-source URL."""
    if batch is not None:
        return batch.versions(item_id, current)
    kind, identity, _ = parse_object_id(item_id)
    if kind == "asset":
        return [current]
    with ctx.db.session() as db:
        query = select(LibraryAsset.asset_id).where(LibraryAsset.media_id == current["asset"].media_id)
        if kind == "episode":
            query = query.where(LibraryAsset.episode_id == identity)
        else:
            query = query.where(LibraryAsset.episode_id.is_(None))
        if isinstance(current["link"], LibraryAsset):
            query = query.where(LibraryAsset.part_key == current["link"].part_key)
        candidates = list(
            db.scalars(
                query.where(
                    ~LibraryAsset.part_key.regexp_match(
                        "^(trailer|extra|special|behindthescenes|deleted|featurette|intro|theme):"
                    )
                ).order_by(LibraryAsset.asset_id.desc())
            )
        )
    result = [current]
    for asset_id in dict.fromkeys(candidates):
        if asset_id == current["asset"].id:
            continue
        version = playback_for_item(ctx, item_id, object_id("asset", asset_id))
        if version:
            result.append(version)
    return result


def token_from(request):
    token = request.headers.get("X-Emby-Token") or request.headers.get("X-MediaBrowser-Token")
    if not token:
        header = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization", "")
        match = re.search(r'(?:Token|token)=["\']?([^"\',\s]+)', header)
        token = match.group(1) if match else None
    return token or request.query_params.get("api_key")


def install_jellyfin_api(app, context):
    play_sessions = {}

    def user_for_token(ctx, token):
        with ctx.db.session() as db:
            session = db.get(LoginSession, hash_token(token or ""))
            user = db.get(User, session.user_id) if session and session.expires_at > time.time() else None
            return user if user and user.active else None

    def authenticated(request: Request):
        user = user_for_token(context(request), token_from(request))
        if not user:
            raise HTTPException(401, "Invalid authentication token")
        return user

    def require_user_id(user_id, user):
        kind, identity, _ = parse_object_id(user_id)
        if kind != "user" or identity != user.id:
            raise HTTPException(403, "User does not match token")

    def check_requested_user(request, user):
        requested = parameter(request.query_params, "userId")
        if requested:
            require_user_id(requested, user)

    jellyfin_playlists.install_playlist_api(app, context, authenticated, require_user_id)

    def system_info(request):
        ctx = context(request)
        return {
            "LocalAddress": str(request.base_url).rstrip("/"),
            "ServerName": "Lazarr",
            "Version": API_VERSION,
            "ProductName": "Lazarr",
            "OperatingSystem": "Linux",
            "Id": server_id(ctx),
            "StartupWizardCompleted": True,
        }

    @app.get("/System/Info/Public")
    async def jellyfin_public_info(request: Request):
        return system_info(request)

    @app.get("/System/Info")
    async def jellyfin_system_info(request: Request, user=Depends(authenticated)):
        return {**system_info(request), "HasPendingRestart": False, "IsShuttingDown": False}

    @app.get("/QuickConnect/Enabled")
    async def jellyfin_quick_connect():
        return False

    @app.get("/Branding/Configuration")
    async def jellyfin_branding_configuration():
        return {"LoginDisclaimer": "", "CustomCss": "", "SplashscreenEnabled": False}

    @app.post("/Users/AuthenticateByName")
    async def jellyfin_login(payload: dict, request: Request):
        ctx = context(request)
        username = str(payload.get("Username") or "").strip()
        password = str(payload.get("Pw") or "")
        client = request.client.host if request.client else "unknown"
        attempts = ctx.login_attempts["jellyfin:" + client] = [
            value for value in ctx.login_attempts["jellyfin:" + client] if value > time.time() - 300
        ]
        if len(attempts) >= 10:
            raise HTTPException(429, "Too many attempts")
        with ctx.db.session() as db:
            user = db.scalar(select(User).where(User.username == username))
            valid = await asyncio.to_thread(verify_password, user.password_hash if user else "", password)
            if not user or not user.active or not valid:
                attempts.append(time.time())
                raise HTTPException(401, "Invalid username or password")
            token, _ = new_session(db, user, ttl=86400 * 365)
            db.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
            audit(db, user.id, "jellyfin.login", str(user.id))
            result_user = user_dto(ctx, user)
        attempts.clear()
        return {
            "User": result_user,
            "SessionInfo": {
                "UserId": result_user["Id"],
                "UserName": result_user["Name"],
                "Client": request.headers.get("X-Emby-Authorization", "Jellyfin client"),
            },
            "AccessToken": token,
            "ServerId": server_id(ctx),
        }

    @app.get("/Users/Me")
    async def jellyfin_me(request: Request, user=Depends(authenticated)):
        return user_dto(context(request), user)

    @app.get("/Users/Public")
    async def jellyfin_public_users():
        # Do not disclose Lazarr account names; clients still offer manual login.
        return []

    @app.post("/Sessions/Logout", status_code=204)
    async def jellyfin_logout(request: Request, user=Depends(authenticated)):
        with context(request).db.session() as db:
            db.execute(delete(LoginSession).where(LoginSession.token_hash == hash_token(token_from(request))))
        return Response(status_code=204)

    @app.get("/Users/{user_id}")
    async def jellyfin_user(user_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return user_dto(context(request), user)

    @app.post("/Users/Configuration", status_code=204)
    async def jellyfin_update_user_configuration(
        payload: dict,
        request: Request,
        userId: str | None = None,
        user=Depends(authenticated),
    ):
        if userId:
            require_user_id(userId, user)
        allowed = {
            "AudioLanguagePreference",
            "CastReceiverId",
            "EnableLocalPassword",
            "EnableNextEpisodeAutoPlay",
            "GroupedFolders",
            "DisplayCollectionsView",
            "DisplayMissingEpisodes",
            "HidePlayedInLatest",
            "PlayDefaultAudioTrack",
            "RememberAudioSelections",
            "RememberSubtitleSelections",
            "LatestItemsExcludes",
            "MyMediaExcludes",
            "OrderedViews",
            "SubtitleLanguagePreference",
            "SubtitleMode",
        }
        ctx = context(request)
        key = f"jellyfin.user.{user.id}"
        with ctx.db.session() as db:
            row = db.get(ConfigEntry, key)
            value = dict(row.value) if row else {}
            value.update({name: setting for name, setting in payload.items() if name in allowed})
            if row:
                row.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
        return Response(status_code=204)

    @app.get("/UserViews")
    async def jellyfin_views(request: Request, userId: str | None = None, user=Depends(authenticated)):
        if userId:
            require_user_id(userId, user)
        ctx = context(request)
        config = user_configuration(ctx, user)
        items = [library_dto(ctx, key, name, user) for key, name in LIBRARIES]
        items = [item for item in items if item["Id"] not in config.get("MyMediaExcludes", [])]
        order = config.get("OrderedViews", [])
        items.sort(key=lambda item: order.index(item["Id"]) if item["Id"] in order else len(order))
        return query_result(items)

    @app.get("/UserViews/GroupingOptions")
    async def jellyfin_grouping_options(userId: str | None = None, user=Depends(authenticated)):
        if userId:
            require_user_id(userId, user)
        return []

    @app.get("/Plugins")
    async def jellyfin_plugins(user=Depends(authenticated)):
        # Infuse probes installed plugins while checking a Jellyfin connection.
        # An empty list selects its regular full-sync path; InfuseSync is optional.
        return []

    @app.get("/DisplayPreferences/{display_preferences_id}")
    async def jellyfin_display_preferences(
        display_preferences_id: str,
        request: Request,
        client: str,
        userId: str | None = None,
        user=Depends(authenticated),
    ):
        if userId:
            require_user_id(userId, user)
        return jellyfin_state.preferences(context(request), user, display_preferences_id, client)

    @app.post("/DisplayPreferences/{display_preferences_id}", status_code=204)
    async def jellyfin_save_display_preferences(
        display_preferences_id: str,
        payload: DisplayPreferences,
        request: Request,
        client: str,
        user=Depends(authenticated),
    ):
        check_requested_user(request, user)
        jellyfin_state.preferences(context(request), user, display_preferences_id, client, payload)
        return Response(status_code=204)

    @app.get("/Library/MediaFolders")
    async def jellyfin_media_folders(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        return query_result([library_dto(ctx, key, name) for key, name in LIBRARIES])

    @app.get("/Library/VirtualFolders")
    async def jellyfin_virtual_folders(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        ids = library_ids(ctx)
        return [
            {
                "Name": name,
                "Locations": [],
                "CollectionType": LIBRARY_COLLECTIONS[key],
                "ItemId": ids[key],
                "LibraryOptions": {"EnableRealtimeMonitor": False},
            }
            for key, name in LIBRARIES
        ]

    def items_response(
        ctx,
        user,
        parent_id=None,
        search_term=None,
        include_types=None,
        recursive=False,
        include_missing=False,
        batch=None,
    ):
        batch = batch or JellyfinDtoBatch(ctx, user)
        allowed = {value.strip() for value in include_types.split(",")} if include_types else set()
        ids = library_ids(ctx)
        if parent_id == str(uuid.UUID(int=0)):
            parent_id = None
        if parent_id:
            try:
                parent_id = str(uuid.UUID(parent_id))
            except ValueError as exc:
                raise HTTPException(404, "Parent not found") from exc
        if parent_id and parent_id not in ids.values() and uuid.UUID(parent_id).version == 5:
            entity = item_dto(ctx, parent_id, user, batch)
            key = {"Genre": "genreIds", "Studio": "studioIds", "Person": "personIds", "Year": "years"}.get(
                entity["Type"]
            )
            items = (
                filter_items(
                    items_response(ctx, user, include_types=include_types, recursive=True, batch=batch),
                    {key: entity["Name"] if key == "years" else entity["Id"]},
                )
                if key
                else []
            )
            if entity["Type"] == "BoxSet":
                items = [
                    i
                    for i in items_response(
                        ctx, user, include_types=include_types, recursive=True, batch=batch
                    )
                    if i.get("CollectionName") == entity["Name"]
                ]
        elif parent_id and parent_id not in ids.values() and parse_object_id(parent_id)[0] == "playlist":
            items = jellyfin_playlists.contents(ctx, parent_id, user)
        elif not parent_id and (recursive or include_types):
            items = []
            for library_id in ids.values():
                items.extend(
                    items_response(
                        ctx,
                        user,
                        library_id,
                        include_types=include_types,
                        recursive=recursive,
                        include_missing=include_missing,
                        batch=batch,
                    )
                )
            if not allowed or "Playlist" in allowed:
                items.extend(jellyfin_playlists.visible(ctx, user))
        elif not parent_id:
            items = [library_dto(ctx, key, name, user, batch) for key, name in LIBRARIES]
            if not allowed or "Playlist" in allowed:
                items.extend(jellyfin_playlists.visible(ctx, user))
        elif parent_id in ids.values():
            key = next(key for key, value in ids.items() if value == parent_id)
            with ctx.db.session() as db:
                rows = [
                    media
                    for media in db.scalars(select(Media).order_by(Media.title, Media.id))
                    if library_kind(media) == key
                ]
            items = [
                media_dto(ctx, media, user, batch)
                for media in rows
                if not allowed
                or (media.kind == "movie" and "Movie" in allowed)
                or (media.kind == "tv" and "Series" in allowed)
            ]
            if recursive:
                if not allowed or "BoxSet" in allowed:
                    entity_source = items or [media_dto(ctx, media, user, batch) for media in rows]
                    items.extend(jellyfin_catalog.entities(entity_source, "BoxSet"))
                for media in rows:
                    if media.kind == "tv":
                        if not allowed or "Season" in allowed:
                            items.extend(
                                season_dto(ctx, media, number, user, batch)
                                for number in available_seasons(ctx, media, batch)
                            )
                        if allowed and "Episode" not in allowed:
                            continue
                        items.extend(
                            items_response(
                                ctx,
                                user,
                                object_id("media", media.id),
                                include_types="Episode" if allowed else None,
                                recursive=True,
                                include_missing=include_missing,
                                batch=batch,
                            )
                        )
        else:
            kind, identity, secondary = parse_object_id(parent_id)
            with ctx.db.session() as db:
                media = db.get(Media, identity) if kind in {"media", "season"} else None
            if not media:
                raise HTTPException(404, "Item not found")
            detail = batch.detail(media.id)
            if kind == "media" and media.kind == "tv":
                if recursive or "Episode" in allowed:
                    items = [
                        item
                        for e in detail["episodes"]
                        for item in [
                            episode_dto(
                                ctx,
                                media,
                                e,
                                user,
                                include_missing=include_missing,
                                batch=batch,
                            )
                        ]
                        if item
                    ]
                elif not allowed or "Season" in allowed:
                    items = [
                        season_dto(ctx, media, number, user, batch)
                        for number in available_seasons(ctx, media, batch)
                    ]
                else:
                    items = []
            elif kind == "season":
                items = (
                    [
                        item
                        for episode in detail["episodes"]
                        if episode["season"] == secondary
                        for item in [
                            episode_dto(
                                ctx,
                                media,
                                episode,
                                user,
                                include_missing=include_missing,
                                batch=batch,
                            )
                        ]
                        if item
                    ]
                    if not allowed or "Episode" in allowed
                    else []
                )
            else:
                items = []
        if search_term:
            items = [item for item in items if search_term.casefold() in item["Name"].casefold()]
        if allowed:
            items = [item for item in items if item.get("Type") in allowed]
        return items

    async def jellyfin_items_impl(request, user, parent_id=None):
        ctx = context(request)
        await ctx.library.enrich()
        params = request.query_params
        requested_user = params.get("userId") or params.get("UserId")
        if requested_user:
            require_user_id(requested_user, user)
        batch = JellyfinDtoBatch(ctx, user)
        items = items_response(
            ctx,
            user,
            parent_id or params.get("parentId") or params.get("ParentId"),
            params.get("searchTerm") or params.get("SearchTerm"),
            ",".join(csv_parameter(params, "includeItemTypes")) or None,
            (params.get("recursive") or params.get("Recursive") or "false").lower() == "true",
            include_missing=bool_parameter(params, "isMissing") or bool_parameter(params, "isUnaired"),
            batch=batch,
        )
        requested_ids = csv_parameter(params, "ids")
        if requested_ids and not (parent_id or parameter(params, "parentId")):
            items = [item_dto(ctx, identity, user, batch) for identity in requested_ids]
        return page(filter_items(items, params), params)

    jellyfin_catalog.install(app, context, authenticated, check_requested_user, items_response)

    @app.get("/Items/")
    @app.get("/Items")
    async def jellyfin_items(request: Request, user=Depends(authenticated)):
        return await jellyfin_items_impl(request, user)

    @app.get("/Users/{user_id}/Items")
    async def jellyfin_legacy_items(user_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return await jellyfin_items_impl(request, user)

    @app.get("/Items/Root")
    async def jellyfin_root(request: Request, user=Depends(authenticated)):
        return {
            "Name": "Lazarr",
            "ServerId": server_id(context(request)),
            "Id": str(uuid.UUID(int=0)),
            "IsFolder": True,
            "Type": "Folder",
        }

    @app.get("/Items/Latest")
    async def jellyfin_latest(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        await ctx.library.enrich()
        params = request.query_params
        check_requested_user(request, user)
        options = dict(params)
        options.setdefault("includeItemTypes", "Movie,Episode")
        options.setdefault("sortBy", "DateCreated,SortName")
        options.setdefault("sortOrder", "Descending,Ascending")
        options.setdefault("limit", "20")
        batch = JellyfinDtoBatch(ctx, user)
        grouped = bool_parameter(params, "groupItems", True)
        include_types = set(csv_parameter(options, "includeItemTypes"))
        parent_id = parameter(params, "parentId")
        ids = library_ids(ctx)
        library_key = next((key for key, value in ids.items() if value == parent_id), None)
        selection_keys = {
            key.casefold()
            for key in params
            if key.casefold()
            not in {
                "userid",
                "parentid",
                "fields",
                "enableuserdata",
                "enableimages",
                "enableimagetypes",
                "imagetypelimit",
                "limit",
                "startindex",
                "groupitems",
                "includeitemtypes",
                "enabletotalrecordcount",
            }
        }
        simple_grouped = (
            grouped
            and include_types.issubset({"Movie", "Episode"})
            and not selection_keys
            and (not parent_id or library_key is not None)
            and csv_parameter(options, "sortBy") == ["DateCreated", "SortName"]
            and csv_parameter(options, "sortOrder") == ["Descending", "Ascending"]
        )
        hide_played = user_configuration(ctx, user, batch).get("HidePlayedInLatest", False)
        if simple_grouped:
            with ctx.db.session() as db:
                rows = [
                    media
                    for media in db.scalars(select(Media))
                    if (library_key is None or library_kind(media) == library_key)
                    and (
                        (media.kind == "movie" and "Movie" in include_types)
                        or (media.kind == "tv" and "Episode" in include_types)
                    )
                ]
            rows.sort(key=lambda media: media.title.casefold())
            rows.sort(key=lambda media: batch.media_metadata(media.id)["added"] or 0, reverse=True)
            needed = number_parameter(options, "startIndex", 0) + number_parameter(options, "limit", 20)
            items = []
            for media in rows:
                count = 1
                if media.kind == "tv":
                    visible = [
                        episode
                        for episode in batch.detail(media.id)["episodes"]
                        if episode_is_visible(ctx, episode, batch)
                    ]
                    if hide_played:
                        visible = [
                            episode
                            for episode in visible
                            if not user_data(
                                ctx,
                                user,
                                object_id("episode", episode["id"]),
                                batch=batch,
                            ).get("Played")
                        ]
                    count = len(visible)
                elif hide_played and user_data(ctx, user, object_id("media", media.id), batch=batch).get(
                    "Played"
                ):
                    count = 0
                if not count:
                    continue
                item = media_dto(ctx, media, user, batch)
                item["ChildCount"] = count
                items.append(item)
                if len(items) >= needed:
                    break
            group_options = {
                key: value for key, value in options.items() if key.casefold() != "includeitemtypes"
            }
            return page(items, group_options)["Items"]

        items = items_response(
            ctx,
            user,
            parameter(params, "parentId"),
            include_types=",".join(include_types),
            recursive=True,
            batch=batch,
        )
        if hide_played:
            items = [item for item in items if not item.get("UserData", {}).get("Played")]
        items = filter_items(items, options)
        if grouped:
            groups = {}
            for item in items:
                key = item.get("SeriesId") or item["Id"]
                if key not in groups:
                    groups[key] = dict(item_dto(ctx, key, user, batch) if key != item["Id"] else item)
                    groups[key]["ChildCount"] = 0
                groups[key]["ChildCount"] += 1
            # Preserve the chronological order of each group's newest child.
            items = list(groups.values())
        return page(items, options)["Items"]

    @app.get("/Users/{user_id}/Items/Latest")
    async def jellyfin_legacy_latest(user_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return await jellyfin_latest(request, user)

    @app.get("/Shows/{series_id}/Seasons")
    async def jellyfin_seasons(series_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        await ctx.library.enrich()
        kind, identity, _ = parse_object_id(series_id)
        with ctx.db.session() as db:
            media = db.get(Media, identity) if kind == "media" else None
        if not media or media.kind != "tv":
            raise HTTPException(404, "Series not found")
        batch = JellyfinDtoBatch(ctx, user)
        numbers = available_seasons(ctx, media, batch)
        check_requested_user(request, user)
        return page(
            filter_items(
                [season_dto(ctx, media, number, user, batch) for number in numbers],
                request.query_params,
            ),
            request.query_params,
        )

    @app.get("/Shows/NextUp")
    async def jellyfin_next_up(request: Request, userId: str | None = None, user=Depends(authenticated)):
        check_requested_user(request, user)
        ctx = context(request)
        return jellyfin_state.next_up(ctx, user, request.query_params, batch=JellyfinDtoBatch(ctx, user))

    @app.get("/Shows/{series_id}/Episodes")
    async def jellyfin_episodes(series_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        kind, identity, secondary = parse_object_id(series_id)
        if kind not in {"media", "season"}:
            raise HTTPException(404, "Series not found")
        implied_season = secondary if kind == "season" else None
        with ctx.db.session() as db:
            media = db.get(Media, identity)
        if not media or media.kind != "tv":
            raise HTTPException(404, "Series not found")
        await ctx.library.enrich_media(identity)
        params = request.query_params
        season = jellyfin_state.number_parameter(params, "season", implied_season)
        season_id = params.get("seasonId") or params.get("SeasonId")
        if season_id:
            season_kind, season_media, season_number = parse_object_id(season_id)
            if (
                season_kind != "season"
                or season_media != identity
                or (implied_season is not None and season_number != implied_season)
            ):
                raise HTTPException(404, "Season not found")
            season = season_number
        batch = JellyfinDtoBatch(ctx, user)
        detail = batch.detail(identity)
        items = [
            item
            for episode in detail["episodes"]
            if season is None or episode["season"] == int(season)
            for item in [episode_dto(ctx, media, episode, user, batch=batch)]
            if item
        ]
        check_requested_user(request, user)
        return page(filter_items(items, params), params)

    @app.get("/Items/{item_id}")
    async def jellyfin_item(item_id: str, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        await ctx.library.enrich()
        try:
            version = uuid.UUID(item_id).version
        except ValueError as exc:
            raise HTTPException(404, "Item not found") from exc
        if version != 5:
            playable = playback_for_item(ctx, item_id)
            if playable:
                await jellyfin_resources.ensure_probe(ctx, playable)
            media_id = media_identity_for_item(ctx, item_id)
            if media_id is not None:
                await jellyfin_resources.discover_extras(ctx, media_id)
        check_requested_user(request, user)
        return jellyfin_catalog.project(
            item_dto(ctx, item_id, user, JellyfinDtoBatch(ctx, user)), request.query_params
        )

    @app.get("/Users/{user_id}/Items/Resume")
    async def jellyfin_legacy_resume(user_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return await jellyfin_resume_items(request, user=user)

    @app.get("/Users/{user_id}/Items/{item_id}")
    async def jellyfin_legacy_item(user_id: str, item_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return await jellyfin_item(item_id, request, user=user)

    @app.get("/UserItems/{item_id}/UserData")
    async def jellyfin_user_data(
        item_id: str, request: Request, userId: str | None = None, user=Depends(authenticated)
    ):
        if userId:
            require_user_id(userId, user)
        ctx = context(request)
        item = item_dto(ctx, item_id, user, JellyfinDtoBatch(ctx, user))
        return item["UserData"]

    @app.post("/UserItems/{item_id}/UserData")
    async def jellyfin_update_user_data(
        item_id: str,
        payload: UserDataUpdate,
        request: Request,
        userId: str | None = None,
        user=Depends(authenticated),
    ):
        if userId:
            require_user_id(userId, user)
        check_requested_user(request, user)
        return jellyfin_state.update_user_data(context(request), user, item_id, payload)

    @app.post("/UserFavoriteItems/{item_id}")
    @app.delete("/UserFavoriteItems/{item_id}")
    @app.post("/Users/{user_id}/FavoriteItems/{item_id}")
    @app.delete("/Users/{user_id}/FavoriteItems/{item_id}")
    async def jellyfin_favorite(
        item_id: str, request: Request, user_id: str | None = None, user=Depends(authenticated)
    ):
        check_requested_user(request, user)
        if user_id:
            require_user_id(user_id, user)
        return jellyfin_state.update_user_data(
            context(request), user, item_id, UserDataUpdate(IsFavorite=request.method == "POST")
        )

    @app.post("/UserItems/{item_id}/Rating")
    @app.delete("/UserItems/{item_id}/Rating")
    @app.post("/Users/{user_id}/Items/{item_id}/Rating")
    @app.delete("/Users/{user_id}/Items/{item_id}/Rating")
    async def jellyfin_rating(
        item_id: str, request: Request, user_id: str | None = None, user=Depends(authenticated)
    ):
        check_requested_user(request, user)
        if user_id:
            require_user_id(user_id, user)
        likes = (
            bool_parameter(request.query_params, "likes")
            if parameter(request.query_params, "likes") is not None and request.method == "POST"
            else None
        )
        return jellyfin_state.update_user_data(
            context(request), user, item_id, UserDataUpdate(Likes=likes), clear_likes=likes is None
        )

    @app.post("/UserPlayedItems/{item_id}")
    @app.post("/Users/{user_id}/PlayedItems/{item_id}")
    async def jellyfin_mark_played(
        item_id: str,
        request: Request,
        userId: str | None = None,
        user_id: str | None = None,
        datePlayed: datetime | None = None,
        user=Depends(authenticated),
    ):
        if user_id:
            require_user_id(user_id, user)
        if userId:
            require_user_id(userId, user)
        check_requested_user(request, user)
        return save_progress(context(request), user, item_id, played=True, date_played=datePlayed)

    @app.delete("/UserPlayedItems/{item_id}")
    @app.delete("/Users/{user_id}/PlayedItems/{item_id}")
    async def jellyfin_mark_unplayed(
        item_id: str,
        request: Request,
        userId: str | None = None,
        user_id: str | None = None,
        user=Depends(authenticated),
    ):
        check_requested_user(request, user)
        if user_id:
            require_user_id(user_id, user)
        if userId:
            require_user_id(userId, user)
        return save_progress(context(request), user, item_id, position_ticks=0, played=False, touch=False)

    @app.get("/UserItems/Resume")
    async def jellyfin_resume_items(request: Request, userId: str | None = None, user=Depends(authenticated)):
        check_requested_user(request, user)
        ctx = context(request)
        batch = JellyfinDtoBatch(ctx, user)
        with ctx.db.session() as db:
            rows = list(
                db.scalars(
                    select(PlaybackProgress)
                    .where(
                        PlaybackProgress.user_id == user.id,
                        PlaybackProgress.position_ticks > 0,
                    )
                    .order_by(PlaybackProgress.last_played_at.desc())
                )
            )
        items = []
        with ctx.db.session() as db:
            histories = list(
                db.scalars(
                    select(PlaybackSession)
                    .where(PlaybackSession.user_id == user.id)
                    .order_by(PlaybackSession.updated_at)
                )
            )
        source_parents = {h.source_id: h.item_id for h in histories}
        resumed_sources = {row.item_id for row in rows if row.item_id in source_parents}
        source_backed = {source_parents[source] for source in resumed_sources}
        parent_id = parameter(request.query_params, "parentId")
        allowed = None
        if parent_id:
            allowed = {
                item["Id"]
                for item in items_response(
                    ctx,
                    user,
                    parent_id,
                    include_types="Movie,Episode",
                    recursive=True,
                    batch=batch,
                )
            }
        active = set()
        if bool_parameter(request.query_params, "excludeActiveSessions"):
            with ctx.db.session() as db:
                active = set(
                    db.scalars(
                        select(PlaybackSession.item_id).where(
                            PlaybackSession.user_id == user.id,
                            PlaybackSession.stopped_at.is_(None),
                            PlaybackSession.updated_at > time.time() - 300,
                        )
                    )
                )
        for row in rows:
            try:
                if row.item_id in source_backed:
                    continue
                item = item_dto(ctx, row.item_id, user, batch)
                parent_item = source_parents.get(row.item_id, row.item_id)
                if (
                    item.get("MediaType") != "Video"
                    or item.get("PlayAccess") != "Full"
                    or parent_item in active
                ):
                    continue
                if allowed is not None and parent_item not in allowed:
                    continue
                if row.item_id in source_parents:
                    current = playback_for_item(ctx, parent_item)
                    if current and object_id("asset", current["asset"].id) == row.item_id:
                        item["Id"] = parent_item
                        item["UserData"] = {**item["UserData"], "ItemId": parent_item, "Key": parent_item}
                items.append(item)
            except HTTPException:
                continue
        params = request.query_params
        return page(filter_items(items, params), params)

    def playback_for(ctx, item_id, source_id=None):
        playable = playback_for_item(ctx, item_id, source_id)
        if not playable:
            raise HTTPException(404, "Playable file not found")
        return playable

    def playback_info_response(item_id, request, user, payload=None):
        source_id = parameter(request.query_params, "mediaSourceId") or (payload or {}).get("MediaSourceId")
        playable = playback_for(context(request), item_id, source_id)
        play_session_id = uuid.uuid4().hex
        versions = [playable] if source_id else playback_versions(context(request), item_id, playable)
        identities = [source_id or item_id] + [
            object_id("asset", version["asset"].id) for version in versions[1:]
        ]
        play_sessions[play_session_id] = (tuple([item_id] + identities), time.time() + 6 * 60 * 60)
        return {
            "MediaSources": [
                media_source(
                    context(request),
                    version,
                    item_id,
                    user,
                    play_session_id,
                    subtitle_compatibility=is_fladder(request),
                    source_id=identity,
                )
                for version, identity in zip(versions, identities, strict=True)
            ],
            "PlaySessionId": play_session_id,
        }

    @app.get("/Playback/BitrateTest")
    async def jellyfin_bitrate_test(size: int = 100_000, user=Depends(authenticated)):
        # Swiftfin measures this response before it asks for PlaybackInfo. Keep the
        # allocation bounded to Jellyfin clients' largest advertised test size.
        return Response(content=bytes(max(0, min(size, 10_000_000))), media_type="application/octet-stream")

    @app.get("/Items/{item_id}/PlaybackInfo")
    async def jellyfin_playback_info_get(item_id: str, request: Request, user=Depends(authenticated)):
        check_requested_user(request, user)
        await jellyfin_resources.ensure_probe(
            context(request),
            playback_for(context(request), item_id, parameter(request.query_params, "mediaSourceId")),
        )
        return playback_info_response(item_id, request, user)

    @app.post("/Items/{item_id}/PlaybackInfo")
    async def jellyfin_playback_info_post(
        item_id: str, payload: dict, request: Request, user=Depends(authenticated)
    ):
        check_requested_user(request, user)
        await jellyfin_resources.ensure_probe(
            context(request),
            playback_for(
                context(request),
                item_id,
                parameter(request.query_params, "mediaSourceId") or payload.get("MediaSourceId"),
            ),
        )
        # Jellyfin keeps the legacy query arguments for generated clients and
        # gives them precedence over the newer PlaybackInfoDto body. Fladder
        # currently sends its selected tracks through those query arguments.
        selection = dict(payload)
        for query_name, body_name in (
            ("audioStreamIndex", "AudioStreamIndex"),
            ("subtitleStreamIndex", "SubtitleStreamIndex"),
        ):
            if query_name in request.query_params:
                selection[body_name] = request.query_params[query_name]
        save_playback_selection(context(request), user, item_id, selection)
        return playback_info_response(item_id, request, user, payload)

    def authorize_playback_request(item_id, request):
        token = token_from(request)
        authenticated_user = user_for_token(context(request), token) if token else None
        play_session_id = request.query_params.get("playSessionId") or request.query_params.get(
            "PlaySessionId"
        )
        now = time.time()
        expired = [key for key, (_, expires_at) in play_sessions.items() if expires_at <= now]
        for key in expired:
            play_sessions.pop(key, None)
        session = play_sessions.get(play_session_id)
        if not authenticated_user and (not session or item_id not in session[0]):
            raise HTTPException(401, "Invalid authentication token")

    async def stream_response(item_id, request, container=None):
        authorize_playback_request(item_id, request)
        playable = playback_for(context(request), item_id, parameter(request.query_params, "mediaSourceId"))
        audio_index = jellyfin_state.number_parameter(request.query_params, "audioStreamIndex")
        if audio_index is not None:
            source = media_source(context(request), playable, item_id)
            selected = next(
                (s for s in source["MediaStreams"] if s["Type"] == "Audio" and s["Index"] == audio_index),
                None,
            )
            if selected is None:
                raise HTTPException(400, "Audio stream not found")
            if selected["IsExternal"]:
                if container and container.casefold() != "mkv":
                    raise HTTPException(415, "External audio remux requires Matroska")
                return await jellyfin_resources.remux_audio(context(request), playable, selected, request)
        path = playable["path"]
        if container and container.casefold() != path.suffix.lstrip(".").casefold():
            raise HTTPException(415, "Container conversion is disabled")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.get("/Videos/{item_id}/stream")
    @app.get("/Videos/{item_id}/stream.{container}")
    async def jellyfin_stream(item_id: str, request: Request, container: str | None = None):
        return await stream_response(item_id, request, container)

    @app.head("/Videos/{item_id}/stream", include_in_schema=False)
    @app.head("/Videos/{item_id}/stream.{container}", include_in_schema=False)
    async def jellyfin_stream_head(item_id: str, request: Request, container: str | None = None):
        return await stream_response(item_id, request, container)

    async def image_response(item_id, image_type, request, index=0):
        image_type = image_type.casefold()
        if image_type == "chapter":
            authorize_playback_request(item_id, request)
            ctx = context(request)
            path = await jellyfin_resources.chapter_image(ctx, playback_for(ctx, item_id), index)
            return FileResponse(await jellyfin_resources.resize_image(ctx, path, request.query_params))
        if index != 0:
            raise HTTPException(404, "Image not found")
        if image_type not in {"primary", "backdrop"}:
            raise HTTPException(404, "Image not found")
        ctx = context(request)
        await ctx.library.enrich()
        kind, identity, _ = parse_object_id(item_id)
        episode = None
        with ctx.db.session() as db:
            if kind == "media":
                media = db.get(Media, identity)
            elif kind == "episode":
                episode = db.get(Episode, identity)
                season = db.get(Season, episode.season_id) if episode else None
                media = db.get(Media, season.media_id) if season else None
            elif kind == "season":
                media = db.get(Media, identity)
            else:
                media = None
        image = None
        if image_type == "backdrop":
            image = media.metadata_json.get("backdrop") if media else None
        else:
            image = episode.still if kind == "episode" and episode and episode.still else None
            image = image or (media.metadata_json.get("poster") if media else None)
        filename = image.rsplit("/", 1)[-1] if image else None
        if not filename:
            raise HTTPException(404, "Image not found")
        path = await fetch_poster(ctx, filename, "w1280" if image_type == "backdrop" else None)
        return FileResponse(await jellyfin_resources.resize_image(ctx, path, request.query_params))

    @app.get("/Items/{item_id}/Images/{image_type}")
    async def jellyfin_image_get(item_id: str, image_type: str, request: Request):
        return await image_response(item_id, image_type, request)

    @app.get("/Items/{item_id}/Images/{image_type}/{index}")
    async def jellyfin_image_indexed_get(item_id: str, image_type: str, index: int, request: Request):
        return await image_response(item_id, image_type, request, index)

    @app.head("/Items/{item_id}/Images/{image_type}", include_in_schema=False)
    async def jellyfin_image_head(item_id: str, image_type: str, request: Request):
        return await image_response(item_id, image_type, request)

    @app.head("/Items/{item_id}/Images/{image_type}/{index}", include_in_schema=False)
    async def jellyfin_image_indexed_head(item_id: str, image_type: str, index: int, request: Request):
        return await image_response(item_id, image_type, request, index)

    jellyfin_resources.install(
        app, context, authenticated, check_requested_user, authorize_playback_request, playback_for
    )

    async def subtitle_response(item_id, media_source_id, index, subtitle_format, request, start=0):
        authorize_playback_request(item_id, request)
        playable = playback_for(context(request), item_id, media_source_id)
        return await jellyfin_resources.subtitle(
            context(request),
            playable,
            item_id,
            media_source_id,
            index,
            subtitle_format.casefold(),
            request,
            start,
        )

    @app.get("/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{subtitle_format}")
    async def jellyfin_subtitle_legacy(
        item_id: str,
        media_source_id: str,
        index: int,
        subtitle_format: str,
        request: Request,
    ):
        return await subtitle_response(item_id, media_source_id, index, subtitle_format, request)

    @app.get(
        "/Videos/{item_id}/{media_source_id}/Subtitles/{index}/{start_position_ticks}/Stream.{subtitle_format}"
    )
    async def jellyfin_subtitle(
        item_id: str,
        media_source_id: str,
        index: int,
        start_position_ticks: int,
        subtitle_format: str,
        request: Request,
    ):
        if start_position_ticks < 0:
            raise HTTPException(400, "Invalid subtitle start position")
        return await subtitle_response(
            item_id, media_source_id, index, subtitle_format, request, start_position_ticks
        )

    @app.websocket("/socket")
    async def jellyfin_socket(websocket: WebSocket):
        if not user_for_token(context(websocket), token_from(websocket)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_json(
            {"MessageType": "ForceKeepAlive", "MessageId": uuid.uuid4().hex, "Data": 60}
        )
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                raw = message.get("bytes") or message.get("text")
                try:
                    payload = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    continue
                if payload.get("MessageType") == "KeepAlive":
                    await websocket.send_json(
                        {
                            "MessageType": "KeepAlive",
                            "MessageId": payload.get("MessageId") or uuid.uuid4().hex,
                        }
                    )
        except WebSocketDisconnect:
            return

    @app.post("/Sessions/Capabilities", status_code=204)
    @app.post("/Sessions/Capabilities/Full", status_code=204)
    @app.post("/Sessions/Playing/Ping", status_code=204)
    async def jellyfin_session_capabilities(request: Request, user=Depends(authenticated)):
        return Response(status_code=204)

    @app.post("/Sessions/Playing", status_code=204)
    @app.post("/Sessions/Playing/Progress", status_code=204)
    @app.post("/Sessions/Playing/Stopped", status_code=204)
    async def jellyfin_session_progress(payload: dict, request: Request, user=Depends(authenticated)):
        item_id = payload.get("ItemId") or (payload.get("Item") or {}).get("Id")
        if item_id:
            event = {
                "/Sessions/Playing": "start",
                "/Sessions/Playing/Progress": "progress",
                "/Sessions/Playing/Stopped": "stop",
            }[request.url.path]
            fallback_key = "implicit:" + hash_token(token_from(request))
            jellyfin_state.record_playback(context(request), user, item_id, payload, event, fallback_key)
            save_playback_selection(context(request), user, item_id, payload)
        return Response(status_code=204)
