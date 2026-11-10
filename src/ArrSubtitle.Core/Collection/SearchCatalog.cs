using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Collection;

internal sealed class SearchCatalog(RequestSession session, CollectorSettings settings)
{
    public async Task<AnimeDetail[]> FindAsync(string query, CancellationToken ct)
    {
        var api = new AnissiaClient(session);
        var candidates = TitleMatcher.SelectSearchCandidates(await api.SearchAllAsync(query, ct), query, settings);
        if (candidates.Length > 0 || !settings.AniListEnabled) return candidates;
        var metadata = await new AniListClient(session).SearchAsync(query, ct);
        var exact = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, query))).ToArray();
        if (exact.Length != 1) return [];
        var found = new Dictionary<int, AnimeDetail>();
        foreach (var title in exact[0].Titles)
        {
            foreach (var item in TitleMatcher.SelectSearchCandidates(await api.SearchAllAsync(title, ct), title, settings)) found[item.AnimeId] = item;
            if (found.Count > 0) break;
        }
        return found.Values.ToArray();
    }

    public async Task<Caption[]> PopulateAsync(SearchAnime state, CancellationToken ct)
    {
        var api = new AnissiaClient(session);
        var detail = state.Anime;
        if (string.IsNullOrWhiteSpace(detail.OriginalTitle))
        {
            var fetched = await api.GetDetailAsync(detail.AnimeId, ct);
            detail = detail with { Title = fetched.Title, OriginalTitle = fetched.OriginalTitle };
        }
        var mappings = settings.Mappings.Where(m => m.AnimeId == detail.AnimeId && m.LibraryPath is null).ToArray();
        if (mappings.Length > 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Multiple explicit mappings for search anime");
        state.Mapping = mappings.SingleOrDefault();
        state.Anime = detail with
        {
            Titles = new[] { detail.Title, detail.OriginalTitle }.Concat(mappings.SelectMany(m => m.Aliases)).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct().ToArray(),
            Season = state.Mapping?.Season ?? TitleMatcher.ExplicitSeason(detail.Title) ?? TitleMatcher.ExplicitSeason(detail.OriginalTitle)
        };
        var captions = await api.GetCaptionsAsync(detail.AnimeId, ct);
        state.CaptionMaximum = captions.Select(c => EpisodeNumber.TryParse(c.Episode, out var e) ? decimal.Parse(e.Value, System.Globalization.CultureInfo.InvariantCulture) : 0).DefaultIfEmpty(0).Max();
        return captions;
    }
}
