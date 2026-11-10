using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;

namespace ArrSubtitle.Core.Networking;

public sealed record FetchResult(Uri Url, byte[] Bytes, string? ContentType, string? Filename)
{
    public string Text => Encoding.UTF8.GetString(Bytes);
}

public sealed class RequestSession : IDisposable
{
    private readonly HttpClient client;
    private readonly ConcurrentDictionary<string, Lazy<Task<FetchResult>>> requests = new();
    private readonly ConcurrentDictionary<string, SemaphoreSlim> hostLocks = new();
    private readonly ConcurrentDictionary<string, long> lastStarts = new();
    private readonly ConcurrentDictionary<string, int> counts = new();
    private readonly SemaphoreSlim global = new(4);
    private long bytes;
    private long retained;
    private readonly CancellationToken lifetime;
    public long ReceivedBytes => Interlocked.Read(ref bytes);
    public Dictionary<string, int> RequestCounts => counts.ToDictionary();

    public RequestSession(HttpMessageHandler? handler = null, CancellationToken lifetime = default)
    {
        this.lifetime = lifetime;
        client = new(handler ?? CreateHandler()) { Timeout = Timeout.InfiniteTimeSpan };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("ArrSubtitle/0.1");
    }

    private static SocketsHttpHandler CreateHandler() => new()
    {
        AllowAutoRedirect = false,
        UseProxy = false,
        AutomaticDecompression = DecompressionMethods.All,
        ConnectTimeout = TimeSpan.FromSeconds(10),
        PooledConnectionLifetime = TimeSpan.FromMinutes(2),
        ConnectCallback = async (context, ct) =>
        {
            var addresses = await Dns.GetHostAddressesAsync(context.DnsEndPoint.Host, ct);
            if (addresses.Length == 0 || addresses.Any(a => !DestinationPolicy.IsPublic(a))) throw new HttpRequestException("DNS returned a non-public destination");
            foreach (var address in addresses.OrderBy(a => a.AddressFamily != AddressFamily.InterNetwork))
            {
                var socket = new Socket(address.AddressFamily, SocketType.Stream, ProtocolType.Tcp);
                try { await socket.ConnectAsync(address, context.DnsEndPoint.Port, ct); return new NetworkStream(socket, true); }
                catch (SocketException) { socket.Dispose(); }
                catch { socket.Dispose(); throw; }
            }
            throw new HttpRequestException("Connection failed");
        }
    };

    public async Task<FetchResult> GetAsync(string url, CancellationToken ct, int limit = 4 * 1024 * 1024, Func<Uri, bool>? scope = null) => await FetchAsync(url, null, limit, scope, ct);
    public async Task<FetchResult> PostJsonAsync(string url, string body, CancellationToken ct) => await FetchAsync(url, body, 4 * 1024 * 1024, null, ct);

    private async Task<FetchResult> FetchAsync(string url, string? body, int limit, Func<Uri, bool>? scope, CancellationToken ct)
    {
        var uri = new Uri(url);
        DestinationPolicy.ValidateUrl(uri);
        if (scope is not null && !scope(uri)) throw new HttpRequestException("URL outside blog scope");
        var key = $"{uri.AbsoluteUri}|{limit}|{(body is null ? "GET" : Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(body))))}";
        var lazy = requests.GetOrAdd(key, _ => new(() => SendAsync(uri, body, limit, scope, lifetime), LazyThreadSafetyMode.ExecutionAndPublication));
        FetchResult result;
        try { result = await lazy.Value.WaitAsync(ct); }
        catch { if (lazy.IsValueCreated && lazy.Value.IsCompleted && !lazy.Value.IsCompletedSuccessfully) requests.TryRemove(key, out _); throw; }
        if (scope is not null && !scope(result.Url)) throw new HttpRequestException("Redirect outside blog scope");
        return result;
    }

    private async Task<FetchResult> SendAsync(Uri initial, string? initialBody, int limit, Func<Uri, bool>? scope, CancellationToken ct)
    {
        var uri = initial; var body = initialBody; var retries = 0; var waited = TimeSpan.Zero;
        for (var redirects = 0; redirects <= 5;)
        {
            DestinationPolicy.ValidateUrl(uri);
            if (scope is not null && !scope(uri)) throw new HttpRequestException("Redirect outside blog scope");
            var hostLock = hostLocks.GetOrAdd(uri.IdnHost, _ => new SemaphoreSlim(1));
            await hostLock.WaitAsync(ct);
            try
            {
                if (lastStarts.TryGetValue(uri.IdnHost, out var last))
                {
                    var remaining = TimeSpan.FromSeconds(1) - Stopwatch.GetElapsedTime(last);
                    if (remaining > TimeSpan.Zero) await Task.Delay(remaining, ct);
                }
                await global.WaitAsync(ct);
                lastStarts[uri.IdnHost] = Stopwatch.GetTimestamp();
                try
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    timeout.CancelAfter(TimeSpan.FromSeconds(20));
                    using var request = new HttpRequestMessage(body is null ? HttpMethod.Get : HttpMethod.Post, uri);
                    if (body is not null) request.Content = new StringContent(body, Encoding.UTF8, "application/json");
                    counts.AddOrUpdate(uri.IdnHost, 1, (_, n) => n + 1);
                    using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
                    var status = (int)response.StatusCode;
                    if (status is 301 or 302 or 303 or 307 or 308)
                    {
                        var location = response.Headers.Location ?? throw new HttpRequestException("Redirect has no location");
                        uri = new Uri(uri, location);
                        if (status == 303) body = null;
                        redirects++;
                        continue;
                    }
                    if (status is 429 or 502 or 503 or 504 && retries < 2)
                    {
                        var delay = response.Headers.RetryAfter?.Delta ?? (response.Headers.RetryAfter?.Date - DateTimeOffset.UtcNow) ?? TimeSpan.FromSeconds(1 << retries);
                        if (delay < TimeSpan.Zero) delay = TimeSpan.Zero;
                        if (waited + delay > TimeSpan.FromSeconds(30)) throw new HttpRequestException("Retry-After exceeds retry budget");
                        waited += delay; retries++;
                        await Task.Delay(delay, ct);
                        continue;
                    }
                    if (!response.IsSuccessStatusCode) throw new HttpRequestException($"HTTP {status} from {uri.Host}", null, response.StatusCode);
                    await using var stream = await response.Content.ReadAsStreamAsync(timeout.Token);
                    using var buffer = new MemoryStream();
                    var chunk = new byte[32768];
                    int read;
                    while ((read = await stream.ReadAsync(chunk, timeout.Token)) != 0)
                    {
                        Interlocked.Add(ref bytes, read);
                        if (buffer.Length + read > limit) throw new InvalidDataException("Response exceeds size limit");
                        buffer.Write(chunk, 0, read);
                    }
                    var payload = buffer.ToArray();
                    if (Interlocked.Add(ref retained, payload.Length) > 128L * 1024 * 1024) throw new InvalidDataException("Request memory budget exceeded");
                    return new(uri, payload, response.Content.Headers.ContentType?.MediaType, response.Content.Headers.ContentDisposition?.FileNameStar ?? response.Content.Headers.ContentDisposition?.FileName?.Trim('"'));
                }
                finally { global.Release(); }
            }
            finally { hostLock.Release(); }
        }
        throw new HttpRequestException("Too many redirects");
    }

    public void Dispose()
    {
        client.Dispose(); global.Dispose();
        foreach (var item in hostLocks.Values) item.Dispose();
    }
}
