import unittest
from pathlib import Path

from torrent_client.app import TorrApp, human_bytes, human_duration, progress_text


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


if __name__ == "__main__":
    unittest.main()
