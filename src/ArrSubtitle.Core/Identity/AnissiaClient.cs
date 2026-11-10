using System.Text.Json;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;

namespace ArrSubtitle.Core.Identity;

public sealed class AnissiaClient(RequestSession session)
{
    private const string BaseUrl = "https://api.anissia.net";

    private async Task<JsonDocument> GetAsync(string path, CancellationToken ct)
    {
        var response = await session.GetAsync(BaseUrl + path, ct);
        var document = JsonDocument.Parse(response.Bytes, new() { MaxDepth = 32 });
        if (!document.RootElement.TryGetProperty("code", out var code) || code.GetString() != "ok" || !document.RootElement.TryGetProperty("data", out _)) { document.Dispose(); throw new InvalidDataException("Invalid Anissia response"); }
        return document;
    }

    public async Task<AnimeDetail> GetDetailAsync(int animeNo, CancellationToken ct)
    {
        if (animeNo <= 0) throw new ArgumentException("Invalid anime ID");
        using var doc = await GetAsync($"/anime/animeNo/{animeNo}", ct);
        var data = doc.RootElement.GetProperty("data");
        if (data.ValueKind != JsonValueKind.Object) throw new InvalidDataException("Invalid anime detail");
        var title = data.GetProperty("subject").GetString();
        if (string.IsNullOrWhiteSpace(title)) throw new InvalidDataException("Missing anime title");
        return new(animeNo, title, data.TryGetProperty("originalSubject", out var native) ? native.GetString() ?? "" : "");
    }

    public async Task<Caption[]> GetCaptionsAsync(int animeNo, CancellationToken ct)
    {
        using var doc = await GetAsync($"/anime/caption/animeNo/{animeNo}", ct);
        var data = doc.RootElement.GetProperty("data");
        if (data.ValueKind != JsonValueKind.Array) throw new InvalidDataException("Invalid caption list");
        return data.EnumerateArray().Select(item => new Caption(item.GetProperty("episode").ToString(), item.GetProperty("website").GetString() ?? "", item.GetProperty("name").GetString() ?? "", item.GetProperty("updDt").GetString() ?? "")).Where(c => Uri.TryCreate(c.Website, UriKind.Absolute, out _)).OrderByDescending(c => c.UpdatedAt, StringComparer.Ordinal).ThenBy(c => c.Name, StringComparer.Ordinal).ThenBy(c => c.Website, StringComparer.Ordinal).ToArray();
    }

    public async Task<AnimeDetail[]> SearchAsync(string query, CancellationToken ct)
    {
        using var doc = await GetAsync("/anime/autocorrect?q=" + Uri.EscapeDataString(query), ct);
        var data = doc.RootElement.GetProperty("data");
        if (data.ValueKind != JsonValueKind.Array) throw new InvalidDataException("Invalid title candidates");
        var candidates = new Dictionary<int, AnimeDetail>();
        foreach (var item in data.EnumerateArray())
        {
            var raw = item.GetString() ?? ""; var split = raw.IndexOf(' ');
            if (split > 0 && int.TryParse(raw[..split], out var id) && id > 0) candidates[id] = new(id, raw[(split + 1)..], "");
        }
        if (candidates.Count > 0) return candidates.Values.ToArray();
        return await SearchAllAsync(query, ct);
    }

    public async Task<AnimeDetail[]> SearchAllAsync(string query, CancellationToken ct)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 256) throw new ArgumentException("Invalid search query");
        var candidates = new Dictionary<int, AnimeDetail>();
        for (var page = 0; page < 100; page++)
        {
            using var listDoc = await GetAsync($"/anime/list/{page}?q=" + Uri.EscapeDataString(query), ct);
            var list = listDoc.RootElement.GetProperty("data");
            var content = list.GetProperty("content");
            var previousCount = candidates.Count;
            foreach (var item in content.EnumerateArray())
            {
                var id = item.GetProperty("animeNo").GetInt32();
                var subject = item.GetProperty("subject").GetString();
                if (id < 1 || string.IsNullOrWhiteSpace(subject)) throw new InvalidDataException("Invalid anime search entry");
                candidates[id] = new(id, subject, item.TryGetProperty("originalSubject", out var native) ? native.GetString() ?? "" : "");
            }
            if (list.TryGetProperty("last", out var last) && last.GetBoolean()) return candidates.Values.ToArray();
            if (list.TryGetProperty("totalPages", out var totalPages) && totalPages.GetInt32() <= page + 1) return candidates.Values.ToArray();
            if (content.GetArrayLength() == 0 || candidates.Count == previousCount) throw new CollectorException(ResultStatus.SearchIncomplete, "Anissia pagination did not advance");
        }
        throw new CollectorException(ResultStatus.SearchIncomplete, "Anissia search page limit reached");
    }
}
