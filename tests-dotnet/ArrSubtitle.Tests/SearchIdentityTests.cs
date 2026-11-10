using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Subtitles;
using Xunit;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Tests;

public sealed class SearchIdentityTests
{
    [Fact]
    public void PartialSearchPreservesTitlePunctuationBoundaries()
    {
        AnimeDetail[] candidates = [new(1, "WORKING!!!", ""), new(2, "WWW.WORKING!!", "")];
        Assert.Empty(TitleMatcher.SelectSearchCandidates(candidates, "Working!!", new()));
        Assert.Equal(2, TitleMatcher.SelectSearchCandidates(candidates, "Working", new()).Length);
    }

    [Fact]
    public void ExactFullTitleExcludesSequelsAndGenreOnlyResults()
    {
        AnimeDetail[] candidates = [new(1, "마법소녀를 동경해서", "魔法少女にあこがれて"), new(2, "마법소녀를 동경해서 2기", ""), new(3, "도쿄 뮤우뮤우 뉴 2기", "")];
        var result = TitleMatcher.SelectSearchCandidates(candidates, "마법소녀를 동경해서", new());
        Assert.Equal(1, Assert.Single(result).AnimeId);
    }

    [Fact]
    public void PartialTitleOnlyKeepsActualTitleMatches()
    {
        AnimeDetail[] candidates = [new(1, "여친, 빌리겠습니다", ""), new(2, "그녀도 여친", ""), new(3, "도쿄 뮤우뮤우", "")];
        Assert.Equal(new[] { 1, 2 }, TitleMatcher.SelectSearchCandidates(candidates, "여친", new()).Select(a => a.AnimeId));
    }

    [Fact]
    public void PunctuationDistinguishedExactTitleDoesNotMergeSeasons()
    {
        AnimeDetail[] candidates = [new(1, "Working!!", ""), new(2, "Working'!!", ""), new(3, "Working!!!", "")];
        Assert.Equal(2, Assert.Single(TitleMatcher.SelectSearchCandidates(candidates, "Working'!!", new())).AnimeId);
    }

    [Fact]
    public void DriveLinkPreservesVisibleRangeAndAnimeTitle()
    {
        var post = new PostCandidate("https://example.blogspot.com/post", "마법소녀를 동경해서", "<a href='https://drive.google.com/file/d/abc123/view'>마법소녀를 동경해서 1-13 Ohys</a>", []);
        var attachment = Assert.Single(AttachmentExtractor.Extract(post));
        Assert.Equal("마법소녀를 동경해서 1-13 Ohys", attachment.Filename);
        Assert.Contains("id=abc123", attachment.Url);
    }

    [Theory]
    [InlineData("Mahou Shoujo ni Akogarete - 01Ohys.smi", "1")]
    [InlineData("Mahou Shoujo ni Akogarete - 13SubsPlease.smi", "13")]
    public void ReadsEpisodeBeforeKnownReleaseSuffix(string filename, string expected)
    {
        Assert.Equal(expected, EpisodeEvidence.Explicit(filename)!.Value.Value);
        ArchiveSelector.ValidateIdentity(filename, new(1, "마법소녀를 동경해서", "魔法少女にあこがれて", ["Mahou Shoujo ni Akogarete"], "fixture"), EpisodeNumber.Parse(expected));
    }

    [Fact]
    public async Task ExactQueryNeverFetchesUnrelatedAnimeCaptions()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"마법소녀를 동경해서","originalSubject":"Test"},{"animeNo":2,"subject":"도쿄 뮤우뮤우","originalSubject":"Other"}],"last":true}}""");
            Assert.DoesNotContain("/2", uri.AbsolutePath);
            return ReplayHandler.Text("""{"code":"ok","data":[]}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new()).SearchAsync("마법소녀를 동경해서", default);
        Assert.Equal(1, Assert.Single(report.Anime).Anime.AnimeId);
        Assert.DoesNotContain(handler.Calls, url => url.EndsWith("animeNo/2", StringComparison.Ordinal));
    }

    [Fact]
    public async Task NoticeWithoutAttachmentsIsSkippedButUnnamedZipProvidesEpisodes()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test","originalSubject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(Samples.Zip(("Test 01Ohys.smi", Samples.Utf16), ("Test 02SubsPlease.smi", Samples.Utf16)));
            if (uri.AbsolutePath == "/notice") return ReplayHandler.Text("<meta property='og:title' content='Test 작업 공지'><div class='post-body'>다음 주 공개 예정</div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"2"},"entry":[{"title":{"$t":"Test 작업 공지"},"content":{"$t":"다음 주 공개 예정"},"link":[{"rel":"alternate","href":"https://global.blogspot.com/notice"}]},{"title":{"$t":"Test 자막"},"content":{"$t":"<a href='https://files.example.com/Test.zip'>Test.zip</a>"},"link":[{"rel":"alternate","href":"https://global.blogspot.com/subtitles"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { Blogs = [new("https://global.blogspot.com/", BlogPlatform.Blogspot, "global.blogspot.com")] }).SearchAsync("Test", default);
        Assert.True(report.Success);
        Assert.Equal(new[] { "1", "2" }, report.Anime.Single().Subtitles.Select(s => s.Episode));
        Assert.Contains(report.Anime.Single().Attempts, a => a.Status == "skipped-non-subtitle");
        Assert.DoesNotContain(report.Anime.Single().Attempts, a => a.Error == "Post has no verified episode or attachment");
    }
}
