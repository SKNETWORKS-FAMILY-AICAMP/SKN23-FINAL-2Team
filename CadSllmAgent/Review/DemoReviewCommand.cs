using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using Autodesk.AutoCAD.Runtime;
using CadSllmAgent.Extraction;
using CadSllmAgent.Models;
using CadSllmAgent.UI;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Review
{
    /// <summary>
    /// 백엔드 없이 RevCloud 결과 화면을 눈으로 확인하기 위한 데모 커맨드.
    ///
    /// AutoCAD 명령창에서 실행:
    ///   DEMOREVIEW  — 3개 위반 RevCloud + MText 주석 생성
    ///   DEMOFIX     — 첫 번째 위반(Critical) 승인 → RevCloud 제거
    ///   CLEANDEMO   — AI_REVIEW 레이어 전체 삭제
    /// </summary>
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class DemoReviewCommand
    {
        // ── 데모 데이터 ─────────────────────────────────────────────────
        // 실제 운영에서는 Python ChatResponse.annotated_entities 가 이 구조로 들어옴.
        // Handle은 실제 도면 객체가 없으므로 DEMOFIX 시 "핸들 미발견" 로그가 남는 것이 정상.
        private static readonly List<AnnotatedEntity> DemoEntities = new()
        {
            // ① Critical — 빨강 (ACI 1) — 전기 / 접지선 단면적 부족
            new()
            {
                Handle = "DEMO_HANDLE_1",
                Type   = "BLOCK",
                Layer  = "E-CABLE",
                BBox   = new BoundingBox { X1 = 0,   Y1 = 0,   X2 = 120, Y2 = 60 },
                Violation = new ViolationInfo
                {
                    Id          = "DEMO-V001",
                    Severity    = "Critical",
                    Rule        = "KEC 142.6",
                    Description = "접지선 단면적 부족 (현재 2.5SQ → 최소 4.0SQ 필요)",
                    Suggestion  = "CABLE_SQ 속성값을 2.5 → 4.0 으로 변경",
                    AutoFix     = new AutoFix
                    {
                        Type         = "ATTRIBUTE",
                        AttributeTag = "CABLE_SQ",
                        NewValue     = "4.0"
                    }
                }
            },

            // ② Major — 주황 (ACI 30) — 배관 / 두께 미달
            new()
            {
                Handle = "DEMO_HANDLE_2",
                Type   = "LINE",
                Layer  = "P-PIPE",
                BBox   = new BoundingBox { X1 = 160, Y1 = 0,   X2 = 320, Y2 = 90 },
                Violation = new ViolationInfo
                {
                    Id          = "DEMO-V002",
                    Severity    = "Major",
                    Rule        = "ASME B31.3 § 304.1",
                    Description = "배관 최소 두께 미달 (Sch.40 — 요구: Sch.80)",
                    Suggestion  = "배관 스펙을 Sch.40 → Sch.80 으로 변경",
                    AutoFix     = null   // 자동 수정 불가 — 수동 처리 필요
                }
            },

            // ③ Minor — 노랑 (ACI 50) — 건축 / 복도 폭 부족
            new()
            {
                Handle = "DEMO_HANDLE_3",
                Type   = "DIMENSION",
                Layer  = "A-DIM",
                BBox   = new BoundingBox { X1 = 0,   Y1 = 130, X2 = 200, Y2 = 220 },
                Violation = new ViolationInfo
                {
                    Id          = "DEMO-V003",
                    Severity    = "Minor",
                    Rule        = "건축법 시행령 제48조",
                    Description = "복도 유효 폭 1,150mm — 최소 1,200mm 미달",
                    Suggestion  = "해당 구간 치수 재검토 후 평면 수정",
                    AutoFix     = null
                }
            }
        };

        // ── 커맨드 ───────────────────────────────────────────────────────

        /// <summary>
        /// DEMOREVIEW: 데모 RevCloud 3개를 도면에 그린다.
        /// 실행 후 자동으로 ZOOM EXTENTS 하여 결과를 화면에 맞춤.
        /// </summary>
        /// <summary>
        /// DEMOTEST: DB 접근 없이 명령창에 텍스트만 출력.
        /// 이 메시지가 뜨면 DLL이 정상 로드된 것.
        /// </summary>
        [CommandMethod("DEMOTEST")]
        public void RunDemoTest()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            string status = doc == null ? "도면 없음 — NEW로 도면을 열어주세요" : "OK";
            System.Windows.Forms.MessageBox.Show(
                $"DLL 로드 확인: {status}\n\n" +
                "사용 가능한 명령:\n" +
                "  DEMOREVIEW  — RevCloud 3개 생성\n" +
                "  DEMOSEND    — React에 위반 목록 전송\n" +
                "  DEMOFIX     — V001 RevCloud 제거\n" +
                "  CLEANDEMO   — AI_REVIEW 레이어 초기화",
                "CAD-Agent DEMOTEST",
                System.Windows.Forms.MessageBoxButtons.OK,
                System.Windows.Forms.MessageBoxIcon.Information);
            doc?.Editor.WriteMessage("\n[DEMOTEST] DLL 로드 확인 OK — DEMOREVIEW 실행 가능\n");
        }

        [CommandMethod("DEMOREVIEW")]
        public void RunDemoReview()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                System.Windows.Forms.MessageBox.Show(
                    "열린 도면이 없습니다.\n새 도면(NEW)을 열고 다시 실행하세요.",
                    "DEMOREVIEW", System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Warning);
                return;
            }

            var ed = doc.Editor;
            try
            {
                ed.WriteMessage("\n[DEMO] RevCloud 데모를 시작합니다...\n");
                RevCloudDrawer.DrawAll(DemoEntities);
                ed.WriteMessage("[DEMO]   빨강(Critical) : KEC 142.6 접지선 단면적 부족\n");
                ed.WriteMessage("[DEMO]   주황(Major)    : ASME B31.3 배관 두께 미달\n");
                ed.WriteMessage("[DEMO]   노랑(Minor)    : 건축법 복도 폭 미달\n");
                ed.WriteMessage("[DEMO] DEMOFIX=승인 테스트 / CLEANDEMO=전체 초기화\n");

                doc.SendStringToExecute("ZOOM\nE\n", true, false, true);
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\n[DEMO 오류] {ex.Message}\n{ex.StackTrace}\n");
                System.Windows.Forms.MessageBox.Show(
                    $"DEMOREVIEW 오류:\n{ex.Message}",
                    "DEMOREVIEW Error", System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Error);
            }
        }

        /// <summary>
        /// DEMOFIX: Critical 위반(DEMO-V001)을 승인 처리.
        /// 실제 도면 핸들이 없으므로 도면 수정은 "핸들 미발견"으로 건너뛰고,
        /// RevCloud 제거만 동작하는 것을 확인할 수 있다.
        /// </summary>
        [CommandMethod("DEMOFIX")]
        public void RunDemoFix()
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
            ed?.WriteMessage("\n[DEMO] DEMO-V001 승인 처리 시작...\n");

            // ApplyFix → 핸들 미발견이므로 도면 수정은 skip, RevCloud는 제거됨
            bool result = DrawingPatcher.ApplyFix(DemoEntities[0]);

            if (result)
                ed?.WriteMessage("[DEMO] 도면 수정 + RevCloud 제거 완료\n");
            else
                ed?.WriteMessage("[DEMO] 도면 핸들 미발견(데모 정상) — RevCloud만 제거합니다.\n");

            // 핸들 미발견 시 RevCloud를 직접 제거
            RevCloudDrawer.RemoveCloud("DEMO-V001");
            ed?.WriteMessage("[DEMO] DEMO-V001 RevCloud 제거 완료\n");
        }

        /// <summary>
        /// DEMOSEND: DemoEntities를 DEMO_VIOLATIONS 메시지로 React에 전송.
        /// React가 위반 목록 + 승인/거절 버튼을 표시한다.
        /// 먼저 DEMOREVIEW로 RevCloud를 그린 뒤 실행하면
        /// 승인 클릭 → APPROVE_FIX → DrawingPatcher → RevCloud 제거까지 확인 가능.
        /// </summary>
        [CommandMethod("DEMOSEND")]
        public void RunDemoSend()
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
            ed?.WriteMessage("\n[DEMO] React에 위반 목록 전송 중...\n");

            try
            {
                var payload = JsonSerializer.Serialize(new
                {
                    annotated_entities = DemoEntities
                }, new JsonSerializerOptions
                {
                    PropertyNamingPolicy        = JsonNamingPolicy.SnakeCaseLower,
                    DefaultIgnoreCondition      =
                        System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
                });

                string msg = $"{{\"action\":\"DEMO_VIOLATIONS\",\"payload\":{payload}}}";
                AgentPalette.PostMessage(msg);

                ed?.WriteMessage("[DEMO] React 위반 목록 전송 완료\n");
                ed?.WriteMessage("[DEMO] React에서 승인 클릭 → APPROVE_FIX → RevCloud 제거 확인\n");
            }
            catch (System.Exception ex)
            {
                ed?.WriteMessage($"\n[DEMOSEND 오류] {ex.Message}\n");
            }
        }

        /// <summary>
        /// RUNEXTRACT: 현재 열린 도면에서 엔티티를 추출해 백엔드로 전송한다.
        /// 백엔드가 실행 중이면 ft01_dataset/ 폴더에 JSON이 자동 저장됨.
        /// </summary>
        [CommandMethod("RUNEXTRACT")]
        public void RunExtract()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                doc?.Editor.WriteMessage("\n[RUNEXTRACT] 열린 도면이 없습니다.\n");
                return;
            }

            var ed = doc.Editor;
            ed.WriteMessage("\n[RUNEXTRACT] 도면 데이터 추출 시작...\n");

            try
            {
                var data = CadDataExtractor.Extract(maxEntityCount: null);

                // 타입별 카운트 출력
                var typeCounts = new System.Collections.Generic.Dictionary<string, int>();
                foreach (var e in data.Entities)
                    typeCounts[e.Type] = typeCounts.TryGetValue(e.Type, out int v) ? v + 1 : 1;

                ed.WriteMessage($"\n[RUNEXTRACT] 추출 완료: {data.EntityCount}개 entity, {data.LayerCount}개 레이어\n");
                foreach (var kv in typeCounts)
                    ed.WriteMessage($"  {kv.Key,-15}: {kv.Value}개\n");

                // 백엔드로 전송 (ft01_dataset/ 자동 저장)
                ed.WriteMessage("\n[RUNEXTRACT] AI 서버로 전송 중...\n");
                _ = CadSllmAgent.Services.ApiClient.SendCadDataAsync(data);
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\n[RUNEXTRACT 오류] {ex.Message}\n");
            }
        }

        /// <summary>
        /// CLEANDEMO: AI_REVIEW 레이어의 모든 객체(RevCloud + MText)를 삭제한다.
        /// 도면을 초기 상태로 되돌릴 때 사용.
        /// </summary>
        [CommandMethod("CLEANDEMO")]
        public void RunCleanDemo()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null)
            {
                System.Windows.Forms.MessageBox.Show(
                    "열린 도면이 없습니다. NEW로 도면을 먼저 여세요.",
                    "CLEANDEMO", System.Windows.Forms.MessageBoxButtons.OK,
                    System.Windows.Forms.MessageBoxIcon.Warning);
                return;
            }
            var ed = doc.Editor;
            try
            {
                using var lockDoc = doc.LockDocument();  // ← 필수: 트랜잭션 전 Lock
                using var tr      = doc.Database.TransactionManager.StartTransaction();

                var bt  = (Autodesk.AutoCAD.DatabaseServices.BlockTable)
                          tr.GetObject(doc.Database.BlockTableId,
                                       Autodesk.AutoCAD.DatabaseServices.OpenMode.ForRead);
                var btr = (Autodesk.AutoCAD.DatabaseServices.BlockTableRecord)
                          tr.GetObject(bt[Autodesk.AutoCAD.DatabaseServices.BlockTableRecord.ModelSpace],
                                       Autodesk.AutoCAD.DatabaseServices.OpenMode.ForWrite);

                // AI_REVIEW + AI_REVIEW_LOW 양쪽 모두 청소
                var reviewLayers = new System.Collections.Generic.HashSet<string>
                {
                    RevCloudDrawer.ReviewLayer,
                    RevCloudDrawer.ReviewLayerLow,
                };

                int count = 0;
                var toErase = new List<Autodesk.AutoCAD.DatabaseServices.ObjectId>();

                foreach (var id in btr)
                {
                    var ent = tr.GetObject(id, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForRead)
                              as Autodesk.AutoCAD.DatabaseServices.Entity;
                    if (ent != null && reviewLayers.Contains(ent.Layer))
                    {
                        toErase.Add(id);
                        count++;
                    }
                }

                foreach (var id in toErase)
                {
                    var ent = tr.GetObject(id, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForWrite)
                              as Autodesk.AutoCAD.DatabaseServices.Entity;
                    ent?.Erase();
                }

                tr.Commit();
                ed.WriteMessage($"\n[DEMO] AI_REVIEW 레이어 객체 {count}개 삭제 완료\n");
            }
            catch (System.Exception ex)
            {
                ed.WriteMessage($"\n[CLEANDEMO 오류] {ex.Message}\n");
            }
        }
    }
}
