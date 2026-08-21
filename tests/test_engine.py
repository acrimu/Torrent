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

    def file_size(self, index):
        return (1000, 200, 50)[index]

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

    def test_selected_files_are_renamed_when_wanted_bytes_finish(self):
        engine = object.__new__(TorrentEngine)
        engine._startup_checks = set()
        engine._final_names_applied = set()
        engine._temp_names_applied = {"key"}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "release").mkdir()
            (base / "release/video.mkv.torrpart").touch()
            engine._sources = {"key": {"save_path": str(base)}}

            class RenameHandle:
                def __init__(self):
                    self.renamed = []

                def torrent_file(self):
                    return FakeInfo()

                def file_priorities(self):
                    return [4, 0, 0]

                def rename_file(self, index, path):
                    self.renamed.append((index, path))

            handle = RenameHandle()
            status = SimpleNamespace(
                state=3,
                is_seeding=False,
                total_wanted=1000,
                total_wanted_done=1000,
            )

            engine._manage_file_names("key", handle, status)

            self.assertEqual(handle.renamed, [(0, "release/video.mkv")])
            self.assertNotIn("key", engine._final_names_applied)

    def test_expanded_selection_keeps_completed_files_at_final_names(self):
        engine = object.__new__(TorrentEngine)
        engine._startup_checks = set()
        engine._final_names_applied = set()
        engine._temp_names_applied = set()
        engine._sources = {"key": {"save_path": ""}}

        class ExpandedSelectionHandle:
            def __init__(self):
                self.renamed = []

            def torrent_file(self):
                return FakeInfo()

            def file_priorities(self):
                return [4, 4, 0]

            def file_progress(self):
                return [1000, 50, 0]

            def rename_file(self, index, path):
                self.renamed.append((index, path))

        handle = ExpandedSelectionHandle()
        status = SimpleNamespace(
            state=3,
            is_seeding=False,
            total_wanted=1200,
            total_wanted_done=1050,
        )

        engine._manage_file_names("key", handle, status)

        self.assertEqual(
            handle.renamed,
            [
                (0, "release/video.mkv"),
                (1, "release/readme.txt.torrpart"),
            ],
        )

    def test_pending_startup_check_does_not_change_file_names(self):
        engine = object.__new__(TorrentEngine)
        engine._pending_checks = {"key"}
        engine._startup_checks = set()
        engine._final_names_applied = set()
        engine._temp_names_applied = set()
        engine._sources = {"key": {"save_path": ""}}

        class PendingHandle:
            def __init__(self):
                self.renamed = []

            def torrent_file(self):
                return FakeInfo()

            def rename_file(self, index, path):
                self.renamed.append((index, path))

        handle = PendingHandle()
        status = SimpleNamespace(
            state=3,
            is_seeding=False,
            total_wanted=1200,
            total_wanted_done=0,
        )

        engine._manage_file_names("key", handle, status)

        self.assertEqual(handle.renamed, [])


class FakeErrorCode:
    def __init__(self, value, message):
        self._value = value
        self._message = message

    def value(self):
        return self._value

    def message(self):
        return self._message


class StatusErrorTests(unittest.TestCase):
    def test_ignores_success_error_code(self):
        status = SimpleNamespace(
            errc=FakeErrorCode(0, "The operation completed successfully")
        )
        self.assertEqual(TorrentEngine._status_error(status), "")

    def test_keeps_real_error_code(self):
        status = SimpleNamespace(errc=FakeErrorCode(5, "Access denied"))
        self.assertEqual(TorrentEngine._status_error(status), "Access denied")


class FakeFileHandle:
    def __init__(self):
        self.priorities = [4, 0, 0]

    def torrent_file(self):
        return FakeInfo()

    def file_priorities(self):
        return self.priorities

    def file_progress(self):
        return [1000, 0, 0]

    def prioritize_files(self, priorities):
        self.priorities = list(priorities)


class FileSelectionTests(unittest.TestCase):
    def make_engine(self):
        engine = object.__new__(TorrentEngine)
        engine._lock = threading.RLock()
        engine._handles = {"key": FakeFileHandle()}
        engine._sources = {"key": {"downloaded": 500}}
        engine._file_priorities_applied = set()
        engine._final_names_applied = {"key"}
        engine._temp_names_applied = {"key"}
        engine.lt = SimpleNamespace()
        engine._save_sources = lambda: None
        return engine

    def test_lists_payload_files_and_current_selection(self):
        engine = self.make_engine()

        files = engine.files("key")

        self.assertEqual(
            [(item.index, item.path, item.size, item.selected) for item in files],
            [
                (0, "release/video.mkv", 1000, True),
                (1, "release/readme.txt", 200, False),
            ],
        )

    def test_legacy_torrent_selects_only_files_with_progress(self):
        engine = self.make_engine()
        engine._handles["key"].priorities = [4, 4, 0]

        files = engine.files("key")

        self.assertEqual([item.selected for item in files], [True, False])

    def test_saved_selection_uses_explicit_priorities(self):
        engine = self.make_engine()
        engine._sources["key"]["file_priorities"] = [0, 4, 0]
        engine._handles["key"].priorities = [0, 4, 0]

        files = engine.files("key")

        self.assertEqual([item.selected for item in files], [False, True])

    def test_applies_and_persists_selected_files(self):
        engine = self.make_engine()

        engine.select_files("key", {1})

        self.assertEqual(engine._handles["key"].priorities, [0, 4, 0])
        self.assertEqual(engine._sources["key"]["file_priorities"], [0, 4, 0])
        self.assertEqual(engine._sources["key"]["downloaded"], 0)
        self.assertIn("key", engine._file_priorities_applied)
        self.assertNotIn("key", engine._final_names_applied)
        self.assertNotIn("key", engine._temp_names_applied)


class FakeHandle:
    def __init__(self):
        self.current_flags = 0
        self.limit = -1
        self.resumed = False
        self.rechecked = False

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

    def force_recheck(self):
        self.rechecked = True


class PauseTests(unittest.TestCase):
    def test_pause_throttles_and_resume_restores(self):
        engine = object.__new__(TorrentEngine)
        engine._lock = threading.RLock()
        engine.lt = SimpleNamespace(torrent_flags=SimpleNamespace(upload_mode=1))
        engine._soft_paused = {}
        engine._pending_checks = set()
        engine._startup_checks = set()
        handle = FakeHandle()
        engine._handles = {"key": handle}

        engine.pause("key")
        self.assertEqual(handle.limit, 1)
        self.assertEqual(handle.current_flags, 1)

        engine.resume("key")
        self.assertEqual(handle.limit, -1)
        self.assertEqual(handle.current_flags, 0)
        self.assertTrue(handle.resumed)

    def test_first_resume_rechecks_restored_torrent(self):
        engine = object.__new__(TorrentEngine)
        engine._lock = threading.RLock()
        engine.lt = SimpleNamespace(torrent_flags=SimpleNamespace(upload_mode=1))
        engine._soft_paused = {}
        engine._pending_checks = {"key"}
        engine._startup_checks = set()
        handle = FakeHandle()
        engine._handles = {"key": handle}

        engine.resume("key")

        self.assertTrue(handle.rechecked)
        self.assertTrue(handle.resumed)
        self.assertNotIn("key", engine._pending_checks)
        self.assertIn("key", engine._startup_checks)


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
        engine._file_priorities_applied = set()
        engine._pending_checks = set()
        engine._startup_checks = set()
        engine._startup_checks_started = set()
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
