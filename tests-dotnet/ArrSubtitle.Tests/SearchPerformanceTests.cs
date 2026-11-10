using System.Collections.Concurrent;
using System.Net;
using System.Text;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class SearchPerformanceTests
{
    [Fact]
    public async Task SharedMakerBlogIsEnumeratedOnceForMultipleAnime()
    {
        var handler = new ReplayHandler(request =>
        {
            var uri = request.RequestUri!;
            if (uri.AbsolutePath.StartsWith("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test One"},{"animeNo":2,"subject":"Test Two"}],"last":true}}""");
            if (uri.AbsolutePath.Contains("/anime/animeNo/", StringComparison.Ordinal)) return Detail(uri.AbsolutePath.EndsWith('1') ? "Test One" : "Test Two");
            if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) return Captions("https://shared.blogspot.com/" + (uri.AbsolutePath.EndsWith('1') ? "one" : "two"));
            if (uri.AbsolutePath is "/one" or "/two") return ReplayHandler.Text($"<meta property='og:title' content='{(uri.AbsolutePath == "/one" ? "Test One" : "Test Two")} 1화'><div class='post-body'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html");
            return ReplayHandler.Text("""{"feed":{"openSearch$totalResults":{"$t":"2"},"entry":[{"title":{"$t":"Test One 1화"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://shared.blogspot.com/one"}]},{"title":{"$t":"Test Two 1화"},"content":{"$t":"<a href='https://files.example.com/01.smi'>01.smi</a>"},"link":[{"rel":"alternate","href":"https://shared.blogspot.com/two"}]}],"link":[]}}""");
        });
        using var session = new RequestSession(handler);
        var report = await new SubtitleSearch(session, new()).SearchAsync("Test", default);
        Assert.True(report.Success);
        Assert.Equal(2, report.Anime.Length);
        Assert.All(report.Anime, a => Assert.Single(a.Subtitles));
        Assert.Single(handler.Calls, c => c.Contains("/feeds/posts/", StringComparison.Ordinal));
    }

    [Fact]
    public async Task IndependentBlogsRunConcurrentlyAndProgressArrivesBeforeCompletion()
    {
        var started = new ConcurrentDictionary<string, bool>();
        var bothStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var progress = new ConcurrentQueue<SearchProgress>();
        using var session = new RequestSession(new AsyncHandler(async (request, ct) =>
        {
            var uri = request.RequestUri!;
            if (uri.Host == "api.anissia.net") return Metadata(uri);
            started.TryAdd(uri.Host, true);
            if (started.Count == 2) bothStarted.TrySetResult();
            await release.Task.WaitAsync(ct);
            return Feed(uri.Host);
        }));
        var settings = new Core.Configuration.CollectorSettings { Blogs = [new("https://a.blogspot.com/", BlogPlatform.Blogspot, "a.blogspot.com"), new("https://b.blogspot.com/", BlogPlatform.Blogspot, "b.blogspot.com")] };
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        var search = new SubtitleSearch(session, settings).SearchAsync("Test", timeout.Token, progress.Enqueue);
        try
        {
            await bothStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.False(search.IsCompleted);
            Assert.Equal(2, progress.Count(p => p.Stage == "source"));
        }
        finally { release.TrySetResult(); }
        var report = await search;
        Assert.True(report.Success);
        Assert.Equal(2, report.Anime.Single().Subtitles.Length);
        Assert.Equal(2, progress.Count(p => p.Stage == "found"));
    }

    [Fact]
    public async Task CancellationPreservesCurrentAnimeAndFoundEpisodes()
    {
        using var cancellation = new CancellationTokenSource();
        var requests = new ConcurrentQueue<string>();
        using var session = new RequestSession(new AsyncHandler(async (request, ct) =>
        {
            var uri = request.RequestUri!;
            requests.Enqueue(uri.AbsoluteUri);
            if (uri.Host == "api.anissia.net") return Metadata(uri);
            if (uri.Query.Contains("start-index", StringComparison.Ordinal)) { await Task.Delay(Timeout.InfiniteTimeSpan, ct); throw new InvalidOperationException(); }
            return Feed(uri.Host, next: true);
        }));
        var settings = new Core.Configuration.CollectorSettings { Blogs = [new("https://a.blogspot.com/", BlogPlatform.Blogspot, "a.blogspot.com")] };
        var report = await new SubtitleSearch(session, settings).SearchAsync("Test", cancellation.Token, update => { if (update.Stage == "found") cancellation.Cancel(); });
        Assert.False(report.Complete);
        Assert.False(report.Success);
        Assert.Single(report.Anime.Single().Subtitles);
        Assert.Contains(report.Attempts, a => a.Status == "cancelled");
        Assert.DoesNotContain(requests, r => r.Contains("start-index", StringComparison.Ordinal));
    }

    [Fact]
    public async Task OverallTimeoutReturnsIncompleteReportWithinBudget()
    {
        var cancelled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var session = new RequestSession(new AsyncHandler(async (_, ct) =>
        {
            try { await Task.Delay(Timeout.InfiniteTimeSpan, ct); }
            finally { cancelled.TrySetResult(); }
            return Metadata(new("https://api.anissia.net/anime/list/0"));
        }));
        var search = new SubtitleSearch(session, new()).SearchAsync("Test", default, timeout: TimeSpan.FromSeconds(1));
        var report = await search.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(report.Complete);
        Assert.Contains(report.Attempts, a => a.Status == "timeout");
        Assert.True(cancelled.Task.IsCompleted);
    }

    [Fact]
    public async Task CancellingOneWaiterDoesNotCancelOtherWaiter()
    {
        var started = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var firstCancellation = new CancellationTokenSource();
        using var session = new RequestSession(new AsyncHandler(async (_, ct) => { started.TrySetResult(); await release.Task.WaitAsync(ct); return ReplayHandler.Text("ok"); }));
        var first = session.GetAsync("https://example.com/", firstCancellation.Token);
        await started.Task;
        var second = session.GetAsync("https://example.com/", default);
        firstCancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => first);
        release.TrySetResult();
        Assert.Equal("ok", (await second).Text);
        Assert.Equal(1, session.RequestCounts["example.com"]);
    }

    [Fact]
    public async Task LastCancelledWaiterStopsRequestAndAllowsRetry()
    {
        var calls = 0;
        var stopped = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var cancellation = new CancellationTokenSource();
        using var session = new RequestSession(new AsyncHandler(async (_, ct) =>
        {
            if (Interlocked.Increment(ref calls) == 1)
            {
                try { await Task.Delay(Timeout.InfiniteTimeSpan, ct); }
                finally { stopped.TrySetResult(); }
            }
            return ReplayHandler.Text("ok");
        }));
        var first = session.GetAsync("https://example.com/", cancellation.Token);
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => first);
        Assert.True(stopped.Task.IsCompleted);
        Assert.Equal("ok", (await session.GetAsync("https://example.com/", default)).Text);
        Assert.Equal(2, calls);
    }

    private static HttpResponseMessage Metadata(Uri uri)
    {
        if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) return ReplayHandler.Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"Test"}],"last":true}}""");
        return uri.AbsolutePath.Contains("caption", StringComparison.Ordinal) ? ReplayHandler.Text("""{"code":"ok","data":[]}""") : Detail("Test");
    }

    private static HttpResponseMessage Detail(string title) => ReplayHandler.Text($"{{\"code\":\"ok\",\"data\":{{\"subject\":\"{title}\",\"originalSubject\":\"{title}\"}}}}");
    private static HttpResponseMessage Captions(string url) => ReplayHandler.Text($"{{\"code\":\"ok\",\"data\":[{{\"episode\":\"1\",\"website\":\"{url}\",\"name\":\"Author\",\"updDt\":\"2026\"}}]}}");
    private static HttpResponseMessage Feed(string host, bool next = false) => ReplayHandler.Text($"{{\"feed\":{{\"openSearch$totalResults\":{{\"$t\":\"{(next ? 2 : 1)}\"}},\"entry\":[{{\"title\":{{\"$t\":\"Test 1화\"}},\"content\":{{\"$t\":\"<a href='https://files.example.com/01.smi'>01.smi</a>\"}},\"link\":[{{\"rel\":\"alternate\",\"href\":\"https://{host}/1\"}}]}}],\"link\":[{(next ? "{\"rel\":\"next\",\"href\":\"https://" + host + "/feeds/posts/default?start-index=2\"}" : "")}]}}}}");

    private sealed class AsyncHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> send) : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var response = await send(request, cancellationToken);
            response.RequestMessage = request;
            return response;
        }
    }
}
