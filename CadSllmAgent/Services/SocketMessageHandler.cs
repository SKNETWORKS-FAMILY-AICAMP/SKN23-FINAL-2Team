/*
 * File    : CadSllmAgent/Services/SocketMessageHandler.cs
 * Author  : 김지우
 * Date : 2026-04-12
 * Description : 
 * Python 백엔드로부터 수신한 WebSocket 메시지를 파싱하여 AutoCAD 명령을 실행.
 * 기존 WebViewMessageHandler의 로직을 계승하되, 통신 채널을 WebSocket으로 전환.
 *
 * E2E 연동(디버깅) 참고:
 * 1) AutoCAD: Review/DemoReviewCommand RUNEXTRACT → /cad Interop (CAD_DATA_EXTRACTED) →
 *    ApiClient / WebView 경로: Redis drawing_path + S3, 포커스 시 drawing_focus.
 * 2) EXTRACT_DATA: 백엔드 websocket(websocket.py) → 본 핸들러 → Extract → 동일 /analyze 페이로드.
 * 3) 백엔드 검토 완료 후 REJECT_FIX / APPROVE_FIX / REVIEW_RESULT: RevCloudDrawer(리비전 클라우드) 연동.
 */
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.EditorInput;
using CadSllmAgent.Review;
using CadSllmAgent.Extraction;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;
using CadSllmAgent.Models;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class SocketMessageHandler
    {
        private static readonly System.Text.Json.JsonSerializerOptions _reviewResultJson = new()
        {
            PropertyNameCaseInsensitive = true,
        };

        private static List<AnnotatedEntity> _pendingEntities = new();
        private static string _currentReviewSessionId = "";

        /// <summary>React 목록/세션 전환 시 — 도면 RevCloud·승인 대기 엔터티 목록을 비운다.</summary>
        public static void ClearAllReviewState()
        {
            RevCloudDrawer.RemoveAllCadSllmReviewMarks();
            _pendingEntities = new List<AnnotatedEntity>();
        }

        // 파이썬 서버에서 메시지가 오면 호출될 함수
        public static void HandleMessage(string jsonMessage)
        {
            Document? doc = AcApp.DocumentManager.MdiActiveDocument;
            Editor? ed = doc?.Editor;

            try
            {
                using var jsonDoc = JsonDocument.Parse(jsonMessage);
                JsonElement root = jsonDoc.RootElement;
                // CRIT-8: action 필드 없으면 KeyNotFoundException → TryGetProperty로 방어
                if (!root.TryGetProperty("action", out var actionProp)) return;
                string action = actionProp.GetString() ?? "";

                switch (action)
                {
                    case "EXTRACT_DATA": // 백엔드에서 데이터 추출을 원격으로 요청할 경우
                    {
                        string extractDomain = "pipe";
                        if (root.TryGetProperty("payload", out var epayload)
                            && epayload.TryGetProperty("domain", out var dProp)
                            && dProp.ValueKind == JsonValueKind.String)
                        {
                            extractDomain = dProp.GetString() ?? "pipe";
                        }
                        else if (root.TryGetProperty("domain", out var dTop)
                            && dTop.ValueKind == JsonValueKind.String)
                        {
                            extractDomain = dTop.GetString() ?? "pipe";
                        }
                        HandleExtractData(ed, extractDomain);
                        break;
                    }

                    case "REVIEW_RESULT": // 파이썬 AI의 검토 결과 수신
                        HandleReviewResult(root, ed);
                        break;

                    case "APPROVE_FIX": // 리액트 -> 파이썬 -> C#으로 전달된 승인 명령
                        HandleApproveFix(root, ed);
                        break;

                    case "REJECT_FIX": // 리액트 -> 파이썬 -> C#으로 전달된 거절 명령
                        HandleRejectFix(root, ed);
                        break;

                    case "ZOOM_TO_ENTITY": // 리액트에서 카드 클릭 시 해당 위치로 이동
                        HandleZoomToEntity(root, ed);
                        break;

                    case "CAD_ACTION": // 사용자 채팅 명령 → 신규 객체 직접 생성 (위반/승인 불필요)
                        HandleCadAction(root, ed);
                        break;

                    case "CANCEL_REVIEW":
                        ApiClient.CancelCurrentSend();
                        ed?.WriteMessage("\n[CAD-Agent] 도면 분석 중단 요청을 받았습니다.\n");
                        break;

                    case "PIPELINE_PROGRESS":
                        // WebSocket ui 전용(React 대화창). 명령줄 출력 없음.
                        break;

                    default:
                        ed?.WriteMessage($"\n[CAD-Agent] 알 수 없는 액션: {action}");
                        break;
                }
            }
            catch (Exception ex)
            {
                var jm = jsonMessage ?? "";
                var preview = jm.Length > 200 ? jm.Substring(0, 200) + "…" : jm;
                CadDebugLog.Exception("SocketMessageHandler.HandleMessage: " + preview, ex);
                ed?.WriteMessage($"\n[CAD-Agent] 메시지 처리 오류: {ex.Message} (자세한 내용: CADAGENTLOG)");
            }
        }

        private static void HandleExtractData(Editor? ed, string domainType = "pipe")
        {
            try
            {
                // 전체 도면은 항상 추출. 선택이 있으면 같은 요청에 선택 구간 스냅샷을 추가로 실어
                // 서버/LLM이 전체 맥락과 관심 영역을 함께 보도록 함.
                ed?.WriteMessage($"\n[CAD-Agent] 도면 전체 추출 중... 도메인={domainType} (선택이 있으면 해당 구간 스냅샷도 함께 전송)\n");
                var focus = CadDataExtractor.ExtractSelected();
                var full = CadDataExtractor.Extract(maxEntityCount: null);
                if (focus != null && focus.EntityCount > 0)
                {
                    ed?.WriteMessage($"[CAD-Agent] 선택 스냅샷: {focus.EntityCount}개 엔티티 (전체 {full.EntityCount}개와 병행 전송)\n");
                    _ = ApiClient.SendCadDataAsync(full, focus, domainType);
                }
                else
                {
                    _ = ApiClient.SendCadDataAsync(full, null, domainType);
                }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleExtractData", ex);
                ed?.WriteMessage($"\n[EXT-01 오류] {ex.Message} | CADAGENTLOG");
            }
        }

        private static void HandleReviewResult(JsonElement root, Editor? ed)
        {
            try
            {
                // CRIT-8: GetProperty → TryGetProperty (payload 없으면 KeyNotFoundException 방지)
                if (!root.TryGetProperty("payload", out var payload))
                {
                    CadDebugLog.Warn("HandleReviewResult: payload 필드 없음");
                    return;
                }
                var result = JsonSerializer.Deserialize<ReviewResult>(payload.GetRawText(), _reviewResultJson);
                if (result == null)
                {
                    CadDebugLog.Warn("HandleReviewResult: payload deserialized to null");
                    return;
                }

                _currentReviewSessionId = result.SessionId ?? "";
                _pendingEntities = result.AnnotatedEntities ?? new List<AnnotatedEntity>();
                if (string.Equals(result.ReviewDomain, "pipe", StringComparison.OrdinalIgnoreCase))
                {
                    _pendingEntities = _pendingEntities
                        .Where(ShouldDrawPipeReviewCloud)
                        .ToList();
                }
                RevCloudDrawer.RemoveAllCadSllmReviewMarks(); // 재검토 시 이전 구름마크 중첩 방지
                RevCloudDrawer.DrawAll(_pendingEntities);
                CadDebugLog.Info($"REVIEW_RESULT entities={_pendingEntities.Count}");
                ed?.WriteMessage($"\n[CAD-Agent] AI 검토 결과 반영 완료 ({_pendingEntities.Count}건)");
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleReviewResult", ex);
                ed?.WriteMessage($"\n[CAD-Agent] REVIEW_RESULT 오류: {ex.Message} (CADAGENTLOG)\n");
            }
        }

        /// <summary>배관(pipe) 검토: NCS식 건축/구조(A-/S-) 레이어는 Python과 동일 정책으로 RevCloud 제외(이중 방어).</summary>
        private static bool ShouldDrawPipeReviewCloud(AnnotatedEntity e)
        {
            if (e == null) return false;
            var layer = (e.Layer ?? "").Trim();
            if (layer.Length < 2) return true;
            if (layer.StartsWith("A-", StringComparison.OrdinalIgnoreCase)) return false;
            if (layer.StartsWith("S-", StringComparison.OrdinalIgnoreCase)) return false;
            return true;
        }

        // ── [ActionAgent 연동 방향 결정] ──────────────────────────────────────────
        // APPROVE_FIX 수신 시 C# DrawingPatcher가 도면을 직접 수정합니다.
        // (Python ActionAgent 경유 없이 C# 직접 수정 방식으로 확정)
        //
        // 흐름:
        //   React UI → APPROVE_FIX (WebSocket) → Python relay
        //     → CAD WebSocket → SocketMessageHandler.HandleApproveFix
        //     → DrawingPatcher.ApplyFix (handle 기반 AutoCAD entity 수정)
        //     → RevCloudDrawer.RemoveCloud (RevCloud 마크 제거)
        //
        // Python ActionAgent(call_action_agent)는 도면 수정 "명령 생성" 단계에서
        // JSON pending_fix를 구성하는 역할이며, 실제 CAD 적용은 C#이 담당합니다.
        private static void HandleApproveFix(JsonElement root, Editor? ed)
        {
            try
            {
                if (!root.TryGetProperty("payload", out var payload))
                {
                    ed?.WriteMessage("\n[CAD-Agent] APPROVE_FIX: payload 없음\n");
                    return;
                }
                string violationId = ReadViolationIdFromPayload(payload);
                var targets = violationId == "ALL"
                    ? _pendingEntities
                    : _pendingEntities.FindAll(e => e.Violation?.Id == violationId);

                if (targets.Count == 0)
                {
                    if (_pendingEntities.Count == 0)
                    {
                        ed?.WriteMessage("\n[CAD-Agent] 승인 실패: CAD에 반영된 검토 결과가 없습니다. AI 검토 완료 후(리비전클라우드 표시) 다시 눌러 주세요.\n");
                    }
                    else
                    {
                        var sample = string.Join(", ", _pendingEntities.Take(5).Select(e => e.Violation?.Id ?? "?"));
                        ed?.WriteMessage($"\n[CAD-Agent] 승인 실패: ID '{violationId}'와 일치하는 항목이 없습니다. (대기 id 예: {sample})\n");
                    }
                    CadDebugLog.Warn($"APPROVE_FIX no targets violationId={violationId} pending={_pendingEntities.Count}");
                    SendFixResult(violationId, false, 0, 0,
                        "승인 실패: CAD에 반영된 검토 결과가 없거나 ID가 일치하지 않습니다.");
                    return;
                }

                int success = 0;
                using (DrawingRevisionTracker.SuppressDirtyEvents())
                {
                    foreach (var entity in targets)
                    {
                        if (DrawingPatcher.ApplyFix(entity)) success++;
                        // 승인 = 검토 항목 완료 처리. auto_fix가 없어 도면이 안 바뀌어도 RevCloud·MText는 항상 제거.
                        // (이전: ApplyFix 실패 시에만 RemoveCloud가 안 돼 구름이 남는 버그)
                        if (!string.IsNullOrEmpty(entity.Violation?.Id))
                            RevCloudDrawer.RemoveCloud(entity.Violation.Id);
                    }
                }
                CadDebugLog.Info($"APPROVE_FIX id={violationId} ok={success}/{targets.Count}");
                RemovePendingTargets(violationId, targets);
                if (success == 0)
                {
                    ed?.WriteMessage(
                        "\n[CAD-Agent] 자동으로 바꿀 수 있는 entity는 0건(auto_fix 없음·대상이 선/원/폴리라인 등). " +
                        "RevCloud·주석은 승인 완료로 제거했습니다. 재질/규격은 필요 시 수동으로 반영하세요. (CADAGENTLOG)\n");
                }
                else
                {
                    ed?.WriteMessage($"\n[CAD-Agent] 수정 승인 처리 완료: 자동 반영 {success}건, RevCloud 제거 완료.\n");
                }

                SendFixResult(
                    violationId,
                    true,
                    success,
                    targets.Count,
                    success > 0
                        ? $"수정 승인 완료: {success}건 자동 반영"
                        : "수정 승인: auto_fix 없음, RevCloud 제거 처리"
                );
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleApproveFix", ex);
                ed?.WriteMessage($"\n[CAD-Agent] APPROVE_FIX 오류: {ex.Message} (CADAGENTLOG)\n");
            }
        }

        /// <summary>UI가 violation_id를 문자열·숫자 JSON 어느 쪽으로 보내든 동일 id로 맞춤.</summary>
        private static string ReadViolationIdFromPayload(JsonElement payload)
        {
            if (!payload.TryGetProperty("violation_id", out var vid)) return "";
            return vid.ValueKind switch
            {
                JsonValueKind.String => vid.GetString() ?? "",
                JsonValueKind.Number => vid.GetRawText().Trim(),
                _ => vid.ToString()
            };
        }

        private static string ReadSessionId(JsonElement root, JsonElement payload)
        {
            if (payload.TryGetProperty("session_id", out var psid)
                && psid.ValueKind == JsonValueKind.String)
                return psid.GetString() ?? "";
            if (root.TryGetProperty("session_id", out var rsid)
                && rsid.ValueKind == JsonValueKind.String)
                return rsid.GetString() ?? "";
            return "";
        }

        /// <summary>
        /// CAD_ACTION — 사용자의 채팅 명령으로 CAD를 직접 수정한다.
        ///
        /// [모드 A] 신규 생성 (handle 없음 또는 type이 CREATE로 시작)
        ///   payload.auto_fix.type == "CREATE_ENTITY" → DrawingPatcher.CreateEntity()
        ///   → CREATE_RESULT 피드백
        ///
        /// [모드 B] 속성 수정 (payload.handle 존재 + type이 LAYER/COLOR/LINEWEIGHT 등)
        ///   payload.handle + payload.auto_fix → AnnotatedEntity 임시 생성
        ///   → DrawingPatcher.ApplyFix() (핸들로 기존 객체 검색 후 수정)
        ///   → FIX_RESULT 피드백
        /// </summary>
        private static void HandleCadAction(JsonElement root, Editor? ed)
        {
            try
            {
                if (!root.TryGetProperty("payload", out var payload))
                {
                    ed?.WriteMessage("\n[CAD-Agent] CAD_ACTION: payload 없음\n");
                    return;
                }
                if (!payload.TryGetProperty("auto_fix", out var fixEl))
                {
                    ed?.WriteMessage("\n[CAD-Agent] CAD_ACTION: auto_fix 없음\n");
                    return;
                }
                string actionSessionId = ReadSessionId(root, payload);

                var fix = JsonSerializer.Deserialize<AutoFix>(
                    fixEl.GetRawText(), _reviewResultJson);
                if (fix == null)
                {
                    ed?.WriteMessage("\n[CAD-Agent] CAD_ACTION: auto_fix 역직렬화 실패\n");
                    return;
                }

                // ── handle 유무로 모드 결정 ────────────────────────────────────
                string handleStr = "";
                if (payload.TryGetProperty("handle", out var hProp)
                    && hProp.ValueKind == JsonValueKind.String)
                    handleStr = hProp.GetString() ?? "";

                bool isCreateMode = string.IsNullOrWhiteSpace(handleStr)
                                    || (fix.Type ?? "").StartsWith("CREATE", StringComparison.OrdinalIgnoreCase);

                if (isCreateMode)
                {
                    // ── 모드 A: 신규 객체 생성 ────────────────────────────────
                    bool success;
                    using (DrawingRevisionTracker.SuppressDirtyEvents())
                    {
                        success = DrawingPatcher.CreateEntity(fix);
                    }
                    string resultLayer = fix.NewLayer ?? DrawingPatcher.ProposalLayerName;

                    if (success)
                        ed?.WriteMessage(
                            $"\n[CAD-Agent] 신규 객체 생성 완료 (type={fix.Type}, layer={resultLayer})\n");
                    else
                        ed?.WriteMessage(
                            "\n[CAD-Agent] 신규 객체 생성 실패 — 좌표 또는 블록명을 확인하세요.\n");

                    CadDebugLog.Info($"CAD_ACTION[CREATE] type={fix.Type} layer={resultLayer} success={success}");

                    _ = SocketClient.SendAsync(System.Text.Json.JsonSerializer.Serialize(new
                    {
                        action = "CREATE_RESULT",
                        session_id = actionSessionId,
                        payload = new
                        {
                            session_id = actionSessionId,
                            success = success,
                            type    = fix.Type ?? "CREATE_ENTITY",
                            layer   = resultLayer,
                            message = success
                                ? $"새 객체가 CAD에 추가되었습니다 (레이어: {resultLayer})"
                                : "객체 생성 실패: 좌표 또는 블록명을 확인하세요.",
                        }
                    }));
                }
                else
                {
                    // ── 모드 B: 기존 객체 속성 수정 ──────────────────────────
                    // AnnotatedEntity를 임시 생성하여 DrawingPatcher.ApplyFix()에 전달한다.
                    // _pendingEntities에 등록하지 않으므로 승인 흐름 없이 즉시 반영된다.
                    var entity = new AnnotatedEntity
                    {
                        Handle    = handleStr,
                        Type      = "",   // ApplyFix는 Type이 아닌 AutoFix.Type을 사용
                        Layer     = "",
                        Violation = new ViolationInfo
                        {
                            Id      = $"chat-{handleStr}",
                            AutoFix = fix,
                        }
                    };

                    bool success;
                    using (DrawingRevisionTracker.SuppressDirtyEvents())
                    {
                        success = DrawingPatcher.ApplyFix(entity);
                    }

                    if (success)
                        ed?.WriteMessage(
                            $"\n[CAD-Agent] 객체 수정 완료 (handle={handleStr}, type={fix.Type})\n");
                    else
                        ed?.WriteMessage(
                            $"\n[CAD-Agent] 객체 수정 실패 — handle={handleStr} 미발견 또는 오류\n");

                    CadDebugLog.Info($"CAD_ACTION[MODIFY] handle={handleStr} type={fix.Type} success={success}");

                    // FIX_RESULT → Python relay → React 채팅창 피드백 (fire-and-forget)
                    _ = SocketClient.SendAsync(System.Text.Json.JsonSerializer.Serialize(new
                    {
                        action = "FIX_RESULT",
                        session_id = actionSessionId,
                        payload = new
                        {
                            session_id      = actionSessionId,
                            success        = success,
                            violation_id   = $"chat-{handleStr}",
                            applied_count  = success ? 1 : 0,
                            total_count    = 1,
                            message        = success
                                ? $"수정 완료: {_FixTypeLabel(fix.Type)} (핸들: {handleStr})"
                                : $"수정 실패: 핸들 {handleStr}을 찾을 수 없습니다.",
                        }
                    }));
                }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleCadAction", ex);
                ed?.WriteMessage($"\n[CAD-Agent] CAD_ACTION 오류: {ex.Message} (CADAGENTLOG)\n");
            }
        }

        /// <summary>auto_fix type을 사용자 친화적 한국어로 변환 (FIX_RESULT 메시지용).</summary>
        private static string _FixTypeLabel(string? type) => (type ?? "").ToUpperInvariant() switch
        {
            "LAYER"       => "레이어 변경",
            "LINEWEIGHT"  => "선 굵기 변경",
            "COLOR"       => "색상 변경",
            "LINETYPE"    => "선 종류 변경",
            "DELETE"      => "객체 삭제",
            "MOVE"        => "위치 이동",
            "ROTATE"      => "회전",
            "SCALE"       => "크기 조정",
            "RECTANGLE_RESIZE" => "사각형 크기 조정",
            "STRETCH_RECT" => "사각형 크기 조정",
            "TEXT_CONTENT"=> "텍스트 내용 변경",
            "TEXT_HEIGHT" => "글자 크기 변경",
            "ATTRIBUTE"   => "블록 속성 변경",
            _             => type ?? "수정",
        };

        private static void HandleZoomToEntity(JsonElement root, Editor? ed)
        {
            if (ed == null) return;
            try
            {
                // CRIT-8: GetProperty → TryGetProperty
                if (!root.TryGetProperty("payload", out var payload)) return;
                string? handle = null;
                if (payload.TryGetProperty("handle", out var hProp) && hProp.ValueKind == JsonValueKind.String)
                {
                    handle = hProp.GetString();
                }

                double x1 = 0, y1 = 0, x2 = 0, y2 = 0;
                bool bboxFound = false;
                Autodesk.AutoCAD.DatabaseServices.ObjectId targetObjectId =
                    Autodesk.AutoCAD.DatabaseServices.ObjectId.Null;

                // 1. 실시간 핸들 추적 (우선순위)
                if (!string.IsNullOrEmpty(handle))
                {
                    var doc = AcApp.DocumentManager.MdiActiveDocument;
                    if (doc != null)
                    {
                        using var tr = doc.TransactionManager.StartTransaction();
                        try
                        {
                            long handleVal = Convert.ToInt64(handle, 16);
                            var objId = doc.Database.GetObjectId(false, new Autodesk.AutoCAD.DatabaseServices.Handle(handleVal), 0);
                            if (objId != Autodesk.AutoCAD.DatabaseServices.ObjectId.Null)
                            {
                                var ent = tr.GetObject(objId, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForRead) as Autodesk.AutoCAD.DatabaseServices.Entity;
                                if (ent != null)
                                {
                                    targetObjectId = objId;
                                    var ext = ent.GeometricExtents;
                                    x1 = ext.MinPoint.X;
                                    y1 = ext.MinPoint.Y;
                                    x2 = ext.MaxPoint.X;
                                    y2 = ext.MaxPoint.Y;
                                    bboxFound = true;
                                }
                            }
                        }
                        catch { }
                        tr.Commit();
                    }
                }

                // 2. 캐시된 bbox(Python 송신값) 폴백
                // bbox JSON 값이 null인 경우 GetProperty 호출 시 InvalidOperationException 발생 → 명시적 null guard
                if (!bboxFound
                    && payload.TryGetProperty("bbox", out var bbox)
                    && bbox.ValueKind == JsonValueKind.Object)
                {
                    try
                    {
                        x1 = bbox.GetProperty("x1").GetDouble();
                        y1 = bbox.GetProperty("y1").GetDouble();
                        x2 = bbox.GetProperty("x2").GetDouble();
                        y2 = bbox.GetProperty("y2").GetDouble();
                        bboxFound = true;
                    }
                    catch { /* bbox 구조 이상 — 무시하고 계속 */ }
                }

                if (!bboxFound) return;

                SelectZoomTarget(ed, targetObjectId);

                const double pad = 500.0;
                double w = System.Math.Max(x2 - x1, 1.0);
                double h = System.Math.Max(y2 - y1, 1.0);
                var view = ed.GetCurrentView();
                view.CenterPoint = new Autodesk.AutoCAD.Geometry.Point2d(
                    (x1 + x2) / 2.0, (y1 + y2) / 2.0);
                view.Height = h + pad * 2;
                view.Width  = w + pad * 2;
                ed.SetCurrentView(view);

                // 줌 위치에 임시 구름 하이라이트 표시
                DrawZoomHighlight(ed, x1, y1, x2, y2);
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleZoomToEntity", ex);
                ed.WriteMessage($"\n[CAD-Agent] ZoomToEntity 오류: {ex.Message} (CADAGENTLOG)\n");
            }
        }

        private static void SelectZoomTarget(
            Editor ed,
            Autodesk.AutoCAD.DatabaseServices.ObjectId targetObjectId)
        {
            try
            {
                ed.SetImpliedSelection(Array.Empty<Autodesk.AutoCAD.DatabaseServices.ObjectId>());
                if (targetObjectId != Autodesk.AutoCAD.DatabaseServices.ObjectId.Null)
                {
                    ed.SetImpliedSelection(new[] { targetObjectId });
                }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("SelectZoomTarget", ex);
            }
        }

        private static void RemovePendingTargets(string violationId, List<AnnotatedEntity> targets)
        {
            if (string.Equals(violationId, "ALL", StringComparison.OrdinalIgnoreCase))
            {
                _pendingEntities.Clear();
                return;
            }
            var ids = new HashSet<string>(
                targets.Select(t => t.Violation?.Id ?? "").Where(x => !string.IsNullOrWhiteSpace(x)),
                StringComparer.OrdinalIgnoreCase);
            _pendingEntities = _pendingEntities
                .Where(e => !ids.Contains(e.Violation?.Id ?? ""))
                .ToList();
        }

        private static void SendFixResult(
            string violationId,
            bool success,
            int appliedCount,
            int totalCount,
            string message)
        {
            _ = SocketClient.SendAsync(System.Text.Json.JsonSerializer.Serialize(new
            {
                action = "FIX_RESULT",
                session_id = _currentReviewSessionId,
                payload = new
                {
                    session_id = _currentReviewSessionId,
                    violation_id = violationId,
                    success,
                    applied_count = appliedCount,
                    total_count = totalCount,
                    message,
                }
            }));
        }

        private const string ZoomHighlightLayer = "AI_ZOOM_HIGHLIGHT";

        private static void DrawZoomHighlight(Editor ed, double x1, double y1, double x2, double y2)
        {
            var doc = AcApp.DocumentManager.MdiActiveDocument;
            if (doc == null) return;
            try
            {
                using var tr = doc.TransactionManager.StartTransaction();
                var db = doc.Database;
                var bt  = (Autodesk.AutoCAD.DatabaseServices.BlockTable)tr.GetObject(db.BlockTableId, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForRead);
                var btr = (Autodesk.AutoCAD.DatabaseServices.BlockTableRecord)tr.GetObject(bt[Autodesk.AutoCAD.DatabaseServices.BlockTableRecord.ModelSpace], Autodesk.AutoCAD.DatabaseServices.OpenMode.ForWrite);

                // 이전 하이라이트 삭제
                var toErase = new List<Autodesk.AutoCAD.DatabaseServices.ObjectId>();
                foreach (Autodesk.AutoCAD.DatabaseServices.ObjectId oid in btr)
                {
                    var obj = tr.GetObject(oid, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForRead);
                    if (obj is Autodesk.AutoCAD.DatabaseServices.Entity ent && ent.Layer == ZoomHighlightLayer)
                        toErase.Add(oid);
                }
                foreach (var oid in toErase)
                    tr.GetObject(oid, Autodesk.AutoCAD.DatabaseServices.OpenMode.ForWrite).Erase();

                // 레이어 생성 및 구름 마크 그리기
                CadSllmAgent.Review.RevCloudDrawer.EnsureLayer(db, tr, ZoomHighlightLayer);
                double margin = System.Math.Max((x2 - x1 + y2 - y1) * 0.05, 50.0);
                var cloud = CadSllmAgent.Review.RevCloudDrawer.BuildCloudPolyline(
                    x1 - margin, y1 - margin, x2 + margin, y2 + margin);
                cloud.Layer = ZoomHighlightLayer;
                cloud.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                    Autodesk.AutoCAD.Colors.ColorMethod.ByAci, 2); // 노란색(2=yellow)
                cloud.ConstantWidth = System.Math.Clamp((x2 - x1 + y2 - y1) * 0.003, 3.0, 20.0);
                btr.AppendEntity(cloud);
                tr.AddNewlyCreatedDBObject(cloud, true);
                tr.Commit();
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("DrawZoomHighlight", ex);
            }
        }

        private static void HandleRejectFix(JsonElement root, Editor? ed)
        {
            try
            {
                if (!root.TryGetProperty("payload", out var payload)) return;
                string violationId = ReadViolationIdFromPayload(payload);

                if (string.Equals(violationId, "ALL", StringComparison.OrdinalIgnoreCase))
                {
                    int n = 0;
                    foreach (var e in _pendingEntities)
                    {
                        if (!string.IsNullOrEmpty(e.Violation?.Id))
                        {
                            RevCloudDrawer.RemoveCloud(e.Violation.Id);
                            n++;
                        }
                    }
                    _pendingEntities.Clear();
                    CadDebugLog.Info($"REJECT_FIX ALL n={n}");
                    ed?.WriteMessage($"\n[CAD-Agent] {n}건 RevCloud 제거(전체 무시)\n");
                }
                else
                {
                    RevCloudDrawer.RemoveCloud(violationId);
                    _pendingEntities = _pendingEntities
                        .Where(e => !string.Equals(e.Violation?.Id, violationId, StringComparison.OrdinalIgnoreCase))
                        .ToList();
                    CadDebugLog.Info($"REJECT_FIX id={violationId}");
                    ed?.WriteMessage($"\n[CAD-Agent] 위반 {violationId} 거절 — RevCloud 제거 완료\n");
                }
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("HandleRejectFix", ex);
                ed?.WriteMessage($"\n[CAD-Agent] REJECT_FIX 오류: {ex.Message}\n");
            }
        }

    }
}
