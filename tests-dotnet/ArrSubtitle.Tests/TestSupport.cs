using System.Net;
using System.Text;
using System.IO.Compression;

namespace ArrSubtitle.Tests;

internal sealed class ReplayHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    public List<string> Calls { get; } = [];
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        lock (Calls) Calls.Add(request.RequestUri!.AbsoluteUri);
        var response = respond(request);
        response.RequestMessage = request;
        return Task.FromResult(response);
    }
    public static HttpResponseMessage Text(string body, string type = "application/json") => new(HttpStatusCode.OK) { Content = new StringContent(body, Encoding.UTF8, type) };
    public static HttpResponseMessage Bytes(byte[] body) => new(HttpStatusCode.OK) { Content = new ByteArrayContent(body) };
}

internal sealed class TempDirectory : IDisposable
{
    public string Path { get; } = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "arr-subtitle-tests-" + Guid.NewGuid().ToString("N"));
    public TempDirectory() => Directory.CreateDirectory(Path);
    public void Dispose() => Directory.Delete(Path, true);
}

internal static class Samples
{
    public const string Sami = "<SAMI><BODY><SYNC Start=0><P>안녕하세요</P><SYNC Start=2000><P>&nbsp;</P></BODY></SAMI>";
    public static byte[] Utf16 => Encoding.Unicode.GetPreamble().Concat(Encoding.Unicode.GetBytes(Sami)).ToArray();
    public static byte[] Zip(params (string Name, byte[] Bytes)[] files)
    {
        using var stream = new MemoryStream();
        using (var zip = new ZipArchive(stream, ZipArchiveMode.Create, true))
            foreach (var file in files) { using var entry = zip.CreateEntry(file.Name).Open(); entry.Write(file.Bytes); }
        return stream.ToArray();
    }
}
