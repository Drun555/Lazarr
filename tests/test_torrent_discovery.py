import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lazarr.torrent import LibtorrentEngine
from lazarr.sdk import DownloadSource, DownloadPlan, FileBinding
from test_torrent import make_torrent

lt = pytest.importorskip("libtorrent")


def saved_state():
    return {
        b"dht state": {
            b"node-id": [b"x" * 20 + b"\x7f\x00\x00\x01"],
            b"nodes": [b"\x7f\x00\x00\x01\xc0\x01"],
        },
        b"settings": {b"listen_interfaces": b"0.0.0.0:1234", b"download_rate_limit": 123},
    }


def test_dht_checkpoint_and_restart_keep_nodes_but_not_old_settings(tmp_path, monkeypatch):
    session = Mock()
    session.pop_alerts.return_value = []
    state = saved_state()
    state.pop(b"settings")
    session.save_state.return_value = state
    factory = Mock(return_value=session)
    monkeypatch.setattr(lt, "session", factory)
    engine = LibtorrentEngine(tmp_path, "127.0.0.1:0")
    engine.checkpoint()
    session.save_state.assert_called_once_with(lt.save_state_flags_t.save_dht_state)
    path = engine.root / "session.state"
    assert lt.bdecode(path.read_bytes()) == state
    # Even if a previous version saved settings too, they must not override configuration.
    path.write_bytes(lt.bencode(saved_state()))
    LibtorrentEngine(tmp_path, "127.0.0.1:5555")
    params = factory.call_args.args[0]
    restored = lt.write_session_params(params, lt.save_state_flags_t.save_dht_state)
    assert restored[b"dht state"] == state[b"dht state"]
    assert params.settings["listen_interfaces"] == "127.0.0.1:5555"
    assert "download_rate_limit" not in params.settings
    assert "dht.transmissionbt.com:6881" in params.settings["dht_bootstrap_nodes"]
    assert "router.bittorrent.com:6881" in params.settings["dht_bootstrap_nodes"]
    # A failed/empty bootstrap must preserve the last known table on disk.
    session.save_state.return_value = {b"dht state": {b"node-id": []}}
    engine.checkpoint()
    assert lt.bdecode(path.read_bytes()) == saved_state()


def test_corrupt_dht_state_falls_back_and_local_only_does_not_restore(tmp_path, monkeypatch, caplog):
    path = tmp_path / "torrent_state" / "session.state"
    path.parent.mkdir()
    path.write_bytes(b"broken resume")
    session = Mock()
    session.pop_alerts.return_value = []
    factory = Mock(return_value=session)
    monkeypatch.setattr(lt, "session", factory)
    LibtorrentEngine(tmp_path, "127.0.0.1:0")
    assert "Cannot restore DHT state" in caplog.text
    assert factory.call_args.args[0].settings["enable_dht"]
    path.write_bytes(lt.bencode(saved_state()))
    engine = LibtorrentEngine(tmp_path, "127.0.0.1:0", local_only=True)
    params = factory.call_args.args[0]
    assert not params.settings["enable_dht"]
    assert params.settings["dht_bootstrap_nodes"] == ""
    routing = lt.write_session_params(params, lt.save_state_flags_t.save_dht_state)[b"dht state"]
    assert not routing.get(b"nodes") and not routing.get(b"node-id")
    engine.checkpoint()
    session.save_state.assert_not_called()
    assert lt.bdecode(path.read_bytes()) == saved_state()


def test_dht_save_failure_does_not_prevent_torrent_checkpoint(tmp_path, monkeypatch, caplog):
    session = Mock()
    session.pop_alerts.return_value = []
    session.save_state.side_effect = OSError("disk failure")
    monkeypatch.setattr(lt, "session", Mock(return_value=session))
    engine = LibtorrentEngine(tmp_path, "127.0.0.1:0")
    handle = Mock()
    engine.handles["test"] = handle
    engine.checkpoint()
    handle.save_resume_data.assert_called_once()
    assert "Cannot save DHT state" in caplog.text


def test_discovery_reports_tracker_failure_without_credentials_or_peer_addresses():
    engine = LibtorrentEngine.__new__(LibtorrentEngine)
    handle = Mock()
    handle.trackers.return_value = [
        {
            "url": "http://user:password@tracker.example/secret/ann?pk=private-key",
            "verified": False,
            "fails": 2,
            "message": "denied private-key from 192.0.2.1",
            "last_error": {"value": 403, "category": "http"},
        }
    ]
    status = SimpleNamespace(
        list_peers=4, list_seeds=3, connect_candidates=1, num_complete=0xFFFFFF, num_incomplete=-1
    )
    result = engine._discovery(handle, status)
    assert result == {
        "known_peers": 4,
        "known_seeds": 3,
        "connection_candidates": 1,
        "tracker_seeds": None,
        "tracker_leechers": None,
        "trackers": [
            {
                "host": "tracker.example",
                "verified": False,
                "failures": 2,
                "error_code": 403,
                "error_category": "http",
            }
        ],
    }
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in ["private-key", "password", "secret/ann", "192.0.2.1"])
    status.num_complete, status.num_incomplete = 19, 5
    assert engine._discovery(handle, status)["tracker_seeds"] == 19


@pytest.mark.swarm
def test_resume_reconnects_saved_peer_without_tracker_or_manual_connection(tmp_path):
    source = tmp_path / "seed"
    source.mkdir()
    content = b"resume peer verification" * 100000
    (source / "video.mkv").write_bytes(content)
    torrent = make_torrent(source, ["video.mkv"])
    seed = lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
        }
    )
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(lt.bdecode(torrent))
    params.save_path = str(source)
    params.flags &= ~(lt.torrent_flags.paused | lt.torrent_flags.auto_managed)
    seed_handle = seed.add_torrent(params)
    engine = LibtorrentEngine(tmp_path / "state", "127.0.0.1:0", local_only=True)
    try:
        deadline = time.monotonic() + 15
        while not seed_handle.status().is_seeding and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seed_handle.status().is_seeding
        metadata = engine.inspect(DownloadSource(torrent=torrent))
        plan = DownloadPlan(
            infohash=metadata.infohash,
            files=metadata.files,
            bindings=[FileBinding(subtask_id=1, video_index=0, video_path="video.mkv", episode_order=1)],
        )
        saved = lt.add_torrent_params()
        saved.ti = params.ti
        saved.save_path = str(tmp_path / "download")
        saved.peers = [("127.0.0.1", seed.listen_port())]
        (engine.root / f"{metadata.infohash}.resume").write_bytes(lt.write_resume_data_buf(saved))
        engine.add(torrent, saved.save_path, plan)
        while time.monotonic() < deadline:
            stats = engine.snapshot(metadata.infohash)
            if stats["complete"]:
                break
            time.sleep(0.05)
        assert stats["complete"], stats
        assert (tmp_path / "download" / "video.mkv").read_bytes() == content
    finally:
        engine.close()
        seed.pause()
