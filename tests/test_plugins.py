import hashlib
import json
import httpx
import pytest
from lazarr.models import ProviderConfig


def plugin_source(version):
    return f'''from lazarr.sdk import ContentProvider, ProviderManifest, SearchPage
class Plugin(ContentProvider):
    manifest = ProviderManifest(id="test_provider", name="Test", kind="content", version="{version}")
    async def healthcheck(self):
        return {{"version": "{version}"}}
    async def search(self, query, cursor=None):
        return SearchPage(items=[])
    async def inspect(self, candidate):
        return candidate
    async def resolve_download(self, candidate):
        raise NotImplementedError("Test fixture has no releases")
'''.encode()


def catalog(version, source, digest=None):
    return {
        "plugins": [
            {
                "id": "test_provider",
                "kind": "content",
                "version": version,
                "sdk": ">=1,<2",
                "url": "https://plugins.example/plugin.py",
                "sha256": digest or hashlib.sha256(source).hexdigest(),
            }
        ]
    }


async def test_updates_pin_active_calls_and_can_rollback_offline(core):
    _, db, manager, _ = core
    source = plugin_source("1.0.0")
    data = catalog("1.0.0", source)

    def transport(request):
        return (
            httpx.Response(200, json=data)
            if request.url.path.endswith("json")
            else httpx.Response(200, content=source)
        )

    manager.transport = httpx.MockTransport(transport)
    await manager.update("https://plugins.example/catalog.json", "test_provider")
    manager.configure("test_provider", {}, True)
    async with manager.open("test_provider") as previous:
        source = plugin_source("1.1.0")
        data = catalog("1.1.0", source)
        await manager.update("https://plugins.example/catalog.json", "test_provider")
        assert (await previous.healthcheck())["version"] == "1.0.0"
    async with manager.open("test_provider") as current:
        assert (await current.healthcheck())["version"] == "1.1.0"
    manager.transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    await manager.rollback("test_provider")
    manager.bootstrap()
    async with manager.open("test_provider") as current:
        assert (await current.healthcheck())["version"] == "1.0.0"


async def test_corrupt_or_incompatible_update_keeps_active_version(core):
    _, _, manager, _ = core
    source = plugin_source("1.0.0")
    data = catalog("1.0.0", source)
    manager.transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(200, json=data)
            if req.url.path.endswith("json")
            else httpx.Response(200, content=source)
        )
    )
    await manager.update("https://plugins.example/catalog.json", "test_provider")
    source = plugin_source("1.1.0")
    data = catalog("1.1.0", source, "0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        await manager.update("https://plugins.example/catalog.json", "test_provider")
    assert manager.classes["test_provider"].manifest.version == "1.0.0"
    data = catalog("1.1.0", source)
    data["plugins"][0]["sdk"] = ">=99"
    with pytest.raises(ValueError, match="SDK"):
        await manager.update("https://plugins.example/catalog.json", "test_provider")
    assert manager.classes["test_provider"].manifest.version == "1.0.0"


def test_provider_secrets_encrypted_and_not_returned(core):
    config, db, manager, _ = core
    manager.configure("tmdb", {"api_key": "private-token"}, True)
    with db.session() as session:
        row = session.get(ProviderConfig, "tmdb")
        assert "private-token" not in row.secrets
        assert "api_key" not in row.config
    result = manager.describe()
    assert "private-token" not in json.dumps(result)
    assert "api_key" in next(p for p in result if p["id"] == "tmdb")["configured_secrets"]
    assert config.data_dir.joinpath("secret.key").stat().st_mode & 0o777 == 0o600
    manager.configure("tmdb", {"api_key": ""}, True)
    with db.session() as session:
        assert (
            manager.secrets.decrypt(session.get(ProviderConfig, "tmdb").secrets)["api_key"] == "private-token"
        )


async def test_configuration_change_does_not_restore_old_session(core):
    _, db, manager, _ = core
    manager.configure("rutracker", {"username": "first"}, True)
    async with manager.open("rutracker") as provider:
        provider.ctx.state["authenticated"] = True
        manager.configure("rutracker", {"username": "second"}, True)
    with db.session() as session:
        assert manager.secrets.decrypt(session.get(ProviderConfig, "rutracker").session_state) == {}


async def test_incomplete_provider_contract_is_not_activated(core):
    _, _, manager, _ = core
    source = plugin_source("1.0.0").replace(b"async def search(", b"async def not_search(")
    data = catalog("1.0.0", source)
    manager.transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(200, json=data)
            if req.url.path.endswith("json")
            else httpx.Response(200, content=source)
        )
    )
    with pytest.raises(ValueError, match="contract"):
        await manager.update("https://plugins.example/catalog.json", "test_provider")
    assert "test_provider" not in manager.classes


async def test_automatic_updates_and_offline_cache(core):
    _, _, manager, _ = core
    source = plugin_source("1.0.0")
    data = catalog("1.0.0", source)

    def transport(req):
        return (
            httpx.Response(200, json=data)
            if req.url.path.endswith("json")
            else httpx.Response(200, content=source)
        )

    manager.transport = httpx.MockTransport(transport)
    await manager.auto_update("https://plugins.example/catalog.json")
    manager.configure("test_provider", {}, True)
    async with manager.open("test_provider") as old:
        source = plugin_source("1.2.0")
        data = catalog("1.2.0", source)
        await manager.auto_update("https://plugins.example/catalog.json")
        assert (await old.healthcheck())["version"] == "1.0.0"
    assert manager.classes["test_provider"].manifest.version == "1.2.0"
    manager.transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with pytest.raises(httpx.ConnectError):
        await manager.auto_update("https://plugins.example/catalog.json")
    async with manager.open("test_provider") as provider:
        assert (await provider.healthcheck())["version"] == "1.2.0"


def test_content_order_persists_and_validates_enabled_providers(core):
    from lazarr.plugins import PluginManager

    config, db, manager, _ = core
    manager.configure("nyaa", {}, True)
    manager.configure("rutracker", {}, True)
    manager.set_content_order(["rutracker", "nyaa"])
    restarted = PluginManager(db, config, manager.secrets)
    restarted.bootstrap()
    assert restarted.available("content") == ["rutracker", "nyaa"]
    assert [p["id"] for p in restarted.search_status()] == ["rutracker", "nyaa", "kinozal"]
    assert [p["id"] for p in restarted.describe() if p.get("kind") == "content"] == [
        "rutracker",
        "nyaa",
        "kinozal",
    ]
    for ids in [["nyaa"], ["nyaa", "nyaa"], ["tmdb", "nyaa"], ["unknown", "nyaa"]]:
        with pytest.raises(ValueError):
            restarted.set_content_order(ids)
    restarted.configure("rutracker", {}, False)
    assert restarted.available("content") == ["nyaa"]
    restarted.set_content_order(["nyaa"])
    restarted.configure("rutracker", {}, True)
    assert restarted.available("content") == ["nyaa", "rutracker"]
