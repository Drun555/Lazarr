"""Opt-in HTTP timings without URLs, credentials, or request bodies."""

import logging
from time import perf_counter
from uuid import uuid4


logger = logging.getLogger("uvicorn.error.performance")


class PerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        request_id = uuid4().hex
        status, ttfb, completed = 500, None, False

        async def timed_send(message):
            nonlocal status, ttfb, completed
            if message["type"] == "http.response.start":
                status = message["status"]
                ttfb = (perf_counter() - started) * 1000
                message = dict(message)
                message["headers"] = [
                    *message.get("headers", []),
                    (b"server-timing", f"app;dur={ttfb:.3f}".encode()),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            await self.app(scope, receive, timed_send)
        finally:
            # Route templates avoid tokens in query strings and arbitrary URL paths.
            route = getattr(scope.get("route"), "path", "<unmatched>")
            logger.info(
                "performance request_id=%s method=%s route=%s status=%s "
                "duration_ms=%.3f ttfb_ms=%s completed=%s",
                request_id,
                scope["method"],
                route,
                status,
                (perf_counter() - started) * 1000,
                f"{ttfb:.3f}" if ttfb is not None else "-",
                completed,
            )
