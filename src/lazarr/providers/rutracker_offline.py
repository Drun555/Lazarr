"""Provider-owned, streaming index of Rutracker XML / XML.XZ snapshots."""

import asyncio
from contextlib import closing
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
import fcntl
import errno
import hashlib
import html
import json
import logging
import lzma
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from threading import Event
import time
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zlib

from lazarr.provider_utils import description_evidence, search_title
from lazarr.sdk import Candidate, DownloadSource, Evidence, OfflineStatus, ProviderError, SearchPage
from lazarr.task_progress import registered_task, task_directory

log = logging.getLogger(__name__)
INDEX_VERSION = 3
PAGE_SIZE = 100
_cancelled = ContextVar("rutracker_index_cancelled", default=None)
_index_task = ContextVar("rutracker_index_task", default=None)
# Include disc images and uncommon video containers too: normal search stages
# decide whether they are supported. Audio-only/software/book torrents cannot
# supply a movie or episode, and dominate full tracker snapshots.
VIDEO_SUFFIXES = {
    "mkv",
    "mp4",
    "avi",
    "m4v",
    "ts",
    "m2ts",
    "mts",
    "webm",
    "mov",
    "mpg",
    "mpeg",
    "vob",
    "iso",
    "img",
    "wmv",
    "asf",
    "flv",
    "ogm",
    "ogv",
    "divx",
    "rm",
    "rmvb",
    "m2v",
}


def snapshot_date(path):
    match = re.search(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", path.name)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            pass
    return None


def check_cancelled():
    event = _cancelled.get()
    if event is not None and event.is_set():
        raise asyncio.CancelledError


def report_progress(**values):
    task = _index_task.get()
    if task is not None:
        task.update(**values)


def select_archive(directories):
    archives = [
        path
        for directory in directories
        for pattern in ("*.xml.xz", "*.xml", "*.sqlite3")
        for path in directory.glob(pattern)
        if path.is_file()
    ]
    return max(
        archives,
        key=lambda p: (snapshot_date(p) or date.min, p.suffix == ".sqlite3", p.stat().st_mtime_ns, str(p)),
        default=None,
    )


def fingerprint(path):
    stat = path.stat()
    return [INDEX_VERSION, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino]


def index_path(archive):
    identity = hashlib.sha256(archive.name.encode()).hexdigest()[:20]
    return archive.parent / ".lazarr-index" / f"{identity}.sqlite3"


def connect(path):
    immutable = "&immutable=1" if path.suffix == ".sqlite3" else ""
    connection = sqlite3.connect(path.as_uri() + "?mode=ro" + immutable, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def cached_metadata(path, signature=None):
    if not path.is_file():
        return None
    try:
        with closing(connect(path)) as db:
            metadata = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
            return (
                metadata
                if isinstance(metadata, dict) and (signature is None or metadata["fingerprint"] == signature)
                else None
            )
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return None


@dataclass(frozen=True)
class Checkpoint:
    offset: int


def chunks(archive, offset=0):
    """Bound decompressed output and expose independently seekable XZ stream ends."""
    with archive.open("rb") as stream:
        stream.seek(offset)
        if not archive.name.endswith(".xz"):
            while data := stream.read(65536):
                check_cancelled()
                report_progress(completed_bytes=stream.tell())
                yield data, None
            return
        pending = b""
        while True:
            pending = pending or stream.read(65536)
            if not pending:
                return
            # XZ permits padding of whole four-byte groups between streams.
            padding = 0
            while pending.startswith(b"\0"):
                stripped = len(pending) - len(pending.lstrip(b"\0"))
                padding += stripped
                pending = pending[stripped:] or stream.read(65536)
                if not pending:
                    break
            if padding % 4:
                raise ValueError("Некорректное заполнение XZ")
            if not pending:
                return
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=2 * 1024 * 1024 * 1024)
            while not decoder.eof:
                check_cancelled()
                data = b""
                if decoder.needs_input:
                    data, pending = pending or stream.read(65536), b""
                    if not data:
                        raise EOFError("Незавершённый XZ-поток")
                output = decoder.decompress(data, max_length=65536)
                report_progress(completed_bytes=stream.tell() - len(decoder.unused_data))
                if output:
                    yield output, None
            pending = decoder.unused_data
            yield b"", stream.tell() - len(pending)


def records(archive, offset=0):
    """Checkpoint only complete XML records; split records keep the previous checkpoint."""
    parser = ET.XMLPullParser(events=("start", "end"))
    if offset:
        parser.feed(b"<torrents>")
    depth = 0
    tail = b""
    for data, boundary in chunks(archive, offset):
        if data:
            tail = (tail + data)[-256:]
            parser.feed(data)
        for event, element in parser.read_events():
            if event == "start":
                depth += 1
                if depth == 1:
                    root = element
                    if root.tag != "torrents":
                        raise ValueError("Ожидался корневой элемент torrents")
            else:
                if depth == 2:
                    if element.tag == "torrent" and element.get("id"):
                        yield element
                    root.remove(element)
                depth -= 1
        if boundary and depth == 1 and tail.rstrip().endswith((b"</torrent>", b"<torrents>")):
            yield Checkpoint(boundary)
    parser.close()


def file_names(element, parents=()):
    for child in element:
        name = child.get("name", "")
        if child.tag == "dir":
            yield from file_names(child, (*parents, name))
        elif child.tag == "file" and name:
            yield "/".join((*parents, name))


def normalize(text):
    return text.casefold().replace("ё", "е")


def publish_index(temporary, destination):
    report_progress(force=True, phase="Сохранение индекса")
    # A portable/renamed database must contain all committed pages in one file.
    with closing(sqlite3.connect(temporary)) as db:
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise sqlite3.OperationalError("Индекс занят чтением; повторите завершение подготовки")
        db.execute("PRAGMA journal_mode=DELETE")
    temporary.replace(destination)


def build_index(archive, destination, signature):
    directory = task_directory.get() or archive.parent / ".lazarr-jobs"
    with registered_task(
        directory,
        destination,
        kind="offline-index",
        detail=f"Rutracker · {archive.name}",
        total_bytes=signature[1],
    ) as task:
        token = _index_task.set(task)
        try:
            metadata = _build_index(archive, destination, signature)
            task.update(completed_bytes=signature[1], scanned=metadata["scanned"], indexed=metadata["topics"])
            return metadata
        finally:
            _index_task.reset(token)


def _build_index(archive, destination, signature):
    """Resume committed XZ stream boundaries; publish only a complete catalog."""
    temporary = destination.with_suffix(".building")
    saved = cached_metadata(temporary, signature)
    if saved and saved.get("complete"):
        publish_index(temporary, destination)
        return saved
    if not saved or "checkpoint" not in saved:
        temporary.unlink(missing_ok=True)
        saved = {"checkpoint": 0, "topics": 0, "scanned": 0, "skipped": 0, "year_to": None}
    started = time.monotonic()
    offset = saved["checkpoint"]
    count, skipped, scanned = (saved[key] for key in ("topics", "skipped", "scanned"))
    latest_year = saved["year_to"] or 0
    report_progress(force=True, phase="Чтение архива", completed_bytes=offset, scanned=scanned, indexed=count)
    snapshot = snapshot_date(archive)
    metadata = {}
    log.info("Rutracker: indexing %s from compressed byte %s", archive.name, offset)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                PRAGMA cache_size=-8192;
                PRAGMA temp_store=FILE;
                CREATE TABLE IF NOT EXISTS metadata(value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS topics(
                    id INTEGER NOT NULL UNIQUE, title TEXT NOT NULL, category TEXT NOT NULL,
                    size INTEGER, infohash TEXT NOT NULL, payload BLOB NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS titles USING fts5(title, content='', tokenize='unicode61');
            """)

            def checkpoint(position, complete=False):
                nonlocal metadata, offset
                metadata = {
                    "format": "lazarr-rutracker",
                    "fingerprint": signature,
                    "topics": count,
                    "scanned": scanned,
                    "skipped": skipped,
                    "year_to": snapshot.year if snapshot else latest_year or None,
                    "checkpoint": position,
                    "complete": complete,
                }
                db.execute("DELETE FROM metadata")
                db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(metadata),))
                db.commit()
                offset = position

            checkpoint(offset)
            for element in records(archive, offset):
                check_cancelled()
                if isinstance(element, Checkpoint):
                    checkpoint(element.offset)
                    continue
                scanned += 1
                report_progress(scanned=scanned, indexed=count)
                if scanned % 100000 == 0:
                    log.info("Rutracker: scanned %s topics, indexed %s from %s", scanned, count, archive.name)
                registered = re.match(r"^(\d{4})[.-]", element.get("registred_at", ""))
                if registered and 1 <= int(registered[1]) <= 9999:
                    latest_year = max(latest_year, int(registered[1]))
                identity = element.get("id", "")
                title = html.unescape(element.findtext("title", "")).strip()
                torrent = element.find("torrent")
                infohash = torrent.get("hash", "") if torrent is not None else ""
                if not identity.isdecimal() or not title or not re.fullmatch(r"[a-fA-F0-9]{40}", infohash):
                    skipped += 1
                    continue
                files = list(file_names(element))
                if files and not any(name.rsplit(".", 1)[-1].casefold() in VIDEO_SUFFIXES for name in files):
                    continue
                raw_size = element.get("size", "")
                size = int(raw_size) if raw_size.isdecimal() else None
                payload = zlib.compress(
                    json.dumps(
                        {"content": element.findtext("content", ""), "files": files},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode(),
                    level=3,
                )
                inserted = db.execute(
                    "INSERT INTO topics VALUES (?, ?, ?, ?, ?, ?)",
                    (int(identity), title, element.findtext("forum", ""), size, infohash.lower(), payload),
                )
                db.execute(
                    "INSERT INTO titles(rowid, title) VALUES (?, ?)", (inserted.lastrowid, normalize(title))
                )
                count += 1
                report_progress(indexed=count)
            if scanned == skipped:
                raise ValueError("В дампе нет раздач с корректными названиями и infohash")
            if fingerprint(archive) != signature:
                raise ValueError("Дамп изменился во время индексации; повторите поиск")
            checkpoint(archive.stat().st_size, complete=True)
        publish_index(temporary, destination)
        log.info("Rutracker: indexed %s topics in %.1f seconds", count, time.monotonic() - started)
        return metadata
    finally:
        # Keep committed work on cancellation/failure. A zero-offset cache has no
        # resumable work and is removed; source changes discard it on next import.
        if not offset:
            temporary.unlink(missing_ok=True)


def ensure_index(archive):
    if archive.suffix == ".sqlite3":
        metadata = cached_metadata(archive)
        if (
            not metadata
            or metadata.get("complete") is False
            or (metadata.get("fingerprint") or [None])[0] != INDEX_VERSION
            or not isinstance(metadata.get("topics"), int)
        ):
            raise ValueError("Файл не является готовым индексом Rutracker совместимой версии")
        with closing(connect(archive)) as db:
            db.execute("SELECT id, title, category, size, infohash, payload FROM topics LIMIT 0")
            db.execute("SELECT rowid FROM titles LIMIT 0")
        return archive, metadata
    destination = index_path(archive)
    signature = fingerprint(archive)
    cached = cached_metadata(destination, signature)
    if cached:
        return destination, cached
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Also serialize API/worker calls made by different application processes.
    with destination.with_suffix(".lock").open("a") as lock:
        while True:
            check_cancelled()
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.1)
        signature = fingerprint(archive)
        cached = cached_metadata(destination, signature)
        return destination, cached or build_index(archive, destination, signature)


def export_index(archive, output):
    """A portable catalog works without the XML and without machine-specific stats."""
    if output.exists():
        raise FileExistsError(f"Файл уже существует: {output}")
    source, metadata = ensure_index(archive)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.hardlink_to(source)
        return metadata
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP}:
            raise
    with tempfile.TemporaryDirectory(prefix=".lazarr-export-", dir=output.parent) as directory:
        temporary = Path(directory) / "index"
        shutil.copyfile(source, temporary)
        # Hard-link publication fails rather than overwriting a concurrent export.
        output.hardlink_to(temporary)
    return metadata


def description_text(content):
    # Keep textual spoiler headings and MediaInfo line boundaries. Only strip known
    # BBCode, so [2024], [1080p] and episode markers remain available to the matcher.
    text = re.sub(r"\[img(?:=[^\]]*)?\].*?\[/img\]", "", content, flags=re.I | re.S)
    text = re.sub(
        r'\[spoiler(?:=(?:"([^"]*)"|([^\]]*)))?\]',
        lambda m: "\n" + (m[1] or m[2] or "") + "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"\[(?:/?(?:quote|pre|code|list|spoiler)|hr|br|\*)(?:=[^\]]*)?\]", "\n", text, flags=re.I)
    text = re.sub(r"\[/?(?:b|i|u|s|size|color|font|align|url|email|img)(?:=[^\]]*)?\]", "", text, flags=re.I)
    return html.unescape(text).strip()


class Catalog:
    def __init__(self, directories, base, background_directory=None):
        self.directories, self.base = directories, base
        self.background_directory = background_directory

    async def _call(self, operation, *args):
        cancelled = Event()

        def run():
            token = _cancelled.set(cancelled)
            directory_token = task_directory.set(self.background_directory or task_directory.get())
            try:
                return operation(*args)
            finally:
                _cancelled.reset(token)
                task_directory.reset(directory_token)

        try:
            return await asyncio.to_thread(run)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except (
            OSError,
            EOFError,
            lzma.LZMAError,
            ET.ParseError,
            sqlite3.Error,
            zlib.error,
            ValueError,
        ) as exc:
            raise ProviderError("offline_data", f"Не удалось прочитать дамп Rutracker: {exc}") from exc

    def _archive(self):
        archive = select_archive(self.directories)
        if archive is None:
            raise ProviderError(
                "not_found", "Поместите .xml.xz, .xml или готовый .sqlite3 в offline/rutracker"
            )
        return archive

    async def status(self):
        return await self._call(self._status)

    def _status(self):
        archive = select_archive(self.directories)
        if archive is None:
            return OfflineStatus(message="Поместите .xml.xz, .xml или готовый .sqlite3 в offline/rutracker")
        if archive.suffix == ".sqlite3":
            _, metadata = ensure_index(archive)
            return OfflineStatus(
                available=bool(metadata.get("year_to")),
                year_to=metadata.get("year_to"),
                message=f"{archive.name}: готовый каталог, раздач: {metadata['topics']}",
            )
        snapshot = snapshot_date(archive)
        cached = cached_metadata(index_path(archive), fingerprint(archive))
        pending = cached_metadata(index_path(archive).with_suffix(".building"), fingerprint(archive))
        year = snapshot.year if snapshot else (cached or {}).get("year_to")
        if not year:
            return OfflineStatus(message="Неизвестна дата дампа: используйте имя rutracker-YYYYMMDD.xml.xz")
        if cached:
            detail = f"раздач: {cached['topics']}"
        elif pending:
            percent = 100 * pending.get("checkpoint", 0) / max(archive.stat().st_size, 1)
            detail = f"подготовка: {percent:.1f}%; обработано {pending['scanned']}; продолжится при поиске"
        else:
            detail = "индекс будет создан при первом поиске или командой prepare-rutracker"
        return OfflineStatus(available=True, year_to=year, message=f"{archive.name}: {detail}")

    def candidate(self, row):
        return Candidate(
            provider="rutracker",
            id=str(row["id"]),
            url=f"{self.base}/viewtopic.php?t={row['id']}",
            title=row["title"],
            revision=row["infohash"],
            size=row["size"],
            offline=True,
            magnet="magnet:?" + urlencode({"xt": "urn:btih:" + row["infohash"], "dn": row["title"]}),
            evidence=[Evidence(field="category", value=row["category"], source="structured")]
            if row["category"]
            else [],
        )

    async def search(self, query, cursor=None):
        return await self._call(self._search, query.text or search_title(query.media), cursor)

    def _search(self, text, cursor):
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", normalize(text))))
        if not tokens:
            return SearchPage(items=[])
        after = int(cursor or 0)
        if after < 0:
            raise ValueError("Некорректный курсор поиска")
        path, _ = ensure_index(self._archive())
        with closing(connect(path)) as db:
            rows = db.execute(
                "SELECT t.id, t.title, t.category, t.size, t.infohash FROM titles "
                "JOIN topics t ON t.rowid=titles.rowid WHERE titles MATCH ? AND t.id>? "
                "ORDER BY t.id LIMIT ?",
                (" AND ".join(f'"{token}"' for token in tokens), after, PAGE_SIZE + 1),
            ).fetchall()
        return SearchPage(
            items=[self.candidate(row) for row in rows[:PAGE_SIZE]],
            next_cursor=str(rows[PAGE_SIZE - 1]["id"]) if len(rows) > PAGE_SIZE else None,
        )

    async def inspect(self, candidate):
        return await self._call(self._inspect, candidate)

    def _inspect(self, candidate):
        path, _ = ensure_index(self._archive())
        with closing(connect(path)) as db:
            row = db.execute("SELECT * FROM topics WHERE id=?", (candidate.id,)).fetchone()
        if row is None:
            raise ProviderError("not_found", "Раздача отсутствует в установленном дампе Rutracker")
        payload = json.loads(zlib.decompress(row["payload"]))
        description = description_text(payload["content"])
        item = self.candidate(row)
        imdb = re.search(r"imdb\.com/title/(tt\d+)", payload["content"], re.I)
        return item.model_copy(
            update={
                "description": description,
                "evidence": [*item.evidence, *description_evidence(description)],
                "external_ids": {"imdb": imdb[1].lower()} if imdb else {},
                "file_hints": payload["files"],
            }
        )

    async def resolve(self, candidate):
        # Resolve from the installed snapshot rather than trusting an arbitrary URI
        # in a saved candidate. TorrentEngine obtains the authoritative metadata.
        item = await self.inspect(candidate)
        return DownloadSource(magnet=item.magnet)
