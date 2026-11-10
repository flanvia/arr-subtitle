using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Subtitles;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class EpisodeNumberingTests
{
    [Fact]
    public void CaptionTitleAndIndividualAttachmentEstablishAbsoluteOffset()
    {
        var caption = new Caption("12", "https://example.tistory.com/24", "Maker", "2026");
        var post = new PostCandidate(caption.Website, "치즈루 24", "<a href='https://files.example.com/all.zip'>KNKR 01-24.zip</a><a href='https://files.example.com/24.zip'>KNKR 24.zip</a>", []);
        var numbering = EpisodeNumbering.FromCaption(caption, post, 2, 12);
        Assert.NotNull(numbering);
        Assert.Equal(12, numbering.Offset);
        Assert.Null(numbering.ToSeason(EpisodeNumber.Parse("12")));
        Assert.Equal("1", numbering.ToSeason(EpisodeNumber.Parse("13"))!.Value.Value);
        Assert.Equal("12", numbering.ToSeason(EpisodeNumber.Parse("24"))!.Value.Value);
        Assert.Null(numbering.ToSeason(EpisodeNumber.Parse("25")));
        Assert.Equal("13", numbering.ToSource(EpisodeNumber.Parse("1")).Value);
    }

    [Fact]
    public void BatchMaximumAloneDoesNotEstablishOffset()
    {
        var caption = new Caption("12", "https://example.com/24", "Maker", "2026");
        var post = new PostCandidate(caption.Website, "Test 전체 자막", "<a href='https://files.example.com/all.zip'>Test 01-24.zip</a>", []);
        Assert.Null(EpisodeNumbering.FromCaption(caption, post, 2, 12));
    }

    [Fact]
    public void ConflictingIndividualAttachmentDoesNotEstablishOffset()
    {
        var caption = new Caption("12", "https://example.com/24", "Maker", "2026");
        var post = new PostCandidate(caption.Website, "Test 24화", "<a href='https://files.example.com/23.zip'>Test 23.zip</a>", []);
        Assert.Null(EpisodeNumbering.FromCaption(caption, post, 2, 12));
    }

    [Fact]
    public void ConflictingOffsetsInvalidateRegistryPermanently()
    {
        var registry = new EpisodeNumberingRegistry();
        Assert.True(registry.Register("source", new(12, 12, "first")));
        Assert.False(registry.Register("source", new(13, 12, "second")));
        Assert.Null(registry.Get("source"));
        Assert.True(registry.HasConflict("source"));
        Assert.False(registry.Register("source", new(12, 12, "third")));
    }

    [Fact]
    public void SourceKeysSeparateMakersWithoutCategories()
    {
        var first = EpisodeNumbering.SourceKey(new("https://a.blogspot.com/", BlogPlatform.Blogspot, "a.blogspot.com"), null);
        var second = EpisodeNumbering.SourceKey(new("https://b.blogspot.com/", BlogPlatform.Blogspot, "b.blogspot.com"), null);
        Assert.NotEqual(first, second);
    }
}
