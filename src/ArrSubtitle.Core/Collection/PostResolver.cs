using System.Globalization;
using ArrSubtitle.Core.Blogs;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Subtitles;

namespace ArrSubtitle.Core.Collection;

internal sealed class PostResolver(RequestSession session, CollectorSettings settings, Action<SearchProgress>? progress)
{
    private readonly AttachmentInspector inspector = new(session);

    public async Task ResolveAsync(SearchAnime state, PostCandidate post, BlogDescriptor blog, string maker, EpisodeNumber[] captionEpisodes, CancellationToken ct)
    {
        var originalCategory = post.CategoryUrl;
        if (post.Html.Length == 0 || post.IsSummary) post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
        var attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
        if (attachments.Length == 0)
        {
            post = await new BlogExplorer(session).ReadPostAsync(post.Url, ct, blog);
            attachments = AttachmentExtractor.Extract(post).Where(a => !EpisodeEvidence.IsAuxiliary(a.Filename)).ToArray();
        }
        var source = EpisodeNumbering.SourceKey(blog, post.CategoryUrl);
        if (state.Numbering.HasConflict(source)) { state.Record(new(DestinationPolicy.Redact(post.Url), "numbering-conflict"), true); return; }
        var anime = state.Anime;
        if (post.CategoryUrl is not null && state.Aliases.TryGetValue(post.CategoryUrl, out var aliases)) anime = anime with { Titles = anime.Titles.Concat(aliases.Keys).Distinct().ToArray() };
        var categoryVerified = originalCategory is not null && originalCategory == post.CategoryUrl && state.Aliases.ContainsKey(originalCategory);
        if (captionEpisodes.Length == 0 && !categoryVerified && !TitleMatcher.Mentions(post.Title + " " + string.Join(' ', post.Labels), anime.Titles))
        {
            state.Record(new(DestinationPolicy.Redact(post.Url), "post-conflict", "Fetched post does not establish anime identity"), true);
            return;
        }
        var season = TitleMatcher.ExplicitSeason(post.Title);
        if (season.HasValue && anime.Season.HasValue && season != anime.Season) return;
        var postEpisodes = EpisodeParser.FromPost(post.Title, anime);
        if (postEpisodes.Length == 0) postEpisodes = captionEpisodes;
        var numbering = state.Mapping is not null ? new EpisodeNumbering(state.Mapping.EpisodeOffset, state.CaptionMaximum, "explicit configured episode offset") : state.Numbering.Get(source);
        var permitted = EpisodeParser.Read(state.Anime.Title, true);
        state.Expect(postEpisodes.Select(e => numbering is null ? e : numbering.ToSeason(e)).OfType<EpisodeNumber>());
        if (attachments.Length == 0)
        {
            state.Record(new(DestinationPolicy.Redact(post.Url), postEpisodes.Length == 0 ? "skipped-non-subtitle" : "attachment-missing", postEpisodes.Length == 0 ? null : "Subtitle post has no supported attachment link"), postEpisodes.Length > 0);
            return;
        }
        var observations = new List<AttachmentObservation>();
        foreach (var attachment in attachments)
        {
            try { observations.AddRange(await inspector.InspectAsync(attachment, postEpisodes, ct)); }
            catch (Exception error) when (error is not OperationCanceledException) { state.Record(new(DestinationPolicy.Redact(post.Url), "attachment-invalid", SearchErrors.Describe(error)), true); }
        }
        if (numbering is null && anime.Season is > 1 && state.CaptionMaximum > 0 && observations.SelectMany(o => o.Episodes).Any(e => decimal.Parse(e.Value, CultureInfo.InvariantCulture) > state.CaptionMaximum))
        {
            state.Record(new(DestinationPolicy.Redact(post.Url), "numbering-ambiguous", "Explicit episode mapping required"), true);
            return;
        }
        var observedEpisodes = observations.SelectMany(o => o.Episodes).Distinct().ToArray();
        foreach (var observation in observations)
        {
            if (postEpisodes.Length > 0 && observedEpisodes.Length == 1 && observation.Episodes.Any(e => !postEpisodes.Contains(e)))
            {
                state.Record(new(DestinationPolicy.Redact(post.Url), "episode-conflict", "Attachment episode conflicts with post"), true);
                continue;
            }
            if (observation.Episodes.Length == 0)
            {
                state.Record(new(DestinationPolicy.Redact(post.Url), "episode-unresolved", "Attachment has no unique episode evidence"), true);
                continue;
            }
            foreach (var original in observation.Episodes)
            {
                var episode = numbering is null ? original : numbering.ToSeason(original);
                if (episode is null || permitted.Length > 0 && !permitted.Contains(episode.Value)) continue;
                if (!await VerifyIdentityAsync(state, anime, observation, original, ct))
                {
                    state.Record(new(DestinationPolicy.Redact(post.Url), "attachment-conflict", $"Attachment identity is not established: {observation.Name}"), true);
                    continue;
                }
                var page = DestinationPolicy.Redact(post.Url);
                var listing = new SubtitleListing(episode.Value.Value, maker, page, [observation.Name], numbering is null ? null : original.Value, numbering?.Evidence);
                if (state.Add(listing, source)) progress?.Invoke(new("found", anime.Title, episode.Value.Value, page));
            }
        }
    }

    private async Task<bool> VerifyIdentityAsync(SearchAnime state, ResolvedAnime anime, AttachmentObservation attachment, EpisodeNumber episode, CancellationToken ct)
    {
        try { ArchiveSelector.ValidateIdentity(attachment.Name, anime, episode); return true; }
        catch (CollectorException)
        {
            if (!state.AliasesChecked && settings.AniListEnabled && anime.OriginalTitle.Length > 0)
            {
                state.AliasesChecked = true;
                var metadata = await new AniListClient(session).SearchAsync(anime.OriginalTitle, ct);
                var verified = metadata.Where(m => m.Titles.Any(t => TitleMatcher.Equal(t, anime.OriginalTitle))).ToArray();
                if (verified.Length == 1) state.Anime = state.Anime with { Titles = state.Anime.Titles.Concat(verified[0].Titles).Distinct().ToArray() };
            }
            try { ArchiveSelector.ValidateIdentity(attachment.Name, anime with { Titles = anime.Titles.Concat(state.Anime.Titles).Distinct().ToArray() }, episode); return true; }
            catch (CollectorException) { return false; }
        }
    }
}
