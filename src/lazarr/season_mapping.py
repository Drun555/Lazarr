"""Season-wide editor over the existing per-episode download bindings."""

import asyncio

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from lazarr.search_runtime import engine_call, pinned

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


def scope(db, task_id, season_number):
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
    if not rows:
        raise HTTPException(404, "В сезоне нет сабтасок")
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
    number: int = Field(ge=1, le=10000)
    title: str = Field(min_length=1, max_length=500)


class MappingRow(BaseModel):
    subtask_id: int
    number: int | None = Field(default=None, ge=1, le=10000)
    title: str = Field(min_length=1, max_length=500)
    release_id: int | None = None
    video_index: int | None = Field(default=None, ge=0)
    track_indices: list[int] = Field(default_factory=list, max_length=1000)


class MappingInput(BaseModel):
    revisions: dict[int, str] = Field(default_factory=dict)
    rows: list[MappingRow] = Field(max_length=10000)


class ReleaseInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def register(app, context, authenticated, permission):
    @app.get("/api/v1/tasks/{task_id}/seasons/{season_number}/mapping")
    @pinned
    async def get_mapping(task_id: int, season_number: int, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            task, _, rows = scope(db, task_id, season_number)
            saved = db.get(ConfigEntry, releases_key(task, season_number))
            release_ids = set(saved.value.get("releases", []) if saved else [])
            episodes = []
            snapshots = {}
            bindings = {}
            for sub, episode in rows:
                links = list(
                    db.execute(
                        select(SubtaskAsset, Download)
                        .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .join(Download, MediaAsset.download_id == Download.id)
                        .where(
                            SubtaskAsset.subtask_id == sub.id,
                            (SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True)),
                        )
                        .order_by(SubtaskAsset.pending.desc(), SubtaskAsset.id.desc())
                    )
                )
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
                    }
                )
            releases = [db.get(Release, identity) for identity in sorted(release_ids)]
        result = []
        for release in releases:
            if not release:
                continue
            metadata = await inspect(ctx, release)
            catalog = mapping_catalog(
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
                    episode["binding"] = remap_binding(binding, files, catalog) if files else binding
            groups = related_files(
                [TorrentFile.model_validate(file) for file in catalog],
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
                        for file in catalog
                    ],
                }
            )
        return {"episodes": episodes, "releases": result}

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
        decision_id = await ctx.worker.add_manual_task_candidate(task_id, payload.url, season_number)
        with ctx.db.session() as db:
            decision = db.get(CandidateDecision, decision_id)
            key = releases_key(task, season_number)
            entry = db.get(ConfigEntry, key)
            ids = entry.value.get("releases", []) if entry else []
            value = {"releases": sorted(set(ids + [decision.release_id]))}
            if entry:
                entry.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
            audit(db, user.id, "season_mapping.release", str(task_id), {"season": season_number, **value})
        return {"ok": True}

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
                if any(episode.number == payload.number for _, episode in rows):
                    raise ValueError("Эпизод с таким номером уже добавлен")
                episode = db.scalar(
                    select(Episode).where(Episode.season_id == season.id, Episode.number == payload.number)
                )
                if not episode:
                    episode = Episode(season_id=season.id, number=payload.number, title=payload.title.strip())
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
        snapshot = await get_mapping(task_id, season_number, request, user)
        existing = {row["subtask_id"]: row for row in snapshot["episodes"]}
        releases = {release["id"]: release for release in snapshot["releases"]}
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
            if row.subtask_id not in existing or row.subtask_id in seen:
                raise ValueError("Серия не принадлежит сезону или указана дважды")
            seen.add(row.subtask_id)
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
            changes = {
                episodes[row.subtask_id].id: row.number
                for row in payload.rows
                if row.number is not None and row.number != episodes[row.subtask_id].number
            }
            season_episodes = list(db.scalars(select(Episode).where(Episode.season_id == season.id)))
            numbers = [changes.get(episode.id, episode.number) for episode in season_episodes]
            if len(numbers) != len(set(numbers)):
                raise ValueError("Номера эпизодов в сезоне должны быть уникальными")
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
            value = {"releases": sorted(releases)}
            if entry:
                entry.value = value
            else:
                db.add(ConfigEntry(key=key, value=value))
        completed = []
        try:
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
                    await ctx.worker.choose(
                        identity,
                        user.id,
                        video_index=video["source_index"],
                        track_indices=[
                            indices[file_identity(catalog[index])] for index in sorted(set(row.track_indices))
                        ],
                        **(
                            {"revision": revision} if revision != releases[row.release_id]["revision"] else {}
                        ),
                    )
                elif changed and old["binding"]:
                    from lazarr.deletion import delete_selection

                    await delete_selection(ctx.worker, row.subtask_id, user.id)
                with ctx.db.session() as db:
                    sub = db.get(Subtask, row.subtask_id)
                    episode = db.get(Episode, sub.episode_id)
                    if episode.title != row.title.strip():
                        episode.title = row.title.strip()
                        key = f"episode_title.{episode.id}"
                        entry = db.get(ConfigEntry, key)
                        if entry:
                            entry.value = {"title": episode.title}
                        else:
                            db.add(ConfigEntry(key=key, value={"title": episode.title}))
                    audit(db, user.id, "season_mapping.save", str(row.subtask_id))
                completed.append(row.subtask_id)
        except Exception as exc:
            raise ValueError(
                f"Сохранено серий: {len(completed)}. {exc}. Повторите сохранение для оставшихся изменений."
            ) from exc
        ctx.scheduler.discard_satisfied()
        return {"ok": True}
