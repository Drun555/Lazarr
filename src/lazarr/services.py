import time
from sqlalchemy import select, update, text
from pydantic import BaseModel, Field
from lazarr.search import enqueue
from lazarr.config import Settings, Requirements
from lazarr.models import (
    ConfigEntry,
    Media,
    Season,
    Episode,
    Task,
    TaskSeason,
    Subtask,
    SubtaskAsset,
    MediaAsset,
    CandidateDecision,
    Release,
    Download,
)
from lazarr.sdk import MetadataItem, SeasonInfo, SubtaskRequest
from lazarr.security import audit


class SeasonSelection(BaseModel):
    season: int = Field(ge=0)
    episodes: list[int] | None = None
    numbering_season: int | None = Field(default=None, ge=1)


class CreateTask(BaseModel):
    provider: str = "tmdb"
    media_id: str
    kind: str = Field(pattern="^(movie|tv)$")
    season: int | None = Field(default=None, ge=0)
    episodes: list[int] | None = None
    requirements: Requirements | None = None
    numbering_season: int | None = Field(default=None, ge=1)
    seasons: list[SeasonSelection] | None = Field(default=None, min_length=1, max_length=200)

    def selections(self):
        legacy = self.season is not None or self.episodes is not None or self.numbering_season is not None
        if self.kind == "movie":
            if legacy or self.seasons is not None:
                raise ValueError("Фильм не содержит сезоны или список серий")
            return []
        if self.seasons is not None:
            if legacy:
                raise ValueError("Укажите seasons либо season, но не оба поля")
            selections = self.seasons
        elif self.season is not None:
            selections = [
                SeasonSelection(
                    season=self.season, episodes=self.episodes, numbering_season=self.numbering_season
                )
            ]
        else:
            raise ValueError("Выберите хотя бы один сезон")
        keys = [(s.numbering_season is not None, s.numbering_season or s.season) for s in selections]
        if len(keys) != len(set(keys)):
            raise ValueError("Сезон указан несколько раз")
        return selections


def resolve_numbering(payload, item):
    if payload.numbering_season is None:
        return payload, {}
    matches = {}
    seasons = set()
    sources = set()
    for key, aliases in item.episode_numbering.items():
        canonical_season, episode = map(int, key.split(":"))
        for alias in aliases:
            if alias["season"] == payload.numbering_season:
                seasons.add(canonical_season)
                matches[str(episode)] = alias["episode"]
                sources.add(alias["source"])
    if len(seasons) != 1 or len(sources) != 1 or len(set(matches.values())) != len(matches):
        raise ValueError("Нет однозначного соответствия сезона; выберите нумерацию TMDB")
    if payload.episodes is not None and (
        not payload.episodes or not set(payload.episodes) <= set(matches.values())
    ):
        raise ValueError("Указаны неизвестные или отсутствующие серии")
    selected = [
        int(key) for key, number in matches.items() if payload.episodes is None or number in payload.episodes
    ]
    numbering = {"season": payload.numbering_season, "episodes": matches, "source": next(iter(sources))}
    return payload.model_copy(update={"season": next(iter(seasons)), "episodes": selected}), numbering


class TaskService:
    def __init__(self, db, plugins):
        self.db, self.plugins = db, plugins

    def settings(self):
        with self.db.session() as db:
            row = db.get(ConfigEntry, "app")
            if not row:
                settings = Settings()
                db.add(ConfigEntry(key="app", value=settings.model_dump()))
                return settings
            return Settings.model_validate(row.value)

    def set_settings(self, settings, user_id):
        with self.db.session() as db:
            row = db.get(ConfigEntry, "app")
            if row:
                if Settings.model_validate(row.value).search_start != settings.search_start:
                    db.execute(update(Subtask).values(next_search_at=0))
                row.value = settings.model_dump()
            else:
                db.add(ConfigEntry(key="app", value=settings.model_dump()))
            audit(db, user_id, "settings.update", "app")

    async def create(self, payload: CreateTask, user_id):
        selections = payload.selections()
        async with self.plugins.open(payload.provider) as provider:
            item = await provider.get_media(payload.kind, payload.media_id)
            season_infos = {}
            for selection in selections:
                canonical, _ = resolve_numbering(selection, item)
                if canonical.season not in season_infos:
                    season_infos[canonical.season] = await provider.get_season(
                        payload.media_id, canonical.season
                    )
        return self.create_from_metadata(payload, item, list(season_infos.values()), user_id)

    def create_from_metadata(self, payload, item, season_info, user_id):
        selections = payload.selections()
        infos = season_info if isinstance(season_info, list) else [season_info] if season_info else []
        infos = {info.number: info for info in infos}
        resolved = []
        occupied = set()
        for selection in selections:
            canonical, numbering = resolve_numbering(selection, item)
            info = infos.get(canonical.season)
            if info is None:
                raise ValueError("Не получены сведения о выбранном сезоне")
            numbers = {e.number for e in info.episodes}
            if canonical.episodes is not None:
                if not canonical.episodes or not set(canonical.episodes) <= numbers:
                    raise ValueError("Указаны неизвестные или отсутствующие серии")
                numbers = set(canonical.episodes)
            parts = {(canonical.season, n) for n in numbers}
            if occupied & parts:
                raise ValueError("Выбранные сезоны содержат одни и те же серии; выберите одну нумерацию")
            occupied.update(parts)
            resolved.append((selection, canonical, numbering, info))
        requirements = payload.requirements or self.settings().defaults
        with self.db.session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            media = db.scalar(
                select(Media).where(
                    Media.provider == item.provider, Media.external_id == item.id, Media.kind == item.kind
                )
            )
            if not media:
                media = Media(
                    provider=item.provider,
                    external_id=item.id,
                    kind=item.kind,
                    title=item.title,
                    year=item.year,
                    metadata_json=item.model_dump(),
                )
                db.add(media)
                db.flush()
            else:
                media.metadata_json = item.model_dump()
                media.title, media.year = item.title, item.year
            task = db.scalar(select(Task).where(Task.media_id == media.id))
            if task is None:
                task = Task(
                    media_id=media.id,
                    created_by=user_id,
                    updated_by=user_id,
                    requirements=requirements.model_dump(),
                )
                db.add(task)
                db.flush()
            elif payload.requirements is not None and task.requirements != payload.requirements.model_dump():
                raise ValueError("У медиа уже есть задача. Измените её требования в карточке медиа")
            existing = set(db.scalars(select(Subtask.episode_id).where(Subtask.task_id == task.id)))
            for selection, canonical, numbering, info in resolved:
                season = self._upsert_season(db, media.id, info)
                key = f"alt:{selection.numbering_season}" if numbering else str(season.number)
                membership = db.scalar(
                    select(TaskSeason).where(TaskSeason.task_id == task.id, TaskSeason.selection_key == key)
                )
                if membership is None:
                    membership = TaskSeason(
                        task_id=task.id,
                        season_id=season.id,
                        selection_key=key,
                        whole_season=selection.episodes is None,
                        numbering=numbering,
                    )
                    db.add(membership)
                else:
                    membership.whole_season |= selection.episodes is None
                    membership.numbering = numbering
                for episode in db.scalars(select(Episode).where(Episode.season_id == season.id)):
                    if episode.id not in existing and (
                        canonical.episodes is None or episode.number in canonical.episodes
                    ):
                        db.add(
                            Subtask(task_id=task.id, episode_id=episode.id, part_key=f"episode:{episode.id}")
                        )
                        existing.add(episode.id)
            if not resolved and None not in existing:
                db.add(Subtask(task_id=task.id, part_key="movie"))
            memberships = list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id)))
            task.season_id = memberships[0].season_id if len(memberships) == 1 else None
            task.numbering = memberships[0].numbering if len(memberships) == 1 else {}
            task.whole_season = all(m.whole_season for m in memberships)
            task.updated_by, task.updated_at = user_id, time.time()
            enqueue(db, task.id)
            audit(db, user_id, "task.create", str(task.id))
            task_id = task.id
        return task_id

    async def add_season(self, media_id, selection, user_id):
        with self.db.session() as db:
            media = db.get(Media, media_id)
            if not media or media.kind != "tv":
                raise ValueError("Сериал не найден")
            payload = CreateTask(
                provider=media.provider, media_id=media.external_id, kind="tv", seasons=[selection]
            )
        return await self.create(payload, user_id)

    def _upsert_season(self, db, media_id, info: SeasonInfo):
        season = db.scalar(select(Season).where(Season.media_id == media_id, Season.number == info.number))
        if not season:
            season = Season(media_id=media_id, number=info.number, title=info.title)
            db.add(season)
            db.flush()
        season.title, season.refreshed_at = info.title, time.time()
        for item in info.episodes:
            episode = db.scalar(
                select(Episode).where(Episode.season_id == season.id, Episode.number == item.number)
            )
            if not episode:
                episode = Episode(season_id=season.id, number=item.number)
                db.add(episode)
            episode.external_id, episode.title, episode.air_date = item.id, item.title, item.air_date
            episode.overview, episode.still = item.overview, item.still
            episode.absolute_number = item.absolute_number
        db.flush()
        return season

    async def refresh_seasons(self):
        with self.db.session() as db:
            rows = [
                (s.id, m.provider, m.external_id, s.number)
                for s, m in db.execute(
                    select(Season, Media)
                    .join(Media, Season.media_id == Media.id)
                    .where(Season.refreshed_at < time.time() - 86400)
                )
            ]
        for season_id, provider_id, external_id, number in rows:
            with self.db.session() as db:
                active = db.scalar(
                    select(Task.id)
                    .join(TaskSeason)
                    .where(TaskSeason.season_id == season_id, Task.paused.is_(False))
                )
                has_numbering = any(
                    t.numbering
                    for t in db.scalars(select(TaskSeason).where(TaskSeason.season_id == season_id))
                )
            if active is None:
                continue
            try:
                async with self.plugins.open(provider_id) as provider:
                    info = await provider.get_season(external_id, number)
                    item = await provider.get_media("tv", external_id) if has_numbering else None
                with self.db.session() as db:
                    season = db.get(Season, season_id)
                    self._upsert_season(db, season.media_id, info)
                    if item:
                        db.get(Media, season.media_id).metadata_json = item.model_dump()
                    for membership in db.scalars(
                        select(TaskSeason).where(
                            TaskSeason.season_id == season_id, TaskSeason.whole_season.is_(True)
                        )
                    ):
                        task = db.get(Task, membership.task_id)
                        existing = set(
                            db.scalars(select(Subtask.episode_id).where(Subtask.task_id == task.id))
                        )
                        allowed = None
                        if membership.numbering:
                            selection, numbering = resolve_numbering(
                                CreateTask(
                                    media_id=external_id,
                                    kind="tv",
                                    season=number,
                                    numbering_season=membership.numbering["season"],
                                ),
                                item,
                            )
                            if (
                                selection.season != number
                                or numbering["source"] != membership.numbering["source"]
                            ):
                                continue
                            membership.numbering = numbering
                            allowed = set(selection.episodes)
                        for episode in db.scalars(select(Episode).where(Episode.season_id == season_id)):
                            if episode.id not in existing and (allowed is None or episode.number in allowed):
                                db.add(
                                    Subtask(
                                        task_id=task.id,
                                        episode_id=episode.id,
                                        part_key=f"episode:{episode.id}",
                                    )
                                )
            except Exception:
                # Provider diagnostics retain the actionable failure; defer refresh for one hour.
                with self.db.session() as db:
                    db.get(Season, season_id).refreshed_at = time.time() - 23 * 3600

    def request_for(self, db, subtask):
        task = db.get(Task, subtask.task_id)
        media = db.get(Media, task.media_id)
        episode = db.get(Episode, subtask.episode_id) if subtask.episode_id else None
        season = db.get(Season, episode.season_id) if episode else None
        return SubtaskRequest(
            id=subtask.id,
            media=MetadataItem.model_validate(media.metadata_json),
            season=season.number if season else None,
            episode=episode.number if episode else None,
            absolute_number=episode.absolute_number if episode else None,
            air_date=episode.air_date if episode else None,
            requirements=Requirements.model_validate(task.requirements),
        )

    def list_tasks(self):
        with self.db.session() as db:
            result = []
            for task in db.scalars(select(Task).order_by(Task.created_at.desc())):
                media = db.get(Media, task.media_id)
                memberships = list(
                    db.scalars(
                        select(TaskSeason).where(TaskSeason.task_id == task.id).order_by(TaskSeason.id)
                    )
                )
                seasons = {m.season_id: db.get(Season, m.season_id) for m in memberships}
                season_rows = sorted(
                    [
                        {
                            "season": m.numbering.get("season", seasons[m.season_id].number),
                            "canonical_season": seasons[m.season_id].number,
                            "whole_season": m.whole_season,
                            "numbering_season": m.numbering.get("season"),
                        }
                        for m in memberships
                    ],
                    key=lambda row: row["season"],
                )
                parts = []
                for sub in db.scalars(select(Subtask).where(Subtask.task_id == task.id).order_by(Subtask.id)):
                    episode = db.get(Episode, sub.episode_id) if sub.episode_id else None
                    season = seasons.get(episode.season_id) if episode else None
                    membership = next(
                        (
                            m
                            for m in memberships
                            if episode
                            and m.season_id == episode.season_id
                            and (not m.numbering or str(episode.number) in m.numbering.get("episodes", {}))
                        ),
                        None,
                    )
                    numbering = membership.numbering if membership else {}
                    current = db.scalar(
                        select(MediaAsset)
                        .join(SubtaskAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .where(SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True))
                    )
                    parts.append(
                        {
                            "id": sub.id,
                            "season": numbering.get("season", season.number) if season else None,
                            "canonical_season": season.number if season else None,
                            "episode": numbering.get("episodes", {}).get(str(episode.number), episode.number)
                            if episode
                            else None,
                            "canonical_episode": episode.number if episode else None,
                            "title": episode.title if episode else media.title,
                            "air_date": episode.air_date
                            if episode
                            else media.metadata_json.get("release_date"),
                            "status": "paused" if task.paused and sub.status != "done" else sub.status,
                            "next_search_at": sub.next_search_at,
                            "error": sub.last_error,
                            "missing_subtitle_languages": sub.missing_subtitle_languages,
                            "current_resolution": current.resolution if current else None,
                        }
                    )
                result.append(
                    {
                        "id": task.id,
                        "media_id": media.id,
                        "title": media.title,
                        "year": media.year,
                        "poster": media.metadata_json.get("poster"),
                        "kind": media.kind,
                        "seasons": season_rows,
                        "season": season_rows[0]["season"] if len(season_rows) == 1 else None,
                        "canonical_season": season_rows[0]["canonical_season"]
                        if len(season_rows) == 1
                        else None,
                        "created_by": task.created_by,
                        "paused": task.paused,
                        "completed": bool(parts) and all(p["status"] == "done" for p in parts),
                        "whole_season": all(m.whole_season for m in memberships),
                        "requirements": task.requirements,
                        "subtasks": sorted(parts, key=lambda p: (p["season"] or 0, p["episode"] or 0)),
                    }
                )
            return result

    def edit(self, task_id, user_id, *, requirements=None, paused=None):
        with self.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError("Задача не найдена")
            if requirements is not None:
                task.requirements = requirements.model_dump()
                # A new requirements revision invalidates earlier decisions/overrides.
                for sub in db.scalars(select(Subtask).where(Subtask.task_id == task_id)):
                    if sub.status == "done" or db.scalar(
                        select(SubtaskAsset.id).where(
                            SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True)
                        )
                    ):
                        continue
                    sub.status, sub.next_search_at, sub.last_error = (
                        "removed" if sub.status == "removed" else "queued",
                        0,
                        None,
                    )
                    sub.missing_subtitle_languages = []
                    for link in db.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id == sub.id)):
                        link.pending, link.current = False, False
                    for decision in db.scalars(
                        select(CandidateDecision).where(CandidateDecision.subtask_id == sub.id)
                    ):
                        selected = db.scalar(
                            select(SubtaskAsset.id)
                            .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                            .join(Download, MediaAsset.download_id == Download.id)
                            .where(
                                SubtaskAsset.subtask_id == sub.id, Download.release_id == decision.release_id
                            )
                        )
                        if selected:
                            decision.action = "evaluated"
                        else:
                            db.delete(decision)
                enqueue(db, task_id)
            if paused is not None:
                task.paused = paused
                if not paused:
                    for sub in db.scalars(select(Subtask).where(Subtask.task_id == task_id)):
                        sub.next_search_at = 0
                    enqueue(db, task_id)
            task.updated_by, task.updated_at = user_id, time.time()
            audit(db, user_id, "task.update", str(task_id))

    def candidates(self, subtask_id):
        with self.db.session() as db:
            result = []
            for decision, release in db.execute(
                select(CandidateDecision, Release)
                .join(Release)
                .where(CandidateDecision.subtask_id == subtask_id)
            ):
                candidate = dict(release.data)
                candidate.pop("magnet", None)
                candidate.pop("download_url", None)
                result.append(
                    {
                        "id": decision.id,
                        "candidate": candidate,
                        "report": decision.report,
                        "action": decision.action,
                    }
                )
            return result

    def task_candidates(self, task_id):
        """Return each release once, with its existing per-episode evaluations."""
        with self.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError("Задача не найдена")
            subtasks = list(db.scalars(select(Subtask).where(Subtask.task_id == task_id)))
            subtask_ids = [sub.id for sub in subtasks]
            if not subtask_ids:
                return []
            grouped = {}
            rows = db.execute(
                select(CandidateDecision, Release, Subtask, Episode)
                .join(Release, CandidateDecision.release_id == Release.id)
                .join(Subtask, CandidateDecision.subtask_id == Subtask.id)
                .outerjoin(Episode, Subtask.episode_id == Episode.id)
                .where(CandidateDecision.subtask_id.in_(subtask_ids))
                .order_by(Release.id, Episode.number, Subtask.id)
            )
            for decision, release, subtask, episode in rows:
                item = grouped.get(release.id)
                if item is None:
                    candidate = dict(release.data)
                    candidate.pop("magnet", None)
                    candidate.pop("download_url", None)
                    item = grouped[release.id] = {
                        "id": decision.id,
                        "release_id": release.id,
                        "candidate": candidate,
                        "total": len(subtasks),
                        "episodes": [],
                    }
                report = decision.report or {}
                item["episodes"].append(
                    {
                        "subtask_id": subtask.id,
                        "episode": episode.number if episode else None,
                        "season": db.get(Season, episode.season_id).number if episode else None,
                        "title": episode.title if episode else task_id,
                        "result": report.get("result", "UNKNOWN"),
                        "has_binding": bool(report.get("binding")),
                        "action": decision.action,
                    }
                )
            result = list(grouped.values())
            for item in result:
                item["matched"] = sum(
                    episode["has_binding"] and episode["result"] != "MISMATCH" for episode in item["episodes"]
                )
            return sorted(
                result,
                key=lambda item: (
                    -item["matched"],
                    -(item["candidate"].get("seeds") or 0),
                    item["candidate"].get("size") or 2**63,
                    item["release_id"],
                ),
            )
