"""Derived playback resources. Originals are never modified by these routes."""

import asyncio
import hashlib
import json
import math
import re
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select, text

from lazarr.jellyfin_state import (
    bool_parameter,
    csv_parameter,
    filter_items,
    number_parameter,
    page,
    parameter,
)
from lazarr.models import Download, LibraryAsset, Media, MediaAsset

TICKS = 10_000_000
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "vtt", "mov_text", "text"}
FORMATS = {"srt": "srt", "vtt": "webvtt", "ass": "ass", "ssa": "ass", "sup": "sup"}
LOCKS = [threading.Lock() for _ in range(32)]
WORKERS = threading.BoundedSemaphore(2)
EXTRA_KINDS = {"trailer", "extra", "special", "behindthescenes", "deleted", "featurette", "intro", "theme"}


async def cached_async(ctx, paths, signature, extension, generate):
    def lookup():
        target = cache_target(ctx, paths, signature, extension)
        return target, target.is_file()

    # Warm subtitles/images must not wait behind a long trickplay render.
    target, hit = await asyncio.to_thread(lookup)
    if hit:
        return target
    return await ctx.background_tasks.run(
        str(signature[0]), cached, ctx, paths, signature, extension, generate, key=target.name
    )


async def probe_async(ctx, path):
    from lazarr.torrent import probe_file

    return await ctx.background_tasks.run("probe", probe_file, path, ctx.config.ffprobe, key=str(path))


def is_extra(part_key):
    return part_key.split(":")[0] in EXTRA_KINDS


async def discover_extras(ctx, media_id):
    """Index only explicitly named extras already downloaded in known torrents."""
    from lazarr.jellyfin import playable_path

    names = {
        "trailers": "trailer",
        "extras": "extra",
        "specials": "special",
        "behind the scenes": "behindthescenes",
        "deleted scenes": "deleted",
        "featurettes": "featurette",
        "intros": "intro",
        "backdrops": "theme",
    }
    with ctx.db.session() as db:
        downloads = list(
            db.scalars(
                select(Download)
                .join(MediaAsset, MediaAsset.download_id == Download.id)
                .join(LibraryAsset, LibraryAsset.asset_id == MediaAsset.id)
                .where(LibraryAsset.media_id == media_id)
            ).unique()
        )
    for download in downloads:
        progress = download.stats.get("files", [])
        for file in download.plan.get("files", []):
            index, relative, size = file.get("index"), file.get("path", ""), file.get("size", 0)
            if (
                not isinstance(index, int)
                or index < 0
                or index >= len(progress)
                or not size
                or progress[index] < size
            ):
                continue
            parsed = Path(relative)
            if parsed.suffix.lower() not in {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts"}:
                continue
            kind = next((names[p.casefold()] for p in parsed.parts[:-1] if p.casefold() in names), None)
            if not kind:
                match = re.search(
                    r"-(trailer|behindthescenes|deleted|featurette|intro|theme)$", parsed.stem, re.I
                )
                kind = match[1].casefold() if match else None
            if not kind:
                continue
            with ctx.db.session() as db:
                existing = db.scalar(
                    select(MediaAsset).where(
                        MediaAsset.download_id == download.id, MediaAsset.video_index == index
                    )
                )
                if existing:
                    continue  # Never reclassify an existing episode or primary asset.
            path = playable_path(download, relative)
            if not path or path.stat().st_size != size:
                continue
            probe = await probe_async(ctx, path)
            video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
            if not probe.get("ok") or not video:
                continue
            with ctx.db.session() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                if db.scalar(
                    select(MediaAsset.id).where(
                        MediaAsset.download_id == download.id, MediaAsset.video_index == index
                    )
                ):
                    continue
                asset = MediaAsset(
                    media_id=media_id,
                    download_id=download.id,
                    video_index=index,
                    path=relative,
                    probe=probe,
                    resolution=video.get("height"),
                    tracks=[],
                )
                db.add(asset)
                db.flush()
                db.add(
                    LibraryAsset(
                        media_id=media_id,
                        asset_id=asset.id,
                        part_key=f"{kind}:{index}",
                        verification={"complete": True},
                        preflight={},
                    )
                )


def ffmpeg(args, *, timeout=120):
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "2", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Media resource processor unavailable") from exc
    if result.returncode:
        raise HTTPException(422, "Unable to extract this media resource")


def cache_target(ctx, paths, signature, extension):
    stamps = [(str(p.resolve()), p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    digest = hashlib.sha256(json.dumps([stamps, signature, 2]).encode()).hexdigest()
    root = ctx.config.data_dir / "cache" / "jellyfin-resources"
    return root / (digest + "." + extension)


def cached(ctx, paths, signature, extension, generate):
    """Bound concurrency, serialize identical work, publish only complete files."""
    target = cache_target(ctx, paths, signature, extension)
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    with LOCKS[int(target.stem[:8], 16) % len(LOCKS)]:
        if target.is_file():
            return target
        with WORKERS, tempfile.TemporaryDirectory(prefix="work-", dir=root) as work:
            output = Path(work) / ("output." + extension)
            generate(output)
            if not output.is_file() or output.stat().st_size > 128 * 1024**2:
                raise HTTPException(422, "Invalid media resource")
            output.replace(target)
        # Best-effort age-based eviction; don't evict fresh/in-flight responses.
        files = [p for p in root.iterdir() if p.is_file()]
        total = sum(p.stat().st_size for p in files if p.exists())
        for path in sorted(files, key=lambda p: p.name):
            try:
                stat = path.stat()
                if path != target and (
                    time.time() - stat.st_mtime > 30 * 86400
                    or (total > 1024**3 and time.time() - stat.st_mtime > 3600)
                ):
                    path.unlink()
                    total -= stat.st_size
            except FileNotFoundError:
                pass
    return target


async def ensure_probe(ctx, playable):
    """Backfill chapters/attachments on access for assets probed by older versions."""
    from lazarr.jellyfin import playable_path

    asset = playable["asset"]
    updated = dict(asset.probe)
    if "chapters" not in updated:
        probe = await probe_async(ctx, playable["path"])
        if probe.get("ok"):
            updated = {**updated, **probe, "chapters": probe.get("chapters", [])}
    external = {}
    tracks = list((playable["link"].preflight.get("binding") or {}).get("tracks", [])) + list(
        asset.tracks or []
    )
    for track in tracks:
        relative = track.get("path")
        if (
            track.get("kind") != "audio"
            or not relative
            or not isinstance(relative, str)
            or not isinstance(playable["link"], LibraryAsset)
        ):
            continue
        path = playable_path(playable["download"], relative)
        if not path or not path.is_file():
            continue
        stamp = f"{path.stat().st_mtime_ns}:{path.stat().st_size}"
        previous = updated.get("external_audio", {}).get(str(path), {})
        if previous.get("stamp") == stamp:
            external[str(path)] = previous
            continue
        probe = await probe_async(ctx, path)
        raw = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
        if raw:
            external[str(path)] = {
                "stamp": stamp,
                "stream": raw,
                "language": track.get("language", "und"),
                "title": track.get("title"),
            }
    if external or "external_audio" in updated:
        updated["external_audio"] = external
    if updated != asset.probe:
        with ctx.db.session() as db:
            row = db.get(MediaAsset, asset.id)
            if row:
                row.probe = updated
                asset.probe = updated


async def remux_audio(ctx, playable, audio, request):
    """Stream-copy video plus a selected sidecar; no transcoding or giant temp files."""
    from lazarr.jellyfin import duration_ticks, playable_path

    path = playable_path(playable["download"], audio["Path"])
    if not path:
        raise HTTPException(404, "Audio file not found")
    if request.headers.get("range"):
        raise HTTPException(416, "Remux streams seek via startTimeTicks, not byte ranges")
    start = number_parameter(request.query_params, "startTimeTicks", 0)
    if start >= duration_ticks(playable["asset"]):
        raise HTTPException(400, "Invalid startTimeTicks")
    if request.method == "HEAD":
        return Response(media_type="video/x-matroska")
    slots = getattr(ctx, "jellyfin_remux_slots", None)
    if slots is None:
        slots = ctx.jellyfin_remux_slots = asyncio.Semaphore(2)
    try:
        await asyncio.wait_for(slots.acquire(), timeout=0.1)
    except TimeoutError as exc:
        raise HTTPException(429, "Too many remux streams") from exc
    process = None

    async def close():
        try:
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        except ProcessLookupError:
            pass
        finally:
            slots.release()

    try:
        args = ["ffmpeg", "-nostdin", "-v", "error"]
        for source in (playable["path"], path):
            args += ["-ss", str(start / TICKS), "-protocol_whitelist", "file,pipe", "-i", str(source)]
        args += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "0:s?",
            "-map",
            "0:t?",
            "-c",
            "copy",
            "-f",
            "matroska",
            "pipe:1",
        ]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        initial = await asyncio.wait_for(process.stdout.read(65536), 30)
        if not initial:
            raise HTTPException(422, "Unable to remux selected audio")
    except BaseException:
        await close()
        raise

    async def chunks():
        try:
            yield initial
            while chunk := await asyncio.wait_for(process.stdout.read(65536), 60):
                yield chunk
        finally:
            await close()

    return StreamingResponse(chunks(), media_type="video/x-matroska", headers={"Accept-Ranges": "none"})


def media_segments(playable, item_id, allowed=None):
    from lazarr.jellyfin import duration_ticks

    result = []
    labels = {
        "intro": "Intro",
        "opening": "Intro",
        "op": "Intro",
        "recap": "Recap",
        "outro": "Outro",
        "ending": "Outro",
        "ed": "Outro",
        "credits": "Outro",
        "commercial": "Commercial",
        "preview": "Preview",
    }
    duration = duration_ticks(playable["asset"])
    for chapter in playable["asset"].probe.get("chapters", []):
        kind = labels.get(chapter.get("tags", {}).get("title", "").strip().casefold())
        if not kind or (allowed and kind not in allowed):
            continue
        try:
            start, end = [round(float(chapter.get(k, 0)) * TICKS) for k in ("start_time", "end_time")]
        except (ValueError, TypeError, OverflowError):
            continue
        if not 0 <= start < end <= duration:
            continue
        result.append(
            {
                "Id": str(uuid.uuid5(uuid.UUID(item_id), f"segment:{kind}:{start}:{end}")),
                "ItemId": item_id,
                "Type": kind,
                "StartTicks": start,
                "EndTicks": end,
            }
        )
    return sorted(result, key=lambda r: r["StartTicks"])


def chapters(playable):
    result = []
    for raw in playable["asset"].probe.get("chapters", []):
        try:
            start = float(raw.get("start_time", 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(start) or start < 0:
            continue
        result.append(
            {
                "StartPositionTicks": round(start * TICKS),
                "ImageDateModified": datetime.fromtimestamp(
                    playable["path"].stat().st_mtime, timezone.utc
                ).isoformat(),
                "Name": raw.get("tags", {}).get("title") or f"Chapter {len(result) + 1}",
                "ImageTag": hashlib.sha256(
                    f"{playable['path'].stat().st_mtime_ns}:{start}".encode()
                ).hexdigest()[:16],
            }
        )
    return sorted(result, key=lambda c: c["StartPositionTicks"])


def attachments(playable, item_id, source_id, play_session_id=None):
    result = []
    for raw in playable["asset"].probe.get("streams", []):
        if raw.get("codec_type") != "attachment":
            continue
        tags = raw.get("tags", {})
        index = int(raw["index"])
        url = f"/Videos/{item_id}/{source_id}/Attachments/{index}"
        if play_session_id:
            url += "?" + urlencode({"playSessionId": play_session_id})
        result.append(
            {
                "Index": index,
                "Codec": raw.get("codec_name"),
                "FileName": Path(tags.get("filename", f"attachment-{index}")).name,
                "MimeType": tags.get("mimetype", "application/octet-stream"),
                "DeliveryUrl": url,
            }
        )
    return result


def trickplay_info(playable):
    from lazarr.jellyfin import duration_ticks

    if not isinstance(playable["link"], LibraryAsset):
        return None  # Sparse/incomplete torrents cannot provide arbitrary seeks.
    video = next(
        (
            s
            for s in playable["asset"].probe.get("streams", [])
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    duration = duration_ticks(playable["asset"]) / TICKS
    if not video or not video.get("width") or not video.get("height") or duration <= 0:
        return None
    height = min(640, max(2, round(320 * video["height"] / video["width"] / 2) * 2))
    return {
        "Width": 320,
        "Height": height,
        "TileWidth": 5,
        "TileHeight": 5,
        "ThumbnailCount": math.ceil(duration / 10),
        "Interval": 10000,
        "Bandwidth": 1_000_000,
    }


def resource_fields(playable, item_id):
    info = trickplay_info(playable)
    return {"Chapters": chapters(playable), "Trickplay": {item_id: {"320": info}} if info else {}}


async def chapter_image(ctx, playable, index):
    await ensure_probe(ctx, playable)
    entries = chapters(playable)
    if index < 0 or index >= len(entries):
        raise HTTPException(404, "Chapter not found")

    def generate(output):
        ffmpeg(
            [
                "-ss",
                str(entries[index]["StartPositionTicks"] / TICKS),
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(playable["path"]),
                "-map",
                "0:v:0",
                "-vf",
                "scale=640:-2",
                "-frames:v",
                "1",
                str(output),
            ],
            timeout=30,
        )

    return await cached_async(ctx, [playable["path"]], ["chapter", index, entries[index]], "jpg", generate)


async def resize_image(ctx, path, params):
    sizes = {name: number_parameter(params, name) for name in ("width", "height", "maxWidth", "maxHeight")}
    if any(v is not None and not 1 <= v <= 4096 for v in sizes.values()):
        raise HTTPException(400, "Image dimensions must be between 1 and 4096")
    fmt = parameter(params, "format", "jpg").lower()
    if fmt not in {"jpg", "jpeg", "png", "webp"}:
        raise HTTPException(400, "Unsupported image format")
    quality = number_parameter(params, "quality", 90)
    if not 1 <= quality <= 100:
        raise HTTPException(400, "Invalid image quality")
    if (
        not any(sizes.values())
        and parameter(params, "format") is None
        and parameter(params, "quality") is None
    ):
        return path

    def generate(output):
        from PIL import Image, UnidentifiedImageError

        try:
            with Image.open(path) as image:
                image = image.convert("RGB")
                width, height = sizes["width"], sizes["height"]
                if width or height:
                    width = width or max(1, round(image.width * height / image.height))
                    height = height or max(1, round(image.height * width / image.width))
                    if width > 4096 or height > 4096:
                        raise HTTPException(400, "Image dimensions exceed limit")
                    image = image.resize((width, height), Image.Resampling.LANCZOS)
                image.thumbnail((sizes["maxWidth"] or 4096, sizes["maxHeight"] or 4096))
                image.save(output, "JPEG" if fmt in {"jpg", "jpeg"} else fmt.upper(), quality=quality)
        except (OSError, UnidentifiedImageError) as exc:
            raise HTTPException(422, "Invalid source image") from exc

    return await cached_async(ctx, [path], ["image", sizes, fmt, quality], fmt, generate)


def auth_query(request, **extra):
    from lazarr.jellyfin import token_from

    token = token_from(request)
    session = parameter(request.query_params, "playSessionId")
    auth = {"api_key": token} if token else {"playSessionId": session} if session else {}
    return urlencode({**auth, **extra})


def clip_subtitles(content, fmt, start, end, copy):
    """Clip intersecting cues, including a cue spanning an HLS segment boundary."""

    def ticks(value):
        parts = value.replace(",", ".").split(":")
        return round(sum(float(part) * 60**index for index, part in enumerate(reversed(parts))) * TICKS)

    def stamp(value):
        scale = 100 if fmt in {"ass", "ssa"} else 1000
        total = round(value / TICKS * scale)
        seconds, fraction = divmod(total, scale)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if scale == 100:
            return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"
        return f"{hours:02}:{minutes:02}:{seconds:02}{',' if fmt == 'srt' else '.'}{fraction:03}"

    def interval(left, right):
        left, right = ticks(left), ticks(right)
        if right <= start or (end is not None and left >= end):
            return None
        left, right = max(left, start), min(right, end) if end is not None else right
        offset = 0 if copy else start
        return stamp(left - offset), stamp(right - offset)

    if fmt in {"ass", "ssa"}:
        lines = []
        for line in content.splitlines():
            if line.startswith("Dialogue:"):
                parts = line.split(",", 9)
                if len(parts) != 10:
                    continue
                bounds = interval(parts[1], parts[2])
                if bounds is None:
                    continue
                parts[1:3] = bounds
                line = ",".join(parts)
            lines.append(line)
        return "\n".join(lines) + "\n"
    pattern = re.compile(r"(?P<a>(?:\d+:)?\d{2}:\d{2}[.,]\d+) --> (?P<b>(?:\d+:)?\d{2}:\d{2}[.,]\d+)")
    blocks = []
    for block in re.split(r"\n\s*\n", content.replace("\r\n", "\n")):
        match = pattern.search(block)
        if match:
            bounds = interval(match["a"], match["b"])
            if bounds is None:
                continue
            block = block[: match.start()] + " --> ".join(bounds) + block[match.end() :]
        if block.strip():
            blocks.append(block)
    return "\n\n".join(blocks) + "\n"


async def subtitle(ctx, playable, item_id, source_id, index, fmt, request, start=0):
    from lazarr.jellyfin import media_source

    if fmt not in FORMATS:
        raise HTTPException(415, "Unsupported subtitle format")
    start = number_parameter(request.query_params, "startPositionTicks", start)
    end = number_parameter(request.query_params, "endPositionTicks")
    if start < 0 or (end is not None and end <= start):
        raise HTTPException(400, "Invalid subtitle interval")
    streams = media_source(ctx, playable, item_id, source_id=source_id)["MediaStreams"]
    stream = next((s for s in streams if s["Index"] == index and s["Type"] == "Subtitle"), None)
    if stream is None:
        raise HTTPException(404, "Subtitle not found")
    bitmap = stream["Codec"] not in TEXT_CODECS
    if bitmap and not (stream["Codec"] == "hdmv_pgs_subtitle" and fmt == "sup"):
        raise HTTPException(415, "Bitmap subtitles cannot be converted to text without OCR")
    if not bitmap and fmt == "sup":
        raise HTTPException(415, "Text to bitmap conversion is not supported")
    if bitmap and (start or end is not None):
        raise HTTPException(415, "Seeking extracted bitmap subtitles is not supported")
    path = Path(stream["Path"]) if stream["IsExternal"] else playable["path"]
    external_count = sum(s["IsExternal"] and s["Type"] == "Subtitle" for s in streams)
    raw_index = index - external_count
    copy = bool_parameter(request.query_params, "copyTimestamps")
    time_map = bool_parameter(request.query_params, "addVttTimeMap")

    def generate(output):
        source = path
        # Normalize old Windows sidecars before asking ffmpeg to parse them.
        if stream["IsExternal"] and not bitmap:
            raw = source.read_bytes()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = raw.decode("cp1251", errors="replace")
            source = output.parent / ("input" + path.suffix)
            source.write_text(content, encoding="utf-8")
            if fmt == path.suffix.lstrip(".").lower():
                output.write_text(content, encoding="utf-8")
                return
        args = [
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(source),
            "-map",
            "0:0" if stream["IsExternal"] else f"0:{raw_index}",
        ]
        args += [
            "-c:s",
            "copy" if bitmap else {"srt": "srt", "vtt": "webvtt", "ass": "ass", "ssa": "ass"}[fmt],
            "-f",
            FORMATS[fmt],
            str(output),
        ]
        ffmpeg(args)

    base = await cached_async(ctx, [path], ["subtitle", raw_index, fmt], fmt, generate)

    def transform(output):
        output.write_bytes(base.read_bytes())
        if not bitmap and (start or end is not None):
            output.write_text(
                clip_subtitles(output.read_text(encoding="utf-8"), fmt, start, end, copy), encoding="utf-8"
            )
        if fmt == "vtt" and time_map:
            content = output.read_text(encoding="utf-8")
            # MPEGTS corresponds to local zero; copied cues already use source time.
            mapping = 0 if copy else round(start / TICKS * 90000)
            output.write_text(
                content.replace("WEBVTT", f"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:{mapping}", 1),
                encoding="utf-8",
            )

    output = base
    if start or end is not None or time_map:
        output = await cached_async(
            ctx, [base], ["subtitle-interval", start, end, copy, time_map], fmt, transform
        )
    return FileResponse(
        output,
        media_type={"vtt": "text/vtt", "sup": "application/octet-stream"}.get(
            fmt, "text/plain; charset=utf-8"
        ),
    )


def install(app, context, authenticated, check_user, authorize, playback):
    from lazarr.jellyfin import duration_ticks, episode_dto, item_dto, object_id, query_result

    @app.get("/Videos/{item_id}/{source_id}/Subtitles/{index}/subtitles.m3u8")
    async def subtitle_playlist(item_id: str, source_id: str, index: int, request: Request):
        authorize(item_id, request)
        playable = playback(context(request), item_id, source_id)
        from lazarr.jellyfin import media_source

        streams = media_source(context(request), playable, item_id, source_id=source_id)["MediaStreams"]
        if not any(
            s["Index"] == index and s["Type"] == "Subtitle" and s["Codec"] in TEXT_CODECS for s in streams
        ):
            raise HTTPException(404, "Text subtitle not found")
        length = number_parameter(request.query_params, "segmentLength", 30)
        if not 1 <= length <= 3600:
            raise HTTPException(400, "Invalid segmentLength")
        duration = duration_ticks(playable["asset"]) / TICKS
        if math.ceil(duration / length) > 20000:
            raise HTTPException(400, "Too many subtitle segments")
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{length}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        for start in range(0, math.ceil(duration), length):
            end = min(start + length, duration)
            query = auth_query(
                request, endPositionTicks=round(end * TICKS), copyTimestamps="true", addVttTimeMap="true"
            )
            lines += [
                f"#EXTINF:{end - start:.3f},",
                f"/Videos/{item_id}/{source_id}/Subtitles/{index}/{start * TICKS}/Stream.vtt?{query}",
            ]
        return Response("\n".join(lines + ["#EXT-X-ENDLIST", ""]), media_type="application/vnd.apple.mpegurl")

    @app.get("/Videos/{item_id}/{source_id}/Attachments/{index}")
    async def attachment(item_id: str, source_id: str, index: int, request: Request):
        authorize(item_id, request)
        ctx = context(request)
        playable = playback(ctx, item_id, source_id)
        await ensure_probe(ctx, playable)
        entry = next((a for a in attachments(playable, item_id, source_id) if a["Index"] == index), None)
        if entry is None:
            raise HTTPException(404, "Attachment not found")

        def generate(output):
            ffmpeg(
                [
                    f"-dump_attachment:{index}",
                    str(output),
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(playable["path"]),
                    "-t",
                    "0",
                    "-f",
                    "null",
                    "-",
                ]
            )

        output = await cached_async(ctx, [playable["path"]], ["attachment", index], "bin", generate)
        return FileResponse(
            output,
            media_type="application/octet-stream",
            filename=entry["FileName"],
            headers={"X-Content-Type-Options": "nosniff"},
        )

    def fonts(ctx):
        root = ctx.config.data_dir / "fonts"
        if not root.is_dir():
            return []
        return [
            p
            for p in root.iterdir()
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in {".ttf", ".otf", ".woff", ".woff2"}
        ]

    @app.get("/FallbackFont/Fonts")
    async def font_list(request: Request, user=Depends(authenticated)):
        return [
            {
                "Name": p.name,
                "Size": p.stat().st_size,
                "DateCreated": datetime.fromtimestamp(p.stat().st_ctime, timezone.utc).isoformat(),
                "DateModified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
            }
            for p in fonts(context(request))
        ]

    @app.get("/FallbackFont/Fonts/{name}")
    async def font(name: str, request: Request, user=Depends(authenticated)):
        path = next((p for p in fonts(context(request)) if p.name == name), None)
        if path is None:
            raise HTTPException(404, "Font not found")
        return FileResponse(path, media_type="application/octet-stream")

    def trickplay(request, item_id, width):
        authorize(item_id, request)
        playable = playback(context(request), item_id, parameter(request.query_params, "mediaSourceId"))
        info = trickplay_info(playable)
        if width != 320 or info is None:
            raise HTTPException(404, "Trickplay unavailable")
        return playable, info

    @app.get("/Videos/{item_id}/Trickplay/{width}/tiles.m3u8")
    async def tiles(item_id: str, width: int, request: Request):
        playable, info = trickplay(request, item_id, width)
        duration = duration_ticks(playable["asset"]) / TICKS
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-TARGETDURATION:250",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-IMAGES-ONLY",
        ]
        for index in range(math.ceil(info["ThumbnailCount"] / 25)):
            query = auth_query(
                request, mediaSourceId=parameter(request.query_params, "mediaSourceId", item_id)
            )
            lines += [
                f"#EXTINF:{min(250, duration - index * 250):.3f},",
                f"#EXT-X-TILES:RESOLUTION=320x{info['Height']},LAYOUT=5x5,DURATION=10.000",
                f"/Videos/{item_id}/Trickplay/320/{index}.jpg?{query}",
            ]
        return Response("\n".join(lines + ["#EXT-X-ENDLIST", ""]), media_type="application/vnd.apple.mpegurl")

    @app.get("/Videos/{item_id}/Trickplay/{width}/{index}.jpg")
    async def tile(item_id: str, width: int, index: int, request: Request):
        playable, info = trickplay(request, item_id, width)
        if index < 0 or index * 25 >= info["ThumbnailCount"]:
            raise HTTPException(404, "Trickplay tile not found")
        count = min(25, info["ThumbnailCount"] - index * 25)

        def generate(output):
            # Sparse seeks avoid decoding 250 seconds of 4K video for each sheet.
            from PIL import Image

            sheet = Image.new("RGB", (320 * 5, info["Height"] * 5))
            for offset in range(count):
                frame = output.parent / f"{offset}.jpg"
                ffmpeg(
                    [
                        "-ss",
                        str(index * 250 + offset * 10),
                        "-protocol_whitelist",
                        "file,pipe",
                        "-i",
                        str(playable["path"]),
                        "-map",
                        "0:v:0",
                        "-vf",
                        f"scale=320:{info['Height']}",
                        "-frames:v",
                        "1",
                        str(frame),
                    ],
                    timeout=30,
                )
                with Image.open(frame) as image:
                    sheet.paste(image, (offset % 5 * 320, offset // 5 * info["Height"]))
            sheet.save(output, "JPEG", quality=80)

        output = await cached_async(
            context(request), [playable["path"]], ["trickplay", index, info], "jpg", generate
        )
        return FileResponse(output, media_type="image/jpeg")

    @app.get("/MediaSegments/{item_id}")
    async def segments(item_id: str, request: Request, user=Depends(authenticated)):
        check_user(request, user)
        playable = playback(context(request), item_id)
        await ensure_probe(context(request), playable)
        return query_result(
            media_segments(playable, item_id, csv_parameter(request.query_params, "includeSegmentTypes"))
        )

    @app.get("/Shows/Upcoming")
    async def upcoming(request: Request, user=Depends(authenticated)):
        check_user(request, user)
        ctx = context(request)
        from lazarr.jellyfin import library_ids
        from lazarr.library import library_kind

        with ctx.db.session() as db:
            rows = list(db.scalars(select(Media).where(Media.kind == "tv")))
        today = datetime.now(timezone.utc).date().isoformat()
        parent = parameter(request.query_params, "parentId")
        items = []
        for media in rows:
            if parent and parent not in {object_id("media", media.id), library_ids(ctx)[library_kind(media)]}:
                continue
            for episode in ctx.library.detail(media.id)["episodes"]:
                if episode.get("air_date") and episode["air_date"] >= today:
                    items.append(episode_dto(ctx, media, episode, user, include_missing=True))
        options = dict(request.query_params)
        options.setdefault("sortBy", "PremiereDate,SortName")
        return page(filter_items(items, options), options)

    def extra_items(ctx, item_id, user, prefixes, inherit=False):
        from lazarr.jellyfin import media_identity_for_item, parse_object_id

        item_dto(ctx, item_id, user)
        media_id = media_identity_for_item(ctx, item_id)
        kind, identity, _ = parse_object_id(item_id)
        episode_id = identity if kind == "episode" else None
        if kind == "asset":
            episode_id = getattr(playback(ctx, item_id)["link"], "episode_id", None)
        with ctx.db.session() as db:
            rows = list(
                db.scalars(
                    select(LibraryAsset)
                    .where(LibraryAsset.media_id == media_id)
                    .order_by(LibraryAsset.part_key, LibraryAsset.id)
                )
            )
        items, seen = [], set()
        for row in rows:
            if row.episode_id != episode_id and not (inherit and row.episode_id is None):
                continue
            if row.part_key.split(":")[0] not in prefixes or row.asset_id in seen:
                continue
            try:
                item = item_dto(ctx, object_id("asset", row.asset_id), user)
            except HTTPException:
                continue
            item["Name"] = Path(item["Path"]).stem
            item["Type"] = "Trailer" if row.part_key.startswith("trailer:") else "Video"
            items.append(item)
            seen.add(row.asset_id)
        return items

    def extras_handler(kind):
        async def handler(item_id: str, request: Request, user=Depends(authenticated)):
            check_user(request, user)
            from lazarr.jellyfin import media_identity_for_item

            item_dto(context(request), item_id, user)
            await discover_extras(context(request), media_identity_for_item(context(request), item_id))
            prefixes = {
                "LocalTrailers": {"trailer"},
                "SpecialFeatures": {"extra", "special", "behindthescenes", "deleted", "featurette"},
                "Intros": {"intro"},
                "ThemeVideos": {"theme"},
                "AdditionalParts": {"part"},
            }[kind]
            items = extra_items(
                context(request),
                item_id,
                user,
                prefixes,
                inherit=kind == "ThemeVideos" and bool_parameter(request.query_params, "inheritFromParent"),
            )
            if kind == "ThemeVideos":
                return {**page(items, request.query_params), "OwnerId": item_id}
            return page(items, request.query_params) if kind in {"Intros", "AdditionalParts"} else items

        return handler

    for kind in ("LocalTrailers", "SpecialFeatures", "Intros", "ThemeVideos", "AdditionalParts"):
        app.add_api_route(
            ("/Videos" if kind == "AdditionalParts" else "/Items") + "/{item_id}/" + kind,
            extras_handler(kind),
            methods=["GET"],
            name="jellyfin_" + kind,
        )

    @app.get("/Items/{item_id}/ThemeMedia")
    async def theme_media(item_id: str, request: Request, user=Depends(authenticated)):
        check_user(request, user)
        from lazarr.jellyfin import media_identity_for_item

        item_dto(context(request), item_id, user)
        await discover_extras(context(request), media_identity_for_item(context(request), item_id))
        videos = extra_items(
            context(request),
            item_id,
            user,
            {"theme"},
            inherit=bool_parameter(request.query_params, "inheritFromParent"),
        )
        return {
            "ThemeVideosResult": {**page(videos, request.query_params), "OwnerId": item_id},
            "ThemeSongsResult": {**query_result([]), "OwnerId": item_id},
            "SoundtrackSongsResult": {**query_result([]), "OwnerId": item_id},
        }
