using System.Text.Json;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Serialization;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SettingsTests
{
    [Fact]
    public void ReadableJsonKeepsKoreanAndJapaneseWithoutRemovingHtmlEscaping()
    {
        var settings = new CollectorSettings { Mappings = [new("여친, 빌리겠습니다", 1, 1, ["彼女、お借りします", "<script>"])] };
        var json = JsonSerializer.Serialize(settings, CollectorJsonContext.Readable.CollectorSettings);
        Assert.Contains("여친, 빌리겠습니다", json);
        Assert.Contains("彼女、お借りします", json);
        Assert.DoesNotContain("<script>", json);
        Assert.Equal(settings.Mappings[0].Title, JsonSerializer.Deserialize(json, CollectorJsonContext.Readable.CollectorSettings)!.Mappings[0].Title);
    }

    [Fact]
    public void EmptyObjectPreservesSettingsDefaults()
    {
        var settings = JsonSerializer.Deserialize("{}", CollectorJsonContext.Default.CollectorSettings)!;
        Assert.True(settings.AniListEnabled);
        Assert.Empty(settings.Blogs);
        Assert.Empty(settings.Mappings);
    }

    [Fact]
    public void RoundTripsWithoutReflection()
    {
        var settings = new CollectorSettings { Blogs = [new("https://a.blogspot.com/", BlogPlatform.Blogspot, "a.blogspot.com")] };
        var json = JsonSerializer.Serialize(settings, CollectorJsonContext.Default.CollectorSettings);
        Assert.Equal(settings.Blogs[0], JsonSerializer.Deserialize(json, CollectorJsonContext.Default.CollectorSettings)!.Blogs[0]);
        Assert.False(JsonSerializer.IsReflectionEnabledByDefault);
    }

    [Fact]
    public async Task PreservesMalformedSettings()
    {
        using var dir = new TempDirectory(); var path = Path.Combine(dir.Path, "config.json");
        await File.WriteAllTextAsync(path, "not-json");
        await Assert.ThrowsAsync<JsonException>(() => SettingsStore.LoadAsync(path, default));
        Assert.Equal("not-json", await File.ReadAllTextAsync(path));
    }

    [Fact]
    public async Task RejectsConcurrentUpdateWithoutOverwriting()
    {
        using var dir = new TempDirectory(); var path = Path.Combine(dir.Path, "config.json");
        await SettingsStore.UpdateAsync(path, new(), "missing", default);
        var first = await SettingsStore.LoadAsync(path, default);
        await SettingsStore.UpdateAsync(path, new() { AniListEnabled = false }, first.Hash, default);
        await Assert.ThrowsAsync<IOException>(() => SettingsStore.UpdateAsync(path, new(), first.Hash, default));
        Assert.False((await SettingsStore.LoadAsync(path, default)).Settings.AniListEnabled);
    }
}
