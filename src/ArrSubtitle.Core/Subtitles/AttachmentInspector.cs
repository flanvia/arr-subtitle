using System.IO.Compression;
using System.Text;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Subtitles;

internal sealed record AttachmentObservation(string Name, EpisodeNumber[] Episodes);

internal sealed class AttachmentInspector(RequestSession session)
{
    public async Task<AttachmentObservation[]> InspectAsync(AttachmentCandidate attachment, EpisodeNumber[] postEpisodes, CancellationToken ct)
    {
        var named = EpisodeParser.Read(attachment.Filename);
        var extension = Path.GetExtension(attachment.Filename).ToLowerInvariant();
        var subtitle = extension is ".smi" or ".srt" or ".ass" or ".ssa";
        if (subtitle) return [new(attachment.Filename, named.Length > 0 ? named : Unique(postEpisodes))];
        var archive = extension == ".zip" || named.Length > 1 || extension.Length == 0 || !AttachmentExtractor.Extensions.Contains(extension);
        if (!archive) throw new CollectorException(ResultStatus.UnsupportedSource, "Unsupported attachment format");
        var file = await session.GetAsync(attachment.Url, ct, 100 * 1024 * 1024);
        if (!file.Bytes.AsSpan().StartsWith(new byte[] { 0x50, 0x4b, 0x03, 0x04 }))
        {
            var detected = SubtitleValidator.DetectExtension(file.Bytes);
            if (detected is null) throw new CollectorException(ResultStatus.InvalidSubtitle, "Attachment is neither a subtitle nor ZIP archive");
            var responseEpisodes = file.Filename is null ? [] : EpisodeParser.Read(file.Filename);
            if (named.Length > 0 && responseEpisodes.Length > 0 && !named.ToHashSet().SetEquals(responseEpisodes)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Response filename episode conflicts with link");
            var episodes = responseEpisodes.Length > 0 ? responseEpisodes : named;
            return [new(file.Filename ?? attachment.Filename, episodes.Length > 0 ? episodes : Unique(postEpisodes))];
        }
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        using var stream = new MemoryStream(file.Bytes, false);
        using var zip = new ZipArchive(stream, ZipArchiveMode.Read, false, Encoding.GetEncoding(949, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback));
        if (zip.Entries.Count > 500 || zip.Entries.Sum(e => e.Length) > 100L * 1024 * 1024) throw new InvalidDataException("Search archive exceeds limits");
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in zip.Entries)
        {
            var name = entry.FullName.Replace('\\', '/');
            if (name.StartsWith('/') || name.Contains(':') || name.Contains('\0') || name.Split('/').Contains("..") || (entry.ExternalAttributes >> 16 & 0xf000) == 0xa000 || !names.Add(name)) throw new InvalidDataException("Unsafe ZIP member");
        }
        var entries = zip.Entries.Where(e => e.Name.Length > 0 && Path.GetExtension(e.Name).ToLowerInvariant() is ".smi" or ".srt" or ".ass" or ".ssa").Where(e => !EpisodeEvidence.IsAuxiliary(e.Name)).ToArray();
        if (entries.Length == 0) throw new CollectorException(ResultStatus.InvalidSubtitle, "ZIP contains no subtitle files");
        return entries.Select(e =>
        {
            var episodes = EpisodeParser.Read(e.Name);
            return new AttachmentObservation(e.FullName, episodes.Length > 0 ? episodes : entries.Length == 1 ? Unique(postEpisodes) : []);
        }).ToArray();
    }

    private static EpisodeNumber[] Unique(EpisodeNumber[] episodes) => episodes.Distinct().Take(2).Count() == 1 ? [episodes[0]] : [];
}
