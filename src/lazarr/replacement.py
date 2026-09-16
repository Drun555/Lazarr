"""Replace an episode selection, protecting files still used by other episodes."""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from lazarr.deletion import managed_directory
from lazarr.models import (
    ConfigEntry,
    CandidateDecision,
    Download,
    LibraryAsset,
    MediaAsset,
    Subtask,
    SubtaskAsset,
    Task,
)


def binding_paths(binding):
    return {binding["video_path"]} | {
        track["path"]
        for track in binding.get("tracks", [])
        if track.get("file_index") is not None and track.get("path")
    }


def retained_paths(db, download):
    paths = set()
    for binding in download.plan.get("bindings", []):
        paths.update(binding_paths(binding))
    for model in (SubtaskAsset, LibraryAsset):
        for link, asset in db.execute(
            select(model, MediaAsset)
            .join(MediaAsset, model.asset_id == MediaAsset.id)
            .where(MediaAsset.download_id == download.id)
        ):
            paths.add(asset.path)
            if link.preflight.get("binding"):
                paths.update(binding_paths(link.preflight["binding"]))
    return paths


async def retire_selections(worker, db, bindings, target_hash, preserve_link=None, published=None):
    """Called under poll/download locks, before storing the new selection."""
    ids = {binding.subtask_id for binding in bindings}
    links = list(db.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id.in_(ids))))
    published = list(published or [])
    for sub in db.scalars(select(Subtask).where(Subtask.id.in_(ids))):
        task = db.get(Task, sub.task_id)
        published.extend(
            db.scalars(
                select(LibraryAsset).where(
                    LibraryAsset.media_id == task.media_id,
                    LibraryAsset.part_key == sub.part_key,
                )
            )
        )
    published = list({link.id: link for link in published}.values())
    asset_ids = {link.asset_id for link in links + published}
    download_ids = set(db.scalars(select(MediaAsset.download_id).where(MediaAsset.id.in_(asset_ids))))
    downloads = [
        d
        for d in db.scalars(select(Download))
        if d.id in download_ids or any(b["subtask_id"] in ids for b in d.plan.get("bindings", []))
    ]
    # Release the engine's storage handles before unlinking anything, including
    # when remapping files within the same torrent.
    roots = {d.id: managed_directory(d) for d in downloads}
    for download in downloads:
        if worker.engine and worker.engine.contains(download.infohash):
            await asyncio.to_thread(worker.engine.remove, download.infohash)
    for link in links + published:
        if preserve_link and (
            link is preserve_link
            or isinstance(link, LibraryAsset)
            and preserve_link.current
            and link.asset_id == preserve_link.asset_id
        ):
            continue
        db.delete(link)
    if preserve_link is None:
        for decision in db.scalars(
            select(CandidateDecision).where(
                CandidateDecision.subtask_id.in_(ids),
                CandidateDecision.action == "selected",
            )
        ):
            decision.action = "evaluated"
    db.flush()
    for download in downloads:
        download.plan = {
            **download.plan,
            "bindings": [b for b in download.plan.get("bindings", []) if b["subtask_id"] not in ids],
        }
        if preserve_link and download.infohash == target_hash:
            download.plan = {
                **download.plan,
                "bindings": download.plan["bindings"] + [b.model_dump() for b in bindings],
            }
        for asset in list(db.scalars(select(MediaAsset).where(MediaAsset.download_id == download.id))):
            if not db.scalar(
                select(SubtaskAsset.id).where(SubtaskAsset.asset_id == asset.id)
            ) and not db.scalar(select(LibraryAsset.id).where(LibraryAsset.asset_id == asset.id)):
                db.delete(asset)
        db.flush()
        paths = retained_paths(db, download)
        if download.infohash == target_hash:
            for binding in bindings:
                paths.update(binding_paths(binding.model_dump()))
        removable = not paths and download.infohash != target_hash
        job = {"directories": [], "resume_files": [], "files": []}
        if removable:
            job["directories"] = [str(roots[download.id])]
            job["resume_files"] = [
                str(worker.config.data_dir / "torrent_state" / f"{download.infohash}.resume")
            ]
            db.delete(download)
        else:
            job["files"] = [
                {"root": str(roots[download.id]), "path": f["path"]}
                for f in download.plan.get("files", [])
                if f["path"] not in paths
            ]
            download.stats = {}
        if job["directories"] or job["files"]:
            db.add(ConfigEntry(key="cleanup." + uuid4().hex, value=job))


async def prune_history(worker):
    """Collapse legacy episode versions once, before restoring torrent handles."""
    from lazarr.sdk import FileBinding

    with worker.db.session() as db:
        for sub in db.scalars(select(Subtask)):
            links = list(db.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id == sub.id)))
            if not links:
                continue
            task = db.get(Task, sub.task_id)
            published = list(
                db.scalars(
                    select(LibraryAsset).where(
                        LibraryAsset.media_id == task.media_id,
                        LibraryAsset.part_key == sub.part_key,
                    )
                )
            )
            selected = max(links, key=lambda link: (link.pending, link.current, link.id))
            if len(links) == 1 and all(p.asset_id == selected.asset_id for p in published):
                continue
            binding = selected.preflight.get("binding")
            if not binding:
                continue
            asset = db.get(MediaAsset, selected.asset_id)
            download = db.get(Download, asset.download_id)
            await retire_selections(
                worker, db, [FileBinding.model_validate(binding)], download.infohash, selected
            )
