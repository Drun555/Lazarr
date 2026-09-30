"""HTTP security headers."""

from starlette.datastructures import MutableHeaders


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
