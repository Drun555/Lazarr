import base64
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from lazarr.models import CandidateDecision, Release
from lazarr.sdk import DownloadSource
from lazarr.services import CreateTask
from test_worker import worker_setup as worker_setup


HASH = "abcdef0123456789" * 2 + "abcdef01"
URI = f"magnet:?xt=urn:btih:{HASH}&dn=Example.Show.S01E01.1080p&tr=udp%3A%2F%2Ftracker.example%3A80"


@pytest.mark.parametrize("topic", [HASH, HASH.upper(), base64.b32encode(bytes.fromhex(HASH)).decode()])
def test_magnet_hash_encodings_share_identity(core, topic):
    item = core[2].manual_candidate(f"magnet:?xt=urn:btih:{topic}&dn=My+Show")
    assert item.provider == "magnet" and item.id == HASH
    assert item.title == "My Show"
    assert item.magnet == item.url


def test_v2_and_hybrid_magnets(core):
    v2 = "1220" + "a" * 64
    manager = core[2]
    assert manager.manual_candidate(f"magnet:?xt=urn:btmh:{v2}").id == v2
    hybrid = manager.manual_candidate(f"magnet:?xt=urn:btmh:{v2}&xt=urn:btih:{HASH}")
    assert hybrid.id == HASH
    assert manager.manual_candidate(f"MAGNET:?xt=urn:btih:{HASH}").magnet.startswith("magnet:?")


@pytest.mark.parametrize(
    "uri",
    [
        "magnet:?dn=MissingHash",
        "magnet:?xt=urn:btih:bad",
        "magnet:?xt=urn:btih:" + "0" * 32,
        "magnet:?xt=urn:btmh:1220bad",
        "magnet://host/?xt=urn:btih:" + HASH,
        f"magnet:?xt=urn:btih:{HASH}&xt=urn:btih:" + "a" * 40,
    ],
)
def test_invalid_magnet_is_rejected(core, uri):
    with pytest.raises(ValueError):
        core[2].manual_candidate(uri)


@pytest.fixture
def manual_setup(core, media, season, worker_setup, monkeypatch):
    worker, engine, _ = worker_setup
    task_id = core[3].create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    sources = []
    original = engine.inspect

    def inspect(source):
        sources.append(source)
        if source.magnet:
            assert source.magnet.lower() == URI.lower()
            source = DownloadSource(torrent=b'["Show.S01E01.1080p.mkv"]')
        return original(source)

    monkeypatch.setattr(engine, "inspect", inspect)
    return worker, engine, task_id, sources


@pytest.mark.parametrize("scope", ["episode", "task", "season", "mapping"])
async def test_custom_magnet_can_be_saved_without_provider_and_selected(core, manual_setup, scope):
    worker, engine, task_id, sources = manual_setup
    if scope == "episode":
        identity = await worker.add_manual_candidate(1, URI)
    else:
        identity = await worker.add_manual_task_candidate(
            task_id,
            URI,
            season_number=1 if scope in {"season", "mapping"} else None,
            return_release=scope == "mapping",
        )
    assert identity and sources[0].magnet == URI and not engine.plans
    with core[1].session() as db:
        release = db.scalar(select(Release))
        assert release.provider == "magnet" and release.external_id == HASH
        assert release.data["magnet"] == URI
        assert release.data["size"] == 100
        assert release.files[0]["path"] == "Show.S01E01.1080p.mkv"
    if scope == "episode":
        again = await worker.add_manual_candidate(1, URI.replace(HASH, HASH.upper()))
        assert again == identity
        await worker.choose(identity, 1, video_index=0)
        assert engine.plans


async def test_magnet_metadata_failure_does_not_save_candidate(core, manual_setup, monkeypatch):
    worker, engine, _, _ = manual_setup

    def fail(source):
        raise TimeoutError("metadata unavailable")

    monkeypatch.setattr(engine, "inspect", fail)
    with pytest.raises(TimeoutError, match="metadata unavailable"):
        await worker.add_manual_candidate(1, URI)
    with core[1].session() as db:
        assert db.scalar(select(func.count()).select_from(Release)) == 0
        assert db.scalar(select(func.count()).select_from(CandidateDecision)) == 0
    assert not engine.plans


def test_magnet_can_be_added_through_api(core, manual_setup, monkeypatch):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from test_api import login

    worker, engine, _, _ = manual_setup
    with TestClient(create_app(core[0])) as client:
        login(client)
        ctx = client.app.state.ctx
        monkeypatch.setattr(ctx, "worker", worker)
        if ctx.engine is None:
            monkeypatch.setattr(ctx, "engine", SimpleNamespace(close=lambda: None))
        response = client.post("/api/v1/subtasks/1/candidates/manual", json={"url": URI})
        assert response.status_code == 200, response.text
        choices = client.get("/api/v1/subtasks/1/candidates").json()
        assert choices[0]["candidate"]["url"] == URI
        assert choices[0]["candidate"]["provider"] == "magnet"
        bad = client.post("/api/v1/subtasks/1/candidates/manual", json={"url": "magnet:?dn=nohash"})
        assert bad.status_code == 422, bad.text
    assert not engine.plans
