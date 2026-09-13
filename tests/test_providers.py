from pathlib import Path
import httpx
import pytest
from lazarr.sdk import SearchQuery, ProviderError
from lazarr.config import Requirements

FIXTURES = Path(__file__).parent / "fixtures"


async def test_nyaa_search_inspect_and_download(core, media):
    _, _, manager, _ = core
    manager.configure("nyaa", {}, True)

    def transport(request):
        if request.url.path == "/":
            return httpx.Response(200, content=(FIXTURES / "nyaa.xml").read_bytes())
        if request.url.path.startswith("/view/"):
            return httpx.Response(200, text=(FIXTURES / "nyaa.html").read_text())
        return httpx.Response(200, content=b"d4:infodee")

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("nyaa") as provider:
        page = await provider.search(
            SearchQuery(media=media, season=1, episodes=[1], requirements=Requirements())
        )
        assert page.items[0].seeds == 42
        assert page.items[0].size == int(2.5 * 1024**3)
        candidate = await provider.inspect(page.items[0])
        assert candidate.file_hints == ["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]
        assert candidate.evidence[0].value == ["ru", "ja"]
        assert (await provider.resolve_download(candidate)).torrent == b"d4:infodee"


async def test_rutracker_cookie_pagination_cp1251_and_expiry(core, media):
    _, _, manager, _ = core
    manager.configure("rutracker", {"session_cookie": "private-session"}, True)
    expired = False

    def transport(request):
        if expired:
            return httpx.Response(200, text='<input name="login_username">')
        name = "rutracker-search.html" if request.url.path.endswith("tracker.php") else "rutracker-topic.html"
        return httpx.Response(
            200,
            content=(FIXTURES / name).read_text().encode("cp1251"),
            headers={"content-type": "text/html; charset=windows-1251"},
        )

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("rutracker") as provider:
        page = await provider.search(SearchQuery(media=media, season=1, requirements=Requirements()))
        assert page.next_cursor == "50"
        assert page.items[0].seeds == 15
        detailed = await provider.inspect(page.items[0])
        assert detailed.external_ids == {"imdb": "tt0042"}
        assert detailed.evidence[0].value == ["ru", "ja"]
    expired = True
    with pytest.raises(ProviderError, match="истекла"):
        async with manager.open("rutracker") as provider:
            await provider.search(SearchQuery(media=media, requirements=Requirements()))


async def test_interactive_captcha_and_rate_limit(core):
    _, _, manager, _ = core
    manager.configure("rutracker", {"username": "user", "password": "password"}, True)

    def transport(request):
        if request.url.path.endswith("captcha.php"):
            return httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
        if b"cap_code=1234" in request.content:
            return httpx.Response(
                302, headers={"set-cookie": "bb_session=authenticated; Path=/", "location": "index.php"}
            )
        return httpx.Response(200, text=(FIXTURES / "rutracker-captcha.html").read_text())

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("rutracker") as provider:
        result = await provider.authenticate({})
        assert result.status == "challenge" and result.image_url.startswith("data:image/")
    async with manager.open("rutracker") as provider:
        assert provider.ctx.state["challenge_tokens"]["cap_sid"] == "challenge-123"
        assert (await provider.authenticate({"cap_code": "1234"})).status == "authenticated"
    manager.transport = httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"retry-after": "90"})
    )
    with pytest.raises(ProviderError) as error:
        async with manager.open("rutracker") as provider:
            await provider.healthcheck()
    assert error.value.retry_after == 90


def test_description_tracks_survive_html_label_boundaries():
    from lazarr.provider_utils import description_evidence

    evidence = description_evidence(
        "Аудио:\nРусский, AC3 5.1\nЯпонский, AAC\nСубтитры:\nАнглийский\nВидео:\nH.264"
    )
    assert [e.value for e in evidence] == [["ru"], ["ja"], ["en"]]
    assert evidence[0].scope == evidence[1].scope == "all_video_files"
    assert evidence[2].scope == "release"


async def test_cloudflare_challenge_is_not_a_password_failure(core):
    _, _, manager, _ = core
    manager.configure("rutracker", {"username": "user", "password": "password"}, True)
    manager.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403,
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
            text="<html><title>Just a moment...</title>Enable JavaScript and cookies to continue</html>",
        )
    )
    with pytest.raises(ProviderError) as error:
        async with manager.open("rutracker") as provider:
            await provider.authenticate({})
    assert error.value.code == "unavailable"
    assert error.value.retry_after == 300
    assert "Cloudflare" in str(error.value)


def test_anime_search_uses_latin_alias_not_original_japanese():
    from lazarr.provider_utils import search_title
    from lazarr.sdk import MetadataItem

    media = MetadataItem(
        id="65942",
        kind="tv",
        title="Re:ZERO – Жизнь с нуля",
        original_title="Re:ゼロから始める異世界生活",
        aliases=["Re:0", "ReZero", "Re:Zero", "Re:Zero kara Hajimeru Isekai Seikatsu"],
    )
    assert search_title(media) == "Re:Zero"


def test_language_aliases_and_adjectives():
    from lazarr.config import Requirements
    from lazarr.provider_utils import description_evidence

    assert Requirements(
        audio_languages=["ru", "rus", "russian", "Русский", "ja", "jap", "japanese", "Японский"]
    ).audio_languages == ["ru", "ja"]
    assert description_evidence("Субтитры: Японские")[0].value == ["ja"]
    assert description_evidence("Аудио: Русская озвучка")[0].value == ["ru"]
    assert description_evidence("Описание: Японские школьники\nВидео: HEVC") == []
    assert description_evidence("MediaInfo\nАудио: RUS AAC") == []


async def test_rutracker_uses_requested_text_and_exposes_category(core, media):
    from urllib.parse import parse_qs

    _, _, manager, _ = core
    manager.configure("rutracker", {"session_cookie": "test"}, True)

    def transport(request):
        values = parse_qs(request.content.decode("ascii"), encoding="cp1251")
        assert values["nm"] == ["Re:Zero kara Hajimeru Isekai Seikatsu"]
        return httpx.Response(
            200,
            text='<table id="tor-tbl"><tr class="hl-tr"><td><a href="viewforum.php?f=1">Манга</a><a class="tLink" href="viewtopic.php?t=42">Re:Zero</a></td></tr></table>',
        )

    manager.transport = httpx.MockTransport(transport)
    async with manager.open("rutracker") as provider:
        page = await provider.search(
            SearchQuery(
                text="Re:Zero kara Hajimeru Isekai Seikatsu", media=media, requirements=Requirements()
            )
        )
    assert page.items[0].evidence[0].field == "category"
    assert page.items[0].evidence[0].value == "Манга"


def test_query_spelling_survives_alias_reordering_and_foreign_translations():
    from lazarr.provider_utils import search_titles
    from lazarr.sdk import MetadataItem

    aliases = ["Re Zero Empezar de cero en un mundo diferente", "ReZero", "Re:Zero", "Re Zero", "Re:0"]
    media = MetadataItem(
        id="65942",
        kind="tv",
        title="Re:ZERO – Жизнь с нуля",
        original_title="Re:ゼロから始める異世界生活",
        aliases=aliases,
    )
    assert search_titles(media) == ["Re:Zero", "Re Zero", "ReZero"]
    media.aliases = list(reversed(aliases))
    assert search_titles(media) == ["Re:Zero", "Re Zero", "ReZero"]
    media.title = "Другое название"
    media.aliases = ["Random Spanish Translation"]
    assert search_titles(media) == ["Другое название"]
