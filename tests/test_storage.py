import concurrent.futures
import dataclasses
import hashlib
import json
import os
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_subtitle import storage
from arr_subtitle.models import Anime, Budget, Cancelled, CollectorError, Mapping, Settings


SRT = "1\n00:00:00,000 --> 00:00:01,000\n안녕하세요\n".encode()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media = self.root / "Example - 01.mkv"
        self.media.write_bytes(b"video")
        self.target = {"episode": "1", "mediaPath": str(self.media)}
        self.anime = Anime(1, "Example", season=1)

    def save(self, data=SRT, extension=".srt"):
        return storage.save(data, extension, self.anime, self.target, "", "https://example.com/post?secret=hidden", Budget())

    def test_save_creates_sidecar_with_hash_and_media_path(self):
        result = self.save()
        self.assertEqual("Downloaded", result["status"])
        self.assertEqual(str(self.media.with_suffix(".ko.srt")), result["path"])
        self.assertEqual(SRT, Path(result["path"]).read_bytes())
        self.assertEqual(hashlib.sha256(SRT).hexdigest(), result["sha256"])
        self.assertEqual(str(self.media), result["mediaPath"])
        self.assertEqual("https://example.com/post", result["page"])

    def test_sidecar_can_be_saved_for_filename_without_video(self):
        self.media.unlink()
        result = self.save()
        self.assertEqual("Downloaded", result["status"])
        self.assertTrue(Path(result["path"]).is_file())

    def test_existing_kor_is_not_overwritten(self):
        path = self.media.with_suffix(".kor.srt")
        path.write_bytes(SRT)
        self.assertEqual("AlreadyPresent", self.save()["status"])
        self.assertFalse(self.media.with_suffix(".ko.srt").exists())
        self.assertEqual(SRT, path.read_bytes())

    def test_invalid_existing_subtitle_is_preserved(self):
        path = self.media.with_suffix(".ko.srt")
        path.write_bytes(b"invalid")
        with self.assertRaises(CollectorError):
            self.save()
        self.assertEqual(b"invalid", path.read_bytes())

    def test_invalid_secondary_subtitle_is_not_hidden_by_valid_one(self):
        self.media.with_suffix(".ko.srt").write_bytes(SRT)
        self.media.with_suffix(".kor.srt").write_bytes(b"invalid")
        with self.assertRaises(CollectorError):
            storage.existing(self.anime, self.target, "")

    def test_concurrent_saves_publish_once(self):
        barrier = threading.Barrier(4)
        def write():
            barrier.wait()
            return self.save()["status"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(lambda _: write(), range(4)))
        self.assertEqual(1, statuses.count("Downloaded"))
        self.assertEqual(3, statuses.count("AlreadyPresent"))
        self.assertEqual(SRT, self.media.with_suffix(".ko.srt").read_bytes())
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_lock_creation_retries_when_existing_entry_disappears(self):
        original = os.open
        attempts = []
        def interrupted(path, flags, *args, **kwargs):
            if str(path).endswith(".lock") and len(attempts) < 2:
                attempts.append(flags)
                if len(attempts) == 1:
                    raise FileExistsError("Concurrent creator")
                raise FileNotFoundError("Concurrent removal")
            return original(path, flags, *args, **kwargs)
        with patch("arr_subtitle.storage.os.open", side_effect=interrupted):
            self.assertEqual(self.save()["status"], "Downloaded")
        self.assertTrue(attempts[0] & os.O_EXCL)
        self.assertFalse(attempts[1] & os.O_CREAT)

    def test_atomic_write_never_overwrites_destination(self):
        destination = self.root / "file.json"
        destination.write_bytes(b"original")
        with self.assertRaises(FileExistsError):
            storage.atomic_write(destination, b"new")
        self.assertEqual(b"original", destination.read_bytes())

    def test_fsync_failure_does_not_publish_partial_file(self):
        destination = self.root / "file.json"
        with patch("arr_subtitle.storage.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                storage.atomic_write(destination, b"new")
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_cancellation_does_not_publish_file(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(Cancelled):
            storage.atomic_write(self.root / "file", b"new", budget=Budget(cancel=event))
        self.assertFalse((self.root / "file").exists())

    def test_symlink_components_and_subtitles_are_rejected(self):
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(OSError):
            storage.checked_path(alias / "child")
        subtitle = self.media.with_suffix(".ko.srt")
        subtitle.symlink_to(self.media)
        with self.assertRaises(OSError):
            self.save()
        self.assertEqual(b"video", self.media.read_bytes())

    def test_junction_attribute_is_rejected(self):
        class Info:
            st_mode = 0
            st_file_attributes = 0x400
        self.assertTrue(storage.is_link(self.root, Info()))

    def test_collect_filename_stays_inside_output(self):
        anime = Anime(1, "../../unsafe:title\\name", season=2)
        result = storage.save(SRT, ".srt", anime, {"episode": "1", "mediaPath": None}, str(self.root / "output"), "https://example.com", Budget())
        path = Path(result["path"])
        self.assertEqual(self.root / "output" / "Season 2", path.parent)
        self.assertNotIn(":", path.name)
        self.assertTrue(path.exists())
        self.assertIn("[anissia-1]", path.name)

    def test_collect_sanitized_titles_do_not_share_existing_subtitle(self):
        target = {"episode": "1", "mediaPath": None}
        first = Anime(1, "A:B")
        second = Anime(2, "A?B")
        first_result = storage.save(SRT, ".srt", first, target, str(self.root), "https://example.com", Budget())
        self.assertIsNone(storage.existing(second, target, str(self.root)))
        second_result = storage.save(SRT, ".srt", second, target, str(self.root), "https://example.com", Budget())
        self.assertEqual("Downloaded", second_result["status"])
        self.assertNotEqual(first_result["path"], second_result["path"])
        self.assertEqual("A_B [anissia-1] - 01.ko.srt", Path(first_result["path"]).name)

    def test_collect_truncated_titles_include_distinct_anime_ids(self):
        target = {"episode": "1"}
        first = Anime(3, "A" * 110 + "First")
        second = Anime(4, "A" * 110 + "Second")
        first_result = storage.save(SRT, ".srt", first, target, str(self.root), "https://example.com", Budget())
        self.assertIsNone(storage.existing(second, target, str(self.root)))
        second_result = storage.save(SRT, ".srt", second, target, str(self.root), "https://example.com", Budget())
        self.assertNotEqual(first_result["path"], second_result["path"])
        self.assertEqual("Downloaded", second_result["status"])

    def test_collect_id_is_present_even_when_title_needs_no_sanitization(self):
        base = storage.stem(self.anime, {"episode": "1"}, str(self.root))
        self.assertEqual("Example [anissia-1] - 01", base.name)
        self.assertEqual(self.media.with_suffix(""), storage.stem(self.anime, self.target, ""))

    def test_settings_round_trip_and_stale_hash(self):
        path = self.root / "config" / "settings.json"
        settings, initial = storage.load_settings(path)
        self.assertEqual("missing", initial)
        settings = dataclasses.replace(settings, anilist_enabled=False, mappings=(Mapping("Example", 1, 2),))
        new_hash = storage.update_settings(path, settings, initial)
        loaded, current = storage.load_settings(path)
        self.assertEqual(settings, loaded)
        self.assertEqual(new_hash, current)
        document = json.loads(path.read_bytes())
        self.assertFalse(document["aniListEnabled"])
        self.assertEqual(2, document["mappings"][0]["animeId"])
        with self.assertRaises(OSError):
            storage.update_settings(path, Settings(), initial)
        self.assertEqual(settings, storage.load_settings(path)[0])

    def test_settings_concurrent_updates_detect_conflict(self):
        path = self.root / "settings.json"
        barrier = threading.Barrier(2)
        def update():
            barrier.wait()
            try:
                storage.update_settings(path, Settings(), "missing")
                return "updated"
            except OSError:
                return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: update(), range(2)))
        self.assertCountEqual(["updated", "conflict"], results)

    def test_windows_lock_initializes_only_after_acquisition(self):
        state = {"locked": False, "attempts": 0}
        def locking(fd, mode, size):
            self.assertEqual(1, size)
            if mode == 1:
                state["attempts"] += 1
                if state["attempts"] == 1:
                    raise PermissionError("Lock is held by another process")
                state["locked"] = True
            else:
                state["locked"] = False
        original_write = os.write
        def write(fd, data):
            self.assertTrue(state["locked"], "Lock initialization must hold the byte-range lock")
            return original_write(fd, data)
        windows_os = types.SimpleNamespace(**{**vars(os), "name": "nt", "write": write})
        msvcrt = types.SimpleNamespace(locking=locking, LK_NBLCK=1, LK_UNLCK=2)
        budget = Budget()
        with patch("arr_subtitle.storage.os", windows_os), patch.dict("sys.modules", {"msvcrt": msvcrt}), patch.object(budget, "wait"):
            with storage._settings_lock(self.root / "settings.json", budget):
                self.assertTrue(state["locked"])
        self.assertFalse(state["locked"])
        self.assertEqual(2, state["attempts"])
        self.assertEqual(b"\0", (self.root / "settings.json.lock").read_bytes())

    def test_oversized_settings_are_rejected(self):
        path = self.root / "settings.json"
        path.write_bytes(b" " * (storage.SETTINGS_LIMIT + 1))
        with self.assertRaises(ValueError):
            storage.load_settings(path)

    def test_deep_settings_json_is_invalid(self):
        path = self.root / "settings.json"
        path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
        with self.assertRaises(ValueError):
            storage.load_settings(path)

    def test_manifest_contains_its_own_path_and_cannot_be_replaced(self):
        report = storage.write_manifest({"requestId": "abc", "episodes": [], "success": False}, self.root)
        self.assertEqual(report, json.loads(Path(report["manifestPath"]).read_bytes()))
        with self.assertRaises(FileExistsError):
            storage.write_manifest(report, self.root)
        with self.assertRaises(ValueError):
            storage.write_manifest({"requestId": "../escape"}, self.root)

    def test_default_paths_follow_platform_directories(self):
        if os.name == "nt":
            self.skipTest("POSIX environment expectation")
        with patch("arr_subtitle.storage.sys.platform", "linux"), patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data")}):
            self.assertEqual(self.root / "config" / "arr-subtitle" / "config.json", storage.config_path())
            self.assertEqual(self.root / "data" / "arr-subtitle", storage.data_path())
