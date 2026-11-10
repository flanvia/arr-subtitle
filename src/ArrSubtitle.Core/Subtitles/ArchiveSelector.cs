using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Subtitles;

public static class EpisodeEvidence
{
    public static EpisodeNumber? Explicit(string filename)
    {
        var stem = Path.GetFileNameWithoutExtension(filename);
        var serial = Regex.Match(stem, @"[sS]\d+[eE](\d{1,4})(?!\d)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (serial.Success) return EpisodeNumber.Parse(serial.Groups[1].Value);
        var match = Regex.Match(stem, @"(?:[sS]\d+[eE]|(?:^|[\s_-]))(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?:화|회)?(?=$|[\s_\[\(])", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        return match.Success && EpisodeNumber.TryParse(match.Groups[1].Value, out var episode) ? episode : null;
    }

    public static bool Matches(string text, EpisodeNumber episode)
    {
        var serial = Regex.Match(text, @"[sS]\d+[eE](\d{1,4})(?!\d)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (serial.Success) return EpisodeNumber.Parse(serial.Groups[1].Value) == episode;
        var escaped = Regex.Escape(episode.Value).Replace(@"\.", @"[._]");
        if (Regex.IsMatch(text, @"(?<!\d)\d+\s*[-~]\s*\d+(?!\d)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) return false;
        return Regex.IsMatch(text, @"(?<![\d.])0*" + escaped + @"(?:v\d+)?(?!\d|[._]\d)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
    }

    public static bool IsAuxiliary(string filename) => Regex.IsMatch(filename, @"(?:\b(?:OP|ED|FONT|FONTS|NCOP|NCED)\b|폰트|글꼴)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
}

public static class ArchiveSelector
{
    public static SubtitlePayload Select(byte[] downloaded, string filename, ResolvedAnime anime, EpisodeNumber episode, PostCandidate post)
    {
        var extension = Path.GetExtension(filename).ToLowerInvariant();
        if (extension is ".7z" or ".rar") throw new CollectorException(ResultStatus.UnsupportedSource, "Archive format is not supported");
        if (extension != ".zip")
        {
            ValidateIdentity(filename, anime, episode);
            if (!EpisodeEvidence.Matches(filename, episode) && !EpisodeEvidence.Matches(post.Title, episode)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Attachment episode is not established");
            SubtitleValidator.Validate(downloaded, extension);
            return new(downloaded, extension, null);
        }
        try
        {
            using var stream = new MemoryStream(downloaded, false);
            Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
            using var archive = new ZipArchive(stream, ZipArchiveMode.Read, false, Encoding.GetEncoding(949, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback));
            if (archive.Entries.Count > 100 || archive.Entries.Sum(e => e.Length) > 100L * 1024 * 1024) throw new InvalidDataException("ZIP exceeds limits");
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var entry in archive.Entries)
            {
                var normalized = entry.FullName.Replace('\\', '/');
                if (normalized.StartsWith('/') || normalized.Split('/').Contains("..") || normalized.Contains(':') || normalized.Contains('\0') || (entry.ExternalAttributes >> 16 & 0xf000) == 0xa000 || !seen.Add(normalized)) throw new InvalidDataException("Unsafe ZIP member");
            }
            var subtitles = archive.Entries.Where(e => e.Name.Length > 0 && new[] { ".smi", ".srt", ".ass", ".ssa" }.Contains(Path.GetExtension(e.Name).ToLowerInvariant()) && !EpisodeEvidence.IsAuxiliary(e.Name)).ToArray();
            var candidates = subtitles.Where(e => EpisodeEvidence.Matches(e.Name, episode)).ToArray();
            if (candidates.Length == 0 && subtitles.Length == 1 && EpisodeEvidence.Explicit(subtitles[0].Name) is null && EpisodeEvidence.Matches(post.Title, episode)) candidates = subtitles;
            if (candidates.Length != 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Cannot uniquely select ZIP episode");
            var selected = candidates[0];
            ValidateIdentity(selected.Name, anime, episode);
            var season = TitleMatcher.ExplicitSeason(selected.Name);
            var expectedSeason = anime.Season ?? TitleMatcher.ExplicitSeason(anime.Title) ?? TitleMatcher.ExplicitSeason(anime.OriginalTitle);
            if (season.HasValue && expectedSeason.HasValue && season != expectedSeason) throw new CollectorException(ResultStatus.AmbiguousMatch, "ZIP member season conflict");
            if (selected.Length > 10 * 1024 * 1024) throw new InvalidDataException("Subtitle exceeds size limit");
            using var source = selected.Open();
            using var target = new MemoryStream();
            var chunk = new byte[32768]; int read;
            while ((read = source.Read(chunk)) > 0)
            {
                if (target.Length + read > 10 * 1024 * 1024) throw new InvalidDataException("Expanded subtitle exceeds limit");
                target.Write(chunk, 0, read);
            }
            var bytes = target.ToArray(); var ext = Path.GetExtension(selected.Name).ToLowerInvariant();
            SubtitleValidator.Validate(bytes, ext);
            return new(bytes, ext, selected.FullName);
        }
        catch (InvalidDataException ex) { throw new CollectorException(ResultStatus.InvalidSubtitle, ex.Message); }
    }

    private static void ValidateIdentity(string filename, ResolvedAnime anime, EpisodeNumber episode)
    {
        var explicitEpisode = EpisodeEvidence.Explicit(filename);
        if (explicitEpisode.HasValue && explicitEpisode.Value != episode) throw new CollectorException(ResultStatus.AmbiguousMatch, "Attachment episode conflicts with request");
        var season = TitleMatcher.ExplicitSeason(filename);
        var expectedSeason = anime.Season ?? TitleMatcher.ExplicitSeason(anime.Title) ?? TitleMatcher.ExplicitSeason(anime.OriginalTitle);
        if (season.HasValue && expectedSeason.HasValue && season != expectedSeason) throw new CollectorException(ResultStatus.AmbiguousMatch, "Attachment season conflicts with request");
        var stem = Path.GetFileNameWithoutExtension(filename);
        var prefix = Regex.Split(stem, @"(?:[sS]\d+[eE]\d+|(?<!\d)\d{1,4}(?:\.\d+)?)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))[0];
        prefix = Regex.Replace(prefix, @"\[[^\]]*\]|\b(?:attachment|subtitle|subtitles|episode|ep)\b", "", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)).Trim(' ', '.', '_', '-');
        if (prefix.Length == 0) return;
        var key = IdentityKey(prefix);
        var matches = anime.Titles.Any(t => IdentityKey(t) == key || TitleMatcher.Normalize(t).StartsWith(TitleMatcher.Normalize(prefix) + " ", StringComparison.Ordinal) && key.Length >= 4);
        if (!matches) throw new CollectorException(ResultStatus.AmbiguousMatch, "Attachment title is not a verified anime alias");
    }

    private static string IdentityKey(string value) => Regex.Replace(TitleMatcher.Normalize(value), @"[\s._-]+", "", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
}
