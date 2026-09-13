"""Nyaa RSS + HTML adapter. Search results are candidates, never final matches."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from lazarr.sdk import (
    ContentProvider,
    ProviderManifest,
    ConfigField,
    Candidate,
    SearchPage,
    DownloadSource,
    ProviderError,
)
from lazarr.provider_utils import size_bytes, description_evidence, search_title


class Plugin(ContentProvider):
    manifest = ProviderManifest(
        id="nyaa",
        name="Nyaa",
        kind="content",
        version="1.0.3",
        sdk=">=1.3,<2",
        config_fields=[ConfigField(name="base_url", label="URL", default="https://nyaa.si")],
        capabilities=["search", "torrent", "magnet", "anonymous"],
    )

    async def search(self, query, cursor=None):
        base = self.ctx.base_url("https://nyaa.si")
        title = query.text or search_title(query.media)
        # Do not require SxxExx: anime releases often use absolute numbering.
        response = await self.ctx.request("GET", base + "/", params={"page": "rss", "q": title, "c": "1_0"})
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise ProviderError("parse_error", "Nyaa RSS format changed") from exc
        items = []
        for item in root.findall("./channel/item"):
            fields = {child.tag.split("}")[-1]: child.text or "" for child in item}
            page = fields.get("guid", "")
            match = re.search(r"/view/(\d+)", page)
            link = fields.get("link", "")
            if not match:
                match = re.search(r"/(?:download|view)/(\d+)", link)
            if not match:
                continue
            identity = match.group(1)
            items.append(
                Candidate(
                    provider="nyaa",
                    id=identity,
                    revision=fields.get("infoHash", ""),
                    url=f"{base}/view/{identity}",
                    title=fields.get("title", ""),
                    size=size_bytes(fields.get("size", "")),
                    seeds=int(fields["seeders"]) if fields.get("seeders", "").isdigit() else None,
                    download_url=f"{base}/download/{identity}.torrent",
                    magnet=link if link.startswith("magnet:") else None,
                )
            )
        return SearchPage(items=items)

    async def inspect(self, candidate):
        response = await self.ctx.request("GET", candidate.url)
        soup = BeautifulSoup(response.text, "html.parser")
        description = soup.select_one("#torrent-description")
        if not soup.select_one(".panel-title") and not description:
            raise ProviderError("parse_error", "Nyaa detail page format changed")
        text = description.get_text("\n", strip=True) if description else ""
        hints = [
            li.get_text(" ", strip=True) for li in soup.select(".torrent-file-list li") if not li.find("ul")
        ]
        magnet = soup.select_one('a[href^="magnet:"]')
        return candidate.model_copy(
            update={
                "description": text,
                "file_hints": hints,
                "evidence": description_evidence(text),
                "magnet": magnet["href"] if magnet else candidate.magnet,
            }
        )

    async def resolve_download(self, candidate):
        base = self.ctx.base_url("https://nyaa.si")
        url = urljoin(base + "/", f"download/{candidate.id}.torrent")
        if urlparse(url).netloc != urlparse(base).netloc:
            raise ProviderError("configuration", "Invalid download URL")
        response = await self.ctx.request("GET", url)
        if not response.content.startswith(b"d"):
            raise ProviderError("parse_error", "Nyaa did not return a torrent")
        return DownloadSource(torrent=response.content)

    async def healthcheck(self):
        await self.ctx.request("GET", self.ctx.base_url("https://nyaa.si") + "/")
        return {"ok": True}
