"""Search engine shipped with the application, with content identity for cached reports."""

from contextvars import ContextVar
from functools import wraps
import hashlib
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace


_generation = None
_pinned = ContextVar("search_engine_generation", default=None)
MODULES = ("matcher", "selection", "provider_utils", "associations")


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


def shipped_generation():
    root = Path(__file__).parent / "_search_builtin"
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(root.glob("*.py")))).hexdigest()
    from lazarr._search_builtin import VERSION

    return load_generation("lazarr._search_builtin", VERSION, digest)


def current_engine():
    global _generation
    if _pinned.get() is not None:
        return _pinned.get()
    if _generation is None:
        _generation = shipped_generation()
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


class SearchEngineManager:
    def bootstrap(self):
        current_engine()

    def status(self):
        return {
            "version": current_engine().version,
            "identity": current_engine().identity,
            "source": "application",
        }
