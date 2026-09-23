"""Periodic preparation of persisted media metadata, never invoked by a GET."""

import asyncio
import logging
from sqlalchemy import select

from lazarr.models import MediaAsset, Download, LibraryAsset, SubtaskAsset, Season
from lazarr.languages import language
from lazarr.subtitle_language import analysis_key, detect_subtitle_language, TEXT_CODECS, TEXT_SUFFIXES


log = logging.getLogger(__name__)


def subtitle_candidates(ctx):
    def unknown(tracks):
        return any(
            track.get("kind", "subtitle") == "subtitle"
            and track.get("path")
            and language(track.get("language")) == "und"
            for track in tracks
        )

    ids = set()
    with ctx.db.session() as db:
        for asset_id, probe, tracks in db.execute(select(MediaAsset.id, MediaAsset.probe, MediaAsset.tracks)):
            if unknown(tracks or []) or any(
                stream.get("codec_type") == "subtitle"
                and stream.get("codec_name") in TEXT_CODECS
                and language(stream.get("tags", {}).get("language")) == "und"
                and not stream.get("detected_language")
                for stream in probe.get("streams", [])
            ):
                ids.add(asset_id)
        for model in (LibraryAsset, SubtaskAsset):
            for asset_id, preflight in db.execute(select(model.asset_id, model.preflight)):
                if unknown((preflight.get("binding") or {}).get("tracks", [])):
                    ids.add(asset_id)
    return sorted(ids)


def prepare_subtitles(ctx, asset_id):
    from lazarr.jellyfin import playable_path

    with ctx.db.session() as db:
        asset = db.get(MediaAsset, asset_id)
        if not asset:
            return
        download = db.get(Download, asset.download_id)
        if not download:
            return
        tracks = list(asset.tracks or [])
        for model in (LibraryAsset, SubtaskAsset):
            for link in db.scalars(select(model).where(model.asset_id == asset_id)):
                tracks.extend((link.preflight.get("binding") or {}).get("tracks", []))
    previous = asset.probe.get("subtitle_analysis") or {}
    candidates = []
    for stream in asset.probe.get("streams", []):
        if (
            stream.get("codec_type") == "subtitle"
            and stream.get("codec_name") in TEXT_CODECS
            and language(stream.get("tags", {}).get("language")) == "und"
            and not stream.get("detected_language")
        ):
            candidates.append((asset.path, stream.get("index"), stream.get("codec_name")))
    for track in tracks:
        if (
            track.get("kind", "subtitle") == "subtitle"
            and language(track.get("language")) == "und"
            and track.get("path")
        ):
            candidates.append((track["path"], None, None))
    results = {}
    for relative, index, codec in dict.fromkeys(candidates):
        path = playable_path(download, relative)
        if not path or (index is None and path.suffix.lower() not in TEXT_SUFFIXES):
            continue
        key = analysis_key(path, index, codec)
        if key:
            value = previous[key] if key in previous else detect_subtitle_language(path, index, codec)
            if analysis_key(path, index, codec) == key:
                results[key] = value
    if results != previous:
        with ctx.db.session() as db:
            row = db.get(MediaAsset, asset_id)
            if row:
                # Preserve concurrent probe/verification updates.
                row.probe = {**row.probe, "subtitle_analysis": results}


class MediaPreparation:
    def __init__(self, ctx):
        self.ctx = ctx
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(self._metadata_loop()), asyncio.create_task(self._subtitle_loop())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _metadata_loop(self):
        while True:
            try:
                with self.ctx.background_tasks.observe("metadata-refresh"):
                    await self.ctx.library.enrich(background=True)
                with self.ctx.db.session() as db:
                    media_ids = list(
                        db.scalars(select(Season.media_id).where(Season.refreshed_at == 0).distinct())
                    )
                for identity in media_ids:
                    with self.ctx.background_tasks.observe("metadata-refresh"):
                        await self.ctx.library.enrich_media(identity, background=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Media preparation failed; retrying next cycle")
            await asyncio.sleep(60)

    async def _subtitle_loop(self):
        while True:
            try:
                asset_ids = await asyncio.to_thread(subtitle_candidates, self.ctx)
                for identity in asset_ids:
                    await self.ctx.background_tasks.run(
                        "subtitle-analysis", prepare_subtitles, self.ctx, identity, key=str(identity)
                    )
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Subtitle preparation failed; retrying next cycle")
            await asyncio.sleep(60)
