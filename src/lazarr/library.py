"""Read model of shared Media, independent of task owners and download folders."""

import asyncio
from contextlib import nullcontext
import time
from pathlib import Path
from sqlalchemy import select
from lazarr.models import (
    Media,
    Season,
    Episode,
    Task,
    Subtask,
    SubtaskAsset,
    LibraryAsset,
    MediaAsset,
    Download,
    Release,
    CandidateDecision,
    ConfigEntry,
)
from lazarr.calendar import released
from lazarr.matcher import classify_external_subtitles
from lazarr.sdk import language
from lazarr.subtitle_language import stored_subtitle_language

LIBRARIES = [("series", "Сериалы"), ("movies", "Кино"), ("anime", "Аниме")]


def library_kind(media):
    data = media.metadata_json
    if 16 in data.get("genre_ids", []) and (
        "JP" in data.get("origin_countries", []) or data.get("original_language") == "ja"
    ):
        return "anime"
    return "movies" if media.kind == "movie" else "series"


class LibraryService:
    def __init__(self, db, plugins, service):
        self.db, self.plugins, self.service = db, plugins, service
        self.refresh_lock = asyncio.Lock()

    def _reserve_refresh(self, kind, identity):
        key = f"preparation.metadata.{kind}.{identity}"
        now = time.time()
        with self.db.session() as db:
            row = db.get(ConfigEntry, key)
            if row and row.value.get("retry_at", 0) > now:
                return False
            if row:
                row.value = {"retry_at": now + 300}
            else:
                db.add(ConfigEntry(key=key, value={"retry_at": now + 300}))
        return True

    async def enrich(self, *, background=False, observe=None):
        # Upgrade metadata saved before taxonomy was part of the SDK. Never infer
        # anime solely from a Japanese title (which could be live-action).
        async with self.refresh_lock:
            with self.db.session() as db:
                pending = [
                    (m.id, m.provider, m.kind, m.external_id, m.title)
                    for m in db.scalars(select(Media))
                    if not m.metadata_json.get("taxonomy_known")
                    or "backdrop" not in m.metadata_json
                    or "people" not in m.metadata_json
                ]
            for identity, provider_id, kind, external_id, title in pending:
                if provider_id not in self.plugins.available("metadata"):
                    continue
                if background and not self._reserve_refresh("media", identity):
                    continue
                try:
                    with (
                        observe(
                            "metadata-refresh",
                            detail=f"{title} · Карточка: фон, участники, рейтинги и классификация",
                        )
                        if observe
                        else nullcontext()
                    ):
                        async with self.plugins.open(provider_id) as provider:
                            item = await provider.get_media(kind, external_id)
                        with self.db.session() as db:
                            row = db.get(Media, identity)
                            if row:
                                fields = [
                                    "backdrop",
                                    "people",
                                    "studios",
                                    "community_rating",
                                    "official_rating",
                                    "status",
                                    "tags",
                                    "remote_trailers",
                                    "collection",
                                ]
                                if item.taxonomy_known:
                                    fields.extend(
                                        [
                                            "genre_ids",
                                            "genres",
                                            "origin_countries",
                                            "original_language",
                                            "taxonomy_known",
                                        ]
                                    )
                                row.metadata_json = {
                                    **row.metadata_json,
                                    **{field: getattr(item, field) for field in fields},
                                }
                except Exception:
                    # Existing local library remains usable when metadata is offline.
                    continue

    async def enrich_media(self, identity, *, background=False, observe=None):
        """Refresh old episode rows once after the episode-metadata migration."""
        async with self.refresh_lock:
            with self.db.session() as db:
                media = db.get(Media, identity)
                pending = [
                    (season.id, season.number)
                    for season in db.scalars(select(Season).where(Season.media_id == identity))
                    if season.refreshed_at == 0
                ]
                provider_id = media.provider if media else None
                external_id = media.external_id if media else None
            if not media or provider_id not in self.plugins.available("metadata"):
                return
            for season_id, number in pending:
                if background and not self._reserve_refresh("season", season_id):
                    continue
                try:
                    with (
                        observe("metadata-refresh", detail=f"{media.title} · Сезон {number}: данные эпизодов")
                        if observe
                        else nullcontext()
                    ):
                        async with self.plugins.open(provider_id) as provider:
                            info = await provider.get_season(external_id, number)
                        with self.db.session() as db:
                            current = db.get(Season, season_id)
                            if current:
                                self.service._upsert_season(db, identity, info)
                except Exception:
                    # A metadata outage must not hide the local library.
                    continue

    async def load_season(self, identity, number):
        """Fetch episode metadata on expansion without creating a download task."""
        with self.db.session() as db:
            media = db.get(Media, identity)
            if not media or media.kind != "tv":
                raise ValueError("Сериал не найден")
            canonical = {
                int(key.split(":")[0])
                for key, aliases in media.metadata_json.get("episode_numbering", {}).items()
                if any(alias["season"] == number for alias in aliases)
            }
            if len(canonical) > 1:
                raise ValueError("Нет однозначного соответствия сезона")
            season_number = next(iter(canonical), number)
            provider_id, external_id = media.provider, media.external_id
        async with self.refresh_lock:
            async with self.plugins.open(provider_id) as provider:
                info = await provider.get_season(external_id, season_number)
            with self.db.session() as db:
                if db.get(Media, identity):
                    self.service._upsert_season(db, identity, info)

    def list(self):
        with self.db.session() as db:
            items = list(db.scalars(select(Media).order_by(Media.title, Media.id)))
            active = {"starting", "downloading"}
            summaries = {}
            for media in items:
                downloads = {
                    download.id: download
                    for download in db.scalars(
                        select(Download)
                        .join(MediaAsset, MediaAsset.download_id == Download.id)
                        .where(MediaAsset.media_id == media.id, Download.state.in_(active))
                    )
                }
                if downloads:
                    values = [
                        min(1.0, max(0.0, float((d.stats or {}).get("progress", 0))))
                        for d in downloads.values()
                    ]
                    summaries[media.id] = {
                        "state": "downloading",
                        "progress": sum(values) / len(values),
                        "download_rate": sum(
                            (d.stats or {}).get("download_rate", 0) or 0 for d in downloads.values()
                        ),
                    }
            return [
                {
                    "id": key,
                    "name": name,
                    "items": [self.tile(m, summaries.get(m.id)) for m in items if library_kind(m) == key],
                }
                for key, name in LIBRARIES
            ]

    def tile(self, media, download=None):
        return {
            "id": media.id,
            "title": media.title,
            "year": media.year,
            "poster": media.metadata_json.get("poster"),
            "kind": media.kind,
            "library": library_kind(media),
            "taxonomy_known": bool(media.metadata_json.get("taxonomy_known")),
            "download": download,
        }

    def detail(self, identity, episode_id=None, *, include_versions=True):
        with self.db.session() as db:
            media = db.get(Media, identity)
            if not media:
                return None
            seasons = {s.id: s for s in db.scalars(select(Season).where(Season.media_id == identity))}
            episode_query = select(Episode).where(Episode.season_id.in_(seasons))
            if episode_id is not None:
                episode_query = episode_query.where(Episode.id == episode_id)
            episodes = list(db.scalars(episode_query))
            tasks = {t.id: t for t in db.scalars(select(Task).where(Task.media_id == identity))}
            sub_query = select(Subtask).where(Subtask.task_id.in_(tasks))
            if episode_id is not None:
                sub_query = sub_query.where(Subtask.episode_id == episode_id)
            subs = list(db.scalars(sub_query))
            stored_versions = {}
            for link, asset, download, release in db.execute(
                select(LibraryAsset, MediaAsset, Download, Release)
                .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
                .join(Download, Download.id == MediaAsset.download_id)
                .join(Release, Release.id == Download.release_id)
                .where(LibraryAsset.media_id == identity)
                .where(include_versions)
                .where(LibraryAsset.episode_id == episode_id if episode_id is not None else True)
            ):
                from lazarr.jellyfin_resources import is_extra

                if is_extra(link.part_key):
                    continue
                stored_versions.setdefault(link.episode_id, []).append(
                    self._version(link, asset, download, release, current=True, pending=False)
                )
            task_versions = {}
            selected_candidates = {}
            if subs and include_versions:
                decisions = {
                    (row.subtask_id, row.release_id): row.id
                    for row in db.scalars(
                        select(CandidateDecision).where(
                            CandidateDecision.subtask_id.in_([s.id for s in subs])
                        )
                    )
                }
                for link, asset, download, release in db.execute(
                    select(SubtaskAsset, MediaAsset, Download, Release)
                    .join(MediaAsset, MediaAsset.id == SubtaskAsset.asset_id)
                    .join(Download, Download.id == MediaAsset.download_id)
                    .join(Release, Release.id == Download.release_id)
                    .where(SubtaskAsset.subtask_id.in_([s.id for s in subs]))
                ):
                    selected_candidates[link.subtask_id] = decisions.get((link.subtask_id, release.id))
                    task_versions.setdefault(link.subtask_id, []).append(
                        self._version(
                            link,
                            asset,
                            download,
                            release,
                            current=link.current,
                            pending=link.pending,
                            subtask_id=link.subtask_id,
                        )
                    )
            timezone = self.service.settings().timezone
            parts = []
            for episode in episodes if media.kind == "tv" else [None]:
                related = [s for s in subs if s.episode_id == (episode.id if episode else None)]
                canonical = seasons[episode.season_id].number if episode else None
                aliases = (
                    media.metadata_json.get("episode_numbering", {}).get(f"{canonical}:{episode.number}", [])
                    if episode
                    else []
                )
                display = (
                    aliases[0]
                    if len(aliases) == 1
                    else {"season": canonical, "episode": episode.number if episode else None}
                )
                date = episode.air_date if episode else media.metadata_json.get("release_date")
                files = {
                    version["id"]: version
                    for version in stored_versions.get(episode.id if episode else None, [])
                }
                for sub in related:
                    for version in task_versions.get(sub.id, []):
                        old = files.get(version["id"])
                        if old:
                            version = {
                                **version,
                                "current": old["current"] or version["current"],
                                "pending": old["pending"] or version["pending"],
                                "verified": old["verified"] or version["verified"],
                            }
                        files[version["id"]] = version
                parts.append(
                    {
                        "id": episode.id if episode else "movie",
                        "season": display["season"],
                        "episode": display["episode"],
                        "canonical_season": canonical,
                        "canonical_episode": episode.number if episode else None,
                        "title": episode.title if episode else media.title,
                        "overview": episode.overview if episode else media.metadata_json.get("overview", ""),
                        "still": episode.still if episode else media.metadata_json.get("poster"),
                        "air_date": date,
                        "released": released(date, timezone),
                        "statuses": sorted(
                            set("paused" if tasks[s.task_id].paused else s.status for s in related)
                        ),
                        "requested": bool(related),
                        "subtasks": [
                            {
                                "id": sub.id,
                                "task_id": sub.task_id,
                                "selected_candidate_id": selected_candidates.get(sub.id),
                                "status": "paused" if tasks[sub.task_id].paused else sub.status,
                            }
                            for sub in related
                        ],
                        "last_search_at": max((s.last_search_at or 0 for s in related), default=0) or None,
                        "files": list(files.values()),
                        "download": self._part_download(list(files.values())),
                    }
                )
            parts.sort(key=lambda p: (p["season"] or 0, p["episode"] or 0))
            return {
                **self.tile(media),
                "metadata": media.metadata_json,
                "seasons": media.metadata_json.get("seasons", []),
                "episodes": parts,
                "last_search_at": max((s.last_search_at or 0 for s in subs), default=0) or None,
                "task_count": len(tasks),
            }

    def _version(self, link, asset, download, release, *, current, pending, subtask_id=None):
        binding = link.preflight.get("binding") or {}
        verification = link.verification or {}
        streams = asset.probe.get("streams", [])

        def subtitle_path(relative):
            if not relative:
                return None
            root = Path(download.save_path).resolve()
            path = (root / relative).resolve()
            return path if path.is_relative_to(root) and path.is_file() else None

        video_path = subtitle_path(asset.path)

        def stream_language(stream):
            tagged = language(stream.get("tags", {}).get("language"))
            if tagged != "und" or stream.get("codec_type") != "subtitle":
                return tagged
            if stream.get("detected_language"):
                return language(stream["detected_language"])
            if video_path and stream.get("index") is not None:
                return stored_subtitle_language(asset, video_path, stream["index"], stream.get("codec_name"))
            return "und"

        tracks = [
            {
                "kind": stream["codec_type"],
                "language": stream_language(stream),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "title": stream.get("tags", {}).get("title"),
                "external": False,
                "verified": True,
            }
            for stream in streams
            if stream.get("codec_type") in {"audio", "subtitle"}
        ]
        binding_tracks = classify_external_subtitles(
            [dict(track) for track in binding.get("tracks", [])], download.plan.get("files", [])
        )
        for track in binding_tracks:
            external = track.get("file_index") is not None
            if external or not streams:
                track_language = language(track.get("language"))
                if external and track["kind"] == "subtitle" and track_language == "und":
                    path = subtitle_path(track.get("path"))
                    if path:
                        track_language = stored_subtitle_language(asset, path)
                tracks.append(
                    {
                        "kind": track["kind"],
                        "language": track_language,
                        "codec": None,
                        "path": track.get("path"),
                        "title": track.get("title"),
                        "forced": bool(track.get("forced")),
                        "external": external,
                        "verified": bool(verification.get("complete") and current),
                    }
                )
        for track in (asset.tracks or []) if not binding else []:
            track_language = language(track.get("language"))
            if track.get("kind", "subtitle") == "subtitle" and track_language == "und":
                path = subtitle_path(track.get("path"))
                if path:
                    track_language = stored_subtitle_language(asset, path)
            tracks.append(
                {
                    "kind": track.get("kind", "subtitle"),
                    "language": track_language,
                    "codec": track.get("codec"),
                    "path": track.get("path"),
                    "external": bool(track.get("external", True)),
                    "verified": bool(track.get("verified", True)),
                    "source": track.get("source"),
                }
            )
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        file = next(
            (item for item in download.plan.get("files", []) if item["index"] == asset.video_index), {}
        )
        return {
            "id": asset.id,
            "path": asset.path,
            "directory": download.save_path,
            "size": file.get("size"),
            "resolution": asset.resolution or binding.get("resolution"),
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "tracks": tracks,
            "current": current,
            "pending": pending,
            "verified": bool(current and verification.get("complete")),
            "download_state": download.state,
            "download": self._download_info(download, subtask_id),
            "release": {
                "provider": release.provider,
                "title": release.data.get("title", ""),
                "url": release.data.get("url", ""),
            },
            "missing_subtitle_languages": verification.get(
                "missing_subtitle_languages", binding.get("missing_subtitle_languages", [])
            ),
        }

    @staticmethod
    def _download_info(download, subtask_id):
        stats = download.stats or {}
        part = (stats.get("bindings") or {}).get(str(subtask_id), {})
        downloaded = download.downloaded or 0
        return {
            "id": download.id,
            "subtask_id": subtask_id,
            "state": download.state,
            "progress": min(1.0, max(0.0, float(part.get("progress", stats.get("progress", 0)) or 0))),
            "eta": part.get("eta", stats.get("eta")),
            "download_rate": stats.get("download_rate", 0) or 0,
            "upload_rate": stats.get("upload_rate", 0) or 0,
            "ratio": download.uploaded / downloaded if downloaded else 0,
            "seed_ratio": download.seed_ratio,
            "seeds": stats.get("seeds", 0) or 0,
            "peers": stats.get("peers", 0) or 0,
            "error": stats.get("error"),
        }

    @staticmethod
    def _part_download(files):
        active = [file["download"] for file in files if file.get("download")]
        if not active:
            return None
        current = next(
            (item for item in active if item["state"] in {"starting", "downloading", "paused"}), active[0]
        )
        return current
