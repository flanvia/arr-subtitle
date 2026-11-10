using System.Security.Cryptography;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Subtitles;

public static class SubtitleStore
{
    public static string Stem(ResolvedAnime anime, EpisodeTarget target, string output)
    {
        if (target.MediaPath is not null) return Path.Combine(Path.GetDirectoryName(Path.GetFullPath(target.MediaPath))!, Path.GetFileNameWithoutExtension(target.MediaPath));
        var safe = string.Concat(anime.Title.Select(c => Path.GetInvalidFileNameChars().Contains(c) || c is '/' or '\\' or ':' || char.IsControl(c) ? '_' : c)).Trim().Trim('.');
        if (safe.Length > 100) safe = safe[..100];
        if (safe.Length == 0) safe = $"anime-{anime.AnimeId}";
        var directory = anime.Season.HasValue ? Path.Combine(Path.GetFullPath(output), $"Season {anime.Season}") : Path.GetFullPath(output);
        return Path.Combine(directory, $"{safe} - {target.Episode.Value.PadLeft(2, '0')}");
    }

    public static EpisodeResult? Existing(ResolvedAnime anime, EpisodeTarget target, string output)
    {
        var stem = Stem(anime, target, output);
        foreach (var suffix in new[] { "ko", "kor" })
        foreach (var extension in new[] { ".smi", ".srt", ".ass", ".ssa" })
        {
            var path = $"{stem}.{suffix}{extension}";
            if (!File.Exists(path)) continue;
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0) throw new IOException("Subtitle target is a symbolic link");
            if (new FileInfo(path).Length > 10 * 1024 * 1024) throw new IOException("Existing subtitle exceeds size limit");
            var bytes = File.ReadAllBytes(path);
            SubtitleValidator.Validate(bytes, extension);
            return new(target.Episode.Value, ResultStatus.AlreadyPresent, path, Sha256: Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant());
        }
        return null;
    }

    public static async Task<EpisodeResult> SaveAsync(SubtitlePayload payload, ResolvedAnime anime, EpisodeTarget target, string output, string page, CancellationToken ct)
    {
        var path = Stem(anime, target, output) + ".ko" + payload.Extension;
        var previous = Existing(anime, target, output);
        if (previous is not null) return previous;
        await AtomicFile.WriteAsync(path, payload.Bytes, false, ct);
        return new(target.Episode.Value, ResultStatus.Downloaded, path, page, Convert.ToHexString(SHA256.HashData(payload.Bytes)).ToLowerInvariant(), ArchiveMember: payload.ArchiveMember);
    }
}
