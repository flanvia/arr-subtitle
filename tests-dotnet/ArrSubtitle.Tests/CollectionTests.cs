using System.Text;
using ArrSubtitle.Cli;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class CollectionTests
{
    private static ReplayHandler DirectHandler() => new(r =>
    {
        var path = r.RequestUri!.AbsolutePath;
        if (path.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"1","website":"https://author.blogspot.com/1.html","name":"author","updDt":"2026"}]}""");
        if (path.Contains("animeNo")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
        if (path == "/1.html") return ReplayHandler.Text("<h1>테스트 1화</h1><div class='post-body'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html");
        if (path == "/01.smi") return ReplayHandler.Bytes(Samples.Utf16);
        throw new Exception("Unexpected request: " + r.RequestUri);
    });

    [Fact]
    public async Task DirectHitSkipsBlogRequests()
    {
        using var dir = new TempDirectory(); var handler = DirectHandler();
        using var session = new RequestSession(handler);
        var collector = new SubtitleCollector(session, new() { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] });
        var report = await collector.CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("1"))], dir.Path), default);
        Assert.True(report.Success);
        Assert.Equal(ResultStatus.Downloaded, report.Episodes[0].Status);
        Assert.DoesNotContain(handler.Calls, c => c.Contains("feeds") || c.Contains("fallback"));
        Assert.Equal(4, handler.Calls.Count);
    }

    [Fact]
    public async Task SearchDoesNotDownloadAttachment()
    {
        var handler = DirectHandler(); using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new()).SearchAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("1"))], ""), default);
        Assert.Equal(ResultStatus.Found, report.Episodes[0].Status);
        Assert.DoesNotContain(handler.Calls, c => c.Contains("files.example"));
    }

    [Fact]
    public async Task MissingCaptionWithNoBlogsReturnsNotFound()
    {
        using var dir = new TempDirectory();
        using var session = new RequestSession(new ReplayHandler(r => ReplayHandler.Text(r.RequestUri!.AbsolutePath.Contains("caption") ? """{"code":"ok","data":[]}""" : """{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""")));
        var report = await new SubtitleCollector(session, new()).CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("7"))], dir.Path), default);
        Assert.Equal(ResultStatus.NotFound, report.Episodes[0].Status);
        Assert.False(report.Success);
    }

    [Fact]
    public async Task HelpDoesNotReadSettingsOrUseNetwork()
    {
        var output = new StringWriter(); var error = new StringWriter();
        var code = await CommandRunner.RunAsync(["--config", "/missing/config.json", "--help"], output, error, default);
        Assert.Equal(0, code);
        Assert.Contains("collect", output.ToString());
        Assert.Equal("", error.ToString());
    }

    [Fact]
    public async Task BatchZipDownloadsOnceAndMetadataDoesNotScaleWithEpisodes()
    {
        using var dir = new TempDirectory();
        var zip = Samples.Zip(Enumerable.Range(1, 12).Select(n => ($"테스트 {n:00}.smi", Samples.Utf16)).ToArray());
        var handler = new ReplayHandler(r =>
        {
            if (r.RequestUri!.AbsolutePath.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
            if (r.RequestUri.AbsolutePath.Contains("animeNo")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
            if (r.RequestUri.AbsolutePath == "/batch.zip") return ReplayHandler.Bytes(zip);
            return ReplayHandler.Text("""{"feed":{"entry":[{"title":{"$t":"테스트 1-12화 자막"},"content":{"$t":"<a href='https://files.example.com/batch.zip'>테스트 1-12.zip</a>"},"link":[{"rel":"alternate","href":"https://fallback.blogspot.com/batch.html"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] }).CollectAsync(new(new(AnimeId: 1), Enumerable.Range(1, 12).Select(n => new EpisodeTarget(EpisodeNumber.Parse(n.ToString()))).ToArray(), dir.Path), default);
        Assert.True(report.Success, string.Join(';', report.Episodes.Select(e => e.Error)));
        Assert.Equal(12, report.Episodes.Length);
        Assert.Single(handler.Calls, c => c.EndsWith("/batch.zip"));
        Assert.Equal(2, handler.Calls.Count(c => c.Contains("api.anissia.net")));
        Assert.Equal(4, handler.Calls.Count);
    }

    [Fact]
    public async Task GlobalFallbackOnlySeesMissingEpisodes()
    {
        using var dir = new TempDirectory();
        var handler = new ReplayHandler(r =>
        {
            if (r.RequestUri!.Host == "fallback.blogspot.com") return ReplayHandler.Text("""{"feed":{"entry":[{"title":{"$t":"테스트 2화 자막"},"content":{"$t":"<a href='https://files.example.com/02.smi'>02.smi</a>"},"link":[{"rel":"alternate","href":"https://fallback.blogspot.com/2.html"}]}],"link":[]}}""");
            if (r.RequestUri.AbsolutePath == "/02.smi") return ReplayHandler.Bytes(Samples.Utf16);
            if (r.RequestUri.AbsolutePath.Contains("feeds")) return ReplayHandler.Text("""{"feed":{"link":[]}}""");
            if (r.RequestUri.AbsolutePath.Contains("caption")) return ReplayHandler.Text("""{"code":"ok","data":[{"episode":"1","website":"https://author.blogspot.com/1.html","name":"a","updDt":"2026"}]}""");
            if (r.RequestUri.AbsolutePath.Contains("animeNo")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
            if (r.RequestUri.AbsolutePath == "/1.html") return ReplayHandler.Text("<h1>테스트 1화</h1><div class='post-body'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html");
            return ReplayHandler.Bytes(Samples.Utf16);
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] }).CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("1")), new(EpisodeNumber.Parse("2"))], dir.Path), default);
        Assert.True(report.Success);
        Assert.Contains("author.blogspot.com", report.Episodes[0].Page);
        Assert.Contains("fallback.blogspot.com", report.Episodes[1].Page);
        Assert.Single(handler.Calls, c => c.EndsWith("01.smi"));
    }

    [Fact]
    public async Task IncompleteSourceCannotBecomeNotFound()
    {
        using var dir = new TempDirectory();
        var handler = new ReplayHandler(r => r.RequestUri!.Host == "fallback.blogspot.com" ? new(System.Net.HttpStatusCode.Forbidden) : ReplayHandler.Text(r.RequestUri.AbsolutePath.Contains("caption") ? """{"code":"ok","data":[]}""" : """{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}"""));
        using var session = new RequestSession(handler);
        var report = await new SubtitleCollector(session, new() { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] }).CollectAsync(new(new(AnimeId: 1), [new(EpisodeNumber.Parse("7"))], dir.Path), default);
        Assert.Equal(ResultStatus.SearchIncomplete, report.Episodes[0].Status);
    }

    [Fact]
    public async Task EmptyScanReportsRequestErrorWithoutJsonNoise()
    {
        using var dir = new TempDirectory();
        var output = new StringWriter(); var error = new StringWriter();
        var code = await CommandRunner.RunAsync(["scan", dir.Path, "--json"], output, error, default);
        Assert.Equal(2, code);
        Assert.Equal("", output.ToString());
        Assert.Contains("No supported videos", error.ToString());
    }
}
