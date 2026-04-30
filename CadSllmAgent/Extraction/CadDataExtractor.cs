/*
 * File    : CadSllmAgent/Extraction/CadDataExtractor.cs
 * Author  : 김다빈
 * WBS     : EXT-01 (도면 데이터 추출), EXE-02 (검토 범위 선택)
 * Create  : 2026-04-06
 *
 * Description :
 *   AutoCAD Model Space에서 entity를 읽어 CadDrawingData(JSON)로 변환한다.
 *
 *   [EXT-01] 추출 항목:
 *     공통     — handle, type, layer, bbox, color, linetype, lineweight
 *     LINE     — start, end, length, angle
 *     CIRCLE   — center, radius, diameter
 *     ARC      — center, radius, start_angle, end_angle, arc_length
 *     POLYLINE — vertices[], is_closed, perimeter, area, constant_width
 *     SPLINE   — fit_points[], is_closed
 *     ELLIPSE  — center, major_axis, minor_ratio, start_angle, end_angle
 *     BLOCK    — block_name, attributes, insert_point, rotation, scale_x, scale_y, scale_z
 *     MTEXT    — text, insert_point, text_height, text_width, rotation, attachment_point
 *     TEXT     — text, insert_point, text_height, rotation
 *     DIMENSION— measurement, dim_type, dim_text, dim_style,
 *                xline1_point, xline2_point, dim_line_point, text_position
 *     HATCH    — pattern_name, pattern_scale, pattern_angle, hatch_area, boundary_count
 *     SOLID    — corner1~4
 *     MLEADER  — text, text_height, start(화살표끝), end(텍스트연결점)
 *
 *   [EXE-02] 검토 범위 — OFF/FREEZE 레이어 제외:
 *     레이어명 표준이 없어 이름 기반 분류 불가.
 *     사용자가 AutoCAD에서 불필요한 레이어를 끄면 자동으로 제외됨.
 *     도메인 관련성 판단(전기/배관/건축 등)은 LLM이 내용을 보고 결정한다.
 *
 *   [C# 필터링 3단계] LLM 토큰 절감:
 *     1 — OFF/FREEZE 레이어 제거       (EXE-02)
 *     2 — bbox 면적 < MinBBoxArea 제거 (점·마킹 노이즈)
 *     3 — maxEntityCount 제한(기본 1500) — null이면 전수 수집(분할 POST는 ApiClient)
 *
 *   handle: DWG 내 고유 16진수 ID. Python이 돌려주면 C#이 객체를 찾아 수정.
 *   bbox  : 도면 좌표 기준 최소/최대 x,y. RevCloud 위치 계산에 사용.
 *
 * LangGraph: `PipingToolNames.GetCadEntityInfo` / `CallReviewAgent` 공통 JSON 소스
 * (PipingToolBridge.cs).
 *
 * Modification History :
 *   - 2026-04-15 (김다빈) : elapsed_ms 제거 (LLM 노이즈), drawing_unit 추출 추가 (SFT 거리 판단용)
 *   - 2026-04-19 (김지우) : C# active_object_ids 추출 로직 추가
 *   - 2026-04-22 : Extract(maxEntityCount) — null이면 캡 없음(청크 업로드용)
 *   - 2026-04-25 : ExtractLayoutRegions() 추가 (Paper Space 뷰포트 → 모델 좌표 범위)
 *                  Extract()에서 entity마다 layout_name 할당
 */

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using Autodesk.AutoCAD.ApplicationServices;
using CadSllmAgent.Services;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using CadSllmAgent.Models;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Extraction
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public static class CadDataExtractor
    {
        private const double MinBBoxDimension = 0.001;

        private static readonly HashSet<string> SupportedTypes = new(StringComparer.OrdinalIgnoreCase)
        {
            "LINE", "ARC", "CIRCLE", "POLYLINE", "LWPOLYLINE",
            "INSERT", "MTEXT", "TEXT", "DIMENSION",
            "SPLINE", "ELLIPSE", "SOLID", "HATCH", "MLEADER"
        };

        // 도메인 인식 추출 우선순위 — 배관(MEP) 레이어와 건축(A-/S-) 레이어를 구분해
        // 캡 적용 시 중요 엔티티가 먼저 선택되도록 점수화.
        //   Priority 0(최고) — MEP/배관 레이어 BLOCK (장비, 부속)
        //   Priority 1       — MEP/배관 레이어 기하 (LINE, ARC, CIRCLE, POLYLINE)
        //   Priority 2       — 건축(A-/S-) 레이어 기하 (벽, 슬라브 기준선)
        //   Priority 3       — 기타 레이어 기하
        //   Priority 4       — TEXT/MTEXT/MLEADER (보조 정보)
        //   Priority 5(최저) — DIMENSION/HATCH/SOLID (치수·해치)
        private static readonly System.Text.RegularExpressions.Regex _archPrefixRe =
            new(@"^(A-|S-)", System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.Compiled);

        private static readonly System.Text.RegularExpressions.Regex _mepHintRe =
            new(@"(GAS|PIPE|PIPING|MEP|HVAC|SPRINK|CWS|HWS|CWR|HWR|DRAIN|VAV|TEX$|DUAL|CHW|HOT|COLD|FW|RISER|VALVE|MANHOLE|DUCT|PUMP|PFW|HFG|FIRE|FM-|FP-|ELEC-F)",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.Compiled);

        private static int GetExtractionPriority(string entityType, string layerName)
        {
            bool isMep  = _mepHintRe.IsMatch(layerName ?? "");
            bool isArch = !isMep && _archPrefixRe.IsMatch(layerName ?? "");

            return entityType.ToUpperInvariant() switch
            {
                "BLOCK" or "INSERT" when isMep  => 0,  // 배관 장비·부속
                "LINE"   or "ARC"    or "CIRCLE"
                    or "POLYLINE" when isMep     => 1,  // 배관 기하
                "LINE"   or "ARC"    or "CIRCLE"
                    or "POLYLINE" when isArch    => 2,  // 건축 기준선
                "BLOCK" or "INSERT"              => 3,  // 기타 블록
                "LINE"   or "ARC"    or "CIRCLE"
                    or "POLYLINE"                => 3,  // 기타 기하
                "MTEXT"  or "TEXT"   or "MLEADER" => 4,
                _                               => 5,  // DIMENSION, HATCH, SOLID 등
            };
        }

        private static HashSet<string> _cachedSelectionHandles = new(StringComparer.OrdinalIgnoreCase);
        /// <summary>직전에 브로드캐스트한 핸들 집합 — 동일 선택 반복 전송 방지.</summary>
        private static HashSet<string> _lastBroadcastedHandles = new(StringComparer.OrdinalIgnoreCase);
        /// <summary>현재 이벤트가 등록된 Document. 문서 전환 시 재등록에 사용.</summary>
        private static Document? _hookedDoc = null;
        private static bool _eventHooked = false;

        /// <summary>
        /// ImpliedSelectionChanged 이벤트를 현재 활성 Document에 등록한다.
        /// <para><paramref name="forceRehook"/>=true이면 이미 등록된 경우에도 새 Document로 재등록한다.
        /// 도면 파일 전환(DocumentActivated) 시 반드시 forceRehook=true로 호출할 것.</para>
        /// </summary>
        public static void HookSelectionEvents(bool forceRehook = false)
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;

            // 같은 Document에 이미 등록되어 있고 강제 재등록이 아니면 스킵
            if (_eventHooked && !forceRehook && ReferenceEquals(_hookedDoc, doc)) return;

            try
            {
                // 이전 Document의 핸들러 해제 (중복 등록 방지)
                if (_hookedDoc != null)
                {
                    _hookedDoc.ImpliedSelectionChanged -= OnImpliedSelectionChanged;
                }

                doc.ImpliedSelectionChanged += OnImpliedSelectionChanged;
                _hookedDoc   = doc;
                _eventHooked = true;
                CadDebugLog.Info($"[CadDataExtractor] ImpliedSelectionChanged 등록 완료: {doc.Name}");
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HookSelectionEvents", ex);
            }
        }

        private static void OnImpliedSelectionChanged(object? sender, EventArgs e)
        {
            try
            {
                var doc = AcApp.DocumentManager.MdiActiveDocument;
                if (doc == null) return;

                var ed = doc.Editor;
                PromptSelectionResult res = ed.SelectImplied();

                if (res.Status == PromptStatus.OK && res.Value.Count > 0)
                {
                    var broadcast = new List<string>();
                    foreach (ObjectId id in res.Value.GetObjectIds())
                        broadcast.Add(id.Handle.ToString());

                    // ✨ [추가] 구름마크 클릭 감지: 단일 선택이고 AI_REVIEW 레이어인 경우
                    if (res.Value.Count == 1)
                    {
                        ObjectId firstId = res.Value.GetObjectIds()[0];
                        using (var tr = doc.Database.TransactionManager.StartTransaction())
                        {
                            var ent = tr.GetObject(firstId, OpenMode.ForRead) as Entity;
                            if (ent != null && (ent.Layer == "AI_REVIEW" || ent.Layer == "AI_REVIEW_LOW"))
                            {
                                // XData에서 violation_id 추출
                                var xdata = ent.GetXDataForApplication("CADSLLM");
                                if (xdata != null)
                                {
                                    var vids = new List<string>();
                                    foreach (TypedValue tv in xdata)
                                    {
                                        if (tv.TypeCode == (int)DxfCode.ExtendedDataAsciiString)
                                            vids.Add(tv.Value?.ToString() ?? "");
                                    }
                                    if (vids.Count > 0)
                                    {
                                        // React에 상세 모달 요청 (첫 번째 ID 기준)
                                        var modalMsg = JsonSerializer.Serialize(new
                                        {
                                            action = "SHOW_VIOLATION_DETAIL",
                                            payload = new { violation_id = vids[0] }
                                        });
                                        CadSllmAgent.UI.AgentPalette.PostMessage(modalMsg);
                                    }
                                }
                            }
                            tr.Commit();
                        }
                    }

                    // 직전과 동일한 선택이면 불필요한 HTTP POST 생략
                    if (broadcast.Count == _lastBroadcastedHandles.Count &&
                        broadcast.All(h => _lastBroadcastedHandles.Contains(h)))
                        return;

                    _cachedSelectionHandles = new HashSet<string>(broadcast, StringComparer.OrdinalIgnoreCase);
                    _lastBroadcastedHandles = new HashSet<string>(broadcast, StringComparer.OrdinalIgnoreCase);
                    _ = Task.Run(async () => await ApiClient.BroadcastSelectionAsync(broadcast));
                    return;
                }

                // Implied가 비어 있을 때(ESC 누름 or 빈 도면 클릭)
                // UX상 ESC를 눌렀을 때 프론트엔드도 선택이 풀리는 것이 맞으므로 동기화합니다.
                if (_lastBroadcastedHandles.Count == 0) return; // 이미 빈 상태 → 전송 불필요

                _cachedSelectionHandles.Clear();
                _lastBroadcastedHandles.Clear();
                _ = Task.Run(async () => await ApiClient.BroadcastSelectionAsync(new List<string>()));
            }
            catch { }
        }

        public static void ClearCachedSelection()
        {
            _cachedSelectionHandles.Clear();
        }

        public static List<string> GetActiveSelectionHandles()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return new List<string>();

            var ed = doc.Editor;
            PromptSelectionResult selectionResult = ed.SelectImplied();
            if (selectionResult.Status == PromptStatus.OK && selectionResult.Value.Count > 0)
            {
                var handles = new List<string>();
                using var tr = doc.Database.TransactionManager.StartTransaction();
                foreach (ObjectId id in selectionResult.Value.GetObjectIds())
                {
                    handles.Add(id.Handle.ToString());
                }
                tr.Commit();

                _cachedSelectionHandles = new HashSet<string>(handles, StringComparer.OrdinalIgnoreCase);
                return handles;
            }

            if (_cachedSelectionHandles.Count > 0)
            {
                ed.WriteMessage($"\n[CAD-Agent] 캐시된 선택 사용: {_cachedSelectionHandles.Count}개 객체\n");
                return new List<string>(_cachedSelectionHandles);
            }

            return new List<string>();
        }

        public static CadDrawingData? ExtractSelected()
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return null;

            var db = doc.Database;
            var ed = doc.Editor;

            var selectedHandles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            PromptSelectionResult selResult = ed.SelectImplied();
            if (selResult.Status == PromptStatus.OK && selResult.Value.Count > 0)
            {
                foreach (ObjectId id in selResult.Value.GetObjectIds())
                    selectedHandles.Add(id.Handle.ToString());
                _cachedSelectionHandles = new HashSet<string>(selectedHandles, StringComparer.OrdinalIgnoreCase);
            }
            else if (_cachedSelectionHandles.Count > 0)
            {
                selectedHandles = new HashSet<string>(_cachedSelectionHandles, StringComparer.OrdinalIgnoreCase);
                ed.WriteMessage($"\n[CAD-Agent] 캐시된 선택 사용: {selectedHandles.Count}개 객체\n");
            }

            if (selectedHandles.Count == 0) return null;

            var result = new CadDrawingData { DrawingUnit = GetDrawingUnit(db) };

            using var tr = db.TransactionManager.StartTransaction();

            var layerTable = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            var layerState = new Dictionary<string, bool[]>();
            foreach (ObjectId layerId in layerTable)
            {
                var lr = (LayerTableRecord)tr.GetObject(layerId, OpenMode.ForRead);
                layerState[lr.Name] = new[] { lr.IsOff, lr.IsFrozen };
            }

            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

            foreach (ObjectId id in btr)
            {
                Entity? ent = null;
                try
                {
                    if (!selectedHandles.Contains(id.Handle.ToString())) continue;

                    ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                    if (ent == null) continue;

                    if (layerState.TryGetValue(ent.Layer, out var ls) && (ls[0] || ls[1])) continue;

                    var cadEnt = BuildCadEntity(tr, ent);
                    if (cadEnt == null) continue;

                    result.Entities.Add(cadEnt);
                }
                catch (Autodesk.AutoCAD.Runtime.Exception ex)
                {
                    string handleStr = ent != null ? ent.Handle.ToString() : id.Handle.ToString();
                    string typeStr = ent != null ? ent.GetType().Name : "Unknown";
                    ed.WriteMessage($"\n[EXT-SEL 예외 무시] 핸들: {handleStr}, 타입: {typeStr}, 사유: {ex.ErrorStatus}");
                }
            }

            tr.Commit();

            result.EntityCount = result.Entities.Count;
            result.LayerCount = result.Layers.Count;

            ed.WriteMessage($"\n[EXT-SEL] 선택 추출 완료: {result.EntityCount}개 entity\n");
            return result;
        }

        /// <param name="maxEntityCount">null이면 모델스페이스 엔티티를 전수 수집(HTTP 분할은 ApiClient).지정 시 면적 큰 순으로 상한 적용(기본 1500).</param>
        public static CadDrawingData Extract(int? maxEntityCount = 5000)
        {
            var sw = Stopwatch.StartNew();
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            var db = doc.Database;
            var ed = doc.Editor;

            var result = new CadDrawingData
            {
                DrawingUnit = GetDrawingUnit(db)
            };

            using var tr = db.TransactionManager.StartTransaction();

            // Paper Space 레이아웃/뷰포트 범위 추출 (DWG에 다수 도면 배치 시 구분용)
            var layouts = ExtractLayoutRegions(db, tr);
            result.Layouts.AddRange(layouts);
            if (layouts.Count > 0)
                ed.WriteMessage($"\n[EXT-01] Paper Space 레이아웃 {layouts.Count}개 뷰포트 감지\n");

            var layerTable = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            var layerState = new Dictionary<string, bool[]>();

            foreach (ObjectId layerId in layerTable)
            {
                var lr = (LayerTableRecord)tr.GetObject(layerId, OpenMode.ForRead);
                layerState[lr.Name] = new[] { lr.IsOff, lr.IsFrozen };
            }

            var bt = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            var btr = (BlockTableRecord)tr.GetObject(bt[BlockTableRecord.ModelSpace], OpenMode.ForRead);

            var layerCountMap = new Dictionary<string, int>();
            var rawEntities = new List<(CadEntity ent, double area, int priority)>();

            int skippedLayer = 0, skippedArea = 0, errorCount = 0;

            foreach (ObjectId id in btr)
            {
                Entity? ent = null;
                try
                {
                    ent = tr.GetObject(id, OpenMode.ForRead) as Entity;
                    if (ent == null) continue;

                    if (layerState.TryGetValue(ent.Layer, out var ls) && (ls[0] || ls[1]))
                    { skippedLayer++; continue; }

                    var cadEnt = BuildCadEntity(tr, ent);
                    if (cadEnt == null) continue;

                    // 엔티티 중심이 어느 레이아웃 뷰포트 범위에 속하는지 판단
                    if (layouts.Count > 0 && cadEnt.BBox != null)
                    {
                        double cx = (cadEnt.BBox.X1 + cadEnt.BBox.X2) / 2.0;
                        double cy = (cadEnt.BBox.Y1 + cadEnt.BBox.Y2) / 2.0;
                        foreach (var lay in layouts)
                        {
                            if (lay.Contains(cx, cy)) { cadEnt.LayoutName = lay.Name; break; }
                        }
                    }

                    double bboxW = 0, bboxH = 0;
                    if (cadEnt.BBox != null)
                    {
                        bboxW = Math.Abs(cadEnt.BBox.X2 - cadEnt.BBox.X1);
                        bboxH = Math.Abs(cadEnt.BBox.Y2 - cadEnt.BBox.Y1);
                        if (bboxW < MinBBoxDimension && bboxH < MinBBoxDimension)
                        { skippedArea++; continue; }
                    }
                    double area = Math.Max(bboxW, bboxH);
                    int priority = GetExtractionPriority(cadEnt.Type ?? "", ent.Layer ?? "");

                    rawEntities.Add((cadEnt, area, priority));
                    layerCountMap.TryGetValue(ent.Layer ?? "", out int cnt);
                    layerCountMap[ent.Layer ?? ""] = cnt + 1;
                }
                catch (Autodesk.AutoCAD.Runtime.Exception ex)
                {
                    // ✨ 핵심 방어 로직: 에러가 나면 멈추지 않고 핸들 번호를 출력한 뒤 다음 객체로 넘어감
                    errorCount++;
                    string handleStr = ent != null ? ent.Handle.ToString() : id.Handle.ToString();
                    string typeStr = ent != null ? ent.GetType().Name : "Unknown";
                    ed.WriteMessage($"\n[EXT-01 오류 건너뜀] 핸들: {handleStr}, 타입: {typeStr}, 사유: {ex.ErrorStatus}");
                    continue;
                }
            }

            int cap = maxEntityCount ?? int.MaxValue;
            // 도메인 인식 정렬: priority 낮을수록(중요도 높음) 먼저, 같은 priority는 면적 큰 순.
            // cap 없으면(전수) 순서 유지(원본 순서로 커밋).
            IEnumerable<CadEntity> finalEntities = rawEntities.Count <= cap
                ? rawEntities.Select(t => t.ent)
                : rawEntities
                    .OrderBy(t => t.priority)
                    .ThenByDescending(t => t.area)
                    .Take(cap)
                    .Select(t => t.ent);

            foreach (var e in finalEntities) result.Entities.Add(e);
            int skippedCap = Math.Max(0, rawEntities.Count - result.Entities.Count);

            foreach (ObjectId layerId in layerTable)
            {
                var lr = (LayerTableRecord)tr.GetObject(layerId, OpenMode.ForRead);
                if (!layerCountMap.TryGetValue(lr.Name, out int count)) count = 0;

                string colorStr = lr.Color.ColorMethod switch
                {
                    Autodesk.AutoCAD.Colors.ColorMethod.ByLayer => "BYLAYER",
                    Autodesk.AutoCAD.Colors.ColorMethod.ByBlock => "BYBLOCK",
                    _ => lr.Color.ColorIndex.ToString()
                };

                string lwStr = lr.LineWeight switch
                {
                    LineWeight.ByLayer => "BYLAYER",
                    LineWeight.ByBlock => "BYBLOCK",
                    _ => (int)lr.LineWeight < 0 ? "DEFAULT" : $"{(int)lr.LineWeight / 100.0:F2}mm"
                };

                string? linetypeName = null;
                try
                {
                    if (lr.LinetypeObjectId != ObjectId.Null)
                    {
                        var ltRec = tr.GetObject(lr.LinetypeObjectId, OpenMode.ForRead) as LinetypeTableRecord;
                        linetypeName = ltRec?.Name;
                    }
                }
                catch { }

                result.Layers.Add(new CadLayer
                {
                    Name = lr.Name,
                    EntityCount = count,
                    IsOn = !lr.IsOff && !lr.IsFrozen,
                    IsLocked = lr.IsLocked,
                    Color = colorStr,
                    Linetype = linetypeName,
                    Lineweight = lwStr,
                    IsPlottable = lr.IsPlottable,
                });
            }

            tr.Commit();
            sw.Stop();

            result.EntityCount = result.Entities.Count;
            result.LayerCount = result.Layers.Count;

            int mepCount  = rawEntities.Count(t => t.priority <= 1);
            int archCount = rawEntities.Count(t => t.priority == 2);
            ed.WriteMessage(
                $"\n[EXT-01] 추출 완료: {result.EntityCount}개 entity, " +
                $"{result.LayerCount}개 레이어, {result.DrawingUnit}, {sw.ElapsedMilliseconds}ms\n" +
                $"  도메인: MEP/배관={mepCount}, 건축(A-/S-)={archCount}, 기타={rawEntities.Count - mepCount - archCount}\n" +
                $"  제외됨: 레이어OFF={skippedLayer}, 면적미달={skippedArea}, 수량캡={skippedCap}, 오류={errorCount}\n");

            return result;
        }

        public static string ToJson(CadDrawingData data) =>
            JsonSerializer.Serialize(data, new JsonSerializerOptions
            {
                WriteIndented = false,
                DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
            });

        private static CadEntity? BuildCadEntity(Transaction tr, Entity ent)
        {
            string typeName = GetTypeName(ent);
            if (!SupportedTypes.Contains(typeName)) return null;

            var bbox = GetBBox(ent);
            if (bbox == null) return null;

            var cadEnt = new CadEntity
            {
                Handle = ent.Handle.ToString(),
                Type = typeName,
                Layer = ent.Layer,
                BBox = bbox,
                Color = GetColorString(ent),
                Linetype = ent.Linetype,
                Lineweight = GetLineweightString(ent),
            };

            switch (ent)
            {
                case Line line:
                    FillLine(cadEnt, line);
                    break;
                case Circle circle:
                    FillCircle(cadEnt, circle);
                    break;
                case Arc arc:
                    FillArc(cadEnt, arc);
                    break;
                case Polyline pl:
                    FillPolyline(cadEnt, pl);
                    break;
                case Spline spline:
                    FillSpline(cadEnt, spline);
                    break;
                case Ellipse ellipse:
                    FillEllipse(cadEnt, ellipse);
                    break;
                case BlockReference bref:
                    cadEnt.Type = "BLOCK";
                    FillBlock(tr, cadEnt, bref);
                    break;
                case MText mt:
                    cadEnt.Type = "MTEXT";
                    FillMText(cadEnt, mt);
                    break;
                case DBText dt:
                    cadEnt.Type = "TEXT";
                    FillText(cadEnt, dt);
                    break;
                case Dimension dim:
                    cadEnt.Type = "DIMENSION";
                    FillDimension(cadEnt, dim);
                    break;
                case Hatch hatch:
                    cadEnt.Type = "HATCH";
                    FillHatch(cadEnt, hatch);
                    break;
                case Solid solid:
                    cadEnt.Type = "SOLID";
                    FillSolid(cadEnt, solid);
                    break;
                case MLeader mleader:
                    cadEnt.Type = "MLEADER";
                    FillMLeader(cadEnt, mleader);
                    break;
            }

            return cadEnt;
        }

        private static void FillLine(CadEntity c, Line l)
        {
            c.Start = Pt(l.StartPoint);
            c.End = Pt(l.EndPoint);
            c.Length = Math.Round(l.Length, 4);
            c.Angle = Math.Round(l.Angle * 180.0 / Math.PI, 4);
        }

        private static void FillCircle(CadEntity c, Circle ci)
        {
            c.Center = Pt(ci.Center);
            c.Radius = Math.Round(ci.Radius, 4);
            c.Diameter = Math.Round(ci.Radius * 2, 4);
        }

        private static void FillArc(CadEntity c, Arc a)
        {
            c.Center = Pt(a.Center);
            c.Radius = Math.Round(a.Radius, 4);
            c.StartAngle = Math.Round(a.StartAngle * 180.0 / Math.PI, 4);
            c.EndAngle = Math.Round(a.EndAngle * 180.0 / Math.PI, 4);
            double centralAngle = a.EndAngle > a.StartAngle
                ? a.EndAngle - a.StartAngle
                : (Math.PI * 2 - a.StartAngle + a.EndAngle);
            c.ArcLength = Math.Round(a.Radius * centralAngle, 4);
        }

        private static void FillPolyline(CadEntity c, Polyline pl)
        {
            var verts = new List<CadVertex>();
            for (int i = 0; i < pl.NumberOfVertices; i++)
            {
                var p = pl.GetPoint2dAt(i);
                verts.Add(new CadVertex
                {
                    X = Math.Round(p.X, 4),
                    Y = Math.Round(p.Y, 4),
                    Bulge = Math.Round(pl.GetBulgeAt(i), 6)
                });
            }
            c.Vertices = verts;
            c.IsClosed = pl.Closed;
            try { c.Perimeter = Math.Round(pl.Length, 4); } catch { }
            try { if (pl.Closed) c.Area = Math.Round(pl.Area, 4); } catch { }
            if (pl.ConstantWidth > 0)
                c.ConstantWidth = Math.Round(pl.ConstantWidth, 4);
        }

        private static void FillSpline(CadEntity c, Spline sp)
        {
            c.IsClosed = sp.Closed;
            if (sp.NumFitPoints > 0)
            {
                var pts = new List<CadPoint>();
                for (int i = 0; i < sp.NumFitPoints; i++)
                    pts.Add(Pt(sp.GetFitPointAt(i)));
                c.FitPoints = pts;
            }
        }

        private static void FillEllipse(CadEntity c, Ellipse el)
        {
            c.Center = Pt(el.Center);
            c.MajorAxis = new CadPoint
            {
                X = Math.Round(el.MajorAxis.X, 4),
                Y = Math.Round(el.MajorAxis.Y, 4)
            };
            c.MinorRatio = Math.Round(el.RadiusRatio, 6);
            c.StartAngle = Math.Round(el.StartAngle * 180.0 / Math.PI, 4);
            c.EndAngle = Math.Round(el.EndAngle * 180.0 / Math.PI, 4);
        }

        private static void FillBlock(Transaction tr, CadEntity c, BlockReference bref)
        {
            c.BlockName = bref.Name;
            c.InsertPoint = new CadPoint
            {
                X = Math.Round(bref.Position.X, 4),
                Y = Math.Round(bref.Position.Y, 4)
            };
            c.Rotation = Math.Round(bref.Rotation * 180.0 / Math.PI, 4);
            c.ScaleX = Math.Round(bref.ScaleFactors.X, 6);
            c.ScaleY = Math.Round(bref.ScaleFactors.Y, 6);
            c.ScaleZ = Math.Round(bref.ScaleFactors.Z, 6);
            c.Attributes = ExtractAttributes(tr, bref);
        }

        private static void FillMText(CadEntity c, MText mt)
        {
            // mt.Text에 MText 서식 코드({\\fArial;} 등)가 남아있는 경우를 방어
            // CleanMText()로 순수 텍스트만 추출하여 LLM에 전달
            c.Text = CleanMText(mt.Text ?? mt.Contents ?? "");
            c.InsertPoint = new CadPoint
            {
                X = Math.Round(mt.Location.X, 4),
                Y = Math.Round(mt.Location.Y, 4)
            };
            c.TextHeight = Math.Round(mt.TextHeight, 4);
            c.TextWidth = Math.Round(mt.Width, 4);
            c.Rotation = Math.Round(mt.Rotation * 180.0 / Math.PI, 4);
            c.AttachmentPoint = mt.Attachment.ToString();
        }

        private static void FillText(CadEntity c, DBText dt)
        {
            c.Text = dt.TextString;
            c.InsertPoint = new CadPoint
            {
                X = Math.Round(dt.Position.X, 4),
                Y = Math.Round(dt.Position.Y, 4)
            };
            c.TextHeight = Math.Round(dt.Height, 4);
            c.Rotation = Math.Round(dt.Rotation * 180.0 / Math.PI, 4);
        }

        private static void FillDimension(CadEntity c, Dimension dim)
        {
            // ✨ 치수선 값 추출 시 예외 방어
            try { c.Measurement = Math.Round(dim.Measurement, 4); } catch { c.Measurement = 0; }

            c.DimText = string.IsNullOrEmpty(dim.DimensionText) ? null : dim.DimensionText;
            c.DimStyle = dim.DimensionStyleName;

            try { c.TextPosition = Pt(dim.TextPosition); } catch { }

            switch (dim)
            {
                case RotatedDimension rd:
                    c.DimType = "LINEAR";
                    try { c.Xline1Point = Pt(rd.XLine1Point); } catch { }
                    try { c.Xline2Point = Pt(rd.XLine2Point); } catch { }
                    try { c.DimLinePoint = Pt(rd.DimLinePoint); } catch { }
                    break;
                case AlignedDimension ad:
                    c.DimType = "ALIGNED";
                    try { c.Xline1Point = Pt(ad.XLine1Point); } catch { }
                    try { c.Xline2Point = Pt(ad.XLine2Point); } catch { }
                    try { c.DimLinePoint = Pt(ad.DimLinePoint); } catch { }
                    break;
                case RadialDimension rad:
                    c.DimType = "RADIAL";
                    try { c.Xline1Point = Pt(rad.Center); } catch { }
                    try { c.Xline2Point = Pt(rad.ChordPoint); } catch { }
                    break;
                case RadialDimensionLarge radl:
                    c.DimType = "RADIAL_LARGE";
                    try { c.Xline1Point = Pt(radl.Center); } catch { }
                    try { c.Xline2Point = Pt(radl.ChordPoint); } catch { }
                    break;
                case LineAngularDimension2 ang:
                    c.DimType = "ANGULAR";
                    try { c.Xline1Point = Pt(ang.XLine1Start); } catch { }
                    try { c.Xline2Point = Pt(ang.XLine2Start); } catch { }
                    break;
                case OrdinateDimension ord:
                    c.DimType = "ORDINATE";
                    try { c.Xline1Point = Pt(ord.Origin); } catch { }
                    try { c.Xline2Point = Pt(ord.DefiningPoint); } catch { }
                    break;
                default:
                    c.DimType = "UNKNOWN";
                    break;
            }
        }

        private static void FillHatch(CadEntity c, Hatch h)
        {
            c.PatternName = h.PatternName;
            c.PatternScale = Math.Round(h.PatternScale, 4);
            c.PatternAngle = Math.Round(h.PatternAngle * 180.0 / Math.PI, 4);
            c.BoundaryCount = h.NumberOfLoops;
            try { c.HatchArea = Math.Round(h.Area, 4); } catch { }
        }

        private static void FillSolid(CadEntity c, Solid s)
        {
            try { c.Corner1 = Pt(s.GetPointAt(0)); } catch { }
            try { c.Corner2 = Pt(s.GetPointAt(1)); } catch { }
            try { c.Corner3 = Pt(s.GetPointAt(2)); } catch { }
            try { c.Corner4 = Pt(s.GetPointAt(3)); } catch { }
        }

        private static void FillMLeader(CadEntity c, MLeader ml)
        {
            if (ml.ContentType == ContentType.MTextContent && ml.MText != null)
            {
                c.Text = ml.MText.Text;
                c.TextHeight = Math.Round(ml.MText.TextHeight, 4);
            }
            try
            {
                if (ml.LeaderCount > 0)
                {
                    int leaderIndex = 0;
                    Point3d startPoint = ml.GetFirstVertex(leaderIndex);
                    c.Start = Pt(startPoint);

                    Point3d endPoint = ml.GetLastVertex(leaderIndex);
                    c.End = Pt(endPoint);
                }
            }
            catch { }
        }

        private static string GetDrawingUnit(Database db)
        {
            return db.Insunits switch
            {
                UnitsValue.Millimeters => "mm",
                UnitsValue.Centimeters => "cm",
                UnitsValue.Meters => "m",
                UnitsValue.Inches => "inch",
                UnitsValue.Feet => "feet",
                _ => "unknown"
            };
        }

        private static CadBBox? GetBBox(Entity ent)
        {
            try
            {
                var ext = ent.GeometricExtents;
                return new CadBBox
                {
                    X1 = Math.Round(ext.MinPoint.X, 4),
                    Y1 = Math.Round(ext.MinPoint.Y, 4),
                    X2 = Math.Round(ext.MaxPoint.X, 4),
                    Y2 = Math.Round(ext.MaxPoint.Y, 4),
                };
            }
            catch { return null; }
        }

        private static Dictionary<string, string>? ExtractAttributes(Transaction tr, BlockReference bref)
        {
            if (bref.AttributeCollection.Count == 0) return null;
            var attrs = new Dictionary<string, string>();
            foreach (ObjectId attId in bref.AttributeCollection)
            {
                var att = tr.GetObject(attId, OpenMode.ForRead) as AttributeReference;
                if (att != null) attrs[att.Tag] = att.TextString;
            }
            return attrs.Count > 0 ? attrs : null;
        }

        // AutoCAD ACI 표준 7색 — LLM이 전선 상(phase) 색상을 인식할 수 있도록 이름 병기
        private static readonly Dictionary<int, string> _aciColorNames = new()
        {
            { 1, "RED"     },
            { 2, "YELLOW"  },
            { 3, "GREEN"   },
            { 4, "CYAN"    },
            { 5, "BLUE"    },
            { 6, "MAGENTA" },
            { 7, "WHITE"   },
        };

        /// <summary>
        /// entity의 색상을 문자열로 반환합니다.
        /// ACI 표준 7색은 "N(NAME)" 형식(예: "1(RED)")으로, 나머지는 인덱스 숫자만 반환합니다.
        /// </summary>
        private static string GetColorString(Entity ent)
        {
            return ent.Color.ColorMethod switch
            {
                Autodesk.AutoCAD.Colors.ColorMethod.ByLayer => "BYLAYER",
                Autodesk.AutoCAD.Colors.ColorMethod.ByBlock => "BYBLOCK",
                _ => _aciColorNames.TryGetValue(ent.Color.ColorIndex, out var name)
                     ? $"{ent.Color.ColorIndex}({name})"
                     : ent.Color.ColorIndex.ToString()
            };
        }

        // MText 서식 코드 제거용 정규식
        // {\fArial|b0|i0|c0|p34;}, {\C1;}, \P(단락구분), \\(이스케이프 백슬래시) 등 제거
        private static readonly System.Text.RegularExpressions.Regex _mtextFormatRe =
            new System.Text.RegularExpressions.Regex(
                @"\{\\[^}]*\}|\\[A-Za-z]\d*;|\\[Pp]",
                System.Text.RegularExpressions.RegexOptions.Compiled
            );

        /// <summary>
        /// MText 서식 코드를 제거하고 순수 텍스트를 반환합니다.
        /// mt.Text가 이미 클린하면 그대로 사용하고, 서식 코드가 남아있으면 정규식으로 제거합니다.
        /// </summary>
        private static string CleanMText(string rawText)
        {
            if (string.IsNullOrEmpty(rawText)) return rawText ?? "";
            // 서식 코드가 없으면 바로 반환 (성능 최적화)
            if (!rawText.Contains('\\') && !rawText.Contains('{'))
                return rawText.Trim();
            var cleaned = _mtextFormatRe.Replace(rawText, " ");
            // 연속 공백 정리 및 줄바꿈 통일
            cleaned = System.Text.RegularExpressions.Regex.Replace(cleaned, @"\s+", " ");
            return cleaned.Trim();
        }

        private static string GetLineweightString(Entity ent)
        {
            return ent.LineWeight switch
            {
                LineWeight.ByLayer => "BYLAYER",
                LineWeight.ByBlock => "BYBLOCK",
                _ => (int)ent.LineWeight < 0
                                        ? "DEFAULT"
                                        : $"{(int)ent.LineWeight / 100.0:F2}mm"
            };
        }

        private static CadPoint Pt(Point3d p) =>
            new() { X = Math.Round(p.X, 4), Y = Math.Round(p.Y, 4), Z = Math.Round(p.Z, 4) };

        private static string GetTypeName(Entity ent) => ent switch
        {
            Line => "LINE",
            Arc => "ARC",
            Circle => "CIRCLE",
            Polyline => "POLYLINE",
            Polyline2d => "POLYLINE",
            Polyline3d => "POLYLINE",
            BlockReference => "INSERT",
            MText => "MTEXT",
            DBText => "TEXT",
            Dimension => "DIMENSION",
            Spline => "SPLINE",
            Ellipse => "ELLIPSE",
            Hatch => "HATCH",
            Solid => "SOLID",
            MLeader => "MLEADER",
            _ => ent.GetType().Name.ToUpper()
        };
    /// <summary>
    /// Paper Space 레이아웃에 포함된 뷰포트들이 참조하는 모델 좌표 범위를 추출한다.
    ///
    /// 하나의 DWG에 여러 도면(층별 평면도 등)이 Model Space에 배치되고
    /// 각 Layout이 해당 영역을 Viewport로 참조하는 일반적인 구성에서,
    /// 각 entity가 어느 도면(Layout 이름)에 속하는지 판별하는 데 사용한다.
    ///
    /// 반환 목록이 비면: Model Space 전용 DWG이거나 Paper Space 없음.
    /// </summary>
    public static List<DrawingLayout> ExtractLayoutRegions(Database db, Transaction tr)
    {
        var result = new List<DrawingLayout>();
        try
        {
            var layoutDict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
            foreach (System.Collections.DictionaryEntry entry in layoutDict)
            {
                Layout? layout = null;
                try { layout = entry.Value is ObjectId oid ? tr.GetObject(oid, OpenMode.ForRead) as Layout : null; }
                catch { }
                if (layout == null) continue;
                if (layout.LayoutName.Equals("Model", StringComparison.OrdinalIgnoreCase)) continue;

                var btr = (BlockTableRecord)tr.GetObject(layout.BlockTableRecordId, OpenMode.ForRead);
                foreach (ObjectId vpId in btr)
                {
                    Viewport? vp = null;
                    try { vp = tr.GetObject(vpId, OpenMode.ForRead) as Viewport; }
                    catch { }

                    // Number==1 은 전체 페이퍼 뷰포트(출력 영역 자체) → 건너뜀
                    if (vp == null || vp.Number <= 1) continue;

                    double scale  = vp.CustomScale > 0 ? vp.CustomScale : 1.0;
                    double halfW  = (vp.Width  / scale) / 2.0;
                    double halfH  = (vp.Height / scale) / 2.0;
                    double cx     = vp.ViewCenter.X;
                    double cy     = vp.ViewCenter.Y;

                    result.Add(new DrawingLayout
                    {
                        Name = layout.LayoutName,
                        X1   = cx - halfW,
                        Y1   = cy - halfH,
                        X2   = cx + halfW,
                        Y2   = cy + halfH,
                    });
                }
            }
        }
        catch (System.Exception ex)
        {
            AcApp.DocumentManager.MdiActiveDocument?.Editor
                 .WriteMessage($"\n[EXT-01] Layout 감지 실패(무시): {ex.Message}\n");
        }
        return result;
    }
}
}