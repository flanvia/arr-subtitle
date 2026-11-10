using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
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
        if (frame is not null && blog?.Platform == BlogPlatform.Naver)
        {
            response = await session.GetAsync(new Uri(response.Url, frame).AbsoluteUri, ct, scope: u => BlogScope.Contains(blog, u));
            doc = parser.ParseDocument(response.Text);
        }
        var title = doc.QuerySelector("meta[property='og:title']")?.GetAttribute("content") ?? doc.QuerySelector("h1.post-title, h2.post-title, h3.post-title, .se-title-text, .pcol1 .htitle, .title_post, .title_view, .article .title, h3.tit_post")?.TextContent.Trim() ?? doc.Title;
        var content = doc.QuerySelector(".se-main-container") ?? doc.QuerySelector("#postViewArea") ?? doc.QuerySelector(".post-body, .tt_article_useless_p_margin, .contents_style, .entry-content, article, main");
        if (content is null) throw new CollectorException(ResultStatus.UnsupportedSource, "Cannot isolate blog post body");
        var labels = doc.QuerySelectorAll("a[rel='tag'], a[href*='/search/label/']").Select(e => e.TextContent.Trim()).Distinct().ToArray();
        return new(response.Url.AbsoluteUri, title ?? "", content.InnerHtml, labels);
    }

    public async IAsyncEnumerable<PostCandidate> ExploreAsync(BlogDescriptor blog, ResolvedAnime anime, [EnumeratorCancellation] CancellationToken ct, bool exhaustive = false, string? label = null)
    {
        Complete = false;
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(TimeSpan.FromSeconds(120));
        if (blog.Platform == BlogPlatform.Blogspot)
        {
            var shortTitle = anime.Titles.Where(t => t.Any(c => c is >= '가' and <= '힣')).OrderBy(t => t.Length).FirstOrDefault() ?? anime.Title;
            var query = Uri.EscapeDataString(shortTitle);
            var feedPath = label is null ? "feeds/posts/default" : "feeds/posts/default/-/" + Uri.EscapeDataString(label);
            var next = new Uri(new Uri(blog.Url), $"{feedPath}?alt=json&max-results=50" + (exhaustive ? "" : $"&q={query}")).AbsoluteUri;
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

        var queue = new Queue<(string Url, int Depth)>();
        if (blog.Platform == BlogPlatform.Naver)
        {
            queue.Enqueue(($"https://blog.naver.com/PostSearchList.naver?blogId={Uri.EscapeDataString(blog.Id)}&SearchText={Uri.EscapeDataString(anime.Title)}&pageNo=1", 0));
            queue.Enqueue(($"https://blog.naver.com/PostList.naver?blogId={Uri.EscapeDataString(blog.Id)}&currentPage=1", 0));
        }
        else
        {
            queue.Enqueue((blog.Url, 0));
            queue.Enqueue((new Uri(new Uri(blog.Url), "rss").AbsoluteUri, 0));
            if (blog.Platform == BlogPlatform.Tistory) queue.Enqueue((new Uri(new Uri(blog.Url), "search/" + Uri.EscapeDataString(anime.Title)).AbsoluteUri, 0));
            else queue.Enqueue((new Uri(new Uri(blog.Url), "sitemap.xml").AbsoluteUri, 0));
        }
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var hadError = false; var truncated = false; var sawEnumeration = false;
        while (queue.TryDequeue(out var current))
        {
            if (!seen.Add(current.Url)) continue;
            if (seen.Count > 100) throw new CollectorException(ResultStatus.SearchIncomplete, "Blog page limit reached");
            FetchResult response;
            try { response = await session.GetAsync(current.Url, budget.Token, scope: u => BlogScope.Contains(blog, u)); }
            catch (HttpRequestException ex) when (ex.StatusCode == System.Net.HttpStatusCode.NotFound) { continue; }
            catch (Exception ex) when (ex is not OperationCanceledException) { hadError = true; continue; }
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
                    var html = entry.Elements().FirstOrDefault(e => e.Name.LocalName is "encoded" or "content" or "description")?.Value ?? "";
                    var labels = entry.Elements().Where(e => e.Name.LocalName == "category").Select(e => e.Attribute("term")?.Value ?? e.Value).ToArray();
                    yield return new(uri.AbsoluteUri, title, html, labels);
                }
                foreach (var location in document.Descendants().Where(e => e.Name.LocalName == "loc"))
                    Enqueue(location.Value, current.Depth + 1);
                sawEnumeration |= document.Root?.Name.LocalName == "urlset";
                continue;
            }
            var doc = parser.ParseDocument(response.Text);
            var body = doc.QuerySelector(".se-main-container") ?? doc.QuerySelector("#postViewArea") ?? doc.QuerySelector(".post-body, .tt_article_useless_p_margin, .contents_style, .entry-content, article");
            if (body is not null)
            {
                var title = doc.QuerySelector("meta[property='og:title']")?.GetAttribute("content") ?? doc.QuerySelector("h1.post-title, h2.post-title, .se-title-text, .title_post, .title_view, .article .title, h3.tit_post")?.TextContent.Trim() ?? doc.Title;
                yield return new(response.Url.AbsoluteUri, title ?? "", body.InnerHtml, []);
            }
            var links = doc.QuerySelectorAll("a[href], link[rel='next'], link[type='application/rss+xml'], link[type='application/atom+xml'], iframe#mainFrame");
            foreach (var element in links)
            {
                var raw = element.GetAttribute("href") ?? element.GetAttribute("src");
                var text = element.TextContent.Trim();
                if (!Uri.TryCreate(response.Url, raw, out var uri) || !BlogScope.Contains(blog, uri)) continue;
                var section = uri.AbsolutePath.Contains("/category/", StringComparison.Ordinal) || uri.AbsolutePath.Contains("/tag/", StringComparison.Ordinal);
                var related = TitleMatcher.Mentions(text, anime.Titles) || section && anime.Titles.Any(t => Uri.UnescapeDataString(uri.AbsolutePath).Contains(t, StringComparison.OrdinalIgnoreCase)) || uri.AbsolutePath.Contains("/search/", StringComparison.Ordinal) || element.LocalName != "a" || element.GetAttribute("rel") == "next" || element.ClassName?.Contains("next", StringComparison.OrdinalIgnoreCase) == true || text is "다음" or "이전" or "Next" or "Older Posts" || uri.Query.Contains("page", StringComparison.OrdinalIgnoreCase) || uri.Query.Contains("logNo=", StringComparison.OrdinalIgnoreCase);
                if (related) Enqueue(uri.AbsoluteUri, current.Depth + 1);
            }
            if (blog.Platform == BlogPlatform.Naver && current.Url.Contains("PostList.naver", StringComparison.Ordinal))
            {
                var posts = doc.QuerySelectorAll("a[href*='logNo='], a[href]").Count(e => e.GetAttribute("href")?.Contains(blog.Id + "/", StringComparison.OrdinalIgnoreCase) == true || e.GetAttribute("href")?.Contains("logNo=", StringComparison.OrdinalIgnoreCase) == true);
                if (posts > 0)
                {
                    var page = BlogDetector.ParseQuery(response.Url.Query).GetValueOrDefault("currentPage") ?? "1";
                    if (int.TryParse(page, out var number)) Enqueue($"https://blog.naver.com/PostList.naver?blogId={Uri.EscapeDataString(blog.Id)}&currentPage={number + 1}", current.Depth);
                }
                else hadError = true;
            }
        }
        Complete = sawEnumeration && !hadError && !truncated;

        void Enqueue(string raw, int depth)
        {
            if (!Uri.TryCreate(raw, UriKind.Absolute, out var uri) || !BlogScope.Contains(blog, uri)) return;
            if (depth > 3) { if (!seen.Contains(raw)) truncated = true; return; }
            if (!seen.Contains(raw)) queue.Enqueue((raw, depth));
        }
    }
}
