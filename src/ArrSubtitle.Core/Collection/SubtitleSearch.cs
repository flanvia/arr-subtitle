using System.Collections.Concurrent;
using System.Diagnostics;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Collection;

public sealed class SubtitleSearch(RequestSession session, CollectorSettings settings)
{
    public async Task<SubtitleSearchReport> SearchAsync(string query, CancellationToken ct, Action<SearchProgress>? progress = null, TimeSpan? timeout = null)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 256) throw new ArgumentException("Invalid search query");
        var duration = timeout ?? TimeSpan.FromSeconds(60);
        if (duration < TimeSpan.FromSeconds(1) || duration > TimeSpan.FromMinutes(10)) throw new ArgumentException("Search timeout must be between 1 and 600 seconds");
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(duration);
        var timer = Stopwatch.StartNew();
        var anime = Array.Empty<SearchAnime>();
        var sources = new ConcurrentDictionary<(BlogPlatform, string), SearchSource>();
        var attempts = new List<SourceAttempt>();
        var complete = true;
        try
        {
            progress?.Invoke(new("search", query));
            var catalog = new SearchCatalog(session, settings);
            var candidates = await catalog.FindAsync(query, budget.Token);
            anime = candidates.Select(c => new SearchAnime(new(c.AnimeId, c.Title, c.OriginalTitle, [c.Title, c.OriginalTitle], "Anissia search"))).ToArray();
            var resolver = new PostResolver(session, settings, progress);
            var discovery = new SearchDiscovery(session, resolver);
            await Parallel.ForEachAsync(anime, new ParallelOptions { MaxDegreeOfParallelism = 4, CancellationToken = budget.Token }, async (state, token) =>
            {
                try
                {
                    progress?.Invoke(new("anime", state.Anime.Title));
                    var captions = await catalog.PopulateAsync(state, token);
                    await discovery.SeedAsync(state, captions, sources, token);
                }
                catch (Exception error) when (error is not OperationCanceledException) { state.Record(new($"anime:{state.Anime.AnimeId}", "failed", SearchErrors.Describe(error)), true); }
            });
            var executor = new SearchSources(session, settings, resolver, progress);
            executor.RegisterConfigured(anime, sources);
            if (anime.Length > 0) await executor.RunAsync(sources.Values.ToArray(), duration - timer.Elapsed, budget.Token);
        }
        catch (OperationCanceledException)
        {
            complete = false;
            var status = ct.IsCancellationRequested ? "cancelled" : "timeout";
            attempts.Add(new("search", status));
            foreach (var state in anime) state.Record(new("search", status), true);
            foreach (var source in sources.Values.Where(s => !s.Finished))
                foreach (var member in source.Members.Values) member.Anime.Record(new(DestinationPolicy.Redact(source.Blog.Url), source.Started ? status : "not-started"), true);
            progress?.Invoke(new(status, query, Detail: "Partial results preserved"));
        }
        catch (Exception error) { complete = false; attempts.Add(new("search", "failed", SearchErrors.Describe(error))); }
        var results = anime.Select(a => a.Result()).OrderBy(a => a.Anime.AnimeId).ToArray();
        return new(query, results, complete && results.All(a => a.Complete), attempts.ToArray(), session.RequestCounts, timer.Elapsed.TotalMilliseconds);
    }
}
