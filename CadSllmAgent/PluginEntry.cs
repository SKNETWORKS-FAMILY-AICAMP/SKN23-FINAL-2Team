using System;
using Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Interop;
using Autodesk.AutoCAD.Interop.Common;
using CadSllmAgent.UI;
using CadSllmAgent.Services;
using CadSllmAgent.Extraction;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class PluginEntry : IExtensionApplication
    {
        /// <summary>
        /// Windows Registry의 MachineGuid — 노트북 고유 식별자.
        /// API 등록 시 백엔드에 전달되어 devices 테이블에 자동 기록됩니다.
        /// </summary>
        public static string MachineId { get; private set; } = "";
        public static string Hostname  { get; private set; } = Environment.MachineName;
        public static string OsUser    { get; private set; } = Environment.UserName;
        public static string OrgId     { get; set; } = "";
        public static string DeviceId  { get; set; } = "";

        public void Initialize()
        {
            try
            {
                MachineId = ReadMachineGuid();
                AcApp.Idle += Application_Idle;
                
                // ✨ [추가됨] 문서 전환/오픈 시 자동으로 감지하기 위한 이벤트 등록
                AcApp.DocumentManager.DocumentActivated += OnDocumentActivated;
            }
            catch { }
        }

        public void Terminate()
        {
            AcApp.DocumentManager.DocumentActivated -= OnDocumentActivated;
            DrawingRevisionTracker.UnhookDocumentEvents();
            DrawingRevisionTracker.ResetForDocumentSwitch();
            SocketClient.StopAndDispose();
        }

        private static void OnDocumentActivated(object? sender, DocumentCollectionEventArgs e)
        {
            try
            {
                // 1. 도면이 바뀌었으니 React의 위반사항 UI를 비워라!
                AgentPalette.PostMessage("{\"action\":\"CLEAR_REVIEW_UI\"}");
                AgentPalette.NotifyActiveDocumentChanged();
                DrawingRevisionTracker.UnhookDocumentEvents();
                DrawingRevisionTracker.ResetForDocumentSwitch();

                // 2. 새 Document에 ImpliedSelectionChanged 재등록 (문서 전환 시 이벤트 복원)
                //    기존 Document의 핸들러는 HookSelectionEvents 내부에서 자동 해제됨
                // 이거 지우면 가만 안둠 !!!!!!!! (중요중요중요)
                CadDataExtractor.HookSelectionEvents(forceRehook: true);
                DrawingRevisionTracker.RefreshSnapshotBaselineForActiveDocument("document_activated");

                var dir = TempSpecLinkService.GetActiveDwgDirectory();
                AgentPalette.SyncTempSpecLinkFromDisk(dir);
            }
            catch (System.Exception ex)
            {
                CadDebugLog.Exception("OnDocumentActivated", ex);
            }
        }

        /// <summary>
        /// HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid 에서
        /// Windows 설치 시 생성된 고유 UUID를 읽습니다.
        /// 읽기 실패 시 "MachineName-UserName" 문자열로 대체합니다.
        /// </summary>
        private static string ReadMachineGuid()
        {
            try
            {
                using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
                    @"SOFTWARE\Microsoft\Cryptography", writable: false);
                var guid = key?.GetValue("MachineGuid") as string;
                if (!string.IsNullOrWhiteSpace(guid))
                    return guid.Trim().ToLowerInvariant();
            }
            catch { }
            return $"{Environment.MachineName}-{Environment.UserName}".ToLowerInvariant();
        }

        private int _menuRetryCount = 0;
        private const int MAX_MENU_RETRIES = 10;
        private System.Timers.Timer? _menuRetryTimer;

        private void Application_Idle(object? sender, EventArgs e)
        {
            try
            {
                AcApp.Idle -= Application_Idle;

                SafeTaskDispatcher.CaptureUiDispatcher();
                CadDataExtractor.HookSelectionEvents();

                // 플러그인 자동 업데이트 체크 (논블로킹 — 실패해도 플러그인 동작에 영향 없음)
                _ = UpdateChecker.CheckAndPrepareAsync();

                try
                {
                    AgentPalette.Show();
                }
                catch (System.Exception ex)
                {
                    CadDebugLog.Exception("AgentPalette.Show", ex);
                    Log($"[CAD-Agent] 팔레트 로드 오류: {ex.Message}");
                }

                _ = SocketClient.ConnectAsync();

                // 메뉴 생성: 타이머 기반 지연 재시도 (2초 간격)
                TryCreateMenuWithDelay();
            }
            catch (System.Exception ex)
            {
                CadDebugLog.Exception("Application_Idle", ex);
                Log($"[CAD-Agent] 초기화 오류: {ex.Message}\n{ex.StackTrace}");
            }
        }

        private void TryCreateMenuWithDelay()
        {
            bool created = CreateTopMenuBar();
            if (created)
            {
                CadDebugLog.Info("Agent 메뉴 생성 성공");
                return;
            }

            if (_menuRetryCount >= MAX_MENU_RETRIES)
            {
                CadDebugLog.Info($"Agent 메뉴 생성 실패 ({MAX_MENU_RETRIES}회 시도). 명령창에서 직접 접근하세요.");
                return;
            }

            _menuRetryCount++;
            CadDebugLog.Info($"메뉴 생성 재시도 예약 ({_menuRetryCount}/{MAX_MENU_RETRIES}) - 2초 후");
            _menuRetryTimer = new System.Timers.Timer(2000);
            _menuRetryTimer.AutoReset = false;
            _menuRetryTimer.Elapsed += (s, args) =>
            {
                SafeTaskDispatcher.EnqueueSafeTask(() =>
                {
                    try
                    {
                        AcApp.Idle += MenuRetry_Idle;
                    }
                    catch { }
                });
            };
            _menuRetryTimer.Start();
        }

        private void MenuRetry_Idle(object? sender, EventArgs e)
        {
            AcApp.Idle -= MenuRetry_Idle;
            TryCreateMenuWithDelay();
        }

        private bool CreateTopMenuBar()
        {
            try
            {
                try
                {
                    AcApp.SetSystemVariable("MENUBAR", 1);
                }
                catch { }

                var acadApp = AcApp.AcadApplication as Autodesk.AutoCAD.Interop.AcadApplication;
                if (acadApp == null) return false;

                var menuGroup = acadApp.MenuGroups.Item(0);
                if (menuGroup == null) return false;

                var popMenus = menuGroup.Menus;
                if (popMenus == null) return false;

                try
                {
                    var existingMenu = popMenus.Item("Agent");
                    existingMenu.RemoveFromMenuBar();
                }
                catch { }

                try
                {
                    var agentMenu = popMenus.Add("Agent");
                    agentMenu.AddMenuItem(agentMenu.Count, "전기 에이전트", "AELEC ");
                    agentMenu.AddMenuItem(agentMenu.Count, "배관 에이전트", "APIPE ");
                    agentMenu.AddMenuItem(agentMenu.Count, "건축 에이전트", "AARCH ");
                    agentMenu.AddMenuItem(agentMenu.Count, "소방 에이전트", "AFIRE ");
                    agentMenu.AddSeparator(agentMenu.Count);
                    agentMenu.AddMenuItem(agentMenu.Count, "시방서 관리...", "AGENT_SPEC_MANAGE ");
                    agentMenu.AddMenuItem(agentMenu.Count, "API키 관리...", "AGENT_KEY_MANAGE ");
                    agentMenu.InsertInMenuBar(acadApp.MenuBar.Count);
                    return true;
                }
                catch { return false; }
            }
            catch { return false; }
        }

        private static void Log(string msg) =>
            AcApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage($"\n{msg}\n");
    }
}
