import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, Field
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
    SubtitleProvider,
)


class CatalogEntry(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    kind: str
    version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,64}$")
    sdk: str = ">=1,<2"
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


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
        self.root = config.plugin_dir
        self.classes: dict[str, type[Provider]] = {}
        self.entries: dict[str, dict] = {}
        self.errors: dict[str, str] = {}
        self.transport = transport
        self.lock = asyncio.Lock()
        self.session_locks: dict[str, asyncio.Lock] = {}
        self.request_pacers = {}
        self.request_interval = 2.0

    def bootstrap(self):
        bundled = Path(__file__).parent / "bundled"
        catalog = json.loads((bundled / "catalog.json").read_text())
        for raw in catalog["plugins"]:
            entry = CatalogEntry.model_validate(raw)
            pointer = self.root / entry.id / "active.json"
            if not pointer.exists():
                content = (bundled / f"{entry.id}.py").read_bytes()
                self._activate(entry, content)
        for pointer in self.root.glob("*/active.json"):
            try:
                entry = CatalogEntry.model_validate_json(pointer.read_text())
                self._load_active(entry)
            except Exception:
                self.errors[pointer.parent.name] = "Cannot load active plugin; update or roll back"
        with self.db.session() as db:
            for plugin_id in self.classes:
                if not db.get(ProviderConfig, plugin_id):
                    db.add(ProviderConfig(id=plugin_id, enabled=plugin_id == "tmdb"))

    def _path(self, entry):
        return self.root / entry.id / f"{entry.version}-{entry.sha256[:16]}.py"

    def _validate(self, entry, content):
        if hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ValueError("Plugin checksum mismatch")
        if SDK_VERSION not in SpecifierSet(entry.sdk):
            raise ValueError("Incompatible plugin SDK")
        if len(content) > 1024 * 1024:
            raise ValueError("Plugin exceeds 1 MiB")
        compile(content, entry.id, "exec")

    def _load(self, entry, path):
        spec = importlib.util.spec_from_file_location(f"lazarr_plugin_{entry.id}_{entry.sha256}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = module.Plugin
        if not issubclass(cls, Provider):
            raise ValueError("Plugin must implement Lazarr Provider")
        manifest = cls.manifest
        if (manifest.id, manifest.version, manifest.kind) != (entry.id, entry.version, entry.kind):
            raise ValueError("Plugin identity does not match catalog")
        if SDK_VERSION not in SpecifierSet(manifest.sdk):
            raise ValueError("Incompatible plugin SDK")
        expected = {
            "content": ContentProvider,
            "metadata": MetadataProvider,
            "calendar": ReleaseCalendarProvider,
            "subtitle": SubtitleProvider,
        }[manifest.kind]
        if not issubclass(cls, expected) or inspect.isabstract(cls):
            raise ValueError("Plugin does not implement its provider contract")
        return cls

    def _load_active(self, entry):
        path = self._path(entry)
        self._validate(entry, path.read_bytes())
        cls = self._load(entry, path)
        self.classes[entry.id], self.entries[entry.id] = cls, entry.model_dump()
        self.errors.pop(entry.id, None)

    def _activate(self, entry, content):
        self._validate(entry, content)
        path = self._path(entry)
        atomic_write(path, content)
        cls = self._load(entry, path)
        pointer = path.parent / "active.json"
        if pointer.exists() and pointer.read_text() != entry.model_dump_json():
            atomic_write(path.parent / "previous.json", pointer.read_bytes())
        atomic_write(pointer, entry.model_dump_json().encode())
        self.classes[entry.id], self.entries[entry.id] = cls, entry.model_dump()
        self.errors.pop(entry.id, None)

    def use_bundled(self, plugin_id):
        bundled = Path(__file__).parent / "bundled"
        catalog = json.loads((bundled / "catalog.json").read_text())
        raw = next((v for v in catalog["plugins"] if v["id"] == plugin_id), None)
        if raw is None:
            raise ValueError("Встроенная версия провайдера отсутствует")
        entry = CatalogEntry.model_validate(raw)
        self._activate(entry, (bundled / f"{entry.id}.py").read_bytes())
        return entry

    async def catalog(self, url):
        if not url.startswith("https://"):
            raise ValueError("HTTPS repository URL is required")
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.get(url)
            response.raise_for_status()
            if len(response.content) > 1024 * 1024:
                raise ValueError("Catalog too large")
            result = [CatalogEntry.model_validate(v) for v in response.json()["plugins"]]
            if len({entry.id for entry in result}) != len(result):
                raise ValueError("Duplicate plugin IDs")
            return result

    async def update(self, url, plugin_id):
        async with self.lock:
            entries = await self.catalog(url)
            entry = next((v for v in entries if v.id == plugin_id), None)
            if entry is None or not entry.url.startswith("https://"):
                raise ValueError("Plugin missing or URL is not HTTPS")
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                async with client.stream("GET", entry.url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            raise ValueError("Plugin too large")
            self._activate(entry, bytes(content))
            with self.db.session() as db:
                if not db.get(ProviderConfig, plugin_id):
                    db.add(ProviderConfig(id=plugin_id))
            return entry

    async def auto_update(self, url):
        bundled = Path(__file__).parent / "bundled"
        for raw in json.loads((bundled / "catalog.json").read_text())["plugins"]:
            current = self.entries.get(raw["id"])
            if current is None or Version(raw["version"]) > Version(current["version"]):
                self.use_bundled(raw["id"])
        if not url:
            return
        # A repository failure leaves all locally cached providers usable.
        entries = await self.catalog(url)
        for entry in entries:
            current = self.entries.get(entry.id)
            if current is None or Version(entry.version) > Version(current["version"]):
                try:
                    await self.update(url, entry.id)
                except (ValueError, httpx.HTTPError):
                    with self.db.session() as db:
                        row = db.get(ProviderConfig, entry.id)
                        if row:
                            row.last_error = "Не удалось обновить плагин; используется локальная версия"

    async def rollback(self, plugin_id):
        async with self.lock:
            # Only known IDs can address files under the plugin directory.
            if plugin_id not in self.entries and plugin_id not in self.errors:
                raise ValueError("Unknown plugin")
            previous = self.root / plugin_id / "previous.json"
            if not previous.exists():
                raise ValueError("No previous version")
            entry = CatalogEntry.model_validate_json(previous.read_text())
            self._activate(entry, self._path(entry).read_bytes())
            return entry

    def content_order(self):
        with self.db.session() as db:
            row = db.get(ConfigEntry, "providers.content_order")
            saved = row.value["ids"] if row else []
        ids = [key for key, cls in self.classes.items() if cls.manifest.kind == "content"]
        return [key for key in saved if key in ids] + [key for key in ids if key not in saved]

    def set_content_order(self, ids):
        enabled = self.available("content")
        if len(ids) != len(set(ids)) or set(ids) != set(enabled):
            raise ValueError("Укажите все включённые контент-провайдеры ровно один раз; обновите настройки")
        order = ids + [key for key in self.content_order() if key not in ids]
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
            pacer = (
                self.request_pacers.setdefault(plugin_id, RequestPacer(self.request_interval))
                if cls.manifest.kind in {"content", "subtitle"}
                else None
            )
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
