using System.Collections.Concurrent;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

internal sealed class SearchDiscovery(RequestSession session, PostResolver resolver)
{
    public async Task SeedAsync(SearchAnime state, Caption[] captions, ConcurrentDictionary<(BlogPlatform, string), SearchSource> sources, CancellationToken ct)
    {
        foreach (var caption in captions)
        {
            try
            {
                var blog = await new BlogDetector(session).DetectAsync(new(caption.Website), ct);
                var source = sources.GetOrAdd((blog.Platform, blog.Id), _ => new(blog));
                source.Members.TryAdd(state.Anime.AnimeId, (state, caption.Name));
                var post = await new BlogExplorer(session).ReadPostAsync(caption.Website, ct, blog);
                var season = TitleMatcher.ExplicitSeason(post.Title);
                if (season.HasValue && state.Anime.Season.HasValue && season != state.Anime.Season)
                {
                    state.Record(new(DestinationPolicy.Redact(post.Url), "season-conflict", "Caption season conflicts with anime title"), true);
                    continue;
                }
                state.Anime = state.Anime with { Season = state.Anime.Season ?? season };
                var labels = post.Labels.Where(label => TitleMatcher.Equal(label, state.Anime.Title) || EpisodeParser.Read(state.Anime.Title, true).Length == 0 && state.Anime.Title.StartsWith(label + " ", StringComparison.Ordinal));
                state.Anime = state.Anime with { Titles = state.Anime.Titles.Concat(labels).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct().ToArray() };
                if (post.CategoryUrl is not null)
                {
                    source.Categories.GetOrAdd(state.Anime.AnimeId, _ => new()).TryAdd(post.CategoryUrl, true);
                    var fileTitles = AttachmentExtractor.Extract(post).Select(a => AttachmentIdentity.Title(a.Filename)).Where(t => t.Length > 0).ToArray();
                    var localAliases = state.Aliases.GetOrAdd(post.CategoryUrl, _ => new());
                    var categoryTitle = Uri.UnescapeDataString(new Uri(post.CategoryUrl).Segments.Last()).Trim('/');
                    var offset = EpisodeNumbering.FromCaption(caption, post, state.Anime.Season, state.CaptionMaximum);
                    var unique = fileTitles.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
                    foreach (var title in unique.Where(t => categoryTitle.Equals(t, StringComparison.OrdinalIgnoreCase) || offset is { Offset: > 0 } && unique.Length == 1)) localAliases.TryAdd(title, true);
                }
                var key = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
                var numbering = state.Mapping is not null ? new EpisodeNumbering(state.Mapping.EpisodeOffset, state.CaptionMaximum, "explicit configured episode offset") : EpisodeNumbering.FromCaption(caption, post, state.Anime.Season, state.CaptionMaximum);
                if (numbering is not null && !state.Numbering.Register(key, numbering))
                {
                    state.Invalidate(key);
                    state.Record(new(DestinationPolicy.Redact(post.Url), "numbering-conflict", "Conflicting caption episode offsets for source"), true);
                    continue;
                }
                await resolver.ResolveAsync(state, post, blog, caption.Name, EpisodeNumber.TryParse(caption.Episode, out var episode) ? [episode] : [], ct);
            }
            catch (Exception error) when (error is not OperationCanceledException) { state.Record(new(DestinationPolicy.Redact(caption.Website), "post-failed", SearchErrors.Describe(error)), true); }
        }
    }
}
