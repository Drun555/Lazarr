"""Rutracker forum adapter with persistent cookies and interactive CAPTCHA support."""

import base64
import re
from urllib.parse import urlencode, urljoin
from bs4 import BeautifulSoup
from lazarr.sdk import (
    ContentProvider,
    ProviderManifest,
    ConfigField,
    Candidate,
    Evidence,
    SearchPage,
    DownloadSource,
    ProviderError,
    AuthResult,
)
from lazarr.provider_utils import size_bytes, description_evidence, search_title


def page_text(response):
    if response.extensions.get("browser_rendered"):
        return response.text
    # Rutracker normally serves Windows-1251, but mirrors may serve UTF-8.
    charset = response.headers.get("content-type", "").lower()
    if "1251" in charset or b"windows-1251" in response.content[:4096].lower():
        return response.content.decode("cp1251", errors="replace")
    return response.text


class Plugin(ContentProvider):
    manifest = ProviderManifest(
        id="rutracker",
        name="Rutracker",
        kind="content",
        version="1.0.3",
        sdk=">=1.3,<2",
        config_fields=[
            ConfigField(name="base_url", label="URL форума", default="https://rutracker.org/forum"),
            ConfigField(name="username", label="Логин", secret=True),
            ConfigField(name="password", label="Пароль", secret=True),
            ConfigField(name="session_cookie", label="bb_session (необязательно)", secret=True),
            ConfigField(name="flaresolverr_url", label="FlareSolverr URL (необязательно)"),
        ],
        auth_methods=["password", "cookie", "captcha"],
        capabilities=["search", "torrent", "authentication"],
    )

    def base(self):
        return self.ctx.base_url("https://rutracker.org/forum")

    async def authenticate(self, values):
        cookie = values.get("session_cookie") or self.ctx.config.get("session_cookie")
        if cookie and not values.get("cap_code"):
            from urllib.parse import urlparse

            self.ctx.http.cookies.set("bb_session", cookie, domain=urlparse(self.base()).hostname, path="/")
            self.ctx.state["authenticated"] = True
            return AuthResult(status="authenticated")
        username = values.get("username") or self.ctx.config.get("username", "")
        password = values.get("password") or self.ctx.config.get("password", "")
        if not username or not password:
            return AuthResult(status="required", message="Укажите логин и пароль либо bb_session")
        form = {"login_username": username, "login_password": password, "login": "Вход", "autologin": "on"}
        form.update(self.ctx.state.get("challenge_tokens", {}))
        if values.get("cap_code"):
            form["cap_code"] = values["cap_code"]
        response = await self.ctx.request(
            "POST",
            self.base() + "/login.php",
            browser_html=True,
            browser_form_encoding="cp1251",
            content=urlencode(form, encoding="cp1251", errors="replace").encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.base() + "/login.php",
            },
        )
        text = page_text(response)
        soup = BeautifulSoup(text, "html.parser")
        challenge = soup.select_one('input[name="cap_code"]')
        if challenge:
            self.ctx.state["challenge_tokens"] = {
                node.get("name"): node.get("value", "")
                for node in soup.select('input[type="hidden"]')
                if node.get("name")
            }
            image = soup.select_one('img[src*="captcha"], img[src*="cap.php"]')
            image_url = None
            if image:
                location = urljoin(self.base() + "/", image["src"])
                from urllib.parse import urlparse

                if urlparse(location).netloc == urlparse(self.base()).netloc:
                    picture = await self.ctx.request("GET", location)
                    if (
                        picture.headers.get("content-type", "").startswith("image/")
                        and len(picture.content) < 1_000_000
                    ):
                        image_url = "data:image/png;base64," + base64.b64encode(picture.content).decode()
            return AuthResult(
                status="challenge",
                message="Введите CAPTCHA",
                fields=[ConfigField(name="cap_code", label="CAPTCHA", required=True)],
                image_url=image_url,
            )
        if any(c.name == "bb_session" for c in self.ctx.http.cookies.jar) and (
            response.is_redirect or "logout" in text or "Выход" in text
        ):
            self.ctx.state["authenticated"] = True
            self.ctx.state.pop("challenge_tokens", None)
            return AuthResult(status="authenticated")
        self.ctx.state["authenticated"] = False
        return AuthResult(status="required", message="Не удалось войти; проверьте учётные данные")

    async def ensure_auth(self):
        if not self.ctx.state.get("authenticated"):
            result = await self.authenticate({})
            if result.status != "authenticated":
                raise ProviderError("auth_required", "Авторизуйтесь в настройках Rutracker")

    def require_page(self, response):
        text = page_text(response)
        if 'name="login_username"' in text or response.is_redirect:
            self.ctx.state["authenticated"] = False
            raise ProviderError("auth_required", "Сессия Rutracker истекла")
        return text

    async def search(self, query, cursor=None):
        await self.ensure_auth()
        title = query.text or search_title(query.media)
        params = {"nm": title, "o": "10", "s": "2", "start": str(int(cursor or 0))}
        response = await self.ctx.request(
            "POST",
            self.base() + "/tracker.php",
            browser_html=True,
            browser_form_encoding="cp1251",
            content=urlencode(params, encoding="cp1251", errors="replace").encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": self.base() + "/tracker.php",
            },
        )
        text = self.require_page(response)
        soup = BeautifulSoup(text, "html.parser")
        items = []
        for row in soup.select("tr.hl-tr, tr[id^='tr-'], tr[id^='tor-']"):
            link = row.select_one("a.tLink, a[data-topic_id], a[href*='viewtopic.php?t=']")
            if not link:
                continue
            identity = (
                link.get("data-topic_id")
                or (re.search(r"[?&]t=(\d+)", link.get("href", "")) or [None, None])[1]
            )
            if not identity:
                continue
            category = row.select_one("a[href*='viewforum.php']")
            seed = row.select_one(".seedmed, .seed")
            size = row.select_one(".tor-size")
            items.append(
                Candidate(
                    provider="rutracker",
                    id=str(identity),
                    url=f"{self.base()}/viewtopic.php?t={identity}",
                    title=link.get_text(" ", strip=True),
                    evidence=[
                        Evidence(
                            field="category", value=category.get_text(" ", strip=True), source="structured"
                        )
                    ]
                    if category
                    else [],
                    size=size_bytes(size.get_text(" ", strip=True)) if size else None,
                    seeds=int(seed.get_text(strip=True))
                    if seed and seed.get_text(strip=True).isdigit()
                    else None,
                )
            )
        if (
            not items
            and not soup.select_one("#tor-tbl, .empty-search-result")
            and not re.search("не найден|нет результатов", text, re.I)
        ):
            raise ProviderError("parse_error", "Rutracker search page format changed")
        next_link = soup.find("a", string=re.compile(r"След|Next"))
        cursor_next = None
        if next_link:
            match = re.search(r"start=(\d+)", next_link.get("href", ""))
            cursor_next = match.group(1) if match else None
        return SearchPage(items=items, next_cursor=cursor_next)

    async def inspect(self, candidate):
        await self.ensure_auth()
        response = await self.ctx.request("GET", candidate.url, browser_html=True)
        soup = BeautifulSoup(self.require_page(response), "html.parser")
        post = soup.select_one(".post_body")
        if not post:
            raise ProviderError("parse_error", "Rutracker topic format changed")
        description = post.get_text("\n", strip=True)
        evidence = description_evidence(description)
        ids = {}
        imdb = re.search(r"imdb\.com/title/(tt\d+)", str(post))
        if imdb:
            ids["imdb"] = imdb.group(1)
        infohash = re.search(r"\b[A-Fa-f0-9]{40}\b", soup.get_text(" "))
        return candidate.model_copy(
            update={
                "description": description,
                "evidence": [*candidate.evidence, *evidence],
                "external_ids": ids,
                "revision": infohash.group(0).lower() if infohash else candidate.revision,
            }
        )

    async def resolve_download(self, candidate):
        await self.ensure_auth()
        response = await self.ctx.request(
            "GET", self.base() + "/dl.php", params={"t": candidate.id}, headers={"Referer": candidate.url}
        )
        if not response.content.startswith(b"d"):
            self.require_page(response)
            raise ProviderError("parse_error", "Rutracker did not return a torrent")
        return DownloadSource(torrent=response.content)

    async def auth_status(self):
        return AuthResult(status="authenticated" if self.ctx.state.get("authenticated") else "required")

    async def healthcheck(self):
        await self.ctx.request("GET", self.base() + "/index.php", browser_html=True)
        return {"ok": True, "auth": (await self.auth_status()).status}
