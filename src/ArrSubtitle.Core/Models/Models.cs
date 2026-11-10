using System.Globalization;
using System.Text.RegularExpressions;

namespace ArrSubtitle.Core.Models;

public readonly record struct EpisodeNumber(string Value)
{
    public static bool TryParse(string value, out EpisodeNumber result)
    {
        result = default;
        if (!Regex.IsMatch(value, @"^\d{1,6}(?:\.\d{1,4})?$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) return false;
        if (!decimal.TryParse(value, NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var number)) return false;
        result = new(number.ToString("0.####", CultureInfo.InvariantCulture));
        return true;
    }

    public static EpisodeNumber Parse(string value) => TryParse(value, out var result) ? result : throw new ArgumentException("Invalid episode number");
    public override string ToString() => Value;
}

public enum ResultStatus { Found, Downloaded, AlreadyPresent, NotFound, SearchIncomplete, AmbiguousMatch, UnsupportedSource, InvalidSubtitle, WriteFailed }
public enum BlogPlatform { Blogspot, Tistory, Naver, Generic }
public sealed record BlogDescriptor(string Url, BlogPlatform Platform, string Id);
public sealed record AnimeQuery(string? Title = null, int? AnimeId = null, int? Season = null, string? LibraryPath = null, string? FileTitle = null, string? MediaPath = null);
public sealed record EpisodeTarget(EpisodeNumber Episode, string? MediaPath = null);
public sealed record ResolvedAnime(int AnimeId, string Title, string OriginalTitle, string[] Titles, string Evidence, int? Season = null);
public sealed record Caption(string Episode, string Website, string Name, string UpdatedAt);
public sealed record AnimeDetail(int AnimeId, string Title, string OriginalTitle);
public sealed record CollectionRequest(AnimeQuery Query, EpisodeTarget[] Episodes, string OutputDirectory, bool SearchOnly = false);
public sealed record SourceAttempt(string Source, string Status, string? Error = null);
public sealed record EpisodeResult(string Episode, ResultStatus Status, string? Path = null, string? Page = null, string? Sha256 = null, string? Error = null, string? ArchiveMember = null);
public sealed record CollectionReport(string RequestId, ResolvedAnime? Anime, EpisodeResult[] Episodes, SourceAttempt[] Attempts, Dictionary<string, int> Requests, long ReceivedBytes, double ElapsedMilliseconds, double? FirstCandidateMilliseconds, string? ManifestPath = null)
{
    public bool Success => Episodes.Length > 0 && Episodes.All(e => e.Status is ResultStatus.Downloaded or ResultStatus.AlreadyPresent or ResultStatus.Found);
}
public sealed record PostCandidate(string Url, string Title, string Html, string[] Labels);
public sealed record AttachmentCandidate(string Url, string Filename, string Page);
public sealed record SubtitlePayload(byte[] Bytes, string Extension, string? ArchiveMember);
public sealed record MediaIdentity(AnimeQuery Query, EpisodeNumber Episode);
public sealed record SubtitleListing(string Episode, string Maker, string Page, string[] Attachments);
public sealed record AnimeSearchResult(ResolvedAnime Anime, SubtitleListing[] Subtitles, string[] MissingEpisodes, bool Complete, SourceAttempt[] Attempts);
public sealed record SubtitleSearchReport(string Query, AnimeSearchResult[] Anime, bool Complete, SourceAttempt[] Attempts, Dictionary<string, int> Requests, double ElapsedMilliseconds)
{
    public bool Success => Complete && Anime.Length > 0 && Anime.All(a => a.Complete && a.MissingEpisodes.Length == 0 && a.Subtitles.Length > 0);
}
public sealed class CollectorException(ResultStatus status, string message) : Exception(message)
{
    public ResultStatus Status { get; } = status;
}
