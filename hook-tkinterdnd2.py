"""PyInstaller hook that bundles tkinterdnd2's native TkDnD files."""

from PyInstaller.utils.hooks import collect_data_files


datas = collect_data_files("tkinterdnd2")
