using System.Text.Json.Serialization;
using ArrSubtitle.Core.Configuration;
using ArrSubtitle.Core.Models;

namespace ArrSubtitle.Core.Serialization;

[JsonSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase, WriteIndented = true, UseStringEnumConverter = true, UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow)]
[JsonSerializable(typeof(CollectorSettings))]
[JsonSerializable(typeof(CollectionReport))]
[JsonSerializable(typeof(BlogDescriptor[]))]
[JsonSerializable(typeof(BlogDescriptor))]
[JsonSerializable(typeof(SubtitleSearchReport))]
public partial class CollectorJsonContext : JsonSerializerContext;
