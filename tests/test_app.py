import unittest
from pathlib import Path

from torrent_client.app import (
    TorrApp,
    eta_text,
    human_bytes,
    human_duration,
    load_preferences,
    progress_text,
    save_preferences,
    transfer_rate,
    torrent_drop_paths,
)


class HumanBytesTests(unittest.TestCase):
    def test_formats_bytes_and_rates(self):
        self.assertEqual(human_bytes(0), "0.0 B")
        self.assertEqual(human_bytes(1000), "1.0 kB")
        self.assertEqual(human_bytes(1500, "B/s"), "1.5 kB/s")

    def test_formats_and_clamps_progress(self):
        self.assertEqual(progress_text(0.425), "42.5%")
        self.assertEqual(progress_text(-1), "0.0%")
        self.assertEqual(progress_text(2), "100.0%")

    def test_formats_duration(self):
        self.assertEqual(human_duration(0), "0s")
        self.assertEqual(human_duration(3661), "1h 1m 1s")
        self.assertEqual(human_duration(90061), "1d 1h 1m 1s")

    def test_hides_stale_transfer_values_when_inactive(self):
        self.assertEqual(transfer_rate(40, inactive=True), "0.0 B/s")
        self.assertEqual(eta_text(1000, 40, inactive=True), "—")

    def test_formats_active_transfer_values(self):
        self.assertEqual(transfer_rate(1500, inactive=False), "1.5 kB/s")
        self.assertEqual(eta_text(3661, 1, inactive=False), "1h 1m 1s")


class FakeEngine:
    def __init__(self):
        self.added = None

    def add(self, source, destination, *, start_paused=False):
        self.added = (source, destination, start_paused)


class AddTorrentTests(unittest.TestCase):
    def test_ui_adds_new_torrent_stopped(self):
        app = object.__new__(TorrApp)
        app.engine = FakeEngine()
        destination = Path("/tmp/downloads")

        app._safe_add("example.torrent", destination)

        self.assertEqual(app.engine.added, ("example.torrent", destination, True))

    def test_filters_dropped_files_to_torrents(self):
        with self.subTest("case-insensitive extension"):
            paths = torrent_drop_paths([__file__, str(Path(__file__).with_suffix(".TORRENT"))])
            self.assertEqual(paths, [])

        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            torrent = Path(directory) / "example.TORRENT"
            other = Path(directory) / "notes.txt"
            torrent.touch()
            other.touch()
            self.assertEqual(torrent_drop_paths([str(torrent), str(other)]), [torrent])


class PreferenceTests(unittest.TestCase):
    def test_round_trips_remembered_directories(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            expected = {
                "last_torrent_directory": str(Path(directory) / "torrents"),
                "last_download_directory": str(Path(directory) / "downloads"),
            }

            save_preferences(path, expected)

            self.assertEqual(load_preferences(path), expected)

    def test_invalid_preferences_are_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_preferences(path), {})


if __name__ == "__main__":
    unittest.main()
