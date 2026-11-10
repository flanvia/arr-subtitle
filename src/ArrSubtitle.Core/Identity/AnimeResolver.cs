using System.Text.Json;
using System.Xml;
using System.Xml.Linq;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Identity;

public sealed class AnimeResolver(RequestSession session, CollectorSettings settings)
{
    public AnimeMapping? GetMapping(AnimeQuery query)
    {
        var matches = settings.Mappings.Where(m => (query.Season is null || m.Season == query.Season) && (query.AnimeId.HasValue ? query.AnimeId == m.AnimeId : TitleMatcher.Equal(m.Title, query.Title ?? "") || m.Aliases.Any(a => TitleMatcher.Equal(a, query.Title ?? "") || TitleMatcher.Equal(a, query.FileTitle ?? ""))) && (m.LibraryPath is null || query.LibraryPath is not null && Path.GetFullPath(m.LibraryPath) == Path.GetFullPath(query.LibraryPath))).ToArray();
        if (matches.Length > 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Multiple explicit mappings match");
        return matches.SingleOrDefault();
    }

    public async Task<ResolvedAnime> ResolveAsync(AnimeQuery query, CancellationToken ct)
    {
        var api = new AnissiaClient(session);
        var mapping = GetMapping(query);
        var explicitId = query.AnimeId ?? mapping?.AnimeId;
        if (explicitId.HasValue)
        {
            if (mapping is not null && query.AnimeId.HasValue && mapping.AnimeId != query.AnimeId) throw new CollectorException(ResultStatus.AmbiguousMatch, "Explicit ID conflicts with mapping");
            var detail = await api.GetDetailAsync(explicitId.Value, ct);
            var resolved = FromDetail(detail, mapping?.Aliases ?? [], "explicit mapping or anime ID") with { Season = query.Season ?? mapping?.Season };
            if (mapping is null) ValidateLocalIdentity(query, resolved);
            return resolved;
        }
        var local = ReadLocalEvidence(query).Titles;
        ValidateLocalIdentity(query, null);
        var titles = new[] { query.FileTitle, query.Title }.Concat(local).Where(t => !string.IsNullOrWhiteSpace(t)).Select(t => t!).Distinct(StringComparer.Ordinal).ToArray();
        if (titles.Length == 0) throw new CollectorException(ResultStatus.AmbiguousMatch, "No anime title provided");
        var specific = query.FileTitle ?? query.Title!;
        var errors = new List<string>();
        var direct = new Dictionary<int, AnimeDetail>();
        foreach (var title in titles)
        {
            try
            {
                foreach (var candidate in await api.SearchAsync(title, ct))
                {
                    var detail = await api.GetDetailAsync(candidate.AnimeId, ct);
                    if (new[] { detail.Title, detail.OriginalTitle }.Any(t => TitleMatcher.Equal(t, specific))) direct[detail.AnimeId] = detail;
                }
            }
            catch (Exception ex) when (ex is not OperationCanceledException) { errors.Add(ex.GetType().Name); }
            if (direct.Count > 0) break;
        }
        if (direct.Count == 1 && SeasonFits(query, direct.Values.Single(), specific))
        {
            var resolved = FromDetail(direct.Values.Single(), [], "exact Anissia title") with { Season = query.Season };
            ValidateLocalIdentity(query, resolved);
            return resolved;
        }
        if (direct.Count > 1) throw Ambiguous(direct.Values);
        if (settings.AniListEnabled)
        {
            var metadata = new AniListClient(session);
            foreach (var title in titles)
            {
                MetadataAnime[] matches;
                try { matches = (await metadata.SearchAsync(title, ct)).Where(m => m.Format is "TV" or "TV_SHORT" or "ONA" && m.Titles.Any(t => TitleMatcher.Equal(t, specific))).ToArray(); }
                catch (Exception ex) when (ex is not OperationCanceledException) { errors.Add(ex.GetType().Name); continue; }
                if (matches.Length > 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Multiple metadata titles match; specify anime ID");
                if (matches.Length == 0) continue;
                var item = matches[0]; var found = new Dictionary<int, AnimeDetail>();
                foreach (var alias in item.Titles.OrderByDescending(t => t.Any(c => c is >= '\u3040' and <= '\u9fff')).Take(8))
                {
                    try
                    {
                        foreach (var candidate in await api.SearchAsync(alias, ct))
                        {
                            var detail = await api.GetDetailAsync(candidate.AnimeId, ct);
                            if (item.Titles.Any(t => TitleMatcher.Equal(t, detail.OriginalTitle) || TitleMatcher.Equal(t, detail.Title) || t.Any(c => c is >= '\u3040' and <= '\u9fff') && TitleMatcher.Normalize(detail.OriginalTitle).StartsWith(TitleMatcher.Normalize(t) + " ", StringComparison.Ordinal))) found[detail.AnimeId] = detail;
                        }
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException) { errors.Add(ex.GetType().Name); }
                    if (found.Count > 0) break;
                }
                if (found.Count == 1 && SeasonFits(query, found.Values.Single(), specific))
                {
                    var resolved = FromDetail(found.Values.Single(), item.Titles, $"AniList {item.Id} exact title and Anissia native title") with { Season = query.Season };
                    ValidateLocalIdentity(query, resolved);
                    return resolved;
                }
                if (found.Count > 0) throw Ambiguous(found.Values);
            }
        }
        throw new CollectorException(errors.Count > 0 ? ResultStatus.SearchIncomplete : ResultStatus.AmbiguousMatch, errors.Count > 0 ? "Metadata lookup incomplete; specify anime ID or mapping" : "Cannot uniquely identify anime and season; specify anime ID or mapping");
    }

    private static bool SeasonFits(AnimeQuery query, AnimeDetail detail, string specific)
    {
        var explicitSeason = TitleMatcher.ExplicitSeason(detail.Title) ?? TitleMatcher.ExplicitSeason(detail.OriginalTitle) ?? TitleMatcher.ExplicitSeason(specific);
        if (query.Season is null) return true;
        if (explicitSeason.HasValue) return explicitSeason == query.Season;
        if (query.Season == 1) return true;
        return false;
    }

    private static CollectorException Ambiguous(IEnumerable<AnimeDetail> candidates) => new(ResultStatus.AmbiguousMatch, "Specify a season mapping or anime ID. Candidates: " + string.Join(", ", candidates.Select(c => $"{c.AnimeId}: {c.Title}")));
    private static ResolvedAnime FromDetail(AnimeDetail detail, IEnumerable<string> aliases, string evidence) => new(detail.AnimeId, detail.Title, detail.OriginalTitle, new[] { detail.Title, detail.OriginalTitle }.Concat(aliases).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct().ToArray(), evidence);

    private sealed record LocalEvidence(string[] Titles, int[] Seasons, int[] AnimeIds);

    private static LocalEvidence ReadLocalEvidence(AnimeQuery query)
    {
        var titles = new List<string>();
        var seasons = new List<int>(); var ids = new List<int>();
        var paths = new List<string>();
        if (query.LibraryPath is not null) paths.AddRange(new[] { "tvshow.nfo", "metadata.json" }.Select(name => Path.Combine(query.LibraryPath, name)));
        if (query.MediaPath is not null) paths.AddRange(new[] { ".nfo", ".json" }.Select(extension => Path.ChangeExtension(query.MediaPath, extension)));
        foreach (var path in paths.Distinct())
        {
            if (!File.Exists(path)) continue;
            if (new FileInfo(path).Length > 1024 * 1024) throw new InvalidDataException("Local metadata exceeds size limit");
            if (path.EndsWith(".nfo", StringComparison.Ordinal))
            {
                using var reader = XmlReader.Create(path, new() { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 1024 * 1024 });
                var xml = XDocument.Load(reader);
                var episodic = xml.Root?.Name.LocalName == "episodedetails";
                titles.AddRange(xml.Descendants().Where(e => episodic ? e.Name.LocalName == "showtitle" : e.Name.LocalName is "title" or "originaltitle").Select(e => e.Value));
                foreach (var node in xml.Descendants().Where(e => e.Name.LocalName == "season")) if (int.TryParse(node.Value, out var season)) seasons.Add(season);
                foreach (var node in xml.Descendants().Where(e => e.Name.LocalName == "uniqueid" && e.Attribute("type")?.Value.Equals("anissia", StringComparison.OrdinalIgnoreCase) == true)) if (int.TryParse(node.Value, out var id)) ids.Add(id);
            }
            else
            {
                using var doc = JsonDocument.Parse(File.ReadAllBytes(path), new() { MaxDepth = 16 });
                foreach (var key in new[] { "Name", "OriginalTitle", "title", "originalTitle" })
                    if (doc.RootElement.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String) titles.Add(value.GetString()!);
                foreach (var key in new[] { "season", "SeasonNumber" }) if (doc.RootElement.TryGetProperty(key, out var value) && value.TryGetInt32(out var season)) seasons.Add(season);
                if (doc.RootElement.TryGetProperty("animeId", out var animeId) && animeId.TryGetInt32(out var id)) ids.Add(id);
                if (doc.RootElement.TryGetProperty("ProviderIds", out var providerIds) && providerIds.TryGetProperty("Anissia", out var providerId) && int.TryParse(providerId.ToString(), out var providerNumber)) ids.Add(providerNumber);
            }
        }
        return new(titles.Distinct().ToArray(), seasons.Distinct().ToArray(), ids.Distinct().ToArray());
    }

    public static void ValidateLocalIdentity(AnimeQuery query, ResolvedAnime? anime)
    {
        var evidence = ReadLocalEvidence(query);
        if (query.Season.HasValue && evidence.Seasons.Any(s => s != query.Season)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Local metadata season conflicts with path");
        if (anime is not null)
        {
            if (evidence.AnimeIds.Any(id => id != anime.AnimeId)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Local metadata anime ID conflicts with resolved title");
            if (evidence.Titles.Length > 0 && !evidence.Titles.Any(t => anime.Titles.Any(a => TitleMatcher.Equal(a, t)))) throw new CollectorException(ResultStatus.AmbiguousMatch, "Local metadata title conflicts with resolved anime");
        }
    }
}
