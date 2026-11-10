using System.Globalization;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Subtitles;

public static class EpisodeParser
{
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(1);

    public static EpisodeNumber[] Read(string text, bool requireLabel = false)
    {
        var stem = AttachmentExtractor.Extensions.Contains(Path.GetExtension(text).ToLowerInvariant()) ? Path.GetFileNameWithoutExtension(text) : text;
        stem = Regex.Replace(stem, @"(?<!\d)\d{3,4}[pi]\b", " ", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, Timeout);
        var serial = Regex.Match(stem, @"\bS\d+(?<episodes>E\d{1,4})+(?!\d)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, Timeout);
        if (serial.Success) return serial.Groups["episodes"].Captures.Select(c => EpisodeNumber.Parse(c.Value[1..])).Distinct().ToArray();
        var range = Regex.Match(stem, @"(?<!\d)(\d{1,4})(?:[-~]|\s*~\s*|\s*-\s*(?=\d{1,4}\s*(?:화|회)))(\d{1,4})(?!\d)", RegexOptions.CultureInvariant, Timeout);
        if (range.Success)
        {
            var first = int.Parse(range.Groups[1].Value, CultureInfo.InvariantCulture);
            var last = int.Parse(range.Groups[2].Value, CultureInfo.InvariantCulture);
            if (last < first || last - first > 1000) throw new InvalidDataException("Invalid episode range");
            return Enumerable.Range(first, last - first + 1).Select(n => EpisodeNumber.Parse(n.ToString(CultureInfo.InvariantCulture))).ToArray();
        }
        var labelled = Regex.Matches(stem, @"(?<!\d)(\d{1,4}(?:\.\d+)?)\s*(?:화|회)(?![\p{L}])|\bEP(?:ISODE)?\s*(\d{1,4}(?:\.\d+)?)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, Timeout);
        if (labelled.Count > 0) return labelled.Select(m => EpisodeNumber.Parse(m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value)).Distinct().ToArray();
        if (requireLabel) return [];
        var trailing = Regex.Match(stem, @"(?:^|[\s_-])(\d{1,4}(?:\.\d+)?)(?:[\p{L}][\p{L}\d_-]*)?(?:[.\s]*[\[(].*)?[.\s]*$", RegexOptions.CultureInvariant, Timeout);
        return trailing.Success ? [EpisodeNumber.Parse(trailing.Groups[1].Value)] : [];
    }

    public static EpisodeNumber[] FromPost(string title, ResolvedAnime anime)
    {
        var stripped = title;
        foreach (var name in anime.Titles.Distinct().OrderByDescending(t => t.Length))
        {
            if (string.IsNullOrWhiteSpace(name)) continue;
            var match = Regex.Match(stripped, @"(?<![\p{L}\p{N}])" + Regex.Escape(name) + @"(?![\p{L}\p{N}!'’])", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, Timeout);
            if (!match.Success) continue;
            stripped = stripped.Remove(match.Index, match.Length).Insert(match.Index, " ");
            break;
        }
        return Read(stripped);
    }
}
