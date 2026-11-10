using System.Text.RegularExpressions;

namespace ArrSubtitle.Core.Subtitles;

internal static class AttachmentIdentity
{
    public static string Title(string filename)
    {
        var stem = Path.GetFileNameWithoutExtension(filename);
        stem = Regex.Replace(stem, @"\[[^\]]*\]", " ", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        var match = Regex.Match(stem, @"(?:\s+-\s*|\s+)(?:S\d+E)?\d", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        return match.Success ? stem[..match.Index].Trim(' ', '.', '_', '-') : stem.Trim();
    }
}
