/*
 * File: CadSllmAgent/Services/CadDebugLog.cs
 * AutoCAD 플러그인 전용: 예외·단계를 파일 + (선택) 커맨드 창에 남깁니다.
 * 경로: %LocalAppData%\CadSllmAgent\cad_agent_debug.log
 */
using System;
using System.IO;
using System.Text;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class CadDebugLog
    {
        private static readonly object _fileLock = new();
        private const string DirName = "CadSllmAgent";
        private const string FileName = "cad_agent_debug.log";
        private const int MaxLogBytes = 1_500_000; // 초과 시 백업 후 자름

        public static string GetLogFilePath()
        {
            var baseDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                DirName);
            return Path.Combine(baseDir, FileName);
        }

        public static void Info(string message) => Write("INFO", message);

        public static void Warn(string message) => Write("WARN", message);

        public static void Error(string message) => Write("ERR ", message);

        public static void Exception(string context, Exception ex)
        {
            if (ex == null) return;
            var sb = new StringBuilder();
            sb.AppendLine(context);
            sb.AppendLine(ex.ToString());
            Write("EX  ", sb.ToString());
        }

        private static void Write(string level, string message)
        {
            try
            {
                var path = GetLogFilePath();
                var dir = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                    Directory.CreateDirectory(dir);

                var line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] [{level}] {message}{Environment.NewLine}";

                lock (_fileLock)
                {
                    MaybeRotate(path);
                    File.AppendAllText(path, line, Encoding.UTF8);
                }
            }
            catch
            {
                /* 로깅 실패는 무시 */
            }
        }

        private static void MaybeRotate(string path)
        {
            try
            {
                if (!File.Exists(path)) return;
                var len = new FileInfo(path).Length;
                if (len <= MaxLogBytes) return;
                var bak = path + ".bak";
                if (File.Exists(bak)) File.Delete(bak);
                File.Move(path, bak);
            }
            catch { /* noop */ }
        }

        /// <summary>명령 창에 붙이기용: 로그 끝 N줄 (UTF-8)</summary>
        public static string ReadTail(int maxLines = 30)
        {
            try
            {
                var path = GetLogFilePath();
                if (!File.Exists(path)) return "[로그 파일 없음 — 아직 기록이 없습니다.]\n";
                var all = File.ReadAllText(path, Encoding.UTF8);
                if (string.IsNullOrEmpty(all)) return "[비어 있음]\n";
                var lines = all.Split(new[] { "\r\n", "\n" }, StringSplitOptions.None);
                var take = Math.Min(maxLines, lines.Length);
                var start = lines.Length - take;
                if (start < 0) start = 0;
                var sb = new StringBuilder();
                sb.AppendLine("--- cad_agent_debug.log (마지막 " + take + "줄) ---");
                for (var i = start; i < lines.Length; i++)
                    sb.AppendLine(lines[i]);
                return sb.ToString();
            }
            catch (Exception ex)
            {
                return "[ReadTail 실패] " + ex.Message + "\n";
            }
        }
    }
}
