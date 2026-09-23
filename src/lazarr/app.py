import asyncio
import hashlib
import fcntl
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, delete, text
from sqlalchemy.exc import IntegrityError
from lazarr.config import RuntimeConfig, Settings, Requirements
from lazarr.db import Database
from lazarr.models import (
    User,
    LoginSession,
    Download,
    CandidateDecision,
    Release,
    Task,
    Subtask,
    SubtaskAsset,
    MediaAsset,
    LibraryAsset,
)
from lazarr.security import (
    SecretStore,
    verify_password,
    password_hash,
    hash_token,
    new_session,
    permitted,
    change_account,
    audit,
)
from lazarr.plugins import PluginManager
from lazarr.sdk import ProviderError, DownloadSource
from lazarr.services import TaskService, CreateTask, SeasonSelection
from lazarr.torrent import LibtorrentEngine
from lazarr.worker import Worker
from lazarr.scheduler import Scheduler
from lazarr.library import LibraryService
from lazarr.background import BackgroundTasks
from lazarr.preparation import MediaPreparation
from lazarr.posters import poster_url, fetch_poster
from lazarr.telegram import TelegramService, TelegramError


class Context:
    def __init__(self, config):
        self.config = config
        self.lock_file = (config.data_dir / "instance.lock").open("a")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Only one Lazarr process may use a data directory") from exc
        self.db = Database(config.data_dir / "lazarr.sqlite")
        self.db.migrate()
        self.secret_store = SecretStore(config.data_dir / "secret.key")
        self.plugins = PluginManager(self.db, config, self.secret_store)
        self.plugins.bootstrap()
        self.service = TaskService(self.db, self.plugins)
        self.service.settings()
        self.library = LibraryService(self.db, self.plugins, self.service)
        self.background_tasks = BackgroundTasks()
        self.preparation = MediaPreparation(self)
        self.engine_error = None
        try:
            self.engine = LibtorrentEngine(config.data_dir, config.listen_interfaces)
        except ImportError:
            self.engine = None
            self.engine_error = "libtorrent недоступен; установите системные Python bindings"
        self.worker = Worker(self.db, self.plugins, self.service, self.engine, config)
        self.scheduler = Scheduler(self.worker, self.service)
        self.login_attempts = defaultdict(list)
        self.telegram = TelegramService(self.db, self.secret_store, config.background)
        from lazarr.telegram_menu import TelegramMenu

        self.telegram.menu = TelegramMenu(self, self.telegram)

    async def close(self):
        await self.preparation.close()
        await self.telegram.stop()
        if self.config.background:
            await self.scheduler.stop()
        elif self.engine:
            await asyncio.to_thread(self.engine.close)
        await self.background_tasks.close()
        self.db.engine.dispose()
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        self.lock_file.close()


def context(request: Request):
    return request.app.state.ctx


def authenticated(request: Request):
    ctx = context(request)
    token = request.cookies.get("lazarr_session", "")
    with ctx.db.session() as db:
        session = db.get(LoginSession, hash_token(token))
        user = db.get(User, session.user_id) if session and session.expires_at > time.time() else None
        if not user or not user.active:
            raise HTTPException(401, "Войдите в аккаунт")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, session.csrf):
                raise HTTPException(403, "Недействительный CSRF token; обновите страницу")
        request.state.csrf = session.csrf
        return user


def permission(name):
    def dependency(user: Annotated[User, Depends(authenticated)]):
        if not permitted(user, name):
            raise HTTPException(403, "Недостаточно прав")
        return user

    return dependency


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("username")
    @classmethod
    def nonempty_username(cls, value):
        if not value.strip():
            raise ValueError("Логин не может быть пустым")
        return value.strip()


class AccountInput(LoginInput):
    pass


class SetupInput(LoginInput):
    password: str = Field(min_length=10, max_length=1024)
    password_confirm: str = Field(min_length=10, max_length=1024)


class AccountEdit(BaseModel):
    active: bool | None = None
    password: str | None = None


class TaskEdit(BaseModel):
    requirements: Requirements | None = None
    paused: bool | None = None


class TaskDelete(BaseModel):
    delete_media: bool = False


class MediaDelete(BaseModel):
    delete_files: bool = False


class ProviderOrder(BaseModel):
    ids: list[str]


class ProviderEdit(BaseModel):
    enabled: bool
    config: dict[str, str | None] = Field(default_factory=dict)


class AuthInput(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class TelegramSettings(BaseModel):
    token: str | None = Field(default=None, max_length=256)


class TelegramDecision(BaseModel):
    status: str = Field(pattern="^(approved|blocked)$")


class ChoiceInput(BaseModel):
    reject: bool = False
    video_index: int | None = None
    track_indices: list[int] = Field(default_factory=list)


class ManualCandidateInput(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadAction(BaseModel):
    action: str = Field(pattern="^(pause|resume)$")


def create_app(config: RuntimeConfig | None = None):
    config = config or RuntimeConfig()

    @asynccontextmanager
    async def lifespan(app):
        ctx = Context(config)
        app.state.ctx = ctx
        if config.background:
            await ctx.scheduler.start()
            await ctx.telegram.start()
            ctx.preparation.start()
        try:
            yield
        finally:
            await ctx.close()

    app = FastAPI(title="Lazarr", version="0.1.0", lifespan=lifespan)
    package = Path(__file__).parent
    templates = Jinja2Templates(directory=package / "templates")
    templates.env.globals["test_environment"] = config.test_environment
    from lazarr.languages import LABELS, ALIASES

    templates.env.globals["asset_version"] = hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted((package / "static").glob("*")) if p.is_file())
    ).hexdigest()[:16]
    templates.env.globals["language_labels"] = LABELS
    templates.env.globals["language_aliases"] = ALIASES
    templates.env.filters["poster_url"] = poster_url
    app.mount("/static", StaticFiles(directory=package / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if config.log == "performance":
        from lazarr.performance import PerformanceMiddleware

        app.add_middleware(PerformanceMiddleware)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(
            {"detail": "Запись уже существует или связана с другими данными"}, status_code=409
        )

    @app.exception_handler(ProviderError)
    async def provider_failure(request, exc):
        return JSONResponse(
            {"detail": str(exc), "code": exc.code, "retry_after": exc.retry_after}, status_code=502
        )

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/v1/status")
    async def status(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        return {
            "engine_available": ctx.engine is not None,
            "engine_error": ctx.engine_error,
            "content_providers": ctx.plugins.available("content"),
            "background": config.background,
            "search": ctx.scheduler.snapshot(),
        }

    @app.get("/api/v1/background-tasks")
    async def background_tasks(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        downloads = await asyncio.to_thread(ctx.service.download_activity)
        return {**ctx.background_tasks.snapshot(user.id), "downloads": downloads}

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        with context(request).db.session() as db:
            first_run = db.scalar(select(User.id).limit(1)) is None
        if first_run:
            return RedirectResponse("/setup", 303)
        return entry_page(request, "login.html")

    def entry_page(request, template):
        token = secrets.token_urlsafe(32)
        response = templates.TemplateResponse(
            request=request,
            name=template,
            context={"csrf": token, "theme_color": context(request).service.settings().theme_color},
        )
        response.set_cookie(
            "lazarr_login_csrf",
            token,
            httponly=True,
            samesite="strict",
            secure=config.secure_cookie,
            max_age=3600,
        )
        return response

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        with context(request).db.session() as db:
            if db.scalar(select(User.id).limit(1)) is not None:
                return RedirectResponse("/login", 303)
        return entry_page(request, "setup.html")

    @app.post("/api/v1/setup", status_code=201)
    async def setup(payload: SetupInput, request: Request):
        cookie = request.cookies.get("lazarr_login_csrf", "")
        if not cookie or not secrets.compare_digest(cookie, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "Обновите страницу настройки")
        ctx = context(request)
        with ctx.db.session() as db:
            if db.scalar(select(User.id).limit(1)) is not None:
                raise HTTPException(409, "Первый аккаунт уже создан")
        if payload.password != payload.password_confirm:
            raise HTTPException(422, "Пароли не совпадают")
        encoded = await asyncio.to_thread(password_hash, payload.password)
        with ctx.db.session() as db:
            # Serialize bootstrap with other web requests and the CLI.
            db.execute(text("BEGIN IMMEDIATE"))
            if db.scalar(select(User.id).limit(1)) is not None:
                raise HTTPException(409, "Первый аккаунт уже создан")
            user = User(username=payload.username, password_hash=encoded, role="admin")
            db.add(user)
            db.flush()
            token, csrf = new_session(db, user)
            audit(db, user.id, "account.bootstrap", str(user.id))
        response = JSONResponse({"csrf": csrf}, status_code=201)
        response.set_cookie(
            "lazarr_session",
            token,
            httponly=True,
            samesite="lax",
            secure=config.secure_cookie,
            max_age=86400 * 7,
        )
        response.delete_cookie("lazarr_login_csrf")
        return response

    @app.post("/api/v1/session")
    async def login(payload: LoginInput, request: Request):
        ctx = context(request)
        cookie = request.cookies.get("lazarr_login_csrf", "")
        if not cookie or not secrets.compare_digest(cookie, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "Обновите страницу входа")
        client = request.client.host if request.client else "unknown"
        attempts = ctx.login_attempts[client] = [
            v for v in ctx.login_attempts[client] if v > time.time() - 300
        ]
        if len(attempts) >= 10:
            raise HTTPException(429, "Слишком много попыток. Повторите через пять минут")
        with ctx.db.session() as db:
            user = db.scalar(select(User).where(User.username == payload.username.strip()))
            valid = (
                await asyncio.to_thread(verify_password, user.password_hash, payload.password)
                if user
                else False
            )
            if not user or not user.active or not valid:
                attempts.append(time.time())
                raise HTTPException(401, "Неверный логин или пароль")
            token, csrf = new_session(db, user)
            db.execute(delete(LoginSession).where(LoginSession.expires_at < time.time()))
            audit(db, user.id, "session.login", str(user.id))
        attempts.clear()
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(
            "lazarr_session",
            token,
            httponly=True,
            samesite="lax",
            secure=config.secure_cookie,
            max_age=86400 * 7,
        )
        response.delete_cookie("lazarr_login_csrf")
        return response

    @app.delete("/api/v1/session")
    async def logout(request: Request, user=Depends(authenticated)):
        with context(request).db.session() as db:
            session = db.get(LoginSession, hash_token(request.cookies.get("lazarr_session", "")))
            if session:
                db.delete(session)
        response = JSONResponse({"ok": True})
        response.delete_cookie("lazarr_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        try:
            user = authenticated(request)
        except HTTPException:
            return RedirectResponse("/login", 303)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "user": user,
                "csrf": request.state.csrf,
                "theme_color": context(request).service.settings().theme_color,
            },
        )

    @app.get("/ui/search", response_class=HTMLResponse)
    async def search_fragment(
        request: Request, q: str = Query(default="", max_length=200), user=Depends(authenticated)
    ):
        results, errors = await metadata_search(context(request), q)
        return templates.TemplateResponse(
            request=request, name="results.html", context={"results": results, "errors": errors, "query": q}
        )

    async def metadata_search(ctx, query):
        if len(query.strip()) < 2:
            return [], []
        providers = ctx.plugins.available("metadata")

        async def search_one(key):
            async with ctx.plugins.open(key) as provider:
                return await provider.search(query.strip())

        responses = await asyncio.gather(*(search_one(key) for key in providers), return_exceptions=True)
        results, errors = [], []
        for key, response in zip(providers, responses):
            if isinstance(response, Exception):
                errors.append(
                    f"{key}: {str(response) if isinstance(response, ProviderError) else 'Ошибка поиска'}"
                )
            else:
                results.extend(response)
        if not providers:
            errors.append("Включите провайдер метаданных в настройках")
        return results, errors

    @app.get("/api/v1/metadata/search")
    async def search_api(
        request: Request, q: str = Query(min_length=2, max_length=200), user=Depends(authenticated)
    ):
        items, errors = await metadata_search(context(request), q)
        return {"items": [v.model_dump() for v in items], "errors": errors}

    @app.get("/api/v1/posters/tmdb/{filename}")
    async def poster(filename: str, request: Request, user=Depends(authenticated)):
        path = await fetch_poster(context(request), filename)
        return FileResponse(path)

    @app.get("/api/v1/metadata/{provider_id}/{kind}/{media_id}")
    async def media_detail(
        provider_id: str, kind: str, media_id: str, request: Request, user=Depends(authenticated)
    ):
        async with context(request).plugins.open(provider_id) as provider:
            return await provider.get_media(kind, media_id)

    @app.get("/api/v1/metadata/{provider_id}/tv/{media_id}/seasons/{number}")
    async def season_detail(
        provider_id: str, media_id: str, number: int, request: Request, user=Depends(authenticated)
    ):
        async with context(request).plugins.open(provider_id) as provider:
            return await provider.get_season(media_id, number)

    @app.get("/api/v1/tasks")
    async def tasks(request: Request, user=Depends(authenticated)):
        return context(request).service.list_tasks()

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(payload: CreateTask, request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        identity = await ctx.service.create(payload, user.id)
        ctx.scheduler.wake.set()
        return {"id": identity, "search_queued": True}

    @app.post("/api/v1/search/run", status_code=202)
    async def run_queue(request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        if not ctx.plugins.available("content"):
            raise ValueError("Включите провайдеры контента в настройках")
        identity = ctx.scheduler.retry_now()
        with ctx.db.session() as db:
            audit(db, user.id, "search.run", identity)
        snapshot = ctx.scheduler.snapshot()
        return {"queued": bool(snapshot["pending_requests"]), "search": snapshot}

    @app.patch("/api/v1/tasks/{identity}")
    async def edit_task(
        identity: int, payload: TaskEdit, request: Request, user=Depends(permission("tasks"))
    ):
        ctx = context(request)
        async with ctx.worker.lock:
            ctx.service.edit(identity, user.id, requirements=payload.requirements, paused=payload.paused)
            await ctx.worker.sync_consumers()
        ctx.scheduler.wake.set()
        return {"ok": True}

    @app.post("/api/v1/tasks/{identity}/search", status_code=202)
    async def run_task(identity: int, request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        with ctx.db.session() as db:
            task = db.get(Task, identity)
            if task is None:
                raise HTTPException(404, "Задача не найдена")
            if task.paused:
                raise ValueError("Сначала возобновите задачу")
        ctx.scheduler.retry_now(identity)
        return {"queued": True}

    @app.post("/api/v1/libraries/media/{identity}/seasons")
    async def add_media_season(
        identity: int, payload: SeasonSelection, request: Request, user=Depends(permission("tasks"))
    ):
        ctx = context(request)
        task_id = await ctx.service.add_season(identity, payload, user.id)
        ctx.scheduler.wake.set()
        return {"id": task_id, "search_queued": True}

    @app.get("/api/v1/libraries/media/{identity}/seasons/{number}/episodes")
    async def library_season_episodes(
        identity: int, number: int, request: Request, user=Depends(permission("library"))
    ):
        await context(request).library.load_season(identity, number)
        return {"ok": True}

    @app.delete("/api/v1/libraries/media/{identity}/seasons/{number}")
    async def delete_library_season(
        identity: int, number: int, request: Request, user=Depends(permission("tasks"))
    ):
        from lazarr.deletion import delete_season

        return await delete_season(context(request).worker, identity, number, user.id)

    @app.delete("/api/v1/tasks/{identity}")
    async def delete_task(
        identity: int, payload: TaskDelete, request: Request, user=Depends(permission("tasks"))
    ):
        from lazarr.deletion import delete_task as remove_task

        return await remove_task(context(request).worker, identity, user.id, payload.delete_media)

    @app.delete("/api/v1/subtasks/{identity}/selection")
    async def delete_episode_selection(identity: int, request: Request, user=Depends(permission("tasks"))):
        from lazarr.deletion import delete_selection

        return await delete_selection(context(request).worker, identity, user.id)

    @app.delete("/api/v1/libraries/media/{media_id}/episodes/{identity}/selection")
    async def delete_library_episode_selection(
        media_id: int, identity: str, request: Request, user=Depends(permission("tasks"))
    ):
        from lazarr.deletion import delete_selection

        return await delete_selection(context(request).worker, identity, user.id, media_id=media_id)

    @app.get("/api/v1/subtasks/{identity}/candidates")
    async def candidates(identity: int, request: Request, user=Depends(authenticated)):
        return context(request).service.candidates(identity)

    @app.get("/api/v1/subtasks/{identity}/search")
    async def subtask_search(identity: int, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            subtask = db.get(Subtask, identity)
            if not subtask:
                raise HTTPException(404, "Серия не найдена")
            task_id = subtask.task_id
        return ctx.scheduler.snapshot(task_id)

    @app.post("/api/v1/subtasks/{identity}/candidates/search")
    async def search_alternatives(identity: int, request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        await ctx.worker.search_alternatives(identity)
        return ctx.service.candidates(identity)

    @app.post("/api/v1/subtasks/{identity}/candidates/manual")
    async def add_manual_candidate(
        identity: int,
        payload: ManualCandidateInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        decision_id = await ctx.worker.add_manual_candidate(identity, payload.url)
        with ctx.db.session() as db:
            audit(db, user.id, "candidate.manual", str(decision_id), {"subtask_id": identity})
        return {"id": decision_id}

    @app.get("/api/v1/tasks/{identity}/candidates")
    async def task_candidates(identity: int, request: Request, user=Depends(authenticated)):
        return context(request).service.task_candidates(identity)

    @app.post("/api/v1/tasks/{identity}/candidates/manual")
    async def add_manual_task_candidate(
        identity: int,
        payload: ManualCandidateInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        decision_id = await ctx.worker.add_manual_task_candidate(identity, payload.url)
        with ctx.db.session() as db:
            audit(db, user.id, "candidate.manual_task", str(decision_id), {"task_id": identity})
        return {"id": decision_id}

    @app.get("/api/v1/tasks/{identity}/seasons/{season}/candidates")
    async def season_candidates(identity: int, season: int, request: Request, user=Depends(authenticated)):
        return context(request).service.task_candidates(identity, season)

    @app.post("/api/v1/tasks/{identity}/seasons/{season}/candidates/manual")
    async def add_manual_season_candidate(
        identity: int,
        season: int,
        payload: ManualCandidateInput,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        decision_id = await ctx.worker.add_manual_task_candidate(identity, payload.url, season)
        with ctx.db.session() as db:
            audit(
                db,
                user.id,
                "candidate.manual_season",
                str(decision_id),
                {"task_id": identity, "season": season},
            )
        return {"id": decision_id}

    @app.get("/api/v1/candidates/{identity}/selection")
    async def candidate_selection(
        identity: int,
        request: Request,
        video_index: int | None = Query(default=None, ge=0),
        user=Depends(authenticated),
    ):
        with context(request).db.session() as db:
            decision = db.get(CandidateDecision, identity)
            if not decision:
                raise HTTPException(404, "Кандидат не найден")
            link = db.scalar(
                select(SubtaskAsset)
                .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
                .join(Download, MediaAsset.download_id == Download.id)
                .where(
                    SubtaskAsset.subtask_id == decision.subtask_id, Download.release_id == decision.release_id
                )
            )
            if (
                video_index is None
                or link
                and link.preflight.get("binding", {}).get("video_index") == video_index
            ):
                return link.preflight.get("binding") if link else None
            # Another video may already belong to another episode or to the
            # library after its task was removed. Use persisted bindings only.
            bindings = []
            for model in (SubtaskAsset, LibraryAsset):
                for selected in db.scalars(
                    select(model)
                    .join(MediaAsset, model.asset_id == MediaAsset.id)
                    .join(Download, MediaAsset.download_id == Download.id)
                    .where(Download.release_id == decision.release_id, MediaAsset.video_index == video_index)
                ):
                    binding = selected.preflight.get("binding")
                    if binding:
                        bindings.append(binding)
            if not bindings:
                return None
            tracks = {
                track["file_index"]: track
                for binding in bindings
                for track in binding.get("tracks", [])
                if track.get("file_index") is not None
            }
            return {**bindings[0], "tracks": list(tracks.values())}

    @app.get("/api/v1/candidates/{identity}/files")
    async def candidate_files(identity: int, request: Request, user=Depends(authenticated)):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        with ctx.db.session() as db:
            decision = db.get(CandidateDecision, identity)
            if not decision:
                raise HTTPException(404, "Кандидат не найден")
            release = db.get(Release, decision.release_id)
            path = ctx.config.data_dir / "torrents" / f"{release.revision}.torrent"
        metadata = await asyncio.to_thread(ctx.engine.inspect, DownloadSource(torrent=path.read_bytes()))
        from lazarr.matcher import episode_numbers

        result = []
        for file in metadata.files:
            season, episodes, _ = episode_numbers(file.path)
            result.append(
                {**file.model_dump(), "episode_order": [season or 0, min(episodes)] if episodes else None}
            )
        return result

    @app.post("/api/v1/candidates/{identity}/choice")
    async def choose_candidate(
        identity: int, payload: ChoiceInput, request: Request, user=Depends(permission("tasks"))
    ):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        await ctx.worker.choose(identity, user.id, payload.reject, payload.video_index, payload.track_indices)
        if not payload.reject:
            ctx.scheduler.discard_satisfied()
        return {"ok": True}

    @app.post("/api/v1/candidates/{identity}/choice-all")
    async def choose_candidate_for_task(identity: int, request: Request, user=Depends(permission("tasks"))):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        result = await ctx.worker.choose_all(identity, user.id)
        ctx.scheduler.discard_satisfied()
        return result

    @app.post("/api/v1/tasks/{task_id}/seasons/{season}/candidates/{identity}/choice")
    async def choose_candidate_for_season(
        task_id: int,
        season: int,
        identity: int,
        request: Request,
        user=Depends(permission("tasks")),
    ):
        ctx = context(request)
        if ctx.engine is None:
            raise HTTPException(503, ctx.engine_error)
        result = await ctx.worker.choose_all(identity, user.id, season, task_id)
        ctx.scheduler.discard_satisfied()
        return result

    @app.get("/api/v1/settings")
    async def settings(request: Request, user=Depends(permission("settings"))):
        return context(request).service.settings()

    @app.get("/api/v1/telegram")
    async def telegram_settings(request: Request, user=Depends(permission("settings"))):
        return context(request).telegram.describe()

    @app.put("/api/v1/telegram")
    async def put_telegram(payload: TelegramSettings, request: Request, user=Depends(permission("settings"))):
        try:
            await context(request).telegram.configure(
                True,
                payload.token,
                user.id,
            )
        except TelegramError as exc:
            raise HTTPException(502, exc.message) from None
        return context(request).telegram.describe()

    @app.get("/api/v1/telegram/users")
    async def telegram_users(request: Request, user=Depends(permission("settings"))):
        return context(request).telegram.users()

    @app.patch("/api/v1/telegram/users/{identity}")
    async def telegram_decide(
        identity: int, payload: TelegramDecision, request: Request, user=Depends(permission("settings"))
    ):
        context(request).telegram.decide(identity, payload.status, user.id)
        return {"ok": True}

    @app.put("/api/v1/settings")
    async def put_settings(payload: Settings, request: Request, user=Depends(permission("settings"))):
        context(request).service.set_settings(payload, user.id)
        return {"ok": True}

    @app.get("/api/v1/accounts")
    async def accounts(request: Request, user=Depends(permission("accounts"))):
        with context(request).db.session() as db:
            return [
                {"id": v.id, "username": v.username, "role": v.role, "active": v.active}
                for v in db.scalars(select(User))
            ]

    @app.post("/api/v1/accounts", status_code=201)
    async def add_account(payload: AccountInput, request: Request, user=Depends(permission("accounts"))):
        encoded = await asyncio.to_thread(password_hash, payload.password)
        with context(request).db.session() as db:
            account = User(username=payload.username.strip(), password_hash=encoded, role="admin")
            db.add(account)
            db.flush()
            audit(db, user.id, "account.create", str(account.id))
            return {"id": account.id}

    @app.patch("/api/v1/accounts/{identity}")
    async def edit_account(
        identity: int, payload: AccountEdit, request: Request, user=Depends(permission("accounts"))
    ):
        with context(request).db.session() as db:
            account = db.get(User, identity)
            if not account:
                raise HTTPException(404, "Аккаунт не найден")
            change_account(db, account, active=payload.active, password=payload.password)
            audit(db, user.id, "account.update", str(identity))
        return {"ok": True}

    @app.get("/api/v1/libraries")
    async def libraries(request: Request, user=Depends(permission("library"))):
        ctx = context(request)
        return await asyncio.to_thread(ctx.library.list)

    @app.get("/api/v1/libraries/media/{identity}")
    async def library_media(identity: int, request: Request, user=Depends(permission("library"))):
        ctx = context(request)
        result = await ctx.background_tasks.run(
            "library-detail", ctx.library.detail, identity, lane="catalog"
        )
        if result is None:
            raise HTTPException(404, "Произведение не найдено")
        task = next(
            iter(await asyncio.to_thread(ctx.service.list_tasks, identity)),
            None,
        )
        result["task"] = task
        result["search"] = await asyncio.to_thread(ctx.scheduler.snapshot, task["id"]) if task else None
        return result

    @app.delete("/api/v1/libraries/media/{identity}")
    async def delete_library_media(
        identity: int, payload: MediaDelete, request: Request, user=Depends(permission("library"))
    ):
        from lazarr.deletion import delete_media

        return await delete_media(context(request).worker, identity, user.id, payload.delete_files)

    @app.get("/api/v1/providers")
    async def providers(request: Request, user=Depends(permission("providers"))):
        return context(request).plugins.describe()

    @app.post("/api/v1/providers/{identity}/secrets/{field_name}/reveal")
    async def reveal_provider_secret(
        identity: str, field_name: str, request: Request, user=Depends(permission("providers"))
    ):
        ctx = context(request)
        value = ctx.plugins.secret(identity, field_name)
        if value is None:
            raise HTTPException(404, "Секрет не найден")
        with ctx.db.session() as db:
            audit(db, user.id, "provider.secret.reveal", f"{identity}.{field_name}")
        return JSONResponse({"value": value}, headers={"Cache-Control": "no-store"})

    @app.put("/api/v1/providers/order")
    async def order_providers(
        payload: ProviderOrder, request: Request, user=Depends(permission("providers"))
    ):
        ctx = context(request)
        ctx.plugins.set_content_order(payload.ids)
        with ctx.db.session() as db:
            audit(db, user.id, "provider.order", ",".join(payload.ids))
        return {"ok": True}

    @app.put("/api/v1/providers/{identity}")
    async def configure_provider(
        identity: str, payload: ProviderEdit, request: Request, user=Depends(permission("providers"))
    ):
        ctx = context(request)
        ctx.plugins.configure(identity, payload.config, payload.enabled)
        ctx.scheduler.wake.set()
        with ctx.db.session() as db:
            audit(db, user.id, "provider.configure", identity)
        return {"ok": True}

    @app.post("/api/v1/providers/{identity}/authenticate")
    async def auth_provider(
        identity: str, payload: AuthInput, request: Request, user=Depends(permission("providers"))
    ):
        async with context(request).plugins.open(
            identity, allow_disabled=True, bypass_cooldown=True
        ) as provider:
            return await provider.authenticate(payload.values)

    @app.get("/api/v1/providers/{identity}/auth")
    async def provider_auth_status(identity: str, request: Request, user=Depends(permission("providers"))):
        async with context(request).plugins.open(
            identity, allow_disabled=True, bypass_cooldown=True
        ) as provider:
            return await provider.auth_status()

    @app.delete("/api/v1/providers/{identity}/auth")
    async def provider_logout(identity: str, request: Request, user=Depends(permission("providers"))):
        async with context(request).plugins.open(
            identity, allow_disabled=True, bypass_cooldown=True
        ) as provider:
            await provider.logout()
        return {"ok": True}

    @app.post("/api/v1/providers/{identity}/health")
    async def provider_health(identity: str, request: Request, user=Depends(permission("providers"))):
        async with context(request).plugins.open(
            identity, allow_disabled=True, bypass_cooldown=True
        ) as provider:
            return await provider.healthcheck()

    @app.get("/api/v1/plugin-catalog")
    async def catalog(request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        url = ctx.service.settings().plugin_repository
        return await ctx.plugins.catalog(url) if url else []

    @app.post("/api/v1/providers/{identity}/update")
    async def update_plugin(identity: str, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        try:
            result = await ctx.plugins.update(ctx.service.settings().plugin_repository, identity)
        except (ValueError, ProviderError):
            raise
        except Exception as exc:
            raise HTTPException(502, "Не удалось получить обновление; активная версия сохранена") from exc
        with ctx.db.session() as db:
            audit(db, user.id, "plugin.update", identity, {"version": result.version})
        return result

    @app.post("/api/v1/providers/{identity}/rollback")
    async def rollback_plugin(identity: str, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        result = await ctx.plugins.rollback(identity)
        with ctx.db.session() as db:
            audit(db, user.id, "plugin.rollback", identity, {"version": result.version})
        return result

    @app.post("/api/v1/providers/{identity}/bundled")
    async def bundled_plugin(identity: str, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        result = ctx.plugins.use_bundled(identity)
        with ctx.db.session() as db:
            audit(db, user.id, "plugin.bundled", identity, {"version": result.version})
        return result

    @app.get("/api/v1/downloads")
    async def downloads(request: Request, user=Depends(authenticated)):
        ctx = context(request)
        with ctx.db.session() as db:
            result = []
            for download in db.scalars(select(Download).order_by(Download.created_at.desc())):
                release = db.get(Release, download.release_id)
                result.append(
                    {
                        "id": download.id,
                        "title": release.data.get("title", download.infohash),
                        "state": download.state,
                        "path": download.save_path,
                        "infohash": download.infohash,
                        "seed_ratio": download.seed_ratio,
                        "ratio": download.uploaded / download.downloaded if download.downloaded else 0,
                        "stats": download.stats,
                        "bindings": download.plan.get("bindings", []),
                    }
                )
            return result

    @app.post("/api/v1/downloads/{identity}/action")
    async def download_action(
        identity: int, payload: DownloadAction, request: Request, user=Depends(permission("downloads"))
    ):
        ctx = context(request)
        with ctx.db.session() as db:
            download = db.get(Download, identity)
            if not download:
                raise HTTPException(404, "Загрузка не найдена")
            download.manual_paused = payload.action == "pause"
            download.state = "paused" if download.manual_paused else "downloading"
            audit(db, user.id, f"download.{payload.action}", str(identity))
        await ctx.worker.sync_consumers()
        return {"ok": True}

    from lazarr.jellyfin import install_jellyfin_api

    install_jellyfin_api(app, context)

    return app
