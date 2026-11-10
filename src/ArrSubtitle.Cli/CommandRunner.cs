using System.Text.Json;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Serialization;

namespace ArrSubtitle.Cli;

public static class CommandRunner
{
    public static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter error, CancellationToken ct)
    {
        if (args.Length == 0 || args.Contains("--help") || args.Contains("-h"))
        {
            await output.WriteLineAsync("""
                arr-subtitle
                  search <query>
                  match <video-path>
                  download <video-path>
                  scan <directory>
                  collect <title> --episodes 1-5 --output <directory>
                  collect --anime-id 3433 --episodes 1-5 --output <directory>
                  blogs add <url> | blogs list | blogs remove <url>
                Options: --config <path>, --json, --season <number>, --anime-id <number>
                Exit codes: 0 success, 1 partial or source failure, 2 invalid request, 130 cancelled
                """);
            return 0;
        }
        if (args.Length == 1 && args[0] == "--version") { await output.WriteLineAsync("0.1.0"); return 0; }
        var json = args.Contains("--json");
        try
        {
            var options = new Dictionary<string, string>();
            var positional = new List<string>();
            for (var i = 0; i < args.Length; i++)
            {
                if (args[i] == "--json") continue;
                if (!args[i].StartsWith("--", StringComparison.Ordinal)) { positional.Add(args[i]); continue; }
                var key = args[i][2..];
                if (!new[] { "config", "anime-id", "episodes", "output", "season" }.Contains(key) || i + 1 >= args.Length || args[i + 1].StartsWith("--", StringComparison.Ordinal) || !options.TryAdd(key, args[++i])) throw new ArgumentException("Invalid or duplicate option");
            }
            var configPath = options.GetValueOrDefault("config") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "arr-subtitle", "config.json");
            var loaded = await SettingsStore.LoadAsync(configPath, ct);
            if (options.ContainsKey("config") && loaded.Hash == "missing") throw new ArgumentException("Explicit settings file does not exist");
            using var session = new RequestSession(lifetime: ct);
            if (positional.Count == 0) throw new ArgumentException("Command required");
            var command = positional[0];
            if (command == "search")
            {
                if (positional.Count != 2 || options.Keys.Any(k => k != "config")) throw new ArgumentException("search requires one query and accepts only config and json options");
                var report = await new SubtitleSearch(session, loaded.Settings).SearchAsync(positional[1], ct);
                if (json) await output.WriteLineAsync(JsonSerializer.Serialize(report, CollectorJsonContext.Default.SubtitleSearchReport));
                else
                {
                    foreach (var anime in report.Anime)
                    {
                        await output.WriteLineAsync($"{anime.Anime.AnimeId}: {anime.Anime.Title}");
                        foreach (var subtitle in anime.Subtitles) await output.WriteLineAsync($"  {subtitle.Episode}: {subtitle.Maker} | {subtitle.Page} | {string.Join(", ", subtitle.Attachments)}");
                        if (anime.MissingEpisodes.Length > 0) await output.WriteLineAsync($"  Missing episodes: {string.Join(", ", anime.MissingEpisodes)}");
                        if (!anime.Complete) await output.WriteLineAsync("  Search incomplete");
                        foreach (var attempt in anime.Attempts.Where(a => a.Error is not null)) await error.WriteLineAsync($"{attempt.Source}: {attempt.Error}");
                    }
                    foreach (var attempt in report.Attempts) await error.WriteLineAsync($"{attempt.Source}: {attempt.Error ?? attempt.Status}");
                    if (report.Anime.Length == 0) await output.WriteLineAsync("No matching subtitles found");
                }
                return ct.IsCancellationRequested ? 130 : report.Success ? 0 : 1;
            }
            if (command == "blogs")
            {
                if (positional.Count == 2 && positional[1] == "list")
                {
                    await output.WriteLineAsync(JsonSerializer.Serialize(loaded.Settings.Blogs, CollectorJsonContext.Default.BlogDescriptorArray)); return 0;
                }
                if (positional.Count != 3 || positional[1] is not ("add" or "remove")) throw new ArgumentException("Invalid blogs command");
                var descriptor = positional[1] == "remove" ? BlogDetector.Known(new Uri(positional[2])) ?? loaded.Settings.Blogs.FirstOrDefault(b => b.Url == positional[2]) ?? throw new ArgumentException("Blog not registered") : await new BlogDetector(session).DetectAsync(new Uri(positional[2]), ct);
                var blogs = loaded.Settings.Blogs.Where(b => b.Id != descriptor.Id || b.Platform != descriptor.Platform).ToList();
                if (positional[1] == "add") blogs.Add(descriptor);
                await SettingsStore.UpdateAsync(configPath, loaded.Settings with { Blogs = blogs.ToArray() }, loaded.Hash, ct);
                await output.WriteLineAsync(JsonSerializer.Serialize(descriptor, CollectorJsonContext.Default.BlogDescriptor));
                return 0;
            }
            int? animeId = options.TryGetValue("anime-id", out var rawId) ? int.Parse(rawId, System.Globalization.CultureInfo.InvariantCulture) : null;
            int? season = options.TryGetValue("season", out var rawSeason) ? int.Parse(rawSeason, System.Globalization.CultureInfo.InvariantCulture) : null;
            if (animeId is <= 0 || season is <= 0) throw new ArgumentException("IDs and seasons must be positive");
            var requests = new List<CollectionRequest>();
            var failures = new List<EpisodeResult>();
            var outputDirectory = options.GetValueOrDefault("output") ?? "";
            if (command == "collect")
            {
                if (positional.Count > 2 || positional.Count == 1 && !animeId.HasValue || !options.TryGetValue("episodes", out var range) || string.IsNullOrWhiteSpace(outputDirectory)) throw new ArgumentException("collect requires title or anime ID, episodes and output");
                requests.Add(new(new(positional.ElementAtOrDefault(1), animeId, season), ParseEpisodes(range).Select(e => new EpisodeTarget(e)).ToArray(), Path.GetFullPath(outputDirectory)));
            }
            else if (command is "match" or "download" or "scan")
            {
                if (positional.Count != 2) throw new ArgumentException("Media path required");
                var media = command == "scan" ? EnumerateMedia(positional[1]).ToArray() : [positional[1]];
                if (media.Length == 0) throw new ArgumentException("No supported videos found");
                var parsed = new List<(AnimeQuery Query, EpisodeTarget Target)>();
                foreach (var path in media)
                {
                    try
                    {
                        var identity = MediaPathParser.Parse(path);
                        if (season.HasValue && identity.Query.Season.HasValue && season != identity.Query.Season) throw new CollectorException(ResultStatus.AmbiguousMatch, "Explicit season conflicts with path");
                        parsed.Add((identity.Query with { AnimeId = animeId, Season = season ?? identity.Query.Season }, new(identity.Episode, Path.GetFullPath(path))));
                    }
                    catch (Exception ex) when (ex is not OperationCanceledException) { failures.Add(new(path, ResultStatus.AmbiguousMatch, Error: ex.Message)); }
                }
                foreach (var group in parsed.GroupBy(p => p.Query with { MediaPath = null }))
                {
                    var unique = new HashSet<string>();
                    var batch = new List<EpisodeTarget>();
                    foreach (var item in group)
                    {
                        if (unique.Add(item.Target.Episode.Value)) batch.Add(item.Target);
                        else requests.Add(new(group.Key, [item.Target], outputDirectory, command == "match"));
                    }
                    requests.Add(new(group.First().Query, batch.ToArray(), outputDirectory, command == "match"));
                }
            }
            else throw new ArgumentException("Unknown command");
            var collector = new SubtitleCollector(session, loaded.Settings);
            var reports = new List<CollectionReport>();
            foreach (var request in requests)
            {
                var report = request.SearchOnly ? await collector.SearchAsync(request, ct) : await collector.CollectAsync(request, ct);
                reports.Add(report);
                if (ct.IsCancellationRequested) break;
            }
            var combined = new CollectionReport(Guid.NewGuid().ToString("N"), reports.Count == 1 ? reports[0].Anime : null, reports.SelectMany(r => r.Episodes).Concat(failures).ToArray(), reports.SelectMany(r => r.Attempts).ToArray(), session.RequestCounts, session.ReceivedBytes, reports.Sum(r => r.ElapsedMilliseconds), reports.FirstOrDefault()?.FirstCandidateMilliseconds);
            if (command != "match")
            {
                var manifestRoot = command == "collect" ? Path.GetFullPath(outputDirectory) : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "arr-subtitle", "runs");
                var manifest = Path.Combine(manifestRoot, "run-" + combined.RequestId + ".json");
                combined = combined with { ManifestPath = manifest };
                await AtomicFile.WriteAsync(manifest, JsonSerializer.SerializeToUtf8Bytes(combined, CollectorJsonContext.Default.CollectionReport), false, CancellationToken.None);
            }
            if (json) await output.WriteLineAsync(JsonSerializer.Serialize(combined, CollectorJsonContext.Default.CollectionReport));
            else
            {
                foreach (var result in combined.Episodes) await output.WriteLineAsync($"{result.Episode}: {result.Status} {result.Path ?? result.Page ?? result.Error}");
                if (combined.ManifestPath is not null) await output.WriteLineAsync($"Manifest: {combined.ManifestPath}");
            }
            return ct.IsCancellationRequested ? 130 : combined.Success ? 0 : 1;
        }
        catch (OperationCanceledException) { await error.WriteLineAsync("Cancelled"); return 130; }
        catch (Exception ex)
        {
            await error.WriteLineAsync($"Error: {ex.Message}");
            return ex is ArgumentException or FormatException or JsonException or InvalidDataException ? 2 : 1;
        }
    }

    public static EpisodeNumber[] ParseEpisodes(string value)
    {
        var episodes = new List<EpisodeNumber>();
        foreach (var part in value.Split(','))
        {
            var bounds = part.Split('-');
            if (bounds.Length == 1) episodes.Add(EpisodeNumber.Parse(part));
            else if (bounds.Length == 2 && int.TryParse(bounds[0], out var start) && int.TryParse(bounds[1], out var end) && start >= 0 && end >= start && end - start < 1000)
                for (var n = start; n <= end; n++) episodes.Add(EpisodeNumber.Parse(n.ToString(System.Globalization.CultureInfo.InvariantCulture)));
            else throw new ArgumentException("Invalid episode range");
        }
        if (episodes.Count is 0 or > 1000) throw new ArgumentException("Episode count exceeds limit");
        return episodes.Distinct().ToArray();
    }

    private static IEnumerable<string> EnumerateMedia(string directory)
    {
        if (!Directory.Exists(directory)) throw new ArgumentException("Media directory does not exist");
        var options = new EnumerationOptions { RecurseSubdirectories = true, AttributesToSkip = FileAttributes.ReparsePoint, IgnoreInaccessible = false };
        return Directory.EnumerateFiles(directory, "*", options).Where(p => new[] { ".mkv", ".mp4", ".avi" }.Contains(Path.GetExtension(p).ToLowerInvariant())).Order(StringComparer.Ordinal);
    }
}
