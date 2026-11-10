using System.Security.Cryptography;
using System.Text.Json;
using ArrSubtitle.Core.Serialization;

namespace ArrSubtitle.Core.Configuration;

public static class SettingsStore
{
    public static async Task<(CollectorSettings Settings, string Hash)> LoadAsync(string path, CancellationToken ct)
    {
        if (!File.Exists(path)) return (new(), "missing");
        if (new FileInfo(path).Length > 1024 * 1024) throw new InvalidDataException("Settings exceed size limit");
        var bytes = await File.ReadAllBytesAsync(path, ct);
        if (bytes.Length > 1024 * 1024) throw new InvalidDataException("Settings exceed size limit");
        var settings = JsonSerializer.Deserialize(bytes, CollectorJsonContext.Default.CollectorSettings) ?? throw new InvalidDataException("Settings cannot be null");
        Validate(settings);
        return (settings, Convert.ToHexString(SHA256.HashData(bytes)));
    }

    public static async Task UpdateAsync(string path, CollectorSettings settings, string expectedHash, CancellationToken ct)
    {
        Validate(settings);
        path = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        using var guard = new FileStream(path + ".lock", FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        var current = await LoadAsync(path, ct);
        if (current.Hash != expectedHash) throw new IOException("Settings changed concurrently");
        var bytes = JsonSerializer.SerializeToUtf8Bytes(settings, CollectorJsonContext.Readable.CollectorSettings);
        if (bytes.Length > 1024 * 1024) throw new InvalidDataException("Settings exceed size limit");
        await AtomicFile.WriteAsync(path, bytes, true, ct);
    }

    private static void Validate(CollectorSettings settings)
    {
        if (settings.Blogs is null || settings.Mappings is null) throw new InvalidDataException("Settings collections cannot be null");
        foreach (var blog in settings.Blogs)
        {
            if (blog is null || !Uri.TryCreate(blog.Url, UriKind.Absolute, out var uri) || uri.Scheme is not ("http" or "https") || !string.IsNullOrEmpty(uri.UserInfo) || string.IsNullOrWhiteSpace(blog.Id) || !Enum.IsDefined(blog.Platform)) throw new InvalidDataException("Invalid blog settings");
        }
        if (settings.Blogs.Select(b => (b.Platform, b.Id)).Distinct().Count() != settings.Blogs.Length) throw new InvalidDataException("Duplicate blog");
        foreach (var map in settings.Mappings)
            if (map is null || string.IsNullOrWhiteSpace(map.Title) || map.Season < 1 || map.AnimeId < 1 || map.Aliases is null || map.Aliases.Any(string.IsNullOrWhiteSpace)) throw new InvalidDataException("Invalid anime mapping");
        if (settings.Mappings.GroupBy(m => (m.Title.ToUpperInvariant(), m.Season, m.LibraryPath is null ? null : Path.GetFullPath(m.LibraryPath))).Any(g => g.Count() > 1)) throw new InvalidDataException("Overlapping anime mappings");
    }
}

public static class AtomicFile
{
    public static async Task WriteAsync(string path, byte[] bytes, bool overwrite, CancellationToken ct)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            await using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536, FileOptions.Asynchronous))
            {
                await file.WriteAsync(bytes, ct);
                file.Flush(true);
            }
            ct.ThrowIfCancellationRequested();
            File.Move(temporary, path, overwrite);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
