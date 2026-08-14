# Torr

Torr is a compact Python desktop BitTorrent client. It accepts `.torrent` files and magnet links, shows transfer progress and peer counts, and supports pause, resume, removal, and restoring the torrent list between launches.

Use Torr only to download or share content you are legally entitled to transfer.

## Setup

Python 3.10–3.12 is recommended because prebuilt `libtorrent` packages may lag behind the newest Python release.

### macOS (Homebrew)

```bash
brew install libtorrent-rasterbar python@3.12
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

### Ubuntu / Debian

```bash
sudo apt install python3-libtorrent python3-tk
python3 app.py
```

If your distribution package is unavailable, create a virtual environment and run `python -m pip install -r requirements.txt`.

## Usage

- **Add torrent** opens a local `.torrent` file.
- **Add magnet** accepts a `magnet:?` link.
- Select a row to pause, resume, or remove it.
- Right-click a torrent for individual pause/resume controls, its download folder, or removal options.
- Each torrent displays a live progress bar and percentage in the torrent list.
- Choosing **Yes** in the removal dialog also deletes downloaded files; **No** preserves them.

Downloads default to `~/Downloads`, while app state is stored in `~/.torr/state`.
Previously added torrents are restored paused when Torr starts. Resume them individually when ready.
