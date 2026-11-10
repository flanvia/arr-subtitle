using System.Diagnostics;
using AngleSharp.Html.Parser;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

public sealed class SubtitleCollector(RequestSession session, CollectorSettings settings)
{
    public Task<CollectionReport> SearchAsync(CollectionRequest request, CancellationToken ct) => RunAsync(request with { SearchOnly = true }, ct);
    public Task<CollectionReport> CollectAsync(CollectionRequest request, CancellationToken ct) => RunAsync(request with { SearchOnly = false }, ct);

    private async Task<CollectionReport> RunAsync(CollectionRequest request, CancellationToken ct)
    {
        var timer = Stopwatch.StartNew();
        var attempts = new List<SourceAttempt>();
        var results = new Dictionary<string, EpisodeResult>();
        var errors = new Dictionary<string, (ResultStatus Status, string Error)>();
        var incomplete = false; double? firstCandidate = null;
        ResolvedAnime? anime = null;
        var id = Guid.NewGuid().ToString("N");
        var targets = request.Episodes;
        var lookupEpisodes = new Dictionary<string, EpisodeNumber>();
        var attachmentAliasesChecked = false;
        var sourceNumbering = new EpisodeNumberingRegistry();
        var sourceAliases = new Dictionary<string, string[]>();
        Caption[] captions = [];
        decimal maximum = 0;
        AnimeMapping? explicitMapping = null;
        try
        {
            var resolver = new AnimeResolver(session, settings);
            anime = await resolver.ResolveAsync(request.Query, ct);
            if (targets.Length == 0 || targets.Select(t => t.Episode.Value).Distinct().Count() != targets.Length) throw new CollectorException(ResultStatus.AmbiguousMatch, "Request must contain unique episodes");
            explicitMapping = resolver.GetMapping(request.Query);
            if (resolver.GetMapping(request.Query) is null)
            foreach (var target in targets.Where(t => t.MediaPath is not null))
            {
                try { AnimeResolver.ValidateLocalIdentity(request.Query with { MediaPath = target.MediaPath }, anime); }
                catch (CollectorException ex) { results[target.Episode.Value] = new(target.Episode.Value, ex.Status, Error: ex.Message); }
            }
            lookupEpisodes = targets.ToDictionary(t => t.Episode.Value, t => t.Episode);
            if (!request.SearchOnly)
            foreach (var target in targets)
            {
                if (results.ContainsKey(target.Episode.Value)) continue;
                try { var existing = SubtitleStore.Existing(anime, target, request.OutputDirectory); if (existing is not null) results[target.Episode.Value] = existing; }
                catch (Exception ex) when (ex is not OperationCanceledException) { results[target.Episode.Value] = new(target.Episode.Value, ResultStatus.WriteFailed, Error: ex.Message); }
            }
            if (Pending().Length == 0) return Report();
            try { captions = await new AnissiaClient(session).GetCaptionsAsync(anime.AnimeId, ct); }
            catch (Exception ex) when (ex is not OperationCanceledException) { captions = []; incomplete = true; attempts.Add(new("Anissia captions", "failed", SafeError(ex))); }
            maximum = captions.Where(c => EpisodeNumber.TryParse(c.Episode, out _)).Select(c => decimal.Parse(EpisodeNumber.Parse(c.Episode).Value, System.Globalization.CultureInfo.InvariantCulture)).DefaultIfEmpty(0).Max();
            var explorer = new BlogExplorer(session);
            var detector = new BlogDetector(session);
            var prepared = new List<(Caption Caption, BlogDescriptor Blog, PostCandidate Post)>();
            foreach (var caption in captions)
            {
                try
                {
                    var blog = await detector.DetectAsync(new Uri(caption.Website), ct);
                    var post = await explorer.ReadPostAsync(caption.Website, ct, blog);
                    var key = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
                    var numbering = explicitMapping is not null ? new EpisodeNumbering(explicitMapping.EpisodeOffset, maximum, "explicit configured episode offset") : EpisodeNumbering.FromCaption(caption, post, anime.Season ?? TitleMatcher.ExplicitSeason(anime.Title), maximum);
                    if (numbering is not null && !sourceNumbering.Register(key, numbering)) attempts.Add(new(DestinationPolicy.Redact(caption.Website), "numbering-conflict"));
                    if (post.CategoryUrl is not null)
                    {
                        var shortNames = AttachmentExtractor.Extract(post).Select(a => System.Text.RegularExpressions.Regex.Match(a.Filename, @"^([A-Za-z]{2,8})\s+\d", System.Text.RegularExpressions.RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))).Where(m => m.Success).Select(m => m.Groups[1].Value).Distinct().ToArray();
                        sourceAliases[post.CategoryUrl] = shortNames;
                    }
                    prepared.Add((caption, blog, post));
                }
                catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(caption.Website), "seed-failed", SafeError(ex))); }
            }
            var seenDirect = new HashSet<string>();
            foreach (var seed in prepared)
            {
                var caption = seed.Caption;
                var wanted = Pending().Where(t => EpisodeNumber.TryParse(caption.Episode, out var number) && number == lookupEpisodes[t.Episode.Value]).ToArray();
                if (wanted.Length == 0 || !seenDirect.Add(caption.Website)) continue;
                try
                {
                    await ProcessPost(seed.Post, wanted, true, seed.Blog);
                }
                catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(caption.Website), "failed", SafeError(ex))); }
            }
            var blogs = new List<BlogDescriptor>();
            var seeds = new Dictionary<(BlogPlatform, string), string>();
            foreach (var caption in captions)
            {
                if (Pending().Length == 0) break;
                try
                {
                    var blog = await detector.DetectAsync(new Uri(caption.Website), ct);
                    if (!blogs.Any(b => b.Id == blog.Id && b.Platform == blog.Platform))
                    {
                        blogs.Add(blog);
                        seeds[(blog.Platform, blog.Id)] = caption.Website;
                    }
                }
                catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(caption.Website), "detection-failed", SafeError(ex))); }
            }
            foreach (var blog in blogs.Concat(settings.Blogs).DistinctBy(b => (b.Platform, b.Id)))
            {
                if (Pending().Length == 0) break;
                var blogExplorer = new BlogExplorer(session);
                try
                {
                    if (seeds.TryGetValue((blog.Platform, blog.Id), out var seedUrl))
                    {
                        try
                        {
                            var seed = await explorer.ReadPostAsync(seedUrl, ct, blog);
                            if (seed.CategoryUrl is not null)
                            {
                                var shortNames = AttachmentExtractor.Extract(seed).Select(a => System.Text.RegularExpressions.Regex.Match(a.Filename, @"^([A-Za-z]{2,8})\s+\d", System.Text.RegularExpressions.RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1))).Where(m => m.Success).Select(m => m.Groups[1].Value).Distinct().ToArray();
                                sourceAliases[seed.CategoryUrl] = shortNames;
                            }
                            var aliases = seed.Labels.Where(label => label.Length >= 4 && anime.Title.StartsWith(label + " ", StringComparison.Ordinal) && TitleMatcher.ExplicitSeason(label) == TitleMatcher.ExplicitSeason(anime.Title)).ToArray();
                            anime = anime with { Titles = anime.Titles.Concat(aliases).Distinct().ToArray() };
                            await ProcessPost(seed, Pending(), false, blog);
                        }
                        catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(seedUrl), "seed-failed", SafeError(ex))); }
                    }
                    if (Pending().Length == 0) break;
                    await foreach (var post in blogExplorer.ExploreAsync(blog, anime, ct))
                    {
                        try { await ProcessPost(post, Pending(), false, blog); }
                        catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(post.Url), "post-failed", SafeError(ex))); }
                        if (Pending().Length == 0) break;
                    }
                    if (Pending().Length > 0 && !blogExplorer.Complete) incomplete = true;
                    attempts.Add(new(DestinationPolicy.Redact(blog.Url), blogExplorer.Complete ? "complete" : Pending().Length == 0 ? "satisfied" : "incomplete"));
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(blog.Url), "timeout")); }
                catch (Exception ex) when (ex is not OperationCanceledException) { incomplete = true; attempts.Add(new(DestinationPolicy.Redact(blog.Url), "failed", SafeError(ex))); }
            }
        }
        catch (CollectorException ex)
        {
            foreach (var target in targets.Where(t => !results.ContainsKey(t.Episode.Value))) results[target.Episode.Value] = new(target.Episode.Value, ex.Status, Error: ex.Message);
        }
        catch (OperationCanceledException)
        {
            foreach (var target in targets.Where(t => !results.ContainsKey(t.Episode.Value))) results[target.Episode.Value] = new(target.Episode.Value, ResultStatus.SearchIncomplete, Error: "Cancelled");
        }
        catch (Exception ex)
        {
            incomplete = true; attempts.Add(new("request", "failed", SafeError(ex)));
        }
        foreach (var target in targets.Where(t => !results.ContainsKey(t.Episode.Value)))
        {
            errors.TryGetValue(target.Episode.Value, out var error);
            var status = incomplete ? ResultStatus.SearchIncomplete : error.Error is not null ? error.Status : ResultStatus.NotFound;
            results[target.Episode.Value] = new(target.Episode.Value, status, Error: error.Error ?? (incomplete ? "Permitted sources could not be fully checked" : "No matching subtitle in permitted sources"));
        }
        return Report();

        EpisodeTarget[] Pending() => targets.Where(t => !results.ContainsKey(t.Episode.Value)).ToArray();
        CollectionReport Report() => new(id, anime, targets.Select(t => results[t.Episode.Value]).ToArray(), attempts.ToArray(), session.RequestCounts, session.ReceivedBytes, timer.Elapsed.TotalMilliseconds, firstCandidate);

        async Task ProcessPost(PostCandidate post, EpisodeTarget[] wanted, bool authoritative, BlogDescriptor? blog = null)
        {
            if (anime is null) return;
            blog ??= await new BlogDetector(session).DetectAsync(new Uri(post.Url), ct);
            var numberingKey = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
            if (sourceNumbering.HasConflict(numberingKey)) throw new CollectorException(ResultStatus.AmbiguousMatch, "Conflicting caption episode offsets for source");
            var numbering = explicitMapping is not null ? new EpisodeNumbering(explicitMapping.EpisodeOffset, maximum, "explicit configured episode offset") : sourceNumbering.Get(numberingKey);
            var postAnime = anime;
            if (post.CategoryUrl is not null && sourceAliases.TryGetValue(post.CategoryUrl, out var localAliases)) postAnime = anime with { Titles = anime.Titles.Concat(localAliases).Distinct().ToArray() };
            if (!authoritative && numbering is null && !TitleMatcher.Mentions(post.Title + " " + string.Join(' ', post.Labels), anime.Titles)) return;
            var postSeason = TitleMatcher.ExplicitSeason(post.Title);
            if (postSeason.HasValue && request.Query.Season.HasValue && postSeason != request.Query.Season) return;
            var eligible = wanted.Where(t => numbering is not null || EpisodeEvidence.Matches(post.Title, lookupEpisodes[t.Episode.Value]) || authoritative || IsRange(post.Title, lookupEpisodes[t.Episode.Value])).ToArray();
            if (eligible.Length == 0) return;
            if (post.Html.Length == 0) post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
            var attachments = AttachmentExtractor.Extract(post);
            if (attachments.Length == 0)
            {
                var fullPost = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
                attachments = AttachmentExtractor.Extract(fullPost);
                post = fullPost;
            }
            foreach (var target in eligible)
            {
                var lookup = numbering?.ToSource(lookupEpisodes[target.Episode.Value]) ?? lookupEpisodes[target.Episode.Value];
                foreach (var attachment in attachments.OrderByDescending(a => EpisodeEvidence.Matches(a.Filename, lookup)))
                {
                    if (EpisodeEvidence.IsAuxiliary(attachment.Filename)) continue;
                    firstCandidate ??= timer.Elapsed.TotalMilliseconds;
                    if (request.SearchOnly) { results[target.Episode.Value] = new(target.Episode.Value, ResultStatus.Found, Page: DestinationPolicy.Redact(post.Url)); break; }
                    try
                    {
                        var file = await session.GetAsync(attachment.Url, ct, 100 * 1024 * 1024);
                        var filename = file.Filename ?? attachment.Filename;
                        if (!AttachmentExtractor.Extensions.Contains(Path.GetExtension(filename).ToLowerInvariant())) filename = attachment.Filename;
                        if (!AttachmentExtractor.Extensions.Contains(Path.GetExtension(filename).ToLowerInvariant()))
                        {
                            var detected = file.Bytes.AsSpan().StartsWith(new byte[] { 0x50, 0x4b, 0x03, 0x04 }) ? ".zip" : SubtitleValidator.DetectExtension(file.Bytes);
                            if (detected is null) throw new CollectorException(ResultStatus.InvalidSubtitle, "Cannot determine subtitle format");
                            filename = "attachment" + detected;
                        }
                        SubtitlePayload payload;
                        if (numbering is null) EpisodeNumbering.ValidateUnmappedArchive(file.Bytes, anime.Season ?? TitleMatcher.ExplicitSeason(anime.Title), maximum);
                        try { payload = ArchiveSelector.Select(file.Bytes, filename, postAnime, lookup, post); }
                        catch (CollectorException ex) when (ex.Message == "Attachment title is not a verified anime alias" && settings.AniListEnabled && !attachmentAliasesChecked)
                        {
                            attachmentAliasesChecked = true;
                            var metadata = await new AniListClient(session).SearchAsync(anime.OriginalTitle, ct);
                            var verified = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, anime.OriginalTitle) || TitleMatcher.Normalize(anime.OriginalTitle).StartsWith(TitleMatcher.Normalize(t) + " ", StringComparison.Ordinal))).ToArray();
                            if (verified.Length == 1) anime = anime with { Titles = anime.Titles.Concat(verified[0].Titles).Distinct().ToArray() };
                            payload = ArchiveSelector.Select(file.Bytes, filename, anime, lookup, post);
                        }
                        try { results[target.Episode.Value] = (await SubtitleStore.SaveAsync(payload, anime, target, request.OutputDirectory, DestinationPolicy.Redact(post.Url), ct)) with { SourceEpisode = numbering is null ? null : lookup.Value, NumberingEvidence = numbering?.Evidence }; }
                        catch (IOException ex) { results[target.Episode.Value] = new(target.Episode.Value, ResultStatus.WriteFailed, Error: ex.Message); }
                        break;
                    }
                    catch (CollectorException ex) { errors[target.Episode.Value] = (ex.Status, ex.Message); attempts.Add(new(DestinationPolicy.Redact(attachment.Url), ex.Status.ToString(), ex.Message)); }
                    catch (Exception ex) when (ex is not OperationCanceledException) { errors[target.Episode.Value] = (ResultStatus.SearchIncomplete, SafeError(ex)); attempts.Add(new(DestinationPolicy.Redact(attachment.Url), "download-failed", SafeError(ex))); }
                }
            }
        }
    }

    private static string SafeError(Exception ex) => ex is HttpRequestException ? "HTTP request failed" : ex.Message.Contains("http", StringComparison.OrdinalIgnoreCase) ? ex.GetType().Name : ex.Message;
    private static bool IsRange(string title, EpisodeNumber episode)
    {
        var match = System.Text.RegularExpressions.Regex.Match(title, @"(?<!\d)(\d+)\s*[-~]\s*(\d+)(?!\d)", System.Text.RegularExpressions.RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        return match.Success && decimal.Parse(episode.Value, System.Globalization.CultureInfo.InvariantCulture) is var number && number >= int.Parse(match.Groups[1].Value) && number <= int.Parse(match.Groups[2].Value);
    }
}
