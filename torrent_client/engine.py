from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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


class TorrentEngine:
    """Small thread-safe wrapper around libtorrent's session API."""

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
        self.session.start_dht()
        self._handles: dict[str, object] = {}
        self._sources: dict[str, dict[str, str]] = {}
        self._load()

    def add(self, source: str, save_path: Path | None = None, *, start_paused: bool = False) -> str:
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
            if start_paused:
                params.flags |= self.lt.torrent_flags.paused
                params.flags &= ~self.lt.torrent_flags.auto_managed
            handle = self.session.add_torrent(params)
            key = self._key(handle)
            self._handles[key] = handle
            self._sources[key] = {"source": source, "save_path": target}
            self._save_sources()
            return key

    def remove(self, key: str, delete_files: bool = False) -> None:
        with self._lock:
            handle = self._handles.pop(key, None)
            self._sources.pop(key, None)
            if handle is not None:
                flags = self.lt.options_t.delete_files if delete_files else self.lt.options_t(0)
                self.session.remove_torrent(handle, flags)
            self._save_sources()

    def pause(self, key: str) -> None:
        with self._lock:
            self._handles[key].pause()

    def resume(self, key: str) -> None:
        with self._lock:
            self._handles[key].resume()

    def snapshots(self) -> list[TorrentSnapshot]:
        rows: list[TorrentSnapshot] = []
        with self._lock:
            for key, handle in list(self._handles.items()):
                if not handle.is_valid():
                    continue
                s = handle.status()
                total = int(s.total_wanted or 0)
                rows.append(TorrentSnapshot(
                    key=key,
                    name=s.name or "Fetching metadata…",
                    progress=max(0.0, min(1.0, float(s.progress))),
                    status=self._state_name(s),
                    download_rate=int(s.download_rate),
                    upload_rate=int(s.upload_rate),
                    peers=int(s.num_peers),
                    total_size=total,
                    downloaded=int(s.total_wanted_done or 0),
                    save_path=self._sources.get(key, {}).get("save_path", ""),
                    paused=bool(s.paused),
                ))
        return rows

    def close(self) -> None:
        with self._lock:
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
                # Restored torrents must not transfer anything until the user
                # explicitly resumes them in this new app session.
                self.add(entry["source"], Path(entry["save_path"]), start_paused=True)
            except Exception:
                continue

    def _save_sources(self) -> None:
        data = list(self._sources.values())
        (self.state_dir / "torrents.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _key(self, handle: object) -> str:
        return str(handle.info_hashes().v1 or handle.info_hashes().v2)

    @staticmethod
    def _state_name(status: object) -> str:
        if status.paused:
            return "Paused"
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
