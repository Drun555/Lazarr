import asyncio

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lazarr.http import FileResponse, SecurityHeadersMiddleware


@pytest.mark.parametrize("range_header", [None, "bytes=0-", "bytes=0-1048575,2097152-"])
def test_disconnected_stream_stops_reading_and_closes_file(tmp_path, monkeypatch, range_header):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"v" * (8 * 1024 * 1024))
    reads, opened = [], []
    original = anyio.AsyncFile.read

    async def tracked_read(file, size=-1):
        opened.append(file)
        data = await original(file, size)
        reads.append(len(data))
        return data

    monkeypatch.setattr(anyio.AsyncFile, "read", tracked_read)

    async def scenario():
        disconnected = asyncio.Event()

        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            # Like Uvicorn, sends after disconnection can return without error.
            if message["type"] == "http.response.body" and reads:
                disconnected.set()
            await anyio.lowlevel.checkpoint()

        headers = [(b"range", range_header.encode())] if range_header else []
        scope = {"type": "http", "method": "GET", "path": "/video", "headers": headers}
        await asyncio.wait_for(SecurityHeadersMiddleware(FileResponse(path))(scope, receive, send), 2)

    asyncio.run(scenario())
    assert 0 < sum(reads) <= 2 * FileResponse.chunk_size
    assert all(file.closed for file in opened)


def test_completed_stream_ranges_head_and_security_headers(tmp_path):
    path = tmp_path / "video.mkv"
    content = b"0123456789" * 20000
    path.write_bytes(content)
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.api_route("/video", methods=["GET", "HEAD"])
    @app.get("/static/video")
    async def video():
        return FileResponse(path, headers={"Cache-Control": "public, max-age=60"})

    with TestClient(app) as client:
        response = client.get("/video")
        assert response.content == content
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["x-frame-options"] == "DENY"
        assert client.get("/static/video").headers["cache-control"] == "public, max-age=60"
        response = client.get("/video", headers={"Range": "bytes=3-8"})
        assert response.status_code == 206
        assert response.content == content[3:9]
        assert response.headers["content-range"] == f"bytes 3-8/{len(content)}"
        response = client.get("/video", headers={"Range": "bytes=0-2,8-10"})
        assert response.status_code == 206
        assert b"012" in response.content and b"890" in response.content
        response = client.head("/video")
        assert response.content == b""
        assert int(response.headers["content-length"]) == len(content)
        assert client.get("/video", headers={"Range": "bytes=999999-"}).status_code == 416
