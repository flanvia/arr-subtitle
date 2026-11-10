using System.Text;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;
using ArrSubtitle.Core.Collection;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class ReviewRegressionTests
{
    private static readonly ResolvedAnime Anime = new(1, "테스트", "Test", ["테스트", "Test"], "fixture", 1);
    private static readonly PostCandidate Post = new("https://example.com/1", "테스트 1화", "", []);

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void RejectsConflictingExplicitAttachmentEpisode(bool zip)
    {
        var bytes = zip ? Samples.Zip(("테스트 02.smi", Samples.Utf16)) : Samples.Utf16;
        Assert.Throws<CollectorException>(() => ArchiveSelector.Select(bytes, zip ? "test.zip" : "테스트 02.smi", Anime, EpisodeNumber.Parse("1"), Post));
    }

    [Theory]
    [InlineData("Other Anime 01.smi")]
    [InlineData("테스트 S02E01.smi")]
    public void RejectsConflictingArchiveIdentity(string name) => Assert.Throws<CollectorException>(() => ArchiveSelector.Select(Samples.Zip((name, Samples.Utf16)), "test.zip", Anime, EpisodeNumber.Parse("1"), Post));

    [Fact]
    public void RejectsPunctuationDistinguishedArchiveTitle()
    {
        var anime = new ResolvedAnime(1, "Working!!!", "Working!!!", ["Working!!!"], "fixture", 3);
        Assert.Throws<CollectorException>(() => ArchiveSelector.Select(Samples.Zip(("Working!! 01.smi", Samples.Utf16)), "test.zip", anime, EpisodeNumber.Parse("1"), Post));
    }

    [Fact]
    public void DoesNotReadSeasonNumberAsEpisode()
    {
        var parsed = MediaPathParser.Parse("/media/테스트/Season 2/테스트 2기 - 01.mkv");
        Assert.Equal("1", parsed.Episode.Value);
        Assert.Equal("테스트 2기", parsed.Query.FileTitle);
    }

    [Fact]
    public void DoesNotTreatSeasonTokenAsAttachmentEpisode()
    {
        Assert.False(EpisodeEvidence.Matches("Test.S01E02.1080p.smi", EpisodeNumber.Parse("1")));
        Assert.Equal("2", EpisodeEvidence.Explicit("Test.S01E02.1080p.smi")!.Value.Value);
    }

    [Fact]
    public async Task RejectsLocalMetadataConflict()
    {
        using var dir = new TempDirectory();
        await File.WriteAllTextAsync(Path.Combine(dir.Path, "tvshow.nfo"), "<tvshow><title>Other</title><season>2</season><uniqueid type='anissia'>99</uniqueid></tvshow>");
        using var session = new RequestSession(new ReplayHandler(r => ReplayHandler.Text(r.RequestUri!.AbsolutePath.Contains("autocorrect") ? """{"code":"ok","data":["1 Test"]}""" : """{"code":"ok","data":{"subject":"Test","originalSubject":"テスト"}}""")));
        var error = await Assert.ThrowsAsync<CollectorException>(() => new AnimeResolver(session, new() { AniListEnabled = false }).ResolveAsync(new("Test", Season: 1, LibraryPath: dir.Path), default));
        Assert.Equal(ResultStatus.AmbiguousMatch, error.Status);
    }

    [Fact]
    public void EpisodeNfoUsesShowTitleRatherThanEpisodeTitle()
    {
        using var dir = new TempDirectory();
        var media = Path.Combine(dir.Path, "Test - 01.mkv");
        File.WriteAllText(Path.ChangeExtension(media, ".nfo"), "<episodedetails><title>First episode name</title><showtitle>테스트</showtitle><season>1</season></episodedetails>");
        AnimeResolver.ValidateLocalIdentity(new("테스트", Season: 1, MediaPath: media), Anime);
    }

    [Fact]
    public async Task RejectsUnregisteredCustomDomainRedirect()
    {
        var handler = new ReplayHandler(_ => { var r = new HttpResponseMessage(System.Net.HttpStatusCode.Redirect); r.Headers.Location = new("https://unregistered.example/post"); return r; });
        using var session = new RequestSession(handler);
        await Assert.ThrowsAsync<HttpRequestException>(() => new BlogExplorer(session).ReadPostAsync("https://registered.example/post", default));
        Assert.Single(handler.Calls);
    }

    [Fact]
    public void RejectsSamiTextInsideHtmlErrorScript() => Assert.Throws<CollectorException>(() => SubtitleValidator.Validate(Encoding.UTF8.GetBytes("<html><title>로그인 실패</title><script>const example = '<SAMI><SYNC Start=0>';</script></html>"), ".smi"));

    [Fact]
    public async Task RejectsOversizedSerializedSettingsBeforeWriting()
    {
        using var dir = new TempDirectory(); var path = Path.Combine(dir.Path, "config.json");
        await SettingsStore.UpdateAsync(path, new(), "missing", default);
        var current = await SettingsStore.LoadAsync(path, default);
        var oversized = new CollectorSettings { Mappings = [new("테스트", 1, 1, [new string('가', 180000)])] };
        await Assert.ThrowsAsync<InvalidDataException>(() => SettingsStore.UpdateAsync(path, oversized, current.Hash, default));
        Assert.Equal(current.Hash, (await SettingsStore.LoadAsync(path, default)).Hash);
    }

    [Fact]
    public async Task DeletedPostDoesNotStopRemainingBlogCandidates()
    {
        using var dir = new TempDirectory();
        var handler = new ReplayHandler(r =>
        {
            if (r.RequestUri!.Host == "files.example.com") return ReplayHandler.Bytes(Samples.Utf16);
            if (r.RequestUri.AbsolutePath == "/deleted") return new(System.Net.HttpStatusCode.NotFound);
            if (r.RequestUri.Host == "fallback.blogspot.com") return ReplayHandler.Text("""{"feed":{"entry":[{"title":{"$t":"테스트 1화"},"link":[{"rel":"alternate","href":"https://fallback.blogspot.com/deleted"}]},{"title":{"$t":"테스트 1화"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://fallback.blogspot.com/good"}]}],"link":[]}}""");
            return ReplayHandler.Text(r.RequestUri.AbsolutePath.Contains("caption") ? """{"code":"ok","data":[]}""" : """{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] }).CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("1"))], dir.Path), default);
        Assert.True(report.Success);
    }

    [Fact]
    public async Task HigherPriorityMakerFeedPrecedesLowerPrioritySeed()
    {
        using var dir = new TempDirectory();
        var handler = new ReplayHandler(r =>
        {
            if (r.RequestUri!.AbsolutePath.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://a.blogspot.com/seed","name":"A","updDt":"2026-02"},{"episode":"12","website":"https://b.blogspot.com/seed","name":"B","updDt":"2026-01"}]}""");
            if (r.RequestUri.Host == "api.anissia.net") return ReplayHandler.Text("""{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
            if (r.RequestUri.Host == "files.example.com") return ReplayHandler.Bytes(Samples.Utf16);
            if (r.RequestUri.Host == "a.blogspot.com" && r.RequestUri.AbsolutePath == "/seed") return ReplayHandler.Text("<meta property='og:title' content='테스트 12화'><div class='post-body'>no attachment</div>", "text/html");
            if (r.RequestUri.Host == "a.blogspot.com") return ReplayHandler.Text("""{"feed":{"entry":[{"title":{"$t":"테스트 1화"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://a.blogspot.com/good"}]}],"link":[]}}""");
            return ReplayHandler.Text("<meta property='og:title' content='테스트 1-12화'><div class='post-body'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new()).CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("1"))], dir.Path), default);
        Assert.True(report.Success);
        Assert.Contains("a.blogspot.com", report.Episodes[0].Page);
        Assert.DoesNotContain(handler.Calls, c => c.Contains("b.blogspot.com"));
    }
}
