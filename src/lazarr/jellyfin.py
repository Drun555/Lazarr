"""Read-only Jellyfin-compatible API backed by Lazarr's verified media."""

import asyncio
import hashlib
import mimetypes
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select, delete

from lazarr.library import LIBRARIES, library_kind
from lazarr.languages import CODES, language
from lazarr.models import (
    Download,
    Episode,
    LoginSession,
    LibraryAsset,
    Media,
    MediaAsset,
    PlaybackProgress,
    Season,
    User,
)
from lazarr.posters import fetch_poster
from lazarr.security import audit, hash_token, new_session, verify_password


API_VERSION = "12.0.0"
TICKS_PER_SECOND = 10_000_000
KINDS = {"media": 1, "season": 2, "episode": 3, "asset": 4, "user": 5}
KIND_NAMES = {value: key for key, value in KINDS.items()}
LIBRARY_COLLECTIONS = {"series": "tvshows", "movies": "movies", "anime": "tvshows"}


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


def user_dto(ctx, user):
    defaults = ctx.service.settings().defaults
    return {
        "Name": user.username,
        "ServerId": server_id(ctx),
        "ServerName": "Lazarr",
        "Id": object_id("user", user.id),
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "EnableAutoLogin": False,
        "Configuration": {
            "AudioLanguagePreference": jellyfin_language(defaults.audio_languages[0])
            if defaults.audio_languages
            else None,
            "SubtitleLanguagePreference": jellyfin_language(defaults.subtitle_languages[0])
            if defaults.subtitle_languages
            else None,
            "PlayDefaultAudioTrack": True,
            "SubtitleMode": "Default" if defaults.subtitle_languages else "None",
            "RememberAudioSelections": False,
            "RememberSubtitleSelections": False,
            "EnableNextEpisodeAutoPlay": True,
        },
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


def provider_ids(media):
    values = {str(k).capitalize(): str(v) for k, v in media.metadata_json.get("external_ids", {}).items()}
    values.setdefault(media.provider.capitalize(), str(media.external_id))
    return values


def user_data(ctx, user, item_id, runtime_ticks=0):
    if user is None:
        return {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False}
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
        "IsFavorite": False,
        "Played": bool(row and row.played),
    }
    if row and row.last_played_at:
        result["LastPlayedDate"] = (
            datetime.fromtimestamp(row.last_played_at, timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if runtime_ticks:
        result["PlayedPercentage"] = min(100, position * 100 / runtime_ticks)
    return result


def media_dto(ctx, media, user=None):
    image = poster_tag(media)
    kind = "Movie" if media.kind == "movie" else "Series"
    data = media.metadata_json
    result = {
        "Name": media.title,
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
        "BackdropImageTags": [],
        "UserData": {},
    }
    playable = playable_asset(ctx, "media", media.id) if media.kind == "movie" else None
    if playable:
        result.update(playable_item_fields(playable))
        source = media_source(ctx, playable, result["Id"])
        result["MediaSources"] = [source]
        result["MediaStreams"] = source["MediaStreams"]
        result["HasSubtitles"] = any(s["Type"] == "Subtitle" for s in source["MediaStreams"])
    result["UserData"] = user_data(ctx, user, result["Id"], result.get("RunTimeTicks", 0))
    return result


def library_dto(ctx, key, name):
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
        "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False},
    }


def season_dto(ctx, media, number):
    count = sum(
        1
        for episode in ctx.library.detail(media.id)["episodes"]
        if episode["season"] == number and playable_asset(ctx, "episode", episode["id"])
    )
    return {
        "Name": f"Сезон {number}",
        "ServerId": server_id(ctx),
        "Id": object_id("season", media.id, number),
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
        "LocationType": "FileSystem",
        "ImageTags": {},
        "SeriesPrimaryImageTag": poster_tag(media),
        "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False},
    }


def playable_path(download, relative):
    root = Path(download.save_path).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def available_seasons(ctx, media):
    detail = ctx.library.detail(media.id)
    return sorted(
        {
            e["season"]
            for e in detail["episodes"]
            if e["season"] is not None and playable_asset(ctx, "episode", e["id"])
        }
    )


def playable_asset(ctx, kind, identity):
    with ctx.db.session() as db:
        statement = (
            select(LibraryAsset, MediaAsset, Download)
            .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
            .join(Download, Download.id == MediaAsset.download_id)
            .order_by(MediaAsset.id.desc())
        )
        if kind == "episode":
            statement = statement.where(LibraryAsset.episode_id == identity)
        else:
            statement = statement.where(
                LibraryAsset.media_id == identity,
                LibraryAsset.episode_id.is_(None),
            )
        for link, asset, download in db.execute(statement):
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
    return None


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


def media_streams(ctx, playable, item_id, media_source_id):
    asset, link, download = playable["asset"], playable["link"], playable["download"]
    defaults = ctx.service.settings().defaults
    streams = []
    for raw in asset.probe.get("streams", []):
        stream_type = raw.get("codec_type")
        if stream_type not in {"video", "audio", "subtitle"}:
            continue
        tags = raw.get("tags", {})
        entry = {
            "Codec": raw.get("codec_name"),
            "Language": jellyfin_language(tags.get("language", "und")),
            "Title": tags.get("title"),
            "Type": stream_type.capitalize(),
            "Index": integer(raw.get("index")) if raw.get("index") is not None else len(streams),
            "IsExternal": False,
            "IsDefault": bool(raw.get("disposition", {}).get("default")),
            "IsForced": bool(raw.get("disposition", {}).get("forced")),
            "Width": integer(raw.get("width")),
            "Height": integer(raw.get("height")),
            "Channels": integer(raw.get("channels")),
            "SampleRate": integer(raw.get("sample_rate")),
            "BitRate": integer(raw.get("bit_rate")),
        }
        streams.append(entry)
    next_index = max((s["Index"] for s in streams), default=-1) + 1
    external_tracks = list((link.preflight.get("binding") or {}).get("tracks", []))
    external_tracks.extend(asset.tracks or [])
    seen_external = set()
    for track in external_tracks:
        relative = track.get("path")
        # A direct HTTP video response cannot combine a separate audio file.
        # External subtitles are independent resources supported by Jellyfin clients.
        if track.get("kind") != "subtitle" or not relative:
            continue
        signature = (relative, language(track.get("language")))
        if signature in seen_external:
            continue
        seen_external.add(signature)
        path = playable_path(download, relative)
        if not path:
            continue
        suffix = path.suffix.lower().lstrip(".")
        entry = {
            "Codec": suffix,
            "Language": jellyfin_language(track.get("language", "und")),
            "Type": track["kind"].capitalize(),
            "Index": next_index,
            "IsExternal": True,
            "IsDefault": False,
            "IsForced": False,
            "Path": str(path),
        }
        if track["kind"] == "subtitle":
            entry.update(
                {
                    "DeliveryMethod": "External",
                    "DeliveryUrl": f"/Videos/{item_id}/{media_source_id}/Subtitles/{next_index}/Stream.{suffix}",
                    "IsExternalUrl": False,
                    "IsTextSubtitleStream": suffix in {"srt", "ass", "ssa", "vtt"},
                    "SupportsExternalStream": True,
                }
            )
        streams.append(entry)
        next_index += 1
    audio = sorted(
        (s for s in streams if s["Type"] == "Audio"),
        key=lambda s: (
            language_rank(s["Language"], [jellyfin_language(v) for v in defaults.audio_languages]),
            s["Index"],
        ),
    )
    subtitles = sorted(
        (s for s in streams if s["Type"] == "Subtitle"),
        key=lambda s: (
            language_rank(s["Language"], [jellyfin_language(v) for v in defaults.subtitle_languages]),
            s["Index"],
        ),
    )
    for stream in streams:
        if stream["Type"] in {"Audio", "Subtitle"}:
            stream["IsDefault"] = False
    if audio:
        audio[0]["IsDefault"] = True
    if subtitles and defaults.subtitle_languages:
        subtitles[0]["IsDefault"] = True
    return (
        streams,
        audio[0]["Index"] if audio else None,
        subtitles[0]["Index"] if subtitles and defaults.subtitle_languages else None,
    )


def media_source(ctx, playable, item_id):
    asset, path = playable["asset"], playable["path"]
    # Fladder requests /Videos/{MediaSource.Id}/stream. Keeping the source id
    # equal to the public item id makes that URL resolve without exposing a
    # filesystem path or requiring the client to know Lazarr's asset ids.
    source_id = item_id
    streams, audio_index, subtitle_index = media_streams(ctx, playable, item_id, source_id)
    duration = duration_ticks(asset)
    return {
        "Protocol": "File",
        "Id": source_id,
        "Path": str(path),
        "Type": "Default",
        "Container": path.suffix.lower().lstrip("."),
        "Size": path.stat().st_size,
        "Name": path.name,
        "IsRemote": False,
        "ETag": f"{path.stat().st_mtime_ns:x}-{path.stat().st_size:x}",
        "RunTimeTicks": duration,
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "SupportsProbing": False,
        "VideoType": "VideoFile",
        "MediaStreams": streams,
        "DefaultAudioStreamIndex": audio_index,
        "DefaultSubtitleStreamIndex": subtitle_index,
        "RequiredHttpHeaders": {},
        "DirectStreamUrl": f"/Videos/{item_id}/stream?Static=true&mediaSourceId={source_id}",
    }


def playable_item_fields(playable):
    path, asset = playable["path"], playable["asset"]
    return {
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
                (
                    s.get("width")
                    for s in asset.probe.get("streams", [])
                    if s.get("codec_type") == "video"
                ),
                None,
            )
        ),
        "Height": integer(asset.resolution),
    }


def episode_dto(ctx, media, episode_data, user=None):
    playable = playable_asset(ctx, "episode", episode_data["id"])
    if not playable:
        return None
    season_number = episode_data["season"]
    still = episode_data.get("still")
    still_tag = image_tag(still)
    episode_number = episode_data["episode"]
    title = episode_data["title"] or f"Серия {episode_number}"
    result = {
        "Name": title,
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
        "UserData": {},
    }
    result.update(playable_item_fields(playable))
    source = media_source(ctx, playable, result["Id"])
    result["MediaSources"] = [source]
    result["MediaStreams"] = source["MediaStreams"]
    result["HasSubtitles"] = any(s["Type"] == "Subtitle" for s in source["MediaStreams"])
    result["UserData"] = user_data(ctx, user, result["Id"], result["RunTimeTicks"])
    return result


def item_dto(ctx, item_id, user=None):
    if item_id in library_ids(ctx).values():
        key = next(key for key, value in library_ids(ctx).items() if value == item_id)
        return library_dto(ctx, key, dict(LIBRARIES)[key])
    kind, identity, secondary = parse_object_id(item_id)
    with ctx.db.session() as db:
        if kind == "media":
            media = db.get(Media, identity)
            if media:
                return media_dto(ctx, media, user)
        elif kind == "season":
            media = db.get(Media, identity)
            if media:
                return season_dto(ctx, media, secondary)
        elif kind == "episode":
            episode = db.get(Episode, identity)
            season = db.get(Season, episode.season_id) if episode else None
            media = db.get(Media, season.media_id) if season else None
            if media:
                detail = ctx.library.detail(media.id)
                data = next((e for e in detail["episodes"] if e["id"] == identity), None)
                if data:
                    item = episode_dto(ctx, media, data, user)
                    if item:
                        return item
    raise HTTPException(404, "Item not found")


def save_progress(ctx, user, item_id, position_ticks=None, played=None, touch=True):
    kind, identity, _ = parse_object_id(item_id)
    playable = playable_asset(ctx, kind, identity) if kind in {"media", "episode"} else None
    if not playable:
        raise HTTPException(404, "Playable file not found")
    runtime = duration_ticks(playable["asset"])
    position = max(0, int(position_ticks or 0)) if position_ticks is not None else None
    completed = played
    if completed is None and position is not None and runtime:
        completed = position >= runtime * 0.9
    now = time.time()
    with ctx.db.session() as db:
        row = db.scalar(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == user.id, PlaybackProgress.item_id == item_id
            )
        )
        if row is None:
            row = PlaybackProgress(user_id=user.id, item_id=item_id)
            db.add(row)
        was_played = row.played
        if completed is True:
            row.position_ticks = 0
        elif position is not None:
            row.position_ticks = position
        if completed is not None:
            row.played = bool(completed)
            if completed and not was_played:
                row.play_count += 1
        if touch:
            row.last_played_at = now
        row.updated_at = now
    return user_data(ctx, user, item_id, runtime)


def token_from(request):
    token = request.headers.get("X-Emby-Token") or request.headers.get("X-MediaBrowser-Token")
    if not token:
        header = request.headers.get("Authorization") or request.headers.get("X-Emby-Authorization", "")
        match = re.search(r'(?:Token|token)=["\']?([^"\',\s]+)', header)
        token = match.group(1) if match else None
    return token or request.query_params.get("api_key")


def install_jellyfin_api(app, context):
    def authenticated(request: Request):
        token = token_from(request)
        with context(request).db.session() as db:
            session = db.get(LoginSession, hash_token(token or ""))
            user = db.get(User, session.user_id) if session and session.expires_at > time.time() else None
            if not user or not user.active:
                raise HTTPException(401, "Invalid authentication token")
            return user

    def require_user_id(user_id, user):
        kind, identity, _ = parse_object_id(user_id)
        if kind != "user" or identity != user.id:
            raise HTTPException(403, "User does not match token")

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

    @app.get("/UserViews")
    async def jellyfin_views(request: Request, userId: str | None = None, user=Depends(authenticated)):
        if userId:
            require_user_id(userId, user)
        ctx = context(request)
        return query_result([library_dto(ctx, key, name) for key, name in LIBRARIES])

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

    def items_response(ctx, user, parent_id=None, search_term=None, include_types=None, recursive=False):
        ids = library_ids(ctx)
        if not parent_id:
            items = [library_dto(ctx, key, name) for key, name in LIBRARIES]
        elif parent_id in ids.values():
            key = next(key for key, value in ids.items() if value == parent_id)
            with ctx.db.session() as db:
                rows = list(db.scalars(select(Media).order_by(Media.title, Media.id)))
                items = [media_dto(ctx, media, user) for media in rows if library_kind(media) == key]
        else:
            kind, identity, secondary = parse_object_id(parent_id)
            with ctx.db.session() as db:
                media = db.get(Media, identity) if kind in {"media", "season"} else None
            if not media:
                raise HTTPException(404, "Item not found")
            detail = ctx.library.detail(media.id)
            if kind == "media" and media.kind == "tv":
                if recursive or include_types == "Episode":
                    items = [
                        item
                        for e in detail["episodes"]
                        for item in [episode_dto(ctx, media, e, user)]
                        if item
                    ]
                else:
                    items = [season_dto(ctx, media, number) for number in available_seasons(ctx, media)]
            elif kind == "season":
                items = [
                    item
                    for episode in detail["episodes"]
                    if episode["season"] == secondary
                    for item in [episode_dto(ctx, media, episode, user)]
                    if item
                ]
            else:
                items = []
        if search_term:
            items = [item for item in items if search_term.casefold() in item["Name"].casefold()]
        if include_types:
            allowed = {value.strip() for value in include_types.split(",")}
            items = [item for item in items if item.get("Type") in allowed]
        return items

    async def jellyfin_items_impl(request, user, parent_id=None):
        params = request.query_params
        requested_user = params.get("userId") or params.get("UserId")
        if requested_user:
            require_user_id(requested_user, user)
        items = items_response(
            context(request),
            user,
            parent_id or params.get("parentId") or params.get("ParentId"),
            params.get("searchTerm") or params.get("SearchTerm"),
            params.get("includeItemTypes") or params.get("IncludeItemTypes"),
            (params.get("recursive") or params.get("Recursive") or "false").lower() == "true",
        )
        start = int(params.get("startIndex") or params.get("StartIndex") or 0)
        raw_limit = params.get("limit") or params.get("Limit")
        return query_result(items, start, int(raw_limit) if raw_limit else None)

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
        params = request.query_params
        items = items_response(context(request), user, params.get("parentId") or params.get("ParentId"))
        return items[: int(params.get("limit") or params.get("Limit") or 20)]

    @app.get("/Users/{user_id}/Items/Latest")
    async def jellyfin_legacy_latest(user_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return await jellyfin_latest(request, user)

    @app.get("/Shows/{series_id}/Seasons")
    async def jellyfin_seasons(series_id: str, request: Request, user=Depends(authenticated)):
        kind, identity, _ = parse_object_id(series_id)
        with context(request).db.session() as db:
            media = db.get(Media, identity) if kind == "media" else None
        if not media or media.kind != "tv":
            raise HTTPException(404, "Series not found")
        numbers = available_seasons(context(request), media)
        return query_result([season_dto(context(request), media, number) for number in numbers])

    @app.get("/Shows/{series_id}/Episodes")
    async def jellyfin_episodes(series_id: str, request: Request, user=Depends(authenticated)):
        kind, identity, _ = parse_object_id(series_id)
        with context(request).db.session() as db:
            media = db.get(Media, identity) if kind == "media" else None
        if not media or media.kind != "tv":
            raise HTTPException(404, "Series not found")
        await context(request).library.enrich_media(identity)
        params = request.query_params
        season = params.get("season") or params.get("Season")
        season_id = params.get("seasonId") or params.get("SeasonId")
        if season_id:
            season_kind, season_media, season_number = parse_object_id(season_id)
            if season_kind != "season" or season_media != identity:
                raise HTTPException(404, "Season not found")
            season = season_number
        detail = context(request).library.detail(identity)
        items = [
            item
            for episode in detail["episodes"]
            if season is None or episode["season"] == int(season)
            for item in [episode_dto(context(request), media, episode, user)]
            if item
        ]
        start = int(params.get("startIndex") or params.get("StartIndex") or 0)
        raw_limit = params.get("limit") or params.get("Limit")
        return query_result(items, start, int(raw_limit) if raw_limit else None)

    @app.get("/Items/{item_id}")
    async def jellyfin_item(item_id: str, request: Request, user=Depends(authenticated)):
        return item_dto(context(request), item_id, user)

    @app.get("/Users/{user_id}/Items/{item_id}")
    async def jellyfin_legacy_item(user_id: str, item_id: str, request: Request, user=Depends(authenticated)):
        require_user_id(user_id, user)
        return item_dto(context(request), item_id, user)

    @app.get("/UserItems/{item_id}/UserData")
    async def jellyfin_user_data(
        item_id: str, request: Request, userId: str | None = None, user=Depends(authenticated)
    ):
        if userId:
            require_user_id(userId, user)
        item = item_dto(context(request), item_id, user)
        return item["UserData"]

    @app.post("/UserItems/{item_id}/UserData")
    async def jellyfin_update_user_data(
        item_id: str,
        payload: dict,
        request: Request,
        userId: str | None = None,
        user=Depends(authenticated),
    ):
        if userId:
            require_user_id(userId, user)
        return save_progress(
            context(request),
            user,
            item_id,
            payload.get("PlaybackPositionTicks"),
            payload.get("Played"),
        )

    @app.post("/UserPlayedItems/{item_id}")
    async def jellyfin_mark_played(
        item_id: str, request: Request, userId: str | None = None, user=Depends(authenticated)
    ):
        if userId:
            require_user_id(userId, user)
        return save_progress(context(request), user, item_id, played=True)

    @app.delete("/UserPlayedItems/{item_id}")
    async def jellyfin_mark_unplayed(
        item_id: str, request: Request, userId: str | None = None, user=Depends(authenticated)
    ):
        if userId:
            require_user_id(userId, user)
        return save_progress(context(request), user, item_id, position_ticks=0, played=False, touch=False)

    @app.get("/UserItems/Resume")
    async def jellyfin_resume_items(request: Request, userId: str | None = None, user=Depends(authenticated)):
        if userId:
            require_user_id(userId, user)
        ctx = context(request)
        with ctx.db.session() as db:
            rows = list(
                db.scalars(
                    select(PlaybackProgress)
                    .where(
                        PlaybackProgress.user_id == user.id,
                        PlaybackProgress.played.is_(False),
                        PlaybackProgress.position_ticks > 0,
                    )
                    .order_by(PlaybackProgress.last_played_at.desc())
                )
            )
        items = []
        for row in rows:
            try:
                items.append(item_dto(ctx, row.item_id, user))
            except HTTPException:
                continue
        params = request.query_params
        start = int(params.get("startIndex") or params.get("StartIndex") or 0)
        raw_limit = params.get("limit") or params.get("Limit")
        return query_result(items, start, int(raw_limit) if raw_limit else None)

    def playback_for(ctx, item_id):
        kind, identity, _ = parse_object_id(item_id)
        playable = playable_asset(ctx, kind, identity) if kind in {"media", "episode"} else None
        if not playable:
            raise HTTPException(404, "Playable file not found")
        return playable

    def playback_info_response(item_id, request):
        playable = playback_for(context(request), item_id)
        return {
            "MediaSources": [media_source(context(request), playable, item_id)],
            "PlaySessionId": uuid.uuid4().hex,
        }

    @app.get("/Items/{item_id}/PlaybackInfo")
    async def jellyfin_playback_info_get(item_id: str, request: Request, user=Depends(authenticated)):
        return playback_info_response(item_id, request)

    @app.post("/Items/{item_id}/PlaybackInfo")
    async def jellyfin_playback_info_post(item_id: str, request: Request, user=Depends(authenticated)):
        return playback_info_response(item_id, request)

    def stream_response(item_id, request, container=None):
        playable = playback_for(context(request), item_id)
        path = playable["path"]
        if container and container.casefold() != path.suffix.lstrip(".").casefold():
            raise HTTPException(415, "Container conversion is disabled")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.get("/Videos/{item_id}/stream")
    @app.get("/Videos/{item_id}/stream.{container}")
    async def jellyfin_stream(
        item_id: str, request: Request, container: str | None = None, user=Depends(authenticated)
    ):
        return stream_response(item_id, request, container)

    @app.head("/Videos/{item_id}/stream", include_in_schema=False)
    @app.head("/Videos/{item_id}/stream.{container}", include_in_schema=False)
    async def jellyfin_stream_head(
        item_id: str, request: Request, container: str | None = None, user=Depends(authenticated)
    ):
        return stream_response(item_id, request, container)

    async def image_response(item_id, image_type, request):
        if image_type.casefold() != "primary":
            raise HTTPException(404, "Image not found")
        ctx = context(request)
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
        image = episode.still if kind == "episode" and episode and episode.still else None
        image = image or (media.metadata_json.get("poster") if media else None)
        filename = image.rsplit("/", 1)[-1] if image else None
        if not filename:
            raise HTTPException(404, "Image not found")
        return FileResponse(await fetch_poster(ctx, filename))

    @app.get("/Items/{item_id}/Images/{image_type}")
    async def jellyfin_image_get(item_id: str, image_type: str, request: Request):
        return await image_response(item_id, image_type, request)

    @app.head("/Items/{item_id}/Images/{image_type}", include_in_schema=False)
    async def jellyfin_image_head(item_id: str, image_type: str, request: Request):
        return await image_response(item_id, image_type, request)

    @app.get("/Videos/{item_id}/{media_source_id}/Subtitles/{index}/Stream.{subtitle_format}")
    async def jellyfin_subtitle(
        item_id: str,
        media_source_id: str,
        index: int,
        subtitle_format: str,
        request: Request,
        user=Depends(authenticated),
    ):
        playable = playback_for(context(request), item_id)
        source = media_source(context(request), playable, item_id)
        if source["Id"] != media_source_id:
            raise HTTPException(404, "Media source not found")
        stream = next(
            (
                stream
                for stream in source["MediaStreams"]
                if stream["Index"] == index and stream["Type"] == "Subtitle" and stream["IsExternal"]
            ),
            None,
        )
        path = Path(stream["Path"]) if stream else None
        if not path or subtitle_format.casefold() != path.suffix.lstrip(".").casefold():
            raise HTTPException(415, "Subtitle conversion is disabled")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "text/plain")

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
            save_progress(context(request), user, item_id, payload.get("PositionTicks"))
        return Response(status_code=204)
