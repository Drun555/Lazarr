import httpx
from fastapi.testclient import TestClient
from lazarr.app import create_app
from lazarr.models import ProviderConfig
from lazarr.posters import poster_url
from test_api import login


def test_poster_proxy_cache_and_configured_host(core):
    config, _, _, _ = core
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/posters/tmdb/example.jpg").status_code == 401
        login(client)
        ctx = client.app.state.ctx
        with ctx.db.session() as db:
            db.get(ProviderConfig, "tmdb").config = {"image_base_url": "https://images.example/t/p/w342"}
        requests = []

        def transport(request):
            requests.append(request)
            assert request.url == "https://images.example/t/p/w342/example.jpg"
            assert not request.headers.get("authorization") and not request.headers.get("cookie")
            return httpx.Response(
                200, content=b"\xff\xd8\xfftest-image", headers={"content-type": "image/jpeg"}
            )

        ctx.poster_transport = httpx.MockTransport(transport)
        for _ in range(2):
            response = client.get("/api/v1/posters/tmdb/example.jpg")
            assert response.status_code == 200 and response.content == b"\xff\xd8\xfftest-image"
        assert len(requests) == 1
        assert client.get("/api/v1/posters/tmdb/bad.svg").status_code == 422
        ctx.poster_transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>blocked</html>"))
        assert client.get("/api/v1/posters/tmdb/other.jpg").status_code == 502
        assert not list((config.data_dir / "posters").glob("*.tmp"))
    assert poster_url("https://image.tmdb.org/t/p/w342/example.jpg") == "/api/v1/posters/tmdb/example.jpg"
