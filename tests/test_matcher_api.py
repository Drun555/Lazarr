"""Episode labels exposed by the file picker API."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from lazarr.app import create_app
from lazarr.models import CandidateDecision, Release
from lazarr.sdk import TorrentFile
from lazarr.services import CreateTask
from test_api import login


def test_candidate_files_order_labelled_episodes(core, media, season):
    config, db, _, service = core
    service.create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1]), media, season, 1
    )
    with db.session() as session:
        release = Release(provider="rutracker", external_id="3339552", revision="labelled", data={})
        session.add(release)
        session.flush()
        decision = CandidateDecision(subtask_id=1, release_id=release.id, report={})
        session.add(decision)
        session.flush()
        identity = decision.id
    torrent = config.data_dir / "torrents" / "labelled.torrent"
    torrent.parent.mkdir(exist_ok=True)
    torrent.write_bytes(b"test metadata")
    paths = [
        "[ANE] Ore no Imouto - Ep02 [BDRip 1080p x264 FLAC].mkv",
        "[ANE] Ore no Imouto - Ep01 [BDRip 1080p x264 FLAC].mkv",
        "[ANE] Ore no Imouto - Ep12 - True Route [BDRip 1080p x264 FLAC].mkv",
    ]

    class Engine:
        def inspect(self, source):
            return SimpleNamespace(
                files=[TorrentFile(index=i, path=p, size=1000, offset=i * 1000) for i, p in enumerate(paths)]
            )

        def close(self):
            pass

    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        original_engine = ctx.engine
        ctx.engine = Engine()
        try:
            response = client.get(f"/api/v1/candidates/{identity}/files")
            assert response.status_code == 200, response.text
            assert [f["episode_order"] for f in response.json()] == [[0, 2], [0, 1], [0, 12]]
        finally:
            ctx.engine = original_engine
