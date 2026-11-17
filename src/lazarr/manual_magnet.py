"""Validate user-supplied magnets without requiring a tracker provider."""

import base64
import re
from urllib.parse import parse_qs, urlparse

from lazarr.sdk import Candidate


def magnet_candidate(value):
    parsed = urlparse(value)
    if parsed.scheme != "magnet" or parsed.netloc or parsed.path or parsed.fragment:
        raise ValueError("Укажите корректную magnet-ссылку")
    query = parse_qs(parsed.query)
    hashes = {}
    for topic in query.get("xt", []):
        match = re.fullmatch(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", topic, re.I)
        v2 = re.fullmatch(r"urn:btmh:1220([a-fA-F0-9]{64})", topic, re.I)
        if match:
            raw = match[1]
            identity = raw.lower() if len(raw) == 40 else base64.b32decode(raw.upper()).hex()
            kind = "btih"
        elif v2:
            identity, kind = "1220" + v2[1].lower(), "btmh"
        else:
            continue
        if kind in hashes and hashes[kind] != identity:
            raise ValueError("Magnet-ссылка содержит противоречивые хеши")
        hashes[kind] = identity
    if not hashes:
        raise ValueError("Magnet-ссылка должна содержать корректный xt=urn:btih: или xt=urn:btmh:")
    identity = hashes.get("btih") or hashes["btmh"]
    # Keep tracker parameters intact; normalize only the URI scheme for libtorrent.
    uri = "magnet:" + value.split(":", 1)[1]
    return Candidate(
        provider="magnet",
        id=identity,
        url=uri,
        magnet=uri,
        title=(query.get("dn", [""])[0].strip() or f"Magnet {identity[:12]}"),
    )
