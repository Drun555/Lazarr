"""Synchronous libtorrent boundary. Call from a thread, never the HTTP event loop."""

import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from lazarr.sdk import DownloadSource, DownloadPlan, TorrentFile, safe_relative_path
from lazarr.plugins import atomic_write

MiB = 1024 * 1024


@dataclass
class TorrentMetadata:
    infohash: str
    files: list[TorrentFile]
    torrent: bytes


class TorrentEngine(Protocol):
    def contains(self, infohash: str) -> bool: ...
    def inspect(self, source: DownloadSource) -> TorrentMetadata: ...
    def add(
        self, torrent: bytes, save_path: str, plan: DownloadPlan, paused: bool = False, counters=None
    ) -> str: ...
    def update_plan(self, infohash: str, plan: DownloadPlan): ...
    def pause(self, infohash: str): ...
    def resume(self, infohash: str): ...
    def snapshot(self, infohash: str) -> dict: ...
    def checkpoint(self): ...
    def close(self): ...


def pieces_for(file: TorrentFile, piece_length: int, start=0, length=None):
    length = file.size - start if length is None else min(length, max(0, file.size - start))
    if length <= 0:
        return set()
    return set(
        range((file.offset + start) // piece_length, (file.offset + start + length - 1) // piece_length + 1)
    )


def desired_priorities(plan, progress):
    priorities = [0] * len(plan.files)
    bindings = sorted(plan.bindings, key=lambda b: (b.episode_order, b.subtask_id))
    files = {f.index: f for f in plan.files}

    def done(binding):
        ids = {binding.video_index} | {t.file_index for t in binding.tracks if t.file_index is not None}
        return all(progress[i] >= files[i].size for i in ids)

    current = next((b for b in bindings if not done(b)), None)
    for binding in bindings:
        # sequential_download follows torrent offsets, not episode numbers.
        # Keep future episodes unselected until the current episode is complete.
        if binding != current and not done(binding):
            continue
        priorities[binding.video_index] = max(priorities[binding.video_index], 4 if binding == current else 1)
        for track in binding.tracks:
            if track.file_index is not None:
                priorities[track.file_index] = max(
                    priorities[track.file_index], 7 if binding == current else 1
                )
    return priorities, current


def probe_file(path: Path, executable="ffprobe") -> dict:
    try:
        result = subprocess.run(
            [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            return {"ok": False, "error": "ffprobe cannot read this file yet"}
        data = json.loads(result.stdout)
        data["ok"] = bool(data.get("streams"))
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "ffprobe unavailable or timed out"}


class LibtorrentEngine:
    def __init__(self, data_dir: Path, listen="0.0.0.0:6881,[::]:6881", local_only=False):
        import libtorrent as lt

        self.lt = lt
        self.root = data_dir / "torrent_state"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        settings = {
            "listen_interfaces": listen,
            "alert_mask": int(
                lt.alert.category_t.error_notification
                | lt.alert.category_t.storage_notification
                | lt.alert.category_t.status_notification
            ),
            "enable_dht": not local_only,
            "enable_lsd": not local_only,
            "enable_upnp": False,
            "enable_natpmp": False,
        }
        self.session = lt.session(settings)
        self.handles, self.plans, self.applied = {}, {}, {}
        self.pending_pieces = set()
        self.priority_upload_mode = set()
        self.urgent_pieces = {}
        self.saving = set()
        self.removed = set()
        self.removing = set()

    def _identity(self, ti):
        hashes = ti.info_hashes()
        return str(hashes.v1) if hashes.has_v1() else str(hashes.v2)

    def contains(self, infohash):
        with self.lock:
            return infohash in self.handles

    def _files(self, ti):
        storage = ti.files()
        result = []
        for index in range(storage.num_files()):
            if storage.file_flags(index) & self.lt.file_storage.flag_symlink:
                raise ValueError("Symlinks inside torrents are unsupported")
            result.append(
                TorrentFile(
                    index=index,
                    path=storage.file_path(index),
                    size=storage.file_size(index),
                    offset=storage.file_offset(index),
                )
            )
        if len({f.path.casefold() for f in result}) != len(result):
            raise ValueError("Torrent contains duplicate file paths")
        return result

    def inspect(self, source):
        lt = self.lt
        if source.torrent:
            if len(source.torrent) > 32 * MiB:
                raise ValueError("Torrent metadata exceeds 32 MiB")
            ti = lt.torrent_info(lt.bdecode(source.torrent))
            return TorrentMetadata(self._identity(ti), self._files(ti), source.torrent)
        if not source.magnet or not source.magnet.startswith("magnet:?"):
            raise ValueError("Expected torrent bytes or magnet URI")
        # A separate session fetches metadata only; it cannot stall ongoing downloads.
        with tempfile.TemporaryDirectory(prefix="lazarr-metadata-") as directory:
            session = lt.session(
                {"listen_interfaces": "0.0.0.0:0", "enable_upnp": False, "enable_natpmp": False}
            )
            params = lt.parse_magnet_uri(source.magnet)
            params.save_path = directory
            params.flags &= ~lt.torrent_flags.auto_managed
            params.flags &= ~lt.torrent_flags.paused
            params.flags |= lt.torrent_flags.upload_mode | lt.torrent_flags.default_dont_download
            handle = session.add_torrent(params)
            try:
                deadline = time.monotonic() + 40
                while not handle.has_metadata():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Torrent metadata unavailable; retry later")
                    time.sleep(0.1)
                ti = handle.torrent_file()
                torrent = lt.bencode(lt.create_torrent(ti).generate())
                return TorrentMetadata(self._identity(ti), self._files(ti), torrent)
            finally:
                session.remove_torrent(handle)
                session.pause()

    def add(self, torrent, save_path, plan, paused=False, counters=None):
        with self.lock:
            metadata = self.inspect(DownloadSource(torrent=torrent))
            if plan.infohash != metadata.infohash:
                raise ValueError("Download plan does not match torrent identity")
            if [f.model_dump() for f in plan.files] != [f.model_dump() for f in metadata.files]:
                raise ValueError("Download plan file list does not match torrent")
            root = Path(save_path).absolute()
            root.mkdir(parents=True, exist_ok=True)
            for file in metadata.files:
                target = (root / safe_relative_path(file.path)).resolve()
                if not target.is_relative_to(root.resolve()):
                    raise ValueError("Download path escapes its destination")
            if metadata.infohash in self.handles:
                self.update_plan(metadata.infohash, plan)
                return metadata.infohash
            lt = self.lt
            resume = self.root / f"{metadata.infohash}.resume"
            try:
                params = (
                    lt.read_resume_data(resume.read_bytes()) if resume.exists() else lt.add_torrent_params()
                )
            except Exception:
                params = lt.add_torrent_params()
            params.ti = lt.torrent_info(lt.bdecode(torrent))
            if counters:
                params.total_uploaded = max(params.total_uploaded, counters["uploaded"])
                params.total_downloaded = max(params.total_downloaded, counters["downloaded"])
            params.save_path = str(root)
            params.flags &= ~lt.torrent_flags.auto_managed
            params.flags |= (
                lt.torrent_flags.paused | lt.torrent_flags.sequential_download | lt.torrent_flags.upload_mode
            )
            params.file_priorities = [0] * len(metadata.files)
            handle = self.session.add_torrent(params)
            self.handles[metadata.infohash], self.plans[metadata.infohash] = handle, plan
            self.priority_upload_mode.add(metadata.infohash)
            self._apply(metadata.infohash)
            if not paused:
                handle.resume()
            return metadata.infohash

    def update_plan(self, infohash, plan):
        with self.lock:
            self.plans[infohash] = plan
            self._apply(infohash)

    def _apply(self, infohash):
        handle, plan = self.handles[infohash], self.plans[infohash]
        # Before resume data / file checking finishes, storage may not exist.
        # prioritize_files() can then update memory without emitting file_prio_alert,
        # leaving our temporary upload_mode enabled forever. Wait for storage and
        # verified progress before choosing the first unfinished episode.
        status = handle.status()
        if status.state in (
            self.lt.torrent_status.checking_resume_data,
            self.lt.torrent_status.checking_files,
            self.lt.torrent_status.downloading_metadata,
        ):
            return
        if infohash in self.pending_pieces:
            # Alerts can be dropped (especially during a large restore). The
            # actual file priorities are updated after the disk operation, so
            # they also acknowledge completion. Keep at most one update in flight.
            if handle.get_file_priorities() != self.applied[infohash]:
                return
            self.pending_pieces.discard(infohash)
        progress = handle.file_progress(self.lt.torrent_handle.piece_granularity)
        priorities, current = desired_priorities(plan, progress)
        if self.applied.get(infohash) != priorities:
            # File priorities apply asynchronously and reset piece priorities.
            # Do not request pieces using the previous plan during that window.
            if not handle.flags() & self.lt.torrent_flags.upload_mode:
                handle.set_flags(self.lt.torrent_flags.upload_mode)
                self.priority_upload_mode.add(infohash)
            handle.prioritize_files(priorities)
            self.applied[infohash] = priorities
            self.pending_pieces.add(infohash)
            # File priorities are asynchronous; piece priorities are set on file_prio_alert.
        elif infohash not in self.pending_pieces:
            self._piece_priorities(infohash, current)

    def _piece_priorities(self, infohash, current=None):
        handle, plan = self.handles[infohash], self.plans[infohash]
        priorities, active = desired_priorities(
            plan, handle.file_progress(self.lt.torrent_handle.piece_granularity)
        )
        current = current or active
        ti = handle.torrent_file()
        length = ti.piece_length()
        pieces = [0] * ti.num_pieces()
        for file in plan.files:
            for index in pieces_for(file, length):
                pieces[index] = max(pieces[index], priorities[file.index])
        boundary = set()
        if current:
            file = plan.files[current.video_index]
            boundary = pieces_for(file, length, 0, MiB) | pieces_for(
                file, length, max(0, file.size - MiB), MiB
            )
            for index in boundary:
                pieces[index] = 7
        handle.prioritize_pieces(pieces)
        # Deadlines take precedence over sequential offset order, so the end of
        # the current video is fetched early as well as its beginning.
        urgent = {index for index in boundary if not handle.have_piece(index)}
        previous = self.urgent_pieces.get(infohash, set())
        for index in previous - urgent:
            handle.reset_piece_deadline(index)
        for index in urgent - previous:
            handle.set_piece_deadline(index, 0)
        self.urgent_pieces[infohash] = urgent
        if infohash in self.priority_upload_mode:
            handle.unset_flags(self.lt.torrent_flags.upload_mode)
            self.priority_upload_mode.discard(infohash)

    def _alerts(self):
        lt = self.lt
        for alert in self.session.pop_alerts():
            if isinstance(alert, lt.save_resume_data_alert):
                params = alert.params
                infohash = (
                    str(params.info_hashes.v1) if params.info_hashes.has_v1() else str(params.info_hashes.v2)
                )
                if infohash in self.handles and infohash not in self.removing:
                    atomic_write(self.root / f"{infohash}.resume", lt.write_resume_data_buf(params))
                self.saving.discard(infohash)
            elif isinstance(alert, lt.torrent_removed_alert):
                hashes = alert.info_hashes
                self.removed.add(str(hashes.v1) if hashes.has_v1() else str(hashes.v2))
            elif isinstance(alert, lt.file_prio_alert):
                for infohash, handle in self.handles.items():
                    if handle == alert.handle and infohash not in self.removing and handle.is_valid():
                        self.pending_pieces.discard(infohash)
                        self._apply(infohash)
                        break

    def snapshot(self, infohash):
        with self.lock:
            self._alerts()
            self._apply(infohash)
            handle, plan = self.handles[infohash], self.plans[infohash]
            status = handle.status()
            progress = list(handle.file_progress())
            ti = handle.torrent_file()
            length = ti.piece_length()
            bindings = {}
            for binding in plan.bindings:
                file = plan.files[binding.video_index]
                dependencies = {t.file_index for t in binding.tracks if t.file_index is not None}
                selected = dependencies | {file.index}
                total = sum(plan.files[i].size for i in selected)
                downloaded = sum(min(progress[i], plan.files[i].size) for i in selected)
                buffer_pieces = pieces_for(file, length, 0, 32 * MiB) | pieces_for(
                    file, length, max(0, file.size - MiB), MiB
                )
                ready = all(handle.have_piece(i) for i in buffer_pieces) and all(
                    progress[i] >= plan.files[i].size for i in dependencies
                )
                bindings[str(binding.subtask_id)] = {
                    "progress": downloaded / total if total else 0,
                    "downloaded": downloaded,
                    "total": total,
                    "complete": downloaded >= total,
                    "buffer_ready": ready,
                    "eta": int((total - downloaded) / status.download_payload_rate)
                    if status.download_payload_rate
                    else None,
                }
            selected = {b.video_index for b in plan.bindings} | {
                t.file_index for b in plan.bindings for t in b.tracks if t.file_index is not None
            }
            total = sum(plan.files[i].size for i in selected)
            done = sum(min(progress[i], plan.files[i].size) for i in selected)
            return {
                "progress": done / total if total else 0,
                "complete": bool(selected) and done >= total,
                "download_rate": status.download_payload_rate,
                "upload_rate": status.upload_payload_rate,
                "eta": int((total - done) / status.download_payload_rate)
                if status.download_payload_rate
                else None,
                "seeds": status.num_seeds,
                "peers": status.num_peers,
                "paused": bool(status.paused),
                "engine_state": str(status.state),
                "upload_only": bool(handle.flags() & self.lt.torrent_flags.upload_mode),
                "priorities_pending": infohash in self.pending_pieces,
                "uploaded": status.all_time_upload,
                "downloaded": status.all_time_download,
                "bindings": bindings,
                "files": progress,
                "error": status.errc.message() if status.errc.value() else None,
            }

    def remove(self, infohash):
        with self.lock:
            handle = self.handles.get(infohash)
            if handle is not None:
                if infohash not in self.removing:
                    handle.pause()
                    self.removing.add(infohash)
                    self.session.remove_torrent(handle)
                deadline = time.monotonic() + 15
                while infohash not in self.removed:
                    self._alerts()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Torrent removal did not finish; files are preserved")
                    self.session.wait_for_alert(100)
                self.handles.pop(infohash, None)
                self.plans.pop(infohash, None)
                self.applied.pop(infohash, None)
                self.pending_pieces.discard(infohash)
                self.priority_upload_mode.discard(infohash)
                self.urgent_pieces.pop(infohash, None)
                self.saving.discard(infohash)
                self.removed.discard(infohash)
                self.removing.discard(infohash)
            (self.root / f"{infohash}.resume").unlink(missing_ok=True)

    def pause(self, infohash):
        with self.lock:
            if infohash in self.handles:
                self.handles[infohash].pause()

    def resume(self, infohash):
        with self.lock:
            self.handles[infohash].resume()

    def checkpoint(self):
        with self.lock:
            self._alerts()
            for infohash, handle in self.handles.items():
                if infohash in self.removing or not handle.is_valid():
                    continue
                self.saving.add(infohash)
                handle.save_resume_data(
                    self.lt.save_resume_flags_t.flush_disk_cache | self.lt.save_resume_flags_t.save_info_dict
                )

    def close(self):
        self.checkpoint()
        # Drain asynchronous save alerts before session destruction.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                self._alerts()
                if not self.saving:
                    break
            time.sleep(0.05)
        with self.lock:
            self.session.pause()

    def connect_peer(self, infohash, address):
        with self.lock:
            self.handles[infohash].connect_peer(address)
