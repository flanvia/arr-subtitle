using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SearchCategoryTests
{
    [Fact]
    public async Task DiscoversTistoryCategoryFromCaptionMetadata()
    {
        using var session = new RequestSession(new ReplayHandler(_ => ReplayHandler.Text("<meta property='og:title' content='수염 13'><script>window.T.entryInfo = {\"entryId\":3565,\"categoryId\":939102,\"categoryLabel\":\"21/HIGE\"};</script><div class='contents_style'>body</div>", "text/html")));
        var post = await new BlogExplorer(session).ReadPostAsync("https://example.tistory.com/3565", default);
        Assert.Equal("https://example.tistory.com/category/21/HIGE", post.CategoryUrl);
    }

    [Fact]
    public async Task CategoryScopeDoesNotTraverseUnrelatedHomeOrSitemap()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath switch
        {
            "/category/21/HIGE" => ReplayHandler.Text("<div class='list_content'><a href='/1'>엄마 1</a></div>", "text/html"),
            "/1" => ReplayHandler.Text("<meta property='og:title' content='엄마 1'><script>window.T.entryInfo = {\"categoryLabel\":\"21/HIGE\"};</script><div class='contents_style'><a href='https://example.com/01.smi'>01.smi</a></div>", "text/html"),
            _ => throw new InvalidOperationException("Unrelated blog request")
        });
        using var session = new RequestSession(handler);
        var posts = new List<PostCandidate>();
        var explorer = new BlogExplorer(session);
        await foreach (var post in explorer.ExploreAsync(new("https://example.tistory.com/", BlogPlatform.Tistory, "example.tistory.com"), new(1, "수염을 깎다", "Test", ["수염을 깎다"], "fixture"), default, exhaustive: true, categoryUrls: ["https://example.tistory.com/category/21/HIGE"], categoryOnly: true)) posts.Add(post);
        Assert.Single(posts);
        Assert.Equal("https://example.tistory.com/category/21/HIGE", posts[0].CategoryUrl);
        Assert.Equal(2, handler.Calls.Count);
    }

    [Fact]
    public async Task BatchPostInspectsZipInsteadOfListingOnlyCaptionEpisode()
    {
        var zip = Samples.Zip(("hige 01s.smi", Samples.Utf16), ("hige 01ns.smi", Samples.Utf16), ("hige 02s.smi", Samples.Utf16), ("hige 03ns.smi", Samples.Utf16));
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"수염을 깎다"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"3","website":"https://example.blogspot.com/batch","name":"Maker","updDt":"2026"}]}""");
            if (uri.AbsolutePath.Contains("animeNo", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"수염을 깎다","originalSubject":"Hige"}}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(zip);
            if (uri.AbsolutePath == "/batch") return ReplayHandler.Text("<meta property='og:title' content='수염을 깎다 TVA 전체 자막'><div class='post-body'><a href='https://files.example.com/hige.zip'>전체.zip</a></div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Mappings = [new("수염을 깎다", 1, 1, ["Hige"])] }).SearchAsync("수염", default);
        Assert.True(report.Success);
        Assert.Equal(new[] { "1", "2", "3" }, report.Anime.Single().Subtitles.Select(s => s.Episode));
        Assert.Single(handler.Calls, c => new Uri(c).Host == "files.example.com");
    }

    [Fact]
    public void AttachmentSizeLabelDoesNotReplaceRealFilenameWithGenericQueryName()
    {
        var candidates = Core.Subtitles.AttachmentExtractor.Extract(new("https://example.tistory.com/1", "post", "<a href='https://cdn.example.com/hige%2013s.zip?knm=tfile.zip'>hige 13s.zip 0.01MB</a>", []));
        Assert.Equal("hige 13s.zip", candidates.Single().Filename);
    }

    [Theory]
    [InlineData("OtherAnime 01.smi")]
    [InlineData("Season 2/01.smi")]
    public async Task BatchArchiveRejectsWrongAnimeAndDirectorySeason(string member)
    {
        var handler = new ReplayHandler(r =>
        {
            var uri = r.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test 1기","originalSubject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(Samples.Zip((member, Samples.Utf16)));
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"1"},"entry":[{"title":{"$t":"Test 1기 전체 자막"},"content":{"$t":"<a href='https://files.example.com/batch.zip'>Test 전체.zip</a>"},"link":[{"rel":"alternate","href":"https://example.blogspot.com/batch"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false, Blogs = [new("https://example.blogspot.com/", BlogPlatform.Blogspot, "example.blogspot.com")] }).SearchAsync("Test", default);
        Assert.False(report.Success);
        Assert.Empty(report.Anime.Single().Subtitles);
    }

    [Fact]
    public async Task HtmlResponseCannotConfirmBatchEpisodeRange()
    {
        var handler = new ReplayHandler(r =>
        {
            var uri = r.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Text("<html>Login</html>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"1"},"entry":[{"title":{"$t":"Test 1-12화"},"content":{"$t":"<a href='https://files.example.com/batch.zip'>Test 1-12.zip</a>"},"link":[{"rel":"alternate","href":"https://example.blogspot.com/batch"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Blogs = [new("https://example.blogspot.com/", BlogPlatform.Blogspot, "example.blogspot.com")] }).SearchAsync("Test", default);
        Assert.False(report.Complete);
        Assert.Empty(report.Anime.Single().Subtitles);
    }

    [Fact]
    public void KoreanSeasonAndEpisodeLabelsDoNotPolluteAttachmentTitle()
    {
        var anime = new ResolvedAnime(1, "그녀도 여친 2기", "Test", ["그녀도 여친 2기"], "fixture", 2);
        Core.Subtitles.ArchiveSelector.ValidateIdentity("그녀도 여친 시즌2 제10화 (Ohys).smi", anime, EpisodeNumber.Parse("10"));
        Assert.Equal(2, Core.Identity.TitleMatcher.ExplicitSeason("그녀도 여친 시즌2 제10화"));
    }

    [Fact]
    public async Task GenericSubtitleCategoryCannotAuthorizeOtherAnime()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("animeNo", StringComparison.Ordinal) && !uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test","originalSubject":"Test"}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"1","website":"https://example.tistory.com/1","name":"Maker","updDt":"2026"}]}""");
            return ReplayHandler.Text("<meta property='og:title' content='Other 1화'><script>window.T.entryInfo = {\"categoryLabel\":\"2021/자막\"};</script><div class='contents_style'><a href='https://files.example.com/Other%2001.smi'>Other 01.smi</a></div>", "text/html");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false }).SearchAsync("Test", default);
        Assert.Empty(report.Anime.Single().Subtitles);
    }

    [Theory]
    [InlineData("Other 01.smi", "Test")]
    [InlineData("Test!! 01.smi", "Test!!!")]
    public async Task MatchingPostCannotPromoteUnverifiedAttachmentAlias(string filename, string animeTitle)
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"TITLE","originalSubject":"TITLE"}],"last":true}}""".Replace("TITLE", animeTitle, StringComparison.Ordinal));
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"1","website":"https://example.tistory.com/1","name":"Maker","updDt":"2026"}]}""");
            return ReplayHandler.Text($"<meta property='og:title' content='{animeTitle} 1화'><script>window.T.entryInfo = {{\"categoryLabel\":\"Subtitles\"}};</script><div class='contents_style'><a href='https://files.example.com/{Uri.EscapeDataString(filename)}'>{filename}</a></div>", "text/html");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false }).SearchAsync(animeTitle, default);
        Assert.Empty(report.Anime.Single().Subtitles);
        Assert.False(report.Success);
    }

    [Fact]
    public async Task EmbeddedCategoryPostsKeepCategoryIdentity()
    {
        var handler = new ReplayHandler(_ => ReplayHandler.Text("<h2><a href='/1'>엄마 1</a></h2><div class='entry'><div class='contents_style'><a href='https://files.example.com/HIGE%2001.smi'>HIGE 01.smi</a></div></div><h2><a href='/2'>수염 2</a></h2><div class='entry'><div class='contents_style'><a href='https://files.example.com/HIGE%2002.smi'>HIGE 02.smi</a></div></div>", "text/html"));
        using var session = new RequestSession(handler);
        var posts = new List<PostCandidate>();
        await foreach (var post in new BlogExplorer(session).ExploreAsync(new("https://example.tistory.com/", BlogPlatform.Tistory, "example.tistory.com"), new(1, "Test", "Test", ["Test"], "fixture"), default, exhaustive: true, categoryUrls: ["https://example.tistory.com/category/21/HIGE"], categoryOnly: true))
        {
            posts.Add(post);
            if (posts.Count == 2) break;
        }
        Assert.Equal(2, posts.Count);
        Assert.All(posts, p => Assert.Equal("https://example.tistory.com/category/21/HIGE", p.CategoryUrl));
    }

    [Fact]
    public async Task LaterSeasonBatchDoesNotClaimPreviousSeasonAbsoluteEpisodes()
    {
        var zip = Samples.Zip(("Test 01.smi", Samples.Utf16), ("Test 24.smi", Samples.Utf16));
        var handler = new ReplayHandler(r =>
        {
            var uri = r.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test 2기","originalSubject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://example.blogspot.com/batch","name":"Maker","updDt":"2026"}]}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(zip);
            if (uri.AbsolutePath == "/batch") return ReplayHandler.Text("<meta property='og:title' content='Test 2기 전체 자막'><div class='post-body'><a href='https://files.example.com/Test.zip'>Test 전체.zip</a></div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false }).SearchAsync("Test", default);
        Assert.Empty(report.Anime.Single().Subtitles);
        Assert.Contains(report.Anime.Single().Attempts, a => a.Status == "numbering-ambiguous");
    }
}
