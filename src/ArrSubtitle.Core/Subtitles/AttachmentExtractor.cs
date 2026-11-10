using System.Text.RegularExpressions;
using AngleSharp.Html.Parser;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Blogs;

namespace ArrSubtitle.Core.Subtitles;

public static class AttachmentExtractor
{
    public static readonly string[] Extensions = [".smi", ".srt", ".ass", ".ssa", ".zip", ".7z", ".rar"];

    public static AttachmentCandidate[] Extract(PostCandidate post)
    {
        var doc = new HtmlParser().ParseDocument(post.Html);
        var found = new Dictionary<string, AttachmentCandidate>();
        foreach (var link in doc.QuerySelectorAll("a[href]"))
        {
            var raw = link.GetAttribute("href");
            if (!Uri.TryCreate(new Uri(post.Url), raw, out var url) || url.Scheme is not ("http" or "https")) continue;
            var filename = link.GetAttribute("download") ?? link.GetAttribute("data-filename") ?? link.GetAttribute("data-file-name") ?? link.GetAttribute("data-origin-name") ?? link.TextContent.Trim();
            var drive = url.Host == "drive.google.com";
            if (drive)
            {
                var match = Regex.Match(url.AbsolutePath, @"^/file/d/([A-Za-z0-9_-]+)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
                var id = match.Success ? match.Groups[1].Value : BlogDetector.ParseQuery(url.Query).GetValueOrDefault("id");
                if (id is null || !Regex.IsMatch(id, @"^[A-Za-z0-9_-]+$", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) continue;
                url = new("https://drive.google.com/uc?export=download&id=" + id);
            }
            if (!Extensions.Contains(Path.GetExtension(filename).ToLowerInvariant()))
            {
                var query = BlogDetector.ParseQuery(url.Query);
                filename = query.GetValueOrDefault("filename") ?? query.GetValueOrDefault("name") ?? Uri.UnescapeDataString(Path.GetFileName(url.AbsolutePath));
            }
            if (drive || Extensions.Contains(Path.GetExtension(filename).ToLowerInvariant()))
            {
                DestinationPolicy.ValidateUrl(url);
                found[url.AbsoluteUri] = new(url.AbsoluteUri, Path.GetFileName(filename.Replace('\\', '/')), post.Url);
            }
        }
        return found.Values.ToArray();
    }
}
