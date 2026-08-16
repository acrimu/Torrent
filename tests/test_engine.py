import unittest
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace

from torrent_client.engine import TorrentEngine


class FakeFiles:
    paths = ("release/video.mkv", "release/readme.txt", "release/.pad/0")

    def num_files(self):
        return len(self.paths)

    def file_path(self, index):
        return self.paths[index]

    def pad_file_at(self, index):
        return index == 2


class FakeInfo:
    def files(self):
        return FakeFiles()

    def total_size(self):
        return 1234


class TemporaryNameTests(unittest.TestCase):
    def test_appends_suffix_and_preserves_directories(self):
        engine = object.__new__(TorrentEngine)
        self.assertEqual(
            engine._temporary_renames(FakeInfo()),
            {
                0: "release/video.mkv.torrpart",
                1: "release/readme.txt.torrpart",
            },
        )

    def test_original_names_exclude_padding_files(self):
        self.assertEqual(
            TorrentEngine._original_names(FakeInfo()),
            {0: "release/video.mkv", 1: "release/readme.txt"},
        )

    def test_restore_checks_existing_final_names(self):
        engine = object.__new__(TorrentEngine)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "release").mkdir()
            (base / "release/video.mkv").touch()
            params = SimpleNamespace(ti=FakeInfo(), save_path=str(base), renamed_files=None)

            all_temporary = engine._prepare_temporary_names(params, check_only=True)

            self.assertFalse(all_temporary)
            self.assertNotIn(0, params.renamed_files)
            self.assertEqual(params.renamed_files[1], "release/readme.txt.torrpart")


class FakeHandle:
    def __init__(self):
        self.current_flags = 0
        self.limit = -1
        self.resumed = False

    def status(self):
        return SimpleNamespace(paused=False)

    def flags(self):
        return self.current_flags

    def upload_limit(self):
        return self.limit

    def set_upload_limit(self, value):
        self.limit = value

    def set_flags(self, value):
        self.current_flags |= value

    def unset_flags(self, value):
        self.current_flags &= ~value

    def resume(self):
        self.resumed = True


class PauseTests(unittest.TestCase):
    def test_pause_throttles_and_resume_restores(self):
        engine = object.__new__(TorrentEngine)
        engine._lock = threading.RLock()
        engine.lt = SimpleNamespace(torrent_flags=SimpleNamespace(upload_mode=1))
        engine._soft_paused = {}
        handle = FakeHandle()
        engine._handles = {"key": handle}

        engine.pause("key")
        self.assertEqual(handle.limit, 1)
        self.assertEqual(handle.current_flags, 1)

        engine.resume("key")
        self.assertEqual(handle.limit, -1)
        self.assertEqual(handle.current_flags, 0)
        self.assertTrue(handle.resumed)


class FakeSession:
    def __init__(self):
        self.calls = []

    def remove_torrent(self, *args):
        self.calls.append(args)


class RemoveTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(TorrentEngine)
        engine._lock = threading.RLock()
        engine._handles = {"key": "handle"}
        engine._sources = {"key": {}}
        engine._soft_paused = {}
        engine.session = FakeSession()
        engine.lt = SimpleNamespace(options_t=SimpleNamespace(delete_files="delete-files"))
        engine._save_sources = lambda: None
        return engine

    def test_remove_keeping_files_uses_no_options_overload(self):
        engine = self.make_engine()
        engine.remove("key", delete_files=False)
        self.assertEqual(engine.session.calls, [("handle",)])

    def test_remove_deleting_files_passes_delete_flag(self):
        engine = self.make_engine()
        engine.remove("key", delete_files=True)
        self.assertEqual(engine.session.calls, [("handle", "delete-files")])


if __name__ == "__main__":
    unittest.main()
