"""HTTP response lifecycle and security headers."""

import asyncio
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse as StarletteFileResponse


class FileResponse(StarletteFileResponse):
    """Stop reading a media file as soon as its client disconnects."""

    # Amortize worker-thread handoffs for high-bitrate media. Awaiting each
    # send still applies backpressure, including while a player is paused.
    chunk_size = 1024 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await super().__call__(scope, receive, send)

        async def disconnected():
            while True:
                if (await receive())["type"] == "http.disconnect":
                    return

        response = asyncio.create_task(super().__call__(scope, receive, send))
        listener = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((response, listener), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # Edge cancellation lets FileResponse await file.close() while
            # unwinding, unlike a cancelled AnyIO scope that cancels close too.
            for task in (response, listener):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response, listener, return_exceptions=True)


class SecurityHeadersMiddleware:
    """Add headers without buffering or wrapping the response body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": list(message["headers"])}
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                headers["X-Frame-Options"] = "DENY"
                if not scope["path"].startswith("/static/"):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, secured_send)
