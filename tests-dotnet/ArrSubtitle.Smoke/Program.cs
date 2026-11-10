using System.Globalization;
using System.IO.Compression;
using System.Net;
using System.Text;
using System.Text.Json;
using ArrSubtitle.Core.Collection;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Identity;
using ArrSubtitle.Core.Models;
using ArrSubtitle.Core.Networking;
using ArrSubtitle.Core.Serialization;
using ArrSubtitle.Core.Subtitles;

Encoding.RegisterProvider(CodePagesEncodingProvider.Instance);
SubtitleValidator.Validate(Encoding.GetEncoding(949).GetBytes(ReplayHandler.Sami), ".smi");
if (TitleMatcher.Equal("Working!!", "Working'!!") || !TitleMatcher.Equal("凶乱令嬢ニア・リストン", "凶乱令嬢ニア・リストン")) throw new Exception("Title identity lost");
var settings = JsonSerializer.Deserialize("{}", CollectorJsonContext.Default.CollectorSettings)!;
if (settings.Mappings is null || settings.Blogs is null) throw new Exception("JSON settings defaults were not initialized: " + JsonSerializer.Serialize(settings, CollectorJsonContext.Default.CollectorSettings));
var scenario = args.FirstOrDefault() ?? "batch";
if (scenario == "tls")
{
    using var client = new System.Net.Http.HttpClient();
    client.DefaultRequestHeaders.UserAgent.ParseAdd("ArrSubtitle-CI/0.1");
    client.Timeout = TimeSpan.FromSeconds(20);
    using var response = await client.GetAsync("https://example.com/");
    response.EnsureSuccessStatusCode();
    Console.WriteLine("HTTPS certificate validation passed");
    return;
}
var count = args.Length > 1 ? int.Parse(args[1], CultureInfo.InvariantCulture) : 12;
if (count is < 1 or > 12) throw new ArgumentException("Invalid scenario episode count");
var directory = Path.Combine(Path.GetTempPath(), "arr-subtitle-smoke-" + Guid.NewGuid().ToString("N"));
try
{
    using var session = new RequestSession(new ReplayHandler(scenario));
    if (scenario is "search" or "search-cancel")
    {
        using var cancellation = new CancellationTokenSource();
        var search = await new SubtitleSearch(session, settings with { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] }).SearchAsync("테스트", cancellation.Token, update => { if (scenario == "search-cancel" && update.Stage == "found") cancellation.Cancel(); });
        if (scenario == "search" ? !search.Success || search.Anime.Single().Subtitles.Length != 12 : search.Complete || search.Anime.Single().Subtitles.Length == 0) throw new Exception("Search omitted episodes or cancellation state");
        var output = JsonSerializer.Serialize(search, CollectorJsonContext.Readable.SubtitleSearchReport);
        if (!output.Contains("테스트", StringComparison.Ordinal)) throw new Exception("Readable JSON escaped Korean text");
        Console.WriteLine(output);
        return;
    }
    var collector = new SubtitleCollector(session, settings with { Blogs = [new("https://fallback.blogspot.com/", BlogPlatform.Blogspot, "fallback.blogspot.com")] });
    var report = await collector.CollectAsync(new(new(AnimeId: 1), Enumerable.Range(1, count).Select(n => new EpisodeTarget(EpisodeNumber.Parse(n.ToString(CultureInfo.InvariantCulture)))).ToArray(), directory), default);
    if (scenario == "missing" ? report.Success : !report.Success) throw new Exception("Unexpected smoke result: " + JsonSerializer.Serialize(report, CollectorJsonContext.Default.CollectionReport));
    if (report.Requests["api.anissia.net"] != 2) throw new Exception("Repeated metadata fetch");
    if (scenario == "batch" && report.Requests["files.example.com"] != 1) throw new Exception("Repeated batch download");
    Console.WriteLine(JsonSerializer.Serialize(report, CollectorJsonContext.Readable.CollectionReport));
}
finally { if (Directory.Exists(directory)) Directory.Delete(directory, true); }

internal sealed class ReplayHandler(string scenario) : HttpMessageHandler
{
    public const string Sami = "<SAMI><BODY><SYNC Start=0><P>안녕하세요</P><SYNC Start=2000><P>&nbsp;</P></BODY></SAMI>";
    private readonly byte[] archive = CreateArchive();
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        await Task.Delay(50, ct);
        var uri = request.RequestUri!;
        HttpResponseMessage response;
        if (uri.AbsolutePath.Contains("/anime/list/", StringComparison.Ordinal)) response = Text("""{"code":"ok","data":{"content":[{"animeNo":1,"subject":"테스트"}],"last":true}}""");
        else if (uri.AbsolutePath.Contains("caption", StringComparison.Ordinal)) response = Text(scenario == "direct" ? """{"code":"ok","data":[{"episode":"1","website":"https://author.blogspot.com/1.html","name":"fixture","updDt":"2026"}]}""" : """{"code":"ok","data":[]}""");
        else if (uri.AbsolutePath.Contains("animeNo", StringComparison.Ordinal)) response = Text("""{"code":"ok","data":{"subject":"테스트","originalSubject":"Test"}}""");
        else if (uri.AbsolutePath == "/1.html") response = Text("<meta property='og:title' content='테스트 1화'><div class='post-body'><a href='https://files.example.com/01.smi'>01.smi</a></div>", "text/html");
        else if (uri.AbsolutePath.EndsWith(".zip", StringComparison.Ordinal)) response = new(HttpStatusCode.OK) { Content = new ByteArrayContent(archive) };
        else if (uri.AbsolutePath.EndsWith(".smi", StringComparison.Ordinal)) response = new(HttpStatusCode.OK) { Content = new ByteArrayContent(Encoding.Unicode.GetPreamble().Concat(Encoding.Unicode.GetBytes(Sami)).ToArray()) };
        else response = Text(scenario == "missing" ? """{"feed":{"openSearch$totalResults":{"$t":"0"},"link":[]}}""" : """{"feed":{"openSearch$totalResults":{"$t":"1"},"entry":[{"title":{"$t":"테스트 1-12화 자막"},"content":{"$t":"<a href='https://files.example.com/batch.zip'>테스트 1-12.zip</a>"},"link":[{"rel":"alternate","href":"https://fallback.blogspot.com/batch.html"}]}],"link":[]}}""");
        response.RequestMessage = request;
        return response;
    }

    private static HttpResponseMessage Text(string text, string type = "application/json") => new(HttpStatusCode.OK) { Content = new StringContent(text, Encoding.UTF8, type) };
    private static byte[] CreateArchive()
    {
        using var stream = new MemoryStream();
        using (var zip = new ZipArchive(stream, ZipArchiveMode.Create, true))
        for (var episode = 1; episode <= 12; episode++)
        {
            using var entry = zip.CreateEntry($"테스트 {episode:00}.smi").Open();
            entry.Write(Encoding.Unicode.GetPreamble().Concat(Encoding.Unicode.GetBytes(Sami)).ToArray());
        }
        return stream.ToArray();
    }
}
