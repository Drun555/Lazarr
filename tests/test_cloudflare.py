import httpx
import pytest
from lazarr.sdk import ProviderContext, ProviderError


def challenge():
    return httpx.Response(403, headers={"cf-mitigated": "challenge"})


async def test_solver_retries_binary_with_clearance_and_browser_agent():
    calls = []

    def site(request):
        calls.append(request)
        if "cf_clearance=clear" in request.headers.get("cookie", ""):
            assert request.headers["user-agent"] == "Browser/1"
            return httpx.Response(200, content=b"d4:infodee")
        return challenge()

    def solver(request):
        assert str(request.url) == "http://solver:8191/v1"
        assert request.extensions["timeout"]["read"] == 190
        assert request.extensions["timeout"]["write"] == 190
        assert request.extensions["timeout"]["connect"] == 190
        assert request.extensions["timeout"]["pool"] == 190
        assert request.extensions["timeout"]
        import json

        assert json.loads(request.content)["maxTimeout"] == 180000
        assert not request.headers.get("cookie")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "status": 200,
                    "url": "https://tracker.test/dl.php",
                    "userAgent": "Browser/1",
                    "response": "<html>ok</html>",
                    "cookies": [
                        {"name": "cf_clearance", "value": "clear", "domain": "tracker.test"},
                        {"name": "evil", "value": "no", "domain": "other.test"},
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        ctx = ProviderContext({"trawl_url": "http://solver:8191"}, {}, client)
        ctx.solver_transport = httpx.MockTransport(solver)
        assert (await ctx.request("GET", "https://tracker.test/dl.php")).content == b"d4:infodee"
        assert ctx.state["browser_user_agent"] == "Browser/1"
        assert "evil" not in client.cookies
    assert len(calls) == 2


async def test_rendered_html_fallback_never_substitutes_torrent_bytes():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: challenge())) as client:
        ctx = ProviderContext({"trawl_url": "http://solver:8191"}, {}, client)
        ctx.solver_transport = httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "userAgent": "Browser/1",
                        "response": "<html>Русский текст</html>",
                    },
                },
            )
        )
        response = await ctx.request("GET", "https://tracker.test/topic", browser_html=True)
        assert response.extensions["browser_rendered"] and "Русский" in response.text
        with pytest.raises(ProviderError, match="скачивание файла"):
            await ctx.request("GET", "https://tracker.test/dl.php")


async def test_solver_error_is_sanitized_and_backs_off():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: challenge())) as client:
        ctx = ProviderContext({"trawl_url": "http://solver:8191"}, {}, client)
        ctx.solver_transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"status": "error", "message": "secret POST data"})
        )
        with pytest.raises(ProviderError) as error:
            await ctx.request(
                "POST", "https://tracker.test/login", content=b"password=secret", browser_html=True
            )
        assert error.value.retry_after == 300 and "secret" not in str(error.value)


async def test_browser_post_preserves_query_and_cyrillic_form():
    import json
    from urllib.parse import urlencode, parse_qs

    calls = []

    def solver(request):
        data = json.loads(request.content)
        calls.append(data)
        assert data["url"] == "https://tracker.test/tracker.php?mode=search"
        if data["cmd"] == "request.post":
            assert parse_qs(data["postData"]) == {"nm": ["Жизнь с нуля"]}
            assert "+" not in data["postData"]
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {"status": 200, "userAgent": "Browser/1", "response": "<html>ok</html>"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: challenge())) as client:
        ctx = ProviderContext({"trawl_url": "http://solver:8191"}, {}, client)
        ctx.solver_transport = httpx.MockTransport(solver)
        await ctx.request(
            "POST",
            "https://tracker.test/tracker.php?mode=search",
            content=urlencode({"nm": "Жизнь с нуля"}, encoding="cp1251").encode("ascii"),
            browser_html=True,
            browser_form_encoding="cp1251",
        )
    assert [c["cmd"] for c in calls] == ["request.get", "request.post"]
