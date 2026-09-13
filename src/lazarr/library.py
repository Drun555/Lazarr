"""Read model of shared Media, independent of task owners and download folders."""

import asyncio
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
)
from lazarr.calendar import released
from lazarr.sdk import language

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

    async def enrich(self):
        # Upgrade metadata saved before taxonomy was part of the SDK. Never infer
        # anime solely from a Japanese title (which could be live-action).
        async with self.refresh_lock:
            with self.db.session() as db:
                pending = [
                    (m.id, m.provider, m.kind, m.external_id)
                    for m in db.scalars(select(Media))
                    if not m.metadata_json.get("taxonomy_known")
                ]
            for identity, provider_id, kind, external_id in pending:
                if provider_id not in self.plugins.available("metadata"):
                    continue
                try:
                    async with self.plugins.open(provider_id) as provider:
                        item = await provider.get_media(kind, external_id)
                    if item.taxonomy_known:
                        with self.db.session() as db:
                            row = db.get(Media, identity)
                            if row:
                                row.metadata_json = {
                                    **row.metadata_json,
                                    **{
                                        k: getattr(item, k)
                                        for k in (
                                            "genre_ids",
                                            "genres",
                                            "origin_countries",
                                            "original_language",
                                            "taxonomy_known",
                                        )
                                    },
                                }
                except Exception:
                    # Existing local library remains usable when metadata is offline.
                    continue

    async def enrich_media(self, identity):
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
                try:
                    async with self.plugins.open(provider_id) as provider:
                        info = await provider.get_season(external_id, number)
                    with self.db.session() as db:
                        current = db.get(Season, season_id)
                        if current:
                            self.service._upsert_season(db, identity, info)
                except Exception:
                    # A metadata outage must not hide the local library.
                    continue

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
                    values = [min(1.0, max(0.0, float((d.stats or {}).get("progress", 0)))) for d in downloads.values()]
                    summaries[media.id] = {
                        "state": "downloading",
                        "progress": sum(values) / len(values),
                        "download_rate": sum((d.stats or {}).get("download_rate", 0) or 0 for d in downloads.values()),
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

    def detail(self, identity):
        with self.db.session() as db:
            media = db.get(Media, identity)
            if not media:
                return None
            seasons = {s.id: s for s in db.scalars(select(Season).where(Season.media_id == identity))}
            episodes = list(db.scalars(select(Episode).where(Episode.season_id.in_(seasons))))
            tasks = {t.id: t for t in db.scalars(select(Task).where(Task.media_id == identity))}
            subs = list(db.scalars(select(Subtask).where(Subtask.task_id.in_(tasks))))
            stored_versions = {}
            for link, asset, download, release in db.execute(
                select(LibraryAsset, MediaAsset, Download, Release)
                .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
                .join(Download, Download.id == MediaAsset.download_id)
                .join(Release, Release.id == Download.release_id)
                .where(LibraryAsset.media_id == identity)
            ):
                stored_versions.setdefault(link.episode_id, []).append(
                    self._version(link, asset, download, release, current=True, pending=False)
                )
            task_versions = {}
            if subs:
                for link, asset, download, release in db.execute(
                    select(SubtaskAsset, MediaAsset, Download, Release)
                    .join(MediaAsset, MediaAsset.id == SubtaskAsset.asset_id)
                    .join(Download, Download.id == MediaAsset.download_id)
                    .join(Release, Release.id == Download.release_id)
                    .where(SubtaskAsset.subtask_id.in_([s.id for s in subs]))
                ):
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
                files = {version["id"]: version for version in stored_versions.get(episode.id if episode else None, [])}
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
                "episodes": parts,
                "last_search_at": max((s.last_search_at or 0 for s in subs), default=0) or None,
                "task_count": len(tasks),
            }

    def _version(self, link, asset, download, release, *, current, pending, subtask_id=None):
        binding = link.preflight.get("binding") or {}
        verification = link.verification or {}
        streams = asset.probe.get("streams", [])
        tracks = [
            {
                "kind": stream["codec_type"],
                "language": language(stream.get("tags", {}).get("language")),
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "title": stream.get("tags", {}).get("title"),
                "external": False,
                "verified": True,
            }
            for stream in streams
            if stream.get("codec_type") in {"audio", "subtitle"}
        ]
        for track in binding.get("tracks", []):
            external = track.get("file_index") is not None
            if external or not streams:
                tracks.append(
                    {
                        "kind": track["kind"],
                        "language": track["language"],
                        "codec": None,
                        "path": track.get("path"),
                        "external": external,
                        "verified": bool(verification.get("complete") and current),
                    }
                )
        for track in asset.tracks or []:
            tracks.append(
                {
                    "kind": track.get("kind", "subtitle"),
                    "language": language(track.get("language")),
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
        current = next((item for item in active if item["state"] in {"starting", "downloading", "paused"}), active[0])
        return current
