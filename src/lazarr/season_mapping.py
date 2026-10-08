"""Season-wide editor over the existing per-episode download bindings."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from lazarr.search_runtime import engine_call, pinned
from lazarr.specials import catalog_for, placement, refresh_catalog
from typing import Literal

from lazarr.models import (
    CandidateDecision,
    ConfigEntry,
    Download,
    Episode,
    MediaAsset,
    Release,
    Season,
    Subtask,
    SubtaskAsset,
    Task,
)
from lazarr.sdk import DownloadSource, TorrentFile
from lazarr.release_files import mapping_catalog, remap_binding, file_identity
from lazarr.security import audit


def file_kind(file):
    return engine_call("associations", "file_kind", file)


def related_files(files, bindings=()):
    return engine_call("associations", "related_files", files, bindings)


def scope(db, task_id, season_number, *, include_deleted=False):
    task = db.get(Task, task_id)
    season = (
        db.scalar(select(Season).where(Season.media_id == task.media_id, Season.number == season_number))
        if task
        else None
    )
    if not season:
        raise HTTPException(404, "Сезон задачи не найден")
    rows = list(
        db.execute(
            select(Subtask, Episode)
            .join(Episode, Subtask.episode_id == Episode.id)
            .where(Subtask.task_id == task_id, Episode.season_id == season.id)
            .order_by(Episode.number)
        )
    )
    if not include_deleted:
        deleted = set(
            db.scalars(
                select(ConfigEntry.key).where(
                    ConfigEntry.key.in_([f"episode_deleted.{episode.id}" for _, episode in rows])
                )
            )
        )
        rows = [(sub, episode) for sub, episode in rows if f"episode_deleted.{episode.id}" not in deleted]
    return task, season, rows


def releases_key(task, season):
    return f"season_mapping.{task.id}.{task.created_at}.{season}"


async def inspect(ctx, release):
    path = ctx.config.data_dir / "torrents" / f"{release.revision}.torrent"
    if ctx.engine is None:
        raise HTTPException(503, ctx.engine_error)
    if not path.is_file():
        raise ValueError(f"Нет метаданных раздачи: {release.data.get('title', release.id)}")
    return await asyncio.to_thread(ctx.engine.inspect, DownloadSource(torrent=path.read_bytes()))


class EpisodeInput(BaseModel):
    number: int | None = Field(default=None, ge=1, le=10000)
    title: str = Field(min_length=1, max_length=500)


class SpecialPosition(BaseModel):
    mode: Literal["auto", "manual"] = "auto"
    airsbefore_season: int | None = Field(default=None, ge=1, le=10000)
    airsafter_season: int | None = Field(default=None, ge=1, le=10000)
    airsbefore_episode: int | None = Field(default=None, ge=1, le=10000)


class MappingRow(BaseModel):
    special_position: SpecialPosition | None = None
    subtask_id: int
    number: int | None = Field(default=None, ge=1, le=10000)
    title: str = Field(min_length=1, max_length=500)
    release_id: int | None = None
    video_index: int | None = Field(default=None, ge=0)
    track_indices: list[int] = Field(default_factory=list, max_length=1000)


class MappingInput(BaseModel):
    background: bool = False
    request_id: UUID | None = None
    pool_release_ids: list[int] | None = Field(default=None, max_length=10000)
    deleted_subtask_ids: list[int] = Field(default_factory=list, max_length=10000)
    season_title: str | None = Field(default=None, max_length=500)
    revisions: dict[int, str] = Field(default_factory=dict)
    rows: list[MappingRow] = Field(max_length=10000)


class ReleaseInput(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    candidate_id: int | None = Field(default=None, ge=1)


def register(app, context, authenticated, permission):
    @app.get("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping")
    @pinned
    async def get_mapping(task_id: int, season_number: int, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        if season_number == 0:
            with ctx.db.session() as db:
                task, _, _ = scope(db, task_id, season_number)
                media_id = task.media_id
            await refresh_catalog(ctx.service, media_id)
        with ctx.db.session() as db:
            task, season, rows = scope(db, task_id, season_number)
            catalog = catalog_for(db, task.media_id) if season_number == 0 else []
            season_title = season.title
            saved = db.get(ConfigEntry, releases_key(task, season_number))
            hidden_release_ids = saved.value.get("hidden_releases", []) if saved else []
            release_ids = set(saved.value.get("releases", []) if saved else [])
            episodes = []
            snapshots = {}
            bindings = {}
            selected_links = list(
                db.execute(
                    select(SubtaskAsset, MediaAsset.download_id)
                    .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                    .where(
                        SubtaskAsset.subtask_id.in_([sub.id for sub, _ in rows]),
                        (SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True)),
                    )
                    .order_by(SubtaskAsset.pending.desc(), SubtaskAsset.id.desc())
                )
            )
            downloads = {
                d.id: d
                for d in db.scalars(
                    select(Download).where(Download.id.in_({identity for _, identity in selected_links}))
                )
            }
            links_by_subtask = {}
            for link, identity in selected_links:
                links_by_subtask.setdefault(link.subtask_id, []).append((link, downloads[identity]))
            for sub, episode in rows:
                links = links_by_subtask.get(sub.id, [])
                release_ids.update(download.release_id for _, download in links)
                for link, download in links:
                    snapshots[download.id] = (
                        download.release_id,
                        download.infohash,
                        download.plan.get("files", []),
                    )
                link, download = links[0] if links else (None, None)
                release_id = download.release_id if download else None
                binding = link.preflight.get("binding") if link else None
                if binding:
                    bindings[sub.id] = (binding, download.plan.get("files", []), download.infohash)
                episodes.append(
                    {
                        "subtask_id": sub.id,
                        "number": episode.number,
                        "title": episode.title,
                        "release_id": release_id if binding else None,
                        "binding": binding,
                        **(
                            {"special_position": placement(db, episode, catalog)}
                            if season_number == 0
                            else {}
                        ),
                    }
                )
            releases = [db.get(Release, identity) for identity in sorted(release_ids)]
        result = []
        for release in releases:
            if not release:
                continue
            metadata = await inspect(ctx, release)
            file_catalog = mapping_catalog(
                release.revision,
                [file.model_dump() for file in metadata.files],
                [
                    (revision, files)
                    for identity, revision, files in snapshots.values()
                    if identity == release.id
                ],
            )
            for episode in episodes:
                if episode["release_id"] == release.id and episode["subtask_id"] in bindings:
                    binding, files, revision = bindings[episode["subtask_id"]]
                    episode["binding"] = remap_binding(binding, files, file_catalog) if files else binding
            groups = await asyncio.to_thread(
                related_files,
                [TorrentFile.model_validate(file) for file in file_catalog],
                [
                    episode["binding"]
                    for episode in episodes
                    if episode["release_id"] == release.id and episode["binding"]
                ],
            )
            result.append(
                {
                    "id": release.id,
                    "title": release.data.get("title", "Раздача"),
                    "revision": release.revision,
                    "url": release.data.get("url"),
                    "files": [
                        {
                            **file,
                            "kind": file_kind(TorrentFile.model_validate(file)),
                            "related": groups.get(file["index"], []),
                            "legacy": file["revision"] != release.revision,
                        }
                        for file in file_catalog
                    ],
                }
            )
        return {
            "job": await asyncio.to_thread(ctx.mapping_jobs.latest, task_id, season_number),
            "season_title": season_title,
            "placement_seasons": catalog,
            "episodes": episodes,
            "releases": result,
            "hidden_release_ids": hidden_release_ids,
        }

    @app.post("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping/releases")
    async def add_release(
        task_id: int,
        season_number: int,
        payload: ReleaseInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        with ctx.db.session() as db:
            task, _, _ = scope(db, task_id, season_number)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        if (payload.url is None) == (payload.candidate_id is None):
            raise ValueError("Укажите URL или существующую раздачу")
        if payload.candidate_id is not None:
            with ctx.db.session() as db:
                _, _, rows = scope(db, task_id, season_number, include_deleted=True)
                decision = db.get(CandidateDecision, payload.candidate_id)
                if not decision or decision.subtask_id not in {sub.id for sub, _ in rows}:
                    raise ValueError("Раздача не принадлежит сезону")
                release = db.get(Release, decision.release_id)
            await inspect(ctx, release)
            release_id = release.id
        else:
            release_id = await ctx.worker.add_manual_task_candidate(
                task_id, payload.url, season_number, return_release=True
            )
        with ctx.db.session() as db:
            key = releases_key(task, season_number)
            entry = db.get(ConfigEntry, key)
            ids = entry.value.get("releases", []) if entry else []
            value = {
                "releases": sorted(set(ids + [release_id])),
                "hidden_releases": [
                    identity
                    for identity in (entry.value.get("hidden_releases", []) if entry else [])
                    if identity != release_id
                ],
            }
            if entry:
                entry.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
            audit(db, user.id, "season_mapping.release", str(task_id), {"season": season_number, **value})
        return {"ok": True, "release_id": release_id}

    @app.post("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping/releases/{release_id}/report")
    @pinned
    async def release_report(
        task_id: int,
        season_number: int,
        release_id: int,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        from lazarr.mapping_report import collect_report
        from lazarr.sdk import Candidate

        ctx = context(request)
        snapshot = await get_mapping(task_id, season_number, request, user)
        release = next((item for item in snapshot["releases"] if item["id"] == release_id), None)
        if release is None:
            raise HTTPException(404, "Раздача не принадлежит сезону")
        with ctx.db.session() as db:
            _, _, rows = scope(db, task_id, season_number)
            requests = [ctx.service.request_for(db, sub) for sub, _ in rows]
            candidate = Candidate.model_validate(db.get(Release, release_id).data)
        return await collect_report(ctx, candidate, requests, snapshot["episodes"], release)

    @app.post("/api/v1/tasks/{task_id}/search/report")
    @pinned
    async def search_report(task_id: int, request: Request, user=Depends(permission("tasks"))):
        from datetime import datetime, timezone
        from lazarr.mapping_report import clean
        from lazarr.models import Media
        from lazarr.sdk import MetadataItem
        from lazarr.search_runtime import current_engine

        ctx = context(request)
        with ctx.db.session() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "Задача не найдена")
            media = MetadataItem.model_validate(db.get(Media, task.media_id).metadata_json)
            rows = list(db.scalars(select(Subtask).where(Subtask.task_id == task_id).order_by(Subtask.id)))
            requests = [
                ctx.service.request_for(db, sub).model_dump(mode="json", exclude={"media"}) for sub in rows
            ]
            episodes = [
                {"subtask_id": sub.id, "title": db.get(Episode, sub.episode_id).title}
                for sub in rows
                if sub.episode_id
            ]
            task_data = {
                "media": media.model_dump(
                    mode="json",
                    include={
                        "id",
                        "provider",
                        "kind",
                        "title",
                        "original_title",
                        "year",
                        "aliases",
                        "external_ids",
                        "episode_numbering",
                        "seasons",
                        "overview",
                    },
                ),
                "requirements": task.requirements,
                "paused": task.paused,
                "requests": requests,
                "episodes": episodes,
            }
        snapshot = ctx.scheduler.snapshot(task_id)
        return clean(
            {
                "schema_version": 1,
                "issue_type": "search_flow",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "engine": {"version": current_engine().version, "identity": current_engine().identity},
                "task": task_data,
                "search": {
                    key: snapshot.get(key)
                    for key in (
                        "state",
                        "stage",
                        "running",
                        "started_at",
                        "updated_at",
                        "groups_total",
                        "groups_done",
                        "pages_checked",
                        "candidates_found",
                        "candidates_checked",
                        "candidates_filtered",
                        "candidates_failed",
                        "pending_requests",
                        "next_attempt_at",
                    )
                },
                "providers": [
                    {key: provider.get(key) for key in ("id", "name", "enabled", "state")}
                    for provider in ctx.plugins.search_status()
                ],
            }
        )

    @app.post("/api/v1/candidates/{identity}/report")
    @pinned
    async def candidate_report(
        identity: int,
        request: Request,
        scope: str = "episode",
        user=Depends(permission("tasks")),
    ):
        from lazarr.mapping_report import collect_report
        from lazarr.sdk import Candidate

        if scope not in {"episode", "season"}:
            raise HTTPException(422, "Неизвестная область отчёта")
        ctx = context(request)
        with ctx.db.session() as db:
            decision = db.get(CandidateDecision, identity)
            if not decision:
                raise HTTPException(404, "Кандидат не найден")
            sub = db.get(Subtask, decision.subtask_id)
            if not sub:
                raise HTTPException(404, "Серия не найдена")
            release = db.get(Release, decision.release_id)
            candidate = Candidate.model_validate(release.data)
            episode = db.get(Episode, sub.episode_id) if sub.episode_id else None
            if scope == "season" and episode:
                rows = list(
                    db.execute(
                        select(Subtask, Episode)
                        .join(Episode, Subtask.episode_id == Episode.id)
                        .where(Subtask.task_id == sub.task_id, Episode.season_id == episode.season_id)
                        .order_by(Episode.number)
                    )
                )
            else:
                rows = [(sub, episode)]
            requests = [ctx.service.request_for(db, item) for item, _ in rows]
            episodes = [
                {
                    "subtask_id": item.id,
                    "number": ep.number if ep else None,
                    "title": ep.title if ep else requests[0].media.title,
                }
                for item, ep in rows
            ]
            files = release.files
        if not files:
            metadata = await inspect(ctx, release)
            files = [file.model_dump() for file in metadata.files]
        report = await collect_report(
            ctx,
            candidate,
            requests,
            [],
            {
                "revision": release.revision,
                "files": files,
            },
        )
        report["task"]["episodes"] = episodes
        report["scope"] = scope
        return report

    @app.post("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping/episodes")
    async def add_episode(
        task_id: int,
        season_number: int,
        payload: EpisodeInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        async with ctx.worker.lock:
            with ctx.db.session() as db:
                _, season, rows = scope(db, task_id, season_number)
                # Draft rows are appended under a free number first. The mapping
                # save then applies the final numbers together, allowing insertion.
                number = payload.number
                if number is None:
                    number = (
                        max(
                            db.scalars(select(Episode.number).where(Episode.season_id == season.id)),
                            default=0,
                        )
                        + 1
                    )
                if any(episode.number == number for _, episode in rows):
                    raise ValueError("Эпизод с таким номером уже добавлен")
                episode = db.scalar(
                    select(Episode).where(Episode.season_id == season.id, Episode.number == number)
                )
                if not episode:
                    episode = Episode(season_id=season.id, number=number, title=payload.title.strip())
                    db.add(episode)
                    db.flush()
                if not payload.title.strip():
                    raise ValueError("Укажите имя эпизода")
                episode.title = payload.title.strip()
                key = f"episode_title.{episode.id}"
                entry = db.get(ConfigEntry, key)
                if entry:
                    entry.value = {"title": episode.title}
                else:
                    db.add(ConfigEntry(key=key, value={"title": episode.title}))
                deleted = db.get(ConfigEntry, f"episode_deleted.{episode.id}")
                if deleted:
                    db.delete(deleted)
                sub = db.scalar(
                    select(Subtask).where(Subtask.task_id == task_id, Subtask.episode_id == episode.id)
                )
                if sub:
                    sub.status = "queued"
                else:
                    sub = Subtask(task_id=task_id, episode_id=episode.id, part_key=f"episode:{episode.id}")
                    db.add(sub)
                db.flush()
                audit(db, user.id, "season_mapping.episode", str(sub.id))
                result = {"subtask_id": sub.id, "number": episode.number, "title": episode.title}
        return result

    @app.put("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping")
    @pinned
    async def save_mapping(
        task_id: int,
        season_number: int,
        payload: MappingInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        from lazarr.mapping_jobs import current_job

        if payload.background:
            async with ctx.mapping_jobs.accept_lock:
                job = await asyncio.to_thread(
                    ctx.mapping_jobs.enqueue, task_id, season_number, payload, user.id
                )
            ctx.mapping_jobs.wake.set()
            return JSONResponse({"ok": True, "job": job}, status_code=202)
        if current_job.get() is None:
            jobs = await asyncio.to_thread(ctx.mapping_jobs.snapshot)
            if any(job["task_id"] == task_id and job["state"] in {"queued", "running"} for job in jobs):
                raise HTTPException(409, "Сопоставление задачи уже применяется")
        await ctx.mapping_jobs.progress("Проверка сохранённого плана и файлов")
        snapshot = await get_mapping(task_id, season_number, request, user)
        existing = {row["subtask_id"]: row for row in snapshot["episodes"]}
        deleted_ids = set(payload.deleted_subtask_ids)
        with ctx.db.session() as db:
            _, _, all_rows = scope(db, task_id, season_number, include_deleted=True)
            if not deleted_ids <= {sub.id for sub, _ in all_rows}:
                raise ValueError("Удаляемая серия не принадлежит сезону")
        if deleted_ids & {row.subtask_id for row in payload.rows}:
            raise ValueError("Нельзя одновременно сохранить и удалить серию")
        releases = {release["id"]: release for release in snapshot["releases"]}
        if payload.pool_release_ids is not None and not set(payload.pool_release_ids) <= releases.keys():
            raise ValueError("Раздача не принадлежит редактору сезона")
        if any(
            identity not in releases or releases[identity]["revision"] != revision
            for identity, revision in payload.revisions.items()
        ):
            raise ValueError(
                "Раздача обновилась после открытия редактора. Откройте сопоставление заново, чтобы загрузить актуальные файлы."
            )
        seen = set()
        # Validate the whole edit before making any download changes.
        for row in payload.rows:
            if row.subtask_id < 0 and row.number is not None:
                existing[row.subtask_id] = {"release_id": None, "binding": None}
            if row.subtask_id not in existing or row.subtask_id in seen:
                raise ValueError("Серия не принадлежит сезону или указана дважды")
            seen.add(row.subtask_id)
            position = row.special_position
            if position is not None:
                if season_number != 0:
                    raise ValueError("Порядок показа доступен только для спецматериалов")
                if position.airsbefore_season and position.airsafter_season:
                    raise ValueError("Выберите только одну позицию спецэпизода")
                if position.airsbefore_episode and not position.airsbefore_season:
                    raise ValueError("Для позиции перед эпизодом укажите сезон")
                if position.mode == "manual":
                    target = position.airsbefore_season or position.airsafter_season
                    target_season = next(
                        (s for s in snapshot["placement_seasons"] if s["number"] == target), None
                    )
                    if target and target_season is None:
                        raise ValueError("Выбран неизвестный сезон")
                    if position.airsbefore_episode and position.airsbefore_episode not in {
                        e["number"] for e in target_season["episodes"]
                    }:
                        raise ValueError("Выбран неизвестный эпизод")
            if not row.title.strip():
                raise ValueError("Укажите имя эпизода")
            if row.video_index is None:
                if row.release_id is not None or row.track_indices:
                    raise ValueError("Для дорожек необходимо выбрать видео")
                continue
            release = releases.get(row.release_id)
            files = {file["index"]: file for file in release["files"]} if release else {}
            if files.get(row.video_index, {}).get("kind") != "video":
                raise ValueError("Выберите видео из раздач этого сезона")
            if any(
                files.get(index, {}).get("kind") not in {"audio", "subtitle"} for index in row.track_indices
            ):
                raise ValueError("Дорожки должны принадлежать раздаче видео")
        with ctx.db.session() as db:
            task, season, scoped_rows = scope(db, task_id, season_number)
            episodes = {sub.id: episode for sub, episode in scoped_rows}
            spare = (
                max(db.scalars(select(Episode.number).where(Episode.season_id == season.id)), default=0) + 1
            )
            for row in payload.rows:
                if row.subtask_id >= 0:
                    continue
                episode = Episode(season_id=season.id, number=spare, title=row.title.strip())
                spare += 1
                db.add(episode)
                db.flush()
                sub = Subtask(task_id=task_id, episode_id=episode.id, part_key=f"episode:{episode.id}")
                db.add(sub)
                db.flush()
                existing[sub.id] = existing.pop(row.subtask_id)
                row.subtask_id = sub.id
                episodes[sub.id] = episode
            changes = {
                episodes[row.subtask_id].id: row.number
                for row in payload.rows
                if row.number is not None and row.number != episodes[row.subtask_id].number
            }
            season_episodes = list(db.scalars(select(Episode).where(Episode.season_id == season.id)))
            retired = {episodes[identity].id for identity in deleted_ids if identity in episodes}
            retired.update(
                episode.id
                for episode in season_episodes
                if db.get(ConfigEntry, f"episode_deleted.{episode.id}")
            )
            # Deleted rows keep their IDs and history, but must not reserve visible
            # numbers when the remaining rows are compacted after a deletion.
            occupied = {
                changes.get(episode.id, episode.number)
                for episode in season_episodes
                if episode.id not in retired
            }
            spare = max([0, *occupied, *(episode.number for episode in season_episodes)]) + 1
            for episode in season_episodes:
                if episode.id in retired:
                    if episode.number in occupied:
                        changes[episode.id] = spare
                        spare += 1
                    occupied.add(changes.get(episode.id, episode.number))
            numbers = [changes.get(episode.id, episode.number) for episode in season_episodes]
            if len(numbers) != len(set(numbers)):
                raise ValueError("Номера эпизодов в сезоне должны быть уникальными")
            if payload.season_title is not None and payload.season_title.strip() != season.title:
                season.title = payload.season_title.strip()
                title_key = f"season_title.{season.id}"
                title_entry = db.get(ConfigEntry, title_key)
                if title_entry:
                    title_entry.value = {"title": season.title}
                else:
                    db.add(ConfigEntry(key=title_key, value={"title": season.title}))
            # Temporary numbers allow swaps without violating the unique season/number key.
            temporary = min([0, *(episode.number for episode in season_episodes)]) - 1
            for episode in season_episodes:
                if episode.id not in changes:
                    continue
                key = f"episode_number.{episode.id}"
                entry = db.get(ConfigEntry, key)
                if not entry:
                    db.add(ConfigEntry(key=key, value={"original": episode.number}))
                episode.number = temporary
                temporary -= 1
            db.flush()
            for episode in season_episodes:
                if episode.id in changes:
                    episode.number = changes[episode.id]
            key = releases_key(task, season_number)
            entry = db.get(ConfigEntry, key)
            pool_ids = (
                set(payload.pool_release_ids)
                if payload.pool_release_ids is not None
                else set(releases) - set(snapshot["hidden_release_ids"])
            )
            value = {"releases": sorted(pool_ids), "hidden_releases": sorted(set(releases) - pool_ids)}
            if entry:
                entry.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
            ctx.mapping_jobs.persist_payload(db, payload)
        completed = []
        try:
            choices = []
            # Submit replacements before removals so shared files remain protected.
            for row in sorted(payload.rows, key=lambda row: row.video_index is None):
                old = existing[row.subtask_id]
                binding = old["binding"] or {}
                old_tracks = sorted(
                    t["file_index"] for t in binding.get("tracks", []) if t.get("file_index") is not None
                )
                changed = (
                    row.release_id != old["release_id"]
                    or row.video_index != binding.get("video_index")
                    or sorted(set(row.track_indices)) != old_tracks
                )
                if changed and row.video_index is not None:
                    with ctx.db.session() as db:
                        decision = db.scalar(
                            select(CandidateDecision).where(
                                CandidateDecision.subtask_id == row.subtask_id,
                                CandidateDecision.release_id == row.release_id,
                            )
                        )
                        if not decision:
                            decision = CandidateDecision(
                                subtask_id=row.subtask_id, release_id=row.release_id, report={}
                            )
                            db.add(decision)
                            db.flush()
                        identity = decision.id
                    catalog = {file["index"]: file for file in releases[row.release_id]["files"]}
                    video = catalog[row.video_index]
                    revision = video["revision"]
                    if revision == releases[row.release_id]["revision"]:
                        target_files = [file for file in catalog.values() if file["revision"] == revision]
                    else:
                        with ctx.db.session() as db:
                            download = db.scalar(
                                select(Download).where(
                                    Download.release_id == row.release_id, Download.infohash == revision
                                )
                            )
                            target_files = download.plan.get("files", [])
                    indices = {
                        file_identity(file): file.get("source_index", file["index"]) for file in target_files
                    }
                    if any(file_identity(catalog[index]) not in indices for index in row.track_indices):
                        raise ValueError("Выбранные дорожки отсутствуют в версии торрента этого видео")
                    choices.append(
                        dict(
                            decision_id=identity,
                            video_index=video["source_index"],
                            track_indices=[
                                indices[file_identity(catalog[index])]
                                for index in sorted(set(row.track_indices))
                            ],
                            **(
                                {"revision": revision}
                                if revision != releases[row.release_id]["revision"]
                                else {}
                            ),
                        )
                    )
            if choices:
                await ctx.mapping_jobs.progress(
                    f"Применение {len(choices)} сопоставлений пакетами по раздачам", 0
                )
                await ctx.worker.choose_many(choices, user.id)
            for row in payload.rows:
                old = existing[row.subtask_id]
                if row.video_index is None and old["binding"]:
                    from lazarr.deletion import delete_selection

                    await delete_selection(ctx.worker, row.subtask_id, user.id)
            # Titles and placement belong to one season edit, not one transaction per episode.
            with ctx.db.session() as db:
                for row in payload.rows:
                    sub = db.get(Subtask, row.subtask_id)
                    episode = db.get(Episode, sub.episode_id)
                    if row.special_position is not None:
                        key = f"special_position.{episode.id}"
                        entry = db.get(ConfigEntry, key)
                        if row.special_position.mode == "auto":
                            if entry:
                                db.delete(entry)
                        else:
                            value = row.special_position.model_dump(exclude={"mode"}, exclude_none=True)
                            if entry:
                                entry.value = value
                            else:
                                db.add(ConfigEntry(key=key, value=value))
                    if episode.title != row.title.strip():
                        episode.title = row.title.strip()
                        key = f"episode_title.{episode.id}"
                        entry = db.get(ConfigEntry, key)
                        if entry:
                            entry.value = {"title": episode.title}
                        else:
                            db.add(ConfigEntry(key=key, value={"title": episode.title}))
                    audit(db, user.id, "season_mapping.save", str(row.subtask_id))
            completed.extend(row.subtask_id for row in payload.rows)
            await ctx.mapping_jobs.progress(f"Применено серий: {len(completed)}", len(completed))
            # Assign returned files first, then retire the deleted rows using shared-file protection.
            from lazarr.deletion import delete_selection

            for identity in deleted_ids:
                await ctx.mapping_jobs.progress("Удаление исключённых серий", len(completed))
                with ctx.db.session() as db:
                    sub = db.get(Subtask, identity)
                    if db.get(ConfigEntry, f"episode_deleted.{sub.episode_id}"):
                        continue
                await delete_selection(ctx.worker, identity, user.id)
                with ctx.db.session() as db:
                    sub = db.get(Subtask, identity)
                    db.add(
                        ConfigEntry(
                            key=f"episode_deleted.{sub.episode_id}", value={"episode_id": sub.episode_id}
                        )
                    )
                    audit(db, user.id, "season_mapping.delete_episode", str(identity))
        except Exception as exc:
            raise ValueError(
                f"Сохранено серий: {len(completed)}. {exc}. Повторите сохранение для оставшихся изменений."
            ) from exc
        from lazarr.storage import reconcile

        await ctx.mapping_jobs.progress("Обновление файлов медиатеки", len(completed))
        await asyncio.to_thread(reconcile, ctx.db)
        ctx.scheduler.discard_satisfied()
        return {"ok": True}

    async def apply_job(job):
        payload = MappingInput.model_validate(job["payload"]).model_copy(update={"background": False})
        await save_mapping(
            job["task_id"],
            job["season_number"],
            payload,
            Request({"type": "http", "app": app}),
            SimpleNamespace(id=job["owner_id"]),
        )

    app.state.apply_mapping_job = apply_job

    @app.post("/api/v1/mapping-jobs/{identity}/retry")
    async def retry_mapping(identity: UUID, request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        async with ctx.mapping_jobs.accept_lock:
            job = await asyncio.to_thread(ctx.mapping_jobs.retry, str(identity))
        ctx.mapping_jobs.wake.set()
        return {"ok": True, "job": job}
