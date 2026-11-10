using AngleSharp.Html.Parser;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Blogs;

public sealed class BlogDetector(RequestSession session)
{
    public static BlogDescriptor? Known(Uri uri)
    {
        DestinationPolicy.ValidateUrl(uri);
        var host = uri.IdnHost.ToLowerInvariant();
        if (host is "blog.naver.com" or "m.blog.naver.com")
        {
            var query = ParseQuery(uri.Query);
            var id = query.GetValueOrDefault("blogId") ?? uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault();
            if (id is null || !System.Text.RegularExpressions.Regex.IsMatch(id, @"^[A-Za-z0-9_-]{1,64}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) throw new InvalidDataException("Cannot identify Naver blog ID");
            return new($"https://blog.naver.com/{id}", BlogPlatform.Naver, id.ToLowerInvariant());
        }
        if (host.EndsWith(".blogspot.com", StringComparison.Ordinal)) return new($"https://{host}/", BlogPlatform.Blogspot, host);
        if (host.EndsWith(".tistory.com", StringComparison.Ordinal)) return new($"https://{host}/", BlogPlatform.Tistory, host);
        return null;
    }

    public async Task<BlogDescriptor> DetectAsync(Uri input, CancellationToken ct)
    {
        var known = Known(input);
        if (known is not null) return known;
        var response = await session.GetAsync(input.AbsoluteUri, ct);
        var finalKnown = Known(response.Url);
        if (finalKnown is not null) return finalKnown;
        var document = new HtmlParser().ParseDocument(response.Text);
        var generator = document.QuerySelector("meta[name='generator']")?.GetAttribute("content") ?? "";
        var root = response.Url.GetLeftPart(UriPartial.Authority) + "/";
        if (generator.Contains("blogger", StringComparison.OrdinalIgnoreCase) || document.QuerySelector("link[href*='/feeds/posts/default']") is not null) return new(root, BlogPlatform.Blogspot, response.Url.IdnHost);
        if (response.Text.Contains("tistory.com", StringComparison.OrdinalIgnoreCase) && response.Text.Contains("tistory", StringComparison.OrdinalIgnoreCase)) return new(root, BlogPlatform.Tistory, response.Url.IdnHost);
        if (document.QuerySelector("link[type='application/rss+xml'], link[type='application/atom+xml'], article, main") is null) throw new CollectorException(ResultStatus.UnsupportedSource, "No supported public blog structure found");
        return new(root, BlogPlatform.Generic, response.Url.IdnHost + ":" + new Uri(root).AbsolutePath);
    }

    public static Dictionary<string, string> ParseQuery(string query) => query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries).Select(p => p.Split('=', 2)).GroupBy(p => Uri.UnescapeDataString(p[0]), StringComparer.OrdinalIgnoreCase).ToDictionary(g => g.Key, g => Uri.UnescapeDataString(g.Last().ElementAtOrDefault(1) ?? ""), StringComparer.OrdinalIgnoreCase);
}

public static class BlogScope
{
    public static bool Contains(BlogDescriptor blog, Uri uri)
    {
        if (uri.Scheme is not ("http" or "https")) return false;
        if (blog.Platform == BlogPlatform.Naver)
        {
            if (uri.IdnHost is not ("blog.naver.com" or "m.blog.naver.com")) return false;
            var query = BlogDetector.ParseQuery(uri.Query);
            if (query.TryGetValue("blogId", out var id)) return id.Equals(blog.Id, StringComparison.OrdinalIgnoreCase);
            return uri.AbsolutePath.Split('/', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault()?.Equals(blog.Id, StringComparison.OrdinalIgnoreCase) == true;
        }
        var root = new Uri(blog.Url);
        return uri.IdnHost.Equals(root.IdnHost, StringComparison.OrdinalIgnoreCase) && uri.AbsolutePath.StartsWith(root.AbsolutePath, StringComparison.Ordinal);
    }
}
