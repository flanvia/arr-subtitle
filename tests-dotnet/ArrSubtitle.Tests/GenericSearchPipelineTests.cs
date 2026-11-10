using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class GenericSearchPipelineTests
{
    [Theory]
    [InlineData("Series - 01CustomGroup.smi", "1")]
    [InlineData("Series - 12AnotherSource.srt", "12")]
    [InlineData("Series S02E03.1080p.ass", "3")]
    [InlineData("86 - 01.smi", "1")]
    [InlineData("Series 01 1080p.ass", "1")]
    [InlineData("Series 01.1080p.ass", "1")]
    public void ParsesEpisodeWithoutReleaseGroupCatalog(string filename, string episode) => Assert.Equal(episode, EpisodeParser.Read(filename).Single().Value);

    [Fact]
    public void NumericTitleIsRemovedOnlyOnce()
    {
        var anime = new ResolvedAnime(1, "86", "86", ["86"], "fixture");
        Assert.Equal("86", EpisodeParser.FromPost("86 86화", anime).Single().Value);
        ArchiveSelector.ValidateIdentity("86 - 01.smi", anime, EpisodeNumber.Parse("1"));
    }

    [Fact]
    public void SerialMultipleEpisodesAreNotReducedToFirstEpisode() => Assert.Equal(new[] { "1", "2" }, EpisodeParser.Read("Series S01E01E02.ass").Select(e => e.Value));

    [Fact]
    public async Task DirectDriveSubtitleIsNotAnArchive()
    {
        var handler = Handler("Series 1화", "<a href='https://drive.google.com/file/d/abc123/view'>Series.smi</a>", null);
        using var session = new RequestSession(handler);
        var report = await Search(session);
        Assert.True(report.Success);
        Assert.Equal("1", Assert.Single(report.Anime.Single().Subtitles).Episode);
        Assert.DoesNotContain(handler.Calls, c => c.Contains("drive.google.com", StringComparison.Ordinal));
    }

    [Fact]
    public async Task SingleUnnamedZipMemberInheritsUniquePostEpisode()
    {
        using var session = new RequestSession(Handler("Series 4화", "<a href='https://files.example.com/archive.zip'>Series.zip</a>", Samples.Zip(("Series.smi", Samples.Utf16))));
        var report = await Search(session);
        var listing = Assert.Single(report.Anime.Single().Subtitles);
        Assert.Equal("4", listing.Episode);
        Assert.Contains("Series.smi", listing.Attachments);
    }

    [Fact]
    public async Task RangeNoticeWithMissingAttachmentRemainsFailure()
    {
        using var session = new RequestSession(Handler("Series 1-13 자막 배포 안내", "No links", null));
        var report = await Search(session);
        Assert.False(report.Success);
        Assert.Contains(report.Anime.Single().Attempts, a => a.Status == "attachment-missing");
    }

    [Fact]
    public async Task UnnumberedMemberCannotBeAssignedToMultipleEpisodes()
    {
        using var session = new RequestSession(Handler("Series 1-3화", "<a href='https://files.example.com/archive.zip'>Series.zip</a>", Samples.Zip(("Series.smi", Samples.Utf16))));
        var report = await Search(session);
        Assert.Empty(report.Anime.Single().Subtitles);
        Assert.Contains(report.Anime.Single().Attempts, a => a.Status == "episode-unresolved");
    }

    [Fact]
    public async Task ExplicitAnimeEpisodeDoesNotReportEarlierEpisodesMissing()
    {
        using var session = new RequestSession(Handler("Series 14화", "<a href='https://files.example.com/Series%2014.smi'>Series 14.smi</a>", null, "Series 14화"));
        var report = await new SubtitleSearch(session, new() { AniListEnabled = false, Blogs = [new("https://example.blogspot.com/", BlogPlatform.Blogspot, "example.blogspot.com")] }).SearchAsync("Series 14화", default);
        Assert.Equal("14", Assert.Single(report.Anime.Single().Subtitles).Episode);
        Assert.Empty(report.Anime.Single().MissingEpisodes);
    }

    [Fact]
    public async Task PartialArchiveReportsDeclaredMissingEpisodes()
    {
        using var session = new RequestSession(Handler("Series 1-3화", "<a href='https://files.example.com/archive.zip'>Series.zip</a>", Samples.Zip(("Series 01.smi", Samples.Utf16))));
        var report = await Search(session);
        Assert.False(report.Success);
        Assert.Equal(new[] { "2", "3" }, report.Anime.Single().MissingEpisodes);
    }

    [Fact]
    public async Task AttachmentCannotContradictSinglePostEpisode()
    {
        using var session = new RequestSession(Handler("Series 2화", "<a href='https://files.example.com/Series%2001.smi'>Series 01.smi</a>", null));
        var report = await Search(session);
        Assert.False(report.Success);
        Assert.Empty(report.Anime.Single().Subtitles);
    }

    [Fact]
    public async Task ReloadedPostCannotChangeAnimeIdentity()
    {
        using var session = new RequestSession(Handler("Series 2화", "", null, fetchedTitle: "Other 2화", fetchedHtml: "<a href='https://files.example.com/02.smi'>02.smi</a>"));
        var report = await Search(session);
        Assert.False(report.Success);
        Assert.Empty(report.Anime.Single().Subtitles);
        Assert.Contains(report.Anime.Single().Attempts, a => a.Status == "post-conflict");
    }

    [Theory]
    [InlineData("Series 자막", "Series 03.smi", "3")]
    [InlineData("Series 03", "Series 04.smi", "")]
    public async Task DownloadedFilenameSuppliesOrConflictsWithEpisodeEvidence(string displayName, string responseName, string expected)
    {
        using var session = new RequestSession(Handler("Series 자막", $"<a href='https://drive.google.com/file/d/abc123/view'>{displayName}</a>", Samples.Utf16, responseName: responseName));
        var report = await Search(session);
        if (expected.Length > 0) Assert.Equal(expected, Assert.Single(report.Anime.Single().Subtitles).Episode);
        else { Assert.False(report.Success); Assert.Empty(report.Anime.Single().Subtitles); }
    }

    private static Task<SubtitleSearchReport> Search(RequestSession session) => new SubtitleSearch(session, new() { AniListEnabled = false, Blogs = [new("https://example.blogspot.com/", BlogPlatform.Blogspot, "example.blogspot.com")] }).SearchAsync("Series", default);

    private static ReplayHandler Handler(string title, string html, byte[]? attachment, string animeTitle = "Series", string? fetchedTitle = null, string? fetchedHtml = null, string? responseName = null) => new(request =>
    {
        var uri = request.RequestUri!;
        if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"TITLE","originalSubject":"Series"}],"last":true}}""".Replace("TITLE", animeTitle, StringComparison.Ordinal));
        if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":[]}""");
        if (uri.Host is "files.example.com" or "drive.google.com")
        {
            var response = ReplayHandler.Bytes(attachment ?? throw new InvalidOperationException("No attachment expected"));
            if (responseName is not null) response.Content.Headers.ContentDisposition = new("attachment") { FileName = responseName };
            return response;
        }
        if (uri.AbsolutePath == "/post") return ReplayHandler.Text($"<meta property='og:title' content='{fetchedTitle ?? title}'><div class='post-body'>{fetchedHtml ?? html}</div>", "text/html");
        using var body = new System.IO.MemoryStream();
        using (var writer = new System.Text.Json.Utf8JsonWriter(body))
        {
            writer.WriteStartObject(); writer.WriteStartObject("feed"); writer.WriteStartObject("openSearch$totalResults"); writer.WriteString("$t", "1"); writer.WriteEndObject();
            writer.WriteStartArray("entry"); writer.WriteStartObject(); writer.WriteStartObject("title"); writer.WriteString("$t", title); writer.WriteEndObject(); writer.WriteStartObject("content"); writer.WriteString("$t", html); writer.WriteEndObject();
            writer.WriteStartArray("link"); writer.WriteStartObject(); writer.WriteString("rel", "alternate"); writer.WriteString("href", "https://example.blogspot.com/post"); writer.WriteEndObject(); writer.WriteEndArray(); writer.WriteEndObject(); writer.WriteEndArray();
            writer.WriteStartArray("link"); writer.WriteEndArray(); writer.WriteEndObject(); writer.WriteEndObject();
        }
        return ReplayHandler.Bytes(body.ToArray());
    });
}
