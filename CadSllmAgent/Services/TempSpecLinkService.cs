using System;
using System.IO;
using System.Text;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    /// <summary>DWG 폴더 아래 숨김 디렉터리에 임시 시방서 연동 메타(link.json)만 기록합니다.</summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class TempSpecLinkService
    {
        public const string FolderName = ".skn23_temp_specs";
        public const string LinkFileName = "link.json";

        public static string? GetActiveDwgDirectory()
        {
            try
            {
                var doc = AcApp.DocumentManager.MdiActiveDocument;
                if (doc?.Database == null) return null;
                var fn = doc.Database.Filename;
                if (string.IsNullOrWhiteSpace(fn)) return null;
                return Path.GetDirectoryName(fn);
            }
            catch { return null; }
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
            EnsureHiddenDirectory(dwgDir);
            var path = Path.Combine(dwgDir, FolderName, LinkFileName);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, linkJsonBody, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }

        public static string? ReadLinkJson(string dwgDir)
        {
            var path = Path.Combine(dwgDir, FolderName, LinkFileName);
            if (!File.Exists(path)) return null;
            try { return File.ReadAllText(path, Encoding.UTF8); }
            catch { return null; }
        }
    }
}
