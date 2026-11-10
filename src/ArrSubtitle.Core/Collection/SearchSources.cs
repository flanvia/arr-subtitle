using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Collection;

internal sealed class SearchSources(RequestSession session, CollectorSettings settings, PostResolver resolver, Action<SearchProgress>? progress)
{
    public void RegisterConfigured(SearchAnime[] anime, System.Collections.Concurrent.ConcurrentDictionary<(BlogPlatform, string), SearchSource> sources)
    {
        foreach (var blog in settings.Blogs)
        {
            var source = sources.GetOrAdd((blog.Platform, blog.Id), _ => new(blog));
            foreach (var item in anime) source.Members.TryAdd(item.Anime.AnimeId, (item, blog.Id));
        }
    }

    public async Task RunAsync(SearchSource[] sources, TimeSpan remaining, CancellationToken ct)
    {
        var waves = Math.Max(1, (sources.Length + 3) / 4);
        var perSource = TimeSpan.FromSeconds(Math.Max(1, Math.Min(40, remaining.TotalSeconds / waves)));
        await Parallel.ForEachAsync(sources.OrderByDescending(s => s.Categories.Count).ThenBy(s => s.Blog.Url, StringComparer.Ordinal), new ParallelOptions { MaxDegreeOfParallelism = 4, CancellationToken = ct }, async (source, token) =>
        {
            using var budget = CancellationTokenSource.CreateLinkedTokenSource(token);
            budget.CancelAfter(perSource);
            source.Started = true;
            progress?.Invoke(new("source", DestinationPolicy.Redact(source.Blog.Url)));
            var members = source.Members.Values.OrderBy(m => m.Anime.Anime.AnimeId).ToArray();
            var titles = members.SelectMany(m => m.Anime.Anime.Titles).Distinct().ToArray();
            var categories = source.Categories.Values.SelectMany(c => c.Keys).Distinct().ToArray();
            var categoryOnly = categories.Length > 0 && members.All(m => source.Categories.ContainsKey(m.Anime.Anime.AnimeId));
            var explorer = new BlogExplorer(session);
            try
            {
                await foreach (var post in explorer.ExploreAsync(source.Blog, members[0].Anime.Anime with { Titles = titles }, budget.Token, exhaustive: true, searchTitles: members.Select(m => m.Anime.Anime.Title).Distinct().ToArray(), categoryUrls: categories, categoryOnly: categoryOnly))
                {
                    foreach (var member in members)
                    {
                        var category = post.CategoryUrl is not null && source.Categories.TryGetValue(member.Anime.Anime.AnimeId, out var known) && known.ContainsKey(post.CategoryUrl);
                        if (!category && !TitleMatcher.Mentions(post.Title + " " + string.Join(' ', post.Labels), member.Anime.Anime.Titles)) continue;
                        try { await resolver.ResolveAsync(member.Anime, post, source.Blog, member.Maker, [], budget.Token); }
                        catch (Exception error) when (error is not OperationCanceledException) { member.Anime.Record(new(DestinationPolicy.Redact(post.Url), "post-failed", SearchErrors.Describe(error)), true); }
                    }
                }
                foreach (var member in members) member.Anime.Record(new(DestinationPolicy.Redact(source.Blog.Url), explorer.Complete ? "complete" : "incomplete"), !explorer.Complete);
                progress?.Invoke(new("source-finished", DestinationPolicy.Redact(source.Blog.Url), Detail: explorer.Complete ? "complete" : "incomplete"));
                source.Finished = true;
            }
            catch (OperationCanceledException) when (!token.IsCancellationRequested)
            {
                foreach (var member in members) member.Anime.Record(new(DestinationPolicy.Redact(source.Blog.Url), "timeout"), true);
                source.Finished = true;
            }
            catch (Exception error) when (error is not OperationCanceledException)
            {
                foreach (var member in members) member.Anime.Record(new(DestinationPolicy.Redact(source.Blog.Url), "failed", SearchErrors.Describe(error)), true);
                source.Finished = true;
            }
        });
    }
}
