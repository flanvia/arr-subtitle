from __future__ import annotations

import hashlib
import heapq
import io
import ipaddress
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .identity import mentions
from .models import Blog, Budget, Cancelled, CollectorError, Post

_LIMIT = 4 * 1024 * 1024
_BODY = ".se-main-container, #postViewArea, .post-body, .tt_article_useless_p_margin, .contents_style, .entry-content"
_TITLE = "h1.post-title, h2.post-title, h3.post-title, .se-title-text, .pcol1 .htitle, .title_post, .title_view, .article .title, h3.tit_post, article h1, article h2, h1"
_PAGE_KEYS = {"page", "pageno", "currentpage"}


def _parts(url):
    try:
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError("Invalid blog URL")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443} or "\\" in url
                or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError("Invalid blog URL")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("Private blog URL")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Private blog URL")
        return parsed
    except (TypeError, UnicodeError, ValueError) as exc:
        raise CollectorError("UnsupportedSource", "Invalid public blog URL") from exc


def _query(url):
    return {key.lower(): value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)}


def _canonical(url):
    parsed = _parts(url)
    numeric = parsed.path.strip("/").isascii() and parsed.path.strip("/").isdigit()
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and not (numeric and key.lower() == "category")]
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", urlencode(query), ""))


def known_blog(url: str) -> Blog | None:
    parsed = _parts(url)
    host = parsed.hostname.lower()
    if host in {"blog.naver.com", "m.blog.naver.com"}:
        segments = parsed.path.strip("/").split("/")
        blog_id = _query(url).get("blogid", segments[0])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", blog_id) or blog_id.lower().endswith(".naver"):
            raise CollectorError("UnsupportedSource", "Cannot identify Naver blog ID")
        return Blog(f"https://blog.naver.com/{blog_id}", "naver", blog_id.lower())
    for suffix, platform in ((".blogspot.com", "blogspot"), (".tistory.com", "tistory")):
        if host.endswith(suffix) and host != suffix[1:]:
            return Blog(f"https://{host}/", platform, host)
    return None


def in_scope(blog: Blog, url: str) -> bool:
    try:
        parsed, root = _parts(url), _parts(blog.url)
        if blog.platform.lower() == "naver":
            if parsed.hostname.lower() not in {"blog.naver.com", "m.blog.naver.com"}:
                return False
            ids = [value.lower() for key, value in parse_qsl(parsed.query) if key.lower() == "blogid"]
            if ids:
                first = parsed.path.strip("/").split("/")[0].lower()
                return all(value == blog.id.lower() for value in ids) and (first == blog.id.lower() or first.endswith(".naver") or not first)
            return parsed.path.strip("/").split("/")[0].lower() == blog.id.lower()
        if parsed.hostname.lower() != root.hostname.lower() or (parsed.port or (443 if parsed.scheme == "https" else 80)) not in {80, 443}:
            return False
        path = posixpath.normpath(unquote(parsed.path))
        base = posixpath.normpath(unquote(root.path))
        return base == "/" or path == base or path.startswith(base.rstrip("/") + "/")
    except (CollectorError, ValueError, TypeError):
        return False


def _data(response):
    data = response.data
    if not isinstance(data, bytes) or len(data) > _LIMIT:
        raise CollectorError("SearchIncomplete", "Blog response exceeds size limit")
    return data


def _html(response):
    return BeautifulSoup(_data(response), "html.parser", from_encoding="utf-8")


def detect_blog(session, url: str, budget: Budget) -> Blog:
    known = known_blog(url)
    if known is not None:
        return known
    budget.check()
    response = session.get(url, budget, limit=_LIMIT)
    known = known_blog(response.url)
    if known is not None:
        return known
    parsed = _parts(response.url)
    root = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    doc = _html(response)
    generator = doc.select_one("meta[name='generator']")
    if (generator and "blogger" in generator.get("content", "").lower()) or doc.select_one("link[href*='/feeds/posts/default']"):
        return Blog(root, "blogspot", parsed.hostname.lower())
    if (generator and "tistory" in generator.get("content", "").lower()) or doc.select_one("script[src*='tistory.com'], link[href*='tistory.com']"):
        return Blog(root, "tistory", parsed.hostname.lower())
    if not doc.select_one("link[type='application/rss+xml'], link[type='application/atom+xml'], article, main"):
        raise CollectorError("UnsupportedSource", "No supported public blog structure found")
    return Blog(root, "generic", parsed.hostname.lower() + ":/")


def _body(doc):
    for selector in (".se-main-container", "#postViewArea", _BODY, "article", "main"):
        body = doc.select_one(selector)
        if body is not None:
            return body
    return None


def _title(doc):
    meta = doc.select_one("meta[property='og:title']")
    if meta and meta.get("content"):
        return meta["content"].strip()
    node = doc.select_one(_TITLE) or doc.find("title")
    return node.get_text(" ", strip=True) if node else ""


def _labels(doc):
    return tuple(dict.fromkeys(node.get_text(" ", strip=True) for node in doc.select("a[rel~='tag'], a[href*='/search/label/'], .tag_label a")))


def _category(doc, page, blog):
    if blog.platform.lower() != "tistory":
        return None
    for script in doc.select("script"):
        match = re.search(r"(?:window\.)?T\.entryInfo\s*=\s*(\{[^;]{1,4096}\})\s*;", script.get_text())
        if not match:
            continue
        try:
            label = json.loads(match[1]).get("categoryLabel")
            if not isinstance(label, str):
                continue
            parts = label.split("/")
            if any(not part or part in {".", ".."} for part in parts):
                continue
            return urljoin(blog.url, "category/" + "/".join(quote(part, safe="") for part in parts))
        except (ValueError, AttributeError):
            continue
    for anchor in doc.select("a.link_category, a.category, .category a, .category-name a, .category_name a"):
        url = urljoin(page, anchor.get("href", ""))
        if in_scope(blog, url) and "/category/" in urlsplit(url).path:
            return _canonical(url)
    return None


def _xml(data):
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I):
        raise CollectorError("SearchIncomplete", "XML declarations are not allowed")
    try:
        depth = 0
        parser = ET.iterparse(io.BytesIO(data), events=("start", "end"))
        for event, _ in parser:
            depth += 1 if event == "start" else -1
            if depth > 32:
                raise CollectorError("SearchIncomplete", "XML depth limit reached")
        return parser.root
    except ET.ParseError as exc:
        raise CollectorError("SearchIncomplete", "Invalid blog XML") from exc


def _local(node):
    return node.tag.rsplit("}", 1)[-1]


def _post_link(blog, url):
    if not in_scope(blog, url):
        return False
    parsed = urlsplit(url)
    if blog.platform.lower() == "naver":
        number = _query(url).get("logno", "")
        segments = parsed.path.strip("/").split("/")
        return bool(number and number.isascii() and number.isdigit()) or (len(segments) == 2 and segments[1].isascii() and segments[1].isdigit())
    path = parsed.path.strip("/")
    return bool(path and path.isascii() and path.isdigit())


def _page_link(next_url, current):
    nxt, old = urlsplit(next_url), urlsplit(current)
    if nxt.path != old.path:
        return False
    query, previous = _query(next_url), _query(current)
    if not any(key in _PAGE_KEYS and value.isascii() and value.isdigit() for key, value in query.items()):
        return False
    return {k: v for k, v in query.items() if k not in _PAGE_KEYS} == {k: v for k, v in previous.items() if k not in _PAGE_KEYS}


def _navigation_link(next_url, current):
    nxt, old = urlsplit(next_url), urlsplit(current)
    if "/archive/" in nxt.path:
        return False
    if nxt.path == old.path:
        return _page_link(next_url, current)
    if _query(next_url) != _query(current):
        return False
    base = lambda path: re.sub(r"/page/[0-9]+/?$", "", path).rstrip("/")
    return base(nxt.path) == base(old.path) and bool(re.search(r"/page/[0-9]+/?$", nxt.path))


class Explorer:
    def __init__(self, session):
        self.session = session
        self.complete = False

    def _get(self, url, budget, blog):
        budget.check()
        if not in_scope(blog, url):
            raise CollectorError("SearchIncomplete", "Blog URL outside registered scope")
        response = self.session.get(url, budget, limit=_LIMIT, scope=lambda value: in_scope(blog, value))
        if not in_scope(blog, response.url):
            raise CollectorError("SearchIncomplete", "Blog redirect outside registered scope")
        _data(response)
        return response

    def read(self, url: str, budget: Budget, blog: Blog | None = None) -> Post:
        if blog is None:
            parsed = _parts(url)
            blog = known_blog(url) or Blog(urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")), "generic", parsed.hostname)
        visited = set()
        for _ in range(4):
            key = _canonical(url)
            if key in visited:
                raise CollectorError("SearchIncomplete", "Repeated blog wrapper")
            visited.add(key)
            response = self._get(url, budget, blog)
            doc = _html(response)
            frame = doc.select_one("iframe#mainFrame") if blog.platform.lower() == "naver" else None
            if frame and frame.get("src"):
                url = urljoin(response.url, frame["src"])
                continue
            body = _body(doc)
            if body is None:
                raise CollectorError("UnsupportedSource", "Cannot isolate blog post body")
            return Post(_canonical(response.url), _title(doc), body.decode_contents(), _labels(doc), category_url=_category(doc, response.url, blog))
        raise CollectorError("SearchIncomplete", "Blog wrapper depth limit reached")

    def explore(self, blog: Blog, titles: tuple[str, ...], budget: Budget,
                categories=(), exhaustive: bool = True) -> Iterator[Post]:
        self.complete = False
        if blog.platform.lower() == "blogspot":
            yield from self._blogger(blog, titles, budget, exhaustive)
            return
        yield from self._crawl(blog, titles, budget, categories, exhaustive)

    def _blogger(self, blog, titles, budget, exhaustive):
        feed_path = "feeds/posts/default"
        query = {"alt": "json", "max-results": "150"}
        if not exhaustive and titles:
            query["q"] = titles[0]
        next_url = urljoin(blog.url, feed_path) + "?" + urlencode(query)
        visited, final_pages, fingerprints, seen = set(), set(), set(), set()
        expected = None
        missing_total = False
        for _ in range(100):
            key = _canonical(next_url)
            if key in visited:
                raise CollectorError("SearchIncomplete", "Repeated Blogger feed page")
            visited.add(key)
            response = self._get(next_url, budget, blog)
            final = _canonical(response.url)
            if final in final_pages:
                raise CollectorError("SearchIncomplete", "Repeated redirected Blogger feed page")
            final_pages.add(final)
            try:
                def reject_constant(value):
                    raise ValueError("Invalid JSON constant")

                def unique_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate JSON key")
                        result[key] = value
                    return result

                data = json.loads(_data(response), parse_constant=reject_constant, object_pairs_hook=unique_keys)
                stack = [(data, 0)]
                while stack:
                    value, depth = stack.pop()
                    if depth > 32:
                        raise ValueError("JSON depth limit reached")
                    if isinstance(value, dict):
                        stack.extend((item, depth + 1) for item in value.values())
                    elif isinstance(value, list):
                        stack.extend((item, depth + 1) for item in value)
                feed = data["feed"]
                total = feed.get("openSearch$totalResults")
                if total is None:
                    missing_total = True
                else:
                    raw = total["$t"]
                    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
                        raise ValueError("Invalid Blogger total count")
                    count = int(raw)
                    if expected is not None and count != expected:
                        raise ValueError("Blogger total changed during pagination")
                    expected = count
                entries, links = feed.get("entry", []), feed.get("link", [])
                if not isinstance(entries, list) or not isinstance(links, list):
                    raise ValueError("Invalid Blogger feed lists")
                parsed_posts = []
                for entry in entries:
                    title = entry["title"]["$t"]
                    raw_link = next(link["href"] for link in entry["link"] if link.get("rel") == "alternate")
                    link = urljoin(response.url, raw_link)
                    if not in_scope(blog, link):
                        raise ValueError("Blogger post outside registered scope")
                    full = entry.get("content")
                    html = (full or entry.get("summary", {"$t": ""}))["$t"]
                    labels = tuple(item["term"] for item in entry.get("category", []))
                    if not all(isinstance(item, str) for item in (title, html, *labels)):
                        raise ValueError("Invalid Blogger entry text")
                    parsed_posts.append(Post(_canonical(link), title, html, labels, full is None))
                fingerprint = tuple(sorted(post.url for post in parsed_posts))
                next_links = [link["href"] for link in links if link.get("rel") == "next"]
                if len(next_links) > 1 or (fingerprint and fingerprint in fingerprints):
                    raise ValueError("Repeated Blogger feed content")
                fingerprints.add(fingerprint)
                for post in parsed_posts:
                    if post.url not in seen:
                        seen.add(post.url)
                        yield post
                if expected is not None and len(seen) > expected:
                    raise ValueError("Blogger feed exceeds declared total")
                if not next_links:
                    if expected is not None and len(seen) != expected:
                        raise ValueError("Blogger feed omitted posts")
                    self.complete = not exhaustive or (expected is not None and not missing_total)
                    return
                next_url = urljoin(response.url, next_links[0])
                if not in_scope(blog, next_url):
                    parsed = _parts(next_url)
                    if parsed.hostname.lower() != "www.blogger.com":
                        raise ValueError("Blogger pagination outside registered scope")
                    next_url = urljoin(blog.url, feed_path) + "?" + parsed.query
            except (KeyError, TypeError, ValueError, AttributeError, StopIteration, RecursionError) as exc:
                raise CollectorError("SearchIncomplete", "Invalid or incomplete Blogger feed: " + str(exc)) from exc
        raise CollectorError("SearchIncomplete", "Blog page limit reached")

    def _crawl(self, blog, titles, budget, categories, exhaustive):
        queue, scheduled, final_pages, fingerprints = [], set(), set(), set()
        listing_content, xml_content = set(), set()
        emitted = {}
        sequence = 0
        failed = truncated = enumeration = False

        def schedule(url, depth, priority, category=None, optional=False):
            nonlocal sequence, truncated, failed
            if not in_scope(blog, url):
                failed = True
                return
            key = _canonical(url)
            if key in scheduled:
                return
            if depth > 3:
                truncated = True
                return
            if len(scheduled) >= 100:
                truncated = True
                return
            scheduled.add(key)
            heapq.heappush(queue, (priority, sequence, key, depth, category, optional))
            sequence += 1

        def emit(post):
            previous = emitted.get(post.url)
            if previous is not None and (not previous or post.summary):
                return False
            emitted[post.url] = post.summary
            return True

        for category in categories:
            schedule(category, 0, -1, _canonical(category))
        if blog.platform.lower() == "naver":
            for title in titles:
                schedule("https://blog.naver.com/PostSearchList.naver?" + urlencode({"blogId": blog.id, "SearchText": title, "pageNo": 1}), 0, 0)
            if not exhaustive:
                schedule("https://blog.naver.com/PostList.naver?" + urlencode({"blogId": blog.id, "currentPage": 1}), 0, 3)
        else:
            if blog.platform.lower() == "tistory":
                for title in titles:
                    schedule(urljoin(blog.url, "search/" + quote(title, safe="")), 0, 0)
            schedule(blog.url, 0, 3)
            schedule(urljoin(blog.url, "rss"), 0, 2, optional=True)
            schedule(urljoin(blog.url, "sitemap.xml"), 0, 4, optional=True)

        while queue:
            budget.check()
            _, _, url, depth, inherited, optional = heapq.heappop(queue)
            if emitted.get(url) is False:
                continue
            try:
                response = self._get(url, budget, blog)
                final = _canonical(response.url)
                if final in final_pages:
                    failed = True
                    continue
                final_pages.add(final)
                data = _data(response)
                content_type = (response.content_type or "").lower()
                xml = "xml" in content_type or data.lstrip().startswith((b"<?xml", b"<rss", b"<feed", b"<urlset", b"<sitemapindex"))
                if xml:
                    root = _xml(data)
                    name = _local(root)
                    if name not in {"rss", "feed", "urlset", "sitemapindex"}:
                        raise CollectorError("SearchIncomplete", "Unsupported blog XML")
                    digest = hashlib.sha256(data).digest()
                    if digest in xml_content:
                        failed = True
                        continue
                    xml_content.add(digest)
                    enumeration |= name == "urlset"
                    for entry in root.iter():
                        if _local(entry) not in {"entry", "item"}:
                            continue
                        children = list(entry)
                        title = next((node.text or "" for node in children if _local(node) == "title"), "")
                        link = next((node.get("href") or node.text or "" for node in children if _local(node) == "link" and node.get("rel", "alternate") == "alternate"), "")
                        post_url = urljoin(response.url, link)
                        if not link or not in_scope(blog, post_url):
                            failed = True
                            continue
                        content = next((node for node in children if _local(node) in {"encoded", "content"}), None)
                        summary = content is None
                        if content is None:
                            content = next((node for node in children if _local(node) in {"description", "summary"}), None)
                        html = "" if content is None else (content.text or "") + "".join(ET.tostring(child, encoding="unicode") for child in content)
                        labels = tuple(node.get("term") or node.text or "" for node in children if _local(node) == "category")
                        post = Post(_canonical(post_url), title, html, labels, summary, inherited)
                        if emit(post):
                            yield post
                        if summary:
                            schedule(post.url, depth + 1, 1, inherited)
                    for node in root.iter():
                        if _local(node) == "loc" and node.text:
                            schedule(urljoin(response.url, node.text.strip()), depth + 1, 4, inherited)
                        elif _local(node) == "link" and node.get("rel") == "next" and node.get("href"):
                            next_url = urljoin(response.url, node.get("href"))
                            if in_scope(blog, next_url) and _canonical(next_url) in final_pages:
                                failed = True
                            schedule(next_url, depth, 2, inherited)
                    continue
                doc = _html(response)
                path = urlsplit(response.url).path
                section = "/category/" in path or "/tag/" in path
                listing = section or "/search/" in path or "PostList.naver" in path or "PostSearchList.naver" in path
                category = inherited or (_canonical(response.url).split("?", 1)[0] if section else _category(doc, response.url, blog))
                enumeration |= section
                entries = [node for node in doc.select(".entry, article") if not node.find_parent(class_="entry") and not node.find_parent("article")]
                embedded = set()
                for entry in entries:
                    body = entry.select_one(_BODY)
                    if body is None:
                        continue
                    header = entry.select_one("h1, h2, h3, .titleWrap")
                    if header is None:
                        for sibling in entry.previous_siblings:
                            if not getattr(sibling, "name", None):
                                continue
                            if sibling.name in {"h1", "h2", "h3"}:
                                header = sibling
                                break
                            if "entry" in sibling.get("class", []):
                                break
                            header = sibling.select_one("h1, h2, h3")
                            if header:
                                break
                    anchor = header.select_one("a[href]") if header else None
                    anchor = anchor or entry.select_one("a[rel~='bookmark'][href]")
                    if anchor is None:
                        continue
                    post_url = urljoin(response.url, anchor["href"])
                    if not in_scope(blog, post_url):
                        failed = True
                        continue
                    post = Post(_canonical(post_url), header.get_text(" ", strip=True) if header else "", body.decode_contents(), _labels(entry), category_url=category)
                    embedded.add(post.url)
                    if emit(post):
                        yield post
                if listing:
                    digest = (path, hashlib.sha256(data).digest())
                    if digest in listing_content:
                        failed = True
                        continue
                    listing_content.add(digest)
                    post_urls = tuple(sorted({_canonical(urljoin(response.url, anchor["href"])) for anchor in doc.select("a[href]") if _post_link(blog, urljoin(response.url, anchor["href"]))}))
                    fingerprint = (urlsplit(response.url).path, post_urls)
                    if post_urls and fingerprint in fingerprints:
                        failed = True
                        continue
                    if post_urls:
                        fingerprints.add(fingerprint)
                if not listing and not embedded and _canonical(response.url) != _canonical(blog.url):
                    body = _body(doc)
                    if body is not None:
                        post = Post(final, _title(doc), body.decode_contents(), _labels(doc), category_url=category)
                        if emit(post):
                            yield post
                    elif not doc.select_one("iframe#mainFrame"):
                        failed = True
                for element in doc.select("a[href], link[rel~='next'], link[type='application/rss+xml'], link[type='application/atom+xml'], iframe#mainFrame"):
                    raw = element.get("href") or element.get("src")
                    target = urljoin(response.url, raw or "")
                    if not in_scope(blog, target) or _canonical(target) in embedded:
                        continue
                    target_path = urlsplit(target).path
                    if re.search(r"\.(?:smi|srt|ass|ssa|zip|rar|7z|mkv|mp4|avi|jpg|jpeg|png|gif|webp|pdf)$", target_path, re.I):
                        continue
                    text = element.get_text(" ", strip=True)
                    rel = element.get("rel", [])
                    is_frame = element.name == "iframe" and blog.platform.lower() == "naver"
                    feed = element.name == "link" and element.get("type") in {"application/rss+xml", "application/atom+xml"}
                    paging_container = element.find_parent(class_=re.compile(r"^(?:paging|pagination|pagination-next|pagination-prev|paging-view-more)$"))
                    navigation = "next" in rel or bool(paging_container)
                    pagination = "/archive/" not in target_path and (_page_link(target, response.url) or (navigation and _navigation_link(target, response.url)))
                    if "/archive/" in target_path or (navigation and not pagination):
                        continue
                    related_section = any(part in target_path for part in ("/category/", "/tag/")) and (mentions(text, titles) or mentions(unquote(target_path), titles))
                    in_sidebar = element.find_parent(["aside", "nav"]) is not None or element.find_parent(class_=re.compile(r"sidebar|blogroll", re.I)) is not None
                    title_match = not in_sidebar and mentions(text, titles)
                    listed_post = listing and _post_link(blog, target) and not in_sidebar
                    if pagination and "next" in rel and _canonical(target) in final_pages:
                        failed = True
                    if pagination or is_frame or feed or related_section or title_match or listed_post:
                        schedule(target, depth if pagination else depth + 1, -1 if pagination and section else 0 if is_frame else 1, category if pagination or listed_post or title_match else None)
            except Cancelled:
                raise
            except (CollectorError, OSError, ValueError) as exc:
                status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status_code is None and isinstance(exc, CollectorError) and str(exc) == "HTTP 404 request failed":
                    status_code = 404
                if not (optional and status_code == 404):
                    failed = True
        self.complete = not failed and not truncated and (enumeration or not exhaustive)
