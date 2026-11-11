import io
import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from arr_subtitle.engine import Engine
from arr_subtitle.models import Anime, Blog, Budget, Caption, Mapping, Post, Settings
from arr_subtitle.network import Response, Session


SAMI = "<SAMI><BODY><SYNC Start=0><P>안녕하세요</P></BODY></SAMI>".encode("utf-8")


def archive(*names):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as handle:
        for name in names:
            handle.writestr(name, SAMI)
    return output.getvalue()


def fixture(title="Series 1-3화", names=("Series 01.smi", "Series 02.smi", "Series 03.smi"), caption=False):
    calls = []
    def transport(url, method, body, timeout, limit):
        calls.append(url)
        if "/anime/list/" in url:
            result = {"code": "ok", "data": {"content": [{"animeNo": 1, "subject": "Series", "originalSubject": "Series"}], "last": True}}
        elif "caption" in url:
            result = {"code": "ok", "data": [{"episode": "3", "website": "https://author.blogspot.com/post", "name": "Maker", "updDt": "2026"}] if caption else []}
        elif "/anime/animeNo/" in url:
            result = {"code": "ok", "data": {"subject": "Series", "originalSubject": "Series"}}
        elif url == "https://files.example.com/batch.zip":
            return Response(url, archive(*names))
        elif url == "https://author.blogspot.com/post":
            return Response(url, f"<meta property='og:title' content='{title}'><div class='post-body'><a href='https://files.example.com/batch.zip'>Series.zip</a></div>".encode())
        elif "/feeds/" in url:
            result = {"feed": {"openSearch$totalResults": {"$t": "1"}, "entry": [{"title": {"$t": title}, "content": {"$t": "<a href='https://files.example.com/batch.zip'>Series.zip</a>"}, "link": [{"rel": "alternate", "href": "https://author.blogspot.com/post"}]}], "link": []}}
        else:
            raise AssertionError(f"Unexpected URL: {url}")
        return Response(url, json.dumps(result).encode(), "application/json")
    return transport, calls


class EngineTests(unittest.TestCase):
    def settings(self):
        return Settings(False, (Blog("https://author.blogspot.com/", "Blogspot", "author.blogspot.com"),))

    def test_batch_search_preserves_every_member(self):
        transport, calls = fixture()
        with Session(transport, interval=0) as session:
            result = Engine(session, self.settings()).search("Series", Budget())
        self.assertTrue(result["success"], result)
        self.assertEqual([s["episode"] for s in result["anime"][0]["subtitles"]], ["1", "2", "3"])
        self.assertEqual(calls.count("https://files.example.com/batch.zip"), 1)

    def test_declared_range_keeps_missing_tail(self):
        transport, _ = fixture(names=("Series 01.smi",))
        with Session(transport, interval=0) as session:
            result = Engine(session, self.settings()).search("Series", Budget())
        self.assertFalse(result["success"])
        self.assertEqual(result["anime"][0]["missingEpisodes"], ["2", "3"])

    def test_wrong_attachment_title_is_not_an_alias(self):
        transport, _ = fixture(names=("Other 01.smi",))
        with Session(transport, interval=0) as session:
            result = Engine(session, self.settings()).search("Series", Budget())
        self.assertFalse(result["success"])
        self.assertFalse(result["anime"][0]["subtitles"])

    def test_collection_preserves_duplicate_episode_video_paths(self):
        transport, calls = fixture()
        with tempfile.TemporaryDirectory() as directory, Session(transport, interval=0) as session:
            paths = [str(Path(directory).resolve() / name) for name in ("Series - 01.mkv", "Series - 01 alternate.mkv")]
            targets = [{"episode": "1", "mediaPath": path} for path in paths]
            result = Engine(session, self.settings()).collect({"animeId": 1}, targets, "", Budget())
            self.assertTrue(result["success"], result)
            self.assertEqual(len(result["episodes"]), 2)
            for entry in result["episodes"]:
                self.assertTrue(Path(entry["path"]).is_file())
            self.assertEqual(calls.count("https://files.example.com/batch.zip"), 1)

    def test_cancellation_keeps_discovered_candidates(self):
        transport, _ = fixture()
        budget = Budget()
        def progress(update):
            if update["stage"] == "found":
                budget.cancel.set()
        with Session(transport, interval=0) as session:
            result = Engine(session, self.settings()).search("Series", budget, progress)
        self.assertFalse(result["complete"])
        self.assertTrue(result["anime"][0]["subtitles"])

    def test_mixed_category_cannot_authorize_numeric_other_anime(self):
        from arr_subtitle.engine import _State
        state = _State(Anime(1, "Series", "Series", ("Series",)))
        state.aliases["https://example.tistory.com/category/Mixed"] = set()
        post = Post("https://example.tistory.com/2", "Other 2화", "<a href='https://files.example.com/02.smi'>02.smi</a>", category_url="https://example.tistory.com/category/Mixed")
        with Session(lambda *_: None, interval=0) as session:
            Engine(session, Settings(False))._post(state, post, Blog("https://example.tistory.com/", "tistory", "example.tistory.com"), "Maker", (), Budget(), None)
        self.assertFalse(state.candidates)
        self.assertFalse(state.complete)

    def test_numbering_cannot_promote_other_anime_alias(self):
        from arr_subtitle.engine import _State
        state = _State(Anime(1, "Series Season 2", "Series", ("Series",), 2), maximum=Decimal(12))
        post = Post("https://example.tistory.com/1", "Series Season 2 13화", "<a href='https://files.example.com/Other13.smi'>Other 13.smi</a>", category_url="https://example.tistory.com/category/Mixed")
        blog = Blog("https://example.tistory.com/", "tistory", "example.tistory.com")
        with Session(lambda *_: None, interval=0) as session:
            engine = Engine(session, Settings(False))
            engine._seed_evidence(state, Caption("1", post.url), post, blog)
        self.assertFalse(state.numbering)
        self.assertFalse(state.aliases[post.category_url])

    def test_caption_coverage_survives_shorter_post_range(self):
        transport, _ = fixture(title="Series 1-3화", caption=True)
        def changed(url, *args):
            if "caption" in url:
                return Response(url, json.dumps({"code": "ok", "data": [{"episode": "5", "website": "https://author.blogspot.com/post"}]}).encode())
            return transport(url, *args)
        with Session(changed, interval=0) as session:
            report = Engine(session, self.settings()).search("Series", Budget())
        self.assertFalse(report["success"])
        self.assertIn("5", report["anime"][0]["missingEpisodes"])

    def test_configured_numbering_converts_caption_fallback_to_source(self):
        from arr_subtitle.engine import _State
        from decimal import Decimal
        state = _State(Anime(1, "Series Season 2", "Series", ("Series",), 2), mapping=Mapping("Series", 2, 1, (), None, 12), maximum=Decimal(12))
        post = Post("https://author.blogspot.com/post", "Series Season 2", "<a href='https://files.example.com/13.smi'>Series 13.smi</a>")
        with Session(lambda *_: None, interval=0) as session:
            Engine(session, Settings(False))._post(state, post, Blog("https://author.blogspot.com/", "blogspot", "author.blogspot.com"), "Maker", ("1",), Budget(), None)
        self.assertEqual([c.episode for c in state.candidates.values()], ["1"])

    def test_path_scoped_mapping_keeps_episode_offset(self):
        source = "https://author.blogspot.com/post"
        def transport(url, *args):
            if "/anime/animeNo/" in url:
                value = {"code": "ok", "data": {"subject": "Series Season 2", "originalSubject": "Series"}}
            elif "caption" in url:
                value = {"code": "ok", "data": []}
            elif "/feeds/" in url:
                value = {"feed": {"openSearch$totalResults": {"$t": "1"}, "entry": [{"title": {"$t": "Series Season 2 13화"}, "content": {"$t": "<a href='https://files.example.com/13.smi'>Series 13.smi</a>"}, "link": [{"rel": "alternate", "href": source}]}], "link": []}}
            else:
                raise AssertionError(url)
            return Response(url, json.dumps(value).encode())
        with tempfile.TemporaryDirectory() as root, Session(transport, interval=0) as session:
            root = str(Path(root).resolve())
            mapping = Mapping("Series", 2, 1, (), root, 12)
            settings = Settings(False, self.settings().blogs, (mapping,))
            report = Engine(session, settings).collect({"title": "Series", "season": 2, "libraryPath": root}, [{"episode": "1"}], root, Budget(), search_only=True)
        self.assertTrue(report["success"], report)
        self.assertEqual(report["episodes"][0]["sourceEpisode"], "13")

    def test_mapping_does_not_bypass_each_file_metadata_conflict(self):
        transport, _ = fixture()
        with tempfile.TemporaryDirectory() as root, Session(transport, interval=0) as session:
            root = str(Path(root).resolve())
            path = Path(root) / "Series - 01.mkv"
            path.with_suffix(".json").write_text('{"animeId":999,"title":"Other"}', encoding="utf-8")
            settings = Settings(False, self.settings().blogs, (Mapping("Series", 1, 1, (), root),))
            report = Engine(session, settings).collect({"title": "Series", "season": 1, "libraryPath": root}, [{"episode": "1", "mediaPath": str(path)}], root, Budget(), search_only=True)
        self.assertFalse(report["success"])
        self.assertEqual(report["episodes"][0]["status"], "AmbiguousMatch")
