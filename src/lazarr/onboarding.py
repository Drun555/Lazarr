"""First-run setup, using the same provider and secret stores as Settings."""

import asyncio
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from lazarr.models import ConfigEntry, ProviderConfig
from lazarr.security import audit, permitted
from lazarr.telegram import TelegramError


def key(user_id):
    return f"onboarding.{user_id}"


def initial_state():
    return {"current": 0, "states": ["pending"] * 4, "completed": False}


def pending(ctx, user):
    with ctx.db.session() as db:
        row = db.get(ConfigEntry, key(user.id))
        return bool(row and not row.value.get("completed"))


def state(ctx, user):
    with ctx.db.session() as db:
        row = db.get(ConfigEntry, key(user.id))
        return dict(row.value) if row else initial_state()


def save(ctx, user, *, step=None, status=None, current=None, complete=False):
    with ctx.db.session() as db:
        row = db.get(ConfigEntry, key(user.id))
        value = dict(row.value) if row else initial_state()
        states = list(value["states"])
        if step is not None:
            states[step] = status
        value["states"] = states
        if current is not None:
            value["current"] = current
        if complete:
            value.update(completed=True, current=4)
        if row:
            row.value = value
        else:
            db.add(ConfigEntry(key=key(user.id), value=value))
        return value


class TrawlInput(BaseModel):
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Укажите HTTP(S) адрес Trawl без пароля и параметров")
        _ = parsed.port
        return value


class TmdbInput(BaseModel):
    api_key: str = Field(pattern=r"^[a-fA-F0-9]{32}$")


class RutrackerInput(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


class TokenInput(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ProgressInput(BaseModel):
    current: int = Field(ge=0, le=3)
    skip: int | None = Field(default=None, ge=0, le=3)


def install(app, templates, context, authenticated, permission):
    # Serialize setup changes so a slow check cannot overwrite a newer saved key.
    lock = asyncio.Lock()

    @app.get("/onboarding")
    async def page(request: Request):
        try:
            user = authenticated(request)
        except HTTPException:
            return RedirectResponse("/login", 303)
        if not permitted(user, "settings") or not permitted(user, "providers"):
            raise HTTPException(403, "Настройка доступна администратору")
        ctx = context(request)
        value = state(ctx, user)
        if value["completed"]:
            return RedirectResponse("/", 303)
        providers = ctx.plugins.describe()
        trawl_url = next(
            (p["config"]["trawl_url"] for p in providers if p["config"].get("trawl_url")),
            ctx.config.trawl_url,
        )
        return templates.TemplateResponse(
            request=request,
            name="onboarding.html",
            context={"csrf": request.state.csrf, "onboarding": {**value, "trawlUrl": trawl_url}},
        )

    @app.post("/api/v1/onboarding/trawl")
    async def trawl(payload: TrawlInput, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        async with lock:
            try:
                async with httpx.AsyncClient(timeout=12, transport=ctx.plugins.transport) as client:
                    response = await client.get(payload.url + "/health")
                    response.raise_for_status()
                    result = response.json()
                    if not isinstance(result, dict) or not (
                        result.get("status") == "ok" or result.get("ok") is True
                    ):
                        raise ValueError("Unexpected health response")
            except (httpx.HTTPError, httpx.InvalidURL, ValueError):
                raise HTTPException(502, "Trawl недоступен. Укажите другой адрес.") from None
            for provider in ctx.plugins.describe():
                if any(f["name"] == "trawl_url" for f in provider["config_fields"]):
                    ctx.plugins.configure(provider["id"], {"trawl_url": payload.url}, provider["enabled"])
            save(ctx, user, step=0, status="success", current=1)
            with ctx.db.session() as db:
                audit(db, user.id, "onboarding.trawl", "trawl")
        return {"ok": True}

    @app.post("/api/v1/onboarding/tmdb")
    async def tmdb(payload: TmdbInput, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        async with lock:
            async with ctx.plugins.open("tmdb", allow_disabled=True, bypass_cooldown=True) as provider:
                provider.ctx.config["api_key"] = payload.api_key
                result = await provider.healthcheck()
                if not result.get("ok"):
                    raise HTTPException(502, "TMDB не подтвердил ключ")
            ctx.plugins.configure("tmdb", {"api_key": payload.api_key}, True)
            save(ctx, user, step=1, status="success")
            with ctx.db.session() as db:
                audit(db, user.id, "onboarding.tmdb", "tmdb")
        return {"ok": True}

    @app.post("/api/v1/onboarding/rutracker")
    async def rutracker(payload: RutrackerInput, request: Request, user=Depends(permission("providers"))):
        ctx = context(request)
        async with lock:
            async with ctx.plugins.open("rutracker", allow_disabled=True, bypass_cooldown=True) as provider:
                # Explicit credentials must not accidentally validate an old session cookie.
                provider.ctx.config.pop("session_cookie", None)
                provider.ctx.http.cookies.clear()
                provider.ctx.state.clear()
                result = await provider.authenticate(payload.model_dump())
            if result.status == "authenticated":
                with ctx.db.session() as db:
                    session_state = db.get(ProviderConfig, "rutracker").session_state
                ctx.plugins.configure(
                    "rutracker",
                    {"username": payload.username, "password": payload.password, "session_cookie": None},
                    True,
                )
                # configure invalidates old sessions; keep the session just authenticated above.
                with ctx.db.session() as db:
                    db.get(ProviderConfig, "rutracker").session_state = session_state
                    audit(db, user.id, "onboarding.rutracker", "rutracker")
                save(ctx, user, step=2, status="success")
                ctx.scheduler.wake.set()
            return result

    @app.post("/api/v1/onboarding/telegram")
    async def telegram(payload: TokenInput, request: Request, user=Depends(permission("settings"))):
        ctx = context(request)
        async with lock:
            try:
                await ctx.telegram.configure(True, payload.token, user.id)
            except TelegramError as exc:
                raise HTTPException(502, exc.message) from None
            save(ctx, user, step=3, status="success")
        return {"ok": True}

    @app.post("/api/v1/onboarding/progress")
    async def progress(payload: ProgressInput, request: Request, user=Depends(permission("settings"))):
        ctx = context(request)
        async with lock:
            value = state(ctx, user)
            skip = payload.skip
            return save(
                ctx,
                user,
                current=payload.current,
                step=skip if skip is not None and value["states"][skip] != "success" else None,
                status="skipped",
            )

    @app.post("/api/v1/onboarding/complete")
    async def complete(request: Request, user=Depends(permission("settings"))):
        ctx = context(request)
        async with lock:
            save(ctx, user, complete=True)
        return {"ok": True}
