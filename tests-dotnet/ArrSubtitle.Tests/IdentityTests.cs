using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class IdentityTests
{
    [Theory]
    [InlineData("01", "1")]
    [InlineData("12.50", "12.5")]
    [InlineData("0", "0")]
    public void NormalizesEpisodeNumbers(string input, string expected) => Assert.Equal(expected, EpisodeNumber.Parse(input).Value);

    [Theory]
    [InlineData("1e3")]
    [InlineData("-1")]
    [InlineData("1.2.3")]
    public void RejectsInvalidEpisodeNumbers(string input) => Assert.False(EpisodeNumber.TryParse(input, out _));

    [Theory]
    [InlineData("Working!!", "Working'!!")]
    [InlineData("Working'!!", "Working!!!")]
    public void PreservesWorkingSequelTitles(string first, string second) => Assert.False(TitleMatcher.Equal(first, second));

    [Fact]
    public void ReadsSeasonFolderAndReleaseTitle()
    {
        var parsed = MediaPathParser.Parse("/media/Working/Season 02/[Group] Working'!! - 01 (1080p).mkv");
        Assert.Equal("Working", parsed.Query.Title);
        Assert.Equal("Working'!!", parsed.Query.FileTitle);
        Assert.Equal(2, parsed.Query.Season);
        Assert.Equal("1", parsed.Episode.Value);
    }

    [Fact]
    public void ReadsBareEpisodeUsingFolders()
    {
        var parsed = MediaPathParser.Parse("/media/Working/Season 2/01.mkv");
        Assert.Null(parsed.Query.FileTitle);
        Assert.Equal("Working", parsed.Query.Title);
        Assert.Equal(2, parsed.Query.Season);
    }

    [Fact]
    public void RejectsFolderFilenameSeasonConflict() => Assert.Throws<CollectorException>(() => MediaPathParser.Parse("/media/Working/Season 2/Working S03E01.mkv"));

    [Fact]
    public void RejectsMultiEpisodeVideo() => Assert.Throws<CollectorException>(() => MediaPathParser.Parse("/media/Working/Season 2/Working S02E01E02.mkv"));

    [Fact]
    public async Task ExplicitMappingSkipsAniListAndTitleSearch()
    {
        var handler = new ReplayHandler(_ => ReplayHandler.Text("""{"code":"ok","data":{"subject":"워킹 2기","originalSubject":"Working'!!"}}"""));
        using var session = new RequestSession(handler);
        var settings = new CollectorSettings { Mappings = [new("Working", 2, 123, [])] };
        var anime = await new AnimeResolver(session, settings).ResolveAsync(new("Working", Season: 2), default);
        Assert.Equal(123, anime.AnimeId);
        Assert.Single(handler.Calls);
        Assert.Contains("animeNo/123", handler.Calls[0]);
    }

    [Fact]
    public async Task RequiresMappingForUnprovenSeason()
    {
        var handler = new ReplayHandler(r => r.RequestUri!.AbsolutePath.Contains("autocorrect") ? ReplayHandler.Text("""{"code":"ok","data":["12 Working"]}""") : ReplayHandler.Text("""{"code":"ok","data":{"subject":"Working","originalSubject":"WORKING!!"}}"""));
        using var session = new RequestSession(handler);
        var error = await Assert.ThrowsAsync<CollectorException>(() => new AnimeResolver(session, new() { AniListEnabled = false }).ResolveAsync(new("Working", Season: 2), default));
        Assert.Equal(ResultStatus.AmbiguousMatch, error.Status);
    }

    [Fact]
    public async Task ResolvesRomajiThroughNativeTitle()
    {
        var handler = new ReplayHandler(r =>
        {
            if (r.RequestUri!.Host == "graphql.anilist.co") return ReplayHandler.Text("""{"data":{"Page":{"media":[{"id":1,"format":"TV","seasonYear":2026,"title":{"romaji":"Kyouran Reijou Nia Liston","native":"凶乱令嬢ニア・リストン","english":null},"synonyms":[],"relations":{"edges":[]}}]}}}""");
            if (r.RequestUri.AbsolutePath.Contains("animeNo/")) return ReplayHandler.Text("""{"code":"ok","data":{"subject":"흉란영애 니아 리스톤","originalSubject":"凶乱令嬢ニア・リストン 病弱令嬢に転生した神殺しの武人の華麗なる無双録"}}""");
            if (r.RequestUri.AbsolutePath.Contains("autocorrect")) return ReplayHandler.Text(Uri.UnescapeDataString(r.RequestUri.Query).Contains("凶乱") ? """{"code":"ok","data":["3433 흉란영애 니아 리스톤"]}""" : """{"code":"ok","data":[]}""");
            return ReplayHandler.Text("""{"code":"ok","data":{"content":[],"last":true}}""");
        });
        using var session = new RequestSession(handler);
        var anime = await new AnimeResolver(session, new()).ResolveAsync(new("Kyouran Reijou Nia Liston", Season: 1), default);
        Assert.Equal(3433, anime.AnimeId);
        Assert.Contains("Kyouran Reijou Nia Liston", anime.Titles);
    }
}
