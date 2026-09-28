import asyncio
import hashlib
import io
import json
from pathlib import Path
import zipfile

import httpx
import pytest

from lazarr import search_runtime as runtime
from lazarr.matcher import episode_numbers


@pytest.fixture(autouse=True)
def restore_generation(monkeypatch):
    monkeypatch.setattr(runtime, "_generation", None)


def package(version="1.0.1", extra=None, broken=False):
    output = io.BytesIO()
    root = Path(runtime.__file__).parent / "_search_builtin"
    with zipfile.ZipFile(output, "w") as archive:
        for path in root.glob("*.py"):
            content = path.read_text()
            if path.name == "__init__.py":
                content = f'VERSION = "{version}"\nAPI_VERSION = 1\n'
            if broken and path.name == "matcher.py":
                content = 'raise RuntimeError("bad engine")'
            archive.writestr(path.name, content)
        if extra:
            archive.writestr(extra, "bad")
    content = output.getvalue()
    entry = runtime.EngineManifest(
        version=version, url="https://example.com/engine.zip", sha256=hashlib.sha256(content).hexdigest()
    )
    return entry, content


@pytest.mark.asyncio
async def test_update_pin_restart_rollback_and_offline(tmp_path):
    entry, content = package()
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            200, content=entry.model_dump_json().encode() if request.url.path == "/catalog" else content
        )

    manager = runtime.SearchEngineManager(tmp_path, httpx.MockTransport(handler))
    manager.bootstrap()
    entered, resume = asyncio.Event(), asyncio.Event()

    @runtime.pinned
    async def operation():
        before = runtime.current_engine().identity
        entered.set()
        await resume.wait()
        assert runtime.current_engine().identity == before
        assert episode_numbers("Show.S01E02.mkv") == (1, {2}, False)

    task = asyncio.create_task(operation())
    await entered.wait()
    await manager.update("https://example.com/catalog")
    assert manager.status()["version"] == "1.0.1"
    resume.set()
    await task
    restarted = runtime.SearchEngineManager(tmp_path)
    restarted.bootstrap()
    assert restarted.status()["version"] == "1.0.1"
    await restarted.rollback()
    assert restarted.status()["source"] == "bundled"
    assert restarted.automatic is False
    restarted.bootstrap()
    assert restarted.automatic is False
    await restarted.update("https://unreachable.invalid/catalog", automatic=True)
    await manager.update("https://example.com/catalog")
    assert manager.automatic is True
    manager.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await manager.update("https://example.com/catalog")
    assert manager.status()["version"] == "1.0.1"


@pytest.mark.parametrize("failure", ["checksum", "api", "sdk", "path", "load"])
def test_rejected_update_keeps_active_pointer(tmp_path, failure):
    manager = runtime.SearchEngineManager(tmp_path)
    manager.bootstrap()
    entry, content = package()
    manager.activate(entry, content)
    before = (tmp_path / "active.json").read_bytes()
    entry, content = package(
        "1.0.2", extra="../escape.py" if failure == "path" else None, broken=failure == "load"
    )
    if failure == "checksum":
        content += b"changed"
    if failure == "api":
        entry.api = 999
    if failure == "sdk":
        entry.sdk = ">=99"
    with pytest.raises((ValueError, RuntimeError)):
        manager.activate(entry, content)
    assert (tmp_path / "active.json").read_bytes() == before
    assert runtime.current_engine().version == "1.0.1"
    assert not (tmp_path.parent / "escape.py").exists()


@pytest.mark.asyncio
async def test_rollback_external_version_pauses_updater(tmp_path):
    manager = runtime.SearchEngineManager(tmp_path)
    manager.bootstrap()
    manager.activate(*package("1.0.1"))
    manager.activate(*package("1.0.2"))
    await manager.rollback()
    assert manager.status()["version"] == "1.0.1"
    assert not manager.automatic
    manager.bootstrap()
    assert manager.status()["version"] == "1.0.1"
    assert json.loads((tmp_path / "active.json").read_text())["automatic"] is False
