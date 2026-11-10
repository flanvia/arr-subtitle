using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Configuration;

public sealed record AnimeMapping(string Title, int Season, int AnimeId, string[] Aliases, string? LibraryPath = null, int EpisodeOffset = 0);
public sealed record CollectorSettings
{
    public bool AniListEnabled { get; set; } = true;
    public BlogDescriptor[] Blogs { get; set; } = [];
    public AnimeMapping[] Mappings { get; set; } = [];
}
