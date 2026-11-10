using System.Text.Json.Serialization;
using System.Text.Json;
using System.Text.Encodings.Web;
using System.Text.Unicode;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Serialization;

[JsonSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase, WriteIndented = true, UseStringEnumConverter = true, UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow)]
[JsonSerializable(typeof(CollectorSettings))]
[JsonSerializable(typeof(CollectionReport))]
[JsonSerializable(typeof(BlogDescriptor[]))]
[JsonSerializable(typeof(BlogDescriptor))]
[JsonSerializable(typeof(SubtitleSearchReport))]
public partial class CollectorJsonContext : JsonSerializerContext
{
    public static CollectorJsonContext Readable { get; } = new(new JsonSerializerOptions
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = true,
        Encoder = JavaScriptEncoder.Create(UnicodeRanges.All)
    });
}
