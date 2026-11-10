using System.Net;
using ArrSubtitle.Core.Networking;
using Xunit;

namespace ArrSubtitle.Tests;

public sealed class NetworkingTests
{
    [Theory]
    [InlineData("127.0.0.1")]
    [InlineData("::ffff:127.0.0.1")]
    [InlineData("10.1.2.3")]
    [InlineData("169.254.169.254")]
    [InlineData("100.64.1.2")]
    [InlineData("2001:db8::1")]
    [InlineData("fc00::1")]
    public void RejectsNonPublicAddresses(string ip) => Assert.False(DestinationPolicy.IsPublic(IPAddress.Parse(ip)));

    [Fact]
    public async Task RejectsPrivateRedirect()
    {
        var handler = new ReplayHandler(_ => { var r = new HttpResponseMessage(HttpStatusCode.Redirect); r.Headers.Location = new("http://127.0.0.1/"); return r; });
        using var session = new RequestSession(handler);
        await Assert.ThrowsAsync<HttpRequestException>(() => session.GetAsync("https://example.com/start", default));
        Assert.Single(handler.Calls);
    }

    [Fact]
    public async Task CoalescesConcurrentGet()
    {
        var handler = new ReplayHandler(_ => ReplayHandler.Text("ok"));
        using var session = new RequestSession(handler);
        await Task.WhenAll(Enumerable.Range(0, 10).Select(_ => session.GetAsync("https://example.com/", default)));
        Assert.Single(handler.Calls);
    }

    [Fact]
    public async Task DoesNotCacheFailedGet()
    {
        var count = 0;
        var handler = new ReplayHandler(_ => ++count == 1 ? new(HttpStatusCode.Forbidden) : ReplayHandler.Text("ok"));
        using var session = new RequestSession(handler);
        await Assert.ThrowsAsync<HttpRequestException>(() => session.GetAsync("https://example.com/", default));
        Assert.Equal("ok", (await session.GetAsync("https://example.com/", default)).Text);
        Assert.Equal(2, count);
    }

    [Fact]
    public async Task CancelsOversizedBody()
    {
        using var session = new RequestSession(new ReplayHandler(_ => ReplayHandler.Text("123456")));
        await Assert.ThrowsAsync<InvalidDataException>(() => session.GetAsync("https://example.com/", default, 5));
    }

    [Fact]
    public async Task HonorsRetryAfterWithoutEarlyRetry()
    {
        var handler = new ReplayHandler(_ => { var r = new HttpResponseMessage(HttpStatusCode.TooManyRequests); r.Headers.RetryAfter = new(TimeSpan.FromMinutes(2)); return r; });
        using var session = new RequestSession(handler);
        await Assert.ThrowsAsync<HttpRequestException>(() => session.GetAsync("https://example.com/", default));
        Assert.Single(handler.Calls);
    }
}
