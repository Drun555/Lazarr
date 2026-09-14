from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from lazarr.bundled.kinozal import Plugin, page_text
from lazarr.config import Requirements
from lazarr.matcher import Matcher
from lazarr.sdk import Candidate, ProviderContext, ProviderError, SearchQuery, SubtaskRequest
from lazarr.selection import reject_reason, title_episode_coverage, title_seasons


FIXTURES = Path(__file__).parent / "fixtures"


def html(name):
    return httpx.Response(200, content=(FIXTURES / name).read_text().encode("cp1251"))


def item():
    return Candidate(
        provider="kinozal", id="42", url="https://kinozal.tv/details.php?id=42", title="Old title"
    )


async def test_search_cp1251_mirror_pagination_original_title_and_category(core, media):
    _, _, manager, _ = core
    manager.configure("kinozal", {"base_url": "https://mirror.example"}, True)

    def transport(request):
        assert request.url.host == "mirror.example"
        params = parse_qs(request.url.query.decode("ascii"), encoding="cp1251")
        assert params["s"] == ["Тестовый сериал"]
        assert params["c"] == params["d"] == ["0"]
        assert params["t"] == ["1"]
        return html("kinozal-search.html")

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("kinozal") as provider:
        query = SearchQuery(text="Тестовый сериал", media=media, season=2, requirements=Requirements())
        page = await provider.search(query)
        assert page.next_cursor == "1"
        assert [v.id for v in page.items] == ["42", "43"]
        assert page.items[0].title.startswith("Тестовый сериал (2 сезон: 1-3 серии из 8)")
        assert page.items[0].url == "https://mirror.example/details.php?id=42"
        assert page.items[0].seeds == 1234
        assert page.items[0].size == int(2.5 * 1024**3)
        assert page.items[0].evidence[0].value == "Сериалы"
        assert (await provider.search(query, "1")).next_cursor == "2"
        assert (await provider.search(query, "2")).next_cursor is None


async def test_inspect_release_tab_tracks_ids_and_revision(core):
    _, _, manager, _ = core
    manager.configure("kinozal", {}, True)
    paths = []
    updated = False

    def transport(request):
        assert request.url.host == "kinozal.me"
        paths.append(request.url.path)
        if request.url.path == "/get_srv_details.php":
            assert request.url.params["pagesd"] == "0"
            return html("kinozal-release.html")
        response = html("kinozal-topic.html")
        if updated:
            return httpx.Response(200, content=response.content.replace(b"12:00", b"13:00"))
        return response

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("kinozal") as provider:
        result = await provider.inspect(item())
        assert result.external_ids == {"imdb": "tt0042", "kinopoisk": "123"}
        assert result.url.startswith("https://kinozal.me/")
        assert "Другой фильм" not in result.description and "Комментарий" not in result.description
        tracks = {(e.field, tuple(e.value), e.delivery) for e in result.evidence}
        assert ("audio_languages", ("en",), "embedded") in tracks
        assert ("audio_languages", ("ru",), "external") in tracks
        assert ("subtitle_languages", ("ru",), "external") in tracks
        assert ("subtitle_languages", ("en",), "unspecified") in tracks
        assert not any(set(e.value) & {"ja", "fr", "de", "es"} for e in result.evidence)
        assert all(e.scope == "release" and not e.complete for e in result.evidence)
        assert result.revision == (await provider.inspect(item())).revision
        updated = True
        assert result.revision != (await provider.inspect(item())).revision
    assert paths == ["/details.php", "/get_srv_details.php"] * 3


async def test_password_login_cp1251_hidden_fields_and_persisted_session(core):
    _, _, manager, _ = core
    manager.configure("kinozal", {"username": "Пользователь", "password": "Пароль"}, True)
    posts = []

    def transport(request):
        if request.url.path == "/login.php":
            return httpx.Response(
                200, text='<form action="/takelogin.php"><input type="hidden" name="touser" value="1"></form>'
            )
        if request.url.path == "/takelogin.php":
            posts.append(parse_qs(request.content.decode(), encoding="cp1251"))
            return httpx.Response(302, headers={"location": "/my.php", "set-cookie": "uid=123; Path=/"})
        if request.url.path == "/my.php":
            assert "uid=123" in request.headers["cookie"]
            return httpx.Response(200, text='<a href="/logout.php?hash4u=test">Выход</a>')
        return html("kinozal-search.html")

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("kinozal") as provider:
        assert (await provider.authenticate({})).status == "authenticated"
    async with manager.open("kinozal") as provider:
        assert (await provider.auth_status()).status == "authenticated"
        await provider.ensure_auth()
    assert posts == [{"touser": ["1"], "username": ["Пользователь"], "password": ["Пароль"]}]


async def test_cookie_validation_torrent_and_magnet_fallback(core):
    _, _, manager, _ = core
    manager.configure("kinozal", {"session_cookie": "uid=123; pass=secret"}, True)
    limited = False

    def transport(request):
        assert "uid=123" in request.headers["cookie"] and "pass=secret" in request.headers["cookie"]
        if request.url.path == "/my.php":
            return httpx.Response(200, text='<a href="/logout.php?hash4u=x">Выход</a>')
        if request.url.path == "/download.php":
            if limited:
                return httpx.Response(200, text="Исчерпан лимит скачивания torrent-файлов за сутки")
            return httpx.Response(200, content=b"d4:infodee")
        assert request.url.params["action"] == "2"
        return httpx.Response(200, text="<ul><li>Инфо хеш: " + "a" * 40 + "</li></ul>")

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("kinozal") as provider:
        assert (await provider.resolve_download(item())).torrent == b"d4:infodee"
        limited = True
        assert (await provider.resolve_download(item())).magnet == "magnet:?xt=urn:btih:" + "a" * 40


@pytest.mark.parametrize(
    "body,code",
    [
        ('<title>Вход :: Кинозал.GURU</title><form action="/takelogin.php"></form>', "auth_required"),
        ("Вы не зарегистрированный пользователь или не авторизированы", "auth_required"),
        ("<h1>Proxy maintenance</h1>", "parse_error"),
    ],
)
async def test_login_gate_and_unknown_html_are_not_empty_results(core, media, body, code):
    _, _, manager, _ = core
    manager.configure("kinozal", {}, True)
    manager.transport = httpx.MockTransport(lambda r: httpx.Response(200, text=body))
    with pytest.raises(ProviderError) as error:
        async with manager.open("kinozal") as provider:
            await provider.search(SearchQuery(media=media, requirements=Requirements()))
    assert error.value.code == code


async def test_empty_search_and_expired_cookie(core, media):
    _, _, manager, _ = core
    manager.configure("kinozal", {}, True)
    manager.transport = httpx.MockTransport(lambda r: httpx.Response(200, text="Найдено 0 раздач"))
    async with manager.open("kinozal") as provider:
        assert (await provider.search(SearchQuery(media=media, requirements=Requirements()))).items == []
    manager.configure("kinozal", {"session_cookie": "uid=1; pass=expired"}, True)
    manager.transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text="<title>Вход :: Кинозал.МЕ</title>")
    )
    async with manager.open("kinozal") as provider:
        assert (await provider.authenticate({})).status == "required"
        assert not provider.ctx.state["authenticated"]


def test_browser_utf8_is_not_decoded_as_cp1251():
    response = httpx.Response(200, text='<meta charset="windows-1251">Субтитры: Русские')
    response.extensions["browser_rendered"] = True
    assert "Русские" in page_text(response)
    response = httpx.Response(200, content='<meta charset="UTF-8">Субтитры: Русские'.encode())
    assert "Русские" in page_text(response)


async def test_cloudflare_binary_failure_uses_same_release_infohash():
    from unittest.mock import AsyncMock

    async with httpx.AsyncClient() as client:
        context = ProviderContext({}, {"authenticated": True}, client)
        context.request = AsyncMock(
            side_effect=[
                ProviderError("unavailable", "Cloudflare продолжает блокировать скачивание файла", 300),
                httpx.Response(200, text="<ul><li>Инфо хеш: " + "b" * 40 + "</li></ul>"),
            ]
        )
        provider = Plugin(context)
        result = await provider.resolve_download(item())
        assert result.magnet == "magnet:?xt=urn:btih:" + "b" * 40
        first, second = context.request.call_args_list
        assert first.args[1] == "https://kinozal.me/download.php?id=42"
        assert not first.kwargs.get("browser_html")
        assert second.args[1] == "https://kinozal.me/get_srv_details.php?id=42&action=2"
        assert second.kwargs["browser_html"]


@pytest.mark.parametrize(
    "title,seasons,coverage",
    [
        ("Сериал (2 сезон: 1-3 серии из 8)", {2}, (2, {1, 2, 3})),
        ("Сериал (1–2 сезоны: 1-16 серии из 16)", {1, 2}, (None, set())),
        ("Сериал (4 сезон: 0-12 серии из 12)", {4}, (4, set(range(13)))),
        ("Сериал (3 сезон: 8 серия из 10)", {3}, (3, {8})),
        ("Сериал (1-2 сезон: 1-20 серии из 20)", {1, 2}, (None, set())),
        ("Сериал (2 сезон: 8-1 серии из 8)", {2}, (None, set())),
        ("Сериал / Сезон: 02 / Серии: 1-8", {2}, (None, set())),
    ],
)
def test_kinozal_season_and_partial_episode_coverage(title, seasons, coverage):
    assert title_seasons(title) == seasons
    assert title_episode_coverage(title) == coverage


def test_wrong_season_and_unreleased_episodes_rejected_before_inspect(media):
    title = "Example Show (2 сезон: 1-3 серии из 8) / 2020 / СТ / WEB-DL (1080p)"
    candidate = item().model_copy(update={"title": title})
    request = SubtaskRequest(id=1, media=media, season=1, episode=1, requirements=Requirements())
    assert "другой сезон" in reject_reason(candidate, [request])
    request.season = 2
    assert reject_reason(candidate, [request]) is None
    assert Matcher().identity(candidate, request).result == "MATCH"
    request.episode = 8
    assert "не пересекаются" in reject_reason(candidate, [request])
    candidate.title = "Example Show (1-2 сезон: 1-16 серии из 16) / 2020 / WEB-DL (1080p)"
    assert reject_reason(candidate, [request]) is None


def test_mirror_setting_rejects_non_root_url():
    provider = Plugin(ProviderContext({"base_url": "https://kinozal.me/browse.php?s=x"}, {}, None))
    with pytest.raises(ProviderError, match="корневой URL"):
        provider.base()
