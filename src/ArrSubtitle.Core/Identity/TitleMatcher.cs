using System.Text;
using System.Text.RegularExpressions;

namespace ArrSubtitle.Core.Identity;

public static class TitleMatcher
{
    public static Models.AnimeDetail[] SelectSearchCandidates(IEnumerable<Models.AnimeDetail> candidates, string query, Configuration.CollectorSettings settings)
    {
        var items = candidates.ToArray();
        IEnumerable<string> Names(Models.AnimeDetail anime) => new[] { anime.Title, anime.OriginalTitle }.Concat(settings.Mappings.Where(m => m.AnimeId == anime.AnimeId && m.LibraryPath is null).SelectMany(m => m.Aliases));
        var exact = items.Where(a => Names(a).Any(t => !string.IsNullOrWhiteSpace(t) && Equal(t, query))).ToArray();
        if (exact.Length > 0) return exact;
        return items.Where(a => Names(a).Any(t => !string.IsNullOrWhiteSpace(t) && PartialMatch(t, query))).ToArray();
    }

    private static bool PartialMatch(string title, string query)
    {
        var value = Normalize(query);
        var punctuated = value.Length > 0 && char.IsPunctuation(value[^1]);
        var before = punctuated ? @"(?<![\p{L}\p{N}\p{Po}])" : "";
        var after = punctuated ? @"(?![\p{Po}])" : "";
        return Regex.IsMatch(Normalize(title), before + Regex.Escape(value) + after, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
    }

    public static string Normalize(string title) => Regex.Replace(title.Normalize(NormalizationForm.FormKC).ToUpperInvariant(), @"\s+", " ", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)).Trim();
    public static bool Equal(string first, string second) => Normalize(first) == Normalize(second);
    public static bool Mentions(string text, IEnumerable<string> titles)
    {
        var normalized = Normalize(text);
        return titles.Where(t => !string.IsNullOrWhiteSpace(t)).Any(title =>
        {
            var value = Normalize(title);
            return Regex.IsMatch(normalized, @"(?<![\p{L}\p{N}])" + Regex.Escape(value) + @"(?![\p{L}\p{N}!'’])", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        });
    }

    public static int? ExplicitSeason(string text)
    {
        var match = Regex.Match(text, @"(?:\bS(?:EASON\s*)?0*(\d+)(?=\b|E\d)|(?<!\d)(\d+)\s*(?:기|期)|시즌\s*(\d+))", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        return match.Success ? int.Parse(match.Groups[1].Success ? match.Groups[1].Value : match.Groups[2].Success ? match.Groups[2].Value : match.Groups[3].Value) : null;
    }
}
