using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
using System.Text.RegularExpressions;
using AngleSharp.Dom;
using AngleSharp.Html.Parser;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Blogs;

public sealed class BlogExplorer(RequestSession session)
{
    private readonly HtmlParser parser = new();
    public bool Complete { get; private set; }

    public async Task<PostCandidate> ReadPostAsync(string url, CancellationToken ct, BlogDescriptor? registeredBlog = null)
    {
        var uri = new Uri(url);
        var blog = registeredBlog ?? BlogDetector.Known(uri) ?? new(uri.GetLeftPart(UriPartial.Authority) + "/", BlogPlatform.Generic, uri.IdnHost);
        var response = await session.GetAsync(url, ct, scope: u => BlogScope.Contains(blog, u));
        var doc = parser.ParseDocument(response.Text);
        var frame = doc.QuerySelector("iframe#mainFrame")?.GetAttribute("src");
        if (frame is not null && blog.Platform == BlogPlatform.Naver)
        {
            response = await session.GetAsync(new Uri(response.Url, frame).AbsoluteUri, ct, scope: u => BlogScope.Contains(blog, u));
            doc = parser.ParseDocument(response.Text);
        }
        var title = doc.QuerySelector("meta[property='og:title']")?.GetAttribute("content") ?? doc.QuerySelector("h1.post-title, h2.post-title, h3.post-title, .se-title-text, .pcol1 .htitle, .title_post, .title_view, .article .title, h3.tit_post")?.TextContent.Trim() ?? doc.Title;
        var content = doc.QuerySelector(".se-main-container") ?? doc.QuerySelector("#postViewArea") ?? doc.QuerySelector(".post-body, .tt_article_useless_p_margin, .contents_style, .entry-content, article, main");
        if (content is null) throw new CollectorException(ResultStatus.UnsupportedSource, "Cannot isolate blog post body");
        var labels = doc.QuerySelectorAll("a[rel='tag'], a[href*='/search/label/']").Select(e => e.TextContent.Trim()).Distinct().ToArray();
        var category = ReadCategory(doc, response.Url, blog);
        return new(response.Url.AbsoluteUri, title ?? "", content.InnerHtml, labels, CategoryUrl: category);
    }

    public async IAsyncEnumerable<PostCandidate> ExploreAsync(BlogDescriptor blog, ResolvedAnime anime, [EnumeratorCancellation] CancellationToken ct, bool exhaustive = false, string? label = null, string[]? searchTitles = null, string[]? categoryUrls = null, bool categoryOnly = false)
    {
        Complete = false;
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(TimeSpan.FromSeconds(120));
        if (blog.Platform == BlogPlatform.Blogspot)
        {
            var shortTitle = anime.Titles.Where(t => t.Any(c => c is >= '가' and <= '힣')).OrderBy(t => t.Length).FirstOrDefault() ?? anime.Title;
            var query = Uri.EscapeDataString(shortTitle);
            var feedPath = label is null ? "feeds/posts/default" : "feeds/posts/default/-/" + Uri.EscapeDataString(label);
            var next = new Uri(new Uri(blog.Url), $"{feedPath}?alt=json&max-results=150" + (exhaustive ? "" : $"&q={query}")).AbsoluteUri;
            var visited = new HashSet<string>();
            var seenPosts = new HashSet<string>();
            int? expected = null;
            for (var page = 0; page < 100; page++)
            {
                if (!visited.Add(next)) throw new CollectorException(ResultStatus.SearchIncomplete, "Repeated feed page");
                var response = await session.GetAsync(next, budget.Token, scope: u => BlogScope.Contains(blog, u));
                using var json = JsonDocument.Parse(response.Bytes, new() { MaxDepth = 32 });
                var feed = json.RootElement.GetProperty("feed");
                if (feed.TryGetProperty("openSearch$totalResults", out var total))
                {
                    if (!int.TryParse(total.GetProperty("$t").GetString(), out var count) || count < 0) throw new InvalidDataException("Invalid Blogger total count");
                    expected ??= count;
                    if (expected != count) throw new CollectorException(ResultStatus.SearchIncomplete, "Blog changed during pagination");
                }
                if (feed.TryGetProperty("entry", out var entries))
                {
                    foreach (var entry in entries.EnumerateArray())
                    {
                        var title = entry.GetProperty("title").GetProperty("$t").GetString() ?? "";
                        var link = entry.GetProperty("link").EnumerateArray().First(l => l.GetProperty("rel").GetString() == "alternate").GetProperty("href").GetString()!;
                        if (!BlogScope.Contains(blog, new Uri(link))) throw new HttpRequestException("Feed post outside blog scope");
                        if (!seenPosts.Add(link)) continue;
                        var html = entry.TryGetProperty("content", out var content) ? content.GetProperty("$t").GetString() ?? "" : "";
                        var labels = entry.TryGetProperty("category", out var categories) ? categories.EnumerateArray().Select(c => c.GetProperty("term").GetString() ?? "").ToArray() : [];
                        yield return new(link, title, html, labels);
                    }
                }
                var nextLink = feed.GetProperty("link").EnumerateArray().FirstOrDefault(l => l.GetProperty("rel").GetString() == "next");
                if (nextLink.ValueKind == JsonValueKind.Undefined)
                {
                    if (expected.HasValue && seenPosts.Count != expected.Value) throw new CollectorException(ResultStatus.SearchIncomplete, "Blogger feed omitted posts");
                    Complete = !exhaustive || expected.HasValue;
                    yield break;
                }
                var raw = nextLink.GetProperty("href").GetString()!;
                var uri = new Uri(raw);
                if (!BlogScope.Contains(blog, uri))
                {
                    if (uri.Host == "www.blogger.com") uri = new Uri(new Uri(blog.Url), feedPath + uri.Query);
                    else throw new HttpRequestException("Feed pagination outside blog scope");
                }
                next = uri.AbsoluteUri;
            }
            throw new CollectorException(ResultStatus.SearchIncomplete, "Blog page limit reached");
        }

        var queue = new PriorityQueue<(string Url, int Depth), (int Priority, long Order)>();
        var scheduled = new HashSet<string>(StringComparer.Ordinal);
        long sequence = 0;
        var searches = searchTitles ?? [anime.Title];
        foreach (var category in categoryUrls ?? [])
            if (Uri.TryCreate(category, UriKind.Absolute, out var categoryUri) && BlogScope.Contains(blog, categoryUri)) Schedule(category, 0, -1);
        if (categoryOnly && categoryUrls is { Length: > 0 }) { }
        else if (blog.Platform == BlogPlatform.Naver)
        {
            foreach (var title in searches) Schedule($"https://blog.naver.com/PostSearchList.naver?blogId={Uri.EscapeDataString(blog.Id)}&SearchText={Uri.EscapeDataString(title)}&pageNo=1", 0, 0);
            if (!exhaustive) Schedule($"https://blog.naver.com/PostList.naver?blogId={Uri.EscapeDataString(blog.Id)}&currentPage=1", 0, 3);
        }
        else
        {
            if (blog.Platform == BlogPlatform.Tistory)
                foreach (var title in searches) Schedule(new Uri(new Uri(blog.Url), "search/" + Uri.EscapeDataString(title)).AbsoluteUri, 0, 0);
            Schedule(blog.Url, 0, 3);
            Schedule(new Uri(new Uri(blog.Url), "rss").AbsoluteUri, 0, 1);
            Schedule(new Uri(new Uri(blog.Url), "sitemap.xml").AbsoluteUri, 0, 4);
        }
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var finalPages = new HashSet<string>(StringComparer.Ordinal);
        var pageFingerprints = new HashSet<string>(StringComparer.Ordinal);
        var emittedPosts = new Dictionary<string, bool>(StringComparer.Ordinal);
        var hadError = false; var truncated = false; var sawEnumeration = false;
        while (queue.TryDequeue(out var current, out _))
        {
            if (!seen.Add(current.Url)) continue;
            if (seen.Count > 100) throw new CollectorException(ResultStatus.SearchIncomplete, "Blog page limit reached");
            FetchResult response;
            try { response = await session.GetAsync(current.Url, budget.Token, scope: u => BlogScope.Contains(blog, u)); }
            catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound)
            {
                if (current.Depth > 0) hadError = true;
                continue;
            }
            catch (Exception ex) when (ex is not OperationCanceledException) { hadError = true; continue; }
            if (!finalPages.Add(CanonicalPage(response.Url.AbsoluteUri))) { hadError = true; continue; }
            var xml = response.Text.TrimStart().StartsWith("<?xml", StringComparison.Ordinal) || response.ContentType?.Contains("xml", StringComparison.OrdinalIgnoreCase) == true;
            if (xml)
            {
                XDocument document;
                try
                {
                    using var reader = XmlReader.Create(new StringReader(response.Text), new() { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 4 * 1024 * 1024 });
                    document = XDocument.Load(reader);
                }
                catch (XmlException) { hadError = true; continue; }
                foreach (var entry in document.Descendants().Where(e => e.Name.LocalName is "item" or "entry"))
                {
                    var title = entry.Elements().FirstOrDefault(e => e.Name.LocalName == "title")?.Value ?? "";
                    var linkNode = entry.Elements().FirstOrDefault(e => e.Name.LocalName == "link" && (e.Attribute("rel") is null || e.Attribute("rel")?.Value == "alternate"));
                    var raw = linkNode?.Attribute("href")?.Value ?? linkNode?.Value;
                    if (!Uri.TryCreate(response.Url, raw, out var uri) || !BlogScope.Contains(blog, uri)) continue;
                    var content = entry.Elements().FirstOrDefault(e => e.Name.LocalName is "encoded" or "content");
                    var summary = content is null;
                    var html = content?.Value ?? entry.Elements().FirstOrDefault(e => e.Name.LocalName == "description")?.Value ?? "";
                    var labels = entry.Elements().Where(e => e.Name.LocalName == "category").Select(e => e.Attribute("term")?.Value ?? e.Value).ToArray();
                    var postKey = CanonicalPage(uri.AbsoluteUri);
                    if (ShouldEmit(postKey, summary)) yield return new(postKey, title, html, labels, summary);
                }
                foreach (var location in document.Descendants().Where(e => e.Name.LocalName == "loc"))
                    Enqueue(location.Value, current.Depth + 1, 4);
                sawEnumeration |= document.Root?.Name.LocalName == "urlset";
                continue;
            }
            var doc = parser.ParseDocument(response.Text);
            if (categoryOnly && response.Url.AbsolutePath.Contains("/category/", StringComparison.Ordinal)) sawEnumeration = true;
            var listingPage = response.Url.AbsolutePath.Contains("/category/", StringComparison.Ordinal) || response.Url.AbsolutePath.Contains("/search/", StringComparison.Ordinal) || response.Url.AbsolutePath.Contains("PostList.naver", StringComparison.Ordinal);
            if (listingPage)
            {
                var postUrls = doc.QuerySelectorAll("a[href]").Select(a => Uri.TryCreate(response.Url, a.GetAttribute("href"), out var link) && IsPostLink(blog, link) ? CanonicalPage(link.AbsoluteUri) : null).Where(u => u is not null).Distinct().Order(StringComparer.Ordinal).ToArray();
                var fingerprint = string.Join('\n', postUrls);
                if (postUrls.Length > 0 && !pageFingerprints.Add(fingerprint)) { hadError = true; continue; }
            }
            var embedded = doc.QuerySelectorAll(".entry, article").Where(e => !e.Ancestors<IElement>().Any(a => a.Matches(".entry, article"))).ToArray();
            if (embedded.Length > 1)
            {
                foreach (var entry in embedded)
                {
                    var entryBody = entry.QuerySelector(".tt_article_useless_p_margin, .contents_style, .post-body, .entry-content");
                    if (entryBody is null) continue;
                    var header = entry.QuerySelector("h1, h2, h3, .titleWrap");
                    for (var sibling = entry.PreviousElementSibling; header is null && sibling is not null; sibling = sibling.PreviousElementSibling)
                    {
                        if (sibling.Matches("h1, h2, h3")) header = sibling;
                        else header = sibling.QuerySelector("h1, h2, h3");
                        if (sibling.ClassList.Contains("entry")) break;
                    }
                    var anchor = header?.QuerySelector("a[href]") ?? entry.QuerySelector("a[href][rel='bookmark']");
                    var rawPost = anchor?.GetAttribute("href");
                    if (rawPost is null) continue;
                    if (!Uri.TryCreate(response.Url, rawPost, out var postUri) || !BlogScope.Contains(blog, postUri)) continue;
                    var postKey = CanonicalPage(postUri.AbsoluteUri);
                    scheduled.Add(postKey);
                    var embeddedCategory = ReadCategory(doc, response.Url, blog);
                    if (categoryOnly && categoryUrls is not null && categoryUrls.Any(c => new Uri(c).AbsolutePath == response.Url.AbsolutePath)) embeddedCategory = response.Url.GetLeftPart(UriPartial.Path);
                    if (ShouldEmit(postKey, false)) yield return new(postKey, header?.TextContent.Trim() ?? "", entryBody.InnerHtml, entry.QuerySelectorAll("a[rel='tag'], .tag_label a").Select(e => e.TextContent.Trim()).ToArray(), CategoryUrl: embeddedCategory);
                }
            }
            var body = doc.QuerySelector(".se-main-container") ?? doc.QuerySelector("#postViewArea") ?? doc.QuerySelector(".post-body, .tt_article_useless_p_margin, .contents_style, .entry-content, article");
            if (body is not null && embedded.Length <= 1)
            {
                var title = doc.QuerySelector("meta[property='og:title']")?.GetAttribute("content") ?? doc.QuerySelector("h1.post-title, h2.post-title, .se-title-text, .title_post, .title_view, .article .title, h3.tit_post, article h1, article h2")?.TextContent.Trim() ?? doc.Title;
                var postKey = CanonicalPage(response.Url.AbsoluteUri);
                if (ShouldEmit(postKey, false)) yield return new(postKey, title ?? "", body.InnerHtml, [], CategoryUrl: ReadCategory(doc, response.Url, blog));
            }
            var links = doc.QuerySelectorAll("a[href], link[rel='next'], link[type='application/rss+xml'], link[type='application/atom+xml'], iframe#mainFrame");
            foreach (var element in links)
            {
                var raw = element.GetAttribute("href") ?? element.GetAttribute("src");
                var text = element.TextContent.Trim();
                if (!Uri.TryCreate(response.Url, raw, out var uri) || !BlogScope.Contains(blog, uri)) continue;
                var section = uri.AbsolutePath.Contains("/category/", StringComparison.Ordinal) || uri.AbsolutePath.Contains("/tag/", StringComparison.Ordinal);
                var titleMatch = TitleMatcher.Mentions(text, anime.Titles);
                var relatedSection = section && anime.Titles.Any(t => Uri.UnescapeDataString(uri.AbsolutePath).Contains(t, StringComparison.OrdinalIgnoreCase));
                var calendar = uri.AbsolutePath.Contains("/archive/", StringComparison.Ordinal);
                var pagingElement = element.Closest(".paging, .pagination, .pagination-next, .pagination-prev, .paging-view-more") is not null;
                var pagination = !calendar && (element.GetAttribute("rel") == "next" || pagingElement && (element.ClassName?.Contains("next", StringComparison.OrdinalIgnoreCase) == true || text is "다음" or "이전" or "Next" or "Older Posts") || IsPageLink(uri, current.Url));
                var frame = element.LocalName == "iframe";
                var feed = element.LocalName == "link" && element.GetAttribute("type") is "application/rss+xml" or "application/atom+xml";
                var categoryMatch = current.Url.Contains("/category/", StringComparison.Ordinal) || current.Url.Contains("/tag/", StringComparison.Ordinal);
                var categoryPost = categoryMatch && IsPostLink(blog, uri) && element.Closest(".list_content, .list-item, .post-item, .list_entry, .area-common, .item_category, .list_post, .list-type-thumbnail, .list_content, .list-content") is not null;
                var permittedCategory = !categoryOnly || (categoryUrls ?? []).Any(c => new Uri(c).AbsolutePath == uri.AbsolutePath);
                if (categoryOnly && (relatedSection || pagination) && !permittedCategory) continue;
                if (titleMatch || relatedSection || pagination || frame || feed && !categoryOnly || categoryPost)
                    Enqueue(uri.AbsoluteUri, pagination ? current.Depth : current.Depth + 1, frame ? 0 : relatedSection ? 1 : pagination && categoryMatch ? 1 : titleMatch ? 2 : feed ? 2 : 3);
            }
            if (blog.Platform == BlogPlatform.Naver && current.Url.Contains("PostList.naver", StringComparison.Ordinal))
            {
                var posts = doc.QuerySelectorAll("a[href]").Count(e =>
                {
                    if (!Uri.TryCreate(response.Url, e.GetAttribute("href"), out var postUri) || !BlogScope.Contains(blog, postUri)) return false;
                    var values = BlogDetector.ParseQuery(postUri.Query);
                    if (values.TryGetValue("logNo", out var logNo)) return logNo.Length > 0 && logNo.All(char.IsAsciiDigit);
                    var segments = postUri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
                    return segments.Length == 2 && segments[0].Equals(blog.Id, StringComparison.OrdinalIgnoreCase) && segments[1].All(char.IsAsciiDigit);
                });
                if (posts > 0)
                {
                    var page = BlogDetector.ParseQuery(response.Url.Query).GetValueOrDefault("currentPage") ?? "1";
                    if (int.TryParse(page, out var number)) Enqueue($"https://blog.naver.com/PostList.naver?blogId={Uri.EscapeDataString(blog.Id)}&currentPage={number + 1}", current.Depth, 3);
                }
                else hadError = true;
            }
        }
        Complete = sawEnumeration && !hadError && !truncated;

        void Enqueue(string raw, int depth, int priority)
        {
            if (!Uri.TryCreate(raw, UriKind.Absolute, out var uri) || !BlogScope.Contains(blog, uri)) return;
            if (depth > 3) { if (!seen.Contains(raw)) truncated = true; return; }
            Schedule(raw, depth, priority);
        }

        void Schedule(string raw, int depth, int priority)
        {
            var key = CanonicalPage(raw);
            if (scheduled.Add(key)) queue.Enqueue((key, depth), (priority, sequence++));
        }

        bool ShouldEmit(string key, bool summary)
        {
            if (emittedPosts.TryGetValue(key, out var previousSummary) && (!previousSummary || summary)) return false;
            emittedPosts[key] = summary;
            return true;
        }
    }

    private static string CanonicalPage(string raw)
    {
        var uri = new Uri(raw);
        var numericPost = uri.AbsolutePath.Trim('/').All(char.IsAsciiDigit) && uri.AbsolutePath.Trim('/').Length > 0;
        var parts = uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries).Where(part =>
        {
            var key = Uri.UnescapeDataString(part.Split('=', 2)[0]);
            return !key.StartsWith("utm_", StringComparison.OrdinalIgnoreCase) && !(numericPost && key.Equals("category", StringComparison.OrdinalIgnoreCase));
        });
        return new UriBuilder(uri) { Fragment = "", Query = string.Join('&', parts) }.Uri.AbsoluteUri;
    }

    private static bool IsPageLink(Uri next, string current)
    {
        var currentUri = new Uri(current);
        var path = next.AbsolutePath;
        if (path != currentUri.AbsolutePath) return false;
        var query = BlogDetector.ParseQuery(next.Query);
        var previous = BlogDetector.ParseQuery(currentUri.Query);
        var pageKeys = new[] { "page", "pageNo", "currentPage" };
        if (!pageKeys.Any(k => query.TryGetValue(k, out var value) && int.TryParse(value, out _))) return false;
        var stable = query.Where(p => !pageKeys.Contains(p.Key, StringComparer.OrdinalIgnoreCase)).OrderBy(p => p.Key).ToArray();
        var oldStable = previous.Where(p => !pageKeys.Contains(p.Key, StringComparer.OrdinalIgnoreCase)).OrderBy(p => p.Key).ToArray();
        return stable.SequenceEqual(oldStable);
    }

    private static bool IsPostLink(BlogDescriptor blog, Uri uri)
    {
        if (!BlogScope.Contains(blog, uri)) return false;
        if (blog.Platform == BlogPlatform.Naver)
        {
            var values = BlogDetector.ParseQuery(uri.Query);
            if (values.TryGetValue("logNo", out var logNo)) return logNo.Length > 0 && logNo.All(char.IsAsciiDigit);
            var segments = uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries);
            return segments.Length == 2 && segments[1].Length > 0 && segments[1].All(char.IsAsciiDigit);
        }
        return uri.AbsolutePath.Trim('/').Length > 0 && uri.AbsolutePath.Trim('/').All(char.IsAsciiDigit);
    }

    private static string? ReadCategory(IDocument document, Uri page, BlogDescriptor blog)
    {
        if (blog.Platform == BlogPlatform.Tistory)
        {
            foreach (var script in document.QuerySelectorAll("script"))
            {
                var match = Regex.Match(script.TextContent, @"(?:window\.)?T\.entryInfo\s*=\s*(\{[^;]{1,4096}\})\s*;", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
                if (!match.Success) continue;
                try
                {
                    using var data = JsonDocument.Parse(match.Groups[1].Value);
                    if (!data.RootElement.TryGetProperty("categoryLabel", out var label) || label.ValueKind != JsonValueKind.String) continue;
                    var parts = label.GetString()!.Split('/');
                    if (parts.Any(p => p.Length == 0 || p is "." or "..")) continue;
                    return new Uri(new Uri(blog.Url), "category/" + string.Join('/', parts.Select(Uri.EscapeDataString))).AbsoluteUri;
                }
                catch (JsonException) { }
            }
            var categoryLink = document.QuerySelector("a.link_category, a.category, .category a, .category-name a, .category_name a")?.GetAttribute("href");
            if (Uri.TryCreate(page, categoryLink, out var uri) && BlogScope.Contains(blog, uri) && uri.AbsolutePath.Contains("/category/", StringComparison.Ordinal)) return uri.AbsoluteUri;
        }
        return null;
    }
}
