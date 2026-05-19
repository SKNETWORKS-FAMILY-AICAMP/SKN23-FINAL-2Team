/*
 * File    : CadSllmAgent.Installer/Program.cs
 * Create  : 2026-04-28
 * Description : 최초 설치용 독립 실행형 콘솔 앱.
 *
 * 사용법:
 *   CadSllmAgent.Installer.exe [--server <backend_url>] [--api-key <key>]
 *
 * 기본값:
 *   --server  http://localhost:8000
 *   --api-key 없음
 *
 * 동작 흐름:
 *   1. 백엔드에서 최신 버전 정보 조회 (GET /api/v1/plugin/version-check)
 *   2. 플러그인 zip 바이너리 스트리밍 다운로드 (GET /api/v1/plugin/download)
 *   3. %APPDATA%\Autodesk\ApplicationPlugins\CadSllmAgent.bundle\ 폴더 생성
 *   4. PackageContents.xml 생성
 *   5. zip 해제 → Contents/ 폴더에 배치
 *   6. version.txt 생성
 */

using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.Win32;

namespace CadSllmAgent.Installer;

class Program
{
    // ※ 여기만 바꾸면 사용자가 더블클릭만으로 설치 가능
    private const string DEFAULT_SERVER = "http://15.164.110.240:8000";
    private const string DEFAULT_API_KEY = "";
    private const string BUNDLE_NAME = "CadSllmAgent.bundle";
    private const string PRODUCT_CODE = "{E3FAF42F-796B-4C81-9D03-C93ECE8EDD19}";

    static async Task<int> Main(string[] args)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;

        PrintBanner();

        // ── 인수 파싱 ────────────────────────────────────────────────────
        string serverUrl = DEFAULT_SERVER;
        string apiKey = DEFAULT_API_KEY;

        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i].ToLowerInvariant())
            {
                case "--server" when i + 1 < args.Length:
                    serverUrl = args[++i].TrimEnd('/');
                    break;
                case "--api-key" when i + 1 < args.Length:
                    apiKey = args[++i];
                    break;
                case "--help":
                    PrintHelp();
                    return 0;
            }
        }

        Console.WriteLine($"  서버: {serverUrl}");
        if (!string.IsNullOrWhiteSpace(apiKey))
        {
            Console.WriteLine($"  인증: {apiKey[..Math.Min(4, apiKey.Length)]}****");
        }
        else
        {
            Console.WriteLine("  인증: 설치 단계에서는 API Key를 사용하지 않음");
        }
        Console.WriteLine();

        using var http = new HttpClient
        {
            BaseAddress = new Uri(serverUrl),
            Timeout = TimeSpan.FromMinutes(10)
        };
        if (!string.IsNullOrWhiteSpace(apiKey))
        {
            http.DefaultRequestHeaders.Add("X-Api-Key", apiKey);
        }

        try
        {
            // ── 1. 버전 정보 조회 ────────────────────────────────────────
            Console.Write("[1/5] 최신 버전 정보 확인 중... ");
            var versionResp = await http.GetAsync("/api/v1/plugin/version-check");
            if (!versionResp.IsSuccessStatusCode)
            {
                Console.WriteLine("실패!");
                string err = await versionResp.Content.ReadAsStringAsync();
                Console.WriteLine($"  오류: HTTP {(int)versionResp.StatusCode} — {err}");
                Console.WriteLine("\n  서버 주소를 확인해주세요.");
                Console.WriteLine($"  사용법: CadSllmAgent.Installer.exe --server {serverUrl}");
                WaitAndExit(1);
                return 1;
            }

            var versionJson = await versionResp.Content.ReadAsStringAsync();
            var versionInfo = JsonSerializer.Deserialize<JsonElement>(versionJson);
            string latestVersion = versionInfo.GetProperty("latest_version").GetString() ?? "0.0.0";
            long fileSize = 0;
            if (versionInfo.TryGetProperty("file_size", out var fsProp))
                fileSize = fsProp.GetInt64();
            string releaseNotes = "";
            if (versionInfo.TryGetProperty("release_notes", out var rnProp))
                releaseNotes = rnProp.GetString() ?? "";

            Console.WriteLine($"v{latestVersion}");
            if (!string.IsNullOrEmpty(releaseNotes))
                Console.WriteLine($"  릴리스 노트: {releaseNotes}");
            Console.WriteLine();

            // ── 2. 설치 경로 확인 ────────────────────────────────────────
            string appPluginsDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "Autodesk", "ApplicationPlugins"
            );
            string bundleDir = Path.Combine(appPluginsDir, BUNDLE_NAME);
            string contentsDir = Path.Combine(bundleDir, "Contents");

            Console.Write("[2/5] 설치 경로 준비 중... ");

            // 기존 설치 확인
            if (Directory.Exists(contentsDir))
            {
                string versionFile = Path.Combine(contentsDir, "version.txt");
                if (File.Exists(versionFile))
                {
                    string existingVersion = File.ReadAllText(versionFile).Trim();
                    if (existingVersion == latestVersion)
                    {
                        Console.WriteLine($"이미 v{latestVersion} 설치됨!");
                        Console.WriteLine("\n  동일 버전이 이미 설치되어 있습니다.");
                        Console.Write("  재설치하시겠습니까? (y/N): ");
                        var answer = Console.ReadLine()?.Trim().ToLowerInvariant();
                        if (answer != "y" && answer != "yes")
                        {
                            Console.WriteLine("  설치를 취소합니다.");
                            WaitAndExit(0);
                            return 0;
                        }
                    }
                }
            }

            Directory.CreateDirectory(contentsDir);
            Console.WriteLine(bundleDir);
            Console.WriteLine();

            // ── 3. 플러그인 다운로드 ─────────────────────────────────────
            Console.Write($"[3/5] 플러그인 다운로드 중");
            string tempZip = Path.Combine(Path.GetTempPath(), $"CadSllmAgent_v{latestVersion}.zip");

            using (var downloadResp = await http.GetAsync(
                "/api/v1/plugin/download",
                HttpCompletionOption.ResponseHeadersRead))
            {
                if (!downloadResp.IsSuccessStatusCode)
                {
                    Console.WriteLine(" 실패!");
                    string err = await downloadResp.Content.ReadAsStringAsync();
                    Console.WriteLine($"  오류: HTTP {(int)downloadResp.StatusCode} — {err}");
                    WaitAndExit(1);
                    return 1;
                }

                long totalBytes = downloadResp.Content.Headers.ContentLength ?? fileSize;
                long downloaded = 0;

                using var sourceStream = await downloadResp.Content.ReadAsStreamAsync();
                using var destStream = File.Create(tempZip);

                byte[] buffer = new byte[256 * 1024]; // 256KB
                int bytesRead;

                while ((bytesRead = await sourceStream.ReadAsync(buffer, 0, buffer.Length)) > 0)
                {
                    await destStream.WriteAsync(buffer, 0, bytesRead);
                    downloaded += bytesRead;

                    if (totalBytes > 0)
                    {
                        int percent = (int)(downloaded * 100 / totalBytes);
                        Console.Write($"\r[3/5] 플러그인 다운로드 중... {percent}% ({downloaded / 1024:N0} KB / {totalBytes / 1024:N0} KB)  ");
                    }
                    else
                    {
                        Console.Write($"\r[3/5] 플러그인 다운로드 중... {downloaded / 1024:N0} KB  ");
                    }
                }
            }

            Console.WriteLine();
            Console.WriteLine($"  저장: {tempZip}");
            Console.WriteLine();

            // ── 4. ZIP 해제 ──────────────────────────────────────────────
            // build_bundle.ps1 가 Contents/ 내부 파일만 묶어서 zip 생성 (Compress-Archive `Path = $ContentsDir\*`).
            // 따라서 zip 엔트리는 `CadSllmAgent.dll`, `WebView2Loader.dll` 등 루트에 위치.
            // → contentsDir 기준으로 풀어야 Autodesk 표준 구조(bundle/Contents/*.dll)가 완성됨.
            Console.Write("[4/5] 파일 설치 중... ");
            int fileCount = 0;

            using (var archive = ZipFile.OpenRead(tempZip))
            {
                foreach (var entry in archive.Entries)
                {
                    if (string.IsNullOrEmpty(entry.Name))
                        continue;

                    string destPath = Path.Combine(contentsDir, entry.FullName);
                    string? destDir = Path.GetDirectoryName(destPath);
                    if (destDir != null && !Directory.Exists(destDir))
                        Directory.CreateDirectory(destDir);

                    entry.ExtractToFile(destPath, overwrite: true);
                    fileCount++;
                }
            }

            Console.WriteLine($"{fileCount}개 파일 완료.");

            // version.txt 생성
            File.WriteAllText(Path.Combine(contentsDir, "version.txt"), latestVersion);
            Console.WriteLine();

            // ── 5. PackageContents.xml 생성 ──────────────────────────────
            Console.Write("[5/5] AutoCAD 패키지 매니페스트 생성 중... ");
            string packageXml = GeneratePackageContentsXml(latestVersion);
            File.WriteAllText(
                Path.Combine(bundleDir, "PackageContents.xml"),
                packageXml,
                System.Text.Encoding.UTF8
            );
            Console.WriteLine("완료.");
            Console.WriteLine();

            // ── 임시 파일 정리 ───────────────────────────────────────────
            try { File.Delete(tempZip); } catch { }

            // ── 6. AutoCAD 신뢰 경로(TRUSTEDPATHS) 자동 등록 ─────────────
            // AutoCAD 2025+ 는 SECURELOAD 기본값이 강화되어 신뢰 경로 밖 DLL 자동 로드를 silent 차단.
            // 설치된 모든 AutoCAD 버전/프로파일에 contentsDir 을 신뢰 경로로 추가한다.
            Console.Write("[6/6] AutoCAD 신뢰 경로 등록 중... ");
            int profilesUpdated = RegisterAcadTrustedPath(contentsDir);
            if (profilesUpdated > 0)
                Console.WriteLine($"{profilesUpdated}개 프로파일 적용됨.");
            else
                Console.WriteLine("적용 대상 프로파일을 찾지 못함 (AutoCAD 미설치 또는 첫 실행 전).");
            Console.WriteLine();

            // ── 완료 메시지 ──────────────────────────────────────────────
            Console.WriteLine("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
            Console.WriteLine("  ✅ CadSllmAgent 플러그인 설치 완료!");
            Console.WriteLine($"  버전: v{latestVersion}");
            Console.WriteLine($"  경로: {bundleDir}");
            Console.WriteLine();
            Console.WriteLine("  AutoCAD를 시작하면 플러그인이 자동으로 로드됩니다.");
            Console.WriteLine("  이후 업데이트는 플러그인이 자동으로 처리합니다.");
            Console.WriteLine("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");

            WaitAndExit(0);
            return 0;
        }
        catch (HttpRequestException ex)
        {
            Console.WriteLine($"\n\n❌ 서버 연결 실패: {ex.Message}");
            Console.WriteLine($"  서버 주소를 확인해주세요: {serverUrl}");
            WaitAndExit(1);
            return 1;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"\n\n❌ 설치 중 오류 발생: {ex.Message}");
            Console.WriteLine($"  {ex.StackTrace}");
            WaitAndExit(1);
            return 1;
        }
    }

    /// <summary>
    /// AutoCAD 2025+ 의 SECURELOAD 검증을 통과시키기 위해 contentsDir 을 모든 설치된
    /// AutoCAD 버전/프로파일의 TrustedPaths 레지스트리에 추가한다.
    ///
    /// 레지스트리 경로:
    ///   HKCU\Software\Autodesk\AutoCAD\<version>\<release>\Profiles\<profile>\General\TrustedPaths
    ///
    /// <returns>업데이트된 프로파일 개수 (0 이면 AutoCAD 미설치 또는 첫 실행 전 상태).</returns>
    /// </summary>
    private static int RegisterAcadTrustedPath(string trustedPath)
    {
        int updatedCount = 0;
        try
        {
            using var acadRoot = Registry.CurrentUser.OpenSubKey(@"Software\Autodesk\AutoCAD", writable: false);
            if (acadRoot == null) return 0;

            foreach (string versionName in acadRoot.GetSubKeyNames())
            {
                // R24.0, R25.0, R26.0, R27.0 등
                using var versionRoot = acadRoot.OpenSubKey(versionName, writable: false);
                if (versionRoot == null) continue;

                foreach (string releaseName in versionRoot.GetSubKeyNames())
                {
                    // ACAD-7001:412, ACAD-7001:409 등 (제품·언어별)
                    using var profilesKey = versionRoot.OpenSubKey($@"{releaseName}\Profiles", writable: false);
                    if (profilesKey == null) continue;

                    foreach (string profileName in profilesKey.GetSubKeyNames())
                    {
                        using var generalKey = profilesKey.OpenSubKey($@"{profileName}\General", writable: true);
                        if (generalKey == null) continue;

                        string current = (generalKey.GetValue("TrustedPaths") as string) ?? "";
                        bool alreadyPresent = current
                            .Split(';', StringSplitOptions.RemoveEmptyEntries)
                            .Any(p => string.Equals(
                                p.Trim().TrimEnd('\\'),
                                trustedPath.TrimEnd('\\'),
                                StringComparison.OrdinalIgnoreCase));

                        if (alreadyPresent) continue;

                        string newValue = string.IsNullOrEmpty(current)
                            ? trustedPath
                            : current.TrimEnd(';') + ";" + trustedPath;
                        generalKey.SetValue("TrustedPaths", newValue, RegistryValueKind.String);
                        updatedCount++;
                    }
                }
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine();
            Console.WriteLine($"  ⚠️ 신뢰 경로 등록 중 예외: {ex.Message}");
            Console.WriteLine("    AutoCAD 명령창에서 TRUSTEDPATHS 로 직접 추가하세요:");
            Console.WriteLine($"    {trustedPath}");
        }
        return updatedCount;
    }

    private static string GeneratePackageContentsXml(string version)
    {
        return $"""
        <?xml version="1.0" encoding="utf-8"?>
        <ApplicationPackage
            SchemaVersion="1.0"
            AppVersion="{version}"
            ProductCode="{PRODUCT_CODE}"
            Name="CadSllmAgent"
            Description="AI-powered CAD Review Agent"
            Author="SKN23-2TEAM">
            
          <CompanyDetails Name="SKN23-2TEAM"
                          Url="https://github.com/JUYEOP024/SKN23-FINAL-2TEAM"
                          Email="admin@skn23.local" />
          
          <Components>
            <RuntimeRequirements
                OS="Win64"
                Platform="AutoCAD"
                SeriesMin="R24.0"
                SeriesMax="R27.0" />
            <ComponentEntry
                AppName="CadSllmAgent"
                Version="{version}"
                ModuleName="./Contents/CadSllmAgent.dll"
                AppType=".Net"
                LoadOnAutoCADStartup="True" />
          </Components>
        </ApplicationPackage>
        """;
    }

    private static void PrintBanner()
    {
        Console.WriteLine();
        Console.WriteLine("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
        Console.WriteLine("   CadSllmAgent — AutoCAD AI Agent 설치 프로그램");
        Console.WriteLine("   SKN23-FINAL-2TEAM");
        Console.WriteLine("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━");
        Console.WriteLine();
    }

    private static void PrintHelp()
    {
        Console.WriteLine("사용법: CadSllmAgent.Installer.exe [옵션]");
        Console.WriteLine();
        Console.WriteLine("옵션:");
        Console.WriteLine("  --server <url>      백엔드 서버 주소 (기본: http://localhost:8000)");
        Console.WriteLine("  --api-key <key>     옵션: 호환용으로만 사용합니다. 기본값 없음");
        Console.WriteLine("  --help              도움말 표시");
        Console.WriteLine();
        Console.WriteLine("예시:");
        Console.WriteLine("  CadSllmAgent.Installer.exe --server http://15.164.110.240:8000");
    }

    private static void WaitAndExit(int code)
    {
        Console.WriteLine();
        Console.WriteLine("아무 키나 누르면 종료됩니다...");
        Console.ReadKey(intercept: true);
    }
}
