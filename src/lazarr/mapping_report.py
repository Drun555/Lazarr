"""Explicit, log-free diagnostic exports for search-engine issues."""

import base64
from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lazarr.matcher import Matcher
from lazarr.sdk import TorrentFile
from lazarr.search_runtime import current_engine


def public_url(value):
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname:
            return ""
        host = url.hostname
        if url.port:
            host += f":{url.port}"
        query = [(key, val) for key, val in parse_qsl(url.query) if key in {"t", "id", "topic", "topic_id"}]
        return urlunsplit((url.scheme, host, url.path, urlencode(query), ""))
    except ValueError:
        return ""


def public_text(value):
    value = re.sub(r'https?://[^\s<>"\']+', lambda match: public_url(match[0]), value)
    return re.sub(
        r"(?i)\b(password|passwd|passkey|token|api[_-]?key|authorization|cookie|session[_-]?id)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )


def clean(value):
    if isinstance(value, str):
        return public_text(value)
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    return value


def build_report(candidate, requests, episodes, release, provider_name, description_source):
    files = [
        TorrentFile.model_validate({**file, "index": file.get("source_index", file["index"])})
        for file in release["files"]
        if not file.get("legacy")
    ]
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": {"version": current_engine().version, "identity": current_engine().identity},
        "release": {
            "url": public_url(candidate.url),
            "title": candidate.title,
            "provider": candidate.provider,
            "provider_name": provider_name,
            "external_id": candidate.id,
            "torrent_revision": release["revision"],
            "description_revision": candidate.revision,
            "description_source": description_source,
            "description_encoding": "base64-utf8",
            "description_base64": base64.b64encode(public_text(candidate.description).encode()).decode(),
            "evidence": [item.model_dump(mode="json") for item in candidate.evidence],
            "external_ids": candidate.external_ids,
            "size": candidate.size,
            "files": release["files"],
        },
        "task": {
            "media": requests[0].media.model_dump(
                mode="json",
                include={
                    "provider",
                    "id",
                    "kind",
                    "title",
                    "original_title",
                    "year",
                    "aliases",
                    "external_ids",
                    "episode_numbering",
                    "seasons",
                    "overview",
                    "original_language",
                },
            ),
            "requests": [item.model_dump(mode="json", exclude={"media"}) for item in requests],
            "saved_episodes": episodes,
        },
    }
    try:
        report["evaluation"] = (
            Matcher().evaluate(candidate, requests, files, release["revision"]).model_dump(mode="json")
        )
    except Exception:
        # The export must remain available when the matcher itself is broken.
        report["evaluation"] = None
        report["evaluation_status"] = "failed"
    return clean(report)


async def collect_report(ctx, candidate, requests, episodes, release):
    import asyncio

    source = "cache" if candidate.description else "unavailable"
    try:
        async with asyncio.timeout(45):
            async with ctx.plugins.open(
                candidate.provider, allow_disabled=True, bypass_cooldown=True
            ) as provider:
                detailed = await provider.inspect(candidate)
        if detailed.description:
            candidate, source = detailed, "live"
    except Exception:
        pass  # Network errors and logs are intentionally excluded from the export.
    cls = ctx.plugins.classes.get(candidate.provider)
    name = cls.manifest.name if cls else candidate.provider
    return build_report(candidate, requests, episodes, release, name, source)
