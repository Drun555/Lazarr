"""Exercise the actual video route, not just the response helper."""

import asyncio

import anyio
import pytest
from fastapi.testclient import TestClient

from lazarr.app import create_app
from lazarr.http import FileResponse
from test_jellyfin import jellyfin_login, playable_episode
from test_jellyfin_state import items


@pytest.mark.parametrize("suffix", ["", ".mkv"])
@pytest.mark.parametrize("disconnect", [False, True])
def test_video_route_pause_resume_and_disconnect(core, media, season, monkeypatch, suffix, disconnect):
    video, _ = playable_episode(core, media, season)
    content = bytes(range(256)) * (32 * 1024)
    video.write_bytes(content)
    reads, opened = [], []
    original = anyio.AsyncFile.read

    async def tracked_read(file, size=-1):
        data = await original(file, size)
        reads.append(len(data))
        opened.append(file)
        return data

    with TestClient(create_app(core[0])) as client:
        auth = jellyfin_login(client)
        item = items(client)[0]
        path = f"/Videos/{item['Id']}/stream{suffix}"
        monkeypatch.setattr(anyio.AsyncFile, "read", tracked_read)

        async def scenario():
            paused, resume, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
            messages = []

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not paused.is_set():
                    paused.set()
                    await resume.wait()
                await anyio.lowlevel.checkpoint()

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 1234),
                "headers": [(b"x-emby-token", auth["AccessToken"].encode()), (b"range", b"bytes=123-")],
            }
            task = asyncio.create_task(client.app(scope, receive, send))
            try:
                await asyncio.wait_for(paused.wait(), 2)
                read_at_pause = sum(reads)
                await asyncio.sleep(0.05)
                assert not task.done()
                assert sum(reads) == read_at_pause  # No read-ahead while send is blocked.
                if disconnect:
                    disconnected.set()
                    resume.set()
                else:
                    resume.set()
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert messages[0]["status"] == 206
            headers = dict(messages[0]["headers"])
            assert headers[b"x-accel-buffering"] == b"no"
            assert headers[b"content-range"] == f"bytes 123-{len(content) - 1}/{len(content)}".encode()
            if disconnect:
                assert sum(reads) <= 2 * FileResponse.chunk_size
            else:
                assert b"".join(m.get("body", b"") for m in messages) == content[123:]
                assert messages[-1]["more_body"] is False
            assert opened and all(file.closed for file in opened)

        client.portal.call(scenario)
        # A client may reopen the stream with a new Range after a pause or seek.
        response = client.get(path, headers={"Range": "bytes=3145729-4194310"})
        assert response.status_code == 206
        assert response.content == content[3145729:4194311]
        assert client.head(path).headers["x-accel-buffering"] == "no"
