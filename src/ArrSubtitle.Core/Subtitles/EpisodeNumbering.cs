using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Subtitles;

public sealed record EpisodeNumbering(int Offset, decimal SeasonMaximum, string Evidence)
{
    public static string SourceKey(BlogDescriptor blog, string? category) => $"{blog.Platform}:{blog.Id}:{category ?? ""}";

    public static void ValidateUnmappedArchive(byte[] bytes, int? season, decimal maximum)
    {
        if (season is null or <= 1 || maximum <= 0 || !bytes.AsSpan().StartsWith(new byte[] { 0x50, 0x4b, 0x03, 0x04 })) return;
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        using var stream = new MemoryStream(bytes, false);
        using var archive = new ZipArchive(stream, ZipArchiveMode.Read, false, Encoding.GetEncoding(949));
        if (archive.Entries.Count > 500) throw new InvalidDataException("ZIP member limit exceeded");
        foreach (var entry in archive.Entries)
        {
            if (!new[] { ".smi", ".srt", ".ass", ".ssa" }.Contains(Path.GetExtension(entry.Name).ToLowerInvariant())) continue;
            var number = EpisodeEvidence.Explicit(entry.Name);
            if (number.HasValue && decimal.Parse(number.Value.Value, CultureInfo.InvariantCulture) > maximum)
                throw new CollectorException(ResultStatus.AmbiguousMatch, "Archive absolute episode numbering is not established");
        }
    }

    public EpisodeNumber? ToSeason(EpisodeNumber source)
    {
        var number = decimal.Parse(source.Value, CultureInfo.InvariantCulture) - Offset;
        return number >= 1 && (SeasonMaximum <= 0 || number <= SeasonMaximum)
            ? EpisodeNumber.Parse(number.ToString(CultureInfo.InvariantCulture)) : null;
    }

    public EpisodeNumber ToSource(EpisodeNumber season) => EpisodeNumber.Parse((decimal.Parse(season.Value, CultureInfo.InvariantCulture) + Offset).ToString(CultureInfo.InvariantCulture));

    public static EpisodeNumbering? FromCaption(Caption caption, PostCandidate post, int? season, decimal maximum)
    {
        if (season is null or <= 1 || !EpisodeNumber.TryParse(caption.Episode, out var captionNumber)) return null;
        var titleNumber = SinglePostEpisode(post.Title);
        if (titleNumber is null) return null;
        var individual = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename) && !Regex.IsMatch(a.Filename, @"\d+\s*[-~]\s*\d+", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)))
            .Select(a => EpisodeEvidence.Explicit(a.Filename)).Where(e => e.HasValue).Select(e => e!.Value).Distinct().ToArray();
        if (individual.Length != 1 || individual[0] != titleNumber.Value) return null;
        var difference = decimal.Parse(titleNumber.Value.Value, CultureInfo.InvariantCulture) - decimal.Parse(captionNumber.Value, CultureInfo.InvariantCulture);
        if (difference < 0 || difference != decimal.Truncate(difference) || difference > 10000) throw new CollectorException(ResultStatus.AmbiguousMatch, "Caption and post episode numbering conflict");
        return new((int)difference, maximum, $"caption {caption.Episode} = source {titleNumber.Value.Value}; title and individual attachment agree");
    }

    public static EpisodeNumber? SinglePostEpisode(string title)
    {
        if (Regex.IsMatch(title, @"\d+\s*[-~]\s*\d+", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) return null;
        var match = Regex.Match(title, @"(?:제\s*)?(?<!\d)(\d{1,4}(?:\.\d+)?)\s*(?:화|회)(?![\p{L}])", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (match.Success) return EpisodeNumber.Parse(match.Groups[1].Value);
        match = Regex.Match(title, @"(?:^|\s)(\d{1,4}(?:\.\d+)?)\s*$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        return match.Success ? EpisodeNumber.Parse(match.Groups[1].Value) : null;
    }
}

public sealed class EpisodeNumberingRegistry
{
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, EpisodeNumbering> values = new(StringComparer.Ordinal);
    private readonly System.Collections.Concurrent.ConcurrentDictionary<string, bool> conflicts = new(StringComparer.Ordinal);

    public bool HasConflict(string key) => conflicts.ContainsKey(key);
    public EpisodeNumbering? Get(string key) => conflicts.ContainsKey(key) ? null : values.GetValueOrDefault(key);
    public bool Register(string key, EpisodeNumbering numbering)
    {
        if (conflicts.ContainsKey(key)) return false;
        if (values.TryGetValue(key, out var previous) && previous.Offset != numbering.Offset)
        {
            conflicts[key] = true;
            values.TryRemove(key, out _);
            return false;
        }
        values[key] = numbering;
        return true;
    }
}
