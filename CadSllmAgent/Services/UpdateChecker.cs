/*
 * File    : CadSllmAgent/Services/UpdateChecker.cs
 * Create  : 2026-04-28
 * Description : 플러그인 자동 업데이트 확인 및 다운로드 서비스.
 *
 *   AutoCAD 시작 시(PluginEntry.Application_Idle) 호출되어:
 *     1. 로컬 version.txt에서 현재 버전 읽기
 *     2. 백엔드 GET /api/v1/plugin/version-check 호출
 *     3. 새 버전이 있으면 GET /api/v1/plugin/download 로 %TEMP%에 다운로드
 *     4. updater.exe를 백그라운드에서 실행 (acad 종료 대기 모드)
 *     5. 사용자에게 "업데이트 준비 완료" 메시지 표시
 */

using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Text.Json;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class UpdateChecker
    {
        private static readonly HttpClient _httpClient = new HttpClient
        {
            BaseAddress = new Uri(AgentConfig.BackendBaseUrl),
            Timeout = TimeSpan.FromMinutes(10)
        };

        /// <summary>
        /// 플러그인 업데이트를 확인하고, 필요 시 다운로드 → updater.exe 실행까지 수행.
        /// 논블로킹으로 호출되며 실패해도 플러그인 동작에 영향 없음.
        /// </summary>
        public static async Task CheckAndPrepareAsync()
        {
            // 런타임 체크 (const 직접 비교 시 CS0162 경고 방지)
            bool enabled = AgentConfig.AutoUpdateEnabled;
            if (!enabled)
                return;

            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;

            try
            {
                // ── 1. 현재 버전 읽기 ────────────────────────────────────
                string localVersion = GetLocalVersion();
                CadDebugLog.Info($"[UpdateChecker] 로컬 버전: v{localVersion}");

                string storedApiKey = GetStoredApiKey();
                if (string.IsNullOrWhiteSpace(storedApiKey))
                {
                    CadDebugLog.Info("[UpdateChecker] API Key가 없어 업데이트 확인을 건너뜁니다.");
                    return;
                }

                // ── 2. 백엔드에서 최신 버전 확인 ─────────────────────────
                var request = new HttpRequestMessage(HttpMethod.Get, "/api/v1/plugin/version-check");
                // 인증 헤더 — 현재 등록된 API Key 사용
                // (PluginEntry.cs에서 라이선스 인증 후 저장된 키가 있으면 사용)
                request.Headers.Add("X-Api-Key", storedApiKey);

                using var response = await _httpClient.SendAsync(request);
                if (!response.IsSuccessStatusCode)
                {
                    CadDebugLog.Info($"[UpdateChecker] 버전 체크 실패: HTTP {(int)response.StatusCode}");
                    return;
                }

                string json = await response.Content.ReadAsStringAsync();
                var versionInfo = JsonSerializer.Deserialize<JsonElement>(json);
                string latestVersion = versionInfo.GetProperty("latest_version").GetString() ?? "0.0.0";

                CadDebugLog.Info($"[UpdateChecker] 서버 최신 버전: v{latestVersion}");

                // ── 3. 버전 비교 ─────────────────────────────────────────
                if (!IsNewerVersion(localVersion, latestVersion))
                {
                    CadDebugLog.Info("[UpdateChecker] 이미 최신 버전입니다.");
                    return;
                }

                ed?.WriteMessage($"\n[CAD-Agent] 📦 새 버전 발견: v{localVersion} → v{latestVersion}");
                ed?.WriteMessage("\n[CAD-Agent] 백그라운드에서 업데이트를 다운로드합니다...");

                // ── 4. 플러그인 다운로드 ──────────────────────────────────
                string tempZip = Path.Combine(Path.GetTempPath(), $"CadSllmAgent_v{latestVersion}.zip");

                var downloadRequest = new HttpRequestMessage(HttpMethod.Get, "/api/v1/plugin/download");
                downloadRequest.Headers.Add("X-Api-Key", storedApiKey);

                using var downloadResp = await _httpClient.SendAsync(
                    downloadRequest,
                    HttpCompletionOption.ResponseHeadersRead
                );

                if (!downloadResp.IsSuccessStatusCode)
                {
                    CadDebugLog.Error($"[UpdateChecker] 다운로드 실패: HTTP {(int)downloadResp.StatusCode}");
                    ed?.WriteMessage($"\n[CAD-Agent] ⚠️ 업데이트 다운로드 실패: HTTP {(int)downloadResp.StatusCode}");
                    return;
                }

                using (var sourceStream = await downloadResp.Content.ReadAsStreamAsync())
                using (var destStream = File.Create(tempZip))
                {
                    await sourceStream.CopyToAsync(destStream);
                }

                var fileInfo = new FileInfo(tempZip);
                CadDebugLog.Info($"[UpdateChecker] 다운로드 완료: {tempZip} ({fileInfo.Length / 1024} KB)");

                // ── 5. updater.exe 실행 ──────────────────────────────────
                string contentsDir = GetContentsDirectory();
                string updaterPath = Path.Combine(contentsDir, AgentConfig.UpdaterExeName);

                if (!File.Exists(updaterPath))
                {
                    CadDebugLog.Error($"[UpdateChecker] updater.exe를 찾을 수 없습니다: {updaterPath}");
                    ed?.WriteMessage($"\n[CAD-Agent] ⚠️ updater.exe를 찾을 수 없어 자동 업데이트를 건너뜁니다.");
                    return;
                }

                int acadPid = Process.GetCurrentProcess().Id;

                var psi = new ProcessStartInfo
                {
                    FileName = updaterPath,
                    Arguments = $"{acadPid} \"{tempZip}\" \"{contentsDir}\"",
                    CreateNoWindow = true,
                    UseShellExecute = false,
                    WindowStyle = ProcessWindowStyle.Hidden,
                };

                Process.Start(psi);

                CadDebugLog.Info($"[UpdateChecker] updater.exe 실행됨 (acad PID={acadPid} 종료 대기)");

                ed?.WriteMessage($"\n[CAD-Agent] ✅ 업데이트 v{latestVersion} 다운로드 완료!");
                ed?.WriteMessage("\n[CAD-Agent] AutoCAD를 종료하면 자동으로 업데이트가 적용됩니다.");
            }
            catch (HttpRequestException ex)
            {
                CadDebugLog.Info($"[UpdateChecker] 서버 연결 불가 (오프라인 모드): {ex.Message}");
            }
            catch (TaskCanceledException)
            {
                CadDebugLog.Info("[UpdateChecker] 요청 타임아웃");
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("UpdateChecker.CheckAndPrepareAsync", ex);
            }
        }

        /// <summary>로컬 version.txt 에서 현재 설치 버전을 읽는다.</summary>
        private static string GetLocalVersion()
        {
            try
            {
                string versionFile = Path.Combine(GetContentsDirectory(), AgentConfig.VersionFileName);
                if (File.Exists(versionFile))
                    return File.ReadAllText(versionFile).Trim();
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("UpdateChecker.GetLocalVersion", ex);
            }

            // version.txt 없으면 Assembly 버전 사용
            var asm = Assembly.GetExecutingAssembly();
            return asm.GetName().Version?.ToString(3) ?? "1.0.0";
        }

        /// <summary>현재 DLL이 위치한 디렉터리 (= .bundle/Contents/).</summary>
        private static string GetContentsDirectory()
        {
            string dllPath = Assembly.GetExecutingAssembly().Location;
            return Path.GetDirectoryName(dllPath) ?? AppContext.BaseDirectory;
        }

        /// <summary>저장된 API Key를 반환. 개발 빌드에서는 테스트 키를 허용.</summary>
        private static string GetStoredApiKey()
        {
            try
            {
                string keyFile = Path.Combine(GetContentsDirectory(), "api_key.txt");
                if (File.Exists(keyFile))
                    return File.ReadAllText(keyFile).Trim();
            }
            catch { }
#if DEBUG
            return "1234";
#else
            return string.Empty;
#endif
        }

        /// <summary>SemVer 비교: latest > current 이면 true.</summary>
        private static bool IsNewerVersion(string current, string latest)
        {
            try
            {
                // "1.0.0" → Version 객체
                var currentVer = ParseVersion(current);
                var latestVer = ParseVersion(latest);
                return latestVer > currentVer;
            }
            catch
            {
                // 파싱 실패 시 문자열 비교
                return string.Compare(latest, current, StringComparison.Ordinal) > 0;
            }
        }

        private static Version ParseVersion(string v)
        {
            // "1.0.0" → System.Version (최소 2부분 필요)
            string cleaned = v.Trim().TrimStart('v', 'V');
            return Version.Parse(cleaned);
        }
    }
}
