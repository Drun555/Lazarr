import hashlib
import os
import time
import pytest
from lazarr.sdk import DownloadSource, DownloadPlan, FileBinding, TrackBinding, TorrentFile
from lazarr.torrent import LibtorrentEngine, desired_priorities, pieces_for

lt = pytest.importorskip("libtorrent")


def make_torrent(root, paths):
    storage = lt.file_storage()
    for path in paths:
        storage.add_file(path, (root / path).stat().st_size)
    creator = lt.create_torrent(storage, 16 * 1024, flags=lt.create_torrent.v1_only)
    creator.set_priv(True)
    lt.set_piece_hashes(creator, str(root))
    return lt.bencode(creator.generate())


def test_priority_order_and_boundary_pieces():
    files = [
        TorrentFile(index=0, path="E02.mkv", size=100, offset=0),
        TorrentFile(index=1, path="E01.mkv", size=100, offset=100),
        TorrentFile(index=2, path="E01.ru.mka", size=20, offset=200),
    ]
    plan = DownloadPlan(
        infohash="x",
        files=files,
        bindings=[
            FileBinding(subtask_id=2, video_index=0, video_path="E02.mkv", episode_order=2),
            FileBinding(
                subtask_id=1,
                video_index=1,
                video_path="E01.mkv",
                episode_order=1,
                tracks=[TrackBinding(kind="audio", language="ru", file_index=2, path="E01.ru.mka")],
            ),
        ],
    )
    priorities, current = desired_priorities(plan, [0, 0, 0])
    assert priorities == [0, 4, 7] and current.subtask_id == 1
    priorities, current = desired_priorities(plan, [0, 100, 0])
    assert priorities == [0, 4, 7] and current.subtask_id == 1
    priorities, current = desired_priorities(plan, [0, 100, 20])
    assert priorities[0] == 4 and current.subtask_id == 2
    assert pieces_for(files[1], 64) == {1, 2, 3}
    assert pieces_for(files[1], 64, 99, 1) == {3}


@pytest.mark.swarm
def test_local_swarm_selective_download_and_resume(tmp_path):
    source = tmp_path / "seed"
    source.mkdir()
    # Deliberately reverse torrent file order relative to episode order.
    paths = [
        "Show/Show.S01E02.mkv",
        "Show/Show.S01E01.mkv",
        "Show/Show.S01E01.ru.mka",
        "Show/unrequested.bin",
    ]
    for i, path in enumerate(paths):
        (source / path).parent.mkdir(parents=True, exist_ok=True)
        (source / path).write_bytes(os.urandom(2 * 1024 * 1024 if i < 2 else 128 * 1024))
    data = make_torrent(source, paths)
    seed = lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "allow_multiple_connections_per_ip": True,
        }
    )
    seed_params = lt.add_torrent_params()
    seed_params.ti = lt.torrent_info(lt.bdecode(data))
    seed_params.save_path = str(source)
    seed_params.flags &= ~lt.torrent_flags.auto_managed
    seed_params.flags &= ~lt.torrent_flags.paused
    seed_handle = seed.add_torrent(seed_params)
    engine = LibtorrentEngine(tmp_path / "state", "127.0.0.1:0", local_only=True)
    try:
        metadata = engine.inspect(DownloadSource(torrent=data))
        plan = DownloadPlan(
            infohash=metadata.infohash,
            files=metadata.files,
            bindings=[
                FileBinding(
                    subtask_id=1,
                    video_index=1,
                    video_path=paths[1],
                    episode_order=1,
                    tracks=[TrackBinding(kind="audio", language="ru", file_index=2, path=paths[2])],
                ),
                FileBinding(subtask_id=2, video_index=0, video_path=paths[0], episode_order=2),
            ],
        )
        engine.add(data, str(tmp_path / "download"), plan)
        deadline = time.monotonic() + 30
        while not seed_handle.status().is_seeding and time.monotonic() < deadline:
            time.sleep(0.05)
        assert seed_handle.status().is_seeding
        engine.handles[metadata.infohash].set_download_limit(512 * 1024)
        observed_first = False
        restarted = False
        engine.connect_peer(metadata.infohash, ("127.0.0.1", seed.listen_port()))
        while time.monotonic() < deadline:
            if restarted:
                # Simulate lost notifications during a busy restore. Priority
                # completion must also be recovered from the handle itself.
                engine.session.pop_alerts()
            stats = engine.snapshot(metadata.infohash)
            if not stats["bindings"]["1"]["complete"]:
                # Files are piece-aligned: there is no shared boundary excuse
                # for downloading E02 before E01 and its required audio.
                assert stats["files"][0] == 0, stats
                observed_first |= stats["files"][1] > 0
            if observed_first and not restarted:
                # A checkpoint may capture the temporary priority guard.
                engine.handles[metadata.infohash].set_flags(lt.torrent_flags.upload_mode)
                engine.close()
                engine = LibtorrentEngine(tmp_path / "state", "127.0.0.1:0", local_only=True)
                engine.add(data, str(tmp_path / "download"), plan)
                engine.handles[metadata.infohash].set_download_limit(512 * 1024)
                engine.connect_peer(metadata.infohash, ("127.0.0.1", seed.listen_port()))
                restarted = True
            if (
                stats["complete"]
                and not engine.handles[metadata.infohash].flags() & lt.torrent_flags.upload_mode
            ):
                break
            time.sleep(0.1)
        assert stats["complete"], stats
        assert restarted
        assert not engine.handles[metadata.infohash].flags() & lt.torrent_flags.upload_mode
        assert observed_first, "Must observe episode 1 downloading before episode 2"
        assert stats["bindings"]["1"]["buffer_ready"]
        for path in paths[:3]:
            assert (
                hashlib.sha256((source / path).read_bytes()).digest()
                == hashlib.sha256((tmp_path / "download" / path).read_bytes()).digest()
            )
        # Unselected bytes may share one boundary piece, but this file must not be selected.
        assert engine.handles[metadata.infohash].get_file_priorities()[3] == 0
        engine.pause(metadata.infohash)
        engine.close()
        assert (engine.root / f"{metadata.infohash}.resume").exists()
        restored = LibtorrentEngine(tmp_path / "state", "127.0.0.1:0", local_only=True)
        try:
            restored.add(data, str(tmp_path / "download"), plan, paused=True)
            limit = time.monotonic() + 5
            while time.monotonic() < limit:
                snapshot = restored.snapshot(metadata.infohash)
                if snapshot["complete"]:
                    break
                time.sleep(0.05)
            assert snapshot["complete"] and snapshot["paused"]
            assert snapshot["downloaded"] >= stats["downloaded"]
            restored.remove(metadata.infohash)
            assert not restored.contains(metadata.infohash)
            assert not (restored.root / f"{metadata.infohash}.resume").exists()
            assert (tmp_path / "download" / paths[0]).exists()
        finally:
            restored.close()
    finally:
        seed.pause()
        engine.session.pause()


def test_torrent_rejects_path_traversal(tmp_path):
    engine = LibtorrentEngine(tmp_path, "127.0.0.1:0", local_only=True)
    try:
        # SDK rejects traversal independently of libtorrent's own path sanitization.
        with pytest.raises(ValueError):
            TorrentFile(index=0, path="../escape.mkv", size=1)
        with pytest.raises(ValueError):
            TorrentFile(index=0, path="/escape.mkv", size=1)
    finally:
        engine.close()


@pytest.mark.parametrize("state", [lt.torrent_status.checking_resume_data, lt.torrent_status.checking_files])
def test_priorities_wait_for_restored_progress(state):
    from types import SimpleNamespace
    from unittest.mock import Mock

    engine = LibtorrentEngine.__new__(LibtorrentEngine)
    engine.lt = lt
    handle = Mock()
    handle.status.return_value = SimpleNamespace(state=state)
    engine.handles = {"restoring": handle}
    engine.plans = {"restoring": Mock()}
    engine._apply("restoring")
    # Before storage is ready libtorrent may accept priorities without posting
    # file_prio_alert. Do not start an operation that cannot release upload_mode.
    handle.prioritize_files.assert_not_called()
    handle.file_progress.assert_not_called()
