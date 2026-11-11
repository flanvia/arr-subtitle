import json
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arr_subtitle.batch import collect_path, collect_paths, group_key, iter_media
from arr_subtitle.models import Budget, Cancelled, Settings
from arr_subtitle.network import Response, Session


class RecordingEngine:
    def __init__(self, callback=None):
        self.session = Session(interval=0)
        self.calls = []
        self.callback = callback
        self.lock = threading.Lock()

    def collect(self, query, targets, output, budget, search_only=False, progress=None):
        with self.lock:
            self.calls.append((query, targets))
        if self.callback:
            self.callback(query, targets, budget)
        return {"anime": {"title": query["title"]}, "episodes": [{"episode": target["episode"], "mediaPath": target["mediaPath"], "status": "Found" if search_only else "Downloaded"} for target in targets], "attempts": [], "success": True, "firstCandidateMilliseconds": 0}


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def video(self, title, filename):
        path = self.root / title / "Season 1" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return str(path)

    def test_same_episode_multiple_videos_are_all_retained(self):
        paths = [self.video("Example", "Example S01E01 [A].mkv"), self.video("Example", "Example S01E01 [B].mp4")]
        engine = RecordingEngine()
        report = collect_paths(engine, paths, "", Budget())
        self.assertTrue(report["success"])
        self.assertEqual(1, len(engine.calls))
        self.assertCountEqual(paths, [result["mediaPath"] for result in report["episodes"]])
        self.assertIn(engine.calls[0][0]["mediaPath"], paths)
        self.assertEqual("Example", engine.calls[0][0]["fileTitle"])
        self.assertTrue(all(target["query"]["mediaPath"] == target["mediaPath"] for target in engine.calls[0][1]))

    def test_group_key_ignores_media_path_but_preserves_release_title(self):
        first = {"title": "Example", "season": 1, "libraryPath": "/series", "mediaPath": "/a", "fileTitle": "A"}
        second = {**first, "mediaPath": "/b", "fileTitle": "A"}
        self.assertEqual(group_key(first), group_key(second))
        self.assertNotEqual(group_key(first), group_key({**second, "fileTitle": "B"}))

    def test_downloads_directory_keeps_release_title_and_media_metadata_path(self):
        path = self.root / "Downloads" / "Example S01E01.mkv"
        path.parent.mkdir()
        path.write_bytes(b"video")
        path.with_suffix(".json").write_text('{"animeId": 42}')
        engine = RecordingEngine()
        report = collect_path(engine, path, "", Budget())
        self.assertTrue(report["success"])
        query, targets = engine.calls[0]
        self.assertEqual("Downloads", query["title"])
        self.assertEqual("Example", query["fileTitle"])
        self.assertEqual(str(path), query["mediaPath"])
        self.assertEqual(query, targets[0]["query"])

    def test_representative_query_reads_per_file_anime_id(self):
        from arr_subtitle.identity import Resolver
        path = self.root / "Downloads" / "Example S01E01.mkv"
        path.parent.mkdir()
        path.write_bytes(b"video")
        path.with_suffix(".json").write_text('{"animeId": 42}')
        calls = []
        def transport(url, *args):
            calls.append(url)
            self.assertTrue(url.endswith("/anime/animeNo/42"))
            return Response(url, json.dumps({"code": "ok", "data": {"subject": "Example", "originalSubject": "Example"}}).encode())
        with Session(transport, interval=0) as session:
            def collect(query, targets, budget):
                anime = Resolver(session, Settings(False)).resolve(query, budget)
                self.assertEqual(42, anime.anime_id)
            report = collect_path(RecordingEngine(collect), path, "", Budget())
        self.assertTrue(report["success"])
        self.assertEqual(1, len(calls))

    def test_mixed_downloads_are_separate_collection_groups(self):
        paths = [self.video("Downloads", "First S01E01.mkv"), self.video("Downloads", "Second S01E01.mkv")]
        engine = RecordingEngine()
        report = collect_paths(engine, paths, "", Budget())
        self.assertTrue(report["success"])
        self.assertEqual(2, len(engine.calls))
        self.assertEqual({"First", "Second"}, {query["fileTitle"] for query, _ in engine.calls})

    def test_release_title_grouping_preserves_sequel_punctuation(self):
        query = {"title": "Downloads", "season": 1, "libraryPath": "/downloads", "fileTitle": "Working!!"}
        self.assertNotEqual(group_key(query), group_key({**query, "fileTitle": "Working!!!"}))

    def test_folder_walk_is_recursive_and_skips_links(self):
        first = self.video("Example", "Example S01E01.mkv")
        second = self.video("Other", "Other S01E01.AVI")
        (self.root / "note.txt").write_text("note")
        try:
            (self.root / "alias").symlink_to(self.root / "Example", target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        self.assertCountEqual([first, second], list(iter_media(self.root)))

    def test_separate_artwork_collects_concurrently_at_most_four(self):
        paths = [self.video(f"Series {number}", f"Series {number} S01E01.mkv") for number in range(8)]
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active, maximum = 0, 0
        def collect(query, targets, budget):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            barrier.wait(timeout=5)
            with lock:
                active -= 1
        engine = RecordingEngine(collect)
        report = collect_paths(engine, paths, "", Budget())
        self.assertTrue(report["success"])
        self.assertEqual(4, maximum)
        self.assertEqual(8, len(report["episodes"]))

    def test_input_consumption_is_bounded_by_chunk(self):
        path = self.video("Example", "Example S01E01.mkv")
        consumed = 0
        calls = 0
        def paths():
            nonlocal consumed
            for _ in range(25):
                consumed += 1
                yield path
        def collect(query, targets, budget):
            nonlocal calls
            calls += 1
            self.assertLessEqual(consumed, calls * 8)
        with patch("arr_subtitle.batch.CHUNK_SIZE", 8):
            report = collect_paths(RecordingEngine(collect), paths(), "", Budget())
        self.assertEqual(4, calls)
        self.assertEqual(25, len(report["episodes"]))

    def test_cancellation_retains_successes_and_unstarted_chunk_targets(self):
        paths = [self.video(f"Series {number}", f"Series {number} S01E01.mkv") for number in range(8)]
        def collect(query, targets, budget):
            budget.cancel.set()
        report = collect_paths(RecordingEngine(collect), paths, "", Budget())
        self.assertTrue(report["cancelled"])
        self.assertFalse(report["success"])
        self.assertFalse(report["enumerationComplete"])
        self.assertEqual(8, len(report["episodes"]))
        self.assertIn("Downloaded", [item["status"] for item in report["episodes"]])
        self.assertIn("SearchIncomplete", [item["status"] for item in report["episodes"]])

    def test_cancellation_while_enumerating_preserves_seen_targets(self):
        path = self.video("Example", "Example S01E01.mkv")
        budget = Budget()
        def paths():
            yield path
            budget.cancel.set()
            yield path
        report = collect_paths(RecordingEngine(), paths(), "", budget)
        self.assertTrue(report["cancelled"])
        self.assertEqual(2, len(report["episodes"]))

    def test_cancelled_iterator_preserves_accumulated_chunk(self):
        path = self.video("Example", "Example S01E01.mkv")
        budget = Budget()
        def paths():
            yield path
            budget.cancel.set()
            raise Cancelled()
        engine = RecordingEngine()
        report = collect_paths(engine, paths(), "", budget)
        self.assertTrue(report["cancelled"])
        self.assertFalse(report["enumerationComplete"])
        self.assertEqual(str(path), report["episodes"][0]["mediaPath"])
        self.assertEqual("1", report["episodes"][0]["episode"])
        self.assertEqual("SearchIncomplete", report["episodes"][0]["status"])
        self.assertFalse(engine.calls)

    def test_cancelled_iterator_preserves_completed_batches_and_current_chunk(self):
        first = self.video("Example", "Example S01E01.mkv")
        second = self.video("Example", "Example S01E02.mkv")
        third = self.video("Example", "Example S01E03.mkv")
        budget = Budget()
        def paths():
            yield first
            yield second
            yield third
            budget.cancel.set()
            raise Cancelled()
        with patch("arr_subtitle.batch.CHUNK_SIZE", 2):
            report = collect_paths(RecordingEngine(), paths(), "", budget)
        self.assertTrue(report["cancelled"])
        statuses = {entry["mediaPath"]: entry["status"] for entry in report["episodes"]}
        self.assertEqual({first: "Downloaded", second: "Downloaded", third: "SearchIncomplete"}, statuses)

    def test_empty_directory_tree_checks_budget_during_traversal(self):
        for number in range(20):
            (self.root / str(number)).mkdir()
        budget = Budget()
        calls = 0
        original = budget.check
        def check():
            nonlocal calls
            calls += 1
            if calls == 7:
                budget.cancel.set()
            original()
        budget.check = check
        with self.assertRaises(Cancelled):
            list(iter_media(self.root, budget))
        self.assertEqual(7, calls)

    def test_nonmedia_entries_check_budget_during_traversal(self):
        for number in range(20):
            (self.root / f"{number}.txt").write_bytes(b"")
        budget = Budget()
        calls = 0
        original = budget.check
        def check():
            nonlocal calls
            calls += 1
            if calls == 7:
                budget.cancel.set()
            original()
        budget.check = check
        report = collect_path(RecordingEngine(), self.root, "", budget)
        self.assertTrue(report["cancelled"])
        self.assertFalse(report["enumerationComplete"])
        self.assertEqual(7, calls)

    def test_parse_failure_does_not_cancel_other_files(self):
        paths = [self.video("Example", "Example S01E01.mkv"), self.video("Example", "unparseable.mkv")]
        report = collect_paths(RecordingEngine(), paths, "", Budget())
        self.assertFalse(report["success"])
        self.assertEqual(2, len(report["episodes"]))
        self.assertEqual({"Downloaded", "AmbiguousMatch"}, {item["status"] for item in report["episodes"]})

    def test_explicit_id_rejects_multiple_series_before_collection(self):
        self.video("Example", "Example S01E01.mkv")
        self.video("Other", "Other S01E01.mkv")
        engine = RecordingEngine()
        with self.assertRaises(ValueError):
            collect_path(engine, self.root, "", Budget(), anime_id=1)
        self.assertFalse(engine.calls)

    def test_empty_directory_is_invalid(self):
        with self.assertRaises(ValueError):
            collect_path(RecordingEngine(), self.root, "", Budget())

    def test_timeout_before_enumeration_is_partial_not_invalid_or_interrupted(self):
        budget = Budget(deadline=0)
        report = collect_paths(RecordingEngine(), [], "", budget)
        self.assertFalse(report["cancelled"])
        self.assertTrue(report["timedOut"])
        self.assertFalse(report["success"])

    def test_filesystem_failure_keeps_already_enumerated_targets(self):
        path = self.video("Example", "Example S01E01.mkv")
        def paths():
            yield path
            raise PermissionError("Cannot read directory")
        report = collect_paths(RecordingEngine(), paths(), "", Budget())
        self.assertFalse(report["success"])
        self.assertEqual(1, len(report["episodes"]))
        self.assertEqual("Downloaded", report["episodes"][0]["status"])
        self.assertEqual("filesystem", report["attempts"][0]["source"])
