using System.Text.RegularExpressions;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Identity;

public static class MediaPathParser
{
    public static MediaIdentity Parse(string mediaPath)
    {
        var path = Path.GetFullPath(mediaPath);
        var name = Path.GetFileNameWithoutExtension(path);
        name = Regex.Replace(name, @"\[[^\]]*\]", " ", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)).Trim();
        name = Regex.Replace(name, @"\s*\((?:\d{3,4}p|HEVC|AVC)[^)]*\)", "", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)).Trim();
        var match = Regex.Match(name, @"^(.*?)\s+[sS](\d+)[eE](\d+)(.*)$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        int? fileSeason = null;
        string title; string episode; string tail;
        if (match.Success)
        {
            title = match.Groups[1].Value.Trim(); fileSeason = int.Parse(match.Groups[2].Value); episode = match.Groups[3].Value; tail = match.Groups[4].Value;
        }
        else
        {
            match = Regex.Match(name, @"^(.*)\s+-\s*(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?:화)?(.*)$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
            if (!match.Success) match = Regex.Match(name, @"^(.*)\s+(\d{1,4}(?:\.\d+)?)(?:v\d+)?(?:화)?\s*$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
            if (!match.Success && EpisodeNumber.TryParse(name, out var bare)) { title = ""; episode = bare.Value; tail = ""; }
            else if (!match.Success) throw new CollectorException(ResultStatus.AmbiguousMatch, "Cannot identify episode from filename");
            else { title = match.Groups[1].Value.Trim().TrimEnd('-').Trim(); episode = match.Groups[2].Value; tail = match.Groups[3].Value; }
        }
        if (Regex.IsMatch(tail, @"^\s*(?:-|[eE])\s*\d", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) throw new CollectorException(ResultStatus.UnsupportedSource, "Multi-episode video is not supported");
        var parent = Directory.GetParent(path)!;
        var seasonMatch = Regex.Match(parent.Name, @"^Season\s+0*(\d+)$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        int? folderSeason = seasonMatch.Success ? int.Parse(seasonMatch.Groups[1].Value) : null;
        var series = seasonMatch.Success ? parent.Parent ?? parent : parent;
        var titleSeason = TitleMatcher.ExplicitSeason(title);
        var seasons = new[] { folderSeason, fileSeason, titleSeason }.Where(s => s.HasValue).Select(s => s!.Value).Distinct().ToArray();
        if (seasons.Length > 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Folder and filename season conflict");
        if (seasons.Contains(0)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Special season requires an explicit mapping");
        return new(new(series.Name, Season: seasons.FirstOrDefault() is var s && seasons.Length > 0 ? s : null, LibraryPath: series.FullName, FileTitle: string.IsNullOrEmpty(title) ? null : title, MediaPath: path), EpisodeNumber.Parse(episode));
    }
}
