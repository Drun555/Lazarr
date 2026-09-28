import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from lazarr.app import create_app
from lazarr.models import Release
from lazarr.sdk import Candidate
from lazarr.services import CreateTask
from test_api import login
from test_season_mapping import seed_release, files


@pytest.mark.parametrize("live", [True, False])
def test_report_contains_task_and_files_without_logs_or_credentials(core, media, season, live):
    config, db, _, service = core
    service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    identity = seed_release(config, db)
    candidate = Candidate(
        provider="nyaa",
        id="test",
        title="Example Show",
        url="https://user:password@example.org/topic?t=12&token=secret",
        description="cached description token=secret",
        download_url="https://private/secret",
        magnet="magnet:?xt=private",
    )
    with db.session() as session:
        session.get(Release, identity).data = candidate.model_dump(mode="json")
    with TestClient(create_app(config)) as client:
        login(client)
        ctx = client.app.state.ctx
        original = ctx.engine
        ctx.engine = SimpleNamespace(
            inspect=lambda _: SimpleNamespace(files=files("Pilot.mkv", "Pilot.ru.srt", "Finale.mkv"))
        )
        inspector = AsyncMock(
            return_value=candidate.model_copy(update={"description": "live description token=secret"})
        )
        if not live:
            inspector.side_effect = RuntimeError("secret internal log")

        @asynccontextmanager
        async def provider(*args, **kwargs):
            yield SimpleNamespace(inspect=inspector)

        ctx.plugins.open = provider
        try:
            path = f"/api/v1/tasks/1/seasons/1/mapping/releases/{identity}/report"
            response = client.post(path, json={})
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["release"]["description_source"] == ("live" if live else "cache")
            text = base64.b64decode(report["release"]["description_base64"]).decode()
            assert text == ("live" if live else "cached") + " description token=[redacted]"
            assert report["release"]["url"] == "https://example.org/topic?t=12"
            assert report["task"]["media"]["title"] == media.title
            assert len(report["task"]["requests"]) == 3
            assert report["task"]["requests"][0]["requirements"]["audio_languages"] == ["ru"]
            assert len(report["release"]["files"]) == 3
            assert "evaluation" in report
            assert not any(
                value in response.text for value in ["secret", "magnet:", "download_url", "logs", "password"]
            )
            inspector.assert_awaited_once()
            for scope, count in [("episode", 1), ("season", 3)]:
                response = client.post(f"/api/v1/candidates/1/report?scope={scope}", json={})
                assert response.status_code == 200, response.text
                assert len(response.json()["task"]["requests"]) == count
                assert response.json()["scope"] == scope
            assert client.post("/api/v1/candidates/999/report", json={}).status_code == 404
            assert (
                client.post("/api/v1/tasks/1/seasons/1/mapping/releases/999/report", json={}).status_code
                == 404
            )
            assert inspector.await_count == 3
        finally:
            ctx.engine = original


def test_search_flow_report_includes_task_without_history(core, media, season):
    config, _, _, service = core
    identity = service.create_from_metadata(CreateTask(media_id="42", kind="tv", season=1), media, season, 1)
    with TestClient(create_app(config)) as client:
        login(client)
        progress = client.app.state.ctx.worker.progress
        progress.begin()
        progress.prepare_tasks([[identity]])
        progress.start_group([identity])
        progress.record("search", "PRIVATE LOG TEXT")
        response = client.post(f"/api/v1/tasks/{identity}/search/report", json={})
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["issue_type"] == "search_flow"
        assert report["task"]["media"]["title"] == media.title
        assert len(report["task"]["requests"]) == 3
        assert report["search"]["stage"] == "search"
        assert "PRIVATE LOG TEXT" not in response.text
        assert "history" not in response.text
        assert client.post("/api/v1/tasks/999/search/report", json={}).status_code == 404
