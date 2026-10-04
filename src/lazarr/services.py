from lazarr.search_runtime import current_engine
import time
import calendar
from contextlib import AsyncExitStack
from datetime import datetime, timezone
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
    LibraryAsset,
    MediaAsset,
    CandidateDecision,
    Release,
    Download,
)
from lazarr.sdk import MetadataItem, SeasonInfo, EpisodeInfo, SubtaskRequest
from lazarr.security import audit
from lazarr.season_structure import insertions, local_metadata, local_number, provider_number


class SeasonSelection(BaseModel):
    season: int = Field(ge=0)
    episodes: list[int] | None = None
    numbering_season: int | None = Field(default=None, ge=1)
    manual: bool = False
    title: str | None = Field(default=None, max_length=500)
    season_id: int | None = Field(default=None, ge=1)


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
        keys = [
            (s.manual, s.numbering_season is not None, s.numbering_season or s.season) for s in selections
        ]
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
            from lazarr.specials import fetch_catalog

            catalog = await fetch_catalog(provider, item) if 0 in season_infos else None
        return self.create_from_metadata(payload, item, list(season_infos.values()), user_id, catalog)

    def create_from_metadata(self, payload, item, season_info, user_id, special_catalog=None):
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
            if special_catalog is not None:
                from lazarr.specials import save_catalog

                save_catalog(db, media.id, special_catalog)
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
                if numbering:
                    numbering = {
                        **numbering,
                        "season": local_number(numbering["season"], insertions(db, media.id)),
                    }
                key = f"alt:{numbering['season']}" if numbering else str(season.number)
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
                    requested = canonical.episodes is None or episode.number in canonical.episodes
                    if db.get(ConfigEntry, f"episode_deleted.{episode.id}"):
                        continue
                    if episode.id in existing and requested:
                        sub = db.scalar(
                            select(Subtask).where(
                                Subtask.task_id == task.id, Subtask.episode_id == episode.id
                            )
                        )
                        if sub.status == "removed":
                            current = db.scalar(
                                select(SubtaskAsset.id).where(
                                    SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True)
                                )
                            )
                            sub.status = "done" if current else "queued"
                            sub.next_search_at = 0
                    if episode.id not in existing and requested:
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
            positions = insertions(db, media_id)
            selection = selection.model_copy(
                update={
                    "season": provider_number(selection.season, positions),
                    "numbering_season": provider_number(selection.numbering_season, positions)
                    if selection.numbering_season is not None
                    else None,
                }
            )
            payload = CreateTask(
                provider=media.provider, media_id=media.external_id, kind="tv", seasons=[selection]
            )
        return await self.create(payload, user_id)

    def _upsert_season(self, db, media_id, info: SeasonInfo, *, local=False):
        if not local:
            info = info.model_copy(update={"number": local_number(info.number, insertions(db, media_id))})
        season = db.scalar(select(Season).where(Season.media_id == media_id, Season.number == info.number))
        if not season:
            season = Season(media_id=media_id, number=info.number, title=info.title)
            db.add(season)
            db.flush()
        override = db.get(ConfigEntry, f"season_title.{season.id}") if season.id else None
        season.title = override.value["title"] if override else info.title
        season.refreshed_at = time.time()
        episodes = list(db.scalars(select(Episode).where(Episode.season_id == season.id)))
        by_original_number = {}
        occupied = {episode.number for episode in episodes}
        for episode in episodes:
            override = db.get(ConfigEntry, f"episode_number.{episode.id}")
            original = override.value["original"] if override else episode.number
            by_original_number[original] = episode
        for item in info.episodes:
            episode = by_original_number.get(item.number)
            if not episode:
                # A manual number takes precedence over newly discovered metadata.
                if item.number in occupied:
                    continue
                episode = Episode(season_id=season.id, number=item.number)
                db.add(episode)
                by_original_number[item.number] = episode
                occupied.add(item.number)
            episode.external_id, episode.air_date = item.id, item.air_date
            override = db.get(ConfigEntry, f"episode_title.{episode.id}") if episode.id else None
            if not override or override.value.get("title") != episode.title:
                episode.title = item.title
            episode.overview, episode.still = item.overview, item.still
            episode.absolute_number = item.absolute_number
        db.flush()
        return season

    async def refresh_seasons(self):
        with self.db.session() as db:
            rows = [
                (s.id, m.provider, m.external_id, provider_number(s.number, insertions(db, m.id)))
                for s, m in db.execute(
                    select(Season, Media)
                    .join(Media, Season.media_id == Media.id)
                    .where(
                        Season.refreshed_at < time.time() - 86400,
                        Season.metadata_json["manual"].as_boolean().is_not(True),
                    )
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
                    item = (
                        await provider.get_media("tv", external_id) if has_numbering or number == 0 else None
                    )
                    from lazarr.specials import fetch_catalog, save_catalog

                    catalog = await fetch_catalog(provider, item) if number == 0 else None
                with self.db.session() as db:
                    season = db.get(Season, season_id)
                    self._upsert_season(db, season.media_id, info)
                    if catalog is not None:
                        save_catalog(db, season.media_id, catalog)
                    if item:
                        stored_media = db.get(Media, season.media_id)
                        stored_media.metadata_json = item.model_dump()
                        item = MetadataItem.model_validate(local_metadata(db, stored_media))
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
                                selection.season != season.number
                                or numbering["source"] != membership.numbering["source"]
                            ):
                                continue
                            membership.numbering = numbering
                            allowed = set(selection.episodes)
                        for episode in db.scalars(select(Episode).where(Episode.season_id == season_id)):
                            if db.get(ConfigEntry, f"episode_deleted.{episode.id}"):
                                continue
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
            media=MetadataItem.model_validate(local_metadata(db, media)),
            season=season.number if season else None,
            episode=episode.number if episode else None,
            absolute_number=episode.absolute_number if episode else None,
            air_date=episode.air_date if episode else None,
            requirements=Requirements.model_validate(task.requirements),
        )

    def wait_for_release(self, subtask_id, user_id):
        now = datetime.now(timezone.utc)
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        until = now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))
        with self.db.session() as db:
            subtask = db.get(Subtask, subtask_id)
            if subtask is None or subtask.status != "needs_selection":
                raise ValueError("Серия больше не требует выбора раздачи")
            subtask.selection_hidden_until = until.timestamp()
            audit(db, user_id, "subtask.wait", str(subtask_id))
        return {"hidden_until": until.timestamp()}

    def selection_activity(self):
        """One cheap query; never inspect torrents while polling the top bar."""
        with self.db.session() as db:
            return [
                {
                    "id": sub.id,
                    "task_id": task.id,
                    "title": media.title,
                    "season": season.number,
                    "episode": episode.number,
                    "episode_title": episode.title,
                    "paused": task.paused,
                }
                for sub, task, media, episode, season in db.execute(
                    select(Subtask, Task, Media, Episode, Season)
                    .join(Task, Task.id == Subtask.task_id)
                    .join(Media, Media.id == Task.media_id)
                    .join(Episode, Episode.id == Subtask.episode_id)
                    .join(Season, Season.id == Episode.season_id)
                    .where(
                        Subtask.status == "needs_selection",
                        Subtask.selection_hidden_until <= time.time(),
                    )
                    .order_by(Media.title, Season.number, Episode.number, Subtask.id)
                )
            ]

    def download_activity(self):
        """Compact live download summary for Tasks; no torrent plans or paths."""
        with self.db.session() as db:
            rows = db.execute(
                select(Download, Release.data)
                .join(Release, Release.id == Download.release_id)
                .where(Download.state.in_({"starting", "downloading", "paused", "error"}))
                .order_by(Download.created_at, Download.id)
            )
            return [
                {
                    "id": download.id,
                    "title": release.get("title") or f"Загрузка #{download.id}",
                    "state": download.state,
                    "progress": download.stats.get("progress", 0),
                    "download_rate": download.stats.get("download_rate", 0),
                    "eta": download.stats.get("eta"),
                }
                for download, release in rows
                if not download.stats.get("complete")
            ]

    def list_tasks(self, media_id=None):
        with self.db.session() as db:
            query = select(Task).order_by(Task.created_at.desc())
            if media_id is not None:
                query = query.where(Task.media_id == media_id)
            tasks = list(db.scalars(query))
            if not tasks:
                return []
            task_ids = [task.id for task in tasks]
            media_rows = {
                m.id: m for m in db.scalars(select(Media).where(Media.id.in_({t.media_id for t in tasks})))
            }
            memberships_by_task = {}
            memberships_all = list(
                db.scalars(select(TaskSeason).where(TaskSeason.task_id.in_(task_ids)).order_by(TaskSeason.id))
            )
            for membership in memberships_all:
                memberships_by_task.setdefault(membership.task_id, []).append(membership)
            seasons_all = {
                s.id: s
                for s in db.scalars(
                    select(Season).where(Season.id.in_({m.season_id for m in memberships_all}))
                )
            }
            subs_query = (
                select(Subtask)
                .where(Subtask.task_id.in_(task_ids), Subtask.status != "removed")
                .order_by(Subtask.id)
            )
            subs_by_task = {}
            for sub in db.scalars(subs_query):
                subs_by_task.setdefault(sub.task_id, []).append(sub)
            sub_ids = select(Subtask.id).where(Subtask.task_id.in_(task_ids))
            episodes = {
                e.id: e
                for e in db.scalars(
                    select(Episode).where(
                        Episode.id.in_(select(Subtask.episode_id).where(Subtask.task_id.in_(task_ids)))
                    )
                )
            }
            assets = {}
            for sub_id, asset in db.execute(
                select(SubtaskAsset.subtask_id, MediaAsset)
                .join(MediaAsset, MediaAsset.id == SubtaskAsset.asset_id)
                .where(SubtaskAsset.subtask_id.in_(sub_ids), SubtaskAsset.current.is_(True))
                .order_by(SubtaskAsset.id)
            ):
                assets.setdefault(sub_id, asset)
            recommendations = {}
            pending_ids = [
                sub.id for rows in subs_by_task.values() for sub in rows if sub.status == "needs_selection"
            ]
            if pending_ids:
                for decision in db.scalars(
                    select(CandidateDecision)
                    .where(
                        CandidateDecision.subtask_id.in_(pending_ids),
                        CandidateDecision.action.not_in(["rejected", "selected"]),
                    )
                    .order_by(CandidateDecision.id)
                ):
                    report = decision.report or {}
                    previous = recommendations.get(decision.subtask_id)
                    if report.get("manual_candidate") and (
                        previous is None or report.get("score", 0) > previous.report.get("score", 0)
                    ):
                        recommendations[decision.subtask_id] = decision
            result = []
            for task in tasks:
                media = media_rows[task.media_id]
                memberships = memberships_by_task.get(task.id, [])
                seasons = {
                    membership.season_id: seasons_all[membership.season_id] for membership in memberships
                }
                season_rows = sorted(
                    [
                        {
                            "season": m.numbering.get("season", seasons[m.season_id].number),
                            "canonical_season": seasons[m.season_id].number,
                            "whole_season": m.whole_season,
                            "numbering_season": m.numbering.get("season"),
                            "manual": bool((seasons[m.season_id].metadata_json or {}).get("manual")),
                            "title": seasons[m.season_id].title,
                            "season_id": m.season_id,
                        }
                        for m in memberships
                    ],
                    key=lambda row: row["season"],
                )
                parts = []
                for sub in subs_by_task.get(task.id, []):
                    episode = episodes.get(sub.episode_id)
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
                    current = assets.get(sub.id)
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
                            "needs_mapping": sub.status == "needs_selection"
                            and sub.id in recommendations
                            and bool(recommendations[sub.id].report.get("needs_mapping")),
                            "recommended_candidate_id": recommendations[sub.id].id
                            if sub.status == "needs_selection" and sub.id in recommendations
                            else None,
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

    async def prepare_selections(self, task_id, selections):
        with self.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError("Задача не найдена")
            media = db.get(Media, task.media_id)
            database_media_id = media.id
            payload = CreateTask(
                provider=media.provider, media_id=media.external_id, kind=media.kind, seasons=selections
            )
        resolved, occupied, keys = [], set(), set()
        async with AsyncExitStack() as stack:
            provider = None
            if any(not selection.manual for selection in selections):
                provider = await stack.enter_async_context(self.plugins.open(payload.provider))
            remote_item = (
                await provider.get_media(payload.kind, payload.media_id)
                if provider
                else MetadataItem.model_validate(media.metadata_json)
            )
            media.metadata_json = remote_item.model_dump()
            with self.db.session() as db:
                positions = insertions(db, database_media_id)
                item = MetadataItem.model_validate(local_metadata(db, media))
            for selection in payload.selections():
                key = (
                    selection.manual,
                    selection.numbering_season is not None,
                    selection.numbering_season or selection.season,
                )
                if key in keys:
                    raise ValueError("Сезон указан несколько раз")
                keys.add(key)
                if selection.manual:
                    if selection.numbering_season is not None or selection.episodes is not None:
                        raise ValueError("Для ручного сезона укажите только номер и название")
                    title = (selection.title or f"Сезон {selection.season}").strip()
                    if not title:
                        raise ValueError("Укажите название сезона")
                    with self.db.session() as db:
                        stored = (
                            db.get(Season, selection.season_id)
                            if selection.season_id
                            else db.scalar(
                                select(Season).where(
                                    Season.media_id == database_media_id,
                                    Season.number == selection.season,
                                )
                            )
                        )
                        if selection.season_id and (
                            not stored
                            or stored.media_id != database_media_id
                            or not (stored.metadata_json or {}).get("manual")
                            or stored.number != selection.season
                        ):
                            raise ValueError("Ручной сезон изменился; откройте редактор заново")
                        if stored and not (stored.metadata_json or {}).get("manual"):
                            stored = None
                        selection = selection.model_copy(update={"season_id": stored.id if stored else None})
                        episodes = (
                            list(
                                db.scalars(
                                    select(Episode)
                                    .where(Episode.season_id == stored.id)
                                    .order_by(Episode.number)
                                )
                            )
                            if stored
                            else []
                        )
                        episode_info = [
                            EpisodeInfo(
                                id=episode.external_id or f"manual:{episode.id}",
                                number=episode.number,
                                title=episode.title,
                                overview=episode.overview,
                                still=episode.still,
                                air_date=episode.air_date,
                                absolute_number=episode.absolute_number,
                            )
                            for episode in episodes
                        ]
                    info = SeasonInfo(number=selection.season, title=title, episodes=episode_info)
                    resolved.append((selection, {}, info, {episode.number for episode in episodes}))
                    continue
                canonical, numbering = resolve_numbering(selection, item)
                info = await provider.get_season(
                    payload.media_id, provider_number(canonical.season, positions)
                )
                info = info.model_copy(update={"number": canonical.season})
                numbers = {episode.number for episode in info.episodes}
                if canonical.episodes is not None:
                    if not canonical.episodes or not set(canonical.episodes) <= numbers:
                        raise ValueError("Указаны неизвестные или отсутствующие серии")
                    numbers = set(canonical.episodes)
                parts = {(info.number, number) for number in numbers}
                if occupied & parts:
                    raise ValueError("Выбранные сезоны содержат одни и те же серии")
                occupied.update(parts)
                resolved.append((selection, numbering, info, numbers))
            if provider and any(
                info.number == 0 and not selection.manual for selection, _, info, _ in resolved
            ):
                from lazarr.specials import fetch_catalog, save_catalog

                catalog = await fetch_catalog(provider, remote_item)
                with self.db.session() as db:
                    save_catalog(db, media.id, catalog)
        return resolved

    def _replace_selections(self, db, task, selections):
        from lazarr.season_structure import insert

        selections = list(selections)
        # New manual rows occupy their requested positions. Shift existing and
        # provider-backed rows, including seasons not selected in this task.
        new_rows = sorted(
            [i for i, (s, _, _, _) in enumerate(selections) if s.manual and s.season_id is None],
            key=lambda i: selections[i][0].season,
        )
        for index in new_rows:
            position = selections[index][0].season
            insert(db, task.media_id, position)
            for other, (selection, numbering, info, numbers) in enumerate(selections):
                if other in new_rows:
                    continue
                updates = {}
                if selection.season >= position:
                    updates["season"] = selection.season + 1
                if selection.numbering_season is not None and selection.numbering_season >= position:
                    updates["numbering_season"] = selection.numbering_season + 1
                if numbering and numbering["season"] >= position:
                    numbering = {**numbering, "season": numbering["season"] + 1}
                if info.number >= position:
                    info = info.model_copy(update={"number": info.number + 1})
                selections[other] = (selection.model_copy(update=updates), numbering, info, numbers)
        old_memberships = list(db.scalars(select(TaskSeason).where(TaskSeason.task_id == task.id)))
        old_seasons = {
            membership.season_id: db.get(Season, membership.season_id) for membership in old_memberships
        }
        for membership in old_memberships:
            db.delete(membership)
        db.flush()
        wanted = set()
        memberships = []
        for selection, numbering, info, numbers in selections:
            if selection.manual:
                season = db.get(Season, selection.season_id) if selection.season_id else None
                if season is None:
                    season = Season(media_id=task.media_id, number=info.number, title=info.title)
                    db.add(season)
                    db.flush()
                season.title = info.title
                season.metadata_json = {**(season.metadata_json or {}), "manual": True}
            else:
                season = self._upsert_season(db, task.media_id, info, local=True)
            membership = TaskSeason(
                task_id=task.id,
                season_id=season.id,
                selection_key=f"alt:{selection.numbering_season}" if numbering else str(season.number),
                whole_season=selection.episodes is None,
                numbering=numbering,
            )
            db.add(membership)
            memberships.append(membership)
            wanted.update(
                db.scalars(
                    select(Episode.id).where(Episode.season_id == season.id, Episode.number.in_(numbers))
                )
            )
        existing = list(db.scalars(select(Subtask).where(Subtask.task_id == task.id)))
        wanted = {identity for identity in wanted if not db.get(ConfigEntry, f"episode_deleted.{identity}")}
        for sub in existing:
            if sub.episode_id in wanted:
                wanted.remove(sub.episode_id)
                if sub.status == "removed":
                    current = db.scalar(
                        select(SubtaskAsset.id).where(
                            SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True)
                        )
                    )
                    sub.status = "done" if current else "queued"
                    sub.next_search_at = 0
            else:
                sub.status, sub.lease_until = "removed", 0
                for link in db.scalars(select(SubtaskAsset).where(SubtaskAsset.subtask_id == sub.id)):
                    link.pending = False
        for episode_id in wanted:
            db.add(Subtask(task_id=task.id, episode_id=episode_id, part_key=f"episode:{episode_id}"))
        task.season_id = memberships[0].season_id if len(memberships) == 1 else None
        task.numbering = memberships[0].numbering if len(memberships) == 1 else {}
        task.whole_season = all(m.whole_season for m in memberships)
        db.flush()
        retained = {membership.season_id for membership in memberships}
        for season_id, season in old_seasons.items():
            if (
                season_id not in retained
                and (season.metadata_json or {}).get("manual")
                and not db.scalar(select(Episode.id).where(Episode.season_id == season_id))
            ):
                db.delete(season)
        enqueue(db, task.id)

    def edit(self, task_id, user_id, *, requirements=None, paused=None, selections=None):
        with self.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError("Задача не найдена")
            if selections is not None:
                self._replace_selections(db, task, selections)
            if requirements is not None and task.requirements != requirements.model_dump():
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

    def refresh_candidate_report(self, db, decision, release, subtask):
        if not release.files or (
            decision.report.get("release_revision") == release.revision
            and decision.report.get("engine_version") == current_engine().identity
        ):
            return
        from lazarr.matcher import Matcher
        from lazarr.sdk import Candidate, TorrentFile

        evaluation = (
            Matcher()
            .evaluate(
                Candidate.model_validate(release.data),
                [self.request_for(db, subtask)],
                [TorrentFile.model_validate(file) for file in release.files],
                release.revision,
            )
            .evaluations[0]
        )
        decision.report = {
            **evaluation.model_dump(mode="json"),
            "release_revision": release.revision,
            "engine_version": current_engine().identity,
        }

    def candidates(self, subtask_id):
        with self.db.session() as db:
            subtask = db.get(Subtask, subtask_id)
            episode = db.get(Episode, subtask.episode_id) if subtask and subtask.episode_id else None
            used = {}
            if episode:
                # Actual file bindings, not historical candidate decisions, define usage.
                for link, episode_join in (
                    (SubtaskAsset, SubtaskAsset.subtask_id == Subtask.id),
                    (LibraryAsset, LibraryAsset.episode_id == Episode.id),
                ):
                    query = select(Download, Episode.number).select_from(link)
                    if link is SubtaskAsset:
                        query = query.join(Subtask, episode_join).join(
                            Episode, Subtask.episode_id == Episode.id
                        )
                        query = query.where(SubtaskAsset.current | SubtaskAsset.pending)
                    else:
                        query = query.join(Episode, episode_join)
                    query = (
                        query.join(MediaAsset, link.asset_id == MediaAsset.id)
                        .join(Download, MediaAsset.download_id == Download.id)
                        .where(Episode.season_id == episode.season_id, Episode.id != episode.id)
                    )
                    for download, number in db.execute(query):
                        entry = used.setdefault(
                            download.release_id, {"download": download, "episodes": set()}
                        )
                        entry["episodes"].add(number)
                # A newly added episode may never have been evaluated against the
                # season's existing torrent. Reuse its saved, authoritative file list.
                from lazarr.matcher import Matcher
                from lazarr.sdk import Candidate, TorrentFile

                for release_id, entry in used.items():
                    decision = db.scalar(
                        select(CandidateDecision).where(
                            CandidateDecision.subtask_id == subtask_id,
                            CandidateDecision.release_id == release_id,
                        )
                    )
                    if decision is None:
                        download = entry["download"]
                        release = db.get(Release, release_id)
                        report = (
                            Matcher()
                            .evaluate(
                                Candidate.model_validate(release.data),
                                [self.request_for(db, subtask)],
                                [
                                    TorrentFile.model_validate(file)
                                    for file in (
                                        release.files
                                        or (
                                            download.plan.get("files", [])
                                            if download.infohash == release.revision
                                            else []
                                        )
                                    )
                                ],
                                release.revision,
                            )
                            .evaluations[0]
                        )
                        db.add(
                            CandidateDecision(
                                subtask_id=subtask_id,
                                release_id=release_id,
                                report=report.model_dump(mode="json"),
                            )
                        )
                db.flush()
            result = []
            for decision, release in db.execute(
                select(CandidateDecision, Release)
                .join(Release)
                .where(CandidateDecision.subtask_id == subtask_id)
            ):
                self.refresh_candidate_report(db, decision, release, subtask)
                candidate = dict(release.data)
                candidate.pop("magnet", None)
                candidate.pop("download_url", None)
                result.append(
                    {
                        "id": decision.id,
                        "candidate": candidate,
                        "report": decision.report,
                        "action": decision.action,
                        "used_in_season": sorted(used[release.id]["episodes"]) if release.id in used else [],
                        "episode_missing": any(
                            criterion.get("field") == "episode" and criterion.get("result") == "MISMATCH"
                            for criterion in (decision.report or {}).get("criteria", [])
                        ),
                    }
                )
            result.sort(
                key=lambda choice: (
                    choice["action"] == "rejected",
                    not (
                        choice["report"].get("manual_candidate") or choice["report"].get("result") == "MATCH"
                    ),
                    -choice["report"].get("score", 0),
                    not bool(choice["used_in_season"]),
                    -(choice["candidate"].get("seeds") or 0),
                    choice["id"],
                )
            )
            recommended = (
                next(
                    (
                        choice["id"]
                        for choice in result
                        if choice["action"] not in {"rejected", "selected"}
                        and choice["report"].get("manual_candidate")
                    ),
                    None,
                )
                if subtask and subtask.status == "needs_selection"
                else None
            )
            for choice in result:
                choice["recommended"] = choice["id"] == recommended
            return result

    def task_candidates(self, task_id, season_number=None):
        """Return each release once, scoped to the task or one of its seasons."""
        with self.db.session() as db:
            task = db.get(Task, task_id)
            if not task:
                raise ValueError("Задача не найдена")
            subtasks_query = select(Subtask).where(Subtask.task_id == task_id)
            if season_number is not None:
                subtasks_query = (
                    subtasks_query.join(Episode, Subtask.episode_id == Episode.id)
                    .join(Season, Episode.season_id == Season.id)
                    .where(Season.number == season_number)
                )
            subtasks = list(db.scalars(subtasks_query))
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
                self.refresh_candidate_report(db, decision, release, subtask)
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
