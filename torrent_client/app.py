from __future__ import annotations

import queue
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Keep imports/test helpers usable before dependencies install.
    DND_FILES = None
    DragDropTk = tk.Tk
else:
    DragDropTk = TkinterDnD.Tk

from . import __version__
from .engine import EngineUnavailable, Poller, TorrentEngine, TorrentSnapshot


def human_bytes(value: int, suffix: str = "B") -> str:
    amount = float(value)
    for unit in ("", "k", "M", "G", "T"):
        if abs(amount) < 1000:
            return f"{amount:.1f} {unit}{suffix}"
        amount /= 1000
    return f"{amount:.1f} P{suffix}"


def progress_text(progress: float) -> str:
    return f"{max(0.0, min(1.0, progress)):.1%}"


def human_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = ([f"{days}d"] if days else []) + ([f"{hours}h"] if hours or days else []) + ([f"{minutes}m"] if minutes or hours or days else []) + [f"{seconds}s"]
    return " ".join(parts)


def transfer_rate(value: int, *, inactive: bool) -> str:
    """Hide libtorrent's briefly stale rate after pausing or stopping."""
    return human_bytes(0 if inactive else value, "B/s")


def eta_text(remaining: int, download_rate: int, *, inactive: bool) -> str:
    if inactive or download_rate <= 0 or remaining <= 0:
        return "—"
    return human_duration(remaining / download_rate)


def torrent_drop_paths(paths: tuple[str, ...] | list[str]) -> list[Path]:
    return [
        path
        for raw_path in paths
        if (path := Path(raw_path)).is_file() and path.suffix.lower() == ".torrent"
    ]


class TorrApp(DragDropTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"TorrentClient {__version__}")
        self.geometry("1080x620")
        self.minsize(820, 420)
        self._updates: queue.Queue[list[TorrentSnapshot]] = queue.Queue(maxsize=1)
        self._snapshots: dict[str, TorrentSnapshot] = {}
        self._progress_bars: dict[str, tk.Canvas] = {}
        self.engine: TorrentEngine | None = None
        self.poller: Poller | None = None
        self._build()
        self._start_engine()
        self.after(150, self._drain_updates)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Add torrent", command=self._add_file).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Add magnet", command=self._add_magnet).pack(side="left", padx=6)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(toolbar, text="Pause", command=lambda: self._toggle(True)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Stop", command=self._stop_selected).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Start / Resume", command=lambda: self._toggle(False)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Remove", command=self._remove).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Select files…", command=self._select_files).pack(side="left", padx=4)

        content = ttk.Panedwindow(self, orient="vertical")
        content.pack(fill="both", expand=True, padx=10, pady=(0, 8))
        table_frame = ttk.Frame(content)
        details_frame = ttk.Frame(content, padding=(4, 8))
        # The list absorbs window-size changes; the inspector keeps its height
        # and remains anchored above the bottom status line.
        content.add(table_frame, weight=1)
        content.add(details_frame, weight=0)
        columns = ("name", "size", "progress", "percent", "status", "down", "up", "peers")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        labels = {"name": "Name", "size": "Size", "progress": "Progress", "percent": "%", "status": "Status", "down": "Download", "up": "Upload", "peers": "Peers"}
        widths = {"name": 300, "size": 90, "progress": 120, "percent": 70, "status": 110, "down": 100, "up": 100, "peers": 60}
        for col in columns:
            self.table.heading(col, text=labels[col])
            self.table.column(col, width=widths[col], anchor="w" if col == "name" else "center")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self._scroll_table)
        self.table.configure(yscrollcommand=lambda first, last: self._on_table_scroll(scroll, first, last))
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.bind("<Button-3>", self._show_context_menu)
        self.table.bind("<Button-2>", self._show_context_menu)
        self.table.bind("<Control-Button-1>", self._show_context_menu)
        self.table.bind("<Configure>", lambda _event: self.after_idle(self._position_progress_bars))
        self.table.bind("<<TreeviewSelect>>", lambda _event: self._refresh_details())
        self._setup_drop_target()

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="Pause", command=lambda: self._toggle(True))
        self.context_menu.add_command(label="Stop", command=self._stop_selected)
        self.context_menu.add_command(label="Start / Resume", command=lambda: self._toggle(False))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Open download folder", command=self._open_download_folder)
        self.context_menu.add_command(label="Select files…", command=self._select_files)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Remove (keep files)", command=lambda: self._remove_selected(False))
        self.context_menu.add_command(label="Remove and delete files…", command=lambda: self._remove_selected(True))

        self._build_details(details_frame)
        self.status = tk.StringVar(value="Starting torrent engine…")
        ttk.Label(self, textvariable=self.status, padding=(10, 6)).pack(fill="x")

    def _start_engine(self) -> None:
        base = Path.home() / ".torr"
        try:
            self.engine = TorrentEngine(base / "state", Path.home() / "Downloads")
        except EngineUnavailable as exc:
            message = str(exc)
            self.status.set(message)
            # Exception variables are cleared when the except block exits.
            # Capture the plain string for this delayed Tkinter callback.
            self.after(
                250,
                lambda message=message: messagebox.showerror(
                    "Torrent engine unavailable", message
                ),
            )
            return
        self.poller = Poller(self.engine, self._queue_update)
        self.poller.start()
        self.status.set("Ready")

    def _queue_update(self, rows: list[TorrentSnapshot]) -> None:
        try:
            self._updates.put_nowait(rows)
        except queue.Full:
            try:
                self._updates.get_nowait()
            except queue.Empty:
                pass
            self._updates.put_nowait(rows)

    def _drain_updates(self) -> None:
        try:
            rows = self._updates.get_nowait()
        except queue.Empty:
            pass
        else:
            self._snapshots = {row.key: row for row in rows}
            live = set()
            for row in rows:
                live.add(row.key)
                inactive = row.paused or row.stopped
                values = (row.name, human_bytes(row.total_size), "", progress_text(row.progress), row.status,
                          transfer_rate(row.download_rate, inactive=inactive),
                          transfer_rate(row.upload_rate, inactive=inactive), row.peers)
                if self.table.exists(row.key):
                    self.table.item(row.key, values=values)
                else:
                    self.table.insert("", "end", iid=row.key, values=values)
            for item in self.table.get_children():
                if item not in live:
                    self.table.delete(item)
                    bar = self._progress_bars.pop(item, None)
                    if bar:
                        bar.destroy()
            self._sync_progress_bars(rows)
            self._refresh_details()
            self.status.set(f"{len(rows)} torrent{'s' if len(rows) != 1 else ''}")
        self.after(250, self._drain_updates)

    def _require_engine(self) -> TorrentEngine | None:
        if self.engine is None:
            messagebox.showerror("Torrent engine unavailable", "Install python-libtorrent, then restart TorrentClient.")
        return self.engine

    def _choose_destination(self) -> Path | None:
        path = filedialog.askdirectory(title="Choose download folder", initialdir=str(Path.home() / "Downloads"))
        return Path(path) if path else None

    def _add_file(self) -> None:
        engine = self._require_engine()
        if not engine:
            return
        source = filedialog.askopenfilename(title="Open torrent", filetypes=[("Torrent files", "*.torrent"), ("All files", "*")])
        if source:
            destination = self._choose_destination()
            if destination:
                self._safe_add(source, destination)

    def _add_magnet(self) -> None:
        engine = self._require_engine()
        if not engine:
            return
        source = simpledialog.askstring("Add magnet", "Paste a magnet link:", parent=self)
        if source:
            destination = self._choose_destination()
            if destination:
                self._safe_add(source.strip(), destination)

    def _setup_drop_target(self) -> None:
        if DND_FILES is None:
            return
        for widget in (self, self.table):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_torrent_drop)

    def _on_torrent_drop(self, event: tk.Event) -> str:
        paths = torrent_drop_paths(list(self.tk.splitlist(event.data)))
        if not paths:
            messagebox.showwarning(
                "No torrent files",
                "Drop one or more files with the .torrent extension.",
            )
            return getattr(event, "action", "copy")
        destination = self._choose_destination()
        if destination:
            for path in paths:
                self._safe_add(str(path), destination)
        return getattr(event, "action", "copy")

    def _safe_add(self, source: str, destination: Path) -> None:
        try:
            assert self.engine is not None
            # Adding a torrent must never implicitly start network transfer.
            self.engine.add(source, destination, start_paused=True)
        except Exception as exc:
            messagebox.showerror("Could not add torrent", str(exc))

    def _selected(self) -> str | None:
        chosen = self.table.selection()
        return chosen[0] if chosen else None

    def _toggle(self, pause: bool) -> None:
        key = self._selected()
        if key and self.engine:
            try:
                (self.engine.pause if pause else self.engine.resume)(key)
            except Exception as exc:
                messagebox.showerror("Could not update torrent", str(exc))

    def _stop_selected(self) -> None:
        key = self._selected()
        if key and self.engine:
            try:
                self.engine.stop(key)
            except Exception as exc:
                messagebox.showerror("Could not stop torrent", str(exc))

    def _remove(self) -> None:
        key = self._selected()
        if not key or not self.engine:
            return
        delete = messagebox.askyesnocancel("Remove torrent", "Also delete the downloaded files?\n\nYes: remove files\nNo: keep files")
        if delete is not None:
            self.engine.remove(key, delete_files=delete)

    def _remove_selected(self, delete_files: bool) -> None:
        key = self._selected()
        if not key or not self.engine:
            return
        if delete_files:
            name = self._snapshots.get(key, TorrentSnapshot(key, "this torrent", 0, "")).name
            confirmed = messagebox.askyesno(
                "Delete torrent data",
                f'Remove "{name}" and permanently delete its downloaded files?',
                icon="warning",
            )
            if not confirmed:
                return
        try:
            self.engine.remove(key, delete_files=delete_files)
        except Exception as exc:
            messagebox.showerror("Could not remove torrent", str(exc))

    def _show_context_menu(self, event: tk.Event) -> str | None:
        key = self.table.identify_row(event.y)
        if not key:
            return None
        self._popup_context_menu(key, event.x_root, event.y_root)
        return "break"

    def _show_bar_context_menu(self, event: tk.Event, key: str) -> str:
        self._popup_context_menu(key, event.x_root, event.y_root)
        return "break"

    def _popup_context_menu(self, key: str, x_root: int, y_root: int) -> None:
        self.table.selection_set(key)
        self.table.focus(key)
        snapshot = self._snapshots.get(key)
        inactive = bool(snapshot and (snapshot.paused or snapshot.stopped))
        self.context_menu.entryconfigure("Pause", state="disabled" if inactive else "normal")
        self.context_menu.entryconfigure("Stop", state="disabled" if snapshot and snapshot.stopped else "normal")
        self.context_menu.entryconfigure("Start / Resume", state="normal" if inactive else "disabled")
        try:
            self.context_menu.tk_popup(x_root, y_root)
        finally:
            self.context_menu.grab_release()

    def _open_download_folder(self) -> None:
        key = self._selected()
        snapshot = self._snapshots.get(key or "")
        if not snapshot or not snapshot.save_path:
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", snapshot.save_path])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", snapshot.save_path])
            else:
                subprocess.Popen(["xdg-open", snapshot.save_path])
        except OSError as exc:
            messagebox.showerror("Could not open folder", str(exc))

    def _select_files(self) -> None:
        key = self._selected()
        if not key or not self.engine:
            return
        try:
            files = self.engine.files(key)
        except Exception as exc:
            messagebox.showerror("Could not read file list", str(exc))
            return
        if not files:
            messagebox.showinfo(
                "File list unavailable",
                "Torrent metadata has not been fetched yet. Start the torrent to fetch "
                "its metadata, then stop it and open Select files again.",
            )
            return

        dialog = tk.Toplevel(self)
        dialog.title("Select files to download")
        dialog.geometry("760x460")
        dialog.minsize(520, 320)
        dialog.transient(self)
        dialog.grab_set()

        ttk.Label(
            dialog,
            text="Choose which files belong to this download. Double-click a row to toggle it.",
            padding=(10, 10, 10, 6),
        ).pack(fill="x")
        frame = ttk.Frame(dialog, padding=(10, 0, 10, 8))
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(
            frame,
            columns=("selected", "size", "path"),
            show="headings",
            selectmode="extended",
        )
        tree.heading("selected", text="Download")
        tree.heading("size", text="Size")
        tree.heading("path", text="File")
        tree.column("selected", width=85, stretch=False, anchor="center")
        tree.column("size", width=95, stretch=False, anchor="e")
        tree.column("path", width=500, anchor="w")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        selected = {item.index for item in files if item.selected}
        for item in files:
            tree.insert(
                "",
                "end",
                iid=str(item.index),
                values=("Yes" if item.selected else "No", human_bytes(item.size), item.path),
            )

        def set_rows(indices: set[int], enabled: bool) -> None:
            for index in indices:
                if enabled:
                    selected.add(index)
                else:
                    selected.discard(index)
                values = list(tree.item(str(index), "values"))
                values[0] = "Yes" if enabled else "No"
                tree.item(str(index), values=values)

        def toggle(_event: tk.Event | None = None) -> None:
            indices = {int(item) for item in tree.selection()}
            if not indices:
                return
            enable = any(index not in selected for index in indices)
            set_rows(indices, enable)

        def apply_selection() -> None:
            if not selected and not messagebox.askyesno(
                "Download no files?",
                "No files are selected. Keep this torrent without downloading its files?",
                parent=dialog,
            ):
                return
            try:
                assert self.engine is not None
                self.engine.select_files(key, selected)
            except Exception as exc:
                messagebox.showerror("Could not update file selection", str(exc), parent=dialog)
                return
            dialog.destroy()

        tree.bind("<Double-1>", toggle)
        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        all_indices = {item.index for item in files}
        ttk.Button(buttons, text="Select all", command=lambda: set_rows(all_indices, True)).pack(side="left")
        ttk.Button(buttons, text="Select none", command=lambda: set_rows(all_indices, False)).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply", command=apply_selection).pack(side="right", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        tree.focus_set()

    def _scroll_table(self, *args: str) -> None:
        self.table.yview(*args)
        self.after_idle(self._position_progress_bars)

    def _on_table_scroll(self, scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        scrollbar.set(first, last)
        self.after_idle(self._position_progress_bars)

    def _sync_progress_bars(self, rows: list[TorrentSnapshot]) -> None:
        for row in rows:
            bar = self._progress_bars.get(row.key)
            if bar is None:
                bar = tk.Canvas(self.table, height=18, highlightthickness=0, borderwidth=0)
                bar.bind("<Button-3>", lambda event, key=row.key: self._show_bar_context_menu(event, key))
                bar.bind("<Button-2>", lambda event, key=row.key: self._show_bar_context_menu(event, key))
                bar.bind("<Control-Button-1>", lambda event, key=row.key: self._show_bar_context_menu(event, key))
                self._progress_bars[row.key] = bar
            bar.delete("all")
            width = max(bar.winfo_width(), 80)
            bar.create_rectangle(0, 0, width, 18, fill="#e4e7eb", outline="")
            bar.create_rectangle(0, 0, width * row.progress, 18, fill="#2d8cff", outline="")
        self.after_idle(self._position_progress_bars)

    def _position_progress_bars(self) -> None:
        for key, bar in self._progress_bars.items():
            box = self.table.bbox(key, "progress")
            if not box:
                bar.place_forget()
                continue
            x, y, width, height = box
            bar.place(x=x + 3, y=y + 2, width=max(1, width - 6), height=max(1, height - 4))

    def _build_details(self, parent: ttk.Frame) -> None:
        self.detail_title = tk.StringVar(value="Select a torrent to see its details")
        ttk.Label(parent, textvariable=self.detail_title, font=("TkDefaultFont", 12, "bold")).pack(fill="x", pady=(0, 7))
        groups = ttk.Frame(parent)
        groups.pack(fill="both", expand=True)
        self.detail_values: dict[str, tk.StringVar] = {}
        definitions = (
            ("General", (("Status", "status"), ("Save location", "path"), ("Info hash", "hash"), ("Tracker", "tracker"), ("Error", "error"))),
            ("Transfer", (("Downloaded", "downloaded"), ("Uploaded", "uploaded"), ("Ratio", "ratio"), ("Remaining", "remaining"), ("ETA", "eta"), ("Wasted", "wasted"))),
            ("Activity", (("Seeds / peers", "swarm"), ("Connections", "connections"), ("Pieces", "pieces"), ("Queue position", "queue"), ("Active time", "active"), ("Seeding time", "seeding"))),
        )
        for column, (title, fields) in enumerate(definitions):
            frame = ttk.LabelFrame(groups, text=title, padding=8)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0))
            # General contains long paths, hashes, trackers, and errors, so it
            # receives half of the inspector width. Transfer and Activity split
            # the remaining half equally.
            groups.columnconfigure(
                column,
                weight=2 if column == 0 else 1,
                uniform="torrent-details",
            )
            for row, (label, key) in enumerate(fields):
                value = tk.StringVar(value="—")
                self.detail_values[key] = value
                ttk.Label(frame, text=f"{label}:").grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
                ttk.Label(
                    frame,
                    textvariable=value,
                    wraplength=300,
                    anchor="nw",
                    justify="left",
                ).grid(row=row, column=1, sticky="new", pady=2)
            frame.columnconfigure(1, weight=1)

    def _refresh_details(self) -> None:
        key = self._selected()
        snapshot = self._snapshots.get(key or "")
        if not snapshot:
            self.detail_title.set("Select a torrent to see its details")
            for value in self.detail_values.values():
                value.set("—")
            return
        remaining = max(0, snapshot.total_size - snapshot.downloaded)
        inactive = snapshot.paused or snapshot.stopped
        eta = eta_text(remaining, snapshot.download_rate, inactive=inactive)
        # total_wanted_done represents all completed torrent payload currently
        # present, whereas all_time_download only covers this libtorrent session.
        downloaded = snapshot.downloaded
        uploaded = snapshot.all_time_uploaded or snapshot.uploaded
        ratio = uploaded / downloaded if downloaded else 0.0
        self.detail_title.set(snapshot.name)
        values = {
            "status": snapshot.status,
            "path": snapshot.save_path or "—",
            "hash": snapshot.key,
            "tracker": snapshot.tracker or "—",
            "error": snapshot.error or "None",
            "downloaded": human_bytes(downloaded),
            "uploaded": human_bytes(uploaded),
            "ratio": f"{ratio:.3f}",
            "remaining": human_bytes(remaining),
            "eta": eta,
            "wasted": human_bytes(snapshot.wasted),
            "swarm": f"{snapshot.seeds} seeds / {snapshot.peers} peers",
            "connections": str(snapshot.connections),
            "pieces": f"{snapshot.pieces} / {snapshot.total_pieces or '—'}",
            "queue": str(snapshot.queue_position) if snapshot.queue_position >= 0 else "—",
            "active": human_duration(snapshot.active_time),
            "seeding": human_duration(snapshot.seeding_time),
        }
        for key, value in values.items():
            self.detail_values[key].set(value)

    def _close(self) -> None:
        if self.poller:
            self.poller.stop()
        if self.engine:
            self.engine.close()
        self.destroy()


def main() -> None:
    TorrApp().mainloop()
