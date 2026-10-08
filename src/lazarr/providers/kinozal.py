"""Kinozal's CP1251 catalogue, release tabs and authenticated torrent downloads."""

import hashlib
import re
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from lazarr.provider_utils import description_evidence, search_title, size_bytes
from lazarr.sdk import (
    AuthResult,
    Candidate,
    ConfigField,
    ContentProvider,
    DownloadSource,
    Evidence,
    ProviderError,
    ProviderManifest,
    SearchPage,
)


DEFAULT_URL = "https://kinozal.me"
CATEGORIES = {
    45: "Русские сериалы",
    46: "Сериалы",
    20: "Аниме",
    21: "Мультфильмы",
    22: "Русские мультфильмы",
    2: "Аудиокниги",
    23: "Игры",
    32: "Программы",
    40: "Изображения",
    41: "Книги",
    **dict.fromkeys((3, 4, 5, 42), "Музыка"),
}
NON_VIDEO = {2, 3, 4, 5, 23, 32, 40, 41, 42}


def page_text(response):
    if response.extensions.get("browser_rendered"):
        return response.text
    encoding = response.headers.get("content-type", "").lower()
    if "utf-8" in encoding or re.search(rb"charset\s*=\s*[\"']?utf-8", response.content[:4096], re.I):
        return response.content.decode("utf-8", errors="replace")
    # The tracker sometimes omits the HTTP charset. Mirrors still use CP1251.
    return response.content.decode("cp1251", errors="replace")


def block_text(node):
    """Preserve line boundaries without splitting inline labels from their values."""
    node = BeautifulSoup(str(node), "html.parser")
    for item in node.select("script, style, iframe"):
        item.decompose()
    for item in node.select("br"):
        item.replace_with("\n")
    for item in node.select("div, p, li, tr, h2, pre"):
        item.insert_before("\n")
        item.insert_after("\n")
    return "\n".join(line for raw in node.get_text().splitlines() if (line := " ".join(raw.split())))


class Plugin(ContentProvider):
    manifest = ProviderManifest(
        id="kinozal",
        name="Kinozal",
        kind="content",
        version="1.0.1",
        sdk=">=1.3,<2",
        config_fields=[
            ConfigField(name="base_url", label="URL зеркала Kinozal", default="https://kinozal.me"),
            ConfigField(name="username", label="Логин", secret=True),
            ConfigField(name="password", label="Пароль", secret=True),
            ConfigField(name="session_cookie", label="Cookies uid и pass (необязательно)", secret=True),
            ConfigField(name="trawl_url", label="Trawl URL (необязательно)"),
        ],
        auth_methods=["password", "cookie"],
        capabilities=["search", "torrent", "magnet", "authentication"],
    )

    def base(self):
        base = self.ctx.base_url(DEFAULT_URL)
        parsed = urlparse(base)
        if parsed.query or parsed.fragment or parsed.path.strip("/"):
            raise ProviderError("configuration", "Укажите корневой URL зеркала Kinozal без пути и параметров")
        return base

    def location(self, page, identity):
        if not re.fullmatch(r"[1-9]\d*", str(identity)):
            raise ProviderError("parse_error", "Некорректный ID раздачи Kinozal")
        # Stored candidates and absolute links can still refer to an old mirror.
        return f"{self.base()}/{page}?id={identity}"

    async def html(self, url, **kwargs):
        response = await self.ctx.request("GET", url, browser_html=True, follow_redirects=True, **kwargs)
        return BeautifulSoup(page_text(response), "html.parser")

    def require_page(self, soup):
        # Public pages have a sidebar login form too; that is not an expired session.
        title = soup.title.get_text() if soup.title else ""
        if re.search(
            r"^\s*Вход\s*::|чтобы просматривать страницы|вы не зарегистрированный пользователь|вы не авторизованы",
            title + " " + soup.get_text(" "),
            re.I,
        ):
            self.ctx.state["authenticated"] = False
            raise ProviderError("auth_required", "Для доступа к Kinozal проверьте учётные данные провайдера")
        if re.search(r"раздача (?:не найдена|удалена)|нет раздачи|не существует", soup.get_text(" "), re.I):
            # Only tracker error panels, never arbitrary release descriptions/comments.
            if not soup.select_one("h1 a[href*='details.php'], td.nam"):
                raise ProviderError("not_found", "Раздача Kinozal не найдена")

    async def authenticate(self, values):
        self.ctx.state["authenticated"] = False
        cookie = values.get("session_cookie") or self.ctx.config.get("session_cookie")
        if cookie:
            parsed = SimpleCookie()
            try:
                parsed.load(cookie)
            except Exception as exc:
                raise ProviderError("configuration", "Укажите cookies в формате uid=…; pass=…") from exc
            if not all(key in parsed for key in ("uid", "pass")):
                raise ProviderError("configuration", "Укажите cookies в формате uid=…; pass=…")
            for key in ("uid", "pass"):
                self.ctx.http.cookies.set(
                    key, parsed[key].value, domain=urlparse(self.base()).hostname, path="/"
                )
        else:
            username = values.get("username") or self.ctx.config.get("username", "")
            password = values.get("password") or self.ctx.config.get("password", "")
            if not username or not password:
                return AuthResult(
                    status="required", message="Укажите логин и пароль Kinozal либо cookies uid и pass"
                )
            login = await self.html(self.base() + "/login.php")
            form = login.select_one("form[action*='takelogin.php']")
            fields = (
                {item["name"]: item.get("value", "") for item in form.select("input[type='hidden'][name]")}
                if form
                else {}
            )
            fields.update(username=username, password=password)
            await self.ctx.request(
                "POST",
                self.base() + "/takelogin.php",
                browser_html=True,
                browser_form_encoding="cp1251",
                follow_redirects=True,
                content=urlencode(fields, encoding="cp1251").encode("ascii"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": self.base() + "/login.php",
                },
            )
        profile = await self.html(self.base() + "/my.php")
        if profile.select_one("a[href*='logout.php']"):
            self.ctx.state["authenticated"] = True
            return AuthResult(status="authenticated")
        return AuthResult(
            status="required", message="Не удалось войти в Kinozal; проверьте логин, пароль или cookies"
        )

    async def ensure_auth(self, required=False):
        if self.ctx.state.get("authenticated"):
            return
        if required or any(self.ctx.config.get(key) for key in ("username", "password", "session_cookie")):
            result = await self.authenticate({})
            if result.status != "authenticated":
                raise ProviderError("auth_required", result.message)

    async def auth_status(self):
        return AuthResult(status="authenticated" if self.ctx.state.get("authenticated") else "required")

    async def healthcheck(self):
        soup = await self.html(self.base() + "/browse.php")
        if not soup.select_one("form[action*='browse.php'], form[action*='takelogin.php']"):
            raise ProviderError("parse_error", "По указанному URL не найдена страница Kinozal")
        return {"ok": True, "auth": (await self.auth_status()).status}

    async def search(self, query, cursor=None):
        await self.ensure_auth()
        page = int(cursor or 0)
        if page < 0:
            raise ProviderError("configuration", "Некорректная страница поиска Kinozal")
        title = query.text or search_title(query.media)
        params = {"s": title, "g": 0, "c": 0, "v": 0, "d": 0, "w": 0, "t": 1, "f": 0, "page": page}
        # Do not add a season/year filter: multi-season packs and animation share categories.
        url = self.base() + "/browse.php?" + urlencode(params, encoding="cp1251", errors="replace")
        soup = await self.html(url)
        self.require_page(soup)
        items = []
        seen = set()
        rows = soup.select("tr:has(> td.nam)")
        for row in rows:
            link = row.select_one("td.nam a[href*='details.php?']")
            match = re.search(r"[?&]id=(\d+)", link.get("href", "")) if link else None
            if not match or match[1] in seen:
                continue
            seen.add(match[1])
            image = row.select_one("td.bt img")
            category = re.search(r"cat\((\d+)\)|/cat/(\d+)\.", str(image)) if image else None
            category_id = int(category[1] or category[2]) if category else None
            if category_id in NON_VIDEO:
                continue
            label = (image.get("title") or image.get("alt")) if image else None
            label = label or CATEGORIES.get(category_id, "")
            # Kinozal omits </td> after the title in raw HTML. html.parser nests
            # the remaining cells there; browser-rendered HTML closes them.
            cells = [cell for cell in row.find_all("td") if cell.find_parent("tr") is row]
            seed = row.select_one("td.sl_s") or (cells[4] if len(cells) > 4 else None)
            seed_text = re.sub(r"\s", "", seed.get_text()) if seed else ""
            items.append(
                Candidate(
                    provider="kinozal",
                    id=match[1],
                    url=self.location("details.php", match[1]),
                    title=link.get_text(" ", strip=True),
                    size=size_bytes(cells[3].get_text(" ", strip=True)) if len(cells) > 3 else None,
                    seeds=int(seed_text) if seed_text.isdigit() else None,
                    evidence=[Evidence(field="category", value=label, source="structured")] if label else [],
                )
            )
        if not rows and not re.search(
            r"найдено\s*0|ничего не найдено|раздач[аи]? не найден|нет раздач", soup.get_text(" "), re.I
        ):
            raise ProviderError("parse_error", "Изменился формат результатов поиска Kinozal")
        pages = []
        for link in soup.select("a[href*='page=']"):
            parsed = urlparse(link["href"])
            if parsed.path not in {"", "browse.php", "/browse.php"}:
                continue
            value = parse_qs(parsed.query).get("page", [""])[0]
            if value.isdigit() and int(value) == page + 1:
                pages.append(value)
        return SearchPage(items=items, next_cursor=pages[0] if pages else None)

    async def inspect(self, candidate):
        await self.ensure_auth()
        url = self.location("details.php", candidate.id)
        soup = await self.html(url)
        self.require_page(soup)
        content = soup.select_one(".mn1_content")
        technical = soup.select_one("#tabs")
        heading = soup.select_one("h1")
        if not content or not technical or not heading:
            raise ProviderError("parse_error", "Изменился формат страницы раздачи Kinozal")
        sections = [block_text(node) for node in content.select(":scope > .bx1.justify")]
        sections.append(block_text(technical))
        # Additional audio/subtitle layout often lives in the AJAX 'Релиз' tab.
        for link in content.select("a[onclick*='showtab(']"):
            match = re.search(r"showtab\(\s*(\d+)\s*,\s*(\d+)\s*\)", link["onclick"])
            if not match or match[1] != candidate.id or link.get_text(strip=True).casefold() != "релиз":
                continue
            extra = await self.html(
                self.location("get_srv_details.php", candidate.id) + "&pagesd=" + match[2],
                headers={"Referer": url},
            )
            self.require_page(extra)
            sections.append(block_text(extra))
            break
        evidence = [
            item.model_copy(update={"scope": "release"})
            for section in sections
            for item in description_evidence(section)
        ]
        ids = dict(candidate.external_ids)
        # Rating links belong to this release. Related releases and comments do not.
        for link in soup.select(".mn1_menu a[href]"):
            imdb = re.search(r"imdb\.com/title/(tt\d+)", link["href"])
            kinopoisk = re.search(r"kinopoisk\.ru/(?:film|series)/(\d+)", link["href"])
            if imdb:
                ids["imdb"] = imdb[1]
            if kinopoisk:
                ids["kinopoisk"] = kinopoisk[1]
        title = heading.get_text(" ", strip=True)
        # A topic ID is stable when the torrent is replaced with newly added episodes.
        revision = "\n".join(
            node.get_text(" ", strip=True)
            for node in soup.select(".mn1_menu li")
            if re.match(r"Обновлен|Вес|Залит", node.get_text(" ", strip=True))
        )
        return candidate.model_copy(
            update={
                "url": url,
                "title": title,
                "description": "\n\n".join(sections),
                "external_ids": ids,
                "evidence": [*candidate.evidence, *evidence],
                "revision": hashlib.sha256((title + "\n" + revision).encode()).hexdigest(),
            }
        )

    async def resolve_download(self, candidate):
        await self.ensure_auth(required=True)
        referer = self.location("details.php", candidate.id)
        try:
            response = await self.ctx.request(
                "GET",
                self.location("download.php", candidate.id),
                follow_redirects=True,
                headers={"Referer": referer},
            )
        except ProviderError as exc:
            if exc.code != "unavailable" or "Cloudflare" not in str(exc):
                raise
            response = None
        if response is not None:
            if response.content.startswith(b"d"):
                return DownloadSource(torrent=response.content)
            soup = BeautifulSoup(page_text(response), "html.parser")
            self.require_page(soup)
            if not re.search(r"лимит|ограничени|торрент.{0,80}(?:сутки|день)", soup.get_text(" "), re.I):
                raise ProviderError("parse_error", "Kinozal не вернул torrent-файл")
        # Kinozal exposes the same torrent's infohash alongside the file list.
        soup = await self.html(
            self.location("get_srv_details.php", candidate.id) + "&action=2", headers={"Referer": referer}
        )
        self.require_page(soup)
        first = soup.select_one("li")
        match = re.search(r"\b[a-fA-F0-9]{40}\b", first.get_text(" ") if first else "")
        if not match:
            raise ProviderError(
                "rate_limited", "Kinozal ограничил выдачу torrent-файлов; magnet недоступен", 3600
            )
        return DownloadSource(magnet="magnet:?xt=urn:btih:" + match[0].lower())
