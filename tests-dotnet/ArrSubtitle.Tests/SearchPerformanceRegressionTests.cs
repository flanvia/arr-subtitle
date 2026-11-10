using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SearchPerformanceRegressionTests
{
    [Theory]
    [InlineData("모브사이코 100")]
    [InlineData("86 -에이티식스-")]
    public async Task NumbersInAnimeTitlesDoNotRestrictEpisodes(string title)
    {
        var handler = new ReplayHandler(request =>
        {
            if (request.RequestUri!.Host == "api.anissia.net") return Metadata(request.RequestUri, title);
            return ReplayHandler.Text("{\"feed\":{\"openSearch$totalResults\":{\"$t\":\"1\"},\"entry\":[{\"title\":{\"$t\":\"" + title + " 1화\"},\"content\":{\"$t\":\"<a href='https://files.example.com/01.smi'>01.smi</a>\"},\"link\":[{\"rel\":\"alternate\",\"href\":\"https://global.blogspot.com/1\"}]}],\"link\":[]}}");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Blogs = [new("https://global.blogspot.com/", BlogPlatform.Blogspot, "global.blogspot.com")] }).SearchAsync(title, default);
        Assert.True(report.Success);
        Assert.Equal("1", Assert.Single(report.Anime.Single().Subtitles).Episode);
    }

    [Fact]
    public async Task CaptionCannotOverrideExplicitAnimeSeason()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"1","website":"https://global.blogspot.com/1","name":"a","updDt":"2026"}]}""");
            if (uri.Host == "api.anissia.net") return Metadata(uri, "Test 1기");
            if (uri.AbsolutePath == "/1") return ReplayHandler.Text("<meta property='og:title' content='Test 2기 1화'><div class='post-body'><a href='https://files.example.com/Test.S02E01.smi'>Test.S02E01.smi</a></div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new()).SearchAsync("Test", default);
        Assert.Empty(report.Anime.Single().Subtitles);
        Assert.NotEqual(2, report.Anime.Single().Anime.Season);
    }

    [Fact]
    public async Task RssSummaryCannotHideAdditionalFullPostEpisodes()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.Host == "api.anissia.net") return Metadata(uri, "Test");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(Samples.Zip(("Test 01.smi", Samples.Utf16), ("Test 02.smi", Samples.Utf16), ("Test 03.smi", Samples.Utf16)));
            return uri.AbsolutePath switch
            {
                "/" => ReplayHandler.Text("<main>Blog</main>", "text/html"),
                "/rss" => ReplayHandler.Text("<rss><channel><item><title>Test 자막</title><link>https://example.com/batch</link><description>&lt;a href='https://files.example.com/01.smi'&gt;01.smi&lt;/a&gt;</description></item></channel></rss>", "application/xml"),
                "/sitemap.xml" => ReplayHandler.Text("<urlset><url><loc>https://example.com/batch</loc></url></urlset>", "application/xml"),
                "/batch" => ReplayHandler.Text("<meta property='og:title' content='Test 1-3화'><article><a href='https://files.example.com/Test.1-3.zip'>Test 1-3.zip</a></article>", "text/html"),
                _ => new(System.Net.HttpStatusCode.NotFound)
            };
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Blogs = [new("https://example.com/", BlogPlatform.Generic, "example.com")] }).SearchAsync("Test", default);
        Assert.True(report.Success);
        Assert.Equal(new[] { "1", "2", "3" }, report.Anime.Single().Subtitles.Select(s => s.Episode));
    }

    [Fact]
    public async Task NestedEntryIsStillOnePost()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath == "/" ? ReplayHandler.Text("<article><h1>Test 1화</h1><div class='entry'><div class='contents_style'><a href='/01.smi'>01.smi</a></div></div></article>", "text/html") : new(System.Net.HttpStatusCode.NotFound));
        using var session = new RequestSession(handler);
        var explorer = new BlogExplorer(session);
        var found = new List<PostCandidate>();
        await foreach (var post in explorer.ExploreAsync(new("https://example.com/", BlogPlatform.Generic, "example.com"), new(1, "Test", "Test", ["Test"], "fixture"), default)) found.Add(post);
        Assert.Single(found);
        Assert.Equal("Test 1화", found[0].Title);
    }

    [Fact]
    public async Task KeepsPlusEncodedSearchMeaning()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath == "/" ? ReplayHandler.Text("<main><a href='/search?SearchText=Test+One'>Test One</a></main>", "text/html") : new(System.Net.HttpStatusCode.NotFound));
        using var session = new RequestSession(handler);
        await foreach (var _ in new BlogExplorer(session).ExploreAsync(new("https://example.com/", BlogPlatform.Generic, "example.com"), new(1, "Test One", "Test One", ["Test One"], "fixture"), default)) { }
        var url = Assert.Single(handler.Calls, c => c.Contains("SearchText=", StringComparison.Ordinal));
        Assert.DoesNotContain("%2B", url, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("Test+One", url);
    }

    private static System.Net.Http.HttpResponseMessage Metadata(Uri uri, string title)
    {
        if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text($"{{\"code\":\"ok\",\"data\":{{\"content\":[{{\"animeNo\":1,\"subject\":\"{title}\"}}],\"last\":true}}}}");
        if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
        return ReplayHandler.Text($"{{\"code\":\"ok\",\"data\":{{\"subject\":\"{title}\",\"originalSubject\":\"{title}\"}}}}");
    }
}
