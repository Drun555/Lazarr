"""Original torrent storage and a recoverable, relative-symlink library."""

import logging
import os
from pathlib import Path
import re
import threading
import unicodedata
from uuid import uuid4

from sqlalchemy import select
from lazarr.models import (
    ConfigEntry,
    Download,
    Episode,
    LibraryAsset,
    Media,
    MediaAsset,
    Season,
    Subtask,
    SubtaskAsset,
    Task,
)

log = logging.getLogger(__name__)
_lock = threading.RLock()
MANIFEST = "storage.links"


def storage_root(configured):
    path = Path(configured).absolute()
    # Preserve existing movie/series settings while consolidating their common root.
    return (
        path.parent
        if path.name.casefold() in {"movies", "series", "anime", "source"} and not path.is_mount()
        else path
    )


def source_directory(configured, infohash):
    return storage_root(configured) / "source" / infohash


def component(value, limit=110):
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    value = value.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(" .") or "Untitled"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value):
        value = "_" + value
    return value


def real_directory(path):
    """Never follow a directory symlink during publication or cleanup."""
    if path.absolute() != path.resolve():
        raise ValueError(f"Storage directory contains a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)


def check_links(root, database=None):
    """Probe the actual mount, not the host OS or Docker backend name."""
    real_directory(root)
    token = ".lazarr-check-" + uuid4().hex
    target, link = root / token, root / (token + ".link")
    try:
        target.write_bytes(b"lazarr")
        link.symlink_to(target.name)
        if not link.is_symlink() or link.read_bytes() != b"lazarr":
            raise OSError("Filesystem did not preserve a usable symbolic link")
    except OSError as error:
        message = (
            f"Cannot create usable symlinks in {root}. For Docker Desktop/WSL2, use a directory "
            "on the Linux filesystem and mount the entire downloads directory. "
            "Original media has not been copied or deleted."
        )
        if database:
            set_status(database, "capability_error", message)
        raise OSError(message) from error
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    if database:
        set_status(database, "capability_error", None)


def _migrate_sources(database):
    """Run before adding torrent handles. Intent survives a crash after rename."""
    from lazarr.deletion import managed_directory

    with _lock:
        with database.session() as db:
            identities = list(db.scalars(select(Download.id)))
        for identity in identities:
            key = f"storage.move.{identity}"
            with database.session() as db:
                download = db.get(Download, identity)
                old = managed_directory(download)
                job = db.get(ConfigEntry, key)
                if job:
                    old, new = Path(job.value["old"]), Path(job.value["new"])
                else:
                    if old.parent.name == "source":
                        continue
                    new = source_directory(old.parent, download.infohash)
                    if new.exists() or new.is_symlink():
                        raise FileExistsError(f"Migration destination already exists: {new}")
                    check_links(new.parent.parent / "user")
                    db.add(ConfigEntry(key=key, value={"old": str(old), "new": str(new)}))
            real_directory(new.parent)
            if new.resolve() != new:
                raise ValueError(f"Unsafe migration destination: {new}")
            if old.exists():
                if new.exists() or new.is_symlink():
                    raise FileExistsError(f"Both migration paths exist: {old}, {new}")
                # Same-filesystem rename only: never silently copy a partially downloaded torrent.
                old.rename(new)
            elif not new.is_dir():
                raise FileNotFoundError(f"Migration source is missing: {old}")
            with database.session() as db:
                db.get(Download, identity).save_path = str(new)
                # Durable pending cleanup jobs must follow a moved torrent too.
                for entry in db.scalars(select(ConfigEntry).where(ConfigEntry.key.startswith("cleanup."))):
                    value = dict(entry.value)
                    value["directories"] = [str(new) if p == str(old) else p for p in value["directories"]]
                    value["files"] = [
                        {**f, "root": str(new) if f["root"] == str(old) else f["root"]}
                        for f in value.get("files", [])
                    ]
                    entry.value = value
                db.delete(db.get(ConfigEntry, key))
            try:
                old.parent.rmdir()  # Only remove empty legacy category directories.
            except OSError:
                pass


def desired_links(db, warnings):
    from lazarr.library import library_kind
    from lazarr.library_metadata import sidecars
    from lazarr.specials import catalog_for, placement

    result, winners, names = {}, set(), set()
    rows = list(
        db.execute(
            select(LibraryAsset, MediaAsset, Download, Media)
            .join(MediaAsset, LibraryAsset.asset_id == MediaAsset.id)
            .join(Download, MediaAsset.download_id == Download.id)
            .join(Media, LibraryAsset.media_id == Media.id)
            .order_by(LibraryAsset.created_at, LibraryAsset.id)
        )
    )
    rows = [row for row in rows if row[0].verification.get("complete")]
    # Buffered files remain pending downloads, not completed library assets.
    # Derive their exports from the live selection so removal/replacement also
    # removes these temporary publications. Completed versions take precedence.
    for link, asset, download, media, sub in db.execute(
        select(SubtaskAsset, MediaAsset, Download, Media, Subtask)
        .join(MediaAsset, SubtaskAsset.asset_id == MediaAsset.id)
        .join(Download, MediaAsset.download_id == Download.id)
        .join(Subtask, SubtaskAsset.subtask_id == Subtask.id)
        .join(Task, Subtask.task_id == Task.id)
        .join(Media, Task.media_id == Media.id)
        .where(SubtaskAsset.pending.is_(True), Subtask.status == "ready")
        .order_by(SubtaskAsset.id)
    ):
        rows.append(
            (
                LibraryAsset(
                    part_key=sub.part_key,
                    episode_id=sub.episode_id,
                    preflight=link.preflight,
                ),
                asset,
                download,
                media,
            )
        )
    for library, asset, download, media in rows:
        identity = (media.id, library.part_key)
        if identity in winners:
            continue
        winners.add(identity)  # First published version wins, even if temporarily unavailable.
        source = Path(download.save_path)
        if source.parent.name != "source":
            continue
        if source.resolve() != source:
            raise ValueError(f"Unsafe source directory: {source}")
        root = source.parent.parent / "user"
        title = component(media.title + (f" ({media.year})" if media.year else ""))
        directory = (
            root / {"movies": "Movies", "series": "Series", "anime": "Anime"}[library_kind(media)] / title
        )
        episode = season = number = None
        stem = title
        if library.episode_id:
            episode = db.get(Episode, library.episode_id)
            season = db.get(Season, episode.season_id)
            aliases = media.metadata_json.get("episode_numbering", {}).get(
                f"{season.number}:{episode.number}", []
            )
            number = aliases[0] if len(aliases) == 1 else {"season": season.number, "episode": episode.number}
            directory /= f"Season {number['season']:02d}"
            stem = component(
                f"{component(media.title, 60)} - S{number['season']:02d}E{number['episode']:02d} - {episode.title}"
            )
        elif library.part_key != "movie":
            continue
        name = str(directory / stem).casefold()
        if name in names:
            continue
        names.add(name)
        video = source / asset.path
        if not video.resolve().is_relative_to(source) or not video.is_file():
            warnings.append(f"Недоступен исходный файл: {video}")
            continue
        files = [(asset.path, stem + "." + component(Path(asset.path).suffix.lstrip("."), 12))]
        for track in library.preflight.get("binding", {}).get("tracks", []):
            if track.get("file_index") is None or not track.get("path"):
                continue
            suffix = component(track.get("language", "und"), 16)
            if track.get("title"):
                suffix += "." + component(track["title"], 40)
            if track.get("forced"):
                suffix += ".forced"
            # Stable index distinguishes multiple tracks of one language, not release versions.
            suffix += f".{track['file_index']}"
            files.append(
                (
                    track["path"],
                    stem + "." + suffix + "." + component(Path(track["path"]).suffix.lstrip("."), 12),
                )
            )
        position = (
            placement(db, episode, catalog_for(db, media.id))["position"]
            if episode and season.number == 0
            else None
        )
        for entry in sidecars(media, episode, season, number, directory, stem, root, position):
            result.setdefault(entry["path"].casefold(), entry)
        for relative, filename in files:
            target = source / relative
            if not target.resolve().is_relative_to(source.resolve()) or not target.is_file():
                continue
            path = directory / filename
            # Case folding also enforces first-wins on case-insensitive Windows shares.
            result.setdefault(
                str(path).casefold(), {"path": str(path), "target": str(target), "root": str(root)}
            )
    return list(result.values())


def remove_link(entry):
    path, root = Path(entry["path"]), Path(entry["root"])
    if not path.is_relative_to(root) or path.parent.resolve() != path.parent:
        raise ValueError(f"Unsafe library link: {path}")
    if "target" not in entry:
        if path.is_symlink():
            raise ValueError(f"Metadata path is a symlink: {path}")
        path.unlink(missing_ok=True)
    elif path.is_symlink():
        path.unlink()  # Never unlink the target, even if the user changed the link.
    elif path.exists():
        raise FileExistsError(f"A regular file occupies a managed link: {path}")
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _reconcile(database):
    """Persist intent before touching links; retry safely after any interruption."""
    with _lock:
        with database.session() as db:
            row = db.get(ConfigEntry, MANIFEST)
            previous = row.value.get("entries", []) if row else []
            owned = {e["path"] for e in previous}
            desired, warnings = [], []
            for entry in desired_links(db, warnings):
                path = Path(entry["path"])
                if str(path) not in owned and (path.exists() or path.is_symlink()):
                    log.warning("Library path already occupied; first file wins: %s", path)
                    continue
                desired.append(entry)
            desired_by_path = {e["path"]: e for e in desired}
            stale = [e for e in previous if e["path"] not in desired_by_path]
            # Keep stale entries until their removal has succeeded.
            if row is None:
                row = ConfigEntry(key=MANIFEST, value={})
                db.add(row)
            row.value = {"entries": desired + stale}
        for entry in stale:
            remove_link(entry)
        owned = {e["path"] for e in previous}
        for entry in desired:
            if "content" in entry:
                write_sidecar(entry, entry["content"].encode("utf-8"))
                continue
            if "image" in entry:
                continue
            path, target = Path(entry["path"]), Path(entry["target"])
            real_directory(path.parent)
            relative = os.path.relpath(target, path.parent)
            if path.is_symlink() and os.readlink(path) == relative and path.is_file():
                continue
            if path.exists() or path.is_symlink():
                if str(path) not in owned or not path.is_symlink():
                    log.warning("Library path already occupied; first file wins: %s", path)
                    continue
            temporary = path.with_name(".lazarr-link-" + uuid4().hex)
            try:
                if path.is_symlink():
                    temporary.symlink_to(relative)
                    temporary.replace(path)
                else:
                    path.symlink_to(relative)
            except FileExistsError:
                continue
            except OSError as error:
                raise OSError(
                    f"Cannot publish library symlink {path}; use a Linux filesystem for Docker/WSL2"
                ) from error
            finally:
                temporary.unlink(missing_ok=True)
        with database.session() as db:
            db.get(ConfigEntry, MANIFEST).value = {"entries": desired}
        set_status(database, "warnings", warnings[:20])


def set_status(database, field, value):
    with database.session() as db:
        row = db.get(ConfigEntry, "storage.status")
        if row is None:
            row = ConfigEntry(key="storage.status", value={})
            db.add(row)
        if row.value.get(field) != value:
            row.value = {**row.value, field: value}


def migrate_sources(database):
    try:
        _migrate_sources(database)
    except (OSError, ValueError) as error:
        set_status(database, "migration_error", str(error))
        raise
    set_status(database, "migration_error", None)


def reconcile(database):
    try:
        _reconcile(database)
    except (OSError, ValueError) as error:
        set_status(database, "links_error", str(error))
        raise
    set_status(database, "links_error", None)


def describe(database, settings):
    with database.session() as db:
        state = db.get(ConfigEntry, "storage.status")
        manifest = db.get(ConfigEntry, MANIFEST)
        roots = sorted({str(storage_root(p)) for p in (settings.movie_path, settings.series_path)})
        return {
            "roots": [{"source": str(Path(p) / "source"), "user": str(Path(p) / "user")} for p in roots],
            "links": sum("target" in e for e in manifest.value.get("entries", [])) if manifest else 0,
            "metadata_files": sum("target" not in e for e in manifest.value.get("entries", []))
            if manifest
            else 0,
            **(state.value if state else {}),
        }


def migration_pending(database):
    with database.session() as db:
        return (
            db.scalar(select(ConfigEntry.key).where(ConfigEntry.key.startswith("storage.move."))) is not None
        )


def playable_path(download, relative):
    if not isinstance(relative, str) or not relative:
        return None
    root = Path(download.save_path).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def write_sidecar(entry, content):
    path, root = Path(entry["path"]), Path(entry["root"])
    if not path.is_relative_to(root):
        raise ValueError(f"Unsafe metadata path: {path}")
    real_directory(path.parent)
    if path.is_symlink():
        raise ValueError(f"Metadata path is a symlink: {path}")
    if path.is_file() and path.read_bytes() == content:
        return
    temporary = path.with_name(".lazarr-metadata-" + uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
