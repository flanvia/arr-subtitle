using System.Text.Json;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Identity;

public sealed record MetadataAnime(int Id, string[] Titles, string Format, int? Year, int[] RelatedIds);

public sealed class AniListClient(RequestSession session)
{
    public async Task<MetadataAnime[]> SearchAsync(string title, CancellationToken ct)
    {
        const string query = "query ($search: String) { Page(page: 1, perPage: 10) { media(search: $search, type: ANIME) { id format seasonYear title { romaji english native } synonyms relations { edges { relationType node { id } } } } } }";
        using var body = new MemoryStream();
        using (var writer = new Utf8JsonWriter(body))
        {
            writer.WriteStartObject(); writer.WriteString("query", query); writer.WriteStartObject("variables"); writer.WriteString("search", title); writer.WriteEndObject(); writer.WriteEndObject();
        }
        var response = await session.PostJsonAsync("https://graphql.anilist.co", System.Text.Encoding.UTF8.GetString(body.ToArray()), ct);
        using var document = JsonDocument.Parse(response.Bytes, new() { MaxDepth = 32 });
        if (document.RootElement.TryGetProperty("errors", out _)) throw new InvalidDataException("AniList returned errors");
        return document.RootElement.GetProperty("data").GetProperty("Page").GetProperty("media").EnumerateArray().Select(item =>
        {
            var titles = item.GetProperty("title").EnumerateObject().Where(p => p.Value.ValueKind == JsonValueKind.String).Select(p => p.Value.GetString()!).Concat(item.GetProperty("synonyms").EnumerateArray().Where(v => v.ValueKind == JsonValueKind.String).Select(v => v.GetString()!)).Distinct().ToArray();
            var related = item.GetProperty("relations").GetProperty("edges").EnumerateArray().Select(e => e.GetProperty("node").GetProperty("id").GetInt32()).ToArray();
            return new MetadataAnime(item.GetProperty("id").GetInt32(), titles, item.GetProperty("format").GetString() ?? "", item.GetProperty("seasonYear").ValueKind == JsonValueKind.Number ? item.GetProperty("seasonYear").GetInt32() : null, related);
        }).ToArray();
    }
}
