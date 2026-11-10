using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.RegularExpressions;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

public sealed class SubtitleSearch(RequestSession session, CollectorSettings settings)
{
    public async Task<SubtitleSearchReport> SearchAsync(string query, CancellationToken ct, Action<SearchProgress>? progress = null, TimeSpan? timeout = null)
    {
        if (string.IsNullOrWhiteSpace(query) || query.Length > 256) throw new ArgumentException("Invalid search query");
        var limit = timeout ?? TimeSpan.FromSeconds(60);
        if (limit < TimeSpan.FromSeconds(1) || limit > TimeSpan.FromMinutes(10)) throw new ArgumentException("Search timeout must be between 1 and 600 seconds");
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(limit);
        var token = budget.Token;
        var timer = Stopwatch.StartNew();
        var contexts = new List<AnimeContext>();
        var attempts = new ConcurrentQueue<SourceAttempt>();
        var sources = new ConcurrentDictionary<(BlogPlatform Platform, string Id), SourceContext>();
        var complete = true;
        try
        {
            progress?.Invoke(new("search", query));
            var api = new AnissiaClient(session);
            var candidates = await api.SearchAllAsync(query, token);
            if (candidates.Length == 0 && settings.AniListEnabled)
            {
                var metadata = await new AniListClient(session).SearchAsync(query, token);
                var identified = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, query))).ToArray();
                if (identified.Length == 1)
                {
                    var native = identified[0].Titles.FirstOrDefault(t => t.Any(c => c is >= '\u3040' and <= '\u9fff'));
                    if (native is not null) candidates = await api.SearchAllAsync(native, token);
                }
            }
            contexts.AddRange(candidates.Select(candidate => new AnimeContext(new(candidate.AnimeId, candidate.Title, candidate.OriginalTitle, [candidate.Title, candidate.OriginalTitle], "Anissia search"))));
            var options = new ParallelOptions { MaxDegreeOfParallelism = 4, CancellationToken = token };
            await Parallel.ForEachAsync(contexts, options, async (context, workToken) =>
            {
                try
                {
                    progress?.Invoke(new("anime", context.Anime.Title));
                    var detail = candidates.Single(c => c.AnimeId == context.Anime.AnimeId);
                    if (string.IsNullOrWhiteSpace(detail.OriginalTitle)) detail = await api.GetDetailAsync(detail.AnimeId, workToken);
                    var aliases = settings.Mappings.Where(m => m.AnimeId == detail.AnimeId).SelectMany(m => m.Aliases);
                    context.Anime = new(detail.AnimeId, detail.Title, detail.OriginalTitle, new[] { detail.Title, detail.OriginalTitle }.Concat(aliases).Where(t => !string.IsNullOrWhiteSpace(t)).Distinct().ToArray(), "Anissia search");
                    var explicitMappings = settings.Mappings.Where(m => m.AnimeId == detail.AnimeId && m.LibraryPath is null).ToArray();
                    if (explicitMappings.Length > 1) throw new CollectorException(ResultStatus.AmbiguousMatch, "Multiple explicit mappings for search anime");
                    context.ExplicitMapping = explicitMappings.SingleOrDefault();
                    if (context.ExplicitMapping is not null) context.Anime = context.Anime with { Season = context.ExplicitMapping.Season };
                    var captions = await api.GetCaptionsAsync(detail.AnimeId, workToken);
                    context.CaptionMaximum = captions.Where(c => EpisodeNumber.TryParse(c.Episode, out _)).Select(c => decimal.Parse(EpisodeNumber.Parse(c.Episode).Value, CultureInfo.InvariantCulture)).DefaultIfEmpty(0).Max();
                    foreach (var caption in captions)
                    {
                        try
                        {
                            var blog = await new BlogDetector(session).DetectAsync(new(caption.Website), workToken);
                            var source = sources.GetOrAdd((blog.Platform, blog.Id), _ => new(blog));
                            source.Anime.TryAdd(detail.AnimeId, (context, caption.Name));
                            var post = await new BlogExplorer(session).ReadPostAsync(caption.Website, workToken, blog);
                            var specificCategory = IsSpecificCategory(post.CategoryUrl);
                            if (specificCategory) source.Categories.GetOrAdd(detail.AnimeId, _ => new()).TryAdd(post.CategoryUrl!, true);
                            var categoryAlias = CategoryAlias(post.CategoryUrl);
                            if (specificCategory && categoryAlias is not null)
                            {
                                var seedAliases = AttachmentExtractor.Extract(post).Select(a => Regex.Match(a.Filename, @"^([A-Za-z]{2,8})\s+\d", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))).Where(m => m.Success).Select(m => m.Groups[1].Value).ToArray();
                                var aliasSet = context.CategoryAliases.GetOrAdd(post.CategoryUrl!, _ => new());
                                foreach (var alias in seedAliases) aliasSet.TryAdd(alias, true);
                            }
                            var knownSeason = TitleMatcher.ExplicitSeason(context.Anime.Title) ?? TitleMatcher.ExplicitSeason(context.Anime.OriginalTitle);
                            var postSeason = TitleMatcher.ExplicitSeason(post.Title);
                            if (knownSeason.HasValue && postSeason.HasValue && knownSeason != postSeason)
                            {
                                context.Fail(new(DestinationPolicy.Redact(post.Url), "season-conflict", "Caption season conflicts with anime title"));
                                continue;
                            }
                            context.Anime = context.Anime with { Season = knownSeason ?? context.Anime.Season ?? postSeason };
                            var sourceKey = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
                            var numbering = context.ExplicitMapping is not null ? new EpisodeNumbering(context.ExplicitMapping.EpisodeOffset, context.CaptionMaximum, "explicit configured episode offset") : EpisodeNumbering.FromCaption(caption, post, context.Anime.Season, context.CaptionMaximum);
                            if (numbering is not null)
                            {
                                if (!context.Numbering.Register(sourceKey, numbering))
                                {
                                    foreach (var listing in context.ListingSources.Where(p => p.Value == sourceKey).Select(p => p.Key).ToArray()) context.Listings.TryRemove(listing, out _);
                                    context.Fail(new(DestinationPolicy.Redact(post.Url), "numbering-conflict", "Conflicting caption episode offsets for source"));
                                    continue;
                                }
                            }
                            var namedEpisodes = ReadNamedEpisodes(context.Anime.Title);
                            var labels = post.Labels.Where(t => t.Length >= 4 && (TitleMatcher.Equal(t, context.Anime.Title) || namedEpisodes.Length == 0 && context.Anime.Title.StartsWith(t + " ", StringComparison.Ordinal)) && TitleMatcher.ExplicitSeason(t) == TitleMatcher.ExplicitSeason(context.Anime.Title));
                            context.Anime = context.Anime with { Titles = context.Anime.Titles.Concat(labels).Distinct().ToArray() };
                            await AddPostAsync(context, post, blog, caption.Name, EpisodeNumber.TryParse(caption.Episode, out var ep) ? [ep] : [], workToken, progress);
                        }
                        catch (Exception ex) when (ex is not OperationCanceledException) { context.Fail(new(DestinationPolicy.Redact(caption.Website), "post-failed", Error(ex))); }
                    }
                }
                catch (Exception ex) when (ex is not OperationCanceledException) { context.Fail(new($"anime:{context.Anime.AnimeId}", "failed", Error(ex))); }
            });
            foreach (var blog in settings.Blogs)
            {
                var source = sources.GetOrAdd((blog.Platform, blog.Id), _ => new(blog));
                foreach (var context in contexts) source.Anime.TryAdd(context.Anime.AnimeId, (context, blog.Id));
            }
            var orderedSources = sources.Values.OrderBy(s => s.Blog.Platform == BlogPlatform.Tistory && s.Categories.Count > 0 ? 0 : s.Blog.Platform == BlogPlatform.Blogspot ? 1 : 2).ThenBy(s => s.Blog.Url, StringComparer.Ordinal).ToArray();
            await Parallel.ForEachAsync(orderedSources, options, async (source, workToken) =>
            {
                using var sourceBudget = CancellationTokenSource.CreateLinkedTokenSource(workToken);
                var waves = Math.Max(1, (sources.Count + 3) / 4);
                var remaining = limit - timer.Elapsed - TimeSpan.FromSeconds(1);
                var sourceSeconds = Math.Max(1, Math.Min(20, remaining.TotalSeconds / waves));
                if (source.Blog.Platform == BlogPlatform.Tistory && source.Categories.Count > 0) sourceSeconds = Math.Max(1, Math.Min(40, remaining.TotalSeconds - 1));
                sourceBudget.CancelAfter(TimeSpan.FromSeconds(sourceSeconds));
                var sourceToken = sourceBudget.Token;
                source.Started = true;
                progress?.Invoke(new("source", DestinationPolicy.Redact(source.Blog.Url)));
                var members = source.Anime.Values.OrderBy(a => a.Context.Anime.AnimeId).ToArray();
                var anime = members[0].Context.Anime;
                var titles = members.SelectMany(m => m.Context.Anime.Titles).Distinct().ToArray();
                var explorer = new BlogExplorer(session);
                try
                {
                    var categories = source.Categories.Values.SelectMany(v => v.Keys).Distinct().ToArray();
                    var categoryOnly = categories.Length > 0 && members.All(m => source.Categories.ContainsKey(m.Context.Anime.AnimeId));
                    await foreach (var post in explorer.ExploreAsync(source.Blog, anime with { Titles = titles }, sourceToken, exhaustive: true, searchTitles: members.Select(m => m.Context.Anime.Title).Distinct().ToArray(), categoryUrls: categories, categoryOnly: categoryOnly))
                    {
                        foreach (var member in members)
                        {
                            var categoryMatch = source.Categories.TryGetValue(member.Context.Anime.AnimeId, out var memberCategories) && post.CategoryUrl is not null && memberCategories.ContainsKey(post.CategoryUrl);
                            if (!categoryMatch && !TitleMatcher.Mentions(post.Title + " " + string.Join(' ', post.Labels), member.Context.Anime.Titles)) continue;
                            try { await AddPostAsync(member.Context, post, source.Blog, member.Maker, [], sourceToken, progress); }
                            catch (Exception ex) when (ex is not OperationCanceledException) { member.Context.Fail(new(DestinationPolicy.Redact(post.Url), "post-failed", Error(ex))); }
                        }
                    }
                    source.Finished = true;
                    foreach (var member in members)
                    {
                        if (explorer.Complete) member.Context.Attempts.Enqueue(new(DestinationPolicy.Redact(source.Blog.Url), "complete"));
                        else member.Context.Fail(new(DestinationPolicy.Redact(source.Blog.Url), "incomplete"));
                    }
                    progress?.Invoke(new("source-finished", DestinationPolicy.Redact(source.Blog.Url), Detail: explorer.Complete ? "complete" : "incomplete"));
                }
                catch (OperationCanceledException) when (!workToken.IsCancellationRequested)
                {
                    source.Finished = true;
                    foreach (var member in members) member.Context.Fail(new(DestinationPolicy.Redact(source.Blog.Url), "timeout"));
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    source.Finished = true;
                    foreach (var member in members) member.Context.Fail(new(DestinationPolicy.Redact(source.Blog.Url), "failed", Error(ex)));
                }
            });
        }
        catch (OperationCanceledException)
        {
            complete = false;
            var status = ct.IsCancellationRequested ? "cancelled" : "timeout";
            attempts.Enqueue(new("search", status));
            foreach (var context in contexts) context.Fail(new("search", status));
            foreach (var source in sources.Values.Where(s => !s.Finished))
                foreach (var member in source.Anime.Values) member.Context.Fail(new(DestinationPolicy.Redact(source.Blog.Url), source.Started ? status : "not-started"));
            progress?.Invoke(new(status, query, Detail: "Partial results preserved"));
        }
        catch (Exception ex) { complete = false; attempts.Enqueue(new("search", "failed", Error(ex))); }
        var results = contexts.Select(BuildResult).OrderBy(r => r.Anime.AnimeId).ToArray();
        return new(query, results, complete && results.All(r => r.Complete), attempts.ToArray(), session.RequestCounts, timer.Elapsed.TotalMilliseconds);
    }

    private async Task AddPostAsync(AnimeContext context, PostCandidate post, BlogDescriptor blog, string maker, EpisodeNumber[] authoritative, CancellationToken ct, Action<SearchProgress>? progress)
    {
        if (post.Html.Length == 0 || post.IsSummary) post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
        var attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
        if (attachments.Length == 0)
        {
            post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
            attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
        }
        var anime = context.Anime;
        var numberingKey = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
        if (context.Numbering.HasConflict(numberingKey)) { context.Fail(new(DestinationPolicy.Redact(post.Url), "numbering-conflict")); return; }
        var numbering = context.ExplicitMapping is not null ? new EpisodeNumbering(context.ExplicitMapping.EpisodeOffset, context.CaptionMaximum, "explicit configured episode offset") : context.Numbering.Get(numberingKey);
        if (post.CategoryUrl is not null && context.CategoryAliases.TryGetValue(post.CategoryUrl, out var localAliases)) anime = anime with { Titles = anime.Titles.Concat(localAliases.Keys).Distinct().ToArray() };
        var season = TitleMatcher.ExplicitSeason(post.Title);
        var expected = anime.Season ?? TitleMatcher.ExplicitSeason(anime.Title) ?? TitleMatcher.ExplicitSeason(anime.OriginalTitle);
        if (season.HasValue && season != expected) return;
        var episodes = ReadEpisodes(post.Title);
        if (episodes.Length == 0) episodes = authoritative;
        if (episodes.Length == 0) episodes = attachments.SelectMany(a => ReadEpisodes(a.Filename)).Distinct().ToArray();
        var batch = Regex.IsMatch(post.Title + " " + string.Join(' ', attachments.Select(a => a.Filename)), @"전체|전\s*화|이전\s*화\s*포함|(?<!\d)\d+\s*[-~]\s*\d+", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        var archiveListings = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        if (batch)
        {
            foreach (var attachment in attachments)
            {
                var file = await session.GetAsync(attachment.Url, ct, 100 * 1024 * 1024);
                if (!file.Bytes.AsSpan().StartsWith(new byte[] { 0x50, 0x4b, 0x03, 0x04 })) { context.Fail(new(DestinationPolicy.Redact(post.Url), "archive-invalid", "Batch attachment is not a ZIP archive")); continue; }
                Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
                using var stream = new MemoryStream(file.Bytes, false);
                using var archive = new ZipArchive(stream, ZipArchiveMode.Read, false, Encoding.GetEncoding(949));
                if (archive.Entries.Count > 500 || archive.Entries.Sum(e => e.Length) > 100L * 1024 * 1024) throw new InvalidDataException("Search archive exceeds limits");
                foreach (var entry in archive.Entries)
                {
                    var name = entry.FullName.Replace('\\', '/');
                    if (name.StartsWith('/') || name.Contains(':') || name.Split('/').Contains("..") || (entry.ExternalAttributes >> 16 & 0xf000) == 0xa000) throw new InvalidDataException("Unsafe search archive member");
                    if (!new[] { ".smi", ".srt", ".ass", ".ssa" }.Contains(Path.GetExtension(entry.Name).ToLowerInvariant()) || EpisodeEvidence.IsAuxiliary(entry.Name)) continue;
                    var numbers = ReadEpisodes(entry.Name);
                    foreach (var number in numbers)
                    {
                        try { ArchiveSelector.ValidateIdentity(entry.FullName, anime with { Season = expected }, number); }
                        catch (CollectorException)
                        {
                            if (!context.AliasesChecked && settings.AniListEnabled && anime.OriginalTitle.Length > 0)
                            {
                                context.AliasesChecked = true;
                                var metadata = await new AniListClient(session).SearchAsync(anime.OriginalTitle, ct);
                                var verified = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, anime.OriginalTitle))).ToArray();
                                if (verified.Length == 1) context.Anime = anime = anime with { Titles = anime.Titles.Concat(verified[0].Titles).Distinct().ToArray() };
                            }
                            try { ArchiveSelector.ValidateIdentity(entry.FullName, anime with { Season = expected }, number); }
                            catch (CollectorException) { context.Fail(new(DestinationPolicy.Redact(post.Url), "archive-identity-conflict", "ZIP member identity is not verified")); continue; }
                        }
                        if (!archiveListings.TryGetValue(number.Value, out var files)) archiveListings[number.Value] = files = [];
                        files.Add(entry.FullName);
                    }
                }
            }
            if (archiveListings.Count > 0)
            {
                var captionMaximum = context.CaptionMaximum;
                var outOfRange = expected is > 1 && captionMaximum > 0 && archiveListings.Keys.Any(e => decimal.Parse(e, CultureInfo.InvariantCulture) > captionMaximum);
                if (outOfRange && numbering is null)
                {
                    context.Fail(new(DestinationPolicy.Redact(post.Url), "numbering-ambiguous", "Batch archive uses unverified absolute episode numbering; explicit mapping required"));
                    archiveListings.Clear();
                    return;
                }
                else episodes = archiveListings.Keys.Select(EpisodeNumber.Parse).ToArray();
            }
            else { context.Fail(new(DestinationPolicy.Redact(post.Url), "archive-incomplete", "Batch subtitle episodes could not be inspected")); return; }
        }
        var explicitAnimeEpisodes = ReadNamedEpisodes(anime.Title);
        if (explicitAnimeEpisodes.Length > 0) episodes = episodes.Where(explicitAnimeEpisodes.Contains).ToArray();
        if (numbering is null && expected is > 1 && context.CaptionMaximum > 0 && episodes.Any(e => decimal.Parse(e.Value, CultureInfo.InvariantCulture) > context.CaptionMaximum))
        {
            context.Fail(new(DestinationPolicy.Redact(post.Url), "numbering-ambiguous", "Post uses unverified absolute episode numbering; explicit mapping required"));
            return;
        }
        if (episodes.Length == 0 || attachments.Length == 0)
        {
            context.Fail(new(DestinationPolicy.Redact(post.Url), "ambiguous", "Post has no verified episode or attachment")); return;
        }
        foreach (var episode in episodes)
        {
            var seasonEpisode = numbering?.ToSeason(episode) ?? (numbering is null ? episode : (EpisodeNumber?)null);
            if (seasonEpisode is null) continue;
            var matching = archiveListings.TryGetValue(episode.Value, out var archived) ? archived.Distinct().Order(StringComparer.Ordinal).ToArray() : attachments.Where(a => MatchesAttachment(a.Filename, episode)).Select(a => a.Filename).Distinct().Order(StringComparer.Ordinal).ToArray();
            if (matching.Length == 0) { context.Fail(new(DestinationPolicy.Redact(post.Url), "attachment-conflict")); continue; }
            var page = DestinationPolicy.Redact(post.Url);
            if (context.Listings.TryAdd((seasonEpisode.Value.Value, page), new(seasonEpisode.Value.Value, maker, page, matching, numbering is null ? null : episode.Value, numbering?.Evidence)))
            {
                context.ListingSources[(seasonEpisode.Value.Value, page)] = numberingKey;
                progress?.Invoke(new("found", anime.Title, seasonEpisode.Value.Value, page));
            }
        }

        bool MatchesAttachment(string filename, EpisodeNumber episode)
        {
            var namedEpisodes = ReadEpisodes(filename);
            if (namedEpisodes.Length > 0 && !namedEpisodes.Contains(episode)) return false;
            var representative = namedEpisodes.FirstOrDefault();
            var normalized = Regex.Replace(filename, @"(?<!\d)\d{1,4}\s*[-~]\s*\d{1,4}", representative.Value ?? episode.Value, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
            try { ArchiveSelector.ValidateIdentity(normalized, anime with { Season = expected }, representative.Value is not null ? representative : episode); return true; }
            catch (CollectorException) { context.Fail(new(DestinationPolicy.Redact(post.Url), "attachment-conflict", "Attachment title, season or episode conflicts with anime")); return false; }
        }
    }

    private static AnimeSearchResult BuildResult(AnimeContext context)
    {
        var subtitles = context.Listings.Values.OrderBy(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).ThenBy(l => l.Maker, StringComparer.Ordinal).ThenBy(l => l.Page, StringComparer.Ordinal).ToArray();
        var integers = subtitles.Select(l => decimal.Parse(l.Episode, CultureInfo.InvariantCulture)).Where(n => n == decimal.Truncate(n) && n > 0).Select(n => (int)n).ToHashSet();
        var last = integers.DefaultIfEmpty(0).Max();
        if (last > 10000) { context.Fail(new("episodes", "incomplete", "Episode coverage limit reached")); last = 10000; }
        var missing = Enumerable.Range(1, last).Where(n => !integers.Contains(n)).Select(n => n.ToString(CultureInfo.InvariantCulture)).ToArray();
        return new(context.Anime, subtitles, missing, context.Complete, context.Attempts.OrderBy(a => a.Source, StringComparer.Ordinal).ThenBy(a => a.Status, StringComparer.Ordinal).ToArray());
    }

    private sealed class AnimeContext(ResolvedAnime anime)
    {
        public ResolvedAnime Anime { get; set; } = anime;
        public bool Complete = true;
        public decimal CaptionMaximum;
        public bool AliasesChecked;
        public ConcurrentDictionary<(string Episode, string Page), SubtitleListing> Listings { get; } = new();
        public ConcurrentDictionary<(string Episode, string Page), string> ListingSources { get; } = new();
        public ConcurrentDictionary<string, ConcurrentDictionary<string, bool>> CategoryAliases { get; } = new();
        public EpisodeNumberingRegistry Numbering { get; } = new();
        public AnimeMapping? ExplicitMapping;
        public ConcurrentQueue<SourceAttempt> Attempts { get; } = new();
        public void Fail(SourceAttempt attempt) { Complete = false; Attempts.Enqueue(attempt); }
    }

    private sealed class SourceContext(BlogDescriptor blog)
    {
        public BlogDescriptor Blog { get; } = blog;
        public ConcurrentDictionary<int, (AnimeContext Context, string Maker)> Anime { get; } = new();
        public ConcurrentDictionary<int, ConcurrentDictionary<string, bool>> Categories { get; } = new();
        public bool Started;
        public bool Finished;
    }

    internal static EpisodeNumber[] ReadEpisodes(string text)
    {
        var range = Regex.Match(text, @"(?<!\d)(\d{1,4})\s*[-~]\s*(\d{1,4})\s*(?:화|회|\b)", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (range.Success)
        {
            var first = int.Parse(range.Groups[1].Value, CultureInfo.InvariantCulture); var last = int.Parse(range.Groups[2].Value, CultureInfo.InvariantCulture);
            if (last < first || last - first > 1000) throw new InvalidDataException("Invalid episode range in post");
            return Enumerable.Range(first, last - first + 1).Select(n => EpisodeNumber.Parse(n.ToString(CultureInfo.InvariantCulture))).ToArray();
        }
        var matches = Regex.Matches(text, @"(?:제\s*)?(?<!\d)(\d{1,4}(?:\.\d+)?)\s*(?:화|회)(?![\p{L}])|\b(?:EP(?:ISODE)?\s*|S\d+E)(\d{1,4}(?:\.\d+)?)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (matches.Count > 0) return matches.Select(m => EpisodeNumber.Parse(m.Groups[1].Success ? m.Groups[1].Value : m.Groups[2].Value)).Distinct().ToArray();
        var variant = Regex.Match(text, @"(?<!\d)(\d{1,4})(?:ns|s)(?=\.[a-z]+$)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        if (variant.Success) return [EpisodeNumber.Parse(variant.Groups[1].Value)];
        var explicitEpisode = EpisodeEvidence.Explicit(text);
        return explicitEpisode.HasValue ? [explicitEpisode.Value] : [];
    }

    private static EpisodeNumber[] ReadNamedEpisodes(string text) => Regex.IsMatch(text, @"\d\s*(?:화|회)|\b(?:EP(?:ISODE)?\s*|S\d+E)\d", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)) ? ReadEpisodes(text) : [];

    private static string? CategoryAlias(string? url) => url is null ? null : Regex.Replace(Uri.UnescapeDataString(new Uri(url).AbsolutePath.Split('/').Last()), @"\d+$", "", RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
    private static bool IsSpecificCategory(string? url)
    {
        var leaf = CategoryAlias(url);
        return !string.IsNullOrWhiteSpace(leaf) && leaf.Length >= 2 && !new[] { "자막", "애니", "애니메이션", "완결", "방영 종료", "방영중", "subtitles", "anime", "etc", "기타" }.Contains(leaf, StringComparer.OrdinalIgnoreCase);
    }

    private static string Error(Exception ex) => ex is HttpRequestException ? "HTTP request failed" : ex.Message.Contains("http", StringComparison.OrdinalIgnoreCase) ? ex.GetType().Name : ex.Message;
}
