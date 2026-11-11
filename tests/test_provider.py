import base64
import copy
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from integrations.bazarr_plus import provider
from arr_subtitle.models import CollectorError
from arr_subtitle.network import Response, Session


SAMI = "<SAMI><BODY><SYNC Start=0><P>안녕하세요</P></BODY></SAMI>".encode("utf-8")
VIDEO = {"kind": "episode", "series": "Series", "season": 1, "episode": 1}
KOREAN = {"alpha3": "kor", "hi": False, "forced": False}
CONFIG = {"fallback_blogs": "https://author.blogspot.com/", "anilist_enabled": False}


def fixture_transport(*, archive=False, failed=False, empty=False, changed=False):
    calls = []

    def transport(url, method, body, timeout, limit):
        calls.append(url)
        if "/anime/list/" in url:
            result = {"code": "ok", "data": {"content": [{"animeNo": 1, "subject": "Series", "originalSubject": "Series"}], "last": True}}
        elif "/anime/animeNo/" in url:
            result = {"code": "ok", "data": {"subject": "Series", "originalSubject": "Series"}}
        elif "caption/animeNo/" in url:
            result = {"code": "ok", "data": []}
        elif url == "https://failed.blogspot.com/" or url.startswith("https://failed.blogspot.com/"):
            raise OSError("Fixture source failed")
        elif url == "https://author.blogspot.com/post":
            attachment = "Series.zip" if archive else "Series 01.smi"
            href = "https://files.example.com/" + ("changed.zip" if changed else "batch.zip") if archive else "https://files.example.com/" + ("changed.smi" if changed else "Series01.smi")
            return Response(url, f"<h1>Series 01화</h1><div class='post-body'><a href='{href}'>{attachment}</a></div>".encode("utf-8"), "text/html")
        elif "/feeds/" in url:
            if failed:
                raise OSError("Fixture source failed")
            attachment = "Series.zip" if archive else "Series 01.smi"
            href = "https://files.example.com/" + ("changed.zip" if changed else "batch.zip") if archive else "https://files.example.com/" + ("changed.smi" if changed else "Series01.smi")
            result = {"feed": {"openSearch$totalResults": {"$t": "0" if empty else "1"}, "entry": [] if empty else [
                {"title": {"$t": "Series 01화"}, "content": {"$t": f"<a href='{href}'>{attachment}</a>"},
                 "link": [{"rel": "alternate", "href": "https://author.blogspot.com/post"}]}], "link": []}}
        elif url.startswith("https://files.example.com/"):
            if archive:
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as handle:
                    handle.writestr("Series 01.smi", SAMI)
                return Response(url, output.getvalue(), "application/zip")
            return Response(url, SAMI, "application/x-sami")
        else:
            raise AssertionError(f"Unexpected fixture URL: {url}")
        return Response(url, json.dumps(result).encode("utf-8"), "application/json")

    return transport, calls


class ProviderTests(unittest.TestCase):
    def search(self, transport, config=None, video=None):
        with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)):
            return provider.ArrSubtitleProvider().search(video or VIDEO, [KOREAN], config or CONFIG)

    def test_search_download_uses_fresh_transport_and_sami(self):
        transport, calls = fixture_transport()
        output = io.StringIO()
        with redirect_stdout(output):
            candidates = self.search(transport)
            self.assertEqual(len(candidates), 1)
            result = candidates[0]
            self.assertEqual(result["matches"], ["series", "season", "episode"])
            self.assertFalse(result["hash_verifiable"])
            self.assertFalse(result["hearing_impaired_verifiable"])
            self.assertNotIn("fps", result)
            self.assertNotIn("release_info", result)
            first_calls = len(calls)
            with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)):
                downloaded = provider.ArrSubtitleProvider().download(json.loads(json.dumps(result["provider_payload"])), KOREAN, CONFIG)
        self.assertEqual(output.getvalue(), "")
        self.assertGreater(len(calls), first_calls)
        self.assertEqual(base64.b64decode(downloaded["content_b64"]), SAMI)
        self.assertEqual(downloaded["format"], "sami")

    def test_archive_member_is_pinned_and_id_is_stable(self):
        transport, _ = fixture_transport(archive=True)
        first = self.search(transport)[0]
        second = self.search(transport, video={**VIDEO, "original_path": "/unrelated/path.mkv", "tvdb_id": 999})[0]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["provider_payload"]["identity"]["member"], "Series 01.smi")
        self.assertEqual(first["provider_payload"]["query"], {"title": "Series", "season": 1})
        with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)):
            downloaded = provider.ArrSubtitleProvider().download(first["provider_payload"], KOREAN, CONFIG)
        self.assertEqual(downloaded["format"], "sami")

    def test_download_verifies_selected_page_without_blog_rediscovery(self):
        transport, calls = fixture_transport(archive=True)
        selected = self.search(transport)[0]["provider_payload"]
        calls.clear()
        def selected_only(url, method, body, timeout, limit):
            if "/feeds/" in url:
                raise AssertionError("Download must not exhaust blog discovery")
            return transport(url, method, body, timeout, limit)
        with patch.object(provider, "Session", side_effect=lambda: Session(selected_only, interval=0)):
            result = provider.ArrSubtitleProvider().download(selected, KOREAN, CONFIG)
        self.assertEqual(base64.b64decode(result["content_b64"]), SAMI)
        self.assertIn("https://author.blogspot.com/post", calls)
        self.assertFalse(any("/feeds/" in url for url in calls))

    def test_custom_domain_detects_blog_platform_for_search_and_download(self):
        original, calls = fixture_transport()
        def transport(url, method, body, timeout, limit):
            if url == "https://author.example.com/":
                return Response(url, b"<meta name='generator' content='Blogger'>", "text/html")
            mapped = url.replace("author.example.com", "author.blogspot.com")
            response = original(mapped, method, body, timeout, limit)
            return Response(url, response.data.replace(b"author.blogspot.com", b"author.example.com"), response.content_type, response.filename)
        config = {**CONFIG, "fallback_blogs": "https://author.example.com/"}
        selected = self.search(transport, config)[0]["provider_payload"]
        self.assertEqual(selected["identity"]["page"], "https://author.example.com/post")
        with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)):
            result = provider.ArrSubtitleProvider().download(selected, KOREAN, config)
        self.assertEqual(base64.b64decode(result["content_b64"]), SAMI)

    def test_unsupported_languages_media_and_flags_do_not_open_session(self):
        instance = provider.ArrSubtitleProvider()
        with patch.object(provider, "Session") as session:
            for language in ({"alpha3": "eng"}, {**KOREAN, "hi": True}, {**KOREAN, "forced": True}):
                self.assertEqual(instance.search(VIDEO, [language], CONFIG), [])
            self.assertEqual(instance.search({**VIDEO, "kind": "movie"}, [KOREAN], CONFIG), [])
            session.assert_not_called()

    def test_malformed_config_is_not_hidden(self):
        for config in ({"timeout": 0}, {"timeout": 21}, {"timeout": True}, {"anilist_enabled": "false"},
                       {"mappings_json": "{}"}, {"mappings_json": "[NaN]"}, {"mappings_json": "["},
                       {"fallback_blogs": "http://127.0.0.1/"}, {"fallback_blogs": []}, {"unknown": True}):
            with self.subTest(config=config), self.assertRaises((ValueError, CollectorError)):
                provider.ArrSubtitleProvider().search(VIDEO, [KOREAN], config)

    def test_config_uses_isolated_normalized_core_settings(self):
        config = {**CONFIG, "fallback_blogs": "\nhttps://author.blogspot.com/\nhttps://author.blogspot.com/\n",
                  "mappings_json": '[{"title":"Series","season":1,"animeId":1}]'}
        settings, timeout, _ = provider._config(config)
        self.assertEqual(timeout, 20)
        self.assertEqual(len(settings.blogs), 1)
        self.assertEqual(settings.mappings[0].anime_id, 1)
        self.assertFalse(settings.anilist_enabled)

    def test_known_blog_platforms_match_core_configuration_names(self):
        settings, _, _ = provider._config({"fallback_blogs": "https://author.blogspot.com/\nhttps://author.tistory.com/\nhttps://blog.naver.com/author"})
        self.assertEqual([blog.platform for blog in settings.blogs], ["Blogspot", "Tistory", "Naver"])

    def test_missing_or_ambiguous_video_fields_raise(self):
        for values in ({"series": None}, {"season": None}, {"season": 0}, {"episode": [1, 2]}, {"episode": True}):
            with self.subTest(values=values), self.assertRaises((ValueError, CollectorError)):
                provider.ArrSubtitleProvider().search({**VIDEO, **values}, [KOREAN], CONFIG)

    def test_settings_change_requires_another_search(self):
        transport, _ = fixture_transport()
        selected = self.search(transport)[0]["provider_payload"]
        with patch.object(provider, "Session") as session, self.assertRaises(ValueError):
            provider.ArrSubtitleProvider().download(selected, KOREAN, {**CONFIG, "anilist_enabled": True})
        session.assert_not_called()

    def test_payload_tampering_is_rejected_before_fetch(self):
        transport, _ = fixture_transport()
        selected = self.search(transport)[0]["provider_payload"]
        for field, value in (("attachment_url", "http://127.0.0.1/private"), ("member", "../../secret.smi"),
                             ("anime_id", True), ("season", "1"), ("episode", "2")):
            malicious = copy.deepcopy(selected)
            malicious["identity"][field] = value
            malicious["id"] = provider._digest(malicious["identity"])
            with self.subTest(field=field), patch.object(provider, "Session") as session, self.assertRaises(ValueError):
                provider.ArrSubtitleProvider().download(malicious, KOREAN, CONFIG)
            session.assert_not_called()

    def test_rehashed_public_url_cannot_bypass_fresh_candidate_proof(self):
        transport, calls = fixture_transport()
        malicious = self.search(transport)[0]["provider_payload"]
        malicious["identity"]["attachment_url"] = "https://attacker.example.com/private.smi"
        malicious["id"] = provider._digest(malicious["identity"])
        with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)), self.assertRaises(CollectorError):
            provider.ArrSubtitleProvider().download(malicious, KOREAN, CONFIG)
        self.assertNotIn("https://attacker.example.com/private.smi", calls)

    def test_stale_candidate_is_not_replaced_by_new_attachment(self):
        transport, _ = fixture_transport()
        selected = self.search(transport)[0]["provider_payload"]
        changed, _ = fixture_transport(changed=True)
        with patch.object(provider, "Session", side_effect=lambda: Session(changed, interval=0)), self.assertRaises(CollectorError):
            provider.ArrSubtitleProvider().download(selected, KOREAN, CONFIG)

    def test_partial_failure_warns_only_on_stderr(self):
        transport, _ = fixture_transport()
        config = {**CONFIG, "fallback_blogs": CONFIG["fallback_blogs"] + "\nhttps://failed.blogspot.com/"}
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(len(self.search(transport, config)), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("partial discovery", errors.getvalue())
        self.assertLess(len(errors.getvalue()), 300)

    def test_zero_results_and_failed_discovery_raise(self):
        transport, _ = fixture_transport(failed=True)
        with self.assertRaises(CollectorError) as error:
            self.search(transport)
        self.assertEqual(error.exception.status, "SearchIncomplete")

    def test_complete_empty_discovery_returns_no_candidates(self):
        transport, _ = fixture_transport(empty=True)
        self.assertEqual(self.search(transport), [])

    def test_provider_does_not_write_video_sidecars(self):
        transport, _ = fixture_transport()
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "Series S01E01.mkv"
            video.write_bytes(b"fixture")
            before = {p.name: p.read_bytes() for p in Path(directory).iterdir()}
            selected = self.search(transport, video={**VIDEO, "name": str(video), "original_path": str(video)})[0]
            with patch.object(provider, "Session", side_effect=lambda: Session(transport, interval=0)):
                provider.ArrSubtitleProvider().download(selected["provider_payload"], KOREAN, CONFIG)
            self.assertEqual({p.name: p.read_bytes() for p in Path(directory).iterdir()}, before)
