import json
import unittest
from urllib.parse import parse_qs, urlsplit

from arr_subtitle.blogs import Explorer, detect_blog, in_scope, known_blog
from arr_subtitle.models import Blog, Budget, Cancelled, CollectorError
from arr_subtitle.network import Response


class Replay:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, budget, limit=4194304, scope=None):
        budget.check()
        if scope is not None and not scope(url):
            raise AssertionError("Request outside scope")
        self.calls.append(url)
        response = self.handler(url)
        if response is None:
            raise CollectorError("SearchIncomplete", "HTTP 404 request failed")
        return response


def html(url, body):
    return Response(url, body.encode("utf-8"), "text/html")


def xml(url, body):
    return Response(url, body.encode("utf-8"), "application/xml")


def entry(number, *, title=None, summary=False):
    return {
        "title": {"$t": title or f"Test {number} episode"},
        "summary" if summary else "content": {"$t": f"<a href='/file{number}.smi'>file{number}.smi</a>"},
        "link": [{"rel": "alternate", "href": f"https://author.blogspot.com/{number}.html"}],
        "category": [{"term": "Test"}],
    }


def feed(url, entries=(), *, total=None, next_url=None):
    value = {"entry": list(entries), "link": []}
    if total is not None:
        value["openSearch$totalResults"] = {"$t": str(total)}
    if next_url is not None:
        value["link"].append({"rel": "next", "href": next_url})
    return Response(url, json.dumps({"feed": value}).encode(), "application/json")


class DetectionTests(unittest.TestCase):
    def test_known_platforms_and_naver_scope(self):
        for url, platform, blog_id in (
            ("https://m.blog.naver.com/author/123", "naver", "author"),
            ("https://blog.naver.com/PostView.naver?blogId=author&logNo=123", "naver", "author"),
            ("https://author.blogspot.com/2026/11/post.html", "blogspot", "author.blogspot.com"),
            ("https://author.tistory.com/123", "tistory", "author.tistory.com"),
        ):
            with self.subTest(url=url):
                blog = known_blog(url)
                self.assertEqual((blog.platform, blog.id), (platform, blog_id))
        blog = known_blog("https://blog.naver.com/author")
        self.assertTrue(in_scope(blog, "https://m.blog.naver.com/author/123"))
        self.assertFalse(in_scope(blog, "https://blog.naver.com/other/123"))
        self.assertFalse(in_scope(blog, "https://blog.naver.com/PostView.naver?blogId=other"))
        self.assertFalse(in_scope(blog, "https://blog.naver.com/PostView.naver?blogId=author&blogId=other"))
        self.assertFalse(in_scope(blog, "https://blog.naver.com/other/123?blogId=author"))

    def test_registration_does_not_enumerate_known_blog(self):
        session = Replay(lambda _: self.fail("Unexpected request"))
        self.assertEqual(detect_blog(session, "https://author.blogspot.com/", Budget()).platform, "blogspot")
        self.assertFalse(session.calls)

    def test_custom_domain_detection_and_generic_detection(self):
        for body, platform in (
            ("<meta name='generator' content='Blogger'>", "blogspot"),
            ("<link href='/feeds/posts/default' type='application/atom+xml'>", "blogspot"),
            ("<script src='https://t1.daumcdn.net/tistory.com/skin.js'></script>", "tistory"),
            ("<main>Blog</main>", "generic"),
        ):
            with self.subTest(platform=platform):
                session = Replay(lambda url: html(url, body))
                blog = detect_blog(session, "https://example.com/post", Budget())
                self.assertEqual(blog.platform, platform)
                self.assertEqual(blog.url, "https://example.com/")
                self.assertEqual(len(session.calls), 1)

    def test_detect_redirect_and_reject_unsupported_structure(self):
        session = Replay(lambda _: html("https://author.tistory.com/1", ""))
        self.assertEqual(detect_blog(session, "https://example.com/", Budget()).platform, "tistory")
        with self.assertRaises(CollectorError):
            detect_blog(Replay(lambda url: html(url, "<p>No blog</p>")), "https://example.com/", Budget())

    def test_scope_uses_path_boundaries_and_rejects_unsafe_urls(self):
        blog = Blog("https://example.com/blog/", "generic", "example.com:/blog/")
        for url in ("https://example.com/blog/post", "http://example.com/blog/post"):
            self.assertTrue(in_scope(blog, url))
        for url in (
            "https://example.com/blogroll/post", "https://example.com/blog/../outside",
            "https://example.com/blog/%2e%2e/outside", "https://other.com/blog/post",
            "https://user@example.com/blog/", "https://example.com:8080/blog/", "file:///blog/post",
        ):
            with self.subTest(url=url):
                self.assertFalse(in_scope(blog, url))
        for url in ("http://127.0.0.1/", "https://localhost/", "https://example.com:22/", "https://u:p@example.com/"):
            with self.subTest(url=url), self.assertRaises(CollectorError):
                known_blog(url)

    def test_invalid_naver_id_is_not_an_api_path(self):
        for url in ("https://blog.naver.com/", "https://blog.naver.com/PostView.naver", "https://blog.naver.com/?blogId=bad%2Fid"):
            with self.subTest(url=url), self.assertRaises(CollectorError):
                known_blog(url)


class ReadTests(unittest.TestCase):
    def test_naver_wrapper_uses_naver_body_instead_of_sidebar(self):
        def handler(url):
            if urlsplit(url).path == "/author/1":
                return html(url, "<iframe id='mainFrame' src='/PostView.naver?blogId=author&logNo=1'></iframe>")
            return html(url, "<meta property='og:title' content='테스트 1화'><div class='post-body'>sidebar</div><div class='se-main-container'><a href='/01.smi'>01.smi</a></div>")
        session = Replay(handler)
        post = Explorer(session).read("https://blog.naver.com/author/1", Budget())
        self.assertEqual(post.title, "테스트 1화")
        self.assertIn("01.smi", post.html)
        self.assertNotIn("sidebar", post.html)
        self.assertEqual(len(session.calls), 2)

    def test_naver_wrapper_scope_and_cycles(self):
        for frame in ("/PostView.naver?blogId=other&logNo=1", "/author/1"):
            with self.subTest(frame=frame), self.assertRaises(CollectorError):
                session = Replay(lambda url: html(url, f"<iframe id='mainFrame' src='{frame}'></iframe>"))
                Explorer(session).read("https://blog.naver.com/author/1", Budget())

    def test_read_rejects_out_of_scope_redirect(self):
        session = Replay(lambda _: html("https://other.com/post", "<article>body</article>"))
        with self.assertRaises(CollectorError):
            Explorer(session).read("https://example.com/post", Budget())

    def test_category_script_and_category_link(self):
        for category_html in (
            '<script>T.entryInfo = {"categoryLabel":"Shows/Test"};</script>',
            "<div class='category'><a href='/category/Shows/Test'>Test</a></div>",
        ):
            with self.subTest(category_html=category_html):
                session = Replay(lambda url: html(url, category_html + "<h1>Test 1</h1><div class='contents_style'>body</div><a rel='tag'>Test</a>"))
                post = Explorer(session).read("https://author.tistory.com/1", Budget())
                self.assertEqual(post.category_url, "https://author.tistory.com/category/Shows/Test")
                self.assertEqual(post.labels, ("Test",))

    def test_category_script_rejects_path_traversal(self):
        session = Replay(lambda url: html(url, '<script>T.entryInfo = {"categoryLabel":"../Test"};</script><article>body</article>'))
        post = Explorer(session).read("https://author.tistory.com/1", Budget())
        self.assertIsNone(post.category_url)

    def test_read_requires_body_and_honors_response_limit(self):
        for body in (b"<p>No isolated body</p>", b"x" * (4194304 + 1)):
            with self.subTest(size=len(body)), self.assertRaises(CollectorError):
                Explorer(Replay(lambda url: Response(url, body))).read("https://example.com/post", Budget())


class BloggerTests(unittest.TestCase):
    def setUp(self):
        self.blog = known_blog("https://author.blogspot.com/")

    def test_exhaustive_pagination_validates_total_and_keeps_all_posts(self):
        def handler(url):
            if "start-index" in url:
                return feed(url, [entry(2, title="Other title")], total=2)
            return feed(url, [entry(1)], total=2, next_url="https://www.blogger.com/feeds/123/posts/default?alt=json&start-index=2")
        session = Replay(handler)
        explorer = Explorer(session)
        posts = list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].labels, ("Test",))
        self.assertTrue(explorer.complete)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all(urlsplit(url).hostname == "author.blogspot.com" for url in session.calls))
        self.assertNotIn("q=", session.calls[0])

    def test_missing_totals_never_mark_exhaustive_complete(self):
        for first_total, second_total in ((None, None), (2, None), (None, 2)):
            with self.subTest(totals=(first_total, second_total)):
                session = Replay(lambda url: feed(url, [entry(2)], total=second_total) if "start-index" in url else feed(url, [entry(1)], total=first_total, next_url="?start-index=2"))
                explorer = Explorer(session)
                self.assertEqual(len(list(explorer.explore(self.blog, ("Test",), Budget()))), 2)
                self.assertFalse(explorer.complete)

    def test_nonexhaustive_uses_title_query_and_marks_query_complete(self):
        session = Replay(lambda url: feed(url, [entry(1, summary=True)]))
        explorer = Explorer(session)
        posts = list(explorer.explore(self.blog, ("Test title",), Budget(), exhaustive=False))
        self.assertTrue(explorer.complete)
        self.assertTrue(posts[0].summary)
        self.assertEqual(parse_qs(urlsplit(session.calls[0]).query)["q"], ["Test title"])

    def test_invalid_total_changed_total_and_omissions_raise_incomplete(self):
        for total in ("bad", -1, 2):
            with self.subTest(total=total):
                explorer = Explorer(Replay(lambda url: feed(url, [entry(1)], total=total)))
                with self.assertRaises(CollectorError) as caught:
                    list(explorer.explore(self.blog, ("Test",), Budget()))
                self.assertEqual(caught.exception.status, "SearchIncomplete")
                self.assertFalse(explorer.complete)
        session = Replay(lambda url: feed(url, [entry(2)], total=3) if "start-index" in url else feed(url, [entry(1)], total=2, next_url="?start-index=2"))
        with self.assertRaises(CollectorError):
            list(Explorer(session).explore(self.blog, ("Test",), Budget()))

    def test_repeated_content_and_repeated_urls_raise(self):
        for next_url in ("?alt=json&max-results=150", "?start-index=2"):
            with self.subTest(next_url=next_url):
                session = Replay(lambda url: feed(url, [entry(1)], total=2, next_url=next_url))
                explorer = Explorer(session)
                with self.assertRaises(CollectorError):
                    list(explorer.explore(self.blog, ("Test",), Budget()))
                self.assertFalse(explorer.complete)
                self.assertLessEqual(len(session.calls), 2)

    def test_repeated_redirect_destination_is_incomplete(self):
        def handler(url):
            number = 2 if "start-index" in url else 1
            return feed("https://author.blogspot.com/feeds/posts/default", [entry(number)], total=2, next_url="?start-index=2" if number == 1 else None)
        explorer = Explorer(Replay(handler))
        with self.assertRaises(CollectorError):
            list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertFalse(explorer.complete)

    def test_redirected_feed_outside_registered_scope_is_rejected(self):
        explorer = Explorer(Replay(lambda _: feed("https://other.blogspot.com/feeds/posts/default", total=0)))
        with self.assertRaises(CollectorError):
            list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertFalse(explorer.complete)

    def test_out_of_scope_pagination_and_entry_are_rejected(self):
        session = Replay(lambda url: feed(url, [entry(1)], total=2, next_url="https://other.com/feed"))
        with self.assertRaises(CollectorError):
            list(Explorer(session).explore(self.blog, ("Test",), Budget()))
        post = entry(1)
        post["link"][0]["href"] = "https://other.com/1"
        with self.assertRaises(CollectorError):
            list(Explorer(Replay(lambda url: feed(url, [post], total=1))).explore(self.blog, ("Test",), Budget()))

    def test_empty_feed_with_zero_total_is_complete(self):
        explorer = Explorer(Replay(lambda url: feed(url, total=0)))
        self.assertEqual(list(explorer.explore(self.blog, (), Budget())), [])
        self.assertTrue(explorer.complete)

    def test_empty_initial_page_can_have_older_posts(self):
        session = Replay(lambda url: feed(url, [entry(1)], total=1) if "start-index" in url else feed(url, total=1, next_url="?start-index=2"))
        explorer = Explorer(session)
        self.assertEqual(len(list(explorer.explore(self.blog, ("Test",), Budget()))), 1)
        self.assertTrue(explorer.complete)

    def test_malformed_json_and_excessive_json_depth_are_incomplete(self):
        payloads = (b'{"feed":{},"feed":{}}', b'{"feed":NaN}', b"[" * 40 + b"0" + b"]" * 40, b"<html>Error</html>")
        for payload in payloads:
            with self.subTest(payload=payload[:40]), self.assertRaises(CollectorError):
                explorer = Explorer(Replay(lambda url: Response(url, payload)))
                list(explorer.explore(self.blog, ("Test",), Budget()))

    def test_blogger_page_limit_reports_incomplete(self):
        def handler(url):
            number = int(parse_qs(urlsplit(url).query).get("start-index", [1])[0])
            return feed(url, [entry(number)], total=101, next_url=f"?start-index={number + 1}")
        session = Replay(handler)
        explorer = Explorer(session)
        with self.assertRaises(CollectorError):
            list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertEqual(len(session.calls), 100)
        self.assertFalse(explorer.complete)


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.blog = Blog("https://example.com/", "generic", "example.com:/")

    def test_generic_sitemap_finds_older_post_and_proves_completion(self):
        def handler(url):
            path = urlsplit(url).path
            if path == "/":
                return html(url, "<main>Blog</main>")
            if path == "/sitemap.xml":
                return xml(url, "<urlset><url><loc>https://example.com/older</loc></url></urlset>")
            if path == "/older":
                return html(url, "<meta property='og:title' content='Test 1'><article>body</article>")
            return None
        explorer = Explorer(Replay(handler))
        posts = list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertEqual([post.title for post in posts], ["Test 1"])
        self.assertTrue(explorer.complete)

    def test_missing_sitemap_post_and_outside_location_are_incomplete(self):
        for location in ("https://example.com/deleted", "https://other.com/post"):
            with self.subTest(location=location):
                session = Replay(lambda url: xml(url, f"<urlset><url><loc>{location}</loc></url></urlset>") if url.endswith("sitemap.xml") else html(url, "<main>Blog</main>") if url == self.blog.url else None)
                explorer = Explorer(session)
                list(explorer.explore(self.blog, ("Test",), Budget()))
                self.assertFalse(explorer.complete)
                self.assertFalse(any("other.com" in url for url in session.calls))

    def test_fragment_category_variants_deduplicate_without_sidebar_crawl(self):
        def handler(url):
            if url == self.blog.url:
                return html(url, "<main><a href='/1'>Test 1</a><a href='/1?category=123#comment'>Test 1</a><a href='/unrelated?page=1'>Other</a></main><aside><a href='/sidebar'>Test related sidebar</a></aside>")
            if urlsplit(url).path == "/1":
                return html(url, "<h1>Test 1</h1><article>body</article>")
            return None
        session = Replay(handler)
        list(Explorer(session).explore(self.blog, ("Test",), Budget()))
        self.assertEqual(sum(urlsplit(url).path == "/1" for url in session.calls), 1)
        self.assertFalse(any("unrelated" in url or "sidebar" in url for url in session.calls))

    def test_attachment_title_does_not_schedule_binary_as_blog_page(self):
        def handler(url):
            if url == self.blog.url:
                return html(url, "<a href='/1'>Test 1</a>")
            if url.endswith("/1"):
                return html(url, "<h1>Test 1</h1><article><a href='/Test 1.smi'>Test 1.smi</a></article>")
            return None
        session = Replay(handler)
        posts = list(Explorer(session).explore(self.blog, ("Test",), Budget()))
        self.assertEqual(len(posts), 1)
        self.assertFalse(any(url.endswith(".smi") for url in session.calls))

    def test_tistory_categories_run_first_with_inherited_post_category(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        def handler(url):
            if urlsplit(url).path == "/category/Test":
                return html(url, "<div class='list_content'><a href='/1'>Episode 1</a></div>")
            if urlsplit(url).path == "/1":
                return html(url, "<h1>Episode 1</h1><div class='contents_style'>body</div>")
            return None
        session = Replay(handler)
        explorer = Explorer(session)
        posts = list(explorer.explore(blog, ("Test",), Budget(), categories=(category,)))
        self.assertEqual(session.calls[0], category)
        self.assertEqual(posts[0].category_url, category)
        self.assertFalse(explorer.complete)

    def test_tistory_inline_entries_are_not_refetched_and_inherit_category(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        page = "<h2><a href='/1'>Test 1</a></h2><div class='entry'><div class='contents_style'>one</div></div><h2><a href='/2'>Test 2</a></h2><div class='entry'><div class='contents_style'>two</div></div>"
        session = Replay(lambda url: html(url, page) if urlsplit(url).path == "/category/Test" else None)
        posts = list(Explorer(session).explore(blog, ("Test",), Budget(), categories=(category,)))
        self.assertEqual([post.title for post in posts], ["Test 1", "Test 2"])
        self.assertEqual([post.category_url for post in posts], [category, category])
        self.assertFalse(any(urlsplit(url).path in {"/1", "/2"} for url in session.calls))

    def test_single_inline_entry_is_not_a_listing_post(self):
        blog = known_blog("https://author.tistory.com/")
        page = "<h2><a href='/1'>Test 1</a></h2><div class='entry'><div class='contents_style'>one</div></div>"
        session = Replay(lambda url: html(url, page) if "/search/" in url else None)
        posts = list(Explorer(session).explore(blog, ("Test",), Budget()))
        self.assertEqual([post.url for post in posts], [blog.url + "1"])

    def test_category_pagination_rejects_changed_category_and_calendar(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        def handler(url):
            if url == category:
                return html(url, "<div class='paging'><a rel='next' href='?page=2'>Next</a><a href='?category=Other&page=3'>3</a></div><a rel='next' href='/archive/202611'>Next</a>")
            if url == category + "?page=2":
                return html(url, "<p>No more posts</p>")
            return None
        session = Replay(handler)
        list(Explorer(session).explore(blog, ("Test",), Budget(), categories=(category,)))
        self.assertIn(category + "?page=2", session.calls)
        self.assertFalse(any("category=Other" in url or "/archive/" in url for url in session.calls))

    def test_repeated_listing_content_is_incomplete(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        def handler(url):
            if "/category/" in url:
                page = int(parse_qs(urlsplit(url).query).get("page", [1])[0])
                return html(url, f"<a href='/1'>Test 1</a><a rel='next' href='?page={page + 1}'>Next</a>")
            if url.endswith("/1"):
                return html(url, "<article>body</article>")
            return None
        session = Replay(handler)
        explorer = Explorer(session)
        list(explorer.explore(blog, ("Test",), Budget(), categories=(category,)))
        self.assertFalse(explorer.complete)
        self.assertEqual(sum("/category/" in url for url in session.calls), 2)

    def test_repeated_empty_listing_is_incomplete(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        page = "<a rel='next' href='?page=2'>Next</a>"
        session = Replay(lambda url: html(url, page) if "/category/" in url else None)
        explorer = Explorer(session)
        list(explorer.explore(blog, (), Budget(), categories=(category,)))
        self.assertFalse(explorer.complete)

    def test_path_based_category_pagination_is_followed(self):
        blog = known_blog("https://author.tistory.com/")
        category = blog.url + "category/Test"
        def handler(url):
            if url == category:
                return html(url, "<a rel='next' href='/category/Test/page/2'>Next</a>")
            if url.endswith("/page/2"):
                return html(url, "<a href='/1'>Episode 1</a>")
            if url.endswith("/1"):
                return html(url, "<article>body</article>")
            return None
        session = Replay(handler)
        posts = list(Explorer(session).explore(blog, (), Budget(), categories=(category,)))
        self.assertIn(category + "/page/2", session.calls)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].category_url, category)

    def test_rss_summary_is_upgraded_with_full_post(self):
        def handler(url):
            if url.endswith("/rss"):
                return xml(url, "<rss><channel><item><title>Test 1</title><link>https://example.com/older</link><description>summary</description></item></channel></rss>")
            if url.endswith("/older"):
                return html(url, "<h1>Test 1</h1><article>full body</article>")
            if url == self.blog.url:
                return html(url, "<main>Blog</main>")
            return None
        posts = list(Explorer(Replay(handler)).explore(self.blog, ("Test",), Budget()))
        self.assertEqual([post.summary for post in posts], [True, False])
        self.assertIn("full body", posts[1].html)

    def test_atom_content_and_labels_preserved_without_refetch(self):
        def handler(url):
            if url.endswith("/rss"):
                return xml(url, "<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Test 1</title><link rel='alternate' href='/older'/><content type='html'>&lt;a href='/01.smi'&gt;01.smi&lt;/a&gt;</content><category term='Test'/></entry></feed>")
            return html(url, "<main>Blog</main>") if url == self.blog.url else None
        session = Replay(handler)
        posts = list(Explorer(session).explore(self.blog, ("Test",), Budget()))
        self.assertFalse(posts[0].summary)
        self.assertEqual(posts[0].labels, ("Test",))
        self.assertIn("01.smi", posts[0].html)
        self.assertNotIn("https://example.com/older", session.calls)

    def test_repeated_xml_pagination_reports_incomplete(self):
        page = "<feed xmlns='http://www.w3.org/2005/Atom'><link rel='next' href='/rss?page=2'/></feed>"
        session = Replay(lambda url: xml(url, page) if "/rss" in url else html(url, "<main>Blog</main>") if url == self.blog.url else None)
        explorer = Explorer(session)
        list(explorer.explore(self.blog, (), Budget()))
        self.assertFalse(explorer.complete)
        self.assertEqual(sum("/rss" in url for url in session.calls), 2)

    def test_utf16_xml_entity_declaration_is_rejected(self):
        payload = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE urlset [<!ENTITY x "bad">]><urlset>&x;</urlset>'.encode("utf-16")
        session = Replay(lambda url: Response(url, payload, "application/xml") if url.endswith("sitemap.xml") else html(url, "<main>Blog</main>") if url == self.blog.url else None)
        explorer = Explorer(session)
        list(explorer.explore(self.blog, (), Budget()))
        self.assertFalse(explorer.complete)

    def test_rss_outside_scope_and_unsafe_xml_are_incomplete(self):
        for payload in (
            "<rss><channel><item><title>Test</title><link>https://other.com/post</link></item></channel></rss>",
            '<!DOCTYPE rss [<!ENTITY payload "bad">]><rss>&payload;</rss>',
            "<urlset>" + "<x>" * 33 + "</x>" * 33 + "</urlset>",
            "<urlset><url>",
        ):
            with self.subTest(payload=payload[:60]):
                session = Replay(lambda url: xml(url, payload) if url.endswith("sitemap.xml") else html(url, "<main>Blog</main>") if url == self.blog.url else None)
                explorer = Explorer(session)
                list(explorer.explore(self.blog, ("Test",), Budget()))
                self.assertFalse(explorer.complete)
                self.assertFalse(any("other.com" in url for url in session.calls))

    def test_depth_limit_reports_incomplete(self):
        def handler(url):
            path = urlsplit(url).path
            if path == "/sitemap.xml":
                return xml(url, "<sitemapindex><sitemap><loc>https://example.com/map1</loc></sitemap></sitemapindex>")
            if path.startswith("/map"):
                number = int(path[4:])
                return xml(url, f"<sitemapindex><sitemap><loc>https://example.com/map{number + 1}</loc></sitemap></sitemapindex>")
            return html(url, "<main>Blog</main>") if path == "/" else None
        session = Replay(handler)
        explorer = Explorer(session)
        list(explorer.explore(self.blog, (), Budget()))
        self.assertFalse(explorer.complete)
        self.assertFalse(any(url.endswith("map4") for url in session.calls))

    def test_crawl_page_limit_reports_incomplete(self):
        locations = "".join(f"<url><loc>https://example.com/{number}</loc></url>" for number in range(1, 105))
        def handler(url):
            if url.endswith("sitemap.xml"):
                return xml(url, f"<urlset>{locations}</urlset>")
            if url.endswith("/rss"):
                return None
            return html(url, "<article>body</article>")
        session = Replay(handler)
        explorer = Explorer(session)
        list(explorer.explore(self.blog, (), Budget()))
        self.assertFalse(explorer.complete)
        self.assertLessEqual(len(session.calls), 100)

    def test_naver_exploration_follows_wrapper_without_unrelated_home(self):
        blog = known_blog("https://blog.naver.com/author")
        def handler(url):
            if "PostSearchList" in url:
                return html(url, "<a href='/author/1'>Test 1</a><a href='/author/'>Blog home</a>")
            if urlsplit(url).path == "/author/1":
                return html(url, "<iframe id='mainFrame' src='/PostView.naver?blogId=author&logNo=1'></iframe>")
            return html(url, "<h1>Test 1</h1><div class='se-main-container'>body</div>")
        session = Replay(handler)
        posts = list(Explorer(session).explore(blog, ("Test",), Budget()))
        self.assertEqual(len(posts), 1)
        self.assertIn("PostView.naver", posts[0].url)
        self.assertEqual(len(session.calls), 3)
        self.assertFalse(any("currentPage=2" in url for url in session.calls))

    def test_cancel_is_not_hidden_as_source_failure(self):
        budget = Budget()
        budget.cancel.set()
        with self.assertRaises(Cancelled):
            list(Explorer(Replay(lambda url: html(url, "<main>Blog</main>"))).explore(self.blog, ("Test",), budget))

    def test_full_exhaustion_without_enumeration_remains_incomplete(self):
        session = Replay(lambda url: html(url, "<main>Blog</main>") if url == self.blog.url else None)
        explorer = Explorer(session)
        list(explorer.explore(self.blog, ("Test",), Budget()))
        self.assertFalse(explorer.complete)
        list(explorer.explore(self.blog, ("Test",), Budget(), exhaustive=False))
        self.assertTrue(explorer.complete)


if __name__ == "__main__":
    unittest.main()
