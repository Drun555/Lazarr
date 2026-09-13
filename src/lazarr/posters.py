"""Authenticated TMDB image cache; clients never need access to the remote CDN."""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse
import httpx
from lazarr.sdk import ProviderError


def poster_url(value):
    if value and value.startswith("https://image.tmdb.org/t/p/"):
        name = value.rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|png|webp)", name):
            return "/api/v1/posters/tmdb/" + name
    return value


async def fetch_poster(ctx, filename):
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|png|webp)", filename):
        raise ValueError("Некорректное имя обложки")
    # Read configuration without requiring API credentials or an enabled search provider.
    from lazarr.models import ProviderConfig

    with ctx.db.session() as db:
        row = db.get(ProviderConfig, "tmdb")
        base = (row.config.get("image_base_url") if row else None) or "https://image.tmdb.org/t/p/w342"
    parsed = urlparse(base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Некорректный URL изображений TMDB")
    url = base.rstrip("/") + "/" + filename
    cache = (
        ctx.config.data_dir / "posters" / (hashlib.sha256(url.encode()).hexdigest() + Path(filename).suffix)
    )
    if cache.exists():
        return cache
    try:
        async with httpx.AsyncClient(timeout=20, transport=getattr(ctx, "poster_transport", None)) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.headers.get("content-type", "").split(";")[0] not in {
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                }:
                    raise ValueError("not an image")
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 5 * 1024 * 1024:
                        raise ValueError("image too large")
        if not (
            data.startswith(b"\xff\xd8\xff")
            or data.startswith(b"\x89PNG\r\n\x1a\n")
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
        ):
            raise ValueError("invalid image")
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, cache)
            finally:
                temporary.unlink(missing_ok=True)
        return cache
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError(
            "unavailable", "Обложка TMDB недоступна. Проверьте URL изображений в настройках провайдера.", 60
        ) from exc
