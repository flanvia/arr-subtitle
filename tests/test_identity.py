import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from arr_subtitle.identity import (
    Anissia, Resolver, equal, explicit_season, mentions, metadata_titles,
    normalize, parse_media, select_candidates,
)
from arr_subtitle.models import Anime, Budget, Cancelled, CollectorError, Mapping, Settings


class Replay:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, budget):
        budget.check()
        self.calls.append((url, None))
        value = self.handler(url, None)
        return SimpleNamespace(data=json.dumps(value).encode() if not isinstance(value, bytes) else value, url=url, filename=None)

    def post_json(self, url, body, budget):
        budget.check()
        self.calls.append((url, body))
        value = self.handler(url, body)
        return SimpleNamespace(data=json.dumps(value).encode(), url=url, filename=None)


def response(data):
    return {"code": "ok", "data": data}


def entry(number=1, title="Test", original="テスト"):
    return {"animeNo": number, "subject": title, "originalSubject": original}


def metadata(names=None, number=10, format="TV"):
    return {"id": number, "format": format, "seasonYear": 2026,
            "title": names or {"romaji": "Native Story", "native": "物語", "english": None},
            "synonyms": [], "relations": {"edges": [{"relationType": "SEQUEL", "node": {"id": 11}}]}}


class TitleTests(unittest.TestCase):
    def test_normalizes_unicode_whitespace_and_case(self):
        self.assertEqual("TEST STORY", normalize("  Ｔｅｓｔ\tstory\n"))
        self.assertTrue(equal("Test  Story", "test story"))

    def test_preserves_punctuation_distinguished_titles(self):
        self.assertFalse(equal("Working!!", "Working'!!"))
        self.assertFalse(equal("Working'!!", "Working!!!"))
        self.assertFalse(mentions("Working!!! 1화", ("Working!!",)))
        self.assertFalse(mentions("Working'!! 1화", ("Working",)))
        self.assertTrue(mentions("[Working!!] 1화", ("Working!!",)))
        self.assertFalse(mentions("UnrelatedWorking 1", ("Working",)))

    def test_exact_title_excludes_sequels(self):
        items = [Anime(1, "마법소녀를 동경해서"), Anime(2, "마법소녀를 동경해서 2기"), Anime(3, "다른 작품")]
        self.assertEqual([items[0]], select_candidates(items, "마법소녀를 동경해서", Settings()))

    def test_partial_search_only_uses_actual_titles(self):
        items = [Anime(1, "여친, 빌리겠습니다"), Anime(2, "그녀도 여친"), Anime(3, "다른 작품")]
        self.assertEqual(items[:2], select_candidates(items, "여친", Settings()))

    def test_partial_search_preserves_punctuation_boundaries(self):
        items = [Anime(1, "WORKING!!!"), Anime(2, "WWW.WORKING!!")]
        self.assertEqual([], select_candidates(items, "Working!!", Settings()))
        self.assertEqual(items, select_candidates(items, "Working", Settings()))
        self.assertEqual([], select_candidates(items, " ", Settings()))
        enclosed = Anime(3, "[Working!!] Special")
        self.assertEqual([enclosed], select_candidates([enclosed], "Working!!", Settings()))

    def test_verified_aliases_are_scoped_to_anime(self):
        items = [Anime(1, "작품", titles=("Verified Name",)), Anime(2, "다른 작품")]
        settings = Settings(mappings=(Mapping("작품", 1, 1, ("Alias",)),))
        self.assertEqual([items[0]], select_candidates(items, "Verified Name", settings))
        self.assertEqual([items[0]], select_candidates(items, "Alias", settings))
        scoped = Settings(mappings=(Mapping("작품", 1, 1, ("Alias",), "/library"),))
        self.assertEqual([], select_candidates(items, "Alias", scoped))

    def test_reads_explicit_season_without_title_specific_rules(self):
        for text, expected in (("Test S02E01", 2), ("Test Season 03", 3), ("Test 2기", 2),
                               ("Test 2期", 2), ("시즌2 제10화", 2), ("86", None)):
            with self.subTest(text=text):
                self.assertEqual(expected, explicit_season(text))


class PathTests(unittest.TestCase):
    def test_season_folder_and_release_title(self):
        query, number = parse_media("/media/Working/Season 02/[Group] Working'!! - 01 (1080p).mkv")
        self.assertEqual("Working", query["title"])
        self.assertEqual("Working'!!", query["fileTitle"])
        self.assertEqual(2, query["season"])
        self.assertEqual("/media/Working", query["libraryPath"])
        self.assertEqual("1", number)

    def test_bare_episode_uses_folders(self):
        query, number = parse_media("/media/Working/Season 2/01.mkv")
        self.assertIsNone(query["fileTitle"])
        self.assertEqual(("Working", 2, "1"), (query["title"], query["season"], number))

    def test_dotted_season_filename_and_bare_season_token(self):
        for name, expected in (("Test.S01E02.1080p", "Test"), ("S01E02", None)):
            query, number = parse_media(f"/media/Test/Season 1/{name}.mkv")
            self.assertEqual(expected, query["fileTitle"])
            self.assertEqual("2", number)

    def test_resolution_is_not_episode(self):
        with self.assertRaises(CollectorError):
            parse_media("/media/Test/Test - 1080p.mkv")

    def test_season_number_and_numeric_title_are_not_episode(self):
        for name, title in (("테스트 2기 - 01", "테스트 2기"), ("86 - 01", "86")):
            query, number = parse_media(f"/media/Test/Season 2/{name}.mkv")
            self.assertEqual((title, "1"), (query["fileTitle"], number))

    def test_rejects_folder_filename_season_conflict(self):
        with self.assertRaises(CollectorError) as error:
            parse_media("/media/Working/Season 2/Working S03E01.mkv")
        self.assertEqual("AmbiguousMatch", error.exception.status)

    def test_rejects_multi_episode_video(self):
        for name in ("Working S02E01E02", "Working - 01-02", "Working - 01 & 02"):
            with self.subTest(name=name), self.assertRaises(CollectorError) as error:
                parse_media(f"/media/Working/Season 2/{name}.mkv")
            self.assertEqual("UnsupportedSource", error.exception.status)

    def test_rejects_unidentified_episode_and_special_season(self):
        for path in ("/media/Test/Test.mkv", "/media/Test/Season 0/Test - 01.mkv"):
            with self.subTest(path=path), self.assertRaises(CollectorError):
                parse_media(path)

    def test_rejects_symlink_media_ancestors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "link").symlink_to(root / "real", target_is_directory=True)
            with self.assertRaises(CollectorError):
                parse_media(root / "link" / "Test - 01.mkv")


class AnissiaTests(unittest.TestCase):
    def test_search_exhausts_list_pages_not_autocomplete(self):
        def handler(url, body):
            self.assertNotIn("autocorrect", url)
            self.assertEqual(["Test & Story"], parse_qs(urlsplit(url).query)["q"])
            page = int(urlsplit(url).path.rsplit("/", 1)[1])
            return response({"content": [entry(page + 1)], "last": page == 1})

        session = Replay(handler)
        self.assertEqual([1, 2], [item.anime_id for item in Anissia(session).search("Test & Story", Budget())])
        self.assertEqual(2, len(session.calls))

    def test_total_pages_is_completion_evidence(self):
        session = Replay(lambda url, body: response({"content": [entry()], "totalPages": 1}))
        self.assertEqual(1, len(Anissia(session).search("Test", Budget())))

    def test_missing_progress_and_page_limit_are_incomplete(self):
        cases = [lambda page: {"content": [entry()], "last": False},
                 lambda page: {"content": [], "last": False},
                 lambda page: {"content": [entry(page + 1)], "last": False}]
        for case in cases:
            session = Replay(lambda url, body: response(case(int(urlsplit(url).path.rsplit("/", 1)[1]))))
            with self.subTest(case=case), self.assertRaises(CollectorError) as error:
                Anissia(session).search("Test", Budget())
            self.assertEqual("SearchIncomplete", error.exception.status)

    def test_rejects_invalid_code_data_and_entry_types(self):
        values = [{"code": "error", "data": {}}, {"code": "ok"}, response([]),
                  response({"content": [{}], "last": True}),
                  response({"content": [entry(True)], "last": True}),
                  response({"content": [], "last": "true"}),
                  b'{"code":"ok","code":"error","data":{}}']
        for value in values:
            with self.subTest(value=value), self.assertRaises(CollectorError):
                Anissia(Replay(lambda url, body: value)).search("Test", Budget())

    def test_detail_validates_id_and_title(self):
        client = Anissia(Replay(lambda url, body: response({"subject": "Test"})))
        self.assertEqual(Anime(123, "Test"), client.detail(123, Budget()))
        for value in (0, -1, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.detail(value, Budget())
        with self.assertRaises(CollectorError):
            Anissia(Replay(lambda url, body: response({"subject": ""}))).detail(1, Budget())
        with self.assertRaises(CollectorError):
            Anissia(Replay(lambda url, body: response({"subject": "Test", "originalSubject": False}))).detail(1, Budget())

    def test_captions_normalize_episodes_and_sort(self):
        session = Replay(lambda url, body: response([
            {"episode": "01.50", "website": "https://example.com/b", "name": "B", "updDt": "2025"},
            {"episode": 2, "website": "https://example.com/a", "name": "A", "updDt": "2026"}]))
        result = Anissia(session).captions(1, Budget())
        self.assertEqual(["2", "1.5"], [item.episode for item in result])

    def test_captions_reject_malformed_urls_and_episode(self):
        for item in ({"episode": "1", "website": "file:///etc/passwd"},
                     {"episode": True, "website": "https://example.com/"},
                     {"episode": "NaN", "website": "https://example.com/"}):
            with self.subTest(item=item), self.assertRaises((CollectorError, ValueError)):
                Anissia(Replay(lambda url, body: response([item]))).captions(1, Budget())


class MetadataTests(unittest.TestCase):
    def test_returns_verified_titles_and_relation_ids(self):
        session = Replay(lambda url, body: {"data": {"Page": {"media": [metadata()]}}})
        result = metadata_titles(session, "Native Story", Budget())
        self.assertEqual([{"id": 10, "titles": ("Native Story", "物語"), "format": "TV", "year": 2026, "relatedIds": (11,)}], result)
        self.assertEqual("Native Story", session.calls[0][1]["variables"]["search"])

    def test_errors_are_not_empty_results(self):
        for value in ({"errors": []}, {"data": {"Page": {"media": [{}]}}}, {"data": {}}):
            with self.subTest(value=value), self.assertRaises(CollectorError):
                metadata_titles(Replay(lambda url, body: value), "Test", Budget())


class ResolverTests(unittest.TestCase):
    def session(self, title="Test", original="テスト"):
        def handler(url, body):
            if "/anime/list/" in url:
                return response({"content": [entry(1, title, original)], "last": True})
            return response(entry(1, title, original))

        return Replay(handler)

    def test_explicit_mapping_skips_title_and_metadata_search(self):
        session = self.session("워킹 2기", "Working'!!")
        settings = Settings(mappings=(Mapping("Working", 2, 123, ("Library Alias",)),))
        result = Resolver(session, settings).resolve({"title": "Working", "season": 2}, Budget())
        self.assertEqual((123, 2), (result.anime_id, result.season))
        self.assertIn("Library Alias", result.titles)
        self.assertEqual(1, len(session.calls))
        self.assertIn("/anime/animeNo/123", session.calls[0][0])

    def test_explicit_id_conflicting_with_mapping_is_rejected(self):
        resolver = Resolver(self.session(), Settings(mappings=(Mapping("Test", 1, 1),)))
        with self.assertRaises(CollectorError):
            resolver.resolve({"title": "Test", "season": 1, "animeId": 2}, Budget())

    def test_mapping_path_and_season_are_scoped(self):
        mapping = Mapping("Test", 2, 1, ("Alias",), "/library/Test")
        resolver = Resolver(self.session(), Settings(mappings=(mapping,)))
        self.assertEqual(mapping, resolver.get_mapping({"title": "Alias", "season": 2, "libraryPath": "/library/Test"}))
        self.assertIsNone(resolver.get_mapping({"title": "Alias", "season": 1, "libraryPath": "/library/Test"}))
        self.assertIsNone(resolver.get_mapping({"title": "Alias", "season": 2, "libraryPath": "/other"}))

    def test_multiple_mappings_are_ambiguous(self):
        resolver = Resolver(self.session(), Settings(mappings=(Mapping("Test", 1, 1), Mapping("Test", 2, 2))))
        with self.assertRaises(CollectorError):
            resolver.get_mapping({"title": "Test"})

    def test_exact_title_and_first_season_resolve(self):
        result = Resolver(self.session(), Settings(False)).resolve({"title": "Test", "season": 1}, Budget())
        self.assertEqual(1, result.anime_id)
        self.assertIn("テスト", result.titles)

    def test_later_season_requires_proof(self):
        with self.assertRaises(CollectorError) as error:
            Resolver(self.session(), Settings(False)).resolve({"title": "Test", "season": 2}, Budget())
        self.assertEqual("AmbiguousMatch", error.exception.status)

    def test_punctuation_filename_selects_exact_sequel(self):
        session = self.session("워킹 2기", "Working'!!")
        result = Resolver(session, Settings(False)).resolve({"title": "Working", "fileTitle": "Working'!!", "season": 2}, Budget())
        self.assertEqual((1, 2), (result.anime_id, result.season))

    def test_romaji_resolves_through_verified_native_alias(self):
        def handler(url, body):
            if body is not None:
                return {"data": {"Page": {"media": [metadata()]}}}
            if "/anime/list/" in url:
                title = parse_qs(urlsplit(url).query)["q"][0]
                return response({"content": [entry(3, "이야기", "物語 長い副題")] if title == "物語" else [], "last": True})
            return response(entry(3, "이야기", "物語 長い副題"))

        result = Resolver(Replay(handler), Settings()).resolve({"title": "Native Story", "season": 1}, Budget())
        self.assertEqual(3, result.anime_id)
        self.assertIn("Native Story", result.titles)

    def test_unverified_metadata_alias_is_not_accepted(self):
        session = Replay(lambda url, body: {"data": {"Page": {"media": [metadata()]}}}
                         if body else response({"content": [], "last": True}))
        with self.assertRaises(CollectorError) as error:
            Resolver(session, Settings()).resolve({"title": "Unrelated", "season": 1}, Budget())
        self.assertEqual("AmbiguousMatch", error.exception.status)

    def test_movies_and_multiple_metadata_matches_are_not_selected(self):
        for items in ([metadata(format="MOVIE")], [metadata(), metadata(number=20)]):
            session = Replay(lambda url, body: {"data": {"Page": {"media": items}}}
                             if body else response({"content": [], "last": True}))
            with self.subTest(items=items), self.assertRaises(CollectorError):
                Resolver(session, Settings()).resolve({"title": "Native Story", "season": 1}, Budget())

    def test_incomplete_lookup_is_not_ambiguity(self):
        session = Replay(lambda url, body: response({"content": [], "last": False}))
        with self.assertRaises(CollectorError) as error:
            Resolver(session, Settings(False)).resolve({"title": "Test"}, Budget())
        self.assertEqual("SearchIncomplete", error.exception.status)

    def test_cancellation_is_not_swallowed(self):
        budget = Budget()
        budget.cancel.set()
        with self.assertRaises(Cancelled):
            Resolver(self.session(), Settings()).resolve({"title": "Test"}, budget)


class LocalEvidenceTests(unittest.TestCase):
    def resolve(self, root, **query):
        return Resolver(ResolverTests().session(), Settings(False)).resolve(
            {"title": "Test", "season": 1, "libraryPath": str(root), **query}, Budget())

    def test_nfo_season_conflict_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tvshow.nfo").write_text("<tvshow><title>Other</title><season>2</season><uniqueid type='anissia'>99</uniqueid></tvshow>")
            with self.assertRaises(CollectorError) as error:
                self.resolve(root)
            self.assertEqual("AmbiguousMatch", error.exception.status)

    def test_local_title_and_id_conflicts_are_rejected(self):
        for contents in ("<tvshow><title>Other</title></tvshow>",
                         "<tvshow><title>Test</title><uniqueid type='anissia'>99</uniqueid></tvshow>"):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "tvshow.nfo").write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(CollectorError):
                    self.resolve(root, animeId=1)

    def test_episode_nfo_uses_showtitle_not_episode_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "Test - 01.mkv"
            media.with_suffix(".nfo").write_text("<episodedetails><title>First episode</title><showtitle>Test</showtitle><season>1</season></episodedetails>")
            self.assertEqual(1, self.resolve(root, mediaPath=str(media)).anime_id)

    def test_external_provider_ids_are_not_anissia_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tvshow.nfo").write_text("<tvshow><title>Test</title><uniqueid type='tvdb'>99</uniqueid><id>12345</id></tvshow>")
            (root / "metadata.json").write_text(json.dumps({"title": "Test", "ProviderIds": {"Tvdb": "999", "Imdb": "tt123"}}))
            self.assertEqual(1, self.resolve(root).anime_id)

    def test_local_anissia_id_can_supply_identity_without_title_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "metadata.json").write_text(json.dumps({"title": "Test", "ProviderIds": {"Anissia": "1"}}))
            session = ResolverTests().session()
            result = Resolver(session, Settings(False)).resolve({"libraryPath": str(root)}, Budget())
            self.assertEqual(1, result.anime_id)
            self.assertEqual(1, len(session.calls))

    def test_xml_dtd_entities_and_depth_are_rejected(self):
        values = [b'<!DOCTYPE tvshow [<!ENTITY x "Test">]><tvshow><title>&x;</title></tvshow>',
                  b'<!DOCTYPE tvshow SYSTEM "file:///etc/passwd"><tvshow/>',
                  ("<x>" * 33 + "Test" + "</x>" * 33).encode()]
        values.append(values[0].decode().encode("utf-16"))
        for value in values:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "tvshow.nfo").write_bytes(value)
                with self.subTest(value=value[:40]), self.assertRaises(CollectorError):
                    self.resolve(root)

    def test_json_depth_and_size_are_bounded(self):
        for data in (b" " * (1024 * 1024 + 1), ("{\"x\":" * 17 + "{}" + "}" * 17).encode()):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "metadata.json").write_bytes(data)
                with self.assertRaises(CollectorError):
                    self.resolve(root)

    def test_symlink_metadata_and_ancestors_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "outside.json").write_text('{"title":"Test"}')
            (root / "metadata.json").symlink_to(root / "outside.json")
            with self.assertRaises(CollectorError):
                self.resolve(root)
            (root / "link").symlink_to(root, target_is_directory=True)
            with self.assertRaises(CollectorError):
                self.resolve(root / "link")


if __name__ == "__main__":
    unittest.main()
