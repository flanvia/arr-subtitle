import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from arr_subtitle import cli
from arr_subtitle.models import Blog, Budget, Settings
from arr_subtitle.network import Response, Session
from arr_subtitle.storage import load_settings, update_settings


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        update_settings(self.config, Settings(anilist_enabled=False), "missing")
        self.output = io.StringIO()
        self.error = io.StringIO()

    def run_cli(self, args, **kwargs):
        return cli.run([*args, "--config", str(self.config)], stdout=self.output, stderr=self.error, **kwargs)

    def video(self, name="Example S01E01.mkv"):
        path = self.root / "Example" / "Season 1" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        return path

    def report(self, success=True, targets=()):
        return {"requestId": "test-run", "anime": None, "episodes": [{"episode": target["episode"], "mediaPath": target.get("mediaPath"), "status": "Found"} for target in targets], "attempts": [], "requests": {}, "receivedBytes": 0, "elapsedMilliseconds": 0, "firstCandidateMilliseconds": None, "success": success}

    def test_help_and_version_without_engine(self):
        self.assertEqual(0, cli.run([], stdout=self.output, stderr=self.error))
        self.assertIn("arr-subtitle", self.output.getvalue())
        self.output = io.StringIO()
        self.assertEqual(0, cli.run(["--version"], stdout=self.output, stderr=self.error))
        self.assertEqual("0.2.0\n", self.output.getvalue())

    def test_search_emits_single_json_document_and_stderr_progress(self):
        def search(query, budget, progress=None):
            progress({"stage": "found", "source": query})
            return self.report()
        with patch("arr_subtitle.engine.Engine.search", side_effect=search):
            code = self.run_cli(["search", "Example", "--json"])
        self.assertEqual(0, code)
        self.assertTrue(json.loads(self.output.getvalue())["success"])
        self.assertIn("found Example", self.error.getvalue())

    def test_real_transport_boundary_is_used_by_search(self):
        calls = []
        def transport(url, method, body, timeout, limit):
            calls.append(url)
            return Response(url, b'{"code":"ok","data":{"content":[],"last":true}}')
        code = self.run_cli(["search", "Example", "--json"], transport=transport)
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertFalse(json.loads(self.output.getvalue())["success"])
        self.assertIn("request", self.error.getvalue())

    def test_invalid_request_returns_two_and_valid_json(self):
        code = self.run_cli(["collect", "Example", "--episodes", "1", "--json"])
        self.assertEqual(2, code)
        self.assertFalse(json.loads(self.output.getvalue())["success"])
        self.assertIn("collect requires", self.error.getvalue())

    def test_unknown_option_returns_two_without_argparse_exit(self):
        code = self.run_cli(["search", "Example", "--unknown", "--json"])
        self.assertEqual(2, code)
        self.assertIn("unrecognized arguments", json.loads(self.output.getvalue())["error"])

    def test_invalid_timeout_returns_two(self):
        self.assertEqual(2, self.run_cli(["search", "Example", "--timeout", "nan", "--json"]))

    def test_missing_explicit_config_is_invalid(self):
        code = cli.run(["blogs", "list", "--config", str(self.root / "missing"), "--json"], stdout=self.output, stderr=self.error)
        self.assertEqual(2, code)

    def test_bare_path_and_download_directory_use_same_collection(self):
        media = self.video()
        self.video("Example S01E02.mp4")
        def collect(query, targets, output, budget, search_only=False, progress=None):
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect) as collect_mock, patch("arr_subtitle.storage.data_path", return_value=self.root / "data"):
            code = self.run_cli([str(media.parent), "--json"])
        self.assertEqual(0, code)
        self.assertEqual(1, collect_mock.call_count)
        result = json.loads(self.output.getvalue())
        self.assertEqual(2, len(result["episodes"]))
        self.assertTrue(Path(result["manifestPath"]).is_file())
        self.assertEqual(result, json.loads(Path(result["manifestPath"]).read_bytes()))

    def test_match_does_not_write_manifest(self):
        media = self.video()
        def collect(query, targets, output, budget, search_only=False, progress=None):
            self.assertTrue(search_only)
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect), patch("arr_subtitle.storage.write_manifest") as manifest:
            code = self.run_cli(["match", str(media), "--json"])
        self.assertEqual(0, code)
        manifest.assert_not_called()

    def test_match_accepts_filename_without_video(self):
        media = self.video()
        media.unlink()
        def collect(query, targets, output, budget, search_only=False, progress=None):
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect):
            code = self.run_cli(["match", str(media), "--json"])
        self.assertEqual(0, code)
        self.assertEqual(str(media), json.loads(self.output.getvalue())["episodes"][0]["mediaPath"])

    def test_download_folder_uses_release_title_with_real_engine(self):
        media = self.root / "Downloads" / "Example S01E01.mkv"
        media.parent.mkdir()
        media.write_bytes(b"video")
        queries = []
        def transport(url, method, body, timeout, limit):
            if "/anime/list/" in url:
                query = parse_qs(urlsplit(url).query)["q"][0]
                queries.append(query)
                content = [{"animeNo": 42, "subject": "Example", "originalSubject": "Example"}] if query == "Example" else []
                value = {"code": "ok", "data": {"content": content, "last": True}}
            elif "/anime/animeNo/42" in url:
                value = {"code": "ok", "data": {"subject": "Example", "originalSubject": "Example"}}
            elif "/anime/caption/" in url:
                value = {"code": "ok", "data": [{"episode": "1", "website": "https://author.blogspot.com/post", "name": "Maker"}]}
            elif "/feeds/" in url:
                value = {"feed": {"openSearch$totalResults": {"$t": "1"}, "entry": [{"title": {"$t": "Example 01"}, "content": {"$t": "<a href='https://files.example.com/Example01.srt'>Example 01.srt</a>"}, "link": [{"rel": "alternate", "href": "https://author.blogspot.com/post"}]}]}}
            elif url == "https://author.blogspot.com/post":
                return Response(url, b"<meta property='og:title' content='Example 01'><div class='post-body'><a href='https://files.example.com/Example01.srt'>Example 01.srt</a></div>")
            elif url == "https://files.example.com/Example01.srt":
                return Response(url, "1\n00:00:00,000 --> 00:00:01,000\n안녕하세요\n".encode())
            else:
                raise AssertionError(f"Unexpected URL: {url}")
            return Response(url, json.dumps(value).encode())
        with patch("arr_subtitle.cli.Session", side_effect=lambda **kwargs: Session(interval=0, **kwargs)), patch("arr_subtitle.storage.data_path", return_value=self.root / "data"):
            code = self.run_cli(["download", str(media.parent), "--json"], transport=transport)
        self.assertEqual(0, code, self.output.getvalue() + self.error.getvalue())
        self.assertEqual(["Example"], queries)
        result = json.loads(self.output.getvalue())
        self.assertEqual(str(media), result["episodes"][0]["mediaPath"])
        self.assertEqual(str(media.with_suffix(".ko.srt")), result["episodes"][0]["path"])

    def test_collect_episodes_and_manifest(self):
        def collect(query, targets, output, budget, progress=None):
            self.assertEqual(["1", "2", "3", "4.5"], [target["episode"] for target in targets])
            self.assertEqual(5, query["animeId"])
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect):
            code = self.run_cli(["collect", "--anime-id", "5", "--episodes", "1-3,2,4.5", "--output", str(self.root / "out"), "--json"])
        self.assertEqual(0, code)
        self.assertTrue(Path(json.loads(self.output.getvalue())["manifestPath"]).is_file())

    def test_partial_collection_returns_one(self):
        with patch("arr_subtitle.engine.Engine.search", return_value=self.report(False)):
            self.assertEqual(1, self.run_cli(["search", "Example", "--json"]))

    def test_cancelled_collection_preserves_successes_and_manifest(self):
        event = threading.Event()
        media = self.video()
        def collect(query, targets, output, budget, search_only=False, progress=None):
            event.set()
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect), patch("arr_subtitle.storage.data_path", return_value=self.root / "data"):
            code = self.run_cli([str(media), "--json"], cancel=event)
        self.assertEqual(130, code)
        result = json.loads(self.output.getvalue())
        self.assertEqual(1, len(result["episodes"]))
        self.assertTrue(Path(result["manifestPath"]).exists())
        self.assertTrue(result["cancelled"])

    def test_collect_cancelled_report_is_written_to_manifest(self):
        event = threading.Event()
        def collect(query, targets, output, budget, progress=None):
            event.set()
            return self.report(targets=targets)
        with patch("arr_subtitle.engine.Engine.collect", side_effect=collect):
            code = self.run_cli(["collect", "Example", "--episodes", "1", "--output", str(self.root / "out"), "--json"], cancel=event)
        self.assertEqual(130, code)
        report = json.loads(self.output.getvalue())
        self.assertTrue(report["cancelled"])
        self.assertFalse(report["success"])
        self.assertTrue(json.loads(Path(report["manifestPath"]).read_bytes())["cancelled"])

    def test_explicit_id_timeout_during_preflight_is_partial(self):
        media = self.video()
        with patch("arr_subtitle.cli.Budget", return_value=Budget(deadline=0)), patch("arr_subtitle.storage.data_path", return_value=self.root / "data"):
            code = self.run_cli([str(media), "--anime-id", "1", "--json"])
        self.assertEqual(1, code)
        report = json.loads(self.output.getvalue())
        self.assertFalse(report["cancelled"])
        self.assertTrue(report["timedOut"])
        self.assertTrue(Path(report["manifestPath"]).is_file())

    def test_manifest_failure_is_reported_without_hiding_results(self):
        with patch("arr_subtitle.engine.Engine.collect", return_value=self.report()), patch("arr_subtitle.storage.write_manifest", side_effect=OSError("disk full")):
            code = self.run_cli(["collect", "Example", "--episodes", "1", "--output", str(self.root), "--json"])
        self.assertEqual(1, code)
        self.assertEqual("disk full", json.loads(self.output.getvalue())["manifestError"])

    def test_blog_add_list_remove_preserves_settings(self):
        blog = Blog("https://example.blogspot.com", "Blogspot", "example")
        with patch("arr_subtitle.blogs.detect_blog", return_value=blog):
            self.assertEqual(0, self.run_cli(["blogs", "add", blog.url, "--json"]))
        self.assertEqual((blog,), load_settings(self.config)[0].blogs)
        self.output = io.StringIO()
        self.assertEqual(0, self.run_cli(["blogs", "list", "--json"]))
        self.assertEqual(blog.url, json.loads(self.output.getvalue())[0]["url"])
        self.output = io.StringIO()
        self.assertEqual(0, self.run_cli(["blogs", "remove", blog.url, "--json"]))
        self.assertFalse(load_settings(self.config)[0].blogs)
        self.assertFalse(load_settings(self.config)[0].anilist_enabled)

    def test_blog_add_keeps_legacy_platform_spelling(self):
        blog = Blog("https://example.blogspot.com/", "blogspot", "example.blogspot.com")
        with patch("arr_subtitle.blogs.detect_blog", return_value=blog):
            self.assertEqual(0, self.run_cli(["blogs", "add", blog.url, "--json"]))
        self.assertEqual("Blogspot", load_settings(self.config)[0].blogs[0].platform)

    def test_episode_range_limits(self):
        self.assertEqual(["1", "2", "3", "1.5"], cli.parse_episodes("1-3,2,1.50"))
        for value in ("", "2-1", "0-1000", "-1", "NaN", "1-2-3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.parse_episodes(value)
