/*
 * File    : CadSllmAgent/commands/AgentCommands.cs
 * Author  : 김지우
 * Create  : 2026-04-06
 * Description : 
 * AutoCAD 커맨드 정의 및 UI 호출 로직.
 * AGENT_SPEC_MANAGE 커맨드를 통한 시방서 관리 모달 호출 추가.
 *
 * Modification History :
 * - 2026-04-06 (김지우) : 시방서 관리 커맨드(AGENT_SPEC_MANAGE) 구현
 * - 2026-04-15 (김지우) : AI 수정 테스트용 커맨드(TEST_AI_FIX) 및 참조(using) 추가
 */
using System;
using System.IO;
using System.Text;
using Autodesk.AutoCAD.Runtime;
using Autodesk.AutoCAD.ApplicationServices;
using CadSllmAgent.Extraction;
using CadSllmAgent.UI;
using CadSllmAgent.Services;

// 추가된 필수 참조 네임스페이스
using CadSllmAgent.Models;
using CadSllmAgent.Review;

using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

//[assembly: CommandClass(typeof(CadSllmAgent.commands.AgentCommands))]

namespace CadSllmAgent.commands
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class AgentCommands
    {
        private static string ExportCadJson(bool selectedOnly)
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument
                ?? throw new InvalidOperationException("No active AutoCAD document.");

            var data = selectedOnly
                ? CadDataExtractor.ExtractSelected()
                : CadDataExtractor.Extract(maxEntityCount: null);

            if (data == null)
                throw new InvalidOperationException("No selected entities were found. Select objects first or use CADJSONEXPORT.");

            var exportDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CadSllmAgent",
                "exports");
            Directory.CreateDirectory(exportDir);

            var drawingName = Path.GetFileNameWithoutExtension(doc.Name);
            if (string.IsNullOrWhiteSpace(drawingName))
                drawingName = "drawing";

            foreach (var ch in Path.GetInvalidFileNameChars())
                drawingName = drawingName.Replace(ch, '_');

            var scope = selectedOnly ? "selected" : "full";
            var fileName = $"{drawingName}_{scope}_{DateTime.Now:yyyyMMdd_HHmmss}.json";
            var path = Path.Combine(exportDir, fileName);

            File.WriteAllText(
                path,
                CadDataExtractor.ToJson(data),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

            return path;
        }

        // ── 에이전트별 AutoCAD 커맨드 ────────────────────────────────
        // 커맨드 라인에서 직접 타이핑하거나, 상단 메뉴바 클릭 시 실행됨

        [CommandMethod("AELEC")]
        public void OpenElec() => AgentPalette.ShowWithAgent("전기");

        [CommandMethod("APIPE")]
        public void OpenPipe() => AgentPalette.ShowWithAgent("배관");

        [CommandMethod("AARCH")]
        public void OpenArch() => AgentPalette.ShowWithAgent("건축");

        [CommandMethod("AFIRE")]
        public void OpenFire() => AgentPalette.ShowWithAgent("소방");


        // ── 유틸리티 ─────────────────────────────────────────────────

        /// <summary>팔레트 토글 (열려있으면 닫고, 닫혀있으면 마지막 에이전트로 열기)</summary>
        [CommandMethod("AGENTTOGGLE")]
        public void ToggleAgent() => AgentPalette.Toggle();

        [CommandMethod("CADJSONEXPORT")]
        public void ExportCurrentDrawingJson()
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
            try
            {
                var path = ExportCadJson(selectedOnly: false);
                CadDebugLog.Info($"CADJSONEXPORT saved: {path}");
                ed?.WriteMessage($"\n[CAD-Agent] JSON export saved: {path}\n");
            }
            catch (System.Exception ex)
            {
                CadDebugLog.Exception("CADJSONEXPORT", ex);
                ed?.WriteMessage($"\n[CAD-Agent] JSON export failed: {ex.Message}\n");
            }
        }

        [CommandMethod("CADJSONEXPORTSEL")]
        public void ExportSelectedDrawingJson()
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
            try
            {
                var path = ExportCadJson(selectedOnly: true);
                CadDebugLog.Info($"CADJSONEXPORTSEL saved: {path}");
                ed?.WriteMessage($"\n[CAD-Agent] selected JSON export saved: {path}\n");
            }
            catch (System.Exception ex)
            {
                CadDebugLog.Exception("CADJSONEXPORTSEL", ex);
                ed?.WriteMessage($"\n[CAD-Agent] selected JSON export failed: {ex.Message}\n");
            }
        }


        // ── 시방서 및 API Key 관리 (모달 창) ──────────────────────────
        
        [CommandMethod("AGENT_SPEC_MANAGE")]
        public void ManageSpec()
        {
            SpecReactWindow modalWindow = new SpecReactWindow("spec");
            AcApp.ShowModalWindow(modalWindow);
        }

        [CommandMethod("AGENT_KEY_MANAGE")]
        public void ManageApiKey()
        {
            SpecReactWindow modalWindow = new SpecReactWindow("api");
            AcApp.ShowModalWindow(modalWindow);
        }


        // ── 테스트용 커맨드 (개발 중) - 테스트후 삭제 예정 ──────────────────────────────
        [CommandMethod("TEST_AI_FIX")]
        public void TestAiFix()
        {
            // 1. 테스트용 위반 데이터 생성 (실제 도면에 존재하는 Handle 번호를 써야 함)
            var testEntity = new AnnotatedEntity
            {
                Handle = "29A", // 주의: 테스트 시 도면에 실제 있는 16진수 핸들로 변경하세요
                Violation = new ViolationInfo
                {
                    Id = "TEST-V-01",
                    AutoFix = new AutoFix
                    {
                        Type = "MOVE",
                        DeltaX = 500.0,
                        DeltaY = 0.0
                    }
                }
            };

            // 2. 패처 호출
            bool result = DrawingPatcher.ApplyFix(testEntity);

            // CS0104 에러 방지를 위해 Application 대신 AcApp 사용
            AcApp.DocumentManager.MdiActiveDocument.Editor.WriteMessage($"\n테스트 결과: {result}");
        }
        // ────────────────────────────────────────────────────────────
    }
}
