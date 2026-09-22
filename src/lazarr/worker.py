from lazarr.notifications import queue_episode_notification
from lazarr.selection import reject_reason, candidate_rank
from lazarr.provider_utils import search_titles

import asyncio
import logging
import re
import time
from pathlib import Path, PurePosixPath
from sqlalchemy import select, update
from lazarr.calendar import next_search_start, released, TMDBCalendar
from lazarr.matcher import Matcher
from lazarr.search import SearchProgress, defer
from lazarr.languages import language_name
from lazarr.models import (
    Subtask,
    Task,
    Episode,
    Season,
    Media,
    Release,
    Download,
    MediaAsset,
    SubtaskAsset,
    LibraryAsset,
    CandidateDecision,
    ConfigEntry,
)
from lazarr.plugins import atomic_write
from lazarr.sdk import (
    Candidate,
    SearchQuery,
    DownloadPlan,
    DownloadSource,
    MatchResult,
    FileBinding,
    Evidence,
    Criterion,
    EpisodeInfo,
    language,
)
from lazarr.security import audit
from lazarr.torrent import probe_file
from lazarr.subtitle_language import detect_subtitle_language

log = logging.getLogger(__name__)


def probe_resolution(stream):
    width, height = stream.get("width", 0), stream.get("height", 0)
    # Width also recognizes letterboxed/cropped cinema releases (1920x800 -> 1080p).
    for level, minimum_width in [
        (4320, 7000),
        (2160, 3500),
        (1440, 2400),
        (1080, 1800),
        (720, 1200),
        (576, 0),
    ]:
        if height >= level or minimum_width and width >= minimum_width:
            return level
    return 480 if height else None


class CandidateFiltered(Exception):
    pass


class Worker:
    def __init__(self, db, plugins, service, engine, config):
        self.db, self.plugins, self.service, self.engine, self.config = db, plugins, service, engine, config
        self.matcher = Matcher()
        self.calendar = TMDBCalendar()
        self.lock = asyncio.Lock()
        self.selection_lock = asyncio.Lock()
        self.selection_changed = asyncio.Event()
        self.download_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.probe_cache = {}
        self.last_checkpoint = 0
        self.last_restore = 0
        self.consumer_state = {}
        self.progress = SearchProgress()

    async def evaluate(self, candidate, subtasks, *, allow_preference_mismatch=False, ignore_filters=False):
        """Only TorrentEngine supplies the authoritative file list to Matcher."""
        self.progress.record("inspect", f"Чтение описания: {candidate.title}", candidate=candidate.title)
        async with self.plugins.open(candidate.provider) as provider:
            detailed = await provider.inspect(candidate)
            reason = (
                None
                if ignore_filters
                else reject_reason(
                    detailed,
                    subtasks,
                    detailed=True,
                    allow_preference_mismatch=allow_preference_mismatch,
                )
            )
            if reason:
                raise CandidateFiltered(reason)
            self.progress.record("resolve", f"Получение torrent / magnet: {candidate.title}")
            source = await provider.resolve_download(detailed)
        self.progress.record("metadata", f"Получение структуры торрента: {candidate.title}")
        metadata = await asyncio.to_thread(self.engine.inspect, source)
        if any(
            part.casefold() == "bdmv" for file in metadata.files for part in PurePosixPath(file.path).parts
        ):
            raise CandidateFiltered("Blu-ray контейнер BDMV не поддерживается")
        self.progress.record(
            "matching", f"Сопоставление {len(metadata.files)} файлов с эпизодами и дорожками"
        )
        report = self.matcher.evaluate(detailed, subtasks, metadata.files, metadata.infohash)
        return detailed, metadata, report

    async def add_manual_candidate(self, subtask_id, url):
        """Inspect and save one explicitly supplied torrent URL for manual selection."""
        async with self.lock:
            with self.db.session() as db:
                subtask = db.get(Subtask, subtask_id)
                if not subtask:
                    raise ValueError("Серия не найдена")
                requests = [self.service.request_for(db, subtask)]
            return await self._save_manual_candidate(requests, url)

    async def add_manual_task_candidate(self, task_id, url, season_number=None):
        """Inspect one supplied URL against every subtask in a task or season."""
        async with self.lock:
            with self.db.session() as db:
                task = db.get(Task, task_id)
                if not task:
                    raise ValueError("Задача не найдена")
                query = select(Subtask).where(Subtask.task_id == task_id)
                if season_number is not None:
                    query = (
                        query.join(Episode, Subtask.episode_id == Episode.id)
                        .join(Season, Episode.season_id == Season.id)
                        .where(Season.number == season_number)
                    )
                subtasks = list(db.scalars(query.order_by(Subtask.id)))
                if not subtasks:
                    raise ValueError("В выбранной области нет серий")
                requests = [self.service.request_for(db, subtask) for subtask in subtasks]
            return await self._save_manual_candidate(requests, url)

    async def _save_manual_candidate(self, requests, url):
        candidate = self.plugins.manual_candidate(url)
        try:
            detailed, metadata, report = await self.evaluate(
                candidate, requests, allow_preference_mismatch=True, ignore_filters=True
            )
        except CandidateFiltered as exc:
            raise ValueError(str(exc)) from exc
        release_id, _ = self._record(detailed, metadata, report)
        with self.db.session() as db:
            decision = db.scalar(
                select(CandidateDecision).where(
                    CandidateDecision.subtask_id == requests[0].id,
                    CandidateDecision.release_id == release_id,
                )
            )
            return decision.id

    async def due_groups(self, task_ids=None, force=False):
        settings = self.service.settings()
        groups = {}
        pending = []
        with self.db.session() as db:
            for sub, task in db.execute(
                select(Subtask, Task)
                .join(Task, Subtask.task_id == Task.id)
                .where(
                    Task.paused.is_(False),
                    True if force else Subtask.next_search_at <= time.time(),
                    True if task_ids is None else Task.id.in_(task_ids),
                    Subtask.lease_until <= time.time(),
                )
            ):
                if db.scalar(
                    select(SubtaskAsset.id).where(
                        SubtaskAsset.subtask_id == sub.id, SubtaskAsset.pending.is_(True)
                    )
                ):
                    continue
                if sub.status in {"done", "removed"} or db.scalar(
                    select(SubtaskAsset.id).where(
                        SubtaskAsset.subtask_id == sub.id, SubtaskAsset.current.is_(True)
                    )
                ):
                    continue
                request = self.service.request_for(db, sub)
                episode = db.get(Episode, sub.episode_id) if sub.episode_id else None
                episode_info = (
                    EpisodeInfo(id=str(episode.id), number=episode.number, air_date=episode.air_date)
                    if episode
                    else None
                )
                pending.append((sub.id, request, episode_info))
        for sub_id, request, episode_info in pending:
            release_date = await self.calendar.release_date(request.media, episode_info)
            if not released(release_date.value, settings.timezone):
                with self.db.session() as db:
                    db.get(Subtask, sub_id).status = "waiting_release"
                continue
            key = (
                request.media.provider,
                request.media.id,
                request.media.kind,
                request.season,
                request.requirements.model_dump_json(),
            )
            groups.setdefault(key, []).append(sub_id)
        return list(groups.values())

    async def run_due(self, task_ids=None, force=False, provider_filter=None):
        if self.lock.locked() or self.engine is None:
            return False
        async with self.lock:
            previous = self.progress.snapshot()
            self.progress.begin()
            self.progress.value["providers"] = self.plugins.search_status()
            try:
                groups = await self.due_groups(task_ids=task_ids, force=force)
                if not groups and not force and previous["state"] != "idle":
                    self.progress.value = previous
                    return True
                self.progress.value["groups_total"] = len(groups)
                with self.db.session() as db:
                    task_groups = [
                        list(set(db.scalars(select(Subtask.task_id).where(Subtask.id.in_(ids)))))
                        for ids in groups
                    ]
                self.progress.prepare_tasks(task_groups)
                with self.db.session() as db:
                    target_ids = list(
                        db.scalars(
                            select(Task.id).where(
                                Task.paused.is_(False), True if task_ids is None else Task.id.in_(task_ids)
                            )
                        )
                    )
                grouped_ids = {identity for group in task_groups for identity in group}
                for identity in set(target_ids) - grouped_ids:
                    self.progress.tasks[identity] = SearchProgress().snapshot()
                    self.progress.tasks[identity].update(
                        state="finished", message="Нет доступных для поиска серий"
                    )
                for ids, identities in zip(groups, task_groups):
                    self.progress.start_group(identities)
                    completed = False
                    try:
                        await self._run_group(ids, provider_filter=provider_filter)
                        completed = True
                    finally:
                        self.progress.finish_group(completed)
                    self.progress.value["groups_done"] += 1
                unavailable = (
                    bool(groups)
                    and not self.progress.value["search_requests"]
                    and not self.progress.value.get("satisfied")
                )
                self.progress.record(
                    "finished",
                    (
                        "Поиск не выполнен: ни один провайдер не вернул результаты"
                        if unavailable
                        else f"Поиск завершён; ошибок: {self.progress.value['errors']}"
                        if self.progress.value["errors"]
                        else "Поиск завершён"
                    )
                    if groups
                    else "Нет доступных для поиска эпизодов",
                    state="error" if unavailable else "finished",
                )
            except asyncio.CancelledError:
                self.progress.record("interrupted", "Поиск прерван; очередь сохранена", state="queued")
                raise
            except Exception:
                self.progress.record("error", "Не удалось завершить поиск", state="error")
                raise
            finally:
                self.progress.value["running"] = False
        return True

    def defer_requests(self, claimed, provider_id, until):
        if until <= time.time():
            return
        with self.db.session() as db:
            task_ids = list(db.scalars(select(Subtask.task_id).where(Subtask.id.in_(claimed))))
            defer(db, task_ids, provider_id, until)

    def unresolved(self, identities):
        """Return episodes which have no selected or pending download."""
        with self.db.session() as db:
            covered = set(
                db.scalars(
                    select(SubtaskAsset.subtask_id).where(
                        SubtaskAsset.subtask_id.in_(identities),
                        (SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True)),
                    )
                )
            )
        return set(identities) - covered

    def search_year(self, db, request, subtask_id):
        """Use the movie year or the first known air date of the requested season."""
        if request.media.kind == "movie":
            if request.media.year:
                return request.media.year
            date = request.media.release_date or ""
            return int(date[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
        subtask = db.get(Subtask, subtask_id)
        episode = db.get(Episode, subtask.episode_id) if subtask.episode_id else None
        if not episode:
            return None
        dates = db.scalars(
            select(Episode.air_date)
            .where(Episode.season_id == episode.season_id, Episode.air_date.is_not(None))
            .order_by(Episode.air_date)
        )
        for date in dates:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                return int(date[:4])
        return None

    async def search_page(self, provider, query, cursor, claimed, acquire):
        if not acquire:
            return await provider.search(query, cursor)
        search = asyncio.create_task(provider.search(query, cursor))
        try:
            while True:
                changed = asyncio.create_task(self.selection_changed.wait())
                try:
                    done, _ = await asyncio.wait({search, changed}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                if search in done:
                    return await search
                self.selection_changed.clear()
                if not self.unresolved(claimed):
                    search.cancel()
                    await asyncio.gather(search, return_exceptions=True)
                    return None
        except asyncio.CancelledError:
            search.cancel()
            await asyncio.gather(search, return_exceptions=True)
            raise

    async def search_alternatives(self, subtask_id):
        async with self.lock:
            with self.db.session() as db:
                subtask = db.get(Subtask, subtask_id)
                if not subtask:
                    raise ValueError("Серия не найдена")
                task_id = subtask.task_id
            self.progress.begin()
            self.progress.value["providers"] = self.plugins.search_status()
            self.progress.value["groups_total"] = 1
            self.progress.prepare_tasks([[task_id]])
            self.progress.start_group([task_id])
            completed = False
            try:
                await self._run_group([subtask_id], acquire=False)
                if not self.progress.value["search_requests"]:
                    self.progress.value["errors"] += 1
                    self.progress.record(
                        "error",
                        "Поиск не выполнен. Проверьте доступность провайдеров в настройках.",
                        state="error",
                    )
                    raise ValueError("Поиск не выполнен. Проверьте доступность провайдеров в настройках.")
                completed = True
            finally:
                self.progress.finish_group(completed)
                self.progress.value["groups_done"] = int(completed)
                state = "error" if self.progress.value["errors"] else "finished"
                self.progress.value.update(running=False, state=state)
                task = self.progress.tasks[task_id]
                task.update(running=False, state=state, message=self.progress.value["message"])

    async def _run_group(self, ids, provider_filter=None, acquire=True):
        settings = self.service.settings()
        now = time.time()
        with self.db.session() as db:
            claimed = []
            for identity in ids:
                if not acquire:
                    claimed.append(identity)
                    continue
                result = db.execute(
                    update(Subtask)
                    .where(Subtask.id == identity, Subtask.lease_until <= now)
                    .values(lease_until=now + 600, status="searching", last_error=None)
                )
                if result.rowcount:
                    claimed.append(identity)
            requests = [self.service.request_for(db, db.get(Subtask, identity)) for identity in claimed]
            year = self.search_year(db, requests[0], claimed[0]) if requests else None
        if not requests:
            return
        self.progress.value.update(
            media=requests[0].media.title,
            season=requests[0].season,
            episodes=[r.episode for r in requests if r.episode is not None],
            candidate="",
            provider="",
        )
        choices = []
        errors = []
        provider_ids = [
            p for p in self.plugins.available("content") if provider_filter is None or p in provider_filter
        ]
        previous = {p["id"]: p for p in self.progress.value["providers"]}
        provider_states = self.plugins.search_status()
        for item in provider_states:
            old = previous.get(item["id"], {})
            item.update(requests=old.get("requests", 0), candidates=old.get("candidates", 0))
        self.progress.value["providers"] = provider_states
        provider_state = {p["id"]: p for p in provider_states}
        for item in provider_states:
            if item["enabled"] and provider_filter is not None and item["id"] not in provider_filter:
                item.update(state="skipped", reason="Повтор запланирован для другого провайдера")
            if not item["enabled"] or item["state"] == "skipped":
                self.progress.record(
                    "provider_skipped", f"{item['name']}: {item['reason']}", provider=item["name"]
                )
        interrupted = False
        try:
            if not provider_ids:
                errors.append("Включите контент-провайдер в настройках")
            query = SearchQuery(
                media=requests[0].media,
                season=requests[0].season,
                episodes=[r.episode for r in requests if r.episode is not None],
                requirements=requests[0].requirements,
            )
            self.progress.value.update(
                media=query.media.title,
                season=query.season,
                episodes=query.episodes,
                candidate="",
                provider="",
            )
            seen = set()
            covered = set()
            for provider_id in provider_ids:
                if acquire and not self.unresolved(claimed):
                    self.progress.value["satisfied"] = True
                    self.progress.record("finished", "Поиск остановлен: раздачи уже выбраны")
                    break
                provider_name = self.plugins.classes[provider_id].manifest.name
                self.progress.value["provider"] = provider_name
                participation = provider_state[provider_id]
                if acquire and all(r.id in covered for r in requests):
                    participation.update(
                        state="skipped", reason="Для всех эпизодов найдены подходящие раздачи"
                    )
                    self.progress.record("provider_skipped", f"{provider_name}: {participation['reason']}")
                    continue
                if participation["state"] == "cooldown":
                    reason = participation["reason"]
                    if acquire:
                        self.defer_requests(claimed, provider_id, participation["retry_at"])
                    errors.append(f"{provider_name}: {reason}")
                    self.progress.record("provider_cooldown", f"{provider_name}: {reason}")
                    continue
                cursor = None
                titles = search_titles(query.media)
                variants = [f"{title} {year}" for title in titles] + titles if year else titles
                variant = 0
                variant_pages = 0
                stopped = False
                for _page in range(3 * len(variants)):
                    if acquire and not self.unresolved(claimed):
                        self.progress.value["satisfied"] = True
                        break
                    if acquire and all(r.id in covered for r in requests):
                        break
                    query.text = variants[variant]
                    participation.update(state="searching", reason=f"Запрос страницы {_page + 1}")
                    self.progress.record(
                        "search",
                        f"{provider_name}: поиск «{query.text}», смещение {cursor or 0}",
                        page=_page + 1,
                    )
                    try:
                        async with self.plugins.open(provider_id) as provider:
                            with self.db.session() as db:
                                db.execute(
                                    update(Subtask)
                                    .where(Subtask.id.in_(claimed))
                                    .values(last_search_at=time.time())
                                )
                            page = await self.search_page(provider, query, cursor, claimed, acquire)
                    except Exception as exc:
                        errors.append(self._error(exc))
                        participation.update(state="error", reason=self._error(exc))
                        latest = next(p for p in self.plugins.search_status() if p["id"] == provider_id)
                        participation["retry_at"] = latest["retry_at"]
                        if acquire:
                            self.defer_requests(claimed, provider_id, latest["retry_at"])
                        self.progress.record("provider_error", f"{provider_name}: {self._error(exc)}")
                        break
                    if page is None or acquire and not self.unresolved(claimed):
                        self.progress.value["satisfied"] = True
                        break
                    variant_pages += 1
                    participation["requests"] += 1
                    participation["candidates"] += len(page.items)
                    participation.update(state="checking", reason="Проверка найденных кандидатов")
                    self.progress.value["search_requests"] += 1
                    self.progress.record(
                        "results", f"{provider_name}: найдено кандидатов — {len(page.items)}"
                    )
                    unique = []
                    for candidate in page.items:
                        key = (candidate.provider, candidate.id, candidate.revision)
                        if key not in seen:
                            seen.add(key)
                            unique.append(candidate)
                    self.progress.value["candidates_found"] += len(unique)
                    shortlist = []
                    for candidate in unique:
                        reason = reject_reason(candidate, requests, allow_preference_mismatch=not acquire)
                        if reason:
                            self.progress.value["candidates_filtered"] += 1
                            self.progress.record("filtered", f"Отсеяно: {candidate.title} — {reason}")
                        else:
                            shortlist.append(candidate)
                    ordered = sorted(shortlist, key=lambda c: candidate_rank(c, requests))
                    for position, candidate in enumerate(ordered):
                        if acquire and not self.unresolved(claimed):
                            self.progress.value["satisfied"] = True
                            break
                        if acquire and all(r.id in covered for r in requests):
                            break
                        try:
                            detailed, metadata, report = await self.evaluate(
                                candidate,
                                requests,
                                allow_preference_mismatch=not acquire,
                            )
                            release_id, allowed = self._record(detailed, metadata, report)
                            choices.append((detailed, metadata, report, release_id, allowed))
                            self.progress.value["candidates_checked"] += 1
                            for evaluation in report.evaluations:
                                if (
                                    evaluation.result == MatchResult.MATCH
                                    and evaluation.subtask_id in allowed
                                ):
                                    covered.add(evaluation.subtask_id)
                        except CandidateFiltered as exc:
                            self.progress.value["candidates_filtered"] += 1
                            self.progress.record("filtered", f"Отсеяно: {candidate.title} — {exc}")
                        except Exception as exc:
                            self.progress.value["candidates_failed"] += 1
                            errors.append(self._error(exc))
                            self.progress.record(
                                "candidate_error", f"Кандидат не проверен: {self._error(exc)}"
                            )
                            latest = next(p for p in self.plugins.search_status() if p["id"] == provider_id)
                            if latest["state"] == "cooldown":
                                if acquire:
                                    self.defer_requests(claimed, provider_id, latest["retry_at"])
                                participation.update(
                                    state="cooldown", reason=latest["reason"], retry_at=latest["retry_at"]
                                )
                                self.progress.value["candidates_deferred"] += len(ordered) - position - 1
                                self.progress.record(
                                    "provider_cooldown",
                                    f"{provider_name}: обход остановлен до окончания паузы",
                                )
                                stopped = True
                                break
                        if not acquire:
                            continue
                        with self.db.session() as db:
                            db.execute(
                                update(Subtask)
                                .where(Subtask.id.in_(claimed))
                                .values(lease_until=time.time() + 600)
                            )
                    if not stopped:
                        self.progress.value["pages_checked"] += 1
                        self.progress.value["group_pages_checked"] += 1
                    if stopped:
                        break
                    participation.update(state="completed", reason="Поиск выполнен")
                    if page.next_cursor and page.next_cursor != cursor and variant_pages < 3:
                        cursor = page.next_cursor
                    elif variant + 1 < len(variants):
                        variant += 1
                        variant_pages = 0
                        cursor = None
                    else:
                        break
            if not acquire:
                self.progress.record("alternatives", "Поиск вариантов завершён. Выберите раздачу вручную.")
                return
            self.progress.record("ranking", "Выбор подходящих раздач и объединение загрузок")
            selected = {}
            for request in requests:
                if request.id not in self.unresolved([request.id]):
                    continue
                ranked = []
                for candidate, metadata, report, release_id, allowed in choices:
                    evaluation = next(e for e in report.evaluations if e.subtask_id == request.id)
                    if evaluation.result != MatchResult.MATCH or request.id not in allowed:
                        continue
                    binding = evaluation.binding
                    ranked.append(
                        (
                            -(binding.resolution or 0),
                            len(binding.missing_subtitle_languages),
                            -(candidate.seeds or 0),
                            candidate.size or 2**63,
                            candidate.provider,
                            candidate.id,
                            metadata.infohash,
                            binding,
                            evaluation,
                            metadata,
                            release_id,
                        )
                    )
                if ranked:
                    best = min(ranked, key=lambda x: x[:6])
                    selected.setdefault(best[6], []).append(best)
            for entries in selected.values():
                metadata, release_id = entries[0][9], entries[0][10]
                plan = DownloadPlan(
                    infohash=metadata.infohash, files=metadata.files, bindings=[entry[7] for entry in entries]
                )
                reports = {entry[8].subtask_id: entry[8] for entry in entries}
                self.progress.record("download", f"Передача в загрузку: {len(plan.bindings)} эпизодов")
                await self.submit(release_id, metadata, plan, reports)
            self.progress.record(
                "group_done",
                f"Обработано эпизодов: {len(requests)}; выбрано раздач: {len(selected)}"
                + (f"; ошибок: {len(errors)}" if errors else ""),
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            errors.append(self._error(exc))
            log.exception("Worker group failed")
        finally:
            self.progress.value["errors"] += len(errors)
            with self.db.session() as db:
                for identity in claimed:
                    if not acquire:
                        continue
                    sub = db.get(Subtask, identity)
                    pending = db.scalar(
                        select(SubtaskAsset.id).where(
                            SubtaskAsset.subtask_id == identity, SubtaskAsset.pending.is_(True)
                        )
                    )
                    current = db.scalar(
                        select(SubtaskAsset.id).where(
                            SubtaskAsset.subtask_id == identity, SubtaskAsset.current.is_(True)
                        )
                    )
                    if not pending:
                        unknown = any(
                            next(e for e in r.evaluations if e.subtask_id == identity).result
                            == MatchResult.UNKNOWN
                            for _, _, r, _, _ in choices
                        )
                        sub.status = "done" if current else "needs_selection" if unknown else "queued"
                    if sub.status == "needs_selection":
                        queue_episode_notification(db, sub, "selection")
                    sub.attempts = sub.attempts + 1 if errors else 0
                    sub.next_search_at = (
                        0 if interrupted else next_search_start(settings.search_start, settings.timezone)
                    )
                    sub.lease_until = 0
                    sub.last_error = "; ".join(dict.fromkeys(errors))[:1000] or None

    def _error(self, exc):
        from lazarr.sdk import ProviderError

        if isinstance(exc, ProviderError):
            return f"{exc.code}: {exc}"
        if isinstance(exc, (ValueError, TimeoutError)):
            return str(exc)[:300]
        return f"Ошибка обработки ({type(exc).__name__})"

    def _record(self, candidate, metadata, report):
        target = self.config.data_dir / "torrents" / f"{metadata.infohash}.torrent"
        atomic_write(target, metadata.torrent)
        with self.db.session() as db:
            release = db.scalar(
                select(Release).where(
                    Release.provider == candidate.provider,
                    Release.external_id == candidate.id,
                    Release.revision == metadata.infohash,
                )
            )
            if not release:
                release = Release(
                    provider=candidate.provider,
                    external_id=candidate.id,
                    revision=metadata.infohash,
                    data=candidate.model_dump(),
                )
                db.add(release)
                db.flush()
            else:
                release.data = candidate.model_dump()
            allowed = set()
            for evaluation in report.evaluations:
                decision = db.scalar(
                    select(CandidateDecision).where(
                        CandidateDecision.subtask_id == evaluation.subtask_id,
                        CandidateDecision.release_id == release.id,
                    )
                )
                if not decision:
                    decision = CandidateDecision(
                        subtask_id=evaluation.subtask_id,
                        release_id=release.id,
                        report=evaluation.model_dump(mode="json"),
                    )
                    db.add(decision)
                decision.report = evaluation.model_dump(mode="json")
                decision.updated_at = time.time()
                if decision.action != "rejected":
                    allowed.add(evaluation.subtask_id)
            return release.id, allowed

    async def choose(self, decision_id, user_id, reject=False, video_index=None, track_indices=None):
        async with self.selection_lock:
            with self.db.session() as db:
                decision = db.get(CandidateDecision, decision_id)
                if not decision:
                    raise ValueError("Кандидат не найден")
                if reject:
                    decision.action = "rejected"
                    audit(db, user_id, "candidate.reject", str(decision_id))
                    return
                sub = db.get(Subtask, decision.subtask_id)
                request = self.service.request_for(db, sub)
                release = db.get(Release, decision.release_id)
                release_id, infohash = release.id, release.revision
                candidate = Candidate.model_validate(release.data)
                already_current = db.scalar(
                    select(SubtaskAsset.id)
                    .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                    .join(Download, MediaAsset.download_id == Download.id)
                    .where(
                        SubtaskAsset.subtask_id == sub.id,
                        SubtaskAsset.current.is_(True),
                        Download.infohash == infohash,
                    )
                )
                if already_current is not None and video_index is None:
                    return
            torrent = (self.config.data_dir / "torrents" / f"{infohash}.torrent").read_bytes()
            metadata = await asyncio.to_thread(self.engine.inspect, DownloadSource(torrent=torrent))
            report = self.matcher.evaluate(candidate, [request], metadata.files, infohash)
            evaluation = report.evaluations[0]
            binding = evaluation.binding
            if video_index is not None:
                from lazarr.matcher import (
                    AUDIO,
                    SUBTITLE,
                    classify_external_subtitles,
                    file_language,
                    playable_video,
                )
                from lazarr.sdk import TrackBinding

                video = next(
                    (f for f in metadata.files if f.index == video_index and playable_video(f)), None
                )
                if not video:
                    raise ValueError("Выберите видеофайл")
                tracks = []
                for index in track_indices or []:
                    file = next((f for f in metadata.files if f.index == index), None)
                    if not file or Path(file.path).suffix.lower() not in AUDIO | SUBTITLE:
                        raise ValueError("Некорректный файл дорожки")
                    tracks.append(
                        TrackBinding(
                            kind="audio" if Path(file.path).suffix.lower() in AUDIO else "subtitle",
                            language=file_language(file.path),
                            file_index=index,
                            path=file.path,
                        )
                    )
                classify_external_subtitles(tracks, metadata.files)
                binding = FileBinding(
                    subtask_id=request.id,
                    video_index=video.index,
                    video_path=video.path,
                    episode_order=(request.season or 0) * 10000 + (request.episode or 0),
                    tracks=tracks,
                    resolution=binding.resolution if binding else None,
                    missing_subtitle_languages=sorted(
                        set(request.requirements.subtitle_languages)
                        - {t.language for t in tracks if t.kind == "subtitle"}
                    ),
                )
            if binding is None:
                raise ValueError("Укажите соответствие видео и внешних дорожек вручную")
            evaluation.binding = binding
            plan = DownloadPlan(infohash=infohash, files=metadata.files, bindings=[binding])
            await self.submit(release_id, metadata, plan, {request.id: evaluation}, override=True)
            self.selection_changed.set()
            with self.db.session() as db:
                db.get(CandidateDecision, decision_id).action = "selected"
                db.get(CandidateDecision, decision_id).report = evaluation.model_dump(mode="json")
                audit(db, user_id, "candidate.select", str(decision_id), {"override": True})

    async def choose_all(self, decision_id, user_id, season_number=None, task_id=None):
        """Apply one release to matching episodes in the task or one season."""
        async with self.selection_lock:
            with self.db.session() as db:
                decision = db.get(CandidateDecision, decision_id)
                if not decision:
                    raise ValueError("Кандидат не найден")
                source_subtask = db.get(Subtask, decision.subtask_id)
                task = db.get(Task, source_subtask.task_id)
                if task_id is not None and task.id != task_id:
                    raise ValueError("Кандидат не относится к указанной задаче")
                release = db.get(Release, decision.release_id)
                release_id, infohash = release.id, release.revision
                candidate = Candidate.model_validate(release.data)
                subtasks_query = select(Subtask).where(Subtask.task_id == task.id)
                if season_number is not None:
                    subtasks_query = (
                        subtasks_query.join(Episode, Subtask.episode_id == Episode.id)
                        .join(Season, Episode.season_id == Season.id)
                        .where(Season.number == season_number)
                    )
                subtasks = list(db.scalars(subtasks_query.order_by(Subtask.id)))
                if not subtasks:
                    raise ValueError("В сезоне нет серий этой задачи")
                current_ids = set(
                    db.scalars(
                        select(SubtaskAsset.subtask_id)
                        .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                        .join(Download, MediaAsset.download_id == Download.id)
                        .where(
                            SubtaskAsset.subtask_id.in_([sub.id for sub in subtasks]),
                            SubtaskAsset.current.is_(True),
                            Download.infohash == infohash,
                        )
                    )
                )
                requests = [
                    self.service.request_for(db, sub) for sub in subtasks if sub.id not in current_ids
                ]
            if not requests:
                return {"selected": len(current_ids), "total": len(subtasks), "skipped": 0}
            torrent_path = self.config.data_dir / "torrents" / f"{infohash}.torrent"
            if not torrent_path.exists():
                raise ValueError("Файл раздачи больше недоступен; запустите поиск повторно")
            torrent = torrent_path.read_bytes()
            metadata = await asyncio.to_thread(self.engine.inspect, DownloadSource(torrent=torrent))
            report = self.matcher.evaluate(candidate, requests, metadata.files, infohash)
            eligible = [
                evaluation
                for evaluation in report.evaluations
                if evaluation.binding is not None and evaluation.result != MatchResult.MISMATCH
            ]
            if not eligible:
                raise ValueError("Раздачу не удалось сопоставить ни с одной серией задачи")
            bindings = [evaluation.binding for evaluation in eligible]
            reports = {evaluation.subtask_id: evaluation for evaluation in eligible}
            plan = DownloadPlan(infohash=infohash, files=metadata.files, bindings=bindings)
            await self.submit(release_id, metadata, plan, reports, override=True)
            self.selection_changed.set()
            selected_ids = {evaluation.subtask_id for evaluation in eligible}
            with self.db.session() as db:
                for evaluation in report.evaluations:
                    row = db.scalar(
                        select(CandidateDecision).where(
                            CandidateDecision.subtask_id == evaluation.subtask_id,
                            CandidateDecision.release_id == release_id,
                        )
                    )
                    if not row:
                        row = CandidateDecision(
                            subtask_id=evaluation.subtask_id,
                            release_id=release_id,
                            report=evaluation.model_dump(mode="json"),
                        )
                        db.add(row)
                    else:
                        row.report = evaluation.model_dump(mode="json")
                    row.updated_at = time.time()
                    if evaluation.subtask_id in selected_ids:
                        row.action = "selected"
                audit(
                    db,
                    user_id,
                    "candidate.select_season" if season_number is not None else "candidate.select_all",
                    str(decision_id),
                    {
                        "task_id": task.id,
                        "season": season_number,
                        "selected": len(selected_ids),
                        "total": len(subtasks),
                    },
                )
            selected = len(selected_ids | current_ids)
            return {"selected": selected, "total": len(subtasks), "skipped": len(subtasks) - selected}

    async def submit(self, release_id, metadata, plan, reports, override=False):
        async with self.poll_lock, self.download_lock:
            with self.db.session() as db:
                for job in db.scalars(select(ConfigEntry).where(ConfigEntry.key.like("cleanup.%"))):
                    if any(Path(p).name == plan.infohash for p in job.value["directories"]) or any(
                        Path(entry["root"]).name == plan.infohash for entry in job.value.get("files", [])
                    ):
                        raise ValueError(
                            "Предыдущие файлы этой раздачи ещё удаляются; повторите после очистки"
                        )
            settings = self.service.settings()
            # Commit the intent before touching the engine. Restore can finish interrupted submissions.
            with self.db.session() as db:
                valid = []
                for binding in plan.bindings:
                    sub = db.get(Subtask, binding.subtask_id)
                    selected = sub and db.scalar(
                        select(SubtaskAsset.id).where(
                            SubtaskAsset.subtask_id == sub.id,
                            (SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True)),
                        )
                    )
                    if sub and (override or not selected and not db.get(Task, sub.task_id).paused):
                        valid.append(binding)
                if not valid:
                    return
                plan.bindings = valid
                from lazarr.replacement import retire_selections

                await retire_selections(self, db, valid, plan.infohash)
                download = db.scalar(select(Download).where(Download.infohash == plan.infohash))
                if download:
                    old = DownloadPlan.model_validate(download.plan)
                    by_id = {b.subtask_id: b for b in old.bindings}
                    by_id.update({b.subtask_id: b for b in valid})
                    plan.bindings = list(by_id.values())
                    download.plan = plan.model_dump()
                    download.state = "starting"
                else:
                    task = db.get(Task, db.get(Subtask, valid[0].subtask_id).task_id)
                    media = db.get(Media, task.media_id)
                    root = settings.movie_path if media.kind == "movie" else settings.series_path
                    download = Download(
                        infohash=plan.infohash,
                        release_id=release_id,
                        save_path=str(Path(root).absolute() / plan.infohash),
                        torrent_file=str(self.config.data_dir / "torrents" / f"{plan.infohash}.torrent"),
                        plan=plan.model_dump(),
                        seed_ratio=settings.seed_ratio,
                    )
                    db.add(download)
                    db.flush()
                for binding in valid:
                    sub = db.get(Subtask, binding.subtask_id)
                    task = db.get(Task, sub.task_id)
                    asset = db.scalar(
                        select(MediaAsset).where(
                            MediaAsset.download_id == download.id,
                            MediaAsset.video_index == binding.video_index,
                        )
                    )
                    if not asset:
                        asset = MediaAsset(
                            media_id=task.media_id,
                            download_id=download.id,
                            video_index=binding.video_index,
                            path=binding.video_path,
                            tracks=[t.model_dump() for t in binding.tracks],
                            resolution=binding.resolution,
                        )
                        db.add(asset)
                        db.flush()
                    else:
                        # Different tasks can select different sidecars for the same video.
                        tracks = {str(t): t for t in asset.tracks}
                        tracks.update({str(t.model_dump()): t.model_dump() for t in binding.tracks})
                        asset.tracks = list(tracks.values())
                    link = db.scalar(
                        select(SubtaskAsset).where(
                            SubtaskAsset.subtask_id == sub.id, SubtaskAsset.asset_id == asset.id
                        )
                    )
                    if not link:
                        link = SubtaskAsset(
                            subtask_id=sub.id,
                            asset_id=asset.id,
                            preflight=reports[sub.id].model_dump(mode="json"),
                            override=override,
                        )
                        db.add(link)
                    else:
                        link.current, link.pending, link.override, link.verification = (
                            False,
                            True,
                            override,
                            {},
                        )
                        link.preflight = reports[sub.id].model_dump(mode="json")
                    if not override:
                        queue_episode_notification(
                            db, sub, "found", db.get(Release, release_id).data.get("title")
                        )
                    sub.status, sub.last_error = "starting", None
                    sub.missing_subtitle_languages = []
                path = download.save_path
                paused = download.manual_paused or all(
                    db.get(Task, db.get(Subtask, b.subtask_id).task_id).paused for b in plan.bindings
                )
            from lazarr.deletion import cleanup

            self.probe_cache.clear()
            await asyncio.to_thread(cleanup, self.db)
            await self.restore(reset_leases=False, skip_hash=plan.infohash)
            if not self.cleanup_blocks(plan.infohash):
                await asyncio.to_thread(self.engine.add, metadata.torrent, path, plan, paused)

    def cleanup_blocks(self, infohash):
        with self.db.session() as db:
            return any(
                any(Path(p).name == infohash for p in job.value["directories"])
                or any(Path(entry["root"]).name == infohash for entry in job.value.get("files", []))
                for job in db.scalars(select(ConfigEntry).where(ConfigEntry.key.like("cleanup.%")))
            )

    async def restore(self, reset_leases=True, skip_hash=None):
        if self.engine is None:
            return
        if reset_leases:
            from lazarr.replacement import prune_history
            from lazarr.deletion import cleanup

            await prune_history(self)
            await asyncio.to_thread(cleanup, self.db)
        with self.db.session() as db:
            rows = [
                (
                    d.infohash,
                    d.torrent_file,
                    d.save_path,
                    d.plan,
                    d.manual_paused or d.state in {"stopped", "replaced", "paused"},
                    {"uploaded": d.uploaded, "downloaded": d.downloaded},
                )
                for d in db.scalars(select(Download))
            ]
            if reset_leases:
                db.execute(update(Subtask).values(lease_until=0))
        for infohash, torrent_file, save_path, plan, paused, counters in rows:
            if infohash == skip_hash or self.engine.contains(infohash) or self.cleanup_blocks(infohash):
                continue
            try:
                await asyncio.to_thread(
                    self.engine.add,
                    Path(torrent_file).read_bytes(),
                    save_path,
                    DownloadPlan.model_validate(plan),
                    paused,
                    counters,
                )
            except Exception:
                with self.db.session() as db:
                    download = db.scalar(select(Download).where(Download.infohash == infohash))
                    download.state, download.stats = "error", {"error": "Не удалось восстановить торрент"}
                log.exception("Torrent restore failed")

    def consumer_rows(self):
        with self.db.session() as db:
            rows = []
            for download in db.scalars(select(Download)):
                plan = DownloadPlan.model_validate(download.plan)
                active_ids, referenced_ids = set(), set()
                for link, sub, task in db.execute(
                    select(SubtaskAsset, Subtask, Task)
                    .join(Subtask, SubtaskAsset.subtask_id == Subtask.id)
                    .join(Task, Subtask.task_id == Task.id)
                    .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                    .where(
                        MediaAsset.download_id == download.id,
                        (SubtaskAsset.current.is_(True) | SubtaskAsset.pending.is_(True)),
                    )
                ):
                    referenced_ids.add(sub.id)
                    if not task.paused:
                        active_ids.add(sub.id)
                active = plan.model_copy(
                    update={"bindings": [b for b in plan.bindings if b.subtask_id in active_ids]}
                )
                if not referenced_ids:
                    download.state = "replaced"
                should_pause = (
                    download.manual_paused or not active_ids or download.state in {"stopped", "replaced"}
                )
                rows.append((download.infohash, active, should_pause))
        return rows

    async def sync_consumers(self):
        if self.engine is None:
            return
        async with self.download_lock:
            rows = await asyncio.to_thread(self.consumer_rows)
            for infohash, plan, paused in rows:
                if not self.engine.contains(infohash):
                    self.consumer_state.pop(infohash, None)
                    continue
                state = (plan.model_dump_json(), paused)
                if self.consumer_state.get(infohash) == state:
                    continue
                await asyncio.to_thread(self.engine.update_plan, infohash, plan)
                await asyncio.to_thread(self.engine.pause if paused else self.engine.resume, infohash)
                self.consumer_state[infohash] = state
            self.consumer_state = {
                key: value for key, value in self.consumer_state.items() if key in {row[0] for row in rows}
            }

    async def poll(self):
        from lazarr.deletion import cleanup

        async with self.poll_lock:
            if time.time() - self.last_restore > 30:
                async with self.download_lock:
                    await asyncio.to_thread(cleanup, self.db)
                    await self.restore(reset_leases=False)
                self.last_restore = time.time()
            await self._poll()

    async def _poll(self):
        if self.engine is None:
            return
        await self.sync_consumers()
        with self.db.session() as db:
            rows = [(d.id, d.infohash, d.save_path) for d in db.scalars(select(Download))]
        for identity, infohash, root in rows:
            if not self.engine.contains(infohash):
                continue
            try:
                stats = await asyncio.to_thread(self.engine.snapshot, infohash)
                with self.db.session() as db:
                    download = db.get(Download, identity)
                    download.stats = stats
                    download.uploaded = max(download.uploaded, stats["uploaded"])
                    download.downloaded = max(download.downloaded, stats["downloaded"])
                    if download.state not in {"stopped", "replaced"}:
                        download.state = (
                            "error"
                            if stats["error"]
                            else "paused"
                            if stats["paused"]
                            else "seeding"
                            if stats["complete"]
                            else "downloading"
                        )
                    ratio = download.uploaded / download.downloaded if download.downloaded else 0.0
                    stop = (
                        stats["complete"]
                        and download.seed_ratio is not None
                        and (
                            download.seed_ratio == 0
                            or download.downloaded > 0
                            and ratio >= download.seed_ratio
                        )
                    )
                    if stop:
                        download.state = "stopped"
                    links = [
                        (link.id, asset.id, sub.id)
                        for link, asset, sub, task in db.execute(
                            select(SubtaskAsset, MediaAsset, Subtask, Task)
                            .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                            .join(Subtask, SubtaskAsset.subtask_id == Subtask.id)
                            .join(Task, Subtask.task_id == Task.id)
                            .where(
                                MediaAsset.download_id == identity,
                                SubtaskAsset.pending.is_(True),
                                Task.paused.is_(False),
                            )
                        )
                    ]
                if stop:
                    await asyncio.to_thread(self.engine.pause, infohash)
                for link_id, asset_id, sub_id in links:
                    state = stats["bindings"].get(str(sub_id))
                    if not state:
                        continue
                    if state["buffer_ready"] or state["complete"]:
                        await self._verify(link_id, asset_id, sub_id, root, state)
                    else:
                        with self.db.session() as db:
                            db.get(Subtask, sub_id).status = (
                                "downloading" if state["downloaded"] else "starting"
                            )
            except Exception:
                log.exception("Download poll failed")
        if time.time() - self.last_checkpoint > 30:
            await asyncio.to_thread(self.engine.checkpoint)
            self.last_checkpoint = time.time()

    async def _probe(self, path, complete):
        key = (str(path), complete)
        cached = self.probe_cache.get(key)
        if cached and time.time() - cached[0] < (3600 if complete and cached[1].get("ok") else 15):
            return cached[1]
        result = await asyncio.to_thread(probe_file, path, self.config.ffprobe)
        self.probe_cache[key] = (time.time(), result)
        return result

    async def _verify(self, link_id, asset_id, sub_id, root, state):
        with self.db.session() as db:
            link, asset, sub = (
                db.get(SubtaskAsset, link_id),
                db.get(MediaAsset, asset_id),
                db.get(Subtask, sub_id),
            )
            request = self.service.request_for(db, sub)
            binding = FileBinding.model_validate(link.preflight["binding"])
            path = Path(root) / asset.path
        probe = await self._probe(path, state["complete"])
        if not probe.get("ok"):
            if state["complete"]:
                with self.db.session() as db:
                    db.get(Subtask, sub_id).status = "error"
                    db.get(Subtask, sub_id).last_error = "Файл скачан, но ffprobe не смог его проверить"
            return
        streams = probe.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        quality = probe_resolution(video or {})
        audio = {
            language(s.get("tags", {}).get("language")) for s in streams if s.get("codec_type") == "audio"
        }
        subtitles = set()
        for stream in streams:
            if stream.get("codec_type") != "subtitle":
                continue
            detected = language(stream.get("tags", {}).get("language"))
            if detected == "und" and stream.get("index") is not None:
                detected = await asyncio.to_thread(
                    detect_subtitle_language, path, stream.get("index"), stream.get("codec_name")
                )
                if detected != "und":
                    stream["detected_language"] = detected
            subtitles.add(detected)
        unknown_external_audio = False
        for track in binding.tracks:
            if track.file_index is None:
                continue
            if track.kind == "subtitle":
                if track.language == "und":
                    detected = await asyncio.to_thread(detect_subtitle_language, Path(root) / track.path)
                    if detected != "und":
                        track.language = detected
                        track.language_source = "content"
                subtitles.add(track.language)
            else:
                external = await self._probe(Path(root) / track.path, state["complete"])
                audio_streams = [s for s in external.get("streams", []) if s.get("codec_type") == "audio"]
                if audio_streams:
                    languages = {language(s.get("tags", {}).get("language")) for s in audio_streams}
                    audio |= (
                        {track.language}
                        if languages == {"und"}
                        and track.language != "und"
                        and track.language_source == "filename"
                        else languages
                    )
                else:
                    unknown_external_audio = True
        wanted = set(request.requirements.audio_languages)
        missing_audio = wanted - audio
        audio_result = (
            MatchResult.MATCH
            if not missing_audio
            else MatchResult.UNKNOWN
            if "und" in audio or unknown_external_audio
            else MatchResult.MISMATCH
        )
        resolution_result = (
            MatchResult.UNKNOWN
            if quality is None
            else MatchResult.MATCH
            if (request.requirements.min_resolution <= quality <= request.requirements.max_resolution)
            else MatchResult.MISMATCH
        )
        criteria = [
            Criterion(
                field="audio",
                result=audio_result,
                reason="Фактические языки: " + ", ".join(language_name(v) for v in sorted(audio)),
                evidence=[
                    Evidence(
                        field="audio_languages",
                        value=sorted(audio),
                        source="probe",
                        scope="file",
                        file_path=binding.video_path,
                        complete=True,
                    )
                ],
            ),
            Criterion(
                field="resolution", result=resolution_result, reason=f"Фактическое разрешение: {quality}"
            ),
        ]
        missing_subs = sorted(set(request.requirements.subtitle_languages) - subtitles)
        with self.db.session() as db:
            link, asset, sub = (
                db.get(SubtaskAsset, link_id),
                db.get(MediaAsset, asset_id),
                db.get(Subtask, sub_id),
            )
            # Do not finish a generation invalidated by editing requirements during probing.
            task = db.get(Task, sub.task_id)
            if not link.pending or task.paused or task.requirements != request.requirements.model_dump():
                return
            asset.probe, asset.resolution = probe, quality
            if any(track.language_source == "content" for track in binding.tracks):
                link.preflight = {**link.preflight, "binding": binding.model_dump(mode="json")}
            sub.missing_subtitle_languages = missing_subs
            mismatch = any(c.result == MatchResult.MISMATCH for c in criteria)
            unknown = any(c.result == MatchResult.UNKNOWN for c in criteria)
            link.verification = {
                "phase": "verification",
                "criteria": [c.model_dump(mode="json") for c in criteria],
                "missing_subtitle_languages": missing_subs,
                "complete": state["complete"],
            }
            if mismatch:
                link.pending = False
                sub.status, sub.last_error = (
                    "needs_selection",
                    "Фактические параметры не соответствуют требованиям",
                )
                decision = db.scalar(
                    select(CandidateDecision)
                    .join(Release)
                    .where(
                        CandidateDecision.subtask_id == sub.id,
                        Release.id == db.get(Download, asset.download_id).release_id,
                    )
                )
                if decision:
                    decision.action = "rejected"
                queue_episode_notification(db, sub, "selection")
                return
            if unknown and not link.override:
                sub.status, sub.last_error = (
                    "needs_selection",
                    "Не удалось подтвердить фактические параметры; требуется ручной выбор",
                )
                queue_episode_notification(db, sub, "selection")
                return
            sub.status = "done" if state["complete"] else "ready"
            sub.last_error = None
            if state["complete"]:
                link.current, link.pending = True, False
                part_key = f"episode:{sub.episode_id}" if sub.episode_id else "movie"
                library_asset = db.scalar(
                    select(LibraryAsset).where(
                        LibraryAsset.media_id == task.media_id,
                        LibraryAsset.part_key == part_key,
                        LibraryAsset.asset_id == asset.id,
                    )
                )
                if library_asset is None:
                    library_asset = LibraryAsset(
                        media_id=task.media_id,
                        episode_id=sub.episode_id,
                        part_key=part_key,
                        asset_id=asset.id,
                    )
                    db.add(library_asset)
                library_asset.preflight = dict(link.preflight)
                library_asset.verification = dict(link.verification)
        if state["complete"]:
            await self.sync_consumers()
