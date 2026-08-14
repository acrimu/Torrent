from __future__ import annotations

import queue
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .engine import EngineUnavailable, Poller, TorrentEngine, TorrentSnapshot


def human_bytes(value: int, suffix: str = "B") -> str:
    amount = float(value)
    for unit in ("", "Ki", "Mi", "Gi", "Ti"):
        if abs(amount) < 1024:
            return f"{amount:.1f} {unit}{suffix}"
        amount /= 1024
    return f"{amount:.1f} Pi{suffix}"


def progress_text(progress: float) -> str:
    return f"{max(0.0, min(1.0, progress)):.1%}"


class TorrApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Torr")
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
        ttk.Button(toolbar, text="Resume", command=lambda: self._toggle(False)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="Remove", command=self._remove).pack(side="left", padx=4)

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))
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

        self.context_menu = tk.Menu(self, tearoff=False)
        self.context_menu.add_command(label="Pause", command=lambda: self._toggle(True))
        self.context_menu.add_command(label="Resume", command=lambda: self._toggle(False))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Open download folder", command=self._open_download_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Remove (keep files)", command=lambda: self._remove_selected(False))
        self.context_menu.add_command(label="Remove and delete files…", command=lambda: self._remove_selected(True))
        self.status = tk.StringVar(value="Starting torrent engine…")
        ttk.Label(self, textvariable=self.status, padding=(10, 6)).pack(fill="x")

    def _start_engine(self) -> None:
        base = Path.home() / ".torr"
        try:
            self.engine = TorrentEngine(base / "state", Path.home() / "Downloads")
        except EngineUnavailable as exc:
            self.status.set(str(exc))
            self.after(250, lambda: messagebox.showerror("Torrent engine unavailable", str(exc)))
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
                values = (row.name, human_bytes(row.total_size), "", progress_text(row.progress), row.status,
                          human_bytes(row.download_rate, "B/s"), human_bytes(row.upload_rate, "B/s"), row.peers)
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
            self.status.set(f"{len(rows)} torrent{'s' if len(rows) != 1 else ''}")
        self.after(250, self._drain_updates)

    def _require_engine(self) -> TorrentEngine | None:
        if self.engine is None:
            messagebox.showerror("Torrent engine unavailable", "Install python-libtorrent, then restart Torr.")
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

    def _safe_add(self, source: str, destination: Path) -> None:
        try:
            assert self.engine is not None
            self.engine.add(source, destination)
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
        self.context_menu.entryconfigure("Pause", state="disabled" if snapshot and snapshot.paused else "normal")
        self.context_menu.entryconfigure("Resume", state="normal" if snapshot and snapshot.paused else "disabled")
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

    def _close(self) -> None:
        if self.poller:
            self.poller.stop()
        if self.engine:
            self.engine.close()
        self.destroy()


def main() -> None:
    TorrApp().mainloop()
