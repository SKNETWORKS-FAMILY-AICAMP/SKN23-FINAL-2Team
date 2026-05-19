using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Windows.Forms.Integration;
using Autodesk.AutoCAD.Windows;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;
using CadSllmAgent.Extraction;
using CadSllmAgent.Services;

namespace CadSllmAgent.UI
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class AgentPalette
    {
        static PaletteSet? _ps      = null;
        static WebView2?   _webView = null;
        static string      _lastAgent = "";   // 마지막으로 열린 에이전트 ID
        static string?     _resolvedFrontendBaseUrl = null;
        static System.Drawing.Size? _lastPaletteSize = null;
        const int MinimumPaletteWidth = 490;
        const int DefaultPaletteWidth = MinimumPaletteWidth;
        const int DefaultPaletteHeight = 800;
        const int MinimumPaletteHeight = 600;
        static System.Windows.Forms.Timer? _minimumWidthTimer = null;

        // ── 공개 메서드 ───────────────────────────────────────────────

        /// <summary>
        /// 특정 에이전트로 팔레트를 연다.
        /// 이미 열려있으면 에이전트만 전환, 닫혀있으면 열면서 전환.
        /// React에 OPEN_AGENT 메시지를 전송하여 대화기록 리스트를 표시하게 한다.
        /// </summary>
        public static void ShowWithAgent(string agentId)
        {
            if (DispatchIfNeeded(() => ShowWithAgent(agentId))) return;

            _lastAgent = agentId;

            if (!IsPaletteVisible())
                OpenPalette();

            // React에 에이전트 전환 + 현재 DWG 파일명 전달
            string dwgName = GetCurrentDwgName();
            var payload = new
            {
                agent = agentId,
                dwg   = dwgName
            };
            string json = JsonSerializer.Serialize(new { action = "OPEN_AGENT", payload });
            PostMessage(json);
            SyncTempSpecLinkFromDisk();

            Log($"[CAD-Agent] {agentId} 에이전트 활성화 — {dwgName}");
        }

        /// <summary>팔레트를 연다. 이미 열려있으면 무시.</summary>
        public static void Show()
        {
            if (DispatchIfNeeded(Show)) return;

            if (IsPaletteVisible())
                return;

            OpenPalette();
        }

        /// <summary>팔레트 ON/OFF 토글.</summary>
        public static void Toggle()
        {
            if (DispatchIfNeeded(Toggle)) return;

            if (IsPaletteVisible()) ClosePalette();
            else
            {
                OpenPalette();
                // 마지막 에이전트가 있으면 복원
                if (!string.IsNullOrEmpty(_lastAgent))
                    ShowWithAgent(_lastAgent);
            }
        }

        // ── 내부 구현 ─────────────────────────────────────────────────

        private static void OpenPalette()
        {
            try
            {
                SafeTaskDispatcher.CaptureUiDispatcher();
                if (_ps == null)
                {
                    _ps = new PaletteSet(
                        " ",
                        new Guid("F95D460B-B095-55D2-9CD3-2C6AD009E7F7"));

                    _ps.Style      = PaletteSetStyles.ShowAutoHideButton |
                                     PaletteSetStyles.ShowCloseButton;
                    _ps.DockEnabled = DockSides.Left | DockSides.Right;
                    _ps.Dock        = DockSides.Right;
                    _ps.Size        = new System.Drawing.Size(DefaultPaletteWidth, DefaultPaletteHeight);
                    _ps.MinimumSize = new System.Drawing.Size(MinimumPaletteWidth, MinimumPaletteHeight);

                    _ps.StateChanged += (s, e) =>
                    {
                        if (_ps == null) return;

                        if (_ps.Visible)
                            StartMinimumWidthGuard();
                        else
                        {
                            RememberPaletteSize();
                            StopMinimumWidthGuard();
                        }
                    };

                    _webView = new WebView2();
                    SafeTaskDispatcher.CaptureUiDispatcher(_webView.Dispatcher);

                    _webView.CoreWebView2InitializationCompleted += async (s, e) =>
                    {
                        if (!e.IsSuccess)
                        {
                            Log($"[WebView2 초기화 실패]: {e.InitializationException?.Message}");
                            return;
                        }

                        await ClearWebViewFrontendCacheAsync();

                        // React → C# 메시지 수신 (REACT_READY 등)
                        _webView.CoreWebView2.WebMessageReceived += (sender, args) =>
                        {
                            try
                            {
                                var raw = args.TryGetWebMessageAsString();
                                if (string.IsNullOrEmpty(raw)) return;

                                using var doc = System.Text.Json.JsonDocument.Parse(raw);
                                string action = doc.RootElement
                                    .GetProperty("action").GetString() ?? "";

                                if (action == "REACT_READY")
                                    OnReactReady();
                                else if (action == "SESSION_INFO")
                                {
                                    var p = doc.RootElement.GetProperty("payload");
                                    PluginEntry.OrgId    = p.TryGetProperty("org_id",    out var o) ? (o.GetString() ?? "") : "";
                                    PluginEntry.DeviceId = p.TryGetProperty("device_id", out var d) ? (d.GetString() ?? "") : "";
                                    Log($"[CAD-Agent] SESSION_INFO 수신 → OrgId={PluginEntry.OrgId}, DeviceId={PluginEntry.DeviceId}");
                                    DrawingRevisionTracker.RefreshSnapshotBaselineForActiveDocument("session_info");
                                }
                                else if (action == "TEMP_SPEC_SELECTION")
                                {
                                    var payload = doc.RootElement.GetProperty("payload");
                                    HandleTempSpecSelectionPayload(payload);
                                }
                                else if (action == "TEMP_SPEC_LINK_CLEAR_CURRENT")
                                {
                                    HandleTempSpecLinkClearCurrent();
                                }
                                else if (action == "NATIVE_UNMAPPED_ALERT")
                                {
                                    int count     = 0;
                                    string detail = "";
                                    if (doc.RootElement.TryGetProperty("payload", out var up))
                                    {
                                        if (up.TryGetProperty("count",  out var c) && c.ValueKind == JsonValueKind.Number)
                                            count = c.GetInt32();
                                        if (up.TryGetProperty("detail", out var dt))
                                            detail = dt.GetString() ?? "";
                                    }
                                    EnqueueNativeUnmappedDialog(count, string.IsNullOrWhiteSpace(detail) ? null : detail);
                                }
                                else if (action == "CLEAR_CAD_REVIEW")
                                    SocketMessageHandler.ClearAllReviewState();
                                // 목록/세션 전환 시에만 보냄. runDraw의 CLEAR_CAD_REVIEW와 달리
                                // WebView 캐시를 지우지 않으면(선택 취소 후에도) N개가 남는다.
                                else if (action == "CLEAR_SELECTION_CACHE")
                                {
                                    CadDataExtractor.ClearCachedSelection();
                                    
                                    // AutoCAD 실제 그립(파란 네모) 선택 강제 해제
                                    try 
                                    {
                                        var acDoc = AcApp.DocumentManager.MdiActiveDocument;
                                        if (acDoc != null) {
                                            acDoc.Editor.SetImpliedSelection(Array.Empty<Autodesk.AutoCAD.DatabaseServices.ObjectId>());
                                        }
                                    } 
                                    catch { }

                                    _ = Task.Run(async () =>
                                        await ApiClient.BroadcastSelectionAsync(new List<string>()));
                                }
                            }
                            catch (System.Exception ex)
                            {
                                Log($"[CAD-Agent] WebMessage 파싱 오류: {ex.Message}");
                            }
                        };

                        await NavigateFrontendAsync();

                        // 테마 동기화. 에이전트 전환(OPEN_AGENT)은 REACT_READY 수신 후 전송.
                        _webView.NavigationCompleted += (sender, args) =>
                        {
                            if (args.IsSuccess)
                            {
                                SyncTheme();
                            }
                            else
                            {
                                Log($"[WebView2 Navigation 실패]: {args.WebErrorStatus} / {AgentConfig.FrontendBaseUrl}");
                            }
                        };
                    };

                    var host = new ElementHost
                    {
                        Dock  = System.Windows.Forms.DockStyle.Fill,
                        Child = _webView
                    };
                    _ps.Add(" ", host);
                }

                RestorePaletteSize();
                _ps.Visible = true;
                EnforceMinimumPaletteWidth();
                StartMinimumWidthGuard();

                if (_webView != null && _webView.CoreWebView2 == null)
                    _ = InitWebViewAsync();
            }
            catch (System.Exception ex)
            {
                Log($"[팔레트 오류]: {ex.Message}");
            }
        }

        private static void ClosePalette()
        {
            RememberPaletteSize();
            if (_ps != null) _ps.Visible = false;
            StopMinimumWidthGuard();
        }

        private static bool IsPaletteVisible()
        {
            try
            {
                return _ps?.Visible == true;
            }
            catch
            {
                return false;
            }
        }

        private static void RememberPaletteSize()
        {
            if (_ps == null) return;

            var current = _ps.Size;
            if (current.Width <= 0 || current.Height <= 0) return;

            _lastPaletteSize = new System.Drawing.Size(
                Math.Max(current.Width, MinimumPaletteWidth),
                Math.Max(current.Height, MinimumPaletteHeight));
        }

        private static void RestorePaletteSize()
        {
            if (_ps == null || !_lastPaletteSize.HasValue) return;

            var size = _lastPaletteSize.Value;
            _ps.Size = new System.Drawing.Size(
                Math.Max(size.Width, MinimumPaletteWidth),
                Math.Max(size.Height, MinimumPaletteHeight));
        }

        private static void EnforceMinimumPaletteWidth()
        {
            if (_ps == null) return;

            var current = _ps.Size;
            int width = Math.Max(current.Width, MinimumPaletteWidth);
            int height = Math.Max(current.Height, MinimumPaletteHeight);

            if (current.Width == width && current.Height == height)
            {
                RememberPaletteSize();
                return;
            }

            _ps.Size = new System.Drawing.Size(width, height);
            RememberPaletteSize();
        }

        private static void StartMinimumWidthGuard()
        {
            if (_minimumWidthTimer != null) return;

            _minimumWidthTimer = new System.Windows.Forms.Timer { Interval = 150 };
            _minimumWidthTimer.Tick += (s, e) =>
            {
                if (_ps == null || !_ps.Visible)
                {
                    StopMinimumWidthGuard();
                    return;
                }

                EnforceMinimumPaletteWidth();
            };
            _minimumWidthTimer.Start();
        }

        private static void StopMinimumWidthGuard()
        {
            if (_minimumWidthTimer == null) return;

            _minimumWidthTimer.Stop();
            _minimumWidthTimer.Dispose();
            _minimumWidthTimer = null;
        }

        // ── 상태 동기화 ───────────────────────────────────────────────

        /// <summary>
        /// React 마운트 완료(REACT_READY) 수신 시 호출.
        /// 1) 노트북 고유 식별자(MACHINE_INFO)를 먼저 전송한다.
        ///    → React API 등록 창에서 machine_id를 백엔드로 함께 보내어
        ///      devices 테이블에 자동 기록할 수 있게 한다.
        /// 2) 대기 중인 에이전트가 있으면 OPEN_AGENT 메시지를 전송한다.
        /// </summary>
        public static void OnReactReady()
        {
            // ── 1) 노트북 식별 정보 전달 ─────────────────────────────────────────
            var machineJson = JsonSerializer.Serialize(new
            {
                action  = "MACHINE_INFO",
                payload = new
                {
                    machine_id = PluginEntry.MachineId,
                    hostname   = PluginEntry.Hostname,
                    os_user    = PluginEntry.OsUser,
                }
            });
            PostMessage(machineJson);

            // ── 2) 에이전트 복원 ──────────────────────────────────────────────────
            if (string.IsNullOrEmpty(_lastAgent)) return;
            string dwgName = GetCurrentDwgName();
            string agentJson = JsonSerializer.Serialize(new
            {
                action  = "OPEN_AGENT",
                payload = new { agent = _lastAgent, dwg = dwgName }
            });
            PostMessage(agentJson);
        }

        /// <summary>Notify React that the active AutoCAD document changed.</summary>
        public static void NotifyActiveDocumentChanged()
        {
            string dwgName = GetCurrentDwgName();
            string json = JsonSerializer.Serialize(new
            {
                action = "DWG_CHANGED",
                payload = new { dwg = dwgName }
            });
            PostMessage(json);
        }

        private static bool DispatchIfNeeded(Action action)
        {
            var dispatcher = _webView?.Dispatcher ?? System.Windows.Application.Current?.Dispatcher;
            if (dispatcher == null || dispatcher.CheckAccess()) return false;
            dispatcher.InvokeAsync(action);
            return true;
        }

        // ── 유틸 ─────────────────────────────────────────────────────

        /// <summary>현재 활성 DWG 파일명을 반환한다.</summary>
        private static string GetCurrentDwgName()
        {
            try
            {
                var doc = AcApp.DocumentManager.MdiActiveDocument;
                if (doc != null && !string.IsNullOrEmpty(doc.Name))
                    return Path.GetFileName(doc.Name);
            }
            catch { }
            return "untitled.dwg";
        }

        private static async Task<string> ResolveFrontendBaseUrlAsync()
        {
            if (_resolvedFrontendBaseUrl == AgentConfig.LocalFrontendBaseUrl)
                return _resolvedFrontendBaseUrl;

            if (AgentConfig.PreferLocalFrontendWhenAvailable &&
                await IsFrontendReachableAsync(AgentConfig.LocalFrontendBaseUrl))
            {
                _resolvedFrontendBaseUrl = AgentConfig.LocalFrontendBaseUrl;
                return _resolvedFrontendBaseUrl;
            }

            return AgentConfig.FrontendBaseUrl;
        }

        private static async Task<bool> IsFrontendReachableAsync(string url)
        {
            try
            {
                using var client = new System.Net.Http.HttpClient
                {
                    Timeout = TimeSpan.FromMilliseconds(700)
                };
                using var response = await client.GetAsync(url);
                return response.IsSuccessStatusCode;
            }
            catch
            {
                return false;
            }
        }

        private static Uri GetFrontendUri(string baseUrl)
        {
            string separator = baseUrl.Contains("?") ? "&" : "?";
            string token = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds().ToString();
            return new Uri($"{baseUrl}{separator}cad=1&v={token}");
        }

        private static async Task NavigateFrontendAsync()
        {
            if (_webView == null) return;

            string baseUrl = await ResolveFrontendBaseUrlAsync();
            Uri uri = GetFrontendUri(baseUrl);
            if (_webView.CoreWebView2 != null)
                _webView.CoreWebView2.Navigate(uri.ToString());
            else
                _webView.Source = uri;

            Log($"[CAD-Agent] WebView2 Navigate -> {uri}");
        }

        private static async Task ClearWebViewFrontendCacheAsync()
        {
            try
            {
                var profile = _webView?.CoreWebView2?.Profile;
                if (profile == null) return;

                var kinds = CoreWebView2BrowsingDataKinds.DiskCache |
                            CoreWebView2BrowsingDataKinds.CacheStorage |
                            CoreWebView2BrowsingDataKinds.ServiceWorkers;
                await profile.ClearBrowsingDataAsync(kinds);
            }
            catch (System.Exception ex)
            {
                Log($"[WebView2 cache clear failed]: {ex.Message}");
            }
        }

        private static async Task InitWebViewAsync()
        {
            try
            {
                string folder = Path.Combine(Path.GetTempPath(), "CadSllmAgent_WebView2");
                var env = await CoreWebView2Environment.CreateAsync(null, folder, null);
                await _webView!.EnsureCoreWebView2Async(env);
                await NavigateFrontendAsync();
            }
            catch (System.Exception ex)
            {
                Log($"[WebView2 초기화 오류]: {ex.Message}");
            }
        }

        private static void SyncTheme()
        {
            try
            {
                short val   = (short)AcApp.GetSystemVariable("COLORTHEME");
                string theme = val == 0 ? "dark" : "light";
                _webView?.CoreWebView2.PostWebMessageAsJson(
                    $"{{\"action\":\"SET_THEME\",\"payload\":\"{theme}\"}}");
            }
            catch (System.Exception ex)
            {
                Log($"[테마 동기화 오류]: {ex.Message}");
            }
        }

        /// <summary>React(WebView2)에 JSON 메시지를 직접 전송한다.</summary>
        public static void PostMessage(string json)
        {
            if (_webView == null) return;
            if (!_webView.Dispatcher.CheckAccess())
            {
                _webView.Dispatcher.InvokeAsync(() => PostMessage(json));
                return;
            }
            try { _webView.CoreWebView2?.PostWebMessageAsString(json); }
            catch (System.Exception ex) { Log($"[PostMessage 오류]: {ex.Message}"); }
        }

        /// <summary>시방서 모달 등에서 연동 메타 수신 시 DWG 폴더에 기록 후 팔레트 React에 브로드캐스트.</summary>
        public static void HandleTempSpecSelectionPayload(System.Text.Json.JsonElement payload)
        {
            try
            {
                var dwgDir = TempSpecLinkService.GetActiveDwgDirectory();
                if (!string.IsNullOrEmpty(dwgDir))
                    TempSpecLinkService.WriteLinkJson(dwgDir, payload.GetRawText());
                var msg = "{\"action\":\"TEMP_SPEC_LINK_LOADED\",\"payload\":" + payload.GetRawText() + "}";
                PostMessage(msg);
            }
            catch (System.Exception ex)
            {
                Log($"[TEMP_SPEC_SELECTION 처리 오류]: {ex.Message}");
            }
        }

        public static void HandleTempSpecLinkClearCurrent()
        {
            try
            {
                var dwgDir = TempSpecLinkService.GetActiveDwgDirectory();
                if (!string.IsNullOrEmpty(dwgDir))
                    TempSpecLinkService.DeleteLinkJson(dwgDir);
                PostMessage("{\"action\":\"TEMP_SPEC_LINK_CLEARED\"}");
            }
            catch (System.Exception ex)
            {
                Log($"[TEMP_SPEC_LINK_CLEAR_CURRENT 처리 오류]: {ex.Message}");
            }
        }

        /// <summary>디스크의 link.json을 읽어 팔레트 React에 전달 (도면 전환 시).</summary>
        public static void SyncTempSpecLinkFromDisk(string? dwgDir = null)
        {
            try
            {
                dwgDir ??= TempSpecLinkService.GetActiveDwgDirectory();
                if (string.IsNullOrEmpty(dwgDir))
                {
                    PostMessage("{\"action\":\"TEMP_SPEC_LINK_CLEARED\"}");
                    return;
                }
                var json = TempSpecLinkService.ReadLinkJson(dwgDir);
                if (string.IsNullOrEmpty(json))
                {
                    PostMessage("{\"action\":\"TEMP_SPEC_LINK_CLEARED\"}");
                    return;
                }
                var trimmed = json.Trim();
                if (!trimmed.StartsWith("{"))
                {
                    PostMessage("{\"action\":\"TEMP_SPEC_LINK_CLEARED\"}");
                    return;
                }
                PostMessage("{\"action\":\"TEMP_SPEC_LINK_LOADED\",\"payload\":" + trimmed + "}");
            }
            catch (System.Exception ex)
            {
                Log($"[SyncTempSpecLinkFromDisk 오류]: {ex.Message}");
            }
        }

        /// <summary>
        /// React(패널)이 아닌 <b>AutoCAD 본 윈도우</b>에 띄우는 정보 모달(WinForms MessageBox).
        /// WebView2 내부 HTML fixed 레이어는 ACAD 메인과 별도 Z-order라 "모달"처럼 보이지 않는 경우가 있어 사용.
        /// </summary>
        private static void EnqueueNativeUnmappedDialog(int count, string? extraDetail)
        {
            void OnIdleOnce(object? sender, EventArgs e)
            {
                try
                {
                    AcApp.Idle -= OnIdleOnce;
                }
                catch
                {
                    return;
                }

                try
                {
                    const string title = "CAD-SLLM — 미등록 레이어 · 블록명";
                    var body =
                        $"이 도면에서 사전에 매핑되지 않은 레이어·블록명이 {count}개 있습니다.\n\n" +
                        "우측 에이전트 패널에서 표준 용어(배관)에 연결할 수 있습니다.";
                    if (!string.IsNullOrEmpty(extraDetail))
                        body += "\n\n" + extraDetail;
                    MessageBox.Show(
                        body,
                        title,
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Information
                    );
                }
                catch (Exception ex)
                {
                    Log($"[NATIVE_UNMAPPED_ALERT] {ex.Message}");
                }
            }

            try
            {
                SafeTaskDispatcher.EnqueueSafeTask(() =>
                {
                    try { AcApp.Idle += OnIdleOnce; }
                    catch { }
                });
            }
            catch (Exception ex)
            {
                Log($"[NATIVE_UNMAPPED_ALERT] Idle 등록 실패: {ex.Message}");
            }
        }

        private static void Log(string msg) =>
            AcApp.DocumentManager.MdiActiveDocument?.Editor
                .WriteMessage($"\n{msg}\n");
    }
}
