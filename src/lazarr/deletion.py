"""Task deletion with shared-download protection and durable file-cleanup retries."""

import asyncio
import re
import shutil
from pathlib import Path
from uuid import uuid4
from sqlalchemy import delete, select
from lazarr.models import (
    Task,
    Subtask,
    SubtaskAsset,
    MediaAsset,
    Download,
    CandidateDecision,
    ConfigEntry,
    Media,
    Season,
    Episode,
    PlaybackProgress,
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
            db.execute(delete(PlaybackProgress).where(PlaybackProgress.item_id.in_(playback_ids)))
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
