"""Portable NFO and artwork alongside published media; never edit torrent files."""

from pathlib import Path
import re
from urllib.parse import urlparse
from xml.etree import ElementTree as ET


def field(parent, name, value, **attributes):
    if value is not None and value != "":
        node = ET.SubElement(parent, name, attributes)
        # XML 1.0 does not allow control characters, even as character references.
        node.text = re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value))
        return node


def identifiers(node, provider, identity, external=None):
    values = {**(external or {}), provider: identity}
    for name, value in sorted(values.items()):
        if value:
            field(node, "uniqueid", value, type=name, default="true" if name == provider else "false")


def ratings(node, data, provider):
    if data.get("community_rating") is not None:
        container = ET.SubElement(node, "ratings")
        rating = ET.SubElement(container, "rating", name=provider, max="10", default="true")
        field(rating, "value", data["community_rating"])
        field(rating, "votes", data.get("vote_count"))


def people(node, entries):
    for person in entries:
        kind, name = person.get("Type"), person.get("Name")
        if kind == "Actor" and name:
            actor = ET.SubElement(node, "actor")
            field(actor, "name", name)
            field(actor, "role", person.get("Role"))
            field(actor, "thumb", person.get("ImageUrl"))
            field(actor, "order", person.get("SortOrder"))
        elif kind in {"Director", "Writer"} and name:
            field(node, kind.lower(), name)


def media_nfo(media):
    data = media.metadata_json
    node = ET.Element("movie" if media.kind == "movie" else "tvshow")
    for name, value in {
        "title": media.title,
        "originaltitle": data.get("original_title"),
        "year": media.year,
        "plot": data.get("overview"),
        "premiered": data.get("release_date"),
        "mpaa": data.get("official_rating"),
        "status": data.get("status"),
        "tagline": data.get("tagline"),
        "runtime": data.get("runtime"),
        "enddate": data.get("end_date"),
    }.items():
        field(node, name, value)
    identifiers(node, media.provider, media.external_id, data.get("external_ids"))
    for name, key in (
        ("genre", "genres"),
        ("country", "origin_countries"),
        ("studio", "studios"),
        ("tag", "tags"),
    ):
        for value in data.get(key, []):
            field(node, name, value)
    ratings(node, data, media.provider)
    people(node, data.get("people", []))
    for trailer in data.get("remote_trailers", []):
        match = re.fullmatch(r"https://www\.youtube\.com/watch\?v=([\w-]+)", trailer.get("Url", ""))
        if match:
            field(node, "trailer", "plugin://plugin.video.youtube/?action=play_video&videoid=" + match[1])
    if media.kind == "movie" and data.get("collection"):
        collection = ET.SubElement(node, "set")
        field(collection, "name", data["collection"])
        field(node, "collectionnumber", data.get("collection_id"))
    return node


def sidecars(media, episode, season, number, directory, stem, root, special_position=None):
    result = []

    def nfo(path, node):
        ET.indent(node)
        content = ET.tostring(node, encoding="utf-8", xml_declaration=True).decode() + "\n"
        result.append({"path": str(path), "root": str(root), "content": content})

    def artwork(path, url):
        if not url:
            return
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in {".jpg", ".png", ".webp"}:
            result.append({"path": str(path) + suffix, "root": str(root), "image": url})

    show_dir = directory.parent if episode else directory
    nfo(show_dir / ("tvshow.nfo" if episode else "movie.nfo"), media_nfo(media))
    artwork(show_dir / "poster", media.metadata_json.get("poster"))
    artwork(show_dir / "fanart", media.metadata_json.get("backdrop"))
    if episode:
        # Only use a provider season title when its numbering matches the exported season.
        matching = season.number == number["season"]
        info = (
            next((s for s in media.metadata_json.get("seasons", []) if s["number"] == season.number), {})
            if matching
            else {}
        )
        if matching:
            # Fresh season details supplement the summary returned with the series.
            details = getattr(season, "metadata_json", None) or {}
            info = {
                **info,
                **{key: value for key, value in details.items() if value is not None and value != ""},
            }
        node = ET.Element("season")
        field(
            node,
            "title",
            (season.title or info.get("title")) if matching else f"Season {number['season']:02d}",
        )
        field(node, "seasonnumber", number["season"])
        field(node, "premiered", info.get("air_date"))
        field(node, "plot", info.get("overview"))
        identifiers(node, media.provider, info.get("id"))
        ratings(node, info, media.provider)
        people(node, info.get("people", []))
        nfo(directory / "season.nfo", node)
        artwork(directory / "poster", info.get("poster"))
        node = ET.Element("episodedetails")
        details = getattr(episode, "metadata_json", None) or {}
        for name, value in {
            "title": episode.title,
            "showtitle": media.title,
            "season": number["season"],
            "episode": number["episode"],
            "plot": episode.overview,
            "aired": episode.air_date,
            "runtime": details.get("runtime"),
        }.items():
            field(node, name, value)
        if number["season"] == 0:
            for name, value in (special_position or {}).items():
                field(node, name, value)
        identifiers(node, media.provider, episode.external_id)
        ratings(node, details, media.provider)
        people(node, details.get("people", []))
        nfo(directory / (stem + ".nfo"), node)
        artwork(directory / (stem + "-thumb"), episode.still)
    return result


async def sync_artwork(ctx):
    """Download outside the storage lock, then recheck ownership before publishing."""
    import asyncio
    from lazarr.models import ConfigEntry
    from lazarr.posters import fetch_poster
    from lazarr.storage import MANIFEST, _lock, write_sidecar, set_status

    with ctx.db.session() as db:
        row = db.get(ConfigEntry, MANIFEST)
        entries = [e for e in row.value.get("entries", []) if "image" in e] if row else []
    errors = []
    fetched = {}
    for entry in entries:
        try:
            url = entry["image"]
            if url not in fetched:
                parsed = urlparse(url)
                # The built-in metadata source is TMDB. Use its configured image mirror and cache.
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "image.tmdb.org"
                    or not parsed.path.startswith("/t/p/")
                ):
                    raise ValueError("Источник изображения не поддерживается")
                path = await fetch_poster(ctx, parsed.path.rsplit("/", 1)[-1], size="w1280")
                fetched[url] = path

            def publish():
                with _lock:
                    with ctx.db.session() as db:
                        current = db.get(ConfigEntry, MANIFEST)
                        if not current or entry not in current.value.get("entries", []):
                            return
                    write_sidecar(entry, fetched[url].read_bytes())

            await asyncio.to_thread(publish)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Keep existing artwork and retry on the next cycle. No secrets/remote URLs in status.
            errors.append(f"Не удалось сохранить изображение: {Path(entry['path']).name}")
    await asyncio.to_thread(set_status, ctx.db, "metadata_errors", errors[:20])
