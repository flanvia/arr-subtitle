using System.Text;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Models;
using AngleSharp.Html.Parser;

namespace ArrSubtitle.Core.Subtitles;

public static class SubtitleValidator
{
    static SubtitleValidator() => Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);

    public static void Validate(byte[] bytes, string extension)
    {
        if (bytes.Length is 0 or > 10 * 1024 * 1024) throw Invalid("Invalid subtitle size");
        string text;
        try
        {
            if (bytes.Length >= 2 && bytes[0] == 0xff && bytes[1] == 0xfe) text = new UnicodeEncoding(false, true, true).GetString(bytes);
            else if (bytes.Length >= 2 && bytes[0] == 0xfe && bytes[1] == 0xff) text = new UnicodeEncoding(true, true, true).GetString(bytes);
            else
            {
                try { text = new UTF8Encoding(false, true).GetString(bytes); }
                catch (DecoderFallbackException) { text = Encoding.GetEncoding(949, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback).GetString(bytes); }
            }
        }
        catch (DecoderFallbackException) { throw Invalid("Unsupported or invalid subtitle encoding"); }
        if (!text.Any(c => c is >= '가' and <= '힣')) throw Invalid("Subtitle contains no Korean text");
        var regexOptions = RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Multiline;
        var valid = extension.ToLowerInvariant() switch
        {
            ".smi" => IsSami(text),
            ".srt" => Regex.IsMatch(text, @"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}", regexOptions, TimeSpan.FromSeconds(1)),
            ".ass" or ".ssa" => text.Contains("[Events]", StringComparison.OrdinalIgnoreCase) && Regex.IsMatch(text, @"^Dialogue:\s*[^\r\n]*\d:\d{2}:\d{2}\.\d{2}", regexOptions, TimeSpan.FromSeconds(1)),
            _ => false
        };
        if (!valid) throw Invalid("Invalid subtitle structure");
    }

    private static bool IsSami(string text)
    {
        var clean = Regex.Replace(text, @"<!--.*?-->|<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>", "", RegexOptions.IgnoreCase | RegexOptions.Singleline | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (!Regex.IsMatch(clean.TrimStart('\ufeff', ' ', '\r', '\n', '\t'), @"^(?:<!doctype\s+sami[^>]*>\s*)?<sami\b", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))) return false;
        var document = new HtmlParser().ParseDocument(clean);
        return document.QuerySelectorAll("sync[start]").Any(sync => long.TryParse(sync.GetAttribute("start"), out var start) && start >= 0) && document.QuerySelectorAll("p").Any(p => p.TextContent.Any(c => c is >= '가' and <= '힣'));
    }

    public static string? DetectExtension(byte[] bytes)
    {
        foreach (var extension in new[] { ".smi", ".srt", ".ass", ".ssa" })
        {
            try { Validate(bytes, extension); return extension; }
            catch (CollectorException) { }
        }
        return null;
    }

    private static CollectorException Invalid(string message) => new(ResultStatus.InvalidSubtitle, message);
}
