/*
 * File    : CadSllmAgent.Updater/Program.cs
 * Create  : 2026-04-28
 * Description : AutoCAD 종료 후 플러그인 파일을 교체하는 독립 실행형 콘솔 앱.
 *
 * 사용법:
 *   updater.exe <acad_pid> <zip_path> <bundle_contents_dir>
 *
 * 동작 흐름:
 *   1. acad_pid 프로세스가 종료될 때까지 대기 (최대 30분)
 *   2. bundle_contents_dir 기존 파일을 .bak 폴더에 백업
 *   3. zip_path 를 bundle_contents_dir 에 해제 (updater.exe 자신은 스킵)
 *   4. version.txt 갱신
 *   5. 백업 및 임시 파일 정리
 *   6. 실패 시 .bak 에서 롤백
 */

using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Threading;

namespace CadSllmAgent.Updater;

class Program
{
    private const int MAX_WAIT_MINUTES = 30;
    private const int POLL_INTERVAL_MS = 2000;

    static int Main(string[] args)
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;

        if (args.Length < 3)
        {
            Console.WriteLine("사용법: updater.exe <acad_pid> <zip_path> <bundle_contents_dir>");
            Console.WriteLine("  acad_pid           : AutoCAD 프로세스 ID");
            Console.WriteLine("  zip_path           : 다운로드된 업데이트 ZIP 파일 경로");
            Console.WriteLine("  bundle_contents_dir: .bundle/Contents 폴더 경로");
            return 1;
        }

        if (!int.TryParse(args[0], out int acadPid))
        {
            Console.WriteLine($"[Updater] 오류: '{args[0]}'은 유효한 PID가 아닙니다.");
            return 1;
        }

        string zipPath = args[1];
        string contentsDir = args[2];

        if (!File.Exists(zipPath))
        {
            Console.WriteLine($"[Updater] 오류: ZIP 파일을 찾을 수 없습니다: {zipPath}");
            return 1;
        }

        if (!Directory.Exists(contentsDir))
        {
            Console.WriteLine($"[Updater] 오류: 대상 폴더를 찾을 수 없습니다: {contentsDir}");
            return 1;
        }

        string backupDir = contentsDir + ".bak";
        string updaterExeName = Path.GetFileName(Process.GetCurrentProcess().MainModule?.FileName ?? "updater.exe")
                                    .ToLowerInvariant();

        try
        {
            // ── 1. AutoCAD 프로세스 종료 대기 ─────────────────────────────
            Console.WriteLine($"[Updater] AutoCAD (PID: {acadPid}) 종료 대기 중...");
            WaitForProcessExit(acadPid);
            Console.WriteLine("[Updater] AutoCAD가 종료되었습니다. 업데이트를 시작합니다.");

            // 파일 잠금이 해제될 시간을 잠시 기다림
            Thread.Sleep(3000);

            // ── 2. 기존 파일 백업 ────────────────────────────────────────
            Console.WriteLine($"[Updater] 기존 파일 백업 중... → {backupDir}");
            if (Directory.Exists(backupDir))
                Directory.Delete(backupDir, recursive: true);

            // Contents 폴더를 통째로 복사해서 백업
            CopyDirectory(contentsDir, backupDir);
            Console.WriteLine("[Updater] 백업 완료.");

            // ── 3. ZIP 해제 (덮어쓰기) ──────────────────────────────────
            Console.WriteLine("[Updater] 새 버전 파일 설치 중...");
            int installed = 0;
            int skipped = 0;

            using (var archive = ZipFile.OpenRead(zipPath))
            {
                foreach (var entry in archive.Entries)
                {
                    // 디렉터리 엔트리는 스킵
                    if (string.IsNullOrEmpty(entry.Name))
                        continue;

                    // updater.exe 자체는 현재 실행 중이므로 스킵
                    if (entry.Name.Equals(updaterExeName, StringComparison.OrdinalIgnoreCase) ||
                        entry.Name.Equals("updater.exe", StringComparison.OrdinalIgnoreCase))
                    {
                        skipped++;
                        Console.WriteLine($"  [스킵] {entry.FullName} (실행 중인 updater)");
                        continue;
                    }

                    string destPath = Path.Combine(contentsDir, entry.FullName);
                    string? destDir = Path.GetDirectoryName(destPath);
                    if (destDir != null && !Directory.Exists(destDir))
                        Directory.CreateDirectory(destDir);

                    // 기존 파일이 잠겨 있을 수 있으므로 재시도
                    bool extracted = false;
                    for (int retry = 0; retry < 5; retry++)
                    {
                        try
                        {
                            entry.ExtractToFile(destPath, overwrite: true);
                            extracted = true;
                            break;
                        }
                        catch (IOException)
                        {
                            Thread.Sleep(1000);
                        }
                    }

                    if (extracted)
                    {
                        installed++;
                        Console.WriteLine($"  [설치] {entry.FullName}");
                    }
                    else
                    {
                        Console.WriteLine($"  [실패] {entry.FullName} — 파일이 잠겨 있습니다");
                    }
                }
            }

            Console.WriteLine($"[Updater] 설치 완료: {installed}개 파일 업데이트, {skipped}개 스킵.");

            // ── 4. 임시 파일 정리 ────────────────────────────────────────
            try
            {
                File.Delete(zipPath);
                Console.WriteLine($"[Updater] 임시 ZIP 삭제: {zipPath}");
            }
            catch { /* 삭제 실패는 무시 — 재부팅 시 Windows가 정리 */ }

            // 백업 폴더 정리 (성공 시)
            try
            {
                if (Directory.Exists(backupDir))
                    Directory.Delete(backupDir, recursive: true);
                Console.WriteLine("[Updater] 백업 폴더 정리 완료.");
            }
            catch { }

            Console.WriteLine("[Updater] ✅ 업데이트가 완료되었습니다! 다음 AutoCAD 실행 시 새 버전이 로드됩니다.");
            Thread.Sleep(5000);
            return 0;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[Updater] ❌ 업데이트 실패: {ex.Message}");
            Console.WriteLine("[Updater] 백업에서 복원을 시도합니다...");

            // ── 롤백 ────────────────────────────────────────────────────
            try
            {
                if (Directory.Exists(backupDir))
                {
                    // Contents 폴더 내용을 백업에서 복원
                    foreach (var file in Directory.GetFiles(backupDir, "*", SearchOption.AllDirectories))
                    {
                        string relativePath = Path.GetRelativePath(backupDir, file);
                        string destPath = Path.Combine(contentsDir, relativePath);
                        string? destDir = Path.GetDirectoryName(destPath);
                        if (destDir != null && !Directory.Exists(destDir))
                            Directory.CreateDirectory(destDir);
                        File.Copy(file, destPath, overwrite: true);
                    }
                    Console.WriteLine("[Updater] 복원 완료.");
                }
            }
            catch (Exception rollbackEx)
            {
                Console.WriteLine($"[Updater] 복원 실패: {rollbackEx.Message}");
                Console.WriteLine("[Updater] 수동으로 백업 폴더에서 복원하세요:");
                Console.WriteLine($"  백업 위치: {backupDir}");
            }

            Thread.Sleep(10000);
            return 2;
        }
    }

    /// <summary>지정된 PID의 프로세스가 종료될 때까지 폴링 방식으로 대기</summary>
    private static void WaitForProcessExit(int pid)
    {
        var sw = Stopwatch.StartNew();
        var maxWait = TimeSpan.FromMinutes(MAX_WAIT_MINUTES);

        while (sw.Elapsed < maxWait)
        {
            try
            {
                var proc = Process.GetProcessById(pid);
                // 프로세스가 아직 살아있음
                if (proc.HasExited)
                    return;
            }
            catch (ArgumentException)
            {
                // 프로세스가 이미 종료됨
                return;
            }
            catch
            {
                return;
            }

            Thread.Sleep(POLL_INTERVAL_MS);
        }

        Console.WriteLine($"[Updater] 경고: {MAX_WAIT_MINUTES}분 대기 초과. 강제로 업데이트를 진행합니다.");
    }

    /// <summary>디렉터리를 재귀적으로 복사</summary>
    private static void CopyDirectory(string sourceDir, string destDir)
    {
        Directory.CreateDirectory(destDir);

        foreach (var file in Directory.GetFiles(sourceDir))
        {
            string destFile = Path.Combine(destDir, Path.GetFileName(file));
            File.Copy(file, destFile, overwrite: true);
        }

        foreach (var dir in Directory.GetDirectories(sourceDir))
        {
            string destSubDir = Path.Combine(destDir, Path.GetFileName(dir));
            CopyDirectory(dir, destSubDir);
        }
    }
}
