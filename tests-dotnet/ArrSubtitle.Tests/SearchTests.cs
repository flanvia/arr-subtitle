using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SearchTests
{
    [Fact]
    public async Task ListsOlderEpisodesAcrossPagesWithoutDownloadingAttachments()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.StartsWith("/anime/list")) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"흉란영애 니아 리스톤"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"3","website":"https://author.blogspot.com/3","name":"Author","updDt":"2026"}]}""");
            if (uri.AbsolutePath.Contains("animeNo")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"흉란영애 니아 리스톤","originalSubject":"Test"}}""");
            if (uri.AbsolutePath == "/3") return ReplayHandler.Text("<meta property='og:title' content='흉란영애 니아 리스톤 3화 자막'><div class='post-body'><a href='https://files.example.com/03.smi'>03.smi</a></div>", "text/html");
            if (uri.Query.Contains("start-index")) return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"3"},"entry":[{"title":{"$t":"흉란영애 니아 리스톤 1화 자막"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://author.blogspot.com/1"}]}],"link":[]}}""");
            if (uri.AbsolutePath.Contains("feeds")) return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"3"},"entry":[{"title":{"$t":"흉란영애 니아 리스톤 3화 자막"},"content":{"$t":"<a href='https://files.example.com/03.smi'>03.smi</a>"},"link":[{"rel":"alternate","href":"https://author.blogspot.com/3"}]},{"title":{"$t":"흉란영애 니아 리스톤 2화 자막"},"content":{"$t":"<a href='https://files.example.com/02.smi'>02.smi</a>"},"link":[{"rel":"alternate","href":"https://author.blogspot.com/2"}]}],"link":[{"rel":"next","href":"https://author.blogspot.com/feeds/posts/default?alt=json&start-index=3"}]}}""");
            throw new Exception("Unexpected attachment request");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false }).SearchAsync("흉란영애", default);
        Assert.True(report.Success);
        Assert.Equal(new[] { "1", "2", "3" }, report.Anime.Single().Subtitles.Select(s => s.Episode));
        Assert.DoesNotContain(handler.Calls, c => c.Contains("files.example.com"));
    }

    [Fact]
    public async Task ReportsMissingMiddleEpisodesAndStillSearchesGlobalBlogs()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.StartsWith("/anime/list")) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            if (uri.AbsolutePath.Contains("animeNo")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test","originalSubject":"テスト"}}""");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"2"},"entry":[{"title":{"$t":"Test 1화"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://global.blogspot.com/1"}]},{"title":{"$t":"Test 3화"},"content":{"$t":"<a href='https://files.example.com/03.smi'>03.smi</a>"},"link":[{"rel":"alternate","href":"https://global.blogspot.com/3"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Blogs = [new("https://global.blogspot.com/", Core.Models.BlogPlatform.Blogspot, "global.blogspot.com")] }).SearchAsync("Test", default);
        Assert.False(report.Success);
        Assert.True(report.Complete);
        Assert.Equal(new[] { "2" }, report.Anime.Single().MissingEpisodes);
        Assert.Contains(handler.Calls, c => c.Contains("global.blogspot.com"));
    }

    [Fact]
    public async Task ReadsAllMatchingAnimePages()
    {
        var handler = new ReplayHandler(request =>
        {
            var path = request.RequestUri!.AbsolutePath;
            if (path.StartsWith("/anime/list/")) return ReplayHandler.Text(path.EndsWith("/0") ? """{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test One"}],"last":false}}""" : """{"code":"ok","data":{"content":[{"animeNo":2,"subject":"Test Two"}],"last":true}}""");
            if (path.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test","originalSubject":"テスト"}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new()).SearchAsync("Test", default);
        Assert.Equal(new[] { 1, 2 }, report.Anime.Select(a => a.Anime.AnimeId));
        Assert.False(report.Success);
    }
}
