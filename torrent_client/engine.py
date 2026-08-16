from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class EngineUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class TorrentSnapshot:
    key: str
    name: str
    progress: float
    status: str
    download_rate: int = 0
    upload_rate: int = 0
    peers: int = 0
    total_size: int = 0
    downloaded: int = 0
    save_path: str = ""
    paused: bool = False
    stopped: bool = False
    uploaded: int = 0
    all_time_downloaded: int = 0
    all_time_uploaded: int = 0
    seeds: int = 0
    connections: int = 0
    pieces: int = 0
    total_pieces: int = 0
    tracker: str = ""
    queue_position: int = -1
    active_time: int = 0
    seeding_time: int = 0
    wasted: int = 0
    error: str = ""


class TorrentEngine:
    """Small thread-safe wrapper around libtorrent's session API."""

    TEMP_SUFFIX = ".torrpart"

    def __init__(self, state_dir: Path, download_dir: Path) -> None:
        try:
            import libtorrent as lt
        except ImportError as exc:
            raise EngineUnavailable(
                "The libtorrent Python binding is not installed. See README.md for setup."
            ) from exc

        self.lt = lt
        self.state_dir = state_dir
        self.download_dir = download_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.session = lt.session({
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "alert_mask": int(lt.alert.category_t.error_notification),
        })
        self._handles: dict[str, object] = {}
        self._sources: dict[str, dict[str, Any]] = {}
        self._startup_checks: set[str] = set()
        self._startup_checks_started: set[str] = set()
        self._temp_names_applied: set[str] = set()
        self._final_names_applied: set[str] = set()
        self._soft_paused: dict[str, tuple[int, bool]] = {}
        # Keep the session stopped while check-only handles are constructed.
        # Resuming the session lets disk checks run; stop_when_ready prevents
        # the subsequent download/seeding phase.
        self.session.pause()
        try:
            self._load()
        finally:
            self.session.resume()
        self.session.start_dht()

    def add(
        self,
        source: str,
        save_path: Path | None = None,
        *,
        start_paused: bool = False,
        check_only: bool = False,
        known_downloaded: int = 0,
    ) -> str:
        target = str((save_path or self.download_dir).expanduser().resolve())
        Path(target).mkdir(parents=True, exist_ok=True)
        with self._lock:
            if source.startswith("magnet:?"):
                params = self.lt.parse_magnet_uri(source)
                params.save_path = target
            elif source.startswith(("http://", "https://")):
                raise ValueError("Web torrent URLs are not supported; use a magnet link or download the .torrent file first.")
            else:
                torrent_path = Path(source).expanduser().resolve()
                if not torrent_path.is_file():
                    raise ValueError(f"Torrent file does not exist: {torrent_path}")
                info = self.lt.torrent_info(str(torrent_path))
                params = self.lt.add_torrent_params()
                params.ti = info
                params.save_path = target
            names_prepared = self._prepare_temporary_names(params, check_only=check_only)
            if check_only:
                # Allow disk verification, but atomically stop on the transition
                # to downloading/seeding. This is libtorrent's race-free mode
                # for "check files without starting the transfer".
                params.flags |= self.lt.torrent_flags.stop_when_ready
                params.flags &= ~self.lt.torrent_flags.paused
            elif start_paused:
                params.flags |= self.lt.torrent_flags.paused
                params.flags &= ~self.lt.torrent_flags.auto_managed
            handle = self.session.add_torrent(params)
            if check_only:
                handle.force_recheck()
            elif start_paused:
                # Belt-and-suspenders protection for libtorrent versions that
                # apply add flags asynchronously.
                handle.pause()
            key = self._key(handle)
            self._handles[key] = handle
            if names_prepared:
                self._temp_names_applied.add(key)
            if check_only:
                self._startup_checks.add(key)
            self._sources[key] = {
                "source": source,
                "save_path": target,
                "downloaded": max(0, int(known_downloaded)),
            }
            self._save_sources()
            return key

    def remove(self, key: str, delete_files: bool = False) -> None:
        with self._lock:
            handle = self._handles.pop(key, None)
            self._sources.pop(key, None)
            self._soft_paused.pop(key, None)
            if handle is not None:
                if delete_files:
                    self.session.remove_torrent(handle, self.lt.options_t.delete_files)
                else:
                    # The Python binding's strongly typed options enum cannot
                    # be constructed from integer 0; use the no-flags overload.
                    self.session.remove_torrent(handle)
            self._save_sources()

    def pause(self, key: str) -> None:
        with self._lock:
            if key in self._soft_paused:
                return
            handle = self._handles[key]
            if bool(handle.status().paused):
                return
            flags = handle.flags()
            was_upload_mode = bool(flags & self.lt.torrent_flags.upload_mode)
            self._soft_paused[key] = (int(handle.upload_limit()), was_upload_mode)
            handle.set_flags(self.lt.torrent_flags.upload_mode)
            # libtorrent requires at least two upload slots. A 1 B/s limit is
            # its smallest supported practical throttle while connections stay.
            handle.set_upload_limit(1)

    def stop(self, key: str) -> None:
        with self._lock:
            handle = self._handles[key]
            self._leave_soft_pause(key, handle)
            handle.unset_flags(self.lt.torrent_flags.auto_managed)
            handle.pause()

    def resume(self, key: str) -> None:
        with self._lock:
            handle = self._handles[key]
            self._leave_soft_pause(key, handle)
            handle.resume()

    def _leave_soft_pause(self, key: str, handle: object) -> None:
        settings = self._soft_paused.pop(key, None)
        if settings is None:
            return
        upload_limit, was_upload_mode = settings
        handle.set_upload_limit(upload_limit)
        if not was_upload_mode:
            handle.unset_flags(self.lt.torrent_flags.upload_mode)

    def snapshots(self) -> list[TorrentSnapshot]:
        rows: list[TorrentSnapshot] = []
        save_verified_state = False
        with self._lock:
            for key, handle in list(self._handles.items()):
                if not handle.is_valid():
                    continue
                s = handle.status()
                state = int(s.state)
                if key in self._startup_checks and state in (1, 7) and not bool(s.paused):
                    self._startup_checks_started.add(key)
                check_finished = (
                    key in self._startup_checks_started
                    and bool(s.paused)
                    and state in (3, 4, 5)
                )
                if check_finished:
                    self._startup_checks.discard(key)
                    self._startup_checks_started.discard(key)
                    save_verified_state = True
                self._manage_file_names(key, handle, s)
                total = max(int(s.total_wanted or 0), self._total_size(handle))
                checked_bytes = int(s.total_wanted_done or 0)
                if key in self._startup_checks:
                    # Once stop_when_ready lands in a transfer-ready state, the
                    # disk check is authoritative—even if files disappeared.
                    completed = checked_bytes
                else:
                    completed = max(
                        checked_bytes,
                        int(self._sources.get(key, {}).get("downloaded", 0) or 0),
                    )
                if state in (4, 5) or bool(getattr(s, "is_seeding", False)):
                    completed = total
                progress = completed / total if total > 0 else float(s.progress)
                self._sources[key]["downloaded"] = completed
                soft_paused = key in self._soft_paused
                stopped = bool(s.paused)
                finished = total > 0 and completed >= total
                if finished:
                    display_status = "Finished"
                elif soft_paused:
                    display_status = "Paused"
                elif stopped:
                    display_status = "Stopped"
                else:
                    display_status = self._state_name(s)
                rows.append(TorrentSnapshot(
                    key=key,
                    name=s.name or "Fetching metadata…",
                    progress=max(0.0, min(1.0, progress)),
                    status=display_status,
                    download_rate=int(s.download_rate),
                    upload_rate=int(s.upload_rate),
                    peers=int(s.num_peers),
                    total_size=total,
                    downloaded=completed,
                    save_path=self._sources.get(key, {}).get("save_path", ""),
                    paused=soft_paused,
                    stopped=stopped,
                    uploaded=int(getattr(s, "total_upload", 0) or 0),
                    all_time_downloaded=int(getattr(s, "all_time_download", 0) or 0),
                    all_time_uploaded=int(getattr(s, "all_time_upload", 0) or 0),
                    seeds=int(getattr(s, "num_seeds", 0) or 0),
                    connections=int(getattr(s, "num_connections", 0) or 0),
                    pieces=int(getattr(s, "num_pieces", 0) or 0),
                    total_pieces=self._total_pieces(handle),
                    tracker=str(getattr(s, "current_tracker", "") or ""),
                    queue_position=int(getattr(s, "queue_position", -1)),
                    active_time=int(getattr(s, "active_time", 0) or 0),
                    seeding_time=int(getattr(s, "seeding_time", 0) or 0),
                    wasted=int(getattr(s, "total_failed_bytes", 0) or 0) + int(getattr(s, "total_redundant_bytes", 0) or 0),
                    error=self._status_error(s),
                ))
            if save_verified_state:
                self._save_sources()
        return rows

    def close(self) -> None:
        with self._lock:
            # Refresh persisted completed-byte totals before writing state.
            self.snapshots()
            for key, handle in self._handles.items():
                if handle.is_valid():
                    try:
                        data = bytes(self.lt.bencode(handle.write_resume_data()))
                        (self.state_dir / f"{key}.fastresume").write_bytes(data)
                    except Exception:
                        pass
            self._save_sources()

    def _load(self) -> None:
        index = self.state_dir / "torrents.json"
        if not index.exists():
            return
        try:
            entries = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in entries:
            try:
                # Restored torrents verify their files, then stop before any
                # payload transfer until explicitly resumed by the user.
                self.add(
                    entry["source"],
                    Path(entry["save_path"]),
                    check_only=True,
                    known_downloaded=int(entry.get("downloaded", 0) or 0),
                )
            except Exception:
                continue

    def _save_sources(self) -> None:
        data = list(self._sources.values())
        (self.state_dir / "torrents.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _key(self, handle: object) -> str:
        return str(handle.info_hashes().v1 or handle.info_hashes().v2)

    def _prepare_temporary_names(self, params: object, *, check_only: bool) -> bool:
        """Apply incomplete names before add_torrent when metadata is known."""
        info = getattr(params, "ti", None)
        if not info:
            return False
        originals = self._original_names(info)
        if check_only:
            base = Path(str(params.save_path))
            # Existing final-name files must be checked under those names.
            # Missing files and existing .torrpart files use temporary paths.
            renames = {
                index: f"{path}{self.TEMP_SUFFIX}"
                for index, path in originals.items()
                if not (base / path).exists()
            }
        else:
            renames = {
                index: f"{path}{self.TEMP_SUFFIX}"
                for index, path in originals.items()
            }
        if not renames:
            return False
        try:
            params.renamed_files = renames
            return len(renames) == len(originals)
        except Exception:
            # Some older Python bindings do not expose map assignment. The
            # handle-based fallback runs on the first status poll.
            return False

    def _manage_file_names(self, key: str, handle: object, status: object) -> None:
        info = handle.torrent_file()
        if not info:
            return
        # Only transfer-ready completion states count. Progress may still read
        # 100% briefly while a forced startup recheck is in progress.
        state = int(getattr(status, "state", -1))
        if key in self._startup_checks:
            return
        finished = bool(getattr(status, "is_seeding", False)) or state in (4, 5)
        if finished:
            if key in self._final_names_applied:
                return
            for index, original in self._original_names(info).items():
                handle.rename_file(index, original)
            self._final_names_applied.add(key)
            self._temp_names_applied.discard(key)
            return
        if key not in self._temp_names_applied:
            for index, temporary in self._temporary_renames(info).items():
                handle.rename_file(index, temporary)
            self._temp_names_applied.add(key)

    def _temporary_renames(self, info: object) -> dict[int, str]:
        return {
            index: f"{path}{self.TEMP_SUFFIX}"
            for index, path in self._original_names(info).items()
            if not path.endswith(self.TEMP_SUFFIX)
        }

    @staticmethod
    def _original_names(info: object) -> dict[int, str]:
        files = info.files()
        names: dict[int, str] = {}
        for index in range(files.num_files()):
            try:
                if files.pad_file_at(index):
                    continue
            except (AttributeError, TypeError):
                pass
            names[index] = str(files.file_path(index))
        return names

    @staticmethod
    def _total_pieces(handle: object) -> int:
        try:
            info = handle.torrent_file()
            return int(info.num_pieces()) if info else 0
        except Exception:
            return 0

    @staticmethod
    def _total_size(handle: object) -> int:
        try:
            info = handle.torrent_file()
            return int(info.total_size()) if info else 0
        except Exception:
            return 0

    @staticmethod
    def _status_error(status: object) -> str:
        error = getattr(status, "errc", None)
        if not error:
            return ""
        try:
            return str(error.message())
        except Exception:
            return str(error)

    @staticmethod
    def _state_name(status: object) -> str:
        names = ["Queued", "Checking", "Metadata", "Downloading", "Finished", "Seeding", "Allocating", "Checking"]
        value = int(status.state)
        return names[value] if 0 <= value < len(names) else "Unknown"


class Poller:
    """Runs snapshot collection off the Tk event loop."""

    def __init__(self, engine: TorrentEngine, callback: Callable[[list[TorrentSnapshot]], None]) -> None:
        self.engine = engine
        self.callback = callback
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="torrent-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(1):
            self.callback(self.engine.snapshots())
