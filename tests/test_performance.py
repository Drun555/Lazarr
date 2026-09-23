import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from lazarr.config import RuntimeConfig
from lazarr.performance import PerformanceMiddleware


@pytest.mark.parametrize("mode", ["standard", "performance"])
def test_application_wiring(tmp_path, mode):
    from lazarr.app import create_app

    app = create_app(RuntimeConfig(tmp_path, background=False, log=mode))
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert ("server-timing" in response.headers) == (mode == "performance")


def test_stream_duration_includes_body(monkeypatch, caplog):
    import asyncio

    ticks = iter([0, 0.05, 0.25])
    monkeypatch.setattr("lazarr.performance.perf_counter", lambda: next(ticks))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    async def send(message):
        pass

    with caplog.at_level(logging.INFO, logger="uvicorn.error.performance"):
        asyncio.run(PerformanceMiddleware(app)({"type": "http", "method": "GET"}, None, send))
    assert "duration_ms=250.000 ttfb_ms=50.000 completed=True" in caplog.text


def test_timings_and_privacy(caplog):
    app = FastAPI()
    app.add_middleware(PerformanceMiddleware)

    @app.get("/items/{identity}")
    async def item(identity: str):
        return StreamingResponse(iter([b"one", b"two"]))

    with caplog.at_level(logging.INFO, logger="uvicorn.error.performance"):
        response = TestClient(app).get("/items/private?api_key=secret")
    assert response.content == b"onetwo"
    assert float(response.headers["server-timing"].split("=")[1]) >= 0
    assert response.headers["x-request-id"] in caplog.text
    assert "route=/items/{identity}" in caplog.text
    assert "status=200" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "completed=True" in caplog.text
    assert "secret" not in caplog.text and "private" not in caplog.text


def test_failure_and_not_found(caplog):
    app = FastAPI()
    app.add_middleware(PerformanceMiddleware)

    @app.get("/fail")
    async def fail():
        raise RuntimeError("private error")

    with caplog.at_level(logging.INFO, logger="uvicorn.error.performance"):
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/fail").status_code == 500
        assert client.get("/missing/secret").status_code == 404
    assert "status=500" in caplog.text and "completed=False" in caplog.text
    assert "status=404" in caplog.text and "route=<unmatched>" in caplog.text
    assert "secret" not in caplog.text and "private error" not in caplog.text


def test_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("LAZARR_LOG", raising=False)
    assert RuntimeConfig(tmp_path).log == "standard"
    monkeypatch.setenv("LAZARR_LOG", "performance")
    assert RuntimeConfig(tmp_path).log == "performance"
    assert RuntimeConfig(tmp_path, log="standard").log == "standard"
    with pytest.raises(ValueError):
        RuntimeConfig(tmp_path, log="unknown")
