"""Versioned local search engines. Each operation pins an immutable generation."""

import asyncio
from contextvars import ContextVar
from functools import wraps
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import httpx
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic import BaseModel, Field

from lazarr.sdk import SDK_VERSION

DEFAULT_SEARCH_REPOSITORY = "https://raw.githubusercontent.com/Drun555/lazarr-search-engine/main/catalog.json"
_generation = None
_pinned = ContextVar("search_engine_generation", default=None)
MODULES = ("matcher", "selection", "provider_utils", "associations")
MAX_PACKAGE = 2 * 1024 * 1024


def load_generation(package, version, digest):
    modules = {name: importlib.import_module(f"{package}.{name}") for name in MODULES}
    generation = SimpleNamespace(version=version, identity=f"{version}:{digest}", **modules)
    if generation.matcher.episode_numbers("Show.S01E02.mkv") != (1, {2}, False):
        raise ValueError("Search engine self-check failed")
    for module, name in (
        ("matcher", "Matcher"),
        ("selection", "reject_reason"),
        ("selection", "candidate_rank"),
        ("provider_utils", "search_titles"),
        ("associations", "related_files"),
    ):
        if not callable(getattr(modules[module], name, None)):
            raise ValueError(f"Search engine missing {module}.{name}")
    return generation


def bundled_generation():
    root = Path(__file__).parent / "_search_builtin"
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(root.glob("*.py")))).hexdigest()
    from lazarr._search_builtin import VERSION

    return load_generation("lazarr._search_builtin", VERSION, digest)


def current_engine():
    global _generation
    if _pinned.get() is not None:
        return _pinned.get()
    if _generation is None:
        _generation = bundled_generation()
    return _generation


def engine_call(module, name, *args, **kwargs):
    return getattr(getattr(current_engine(), module), name)(*args, **kwargs)


def pinned(fn):
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def asynchronous(*args, **kwargs):
            token = _pinned.set(current_engine())
            try:
                return await fn(*args, **kwargs)
            finally:
                _pinned.reset(token)

        return asynchronous

    @wraps(fn)
    def synchronous(*args, **kwargs):
        token = _pinned.set(current_engine())
        try:
            return fn(*args, **kwargs)
        finally:
            _pinned.reset(token)

    return synchronous


class EngineManifest(BaseModel):
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    api: int = 1
    sdk: str = ">=1.6,<2"
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SearchEngineManager:
    def __init__(self, root, transport=None):
        self.root = Path(root)
        self.transport = transport
        self.lock = asyncio.Lock()
        self.error = None
        self.entry = None
        self.automatic = True

    def bootstrap(self):
        global _generation
        self.entry = None
        self.error = None
        _generation = bundled_generation()
        pointer = self.root / "active.json"
        if pointer.exists():
            try:
                data = json.loads(pointer.read_text())
                self.automatic = data.get("automatic", True)
                if data.get("bundled"):
                    return
                entry = EngineManifest.model_validate(data)
                _generation = self.prepare(entry, self.package_path(entry).read_bytes())
                self.entry = entry
            except Exception as exc:
                self.error = f"Installed engine unavailable; using bundled version: {exc}"

    def package_path(self, entry):
        return self.root / f"{entry.sha256}.zip"

    def prepare(self, entry, content):
        if entry.api != 1 or SDK_VERSION not in SpecifierSet(entry.sdk):
            raise ValueError("Incompatible search engine API/SDK")
        if len(content) > MAX_PACKAGE or hashlib.sha256(content).hexdigest() != entry.sha256:
            raise ValueError("Search engine size/checksum mismatch")
        package = f"lazarr_search_{entry.sha256}"
        destination = self.root / entry.sha256
        expected = {f"{name}.py" for name in (*MODULES, "__init__")}
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if {info.filename for info in infos} != expected or len(infos) != len(expected):
                raise ValueError("Unexpected search engine package files")
            if sum(info.file_size for info in infos) > MAX_PACKAGE:
                raise ValueError("Expanded search engine too large")
            source = {info.filename: archive.read(info) for info in infos}
        for name, data in source.items():
            compile(data, name, "exec")
        from lazarr.plugins import atomic_write

        for name, data in source.items():
            atomic_write(destination / name, data)
        if package not in sys.modules:
            spec = importlib.util.spec_from_file_location(package, destination / "__init__.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[package] = module
            try:
                spec.loader.exec_module(module)
                if module.VERSION != entry.version or module.API_VERSION != entry.api:
                    raise ValueError("Search engine manifest mismatch")
                generation = load_generation(package, entry.version, entry.sha256)
            except Exception:
                for key in list(sys.modules):
                    if key == package or key.startswith(package + "."):
                        sys.modules.pop(key, None)
                raise
        else:
            generation = load_generation(package, entry.version, entry.sha256)
        return generation

    async def fetch(self, url):
        if not url.startswith("https://"):
            raise ValueError("Search engine repository must use HTTPS")
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_PACKAGE:
                        raise ValueError("Search engine download too large")
        return bytes(content)

    def activate(self, entry, content, automatic=True):
        global _generation
        from lazarr.plugins import atomic_write

        generation = self.prepare(entry, content)
        atomic_write(self.package_path(entry), content)
        pointer = self.root / "active.json"
        if not pointer.exists():
            atomic_write(self.root / "previous.json", b'{"bundled":true}')
        if pointer.exists() and (not self.entry or self.entry.sha256 != entry.sha256):
            atomic_write(self.root / "previous.json", pointer.read_bytes())
        atomic_write(pointer, json.dumps({**entry.model_dump(), "automatic": automatic}).encode())
        self.entry, self.automatic, self.error = entry, automatic, None
        _generation = generation

    async def update(self, url, automatic=False):
        async with self.lock:
            if not url and not automatic:
                raise ValueError("Укажите и сохраните адрес каталога движка в настройках")
            if not url or (automatic and not self.automatic):
                return self.status()
            try:
                entry = EngineManifest.model_validate_json(await self.fetch(url))
                if automatic and (
                    Version(entry.version) < Version(current_engine().version)
                    or (self.entry and Version(entry.version) == Version(current_engine().version))
                ):
                    return self.status()
                if automatic and self.entry and entry.sha256 == self.entry.sha256:
                    return self.status()
                self.activate(entry, await self.fetch(entry.url))
            except Exception as exc:
                self.error = str(exc)
                raise
        return self.status()

    async def rollback(self):
        global _generation
        async with self.lock:
            pointer = self.root / "previous.json"
            if not pointer.exists():
                raise ValueError("No previous search engine version")
            if json.loads(pointer.read_text()).get("bundled"):
                from lazarr.plugins import atomic_write

                generation = bundled_generation()
                active = self.root / "active.json"
                previous = active.read_bytes()
                atomic_write(active, b'{"bundled":true,"automatic":false}')
                atomic_write(pointer, previous)
                self.entry, self.automatic, self.error = None, False, None
                _generation = generation
                return self.status()
            entry = EngineManifest.model_validate_json(pointer.read_text())
            # Prevent the hourly updater from immediately undoing the rollback.
            self.activate(entry, self.package_path(entry).read_bytes(), automatic=False)
        return self.status()

    def status(self):
        return {
            "version": current_engine().version,
            "identity": current_engine().identity,
            "source": "repository" if self.entry else "bundled",
            "error": self.error,
            "automatic": self.automatic,
            "can_rollback": (self.root / "previous.json").exists(),
        }
