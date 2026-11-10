using System.Text;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Subtitles;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SubtitleTests
{
    private static readonly ResolvedAnime Anime = new(1, "테스트", "Test", ["테스트", "Test"], "fixture");
    private static readonly PostCandidate Post = new("https://example.com/post", "테스트 1-12화 자막", "", []);

    [Fact]
    public void SelectsRequestedEpisodeFromBatchZip()
    {
        var zip = Samples.Zip(("테스트 01.smi", Samples.Utf16), ("테스트 02.smi", Samples.Utf16));
        var selected = ArchiveSelector.Select(zip, "테스트 01-12.zip", Anime, EpisodeNumber.Parse("2"), Post);
        Assert.Equal("테스트 02.smi", selected.ArchiveMember);
    }

    [Fact]
    public void RejectsArchiveTraversal()
    {
        var zip = Samples.Zip(("../테스트 01.smi", Samples.Utf16));
        Assert.Throws<CollectorException>(() => ArchiveSelector.Select(zip, "test.zip", Anime, EpisodeNumber.Parse("1"), Post));
    }

    [Fact]
    public void RejectsHtmlWithSubtitleExtension() => Assert.Throws<CollectorException>(() => SubtitleValidator.Validate(Encoding.UTF8.GetBytes("<html>자막 다운로드 오류</html>"), ".smi"));

    [Fact]
    public void AcceptsUtf16SamiAndCp949()
    {
        SubtitleValidator.Validate(Samples.Utf16, ".smi");
        Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
        SubtitleValidator.Validate(Encoding.GetEncoding(949).GetBytes(Samples.Sami), ".smi");
    }

    [Fact]
    public async Task PreservesExistingSubtitle()
    {
        using var dir = new TempDirectory();
        var target = new EpisodeTarget(EpisodeNumber.Parse("1"));
        var saved = await SubtitleStore.SaveAsync(new(Samples.Utf16, ".smi", null), Anime, target, dir.Path, "https://example.com", default);
        var again = await SubtitleStore.SaveAsync(new(Encoding.UTF8.GetBytes(Samples.Sami), ".smi", null), Anime, target, dir.Path, "https://example.com", default);
        Assert.Equal(ResultStatus.AlreadyPresent, again.Status);
        Assert.Equal(Samples.Utf16, await File.ReadAllBytesAsync(saved.Path!));
    }

    [Fact]
    public void ExtractsDriveAttachmentAndIgnoresFolder()
    {
        var attachments = AttachmentExtractor.Extract(Post with { Html = "<a href='https://drive.google.com/uc?id=abc&export=download'>테스트 01.zip</a><a href='https://drive.google.com/drive/folders/xyz'>folder</a>" });
        Assert.Single(attachments);
        Assert.Contains("id=abc", attachments[0].Url);
    }

    [Fact]
    public void DoesNotTreatWorkingSequelAsFirstSeason()
    {
        Assert.False(ArrSubtitle.Core.Identity.TitleMatcher.Mentions("Working!!! 1화 자막", ["Working!!"]));
    }

    [Fact]
    public async Task ConcurrentWriterCannotOverwriteSameTarget()
    {
        using var dir = new TempDirectory();
        var path = Path.Combine(dir.Path, "same.smi");
        var first = Samples.Utf16; var second = Encoding.UTF8.GetBytes(Samples.Sami);
        async Task<bool> Save(byte[] bytes)
        {
            try { await ArrSubtitle.Core.Configuration.AtomicFile.WriteAsync(path, bytes, false, default); return true; }
            catch (IOException) { return false; }
        }
        var results = await Task.WhenAll(Save(first), Save(second));
        Assert.Single(results, r => r);
        var saved = await File.ReadAllBytesAsync(path);
        Assert.True(saved.SequenceEqual(first) || saved.SequenceEqual(second));
    }
}
