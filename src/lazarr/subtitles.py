"""Download and register external subtitles for verified library assets."""

import asyncio
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from lazarr.languages import language
from lazarr.models import (
    AuditEvent,
    Download,
    Episode,
    LibraryAsset,
    Media,
    MediaAsset,
    Season,
    Subtask,
    SubtaskAsset,
    Task,
)
from lazarr.sdk import ProviderError, SubtitleRequest


class SubtitleService:
    def __init__(self, db, plugins, task_service):
        self.db = db
        self.plugins = plugins
        self.task_service = task_service
        self.lock = asyncio.Lock()

    def _targets(self, media_id, requested_languages):
        defaults = self.task_service.settings().defaults.subtitle_languages
        with self.db.session() as db:
            media = db.get(Media, media_id)
            if not media:
                return None, []
            task_requirements = {}
            for subtask, task in db.execute(
                select(Subtask, Task)
                .join(Task, Task.id == Subtask.task_id)
                .where(Task.media_id == media_id)
            ):
                task_requirements.setdefault(subtask.episode_id, set()).update(
                    task.requirements.get("subtitle_languages", [])
                )
            rows = list(
                db.execute(
                    select(LibraryAsset, MediaAsset, Download, Episode, Season)
                    .join(MediaAsset, MediaAsset.id == LibraryAsset.asset_id)
                    .join(Download, Download.id == MediaAsset.download_id)
                    .outerjoin(Episode, Episode.id == LibraryAsset.episode_id)
                    .outerjoin(Season, Season.id == Episode.season_id)
                    .where(LibraryAsset.media_id == media_id)
                    .order_by(Season.number, Episode.number, MediaAsset.id)
                )
            )
            grouped = {}
            for link, asset, download, episode, season in rows:
                if not link.verification.get("complete"):
                    continue
                root = Path(download.save_path).resolve()
                video = (root / asset.path).resolve()
                if not video.is_relative_to(root) or not video.is_file():
                    continue
                target = grouped.setdefault(
                    asset.id,
                    {
                        "asset_id": asset.id,
                        "root": root,
                        "video": video,
                        "title": media.title,
                        "aliases": list(
                            dict.fromkeys(
                                value
                                for value in [
                                    media.metadata_json.get("original_title"),
                                    *(media.metadata_json.get("aliases") or []),
                                ]
                                if value and value != media.title
                            )
                        ),
                        "year": media.year,
                        "media_kind": "episode" if episode else "movie",
                        "season": season.number if season else None,
                        "episode": episode.number if episode else None,
                        "episode_id": episode.id if episode else None,
                        "episode_external_id": episode.external_id if episode else None,
                        "requirements": set(),
                        "present": set(),
                    },
                )
                target["requirements"].update(task_requirements.get(episode.id if episode else None, []))
                for stream in asset.probe.get("streams", []):
                    if stream.get("codec_type") == "subtitle":
                        target["present"].add(language((stream.get("tags") or {}).get("language")))
                for track in asset.tracks or []:
                    if track.get("kind") == "subtitle":
                        target["present"].add(language(track.get("language")))
                for track in (link.preflight.get("binding") or {}).get("tracks", []):
                    if track.get("kind") == "subtitle":
                        target["present"].add(language(track.get("language")))
            metadata_ids = dict(media.metadata_json.get("external_ids") or {})
            metadata_ids.setdefault(media.provider, media.external_id)
            for target in grouped.values():
                canonical = f"{target['season']}:{target['episode']}"
                aliases = media.metadata_json.get("episode_numbering", {}).get(canonical, [])
                if len(aliases) == 1:
                    target["season"] = aliases[0].get("season", target["season"])
                    target["episode"] = aliases[0].get("episode", target["episode"])
                target["external_ids"] = dict(metadata_ids)
                if target["episode_external_id"]:
                    target["external_ids"][media.provider] = target["episode_external_id"]
                wanted = requested_languages or sorted(target["requirements"]) or defaults
                target["languages"] = [code for code in wanted if code not in target["present"]]
            return media, list(grouped.values())

    @staticmethod
    def _best(candidates, wanted):
        matches = [item for item in candidates if language(item.language) == wanted]
        return min(
            matches,
            key=lambda item: (
                item.machine_translated,
                item.ai_translated,
                item.hearing_impaired,
                -item.rating,
                -item.downloads,
                item.id,
            ),
            default=None,
        )

    @staticmethod
    def _write(target, language_code, downloaded, provider_id):
        suffix = Path(downloaded.filename).suffix.lower()
        destination = target["video"].with_name(
            f"{target['video'].stem}.{provider_id}.{language_code}{suffix}"
        )
        if destination.exists():
            index = 2
            while destination.exists():
                destination = target["video"].with_name(
                    f"{target['video'].stem}.{provider_id}-{index}.{language_code}{suffix}"
                )
                index += 1
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent, prefix=".lazarr-subtitle-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(downloaded.content)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def _register(self, target, language_code, candidate, destination, user_id, provider_id):
        relative = destination.relative_to(target["root"]).as_posix()
        with self.db.session() as db:
            asset = db.get(MediaAsset, target["asset_id"])
            tracks = list(asset.tracks or [])
            tracks.append(
                {
                    "kind": "subtitle",
                    "language": language_code,
                    "codec": destination.suffix.lstrip("."),
                    "path": relative,
                    "external": True,
                    "verified": True,
                    "source": provider_id,
                    "source_id": candidate.id,
                }
            )
            asset.tracks = tracks
            links = list(db.scalars(select(SubtaskAsset).where(SubtaskAsset.asset_id == asset.id)))
            for link in links:
                verification = dict(link.verification)
                verification["missing_subtitle_languages"] = [
                    value
                    for value in verification.get("missing_subtitle_languages", [])
                    if language(value) != language_code
                ]
                link.verification = verification
                subtask = db.get(Subtask, link.subtask_id)
                subtask.missing_subtitle_languages = [
                    value for value in subtask.missing_subtitle_languages if language(value) != language_code
                ]
            for link in db.scalars(select(LibraryAsset).where(LibraryAsset.asset_id == asset.id)):
                verification = dict(link.verification)
                verification["missing_subtitle_languages"] = [
                    value
                    for value in verification.get("missing_subtitle_languages", [])
                    if language(value) != language_code
                ]
                link.verification = verification
            db.add(
                AuditEvent(
                    user_id=user_id,
                    action="subtitle.download",
                    target=str(asset.id),
                    details={"language": language_code, "provider": provider_id, "path": relative},
                )
            )
        return relative

    async def download(self, media_id, requested_languages, user_id):
        async with self.lock:
            media, targets = self._targets(media_id, requested_languages)
            if media is None:
                return None
            providers = self.plugins.available("subtitle")
            if not providers:
                raise ProviderError("configuration", "Включите провайдер субтитров в настройках")
            result = {"downloaded": [], "not_found": [], "skipped": 0, "errors": []}
            if not targets:
                return result
            pending = {target["asset_id"]: set(target["languages"]) for target in targets}
            result["skipped"] = sum(not values for values in pending.values())
            provider_errors = []
            for provider_id in providers:
                try:
                    async with self.plugins.open(provider_id, bypass_cooldown=True) as provider:
                        for target in targets:
                            wanted = sorted(pending[target["asset_id"]])
                            if not wanted:
                                continue
                            query = SubtitleRequest(
                                media_kind=target["media_kind"],
                                title=target["title"],
                                year=target["year"],
                                season=target["season"],
                                episode=target["episode"],
                                languages=wanted,
                                external_ids=target["external_ids"],
                                video_filename=target["video"].name,
                                aliases=target["aliases"],
                            )
                            candidates = await provider.search(query)
                            for language_code in wanted:
                                candidate = self._best(candidates, language_code)
                                label = (
                                    f"S{target['season']}E{target['episode']}"
                                    if target["media_kind"] == "episode"
                                    else media.title
                                )
                                if not candidate:
                                    continue
                                downloaded = await provider.download(candidate)
                                destination = await asyncio.to_thread(
                                    self._write, target, language_code, downloaded, provider_id
                                )
                                relative = self._register(
                                    target, language_code, candidate, destination, user_id, provider_id
                                )
                                pending[target["asset_id"]].discard(language_code)
                                result["downloaded"].append(
                                    {
                                        "item": label,
                                        "language": language_code,
                                        "path": relative,
                                        "provider": provider_id,
                                    }
                                )
                except ProviderError as exc:
                    provider_errors.append(
                        {"code": exc.code, "message": str(exc), "provider": provider_id}
                    )
            for target in targets:
                label = (
                    f"S{target['season']}E{target['episode']}"
                    if target["media_kind"] == "episode"
                    else media.title
                )
                for language_code in sorted(pending[target["asset_id"]]):
                    result["not_found"].append({"item": label, "language": language_code})
            if result["not_found"]:
                result["errors"] = provider_errors
            return result
