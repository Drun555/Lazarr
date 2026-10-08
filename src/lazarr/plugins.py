import asyncio
import importlib
import inspect
import os
import re
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import httpx
from packaging.specifiers import SpecifierSet
from sqlalchemy import select
from lazarr.models import ProviderConfig, ConfigEntry
from lazarr.rate_limit import RequestPacer
from lazarr.sdk import (
    Provider,
    ProviderContext,
    ProviderError,
    SDK_VERSION,
    ContentProvider,
    MetadataProvider,
    ReleaseCalendarProvider,
)


PROVIDER_TYPES = {
    "content": ContentProvider,
    "metadata": MetadataProvider,
    "calendar": ReleaseCalendarProvider,
}


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as file:
        os.chmod(temp, 0o600)
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    temp.replace(path)


class PluginManager:
    def __init__(self, db, config, secrets, transport=None):
        self.db, self.config, self.secrets = db, config, secrets
        from lazarr.search_runtime import SearchEngineManager

        self.search_engine = SearchEngineManager()
        self.classes: dict[str, type[Provider]] = {}
        self.errors: dict[str, str] = {}
        self.transport = transport
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.request_pacers = {}
        self.request_interval = 2.0
        self.request_interval_ranges = {"rutracker": (10.0, 30.0)}

    def bootstrap(self):
        self.search_engine.bootstrap()
        self.classes.clear()
        self.errors.clear()
        for name in ("tmdb", "nyaa", "rutracker", "kinozal"):
            cls = importlib.import_module(f"lazarr.providers.{name}").Plugin
            manifest = cls.manifest
            expected = PROVIDER_TYPES.get(manifest.kind)
            if (
                manifest.id != name
                or expected is None
                or not issubclass(cls, expected)
                or inspect.isabstract(cls)
            ):
                raise ValueError(f"Invalid provider contract: {name}")
            if SDK_VERSION not in SpecifierSet(manifest.sdk):
                raise ValueError(f"Incompatible provider SDK: {name}")
            self.classes[name] = cls
        with self.db.session() as db:
            for plugin_id in self.classes:
                if not db.get(ProviderConfig, plugin_id):
                    db.add(ProviderConfig(id=plugin_id, enabled=plugin_id == "tmdb"))

    def content_order(self):
        with self.db.session() as db:
            row = db.get(ConfigEntry, "providers.content_order")
            saved = row.value["ids"] if row else []
        ids = [key for key, cls in self.classes.items() if cls.manifest.kind == "content"]
        return [key for key in saved if key in ids] + [key for key in ids if key not in saved]

    def set_content_order(self, ids):
        current = self.content_order()
        enabled = self.available("content")
        if len(ids) != len(set(ids)) or set(ids) not in (set(current), set(enabled)):
            raise ValueError("Укажите контент-провайдеры ровно один раз; обновите настройки")
        order = ids + [key for key in current if key not in ids]
        with self.db.session() as db:
            row = db.get(ConfigEntry, "providers.content_order")
            if row:
                row.value = {"ids": order}
            else:
                db.add(ConfigEntry(key="providers.content_order", value={"ids": order}))

    def available(self, kind=None):
        with self.db.session() as db:
            enabled = {
                row.id for row in db.scalars(select(ProviderConfig).where(ProviderConfig.enabled.is_(True)))
            }
        ids = [
            key
            for key, cls in self.classes.items()
            if key in enabled and (kind is None or cls.manifest.kind == kind)
        ]
        order = self.content_order()
        return sorted(ids, key=lambda key: order.index(key) if key in order else -1)

    def manual_candidate(self, value):
        """Turn a URL from a shipped content provider into a safe candidate stub."""
        from lazarr.sdk import Candidate

        raw = str(value).strip()
        parsed = urlparse(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Укажите корректный HTTP(S) URL раздачи")
        query = parse_qs(parsed.query)
        enabled = set(self.available("content"))
        with self.db.session() as db:
            for plugin_id in ("rutracker", "nyaa", "kinozal"):
                if plugin_id not in enabled or plugin_id not in self.classes:
                    continue
                cls = self.classes[plugin_id]
                row = db.get(ProviderConfig, plugin_id)
                base_field = next(
                    (field for field in cls.manifest.config_fields if field.name == "base_url"), None
                )
                base = (row.config.get("base_url") if row else None) or (
                    base_field.default if base_field else ""
                )
                configured = urlparse(base)
                try:
                    same_origin = (
                        parsed.scheme == configured.scheme
                        and parsed.hostname.casefold() == (configured.hostname or "").casefold()
                        and parsed.port == configured.port
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    continue
                base_path = configured.path.rstrip("/")
                identity = None
                if plugin_id == "rutracker" and parsed.path == base_path + "/viewtopic.php":
                    identity = query.get("t", [None])[0]
                elif plugin_id == "nyaa":
                    match = re.fullmatch(re.escape(base_path) + r"/view/(\d+)/?", parsed.path)
                    identity = match[1] if match else None
                elif plugin_id == "kinozal" and parsed.path == base_path + "/details.php":
                    identity = query.get("id", [None])[0]
                if identity and str(identity).isdigit() and int(identity) > 0:
                    return Candidate(
                        provider=plugin_id,
                        id=str(identity),
                        url=raw,
                        title=f"Ручная раздача #{identity}",
                    )
        raise ValueError("URL не распознан. Используйте ссылку включённого Rutracker, Nyaa или Kinozal")

    def request_pacer(self, plugin_id):
        intervals = self.request_interval_ranges.get(
            plugin_id, (self.request_interval, self.request_interval)
        )
        # Tests and explicit local callers use zero to disable all pacing.
        if self.request_interval <= 0:
            intervals = (0, 0)
        return self.request_pacers.setdefault(plugin_id, RequestPacer(*intervals))

    def search_status(self):
        """Public content-provider availability, without opening a network session."""
        result = []
        with self.db.session() as db:
            for identity, cls in self.classes.items():
                if cls.manifest.kind != "content":
                    continue
                row = db.get(ProviderConfig, identity)
                enabled = bool(row and row.enabled)
                retry_at = row.retry_at if row else 0
                state = "disabled" if not enabled else "cooldown" if retry_at > time.time() else "waiting"
                reason = (
                    "Выключен в настройках"
                    if not enabled
                    else ("Временная пауза после ошибки: " + (row.last_error or "причина не сохранена"))
                    if state == "cooldown"
                    else "Ожидает запроса"
                )
                result.append(
                    {
                        "id": identity,
                        "name": cls.manifest.name,
                        "enabled": enabled,
                        "state": state,
                        "reason": reason,
                        "retry_at": retry_at,
                        "requests": 0,
                        "candidates": 0,
                    }
                )
        order = self.content_order()
        return sorted(result, key=lambda item: order.index(item["id"]))

    def reset_content_cooldowns(self):
        """Let an explicit user search retry enabled content providers immediately."""
        reset = []
        with self.db.session() as db:
            for identity, cls in self.classes.items():
                if cls.manifest.kind != "content":
                    continue
                row = db.get(ProviderConfig, identity)
                if row and row.enabled and row.retry_at > 0:
                    row.retry_at = 0
                    reset.append(identity)
        return reset

    def describe(self):
        with self.db.session() as db:
            result = []
            for key, cls in self.classes.items():
                row = db.get(ProviderConfig, key)
                secrets = self.secrets.decrypt(row.secrets)
                result.append(
                    {
                        **cls.manifest.model_dump(),
                        "enabled": row.enabled,
                        "config": row.config,
                        "configured_secrets": list(secrets),
                        "error": row.last_error,
                        "retry_at": row.retry_at,
                    }
                )
            for key, error in self.errors.items():
                result.append(
                    {
                        "id": key,
                        "name": key,
                        "error": error,
                        "enabled": False,
                        "config_fields": [],
                        "config": {},
                        "configured_secrets": [],
                    }
                )
            order = self.content_order()
            return sorted(result, key=lambda item: order.index(item["id"]) if item["id"] in order else -1)

    def secret(self, plugin_id, field_name):
        cls = self.classes.get(plugin_id)
        if not cls or not any(
            field.name == field_name and field.secret for field in cls.manifest.config_fields
        ):
            return None
        with self.db.session() as db:
            row = db.get(ProviderConfig, plugin_id)
            return self.secrets.decrypt(row.secrets).get(field_name) if row else None

    def configure(self, plugin_id, values, enabled):
        cls = self.classes.get(plugin_id)
        if not cls:
            raise ValueError("Unknown plugin")
        fields = {v.name: v for v in cls.manifest.config_fields}
        if set(values) - fields.keys():
            raise ValueError("Unknown configuration field")
        if "base_url" in values:
            ProviderContext(values, {}, None).base_url("https://example.org")
        with self.db.session() as db:
            row = db.get(ProviderConfig, plugin_id)
            secret_values = self.secrets.decrypt(row.secrets)
            public = dict(row.config)
            changed = False
            for key, value in values.items():
                target = secret_values if fields[key].secret else public
                if fields[key].secret and value == "":
                    continue  # Blank password inputs preserve existing credentials.
                changed |= target.get(key) != value
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = str(value)
            row.config, row.secrets, row.enabled = public, self.secrets.encrypt(secret_values), enabled
            if changed:
                row.session_state = ""
                row.retry_at = 0
                backoff = db.get(ConfigEntry, f"provider.backoff.{plugin_id}")
                if backoff:
                    db.delete(backoff)
                row.last_error = None

    @asynccontextmanager
    async def open(self, plugin_id, *, allow_disabled=False, bypass_cooldown=False):
        # Pin a class before awaiting; updates cannot replace an in-flight generation.
        cls = self.classes.get(plugin_id)
        if not cls:
            raise ProviderError("configuration", "Provider not loaded")
        lock = self.session_locks.setdefault(plugin_id, asyncio.Lock())
        async with lock:
            with self.db.session() as db:
                row = db.get(ProviderConfig, plugin_id)
                if not row or (not row.enabled and not allow_disabled):
                    raise ProviderError("configuration", "Provider disabled")
                if row.retry_at > time.time() and not bypass_cooldown:
                    delay = int(row.retry_at - time.time()) + 1
                    raise ProviderError(
                        "rate_limited",
                        f"Повтор через {delay} с. Причина паузы: {row.last_error or 'временное ограничение'}",
                        delay,
                    )
                config = {v.name: v.default for v in cls.manifest.config_fields}
                config.update(row.config)
                config.update(self.secrets.decrypt(row.secrets))
                state = self.secrets.decrypt(row.session_state)
                original_config = (row.config, row.secrets)
            jar = httpx.Cookies()
            for item in state.pop("cookies", []):
                jar.set(item["name"], item["value"], domain=item["domain"], path=item["path"])
            pacer = self.request_pacer(plugin_id) if cls.manifest.kind == "content" else None
            async with httpx.AsyncClient(
                timeout=25,
                cookies=jar,
                transport=self.transport,
                verify=self._tls_context(cls),
                headers={"User-Agent": "Lazarr/0.1"},
                event_hooks={"request": [pacer.wait]} if pacer else None,
            ) as client:
                context = ProviderContext(config, state, client)
                context.request_gate = pacer.wait if pacer else None
                provider = cls(context)
                try:
                    yield provider
                    with self.db.session() as db:
                        db.get(ProviderConfig, plugin_id).last_error = None
                        backoff = db.get(ConfigEntry, f"provider.backoff.{plugin_id}")
                        if backoff:
                            db.delete(backoff)
                except ProviderError as exc:
                    with self.db.session() as db:
                        row = db.get(ProviderConfig, plugin_id)
                        row.last_error = f"{exc.code}: {str(exc)}"
                        delay = max(exc.retry_after, 5)
                        if exc.code in {"rate_limited", "unavailable"}:
                            key = f"provider.backoff.{plugin_id}"
                            backoff = db.get(ConfigEntry, key)
                            failures = min(7, (backoff.value["failures"] if backoff else 0) + 1)
                            if not backoff:
                                backoff = ConfigEntry(key=key, value={})
                                db.add(backoff)
                            backoff.value = {"failures": failures}
                            delay = max(delay, min(3600, 60 * 2 ** (failures - 1)))
                        row.retry_at = time.time() + delay
                    raise
                finally:
                    await provider.close()
                    state["cookies"] = [
                        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                        for c in client.cookies.jar
                    ]
                    with self.db.session() as db:
                        row = db.get(ProviderConfig, plugin_id)
                        # A simultaneous settings edit must not resurrect old cookies.
                        if (row.config, row.secrets) == original_config:
                            row.session_state = self.secrets.encrypt(state)

    @staticmethod
    def _tls_context(cls):
        if "legacy_tls" not in cls.manifest.capabilities:
            return True
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT:@SECLEVEL=1")
        return context
