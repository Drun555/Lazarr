import time
from sqlalchemy import select, update
from pydantic import BaseModel, Field
from lazarr.search import enqueue
from lazarr.config import Settings, Requirements
from lazarr.models import (
    ConfigEntry,
    Media,
    Season,
    Episode,
    Task,
    Subtask,
    SubtaskAsset,
    MediaAsset,
    CandidateDecision,
    Release,
)
from lazarr.sdk import MetadataItem, SeasonInfo, SubtaskRequest
from lazarr.security import audit


class CreateTask(BaseModel):
    provider: str = "tmdb"
    media_id: str
    kind: str = Field(pattern="^(movie|tv)$")
    season: int | None = Field(default=None, ge=0)
    episodes: list[int] | None = None
    requirements: Requirements | None = None
    numbering_season: int | None = Field(default=None, ge=1)


def resolve_numbering(payload, item):
    if payload.numbering_season is None:
        return payload, {}
    if payload.kind != "tv":
        raise ValueError("Альтернативная нумерация доступна только для сериала")
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
        if payload.kind == "tv" and payload.season is None:
            raise ValueError("Выберите сезон")
        if payload.kind == "movie" and (payload.season is not None or payload.episodes is not None):
            raise ValueError("Фильм не содержит сезон или список серий")
        async with self.plugins.open(payload.provider) as provider:
            item = await provider.get_media(payload.kind, payload.media_id)
            canonical, _ = resolve_numbering(payload, item)
            season_info = (
                await provider.get_season(payload.media_id, canonical.season)
                if payload.kind == "tv"
                else None
            )
        return self.create_from_metadata(payload, item, season_info, user_id)

    def create_from_metadata(self, payload, item, season_info, user_id):
        whole_season = payload.episodes is None
        payload, numbering = resolve_numbering(payload, item)
        requirements = payload.requirements or self.settings().defaults
        if payload.episodes is not None:
            if (
                not payload.episodes
                or not season_info
                or not set(payload.episodes) <= {e.number for e in season_info.episodes}
            ):
                raise ValueError("Указаны неизвестные или отсутствующие серии")
        with self.db.session() as db:
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
            season = self._upsert_season(db, media.id, season_info) if season_info else None
            task = Task(
                media_id=media.id,
                season_id=season.id if season else None,
                created_by=user_id,
                updated_by=user_id,
                requirements=requirements.model_dump(),
                whole_season=whole_season,
                numbering=numbering,
            )
            db.add(task)
            db.flush()
            if season:
                for episode in db.scalars(select(Episode).where(Episode.season_id == season.id)):
                    if payload.episodes is None or episode.number in payload.episodes:
                        db.add(
                            Subtask(task_id=task.id, episode_id=episode.id, part_key=f"episode:{episode.id}")
                        )
            else:
                db.add(Subtask(task_id=task.id, part_key="movie"))
            enqueue(db, task.id)
            audit(db, user_id, "task.create", str(task.id))
            task_id = task.id
        return task_id

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
                active = db.scalar(select(Task.id).where(Task.season_id == season_id, Task.paused.is_(False)))
                has_numbering = any(
                    t.numbering for t in db.scalars(select(Task).where(Task.season_id == season_id))
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
                    for task in db.scalars(
                        select(Task).where(Task.season_id == season_id, Task.whole_season.is_(True))
                    ):
                        existing = set(
                            db.scalars(select(Subtask.episode_id).where(Subtask.task_id == task.id))
                        )
                        allowed = None
                        if task.numbering:
                            selection, numbering = resolve_numbering(
                                CreateTask(
                                    media_id=external_id,
                                    kind="tv",
                                    season=number,
                                    numbering_season=task.numbering["season"],
                                ),
                                item,
                            )
                            if selection.season != number or numbering["source"] != task.numbering["source"]:
                                continue
                            task.numbering = numbering
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
        season = db.get(Season, task.season_id) if task.season_id else None
        current = db.scalar(
            select(MediaAsset)
            .join(SubtaskAsset, SubtaskAsset.asset_id == MediaAsset.id)
            .where(SubtaskAsset.subtask_id == subtask.id, SubtaskAsset.current.is_(True))
        )
        return SubtaskRequest(
            id=subtask.id,
            media=MetadataItem.model_validate(media.metadata_json),
            season=season.number if season else None,
            episode=episode.number if episode else None,
            absolute_number=episode.absolute_number if episode else None,
            air_date=episode.air_date if episode else None,
            requirements=Requirements.model_validate(task.requirements),
            current_resolution=current.resolution if current else None,
        )

    def list_tasks(self):
        with self.db.session() as db:
            result = []
            for task in db.scalars(select(Task).order_by(Task.created_at.desc())):
                media = db.get(Media, task.media_id)
                season = db.get(Season, task.season_id) if task.season_id else None
                parts = []
                for sub in db.scalars(select(Subtask).where(Subtask.task_id == task.id).order_by(Subtask.id)):
                    episode = db.get(Episode, sub.episode_id) if sub.episode_id else None
                    current = db.scalar(
                        select(MediaAsset)
                        .join(SubtaskAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .where(SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True))
                    )
                    parts.append(
                        {
                            "id": sub.id,
                            "episode": task.numbering.get("episodes", {}).get(
                                str(episode.number), episode.number
                            )
                            if episode
                            else None,
                            "canonical_episode": episode.number if episode else None,
                            "title": episode.title if episode else media.title,
                            "air_date": episode.air_date
                            if episode
                            else media.metadata_json.get("release_date"),
                            "status": "paused" if task.paused else sub.status,
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
                        "season": task.numbering.get("season", season.number) if season else None,
                        "canonical_season": season.number if season else None,
                        "created_by": task.created_by,
                        "paused": task.paused,
                        "whole_season": task.whole_season,
                        "requirements": task.requirements,
                        "subtasks": sorted(parts, key=lambda p: p["episode"] or 0),
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
                    sub.status, sub.next_search_at, sub.last_error = "queued", 0, None
                    sub.missing_subtitle_languages = []
                    for link in db.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id == sub.id)):
                        link.pending, link.current = False, False
                    for decision in db.scalars(
                        select(CandidateDecision).where(CandidateDecision.subtask_id == sub.id)
                    ):
                        db.delete(decision)
            if paused is not None:
                task.paused = paused
                if not paused:
                    for sub in db.scalars(select(Subtask).where(Subtask.task_id == task_id)):
                        sub.next_search_at = 0
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
                        "title": episode.title if episode else task_id,
                        "result": report.get("result", "UNKNOWN"),
                        "has_binding": bool(report.get("binding")),
                        "action": decision.action,
                    }
                )
            result = list(grouped.values())
            for item in result:
                item["matched"] = sum(
                    episode["has_binding"] and episode["result"] != "MISMATCH"
                    for episode in item["episodes"]
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
