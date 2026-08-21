# TorrentClient

A compact desktop BitTorrent client written in Python with Tkinter and
libtorrent.

TorrentClient supports `.torrent` files and magnet links, provides live transfer details,
and deliberately avoids starting downloads without an explicit user action.

> Use TorrentClient only to download or share content you are legally entitled to
> transfer.

## Features

- Add local `.torrent` files and magnet links
- Select which files to download from multi-file torrents
- Start, softly pause, stop, and resume torrents individually
- Live progress bars, transfer rates, peer counts, and percentages
- Detailed torrent inspector with size, verified bytes, ratio, ETA, tracker,
  info hash, pieces, queue position, and errors
- Right-click context menu for individual torrent actions
- Configurable download destination
- Persistent torrent list across application restarts
- Hash-checking of restored torrents when Start / Resume is clicked
- `.torrpart` filenames while payload files are incomplete
- Optional removal of downloaded data

## Transfer behavior

TorrentClient is intentionally conservative about starting network activity:

- A newly added torrent starts in the **Stopped** state.
- **Start / Resume** explicitly begins downloading or seeding.
- **Pause** keeps peer connections available but prevents new downloads and
  throttles uploads to the minimum supported rate.
- **Stop** disconnects peers and stops tracker activity.
- When TorrentClient starts, existing torrents remain stopped and are not
  hash-checked automatically.
- The first **Start / Resume** after launch verifies existing files before any
  downloading or seeding begins.
- A fully verified torrent displays **Finished**, even though it remains
  internally stopped until the user starts seeding it.

## Requirements

- Python 3.10 or newer
- Tkinter
- libtorrent 2.x Python bindings

Python 3.10–3.12 is recommended because prebuilt libtorrent wheels may not be
available immediately for the newest Python releases.

## Installation

Clone the repository and enter its directory:

```bash
git clone <repository-url>
cd Torrent
```

### macOS

Install the native dependency and a supported Python version with Homebrew:

```bash
brew install libtorrent-rasterbar python@3.12
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `pip` cannot find a compatible libtorrent wheel, use a Python version for
which one is available or build the bindings against Homebrew's
`libtorrent-rasterbar` installation.

### Ubuntu / Debian

Use the distribution packages:

```bash
sudo apt update
sudo apt install python3-libtorrent python3-tk
```

Alternatively, create a virtual environment and install the project dependency:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Running TorrentClient

From the repository root:

```bash
python TorrentClient.py
```

The project can also be installed as a command:

```bash
python -m pip install -e .
torr
```

### Building a Windows executable

With PyInstaller installed, run:

```powershell
pyinstaller TorrentClient.spec
```

The executable is created at `dist\TorrentClient.exe`.

## Usage

1. Select **Add torrent** or **Add magnet**.
2. Choose the destination directory.
3. Select the stopped torrent and click **Start / Resume**.
4. Click a torrent to inspect its live details in the bottom panel.
5. Right-click a torrent for pause, stop, start, folder, and removal actions.

For a multi-file torrent, select it and click **Select files…** before starting
the download. Magnet links need to fetch their metadata before the file list is
available; start the magnet briefly, stop it after metadata appears, and then
choose its files.

Removing a torrent can either preserve its downloaded files or delete them.
Deletion requires explicit confirmation.

## Files and application state

Downloads default to:

```text
~/Downloads
```

The saved torrent list and engine state are stored under:

```text
~/.torr/state
```

Incomplete payload files receive a `.torrpart` suffix. For example:

```text
ubuntu.iso.torrpart
```

TorrentClient restores the original metadata-defined filenames after the entire torrent
has finished.

## Testing

The test suite uses Python's standard library and does not require pytest:

```bash
python -m unittest discover -s tests -v
```

To perform a syntax check as well:

```bash
python -m compileall -q TorrentClient.py torrent_client tests
python -m unittest discover -s tests -v
```

## Project structure

```text
Torrent/
├── TorrentClient.py          # Application entry point
├── torrent_client/
│   ├── app.py                # Tkinter interface
│   └── engine.py             # libtorrent integration and persistence
├── tests/                    # Unit and regression tests
├── pyproject.toml            # Package metadata and command entry point
└── requirements.txt          # Runtime dependency
```

## Troubleshooting

### `No module named 'libtorrent'`

The Python interpreter running TorrentClient cannot find the libtorrent binding. Confirm
that installation and execution use the same interpreter:

```bash
python -c "import sys, libtorrent; print(sys.executable, libtorrent.__version__)"
```

### The interface still shows old behavior

Python does not reload edited modules in an already running application. Fully
quit TorrentClient and launch it again from the repository root.

### A full-size file is not 100% complete

Torrent payload files may be preallocated to their final size. TorrentClient reports
completion from verified piece hashes, not from filesystem length, so a
full-size file can still be incomplete or corrupted.

## Development status

TorrentClient is a small desktop project under active development. Back up important data
and verify the selected destination before using any option that deletes files.

## License

This project is licensed under the **MIT License**. See the `LICENSE` file for details.
