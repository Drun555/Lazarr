"""Task deletion with shared-download protection and durable file-cleanup retries."""

import asyncio
import re
import shutil
import time
from pathlib import Path
from uuid import uuid4
from sqlalchemy import delete, select
from lazarr.models import (
    Task,
    TaskSeason,
    Subtask,
    SubtaskAsset,
    LibraryAsset,
    MediaAsset,
    Download,
    CandidateDecision,
    ConfigEntry,
    Media,
    Season,
    Episode,
    PlaybackProgress,
    PlaybackSession,
    VideoPlaylist,
)
from lazarr.security import audit


def managed_directory(download):
    path = Path(download.save_path)
    if (
        not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", download.infohash)
        or not path.is_absolute()
        or path.name != download.infohash
        or path.resolve() != path
    ):
        raise ValueError("Небезопасный путь загрузки: удаление отменено")
    return path


def cleanup(db):
    pending = False
    with db.session() as session:
        jobs = [
            (j.key, j.value)
            for j in session.scalars(select(ConfigEntry).where(ConfigEntry.key.like("cleanup.%")))
        ]
    for key, job in jobs:
        try:
            for entry in job.get("files", []):
                from lazarr.replacement import retained_paths

                root = Path(entry["root"])
                path = root / entry["path"]
                if (
                    not root.is_absolute()
                    or root.resolve() != root
                    or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", root.name)
                    or path.resolve() != path
                    or not path.is_relative_to(root)
                    or path == root
                ):
                    raise ValueError("Unsafe file cleanup path")
                with db.session() as session:
                    download = session.scalar(select(Download).where(Download.save_path == str(root)))
                    if download and entry["path"] in retained_paths(session, download):
                        continue
                path.unlink(missing_ok=True)
            for raw in job["directories"]:
                with db.session() as session:
                    if session.scalar(select(Download.id).where(Download.save_path == raw)):
                        raise ValueError("Download path is in use")
                path = Path(raw)
                if path.resolve() != path or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", path.name):
                    raise ValueError("Unsafe cleanup path")
                if path.exists():
                    shutil.rmtree(path)
            for raw in job["resume_files"]:
                Path(raw).unlink(missing_ok=True)
            with db.session() as session:
                session.execute(delete(ConfigEntry).where(ConfigEntry.key == key))
        except (OSError, ValueError):
            pending = True
    return pending


async def delete_selection(worker, identity, user_id, *, media_id=None):
    from types import SimpleNamespace
    from lazarr.replacement import retire_selections

    async with worker.lock, worker.poll_lock, worker.download_lock:
        with worker.db.session() as db:
            published = []
            if media_id is None:
                sub = db.get(Subtask, identity)
                if sub is None:
                    raise ValueError("Серия не найдена")
                subs = [sub]
            else:
                media = db.get(Media, media_id)
                episode_id = None if identity == "movie" else int(identity)
                episode = db.get(Episode, episode_id) if episode_id else None
                if (
                    not media
                    or (
                        media.kind == "tv"
                        and (not episode or db.get(Season, episode.season_id).media_id != media_id)
                    )
                    or (media.kind != "tv" and identity != "movie")
                ):
                    raise ValueError("Серия не найдена")
                subs = list(
                    db.scalars(
                        select(Subtask)
                        .join(Task)
                        .where(Task.media_id == media_id, Subtask.episode_id == episode_id)
                    )
                )
                published = list(
                    db.scalars(
                        select(LibraryAsset).where(
                            LibraryAsset.media_id == media_id, LibraryAsset.episode_id == episode_id
                        )
                    )
                )
            await retire_selections(
                worker,
                db,
                [SimpleNamespace(subtask_id=sub.id) for sub in subs],
                None,
                published=published if not subs else None,
            )
            for sub in subs:
                sub.status = "removed"
                sub.lease_until = sub.next_search_at = 0
                sub.last_error = None
                sub.missing_subtitle_languages = []
            audit(db, user_id, "subtask.delete_selection", str(identity))
        worker.probe_cache.clear()
        pending = await asyncio.to_thread(cleanup, worker.db)
        await worker.restore(reset_leases=False)
    return {"ok": True, "cleanup_pending": pending}


async def delete_season(worker, media_id, number, user_id):
    from types import SimpleNamespace
    from lazarr.library import LibraryService
    from lazarr.replacement import retire_selections

    async with worker.lock, worker.poll_lock, worker.download_lock:
        detail = LibraryService(worker.db, worker.plugins, worker.service).detail(media_id)
        if not detail or detail["kind"] != "tv":
            raise ValueError("Сериал не найден")
        episode_ids = {e["id"] for e in detail["episodes"] if e["season"] == number}
        with worker.db.session() as db:
            task = db.scalar(select(Task).where(Task.media_id == media_id))
            subs = (
                list(
                    db.scalars(
                        select(Subtask).where(Subtask.task_id == task.id, Subtask.episode_id.in_(episode_ids))
                    )
                )
                if task
                else []
            )
            published = list(
                db.scalars(
                    select(LibraryAsset).where(
                        LibraryAsset.media_id == media_id, LibraryAsset.episode_id.in_(episode_ids)
                    )
                )
            )
            await retire_selections(
                worker, db, [SimpleNamespace(subtask_id=s.id) for s in subs], None, published=published
            )
            ids = {s.id for s in subs}
            db.execute(delete(CandidateDecision).where(CandidateDecision.subtask_id.in_(ids)))
            db.execute(delete(Subtask).where(Subtask.id.in_(ids)))
            if task:
                for membership in list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id))):
                    season = db.get(Season, membership.season_id)
                    affected = membership.numbering.get("season", season.number) == number
                    if not membership.numbering:
                        affected |= any(
                            db.get(Episode, identity).season_id == season.id for identity in episode_ids
                        )
                    if affected:
                        other = db.scalar(
                            select(Subtask.id)
                            .join(Episode)
                            .where(Subtask.task_id == task.id, Episode.season_id == season.id)
                        )
                        if not membership.numbering and other:
                            # Canonical selections may span multiple displayed seasons.
                            membership.whole_season = False
                        else:
                            db.delete(membership)
                db.flush()
                remaining = list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id)))
                if not remaining and not db.scalar(select(Subtask.id).where(Subtask.task_id == task.id)):
                    for entry in db.scalars(
                        select(ConfigEntry).where(ConfigEntry.key.startswith("search.request."))
                    ):
                        if entry.value.get("task_id") == task.id:
                            db.delete(entry)
                    db.delete(task)
                else:
                    task.season_id = remaining[0].season_id if len(remaining) == 1 else None
                    task.numbering = remaining[0].numbering if len(remaining) == 1 else {}
                    task.whole_season = all(m.whole_season for m in remaining)
                    task.updated_by, task.updated_at = user_id, time.time()
            audit(db, user_id, "season.delete", str(media_id), {"season": number})
        worker.probe_cache.clear()
        pending = await asyncio.to_thread(cleanup, worker.db)
        await worker.restore(reset_leases=False)
    return {"ok": True, "cleanup_pending": pending}


async def delete_task(worker, identity, user_id, delete_media=False):
    async with worker.lock, worker.poll_lock, worker.download_lock:
        with worker.db.session() as db:
            task = db.get(Task, identity)
            if task is None:
                raise ValueError("Задача не найдена")
            ids = set(db.scalars(select(Subtask.id).where(Subtask.task_id == identity)))
            downloads = list(db.scalars(select(Download)))
            affected, removable = [], []
            for download in downloads:
                links = set(
                    db.scalars(
                        select(SubtaskAsset.subtask_id)
                        .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .where(MediaAsset.download_id == download.id)
                    )
                )
                consumers = links | {b["subtask_id"] for b in download.plan.get("bindings", [])}
                if not consumers & ids:
                    continue
                affected.append(download)
                if not consumers - ids:
                    removable.append(download)
            paths = [managed_directory(d) for d in removable] if delete_media else []
            for download in removable:
                if worker.engine and worker.engine.contains(download.infohash):
                    if delete_media:
                        await asyncio.to_thread(worker.engine.remove, download.infohash)
                    else:
                        await asyncio.to_thread(worker.engine.pause, download.infohash)
            db.execute(delete(CandidateDecision).where(CandidateDecision.subtask_id.in_(ids)))
            db.execute(delete(SubtaskAsset).where(SubtaskAsset.subtask_id.in_(ids)))
            removable_ids = {download.id for download in removable}
            removable_asset_ids = set(
                db.scalars(select(MediaAsset.id).where(MediaAsset.download_id.in_(removable_ids)))
            )
            if delete_media and removable_asset_ids:
                db.execute(delete(LibraryAsset).where(LibraryAsset.asset_id.in_(removable_asset_ids)))
            for download in affected:
                download.plan = {
                    **download.plan,
                    "bindings": [b for b in download.plan.get("bindings", []) if b["subtask_id"] not in ids],
                }
                if download in removable:
                    if delete_media:
                        db.execute(delete(MediaAsset).where(MediaAsset.download_id == download.id))
                        db.delete(download)
                    else:
                        download.state = "stopped"
                        download.manual_paused = True
            db.execute(delete(Subtask).where(Subtask.id.in_(ids)))
            for entry in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith("search.request."))):
                if entry.value.get("task_id") == identity:
                    db.delete(entry)
            db.delete(task)
            if paths:
                db.add(
                    ConfigEntry(
                        key="cleanup." + uuid4().hex,
                        value={
                            "directories": [str(p) for p in paths],
                            "resume_files": [
                                str(worker.config.data_dir / "torrent_state" / f"{d.infohash}.resume")
                                for d in removable
                            ],
                        },
                    )
                )
            audit(
                db,
                user_id,
                "task.delete",
                str(identity),
                {"delete_media": delete_media, "shared_downloads_kept": len(affected) - len(removable)},
            )
        pending = await asyncio.to_thread(cleanup, worker.db)
    await worker.sync_consumers()
    return {"ok": True, "cleanup_pending": pending, "shared_downloads_kept": len(affected) - len(removable)}


async def delete_media(worker, identity, user_id, delete_files=False):
    """Remove a shared Media and all of its tasks, preserving files by default."""
    async with worker.lock, worker.poll_lock, worker.download_lock:
        with worker.db.session() as db:
            media = db.get(Media, identity)
            if media is None:
                raise ValueError("Произведение не найдено")
            task_ids = set(db.scalars(select(Task.id).where(Task.media_id == identity)))
            subtask_ids = set(db.scalars(select(Subtask.id).where(Subtask.task_id.in_(task_ids))))
            asset_ids = set(db.scalars(select(MediaAsset.id).where(MediaAsset.media_id == identity)))
            asset_download_ids = set(
                db.scalars(select(MediaAsset.download_id).where(MediaAsset.id.in_(asset_ids)))
            )
            affected, removable = [], []
            for download in db.scalars(select(Download)):
                consumers = set(
                    db.scalars(
                        select(SubtaskAsset.subtask_id)
                        .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .where(MediaAsset.download_id == download.id)
                    )
                ) | {b["subtask_id"] for b in download.plan.get("bindings", [])}
                if download.id not in asset_download_ids and not consumers & subtask_ids:
                    continue
                affected.append(download)
                has_other_media = bool(
                    db.scalar(
                        select(MediaAsset.id).where(
                            MediaAsset.download_id == download.id,
                            MediaAsset.media_id != identity,
                        )
                    )
                )
                if not consumers - subtask_ids and not has_other_media:
                    removable.append(download)

            paths = [managed_directory(d) for d in removable] if delete_files else []
            for download in removable:
                if worker.engine and worker.engine.contains(download.infohash):
                    await asyncio.to_thread(worker.engine.remove, download.infohash)

            if subtask_ids:
                db.execute(delete(CandidateDecision).where(CandidateDecision.subtask_id.in_(subtask_ids)))
                db.execute(delete(SubtaskAsset).where(SubtaskAsset.subtask_id.in_(subtask_ids)))
            if asset_ids:
                db.execute(delete(SubtaskAsset).where(SubtaskAsset.asset_id.in_(asset_ids)))
                db.execute(delete(LibraryAsset).where(LibraryAsset.asset_id.in_(asset_ids)))
            removable_ids = {d.id for d in removable}
            for download in affected:
                if download.id in removable_ids:
                    continue
                download.plan = {
                    **download.plan,
                    "bindings": [
                        b for b in download.plan.get("bindings", []) if b["subtask_id"] not in subtask_ids
                    ],
                }
            if removable_ids:
                db.execute(delete(MediaAsset).where(MediaAsset.download_id.in_(removable_ids)))
                db.execute(delete(Download).where(Download.id.in_(removable_ids)))
            if asset_ids:
                db.execute(delete(MediaAsset).where(MediaAsset.id.in_(asset_ids)))
            if subtask_ids:
                db.execute(delete(Subtask).where(Subtask.id.in_(subtask_ids)))
            for entry in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith("search.request."))):
                if entry.value.get("task_id") in task_ids:
                    db.delete(entry)
            if task_ids:
                db.execute(delete(Task).where(Task.id.in_(task_ids)))
            season_ids = set(db.scalars(select(Season.id).where(Season.media_id == identity)))
            episode_ids = set(db.scalars(select(Episode.id).where(Episode.season_id.in_(season_ids))))
            from lazarr.jellyfin import object_id

            playback_ids = {object_id("media", identity)} | {
                object_id("episode", episode_id) for episode_id in episode_ids
            }
            playback_ids.update(object_id("asset", asset_id) for asset_id in asset_ids)
            # Public season numbers may differ from provider numbers (anime numbering).
            season_prefix = object_id("season", identity)[:-8]
            playback_ids.update(
                db.scalars(
                    select(PlaybackProgress.item_id).where(PlaybackProgress.item_id.startswith(season_prefix))
                )
            )
            playback_ids.update(
                object_id("season", identity, season.number)
                for season in db.scalars(select(Season).where(Season.media_id == identity))
            )
            playback_ids.update(
                db.scalars(select(PlaybackSession.source_id).where(PlaybackSession.item_id.in_(playback_ids)))
            )
            db.execute(delete(PlaybackProgress).where(PlaybackProgress.item_id.in_(playback_ids)))
            db.execute(delete(PlaybackSession).where(PlaybackSession.item_id.in_(playback_ids)))
            for playlist in db.scalars(select(VideoPlaylist)):
                playlist.entries = [
                    entry for entry in playlist.entries if entry["item_id"] not in playback_ids
                ]
            if season_ids:
                db.execute(delete(Episode).where(Episode.season_id.in_(season_ids)))
                db.execute(delete(Season).where(Season.id.in_(season_ids)))
            db.delete(media)
            if paths:
                db.add(
                    ConfigEntry(
                        key="cleanup." + uuid4().hex,
                        value={
                            "directories": [str(p) for p in paths],
                            "resume_files": [
                                str(worker.config.data_dir / "torrent_state" / f"{d.infohash}.resume")
                                for d in removable
                            ],
                        },
                    )
                )
            audit(
                db,
                user_id,
                "media.delete",
                str(identity),
                {
                    "delete_files": delete_files,
                    "tasks_deleted": len(task_ids),
                    "shared_downloads_kept": len(affected) - len(removable),
                },
            )
        pending = await asyncio.to_thread(cleanup, worker.db)
    await worker.sync_consumers()
    return {
        "ok": True,
        "cleanup_pending": pending,
        "tasks_deleted": len(task_ids),
        "shared_downloads_kept": len(affected) - len(removable),
    }
