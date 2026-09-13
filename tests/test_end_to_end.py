"""Real Worker -> libtorrent loopback -> ffprobe verification, using generated media only."""

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
import pytest
from sqlalchemy import select
from lazarr.config import Requirements
from lazarr.models import ProviderConfig, Subtask, SubtaskAsset
from lazarr.sdk import ContentProvider, ProviderManifest, SearchPage, DownloadSource
from lazarr.services import CreateTask
from lazarr.torrent import LibtorrentEngine
from lazarr.worker import Worker
from conftest import candidate, audio_claim
from test_torrent import make_torrent

lt = pytest.importorskip("libtorrent")


@pytest.mark.swarm
async def test_real_worker_swarm_and_ffprobe(core, media, season, tmp_path):
    config, db, plugins, service = core
    fallback = Path(__file__).parents[1] / ".ffmpeg/bin"
    ffmpeg = shutil.which("ffmpeg") or str(fallback / "ffmpeg")
    ffprobe = shutil.which("ffprobe") or str(fallback / "ffprobe")
    if not Path(ffmpeg).exists() or not Path(ffprobe).exists():
        pytest.skip("ffmpeg/ffprobe required")
    config.ffprobe = ffprobe
    seed_root = tmp_path / "seed"
    (seed_root / "Show").mkdir(parents=True)
    paths = [f"Show/Example.Show.S01E0{i}.1080p.mkv" for i in [2, 1]]
    for path in paths:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1920x1080:r=5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=8000",
                "-t",
                "1",
                "-c:v",
                "mpeg4",
                "-q:v",
                "15",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=rus",
                str(seed_root / path),
            ],
            check=True,
            timeout=30,
        )
    torrent = make_torrent(seed_root, paths)

    class LocalProvider(ContentProvider):
        manifest = ProviderManifest(id="local_swarm", name="Local test", kind="content", version="1.0.0")

        async def search(self, query, cursor=None):
            return SearchPage(items=[candidate(provider="local_swarm")])

        async def inspect(self, item):
            return item.model_copy(update={"evidence": [audio_claim(path) for path in paths]})

        async def resolve_download(self, item):
            return DownloadSource(torrent=torrent)

    plugins.classes["local_swarm"] = LocalProvider
    with db.session() as session:
        session.add(ProviderConfig(id="local_swarm", enabled=True))
    settings = service.settings()
    settings.series_path = str(tmp_path / "downloads")
    settings.seed_ratio = None
    service.set_settings(settings, 1)
    service.create_from_metadata(
        CreateTask(
            media_id="42",
            kind="tv",
            season=1,
            episodes=[1, 2],
            requirements=Requirements(subtitle_languages=["ru"]),
        ),
        media,
        season,
        1,
    )
    engine = LibtorrentEngine(config.data_dir, "127.0.0.1:0", local_only=True)
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
    params.save_path = str(seed_root)
    params.flags &= ~lt.torrent_flags.auto_managed
    params.flags &= ~lt.torrent_flags.paused
    handle = seed.add_torrent(params)
    worker = Worker(db, plugins, service, engine, config)
    try:
        await worker.run_due()
        assert len(engine.handles) == 1
        infohash = next(iter(engine.handles))
        deadline = time.monotonic() + 25
        while not handle.status().is_seeding and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        engine.connect_peer(infohash, ("127.0.0.1", seed.listen_port()))
        while time.monotonic() < deadline:
            await worker.poll()
            with db.session() as session:
                parts = list(session.scalars(select(Subtask)))
                if all(part.status == "done" for part in parts):
                    break
            await asyncio.sleep(0.1)
        assert [part.status for part in parts] == ["done", "done"]
        assert all(part.missing_subtitle_languages == ["ru"] for part in parts)
        with db.session() as session:
            links = list(session.scalars(select(SubtaskAsset)))
            assert all(link.current and link.verification["complete"] for link in links)
            assert all(c["result"] == "MATCH" for link in links for c in link.verification["criteria"])
    finally:
        await asyncio.to_thread(engine.close)
        seed.pause()
