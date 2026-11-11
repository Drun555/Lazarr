import asyncio
import binascii
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import lzma
import shutil
import sqlite3
import struct
import time
from threading import Event
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import httpx
import pytest
from sqlalchemy import select

from lazarr.config import Requirements
from lazarr.models import Release, SubtaskAsset
from lazarr.providers import rutracker_offline as offline
from lazarr.sdk import DownloadSource, ProviderError, SearchQuery
from lazarr.services import CreateTask
from test_worker import worker_setup as worker_setup


def topic(identity=42, title="Example Show (2020) 1080p", infohash=None):
    infohash = infohash or f"{identity:040x}"
    return f'''<torrent id="{identity}" registred_at="2024.01.02 03:04:05" size="123456789">
    <title>{escape(title)}</title><torrent hash="{infohash}" tracker_id="1"/>
    <forum id="9">Зарубежные сериалы (HD Video)</forum>
    <content><![CDATA[[b]Год выпуска[/b]: 2020
[b]Аудио[/b]: Русский, Японский
[b]Субтитры[/b]: Русские
[spoiler="MediaInfo"][pre]General
Complete name : Show/Show.S01E01.1080p.mkv
Video
Language : English
Audio
Language : Russian
[/pre][/spoiler]
[url=https://imdb.com/title/tt0042]IMDb[/url]
[img]https://example.invalid/image.jpg[/img]
Название &amp; описание [2020] [1080p]
]]></content>
    <dir name="Show"><file name="Show.S01E01.1080p.mkv" size="123456789"/>
    <dir name="Subs"><file name="Show.S01E01.ru.srt" size="100"/></dir></dir>
    </torrent>'''


def write_archive(directory, entries=None, name="rutracker-20260927.xml.xz", *, concatenated=False):
    path = directory / name
    data = (
        '<?xml version="1.0" encoding="UTF-8"?><torrents>'
        + "".join(entries if entries is not None else [topic()])
        + "</torrents>"
    ).encode()
    if name.endswith(".xz"):
        if concatenated:
            split = len(data) // 2
            data = lzma.compress(data[:split]) + lzma.compress(data[split:])
        else:
            data = lzma.compress(data)
    path.write_bytes(data)
    return path


@pytest.fixture
def catalog(core):
    manager = core[2]
    manager.configure("rutracker", {}, True)
    manager.transport = httpx.MockTransport(lambda request: pytest.fail("Local dump must not request HTTP"))
    directory = manager.offline_directories("rutracker")[0]
    return directory


@pytest.mark.parametrize("extension", [".xml", ".xml.xz"])
async def test_dump_search_inspect_and_magnet_without_auth(core, media, catalog, extension):
    archive = write_archive(catalog, name="rutracker-20260927" + extension, concatenated=True)
    async with core[2].open("rutracker", offline=True) as provider:
        status = await provider.offline_status()
        assert status.available and status.year_to == 2026
        assert not offline.index_path(archive).exists()  # Status need not unpack a dated snapshot.
        page = await provider.search(
            SearchQuery(requirements=Requirements(), media=media, text="example show 2020"), offline=True
        )
        assert len(page.items) == 1 and page.next_cursor is None
        item = page.items[0]
        assert item.offline and item.id == "42" and item.seeds is None
        assert item.revision == f"{42:040x}" and item.size == 123456789
        detailed = await provider.inspect(item, offline=True)
        assert detailed.external_ids == {"imdb": "tt0042"}
        assert (
            "[b]" not in detailed.description and "Название & описание [2020] [1080p]" in detailed.description
        )
        assert {"MediaInfo", "General", "Audio"} <= set(detailed.description.splitlines())
        assert "example.invalid/image" not in detailed.description
        assert detailed.file_hints == ["Show/Show.S01E01.1080p.mkv", "Show/Subs/Show.S01E01.ru.srt"]
        audio = [e for e in detailed.evidence if e.field == "audio_languages"]
        assert any("ru" in e.value for e in audio)
        assert not any("en" in e.value for e in audio)
        source = await provider.resolve_download(detailed, offline=True)
        assert source.torrent is None
        assert parse_qs(urlparse(source.magnet).query)["xt"] == [f"urn:btih:{42:040x}"]


async def test_unicode_search_query_escaping_and_pagination(core, media, catalog, monkeypatch):
    monkeypatch.setattr(offline, "PAGE_SIZE", 2)
    write_archive(
        catalog, [topic(i, "Ёжик в тумане / Hedgehog in the Fog [1975] 1080p") for i in range(1, 6)]
    )
    async with core[2].open("rutracker", offline=True) as provider:
        query = SearchQuery(requirements=Requirements(), media=media, text='"ЕЖИК" в тумане (1975)')
        first = await provider.search(query, offline=True)
        second = await provider.search(query, first.next_cursor, offline=True)
        third = await provider.search(query, second.next_cursor, offline=True)
        assert [i.id for page in [first, second, third] for i in page.items] == ["1", "2", "3", "4", "5"]
        assert third.next_cursor is None
        query.text = "Hedgehog in the Fog"
        assert (await provider.search(query, offline=True)).items
        query.text = '" OR *; --'
        assert not (await provider.search(query, offline=True)).items
        query.text = "***"
        assert not (await provider.search(query, offline=True)).items


def test_index_reuse_source_replacement_and_corrupt_cache_rebuild(tmp_path, monkeypatch):
    archive = write_archive(tmp_path)
    path, metadata = offline.ensure_index(archive)
    before = path.stat().st_mtime_ns
    original = offline.build_index
    monkeypatch.setattr(offline, "build_index", lambda *a: pytest.fail("Repeated read must reuse index"))
    assert offline.ensure_index(archive) == (path, metadata)
    assert path.stat().st_mtime_ns == before
    monkeypatch.setattr(offline, "build_index", original)
    write_archive(tmp_path, [topic(77, "Changed topic")])
    _, updated = offline.ensure_index(archive)
    assert updated["fingerprint"] != metadata["fingerprint"]
    with closing(offline.connect(path)) as db:
        assert db.execute("SELECT id FROM topics").fetchone()[0] == 77
    path.write_bytes(b"broken cache")
    assert offline.ensure_index(archive)[1]["topics"] == 1


@pytest.mark.parametrize("damage", ["truncated_xz", "invalid_xml", "wrong_root", "no_valid_topics"])
async def test_bad_archives_fail_without_publishing_partial_index(tmp_path, media, damage):
    archive = write_archive(tmp_path)
    if damage == "truncated_xz":
        archive.write_bytes(archive.read_bytes()[:-20])
    elif damage == "invalid_xml":
        archive.write_bytes(lzma.compress(("<torrents>" + topic() + "<broken").encode()))
    elif damage == "wrong_root":
        archive.write_bytes(lzma.compress(b"<wrong/>"))
    else:
        write_archive(tmp_path, [topic(infohash="invalid")])
    with pytest.raises(ProviderError, match="дамп Rutracker"):
        await offline.Catalog((tmp_path,), "https://example.invalid/forum").search(
            SearchQuery(requirements=Requirements(), media=media)
        )
    assert not offline.index_path(archive).exists()
    assert not list((tmp_path / ".lazarr-index").glob("*.building"))


def test_failed_replacement_retains_old_index_but_never_returns_stale_results(tmp_path):
    archive = write_archive(tmp_path)
    path, _ = offline.ensure_index(archive)
    before = path.read_bytes()
    archive.write_bytes(lzma.compress(b"<torrents>"))
    with pytest.raises(offline.ET.ParseError):
        offline.ensure_index(archive)
    assert path.read_bytes() == before


def test_concurrent_import_only_builds_once(tmp_path, monkeypatch):
    archive = write_archive(tmp_path)
    original = offline.build_index
    calls = []

    def build(*args):
        calls.append(args)
        time.sleep(0.05)
        return original(*args)

    monkeypatch.setattr(offline, "build_index", build)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(offline.ensure_index, archive) for _ in range(2)]
        assert futures[0].result() == futures[1].result()
    assert len(calls) == 1


async def test_latest_snapshot_across_roots_and_removed_dump(tmp_path, media):
    other = tmp_path / "other"
    other.mkdir()
    write_archive(tmp_path, [topic(1)], name="rutracker-20250101.xml.xz")
    newest = write_archive(other, [topic(2)], name="rutracker-20260927.xml.xz")
    catalog = offline.Catalog((tmp_path, other), "https://mirror.invalid/forum")
    assert (await catalog.status()).year_to == 2026
    item = (await catalog.search(SearchQuery(requirements=Requirements(), media=media))).items[0]
    assert item.id == "2" and item.url == "https://mirror.invalid/forum/viewtopic.php?t=2"
    newest.unlink()
    assert (await catalog.search(SearchQuery(requirements=Requirements(), media=media))).items[0].id == "1"
    for path in tmp_path.glob("*.xml.xz"):
        path.unlink()
    assert not (await catalog.status()).available
    with pytest.raises(ProviderError, match="Поместите"):
        await catalog.search(SearchQuery(requirements=Requirements(), media=media))


async def test_unnamed_snapshot_derives_coverage_from_registration_dates(tmp_path):
    archive = write_archive(tmp_path, name="dump.xml")
    offline.ensure_index(archive)
    status = await offline.Catalog((tmp_path,), "https://example.invalid").status()
    assert status.available and status.year_to == 2024


async def test_non_video_payloads_are_skipped_but_disc_images_and_unknown_file_lists_remain(tmp_path, media):
    audio = topic(1).replace("Show.S01E01.1080p.mkv", "Music.flac")
    disc = topic(2).replace("Show.S01E01.1080p.mkv", "Movie.iso")
    unknown = topic(3)
    start = unknown.index('<dir name="Show">')
    unknown = unknown[:start] + "</torrent>"
    archive = write_archive(tmp_path, [audio, disc, unknown])
    catalog = offline.Catalog((tmp_path,), "https://example.invalid")
    page = await catalog.search(SearchQuery(requirements=Requirements(), media=media))
    assert [item.id for item in page.items] == ["2", "3"]
    assert offline.ensure_index(archive)[1]["scanned"] == 3
    write_archive(tmp_path, [audio])
    assert not (await catalog.search(SearchQuery(requirements=Requirements(), media=media))).items


async def test_index_build_does_not_block_event_loop(tmp_path, media, monkeypatch):
    write_archive(tmp_path)
    original = offline.ensure_index
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_index(path):
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.1)
        return original(path)

    monkeypatch.setattr(offline, "ensure_index", slow_index)
    search = asyncio.create_task(
        offline.Catalog((tmp_path,), "https://example.invalid").search(
            SearchQuery(requirements=Requirements(), media=media)
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert not search.done()
    assert (await search).items


@pytest.mark.parametrize("waiting_for_lock", [False, True])
async def test_cancelled_search_stops_import_or_lock_wait(tmp_path, media, monkeypatch, waiting_for_lock):
    archive = write_archive(tmp_path, [topic(i) for i in range(1, 100)])
    destination = offline.index_path(archive)
    destination.parent.mkdir(exist_ok=True)
    entered, finished = Event(), Event()
    original_call = offline.ensure_index
    original_records = offline.records

    def slow_records(path, offset=0):
        for item in original_records(path, offset):
            time.sleep(0.01)
            yield item

    def ensure(path):
        entered.set()
        try:
            return original_call(path)
        finally:
            finished.set()

    monkeypatch.setattr(offline, "records", slow_records)
    monkeypatch.setattr(offline, "ensure_index", ensure)
    with destination.with_suffix(".lock").open("a") as lock:
        if waiting_for_lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
        search = asyncio.create_task(
            offline.Catalog((tmp_path,), "https://example.invalid").search(
                SearchQuery(requirements=Requirements(), media=media)
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        search.cancel()
        with pytest.raises(asyncio.CancelledError):
            await search
        assert await asyncio.to_thread(finished.wait, 2)
    assert not destination.exists()
    assert not destination.with_suffix(".building").exists()


async def test_worker_uses_real_rutracker_dump_and_magnet(
    core, media, season, catalog, worker_setup, monkeypatch
):
    worker, engine, _ = worker_setup
    core[2].configure("demo", {}, False)
    write_archive(catalog)
    core[3].create_from_metadata(
        CreateTask(media_id="42", kind="tv", season=1, episodes=[1], requirements=Requirements()),
        media,
        season,
        1,
    )
    original = engine.inspect
    sources = []

    def inspect(source):
        sources.append(source)
        assert source.magnet and source.torrent is None
        return original(DownloadSource(torrent=json.dumps(["Show/Show.S01E01.1080p.mkv"]).encode()))

    monkeypatch.setattr(engine, "inspect", inspect)
    await worker.run_due()
    assert len(sources) == 1 and engine.plans
    with core[1].session() as db:
        assert db.scalar(select(SubtaskAsset)) is not None
        release = db.scalar(select(Release))
        assert release.provider == "rutracker" and release.data["offline"]


async def test_unknown_topic_never_uses_arbitrary_saved_magnet(core, media, catalog):
    write_archive(catalog)
    async with core[2].open("rutracker", offline=True) as provider:
        item = (
            await provider.search(SearchQuery(requirements=Requirements(), media=media), offline=True)
        ).items[0]
        with pytest.raises(ProviderError, match="отсутствует"):
            await provider.resolve_download(
                item.model_copy(update={"id": "999", "magnet": "magnet:?bogus"}), offline=True
            )


def test_resume_at_committed_xz_stream_without_reading_prefix(tmp_path, monkeypatch):
    parts = [b"<torrents>", topic(7).encode(), topic(2).encode(), b"</torrents>"]
    compressed = [lzma.compress(part) for part in parts]
    archive = tmp_path / "rutracker-20260927.xml.xz"
    archive.write_bytes(b"".join(compressed))
    position = len(compressed[0]) + len(compressed[1])
    original = offline.records

    def interrupted(path, offset=0):
        for entry in original(path, offset):
            yield entry
            if isinstance(entry, offline.Checkpoint) and entry.offset == position:
                raise InterruptedError("simulated restart")

    monkeypatch.setattr(offline, "records", interrupted)
    with pytest.raises(InterruptedError):
        offline.ensure_index(archive)
    final = offline.index_path(archive)
    pending = offline.cached_metadata(final.with_suffix(".building"), offline.fingerprint(archive))
    assert pending["checkpoint"] == position and pending["topics"] == 1
    assert not final.exists()
    offsets = []

    def resumed(path, offset=0):
        offsets.append(offset)
        yield from original(path, offset)

    monkeypatch.setattr(offline, "records", resumed)
    path, metadata = offline.ensure_index(archive)
    assert offsets == [position]
    assert metadata["complete"] and metadata["topics"] == metadata["scanned"] == 2
    with closing(offline.connect(path)) as db:
        assert [row[0] for row in db.execute("SELECT id FROM topics ORDER BY id")] == [2, 7]
    assert not final.with_suffix(".building").exists()


def test_xz_boundaries_inside_records_are_not_checkpointed(tmp_path):
    item = topic().encode()
    parts = [b"<torrents>", item[:10], item[10:400], item[400:], b"</torrents>"]
    compressed = [lzma.compress(part) + b"\0" * 4 for part in parts]
    archive = tmp_path / "rutracker-20260927.xml.xz"
    archive.write_bytes(b"".join(compressed))
    entries = list(offline.records(archive))
    checkpoints = [e.offset for e in entries if isinstance(e, offline.Checkpoint)]
    assert checkpoints == [len(compressed[0]) - 4, sum(map(len, compressed[:4])) - 4]
    assert offline.ensure_index(archive)[1]["topics"] == 1


async def test_portable_catalog_works_without_source_or_original_cache(tmp_path, media):
    original_dir, remote = tmp_path / "original", tmp_path / "remote"
    original_dir.mkdir()
    remote.mkdir()
    archive = write_archive(original_dir)
    output = original_dir / "rutracker-20260927.sqlite3"
    offline.export_index(archive, output)
    shutil.copyfile(output, remote / output.name)  # New inode and a different filesystem location.
    shutil.rmtree(original_dir)
    catalog = offline.Catalog((remote,), "https://mirror.invalid/forum")
    status = await catalog.status()
    assert status.available and status.year_to == 2026
    item = (await catalog.search(SearchQuery(media=media, requirements=Requirements()))).items[0]
    assert (await catalog.inspect(item)).external_ids == {"imdb": "tt0042"}
    assert (await catalog.resolve(item)).magnet
    assert not (remote / ".lazarr-index").exists()


def test_portable_export_never_overwrites_an_existing_file(tmp_path):
    archive = write_archive(tmp_path)
    output = tmp_path / "rutracker-20260927.sqlite3"
    output.write_bytes(b"user data")
    with pytest.raises(FileExistsError):
        offline.export_index(archive, output)
    assert output.read_bytes() == b"user data"


async def test_incomplete_catalog_cannot_be_used_as_portable(tmp_path):
    archive = write_archive(tmp_path)
    path, metadata = offline.ensure_index(archive)
    incomplete = tmp_path / "rutracker-20260927.sqlite3"
    shutil.copyfile(path, incomplete)
    with closing(sqlite3.connect(incomplete)) as db:
        metadata["complete"] = False
        db.execute("UPDATE metadata SET value=?", (json.dumps(metadata),))
        db.commit()
    with pytest.raises(ProviderError, match="готовым индексом"):
        await offline.Catalog((tmp_path,), "https://example.invalid").status()


def test_prepare_cli_exports_catalog_without_initializing_application_database(tmp_path, monkeypatch, capsys):
    import sys
    from lazarr import cli

    archive = write_archive(tmp_path)
    output = tmp_path / "rutracker-20260927.sqlite3"
    priorities = []
    monkeypatch.setattr(cli.os, "nice", priorities.append)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lazarr",
            "--data-dir",
            str(tmp_path / "unused-data"),
            "prepare-rutracker",
            str(archive),
            "--output",
            str(output),
        ],
    )
    monkeypatch.setenv("LAZARR_DATA_DIR", str(tmp_path / "unused-data"))
    cli.main()
    assert priorities == [10]
    assert offline.ensure_index(output)[1]["topics"] == 1
    assert not (tmp_path / "unused-data/lazarr.sqlite").exists()
    from lazarr.task_progress import persisted_tasks

    jobs = persisted_tasks(tmp_path / "unused-data/background-jobs")
    assert len(jobs) == 1 and jobs[0]["kind"] == "offline-index"
    assert jobs[0]["state"] == "completed" and jobs[0]["progress"] == 1
    assert "Готово:" in capsys.readouterr().out


def test_real_dump_one_gib_dictionary_header_is_supported(tmp_path):
    # Real snapshots advertise a 1 GiB dictionary even for small independent
    # streams. Reproduce the header without allocating a huge compressor.
    data = bytearray(lzma.compress(("<torrents>" + topic() + "</torrents>").encode()))
    header_size = (data[12] + 1) * 4
    assert data[13:16] == b"\0\x21\x01"
    data[16] = 36  # LZMA2 dictionary property for 1 GiB.
    end = 12 + header_size
    data[end - 4 : end] = struct.pack("<I", binascii.crc32(data[12 : end - 4]))
    archive = tmp_path / "rutracker-20260927.xml.xz"
    archive.write_bytes(data)
    assert offline.ensure_index(archive)[1]["topics"] == 1
