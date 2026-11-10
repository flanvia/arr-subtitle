using System.Collections.Concurrent;
using System.Globalization;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

internal sealed class SearchAnime(ResolvedAnime anime)
{
    public ResolvedAnime Anime { get; set; } = anime;
    public decimal CaptionMaximum { get; set; }
    public bool AliasesChecked { get; set; }
    public AnimeMapping? Mapping { get; set; }
    public EpisodeNumberingRegistry Numbering { get; } = new();
    public ConcurrentDictionary<string, ConcurrentDictionary<string, bool>> Aliases { get; } = new();
    private readonly ConcurrentDictionary<(string Episode, string Page), (SubtitleListing Listing, string Source)> listings = new();
    private readonly ConcurrentQueue<SourceAttempt> attempts = new();
    private readonly ConcurrentDictionary<string, bool> expectedEpisodes = new();
    private int incomplete;

    public void Record(SourceAttempt attempt, bool failed = false)
    {
        attempts.Enqueue(attempt);
        if (failed) Interlocked.Exchange(ref incomplete, 1);
    }

    public bool Add(SubtitleListing listing, string source)
    {
        var key = (listing.Episode, listing.Page);
        if (listings.TryAdd(key, (listing, source))) return true;
        listings.AddOrUpdate(key, (listing, source), (_, previous) => (previous.Listing with { Attachments = previous.Listing.Attachments.Concat(listing.Attachments).Distinct().Order(StringComparer.Ordinal).ToArray() }, source));
        return false;
    }
    public void Expect(IEnumerable<EpisodeNumber> episodes)
    {
        foreach (var episode in episodes) expectedEpisodes.TryAdd(episode.Value, true);
    }
    public void Invalidate(string source)
    {
        foreach (var item in listings.Where(p => p.Value.Source == source)) listings.TryRemove(item.Key, out _);
    }

    public AnimeSearchResult Result()
    {
        var ordered = listings.Values.Select(v => v.Listing).OrderBy(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).ThenBy(l => l.Maker, StringComparer.Ordinal).ThenBy(l => l.Page, StringComparer.Ordinal).ToArray();
        var integers = ordered.Select(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).Where(n => n > 0 && n == decimal.Truncate(n)).Select(n => (int)n).ToHashSet();
        var last = integers.DefaultIfEmpty(0).Max();
        if (last > 10000) { Record(new("episodes", "incomplete", "Episode coverage limit reached"), true); last = 10000; }
        var designated = EpisodeParser.Read(Anime.Title, true);
        var expected = designated.Length > 0 ? designated.Select(e => e.Value) : Enumerable.Range(1, last).Select(n => n.ToString(CultureInfo.InvariantCulture)).Concat(expectedEpisodes.Keys).Distinct().OrderBy(e => decimal.Parse(e, CultureInfo.InvariantCulture));
        var found = ordered.Select(l => l.Episode).ToHashSet(StringComparer.Ordinal);
        return new(Anime, ordered, expected.Where(e => !found.Contains(e)).ToArray(), incomplete == 0, attempts.OrderBy(a => a.Source, StringComparer.Ordinal).ThenBy(a => a.Status, StringComparer.Ordinal).ToArray());
    }
}

internal sealed class SearchSource(BlogDescriptor blog)
{
    public BlogDescriptor Blog { get; } = blog;
    public ConcurrentDictionary<int, (SearchAnime Anime, string Maker)> Members { get; } = new();
    public ConcurrentDictionary<int, ConcurrentDictionary<string, bool>> Categories { get; } = new();
    public bool Started { get; set; }
    public bool Finished { get; set; }
}

internal static class SearchErrors
{
    public static string Describe(Exception error) => error is HttpRequestException ? "HTTP request failed" : error.Message.Contains("http", StringComparison.OrdinalIgnoreCase) ? error.GetType().Name : error.Message;
}
