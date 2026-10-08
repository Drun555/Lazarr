import json
import pytest
from lazarr.models import ProviderConfig
from lazarr.plugins import PluginManager


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


def test_content_order_persists_and_validates_enabled_providers(core):
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
    restarted.set_content_order(["kinozal", "nyaa", "rutracker"])
    assert [p["id"] for p in restarted.describe() if p.get("kind") == "content"] == [
        "kinozal",
        "nyaa",
        "rutracker",
    ]
    restarted.configure("rutracker", {}, True)
    assert restarted.available("content") == ["nyaa", "rutracker"]


def test_shipped_providers_ignore_cached_code_and_preserve_settings(core, monkeypatch):
    config, db, manager, _ = core
    legacy = config.data_dir / "plugins"
    monkeypatch.setenv("LAZARR_PLUGIN_DIR", str(legacy))
    for name in ("tmdb", "external_provider", "search_engine"):
        folder = legacy / name
        folder.mkdir(parents=True)
        (folder / "active.json").write_text('{"version":"99.0.0"}')
        (folder / "plugin.py").write_text('raise RuntimeError("cached code executed")')
    manager.configure("tmdb", {"api_key": "saved-token"}, False)
    manager.configure("rutracker", {"username": "saved-user", "trawl_url": "http://localhost:8191"}, True)
    with db.session() as session:
        row = session.get(ProviderConfig, "rutracker")
        row.session_state = manager.secrets.encrypt({"authenticated": True})
    before = {path: path.read_bytes() for path in legacy.rglob("*") if path.is_file()}
    restarted = PluginManager(db, config, manager.secrets)
    restarted.bootstrap()
    assert set(restarted.classes) == {"tmdb", "nyaa", "rutracker", "kinozal"}
    assert all(cls.__module__.startswith("lazarr.providers.") for cls in restarted.classes.values())
    assert not restarted.errors
    assert {path: path.read_bytes() for path in legacy.rglob("*") if path.is_file()} == before
    with db.session() as session:
        tmdb = session.get(ProviderConfig, "tmdb")
        assert not tmdb.enabled
        assert manager.secrets.decrypt(tmdb.secrets)["api_key"] == "saved-token"
        tracker = session.get(ProviderConfig, "rutracker")
        assert tracker.enabled
        assert tracker.config["trawl_url"] == "http://localhost:8191"
        assert manager.secrets.decrypt(tracker.session_state) == {"authenticated": True}


def test_startup_does_not_create_plugin_cache(core):
    config, _, _, _ = core
    assert not (config.data_dir / "plugins").exists()
