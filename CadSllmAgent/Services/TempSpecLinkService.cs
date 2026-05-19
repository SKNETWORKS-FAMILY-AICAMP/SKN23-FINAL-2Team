using System;
using System.IO;
using System.Text;
using System.Text.Json;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    /// <summary>Stores DWG-specific temp spec links in a single hidden sidecar file per DWG folder.</summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class TempSpecLinkService
    {
        public const string FolderName = ".skn23_temp_specs";
        public const string LinkFileName = "link.json";

        public static string? GetActiveDwgPath()
        {
            try
            {
                var doc = AcApp.DocumentManager.MdiActiveDocument;
                if (doc?.Database == null) return null;
                var fn = doc.Database.Filename;
                return string.IsNullOrWhiteSpace(fn) ? null : fn;
            }
            catch { return null; }
        }

        public static string? GetActiveDwgDirectory()
        {
            var fn = GetActiveDwgPath();
            if (string.IsNullOrWhiteSpace(fn)) return null;
            return Path.GetDirectoryName(fn);
        }

        public static string? GetActiveDwgKey()
        {
            var fn = GetActiveDwgPath();
            if (string.IsNullOrWhiteSpace(fn)) return null;
            var key = Path.GetFileName(fn);
            return string.IsNullOrWhiteSpace(key) ? null : key;
        }

        public static void EnsureHiddenDirectory(string dwgDir)
        {
            var dir = Path.Combine(dwgDir, FolderName);
            if (!Directory.Exists(dir))
            {
                var di = Directory.CreateDirectory(dir);
                di.Attributes |= FileAttributes.Hidden;
            }
            else
            {
                new DirectoryInfo(dir).Attributes |= FileAttributes.Hidden;
            }
        }

        public static void WriteLinkJson(string dwgDir, string linkJsonBody)
        {
            var dwgKey = GetActiveDwgKey();
            if (string.IsNullOrWhiteSpace(dwgKey)) return;

            using var payloadDoc = JsonDocument.Parse(linkJsonBody);
            EnsureHiddenDirectory(dwgDir);
            var path = Path.Combine(dwgDir, FolderName, LinkFileName);
            var tmp = path + ".tmp";

            using (var stream = File.Create(tmp))
            using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true }))
            {
                writer.WriteStartObject();
                writer.WriteNumber("version", 3);
                writer.WriteString("updated_at", DateTimeOffset.UtcNow);
                writer.WritePropertyName("dwgs");
                writer.WriteStartObject();

                CopyExistingDwgEntries(writer, path, dwgKey);

                writer.WritePropertyName(dwgKey);
                payloadDoc.RootElement.WriteTo(writer);

                writer.WriteEndObject();
                writer.WriteEndObject();
            }

            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }

        public static string? ReadLinkJson(string dwgDir)
        {
            var path = Path.Combine(dwgDir, FolderName, LinkFileName);
            if (!File.Exists(path)) return null;
            try
            {
                var raw = File.ReadAllText(path, Encoding.UTF8);
                var trimmed = raw.Trim();
                if (!trimmed.StartsWith("{")) return null;

                using var doc = JsonDocument.Parse(trimmed);
                if (!doc.RootElement.TryGetProperty("dwgs", out var dwgs) ||
                    dwgs.ValueKind != JsonValueKind.Object)
                {
                    return trimmed;
                }

                var dwgKey = GetActiveDwgKey();
                if (string.IsNullOrWhiteSpace(dwgKey)) return null;
                return dwgs.TryGetProperty(dwgKey, out var payload)
                    ? payload.GetRawText()
                    : null;
            }
            catch { return null; }
        }

        public static bool DeleteLinkJson(string dwgDir)
        {
            var path = Path.Combine(dwgDir, FolderName, LinkFileName);
            if (!File.Exists(path)) return false;

            var dwgKey = GetActiveDwgKey();
            if (string.IsNullOrWhiteSpace(dwgKey))
            {
                TryDeleteFile(path);
                return true;
            }

            try
            {
                var raw = File.ReadAllText(path, Encoding.UTF8);
                using var doc = JsonDocument.Parse(raw);
                if (!doc.RootElement.TryGetProperty("dwgs", out var dwgs) ||
                    dwgs.ValueKind != JsonValueKind.Object)
                {
                    TryDeleteFile(path);
                    return true;
                }

                var tmp = path + ".tmp";
                var remaining = 0;
                using (var stream = File.Create(tmp))
                using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true }))
                {
                    writer.WriteStartObject();
                    writer.WriteNumber("version", 3);
                    writer.WriteString("updated_at", DateTimeOffset.UtcNow);
                    writer.WritePropertyName("dwgs");
                    writer.WriteStartObject();

                    foreach (var entry in dwgs.EnumerateObject())
                    {
                        if (string.Equals(entry.Name, dwgKey, StringComparison.OrdinalIgnoreCase))
                            continue;
                        writer.WritePropertyName(entry.Name);
                        entry.Value.WriteTo(writer);
                        remaining++;
                    }

                    writer.WriteEndObject();
                    writer.WriteEndObject();
                }

                if (remaining == 0)
                {
                    TryDeleteFile(tmp);
                    TryDeleteFile(path);
                }
                else
                {
                    if (File.Exists(path)) File.Delete(path);
                    File.Move(tmp, path);
                }
                return true;
            }
            catch
            {
                return false;
            }
        }

        private static void CopyExistingDwgEntries(Utf8JsonWriter writer, string path, string currentDwgKey)
        {
            if (!File.Exists(path)) return;

            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
                if (!doc.RootElement.TryGetProperty("dwgs", out var dwgs) ||
                    dwgs.ValueKind != JsonValueKind.Object)
                {
                    return;
                }

                foreach (var entry in dwgs.EnumerateObject())
                {
                    if (string.Equals(entry.Name, currentDwgKey, StringComparison.OrdinalIgnoreCase))
                        continue;
                    writer.WritePropertyName(entry.Name);
                    entry.Value.WriteTo(writer);
                }
            }
            catch
            {
                // Ignore malformed sidecar data; the current DWG entry will be rewritten.
            }
        }

        private static void TryDeleteFile(string path)
        {
            try
            {
                if (File.Exists(path)) File.Delete(path);
            }
            catch { }
        }
    }
}
