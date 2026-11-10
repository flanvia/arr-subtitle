using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class CumulativeBatchTests
{
    [Fact]
    public async Task SearchMapsOnlyCurrentSeasonAndKeepsOriginalEpisode()
    {
        var handler = Handler();
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false }).SearchAsync("Test", default);
        var anime = Assert.Single(report.Anime);
        Assert.Equal(Enumerable.Range(1, 12).Select(n => n.ToString()), anime.Subtitles.Select(s => s.Episode).Distinct());
        Assert.Equal("13", anime.Subtitles.First(s => s.Episode == "1").SourceEpisode);
        Assert.Contains("KNKR 13.smi", anime.Subtitles.First(s => s.Episode == "1").Attachments);
        Assert.DoesNotContain(anime.Subtitles.SelectMany(s => s.Attachments), n => n == "KNKR 01.smi");
    }

    [Fact]
    public async Task DownloadSeasonEpisodeSelectsAbsoluteArchiveMember()
    {
        using var directory = new TempDirectory();
        using var session = new RequestSession(Handler());
        var report = await new SubtitleCollector(session, new() { AniListEnabled = false }).CollectAsync(new(new(AnimeId: 1, Season: 2), [new(EpisodeNumber.Parse("1")), new(EpisodeNumber.Parse("12"))], directory.Path), default);
        Assert.True(report.Success, string.Join(';', report.Episodes.Select(e => e.Error)));
        Assert.Equal("KNKR 13.smi", report.Episodes[0].ArchiveMember);
        Assert.Equal("13", report.Episodes[0].SourceEpisode);
        Assert.Equal("KNKR 24.smi", report.Episodes[1].ArchiveMember);
        Assert.EndsWith("01.ko.smi", report.Episodes[0].Path);
    }

    [Fact]
    public async Task ExplicitOffsetIsAppliedOnlyOnceInDownloadAndSearch()
    {
        using var directory = new TempDirectory();
        var settings = new Core.Configuration.CollectorSettings { AniListEnabled = false, Mappings = [new("Test", 2, 1, ["KNKR"], EpisodeOffset: 12)] };
        using var session = new RequestSession(Handler());
        var collector = new SubtitleCollector(session, settings);
        var report = await collector.CollectAsync(new(new(AnimeId: 1, Season: 2), [new(EpisodeNumber.Parse("1"))], directory.Path), default);
        Assert.True(report.Success);
        Assert.Equal("13", report.Episodes[0].SourceEpisode);
        Assert.Equal("KNKR 13.smi", report.Episodes[0].ArchiveMember);
        var search = await new SubtitleSearch(session, settings).SearchAsync("Test", default);
        Assert.Contains(search.Anime.Single().Subtitles, s => s.Episode == "1" && s.SourceEpisode == "13");
    }

    [Fact]
    public async Task UnprovenCumulativeBatchCannotBeDownloadedAsLocalSeasonNumber()
    {
        using var directory = new TempDirectory();
        var zip = Samples.Zip(Enumerable.Range(1, 24).Select(n => ($"Test {n:00}.smi", Samples.Utf16)).ToArray());
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://example.blogspot.com/batch","name":"Maker","updDt":"2026"}]}""");
            if (uri.Host == "api.anissia.net") return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test 2기","originalSubject":"Test"}}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(zip);
            if (uri.AbsolutePath == "/batch") return ReplayHandler.Text("<meta property='og:title' content='Test 2기 전체 자막'><div class='post-body'><a href='https://files.example.com/all.zip'>Test 01-24.zip</a></div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { AniListEnabled = false }).CollectAsync(new(new(AnimeId: 1, Season: 2), [new(EpisodeNumber.Parse("12"))], directory.Path), default);
        Assert.False(report.Success);
        Assert.DoesNotContain(report.Episodes, e => e.Status == ResultStatus.Downloaded);
        Assert.Empty(Directory.GetFiles(directory.Path, "*.smi", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task ConflictingCaptionOffsetsPreventDownloadBeforeAnyWrite()
    {
        using var directory = new TempDirectory();
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://example.blogspot.com/a","name":"Maker","updDt":"2026-02"},{"episode":"11","website":"https://example.blogspot.com/b","name":"Maker","updDt":"2026-01"}]}""");
            if (uri.Host == "api.anissia.net") return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test 2기","originalSubject":"Test"}}""");
            if (uri.AbsolutePath is "/a" or "/b") return ReplayHandler.Text("<meta property='og:title' content='Test 24화'><div class='post-body'><a href='https://files.example.com/all.zip'>Test 01-24.zip</a><a href='https://files.example.com/24.zip'>Test 24.zip</a></div>", "text/html");
            if (uri.Host == "files.example.com") throw new InvalidOperationException("Conflicting source must not download files");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { AniListEnabled = false }).CollectAsync(new(new(AnimeId: 1, Season: 2), [new(EpisodeNumber.Parse("12"))], directory.Path), default);
        Assert.False(report.Success);
        Assert.DoesNotContain(handler.Calls, c => new Uri(c).Host == "files.example.com");
        Assert.Empty(Directory.GetFiles(directory.Path, "*.smi", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task DifferentMakerWithoutCategoryKeepsOwnNumbering()
    {
        using var directory = new TempDirectory();
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://a.blogspot.com/24","name":"A","updDt":"2026-02"},{"episode":"12","website":"https://b.blogspot.com/12","name":"B","updDt":"2026-01"}]}""");
            if (uri.Host == "api.anissia.net") return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test 2기","originalSubject":"Test"}}""");
            if (uri.Host == "a.blogspot.com") return ReplayHandler.Text("<meta property='og:title' content='Test 24화'><div class='post-body'><a href='https://files.example.com/missing.zip'>Test 24.zip</a></div>", "text/html");
            if (uri.Host == "b.blogspot.com") return ReplayHandler.Text("<meta property='og:title' content='Test 12화'><div class='post-body'><a href='https://files.example.com/local.zip'>Test 12.zip</a></div>", "text/html");
            if (uri.AbsolutePath == "/missing.zip") return new(System.Net.HttpStatusCode.NotFound);
            return ReplayHandler.Bytes(Samples.Zip(("Test 12.smi", Samples.Utf16)));
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { AniListEnabled = false }).CollectAsync(new(new(AnimeId: 1, Season: 2), [new(EpisodeNumber.Parse("12"))], directory.Path), default);
        Assert.True(report.Success);
        Assert.Contains("b.blogspot.com", report.Episodes[0].Page);
        Assert.Equal("12", report.Episodes[0].SourceEpisode);
        Assert.Equal("Test 12.smi", report.Episodes[0].ArchiveMember);
    }

    private static ReplayHandler Handler()
    {
        var batch = Samples.Zip(Enumerable.Range(1, 24).Select(n => ($"KNKR {n:00}.smi", Samples.Utf16)).ToArray());
        return new(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test 2기","originalSubject":"Test"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"12","website":"https://example.tistory.com/24","name":"Maker","updDt":"2026"}]}""");
            if (uri.AbsolutePath.Contains("animeNo", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"Test 2기","originalSubject":"Test"}}""");
            if (uri.Host == "files.example.com") return ReplayHandler.Bytes(batch);
            if (uri.AbsolutePath == "/24") return ReplayHandler.Text("<meta property='og:title' content='치즈루 24'><script>window.T.entryInfo = {\"categoryLabel\":\"22/RK2\"};</script><div class='contents_style'><a href='https://files.example.com/batch.zip'>KNKR 01-24.zip</a><a href='https://files.example.com/24.zip'>KNKR 24.zip</a></div>", "text/html");
            return ReplayHandler.Text("<div class='list_content'><a href='/24'>치즈루 24</a></div>", "text/html");
        });
    }
}
