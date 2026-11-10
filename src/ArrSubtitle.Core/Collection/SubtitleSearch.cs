using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

public sealed class SubtitleSearch(RequestSession session, CollectorSettings settings)
{
    public async Task<SubtitleSearchReport> SearchAsync(string query, CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 256) throw new ArgumentException("Invalid search query");
        var timer = Stopwatch.StartNew();
        var results = new List<AnimeSearchResult>();
        var attempts = new List<SourceAttempt>();
        var complete = true;
        try
        {
            var api = new AnissiaClient(session);
            var candidates = await api.SearchAllAsync(query, ct);
            if (candidates.Length == 0 && settings.AniListEnabled)
            {
                var metadata = await new AniListClient(session).SearchAsync(query, ct);
                var identified = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, query))).ToArray();
                if (identified.Length == 1)
                {
                    var native = identified[0].Titles.FirstOrDefault(t => t.Any(c => c is >= '\u3040' and <= '\u9fff'));
                    if (native is not null) candidates = await api.SearchAllAsync(native, ct);
                }
            }
            foreach (var candidate in candidates)
            {
                ct.ThrowIfCancellationRequested();
                try { results.Add(await SearchAnimeAsync(candidate.AnimeId, ct)); }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    complete = false;
                    attempts.Add(new($"anime:{candidate.AnimeId}", "failed", Error(ex)));
                }
            }
        }
        catch (OperationCanceledException) { complete = false; attempts.Add(new("search", "cancelled")); }
        catch (Exception ex) { complete = false; attempts.Add(new("search", "failed", Error(ex))); }
        return new(query, results.ToArray(), complete && results.All(r => r.Complete), attempts.ToArray(), session.RequestCounts, timer.Elapsed.TotalMilliseconds);
    }

    private async Task<AnimeSearchResult> SearchAnimeAsync(int animeId, CancellationToken ct)
    {
        var api = new AnissiaClient(session);
        var detail = await api.GetDetailAsync(animeId, ct);
        var aliases = settings.Mappings.Where(m => m.AnimeId == animeId).SelectMany(m => m.Aliases);
        var anime = new ResolvedAnime(animeId, detail.Title, detail.OriginalTitle, new[] { detail.Title, detail.OriginalTitle }.Concat(aliases).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct().ToArray(), "Anissia search");
        var listings = new Dictionary<(string Episode, string Page), SubtitleListing>();
        var attempts = new List<SourceAttempt>();
        var complete = true;
        Caption[] captions;
        try { captions = await api.GetCaptionsAsync(animeId, ct); }
        catch (Exception ex) when (ex is not OperationCanceledException) { captions = []; complete = false; attempts.Add(new("Anissia captions", "failed", Error(ex))); }
        var detector = new BlogDetector(session);
        var blogs = new Dictionary<(BlogPlatform, string), (BlogDescriptor Blog, string Maker, string? Label)>();
        foreach (var caption in captions)
        {
            try
            {
                var blog = await detector.DetectAsync(new(caption.Website), ct);
                var key = (blog.Platform, blog.Id);
                blogs.TryAdd(key, (blog, caption.Name, null));
                var post = await new BlogExplorer(session).ReadPostAsync(caption.Website, ct, blog);
                var labels = post.Labels.Where(t => t.Length >= 4 && (TitleMatcher.Equal(t, anime.Title) || anime.Title.StartsWith(t + " ", StringComparison.Ordinal)) && TitleMatcher.ExplicitSeason(t) == TitleMatcher.ExplicitSeason(anime.Title)).OrderByDescending(t => t.Length).ToArray();
                if (labels.Length > 0)
                {
                    anime = anime with { Titles = anime.Titles.Concat(labels).Distinct().ToArray() };
                    blogs[key] = (blog, caption.Name, labels[0]);
                }
                await AddPostAsync(post, blog, caption.Name, EpisodeNumber.TryParse(caption.Episode, out var ep) ? [ep] : []);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                complete = false;
                attempts.Add(new(DestinationPolicy.Redact(caption.Website), "post-failed", Error(ex)));
            }
        }
        foreach (var configured in settings.Blogs) blogs.TryAdd((configured.Platform, configured.Id), (configured, configured.Id, null));
        foreach (var source in blogs.Values)
        {
            var explorer = new BlogExplorer(session);
            try
            {
                await foreach (var post in explorer.ExploreAsync(source.Blog, anime, ct, exhaustive: true, label: source.Label))
                {
                    if (!TitleMatcher.Mentions(post.Title + " " + string.Join(' ', post.Labels), anime.Titles)) continue;
                    try { await AddPostAsync(post, source.Blog, source.Maker, []); }
                    catch (Exception ex) when (ex is not OperationCanceledException) { complete = false; attempts.Add(new(DestinationPolicy.Redact(post.Url), "post-failed", Error(ex))); }
                }
                if (!explorer.Complete) complete = false;
                attempts.Add(new(DestinationPolicy.Redact(source.Blog.Url), explorer.Complete ? "complete" : "incomplete"));
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested) { complete = false; attempts.Add(new(DestinationPolicy.Redact(source.Blog.Url), "timeout")); }
            catch (Exception ex) when (ex is not OperationCanceledException) { complete = false; attempts.Add(new(DestinationPolicy.Redact(source.Blog.Url), "failed", Error(ex))); }
        }
        var subtitles = listings.Values.OrderBy(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).ThenBy(l => l.Maker, StringComparer.Ordinal).ThenBy(l => l.Page, StringComparer.Ordinal).ToArray();
        var integers = subtitles.Select(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).Where(n => n == decimal.Truncate(n) && n > 0).Select(n => (int)n).ToHashSet();
        var last = integers.DefaultIfEmpty(0).Max();
        if (last > 10000) { complete = false; last = 10000; attempts.Add(new("episodes", "incomplete", "Episode coverage limit reached")); }
        var missing = Enumerable.Range(1, last).Where(n => !integers.Contains(n)).Select(n => n.ToString(CultureInfo.InvariantCulture)).ToArray();
        return new(anime, subtitles, missing, complete, attempts.ToArray());

        async Task AddPostAsync(PostCandidate post, BlogDescriptor blog, string maker, EpisodeNumber[] authoritative)
        {
            var season = TitleMatcher.ExplicitSeason(post.Title);
            var expected = TitleMatcher.ExplicitSeason(anime.Title) ?? TitleMatcher.ExplicitSeason(anime.OriginalTitle);
            if (season.HasValue && season != expected) return;
            var episodes = ReadEpisodes(post.Title);
            if (episodes.Length == 0) episodes = authoritative;
            if (post.Html.Length == 0) post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
            var attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
            if (attachments.Length == 0)
            {
                post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
                attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
            }
            if (episodes.Length == 0) episodes = attachments.SelectMany(a => ReadEpisodes(a.Filename)).Distinct().ToArray();
            if (episodes.Length == 0 || attachments.Length == 0)
            {
                complete = false; attempts.Add(new(DestinationPolicy.Redact(post.Url), "ambiguous", "Post has no verified episode or attachment")); return;
            }
            foreach (var episode in episodes)
            {
                var matching = attachments.Where(a => EpisodeEvidence.Explicit(a.Filename) is not { } number || number == episode || ReadEpisodes(a.Filename).Contains(episode)).Select(a => a.Filename).Distinct().ToArray();
                if (matching.Length == 0) { complete = false; continue; }
                var page = DestinationPolicy.Redact(post.Url);
                listings[(episode.Value, page)] = new(episode.Value, maker, page, matching);
            }
        }
    }

    internal static EpisodeNumber[] ReadEpisodes(string text)
    {
        var range = Regex.Match(text, @"(?<!\d)(\d{1,4})\s*[-~]\s*(\d{1,4})\s*(?:화|회|\b)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (range.Success)
        {
            var first = int.Parse(range.Groups[1].Value, CultureInfo.InvariantCulture); var last = int.Parse(range.Groups[2].Value, CultureInfo.InvariantCulture);
            if (last < first || last - first > 1000) throw new InvalidDataException("Invalid episode range in post");
            return Enumerable.Range(first, last - first + 1).Select(n => EpisodeNumber.Parse(n.ToString(CultureInfo.InvariantCulture))).ToArray();
        }
        var matches = Regex.Matches(text, @"(?:제\s*)?(?<!\d)(\d{1,4}(?:\.\d+)?)\s*(?:화|회)(?![\p{L}])|\b(?:EP(?:ISODE)?\s*|S\d+E)(\d{1,4}(?:\.\d+)?)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (matches.Count > 0) return matches.Select(m => EpisodeNumber.Parse(m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value)).Distinct().ToArray();
        var explicitEpisode = EpisodeEvidence.Explicit(text);
        return explicitEpisode.HasValue ? [explicitEpisode.Value] : [];
    }

    private static string Error(Exception ex) => ex is HttpRequestException ? "HTTP request failed" : ex.Message.Contains("http", StringComparison.OrdinalIgnoreCase) ? ex.GetType().Name : ex.Message;
}
