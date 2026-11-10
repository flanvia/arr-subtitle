using System.Text;
using System.Text.RegularExpressions;

namespace ArrSubtitle.Core.Identity;

public static class TitleMatcher
{
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
