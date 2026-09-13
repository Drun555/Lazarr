"""Optional browser challenge transport. FlareSolverr remains a separate service."""

import os
from urllib.parse import urlparse, parse_qsl, urlencode, quote
import httpx


def challenged(response):
    return response.status_code in {403, 503} and (
        response.headers.get("cf-mitigated") == "challenge"
        or (
            "cloudflare" in response.headers.get("server", "").lower()
            and b"Just a moment" in response.content[:16384]
        )
    )


async def solve(context, method, url, kwargs, *, html, form_encoding="utf-8"):
    from lazarr.sdk import ProviderError

    endpoint = context.config.get("flaresolverr_url") or os.getenv("LAZARR_FLARESOLVERR_URL", "")
    if not endpoint:
        raise ProviderError(
            "unavailable", "Cloudflare блокирует доступ. Настройте FlareSolverr у провайдера.", 300
        )
    parsed = urlparse(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError("configuration", "Некорректный URL FlareSolverr")
    endpoint = endpoint.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint += "/v1"
    target = str(httpx.URL(url, params=kwargs["params"])) if "params" in kwargs else url
    hostname = urlparse(target).hostname

    async def browser(command):
        if context.request_gate:
            await context.request_gate()
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in context.http.cookies.jar
            if hostname == c.domain.lstrip(".") or hostname.endswith("." + c.domain.lstrip("."))
        ]
        payload = {"cmd": command, "url": target, "maxTimeout": 180000, "cookies": cookies}
        if command == "request.post":
            body = kwargs.get("content", b"")
            encoded = body.decode("ascii") if isinstance(body, bytes) else body
            payload["postData"] = urlencode(
                parse_qsl(encoded, encoding=form_encoding, keep_blank_values=True), quote_via=quote
            )
        try:
            # A separate client prevents provider cookies and Authorization from reaching the solver endpoint.
            async with httpx.AsyncClient(timeout=190, transport=context.solver_transport) as client:
                response = await client.post(endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
            solution = data.get("solution", {})
            if data.get("status") != "ok" or solution.get("status") != 200:
                raise ValueError("unsolved")
            if urlparse(solution.get("url", target)).hostname != hostname:
                raise ValueError("unexpected redirect")
            agent = solution.get("userAgent")
            if not agent:
                raise ValueError("missing browser identity")
            context.state["browser_user_agent"] = agent
            context.http.headers["User-Agent"] = agent
            for cookie in solution.get("cookies", []):
                domain = cookie.get("domain", hostname).lstrip(".")
                if hostname == domain or hostname.endswith("." + domain):
                    context.http.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie.get("domain", hostname),
                        path=cookie.get("path", "/"),
                    )
            result = httpx.Response(
                200,
                text=solution.get("response", ""),
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request(method, target),
            )
            result.extensions["browser_rendered"] = True
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "unavailable",
                "FlareSolverr не смог открыть страницу. Проверьте сервис и доступность провайдера.",
                300,
            ) from exc

    solved = await browser("request.get")
    try:
        retried = await context.http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ProviderError("unavailable", "Network request failed", 60) from exc
    if not challenged(retried):
        return retried
    if html and method.upper() == "GET":
        return solved
    if html and method.upper() == "POST":
        return await browser("request.post")
    # A rendered DOM cannot represent a torrent file or a CAPTCHA image.
    raise ProviderError(
        "unavailable", "Cloudflare продолжает блокировать скачивание файла после FlareSolverr", 300
    )
