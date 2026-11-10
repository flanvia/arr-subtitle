using System.Net;
using System.Net.Sockets;

namespace ArrSubtitle.Core.Networking;

public static class DestinationPolicy
{
    public static void ValidateUrl(Uri uri)
    {
        if (!uri.IsAbsoluteUri || uri.Scheme is not ("http" or "https") || uri.UserInfo.Length != 0 || uri.HostNameType == UriHostNameType.Unknown || uri.Port is not (80 or 443)) throw new HttpRequestException("Unsafe destination URL");
        if (IPAddress.TryParse(uri.Host, out var ip) && !IsPublic(ip)) throw new HttpRequestException("Non-public destination");
    }

    public static bool IsPublic(IPAddress address)
    {
        if (address.IsIPv4MappedToIPv6) address = address.MapToIPv4();
        var bytes = address.GetAddressBytes();
        if (address.AddressFamily == AddressFamily.InterNetwork)
        {
            var a = bytes[0]; var b = bytes[1]; var c = bytes[2];
            return !(a is 0 or 10 or 127 || a >= 224 || a == 100 && b is >= 64 and <= 127 || a == 169 && b == 254 || a == 172 && b is >= 16 and <= 31 || a == 192 && (b == 168 || b == 0 || b == 88 && c == 99) || a == 198 && (b is 18 or 19 || b == 51 && c == 100) || a == 203 && b == 0 && c == 113);
        }
        return address.AddressFamily == AddressFamily.InterNetworkV6 && (bytes[0] & 0xe0) == 0x20 && !(bytes[0] == 0x20 && bytes[1] == 0x01 && (bytes[2] < 2 || bytes[2] == 0x0d && bytes[3] == 0xb8)) && !(bytes[0] == 0x20 && bytes[1] == 0x02);
    }

    public static string Redact(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return "invalid-url";
        if (uri.Host is "blog.naver.com" or "m.blog.naver.com")
        {
            var parts = Blogs.BlogDetector.ParseQuery(uri.Query);
            if (parts.TryGetValue("blogId", out var blog) && parts.TryGetValue("logNo", out var post) && System.Text.RegularExpressions.Regex.IsMatch(blog, @"^[A-Za-z0-9_-]+$", System.Text.RegularExpressions.RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)) && post.All(char.IsAsciiDigit)) return $"https://blog.naver.com/{blog}/{post}";
        }
        return uri.GetLeftPart(UriPartial.Path);
    }
}
