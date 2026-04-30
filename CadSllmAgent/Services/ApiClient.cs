/*
 * File    : CadSllmAgent/Services/ApiClient.cs
 * Author  : 김지우
 * Date : 2026-04-12
 * Description : Python FastAPI 서버와 REST 통신을 담당하는 클라이언트
 *
 * 배관 LangGraph tool 정합: `PipingToolNames.CallReviewAgent`에 해당하는
 * CAD 기하 데이터(전체+선택)를 `/api/v1/cad/analyze`로 보내 `call_review_agent` 입력이 된다.
 * (C# PipingToolBridge.cs 참고)
 */
using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Threading;
using System.Collections.Generic;
using System.Linq;
using Autodesk.AutoCAD.ApplicationServices;
using CadSllmAgent.Models;
using CadSllmAgent.Extraction;
using CadSllmAgent.UI;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace CadSllmAgent.Services
{
    [System.Runtime.Versioning.SupportedOSPlatform("windows")]
    public class ApiClient
    {
        private static readonly HttpClient _httpClient = new HttpClient
        {
            BaseAddress = new Uri(AgentConfig.BackendBaseUrl),
            Timeout = TimeSpan.FromMinutes(5)
        };

        private static readonly JsonSerializerOptions _jsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
            DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
        };

        /// <summary>선택 변경 시 React 쪽 객체 수 실시간 반영 (빈 목록 = 전부 해제).</summary>
        public static async Task BroadcastSelectionAsync(IReadOnlyList<string> activeHandles)
        {
            try
            {
                var requestBody = new { active_object_ids = activeHandles };
                string json = JsonSerializer.Serialize(requestBody, _jsonOptions);
                var content = new StringContent(json, Encoding.UTF8, "application/json");
                using var resp = await _httpClient.PostAsync("/api/v1/cad/selection", content);
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("ApiClient.BroadcastSelectionAsync", ex);
            }
        }

        /// <summary>엔티티 N개당 1 POST — HTTP/Redis 한도·메모리 보호(전수 도면은 여러 청크로 머지).</summary>
        public const int ExtractionEntityChunkSize = AgentConfig.ExtractionEntityChunkSize;
        private static CancellationTokenSource? _sendCts;

        public static void CancelCurrentSend()
        {
            var cts = _sendCts;
            if (cts != null && !cts.IsCancellationRequested)
                cts.Cancel();
        }

        /// <param name="focusDrawing">DWG에서 부분 추출(선택 스냅샷). 있으면 서버가 S3 풀 JSON과 맞춰 <c>context_mode=full_with_focus</c>로 병합·비교.</param>
        public static async Task SendCadDataAsync(CadDrawingData fullDrawing, CadDrawingData? focusDrawing = null, string domainType = "pipe")
        {
            var previous = _sendCts;
            previous?.Cancel();

            using var cts = new CancellationTokenSource();
            _sendCts = cts;
            try
            {
                int n = fullDrawing.Entities?.Count ?? 0;
                int f = focusDrawing?.EntityCount ?? 0;
                if (n <= ExtractionEntityChunkSize && f <= ExtractionEntityChunkSize)
                {
                    await SendCadDataSinglePostAsync(fullDrawing, focusDrawing, domainType, cts.Token);
                    return;
                }
                await SendCadDataChunkedAsync(fullDrawing, focusDrawing, domainType, cts.Token);
            }
            catch (OperationCanceledException) when (cts.IsCancellationRequested)
            {
                AcApp.DocumentManager.MdiActiveDocument?.Editor
                    ?.WriteMessage("\n[CAD-Agent] 도면 분석 전송이 중단되었습니다.");
            }
            finally
            {
                if (ReferenceEquals(_sendCts, cts))
                    _sendCts = null;
            }
        }

        private static async Task SendCadDataChunkedAsync(
            CadDrawingData fullDrawing, CadDrawingData? focusDrawing, string domainType, CancellationToken cancellationToken)
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;
            var activeIds = CadDataExtractor.GetActiveSelectionHandles();
            var sessionId = Guid.NewGuid().ToString();
            var all = fullDrawing.Entities ?? new List<CadEntity>();
            int total = (int)Math.Ceiling(all.Count / (double)ExtractionEntityChunkSize);
            CadDrawingData? focus0 = focusDrawing;

            if (focus0 != null && focus0.EntityCount > ExtractionEntityChunkSize)
            {
                ed?.WriteMessage(
                    $"\n[CAD-Agent] 경고: 선택 스냅샷 {focus0.EntityCount}개 → HTTP 한도로 앞 {ExtractionEntityChunkSize}개만 전송합니다.\n");
                var slim = new CadDrawingData
                {
                    DrawingUnit = focus0.DrawingUnit,
                    LayerCount = focus0.LayerCount,
                    EntityCount = ExtractionEntityChunkSize,
                    Layers = focus0.Layers,
                    Entities = focus0.Entities.Take(ExtractionEntityChunkSize).ToList(),
                };
                focus0 = slim;
            }
            var hasFocus = focus0 != null && focus0.EntityCount > 0;
            var dwgCompareMode = hasFocus ? "full_with_focus" : "full_only";
            if (hasFocus)
                CadDebugLog.Info($"SendCadDataChunked: focus는 첫 청크에만 동봉 full_chunks={total}");

            for (int i = 0; i < total; i++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var slice = all.Skip(i * ExtractionEntityChunkSize).Take(ExtractionEntityChunkSize).ToList();
                CadDrawingData part = i == 0
                    ? new CadDrawingData
                    {
                        DrawingUnit = fullDrawing.DrawingUnit,
                        LayerCount = fullDrawing.LayerCount,
                        EntityCount = slice.Count,
                        Layers = fullDrawing.Layers,
                        Entities = slice,
                    }
                    : new CadDrawingData
                    {
                        DrawingUnit = fullDrawing.DrawingUnit,
                        EntityCount = slice.Count,
                        Entities = slice,
                    };

                var payload = new
                {
                    drawing_data = part,
                    focus_drawing_data = (i == 0 && hasFocus) ? focus0 : null,
                    active_object_ids = activeIds,
                    org_id = PluginEntry.OrgId,
                    device_id = PluginEntry.DeviceId,
                    machine_id = PluginEntry.MachineId,
                    review_tool = "call_review_agent", // 문자열 하드코딩으로 변경 (오류 해결)
                    domain_type = domainType,
                    dwg_compare_mode = dwgCompareMode,
                    extraction_chunk = new { index = i, count = total },
                };

                var requestBody = new
                {
                    session_id = sessionId,
                    action = "CAD_DATA_EXTRACTED",
                    payload = payload,
                };

                string json = JsonSerializer.Serialize(requestBody, _jsonOptions);
                if (i == 0)
                    ed?.WriteMessage(
                        $"\n[CAD-Agent] 도면 청크 전송 1/{total} (총 엔티티 {all.Count}개, {ExtractionEntityChunkSize}개/회)…");

                // 5xx / 타임아웃 에 한해 최대 3회 지수 백오프 재시도
                bool chunkOk = false;
                for (int attempt = 0; attempt < 3; attempt++)
                {
                    if (attempt > 0)
                    {
                        int delaySec = 1 << attempt; // 2s, 4s
                        ed?.WriteMessage($"\n[CAD-Agent] 재시도 {attempt}/2 — {delaySec}초 후…");
                        await Task.Delay(TimeSpan.FromSeconds(delaySec), cancellationToken);
                    }

                    try
                    {
                        var content = new StringContent(json, Encoding.UTF8, "application/json");
                        var response = await _httpClient.PostAsync("/api/v1/cad/analyze", content, cancellationToken);

                        if (response.IsSuccessStatusCode)
                        {
                            chunkOk = true;
                            break;
                        }

                        string err = await response.Content.ReadAsStringAsync();
                        int statusCode = (int)response.StatusCode;

                        // 4xx(클라이언트 오류)는 재시도해도 의미 없음 → 즉시 중단
                        if (statusCode >= 400 && statusCode < 500)
                        {
                            CadDebugLog.Error($"ApiClient chunk {i + 1}/{total} HTTP {statusCode} (클라이언트 오류): {err}");
                            ed?.WriteMessage($"\n[CAD-Agent] 청크 전송 실패 ({i + 1}/{total}): {response.StatusCode}");
                            ed?.WriteMessage($"\n[상세]: {err}");
                            return;
                        }

                        // 5xx: 재시도 대상
                        CadDebugLog.Error($"ApiClient chunk {i + 1}/{total} 시도{attempt + 1} HTTP {statusCode}: {err}");
                        if (attempt == 2)
                        {
                            ed?.WriteMessage($"\n[CAD-Agent] 청크 전송 최종 실패 ({i + 1}/{total}): {response.StatusCode}");
                            ed?.WriteMessage($"\n[상세]: {err}");
                            return;
                        }
                    }
                    catch (TaskCanceledException)
                    {
                        if (cancellationToken.IsCancellationRequested)
                        {
                            ed?.WriteMessage("\n[CAD-Agent] 도면 분석 전송이 중단되었습니다.");
                            return;
                        }
                        CadDebugLog.Error($"ApiClient chunk {i + 1}/{total} 시도{attempt + 1} 타임아웃");
                        if (attempt == 2)
                        {
                            ed?.WriteMessage($"\n[CAD-Agent] 청크 {i + 1}/{total} 타임아웃 — 전송 중단");
                            return;
                        }
                    }
                }

                if (!chunkOk) return;

                ed?.WriteMessage(
                    (i < total - 1)
                        ? $"\n[CAD-Agent] 청크 {i + 1}/{total} OK — 이어서 전송…"
                        : $"\n[CAD-Agent] 마지막 청크 {total}/{total} OK. 서버가 병합·저장합니다.");
            }

            try
            {
                DrawingRevisionTracker.MarkSnapshotCached(sessionId);
                var bridge = new { action = "CAD_ANALYZE_COMPLETE", session_id = sessionId };
                AgentPalette.PostMessage(JsonSerializer.Serialize(bridge, _jsonOptions));
            }
            catch (Exception exb)
            {
                CadDebugLog.Exception("ApiClient PostMessage CAD_ANALYZE_COMPLETE (chunked)", exb);
            }
            int fN = focus0?.EntityCount ?? 0;
            CadDebugLog.Info(
                $"SendCadDataAsync chunked OK session={sessionId} full={all.Count} " +
                $"focus={fN} active={activeIds.Count} chunks={total} compare={dwgCompareMode}"
            );
            ed?.WriteMessage(
                $"\n[CAD-Agent] 전체 {all.Count}개 엔티티 {total}회 분할 전송 완료. AI 쪽 세션: {sessionId}");
        }

        private static async Task SendCadDataSinglePostAsync(
            CadDrawingData fullDrawing, CadDrawingData? focusDrawing, string domainType, CancellationToken cancellationToken)
        {
            var ed = AcApp.DocumentManager.MdiActiveDocument?.Editor;

            try
            {
                var activeIds = CadDataExtractor.GetActiveSelectionHandles();
                var hasFocus = focusDrawing != null && focusDrawing.EntityCount > 0;
                var dwgCompareMode = hasFocus ? "full_with_focus" : "full_only";

                var cacheSessionId = Guid.NewGuid().ToString();
                var requestBody = new
                {
                    session_id = cacheSessionId,
                    action = "CAD_DATA_EXTRACTED",
                    payload = new
                    {
                        drawing_data = fullDrawing,
                        focus_drawing_data = focusDrawing,
                        active_object_ids = activeIds,
                        org_id = PluginEntry.OrgId,
                        device_id = PluginEntry.DeviceId,
                        machine_id = PluginEntry.MachineId,
                        review_tool = "call_review_agent", // 문자열 하드코딩으로 변경 (오류 해결)
                        domain_type = domainType,
                        dwg_compare_mode = dwgCompareMode,
                    }
                };

                string json = JsonSerializer.Serialize(requestBody, _jsonOptions);
                var content = new StringContent(json, Encoding.UTF8, "application/json");

                ed?.WriteMessage($"\n[CAD-Agent] AI 서버로 도면 데이터 전송 중... (MachineId: {PluginEntry.MachineId})");

                var response = await _httpClient.PostAsync("/api/v1/cad/analyze", content, cancellationToken);

                if (response.IsSuccessStatusCode)
                {
                    DrawingRevisionTracker.MarkSnapshotCached(cacheSessionId);
                    CadDebugLog.Info(
                        $"SendCadDataAsync OK session={cacheSessionId} full={fullDrawing.EntityCount} " +
                        $"focus={(focusDrawing?.EntityCount ?? 0)} active={activeIds.Count} compare={dwgCompareMode}"
                    );
                    try
                    {
                        var bridge = new { action = "CAD_ANALYZE_COMPLETE", session_id = cacheSessionId };
                        AgentPalette.PostMessage(JsonSerializer.Serialize(bridge, _jsonOptions));
                    }
                    catch (Exception exb)
                    {
                        CadDebugLog.Exception("ApiClient PostMessage CAD_ANALYZE_COMPLETE", exb);
                    }

                    if (focusDrawing != null && focusDrawing.EntityCount > 0)
                    {
                        ed?.WriteMessage(
                            $"\n[CAD-Agent] 전송 성공 (전체 {fullDrawing.EntityCount}개 + 선택 스냅샷 {focusDrawing.EntityCount}개, 핸들 {activeIds.Count}개). "
                            + "전체 맥락과 관심 구간을 함께 분석합니다.");
                    }
                    else if (activeIds.Count > 0)
                    {
                        ed?.WriteMessage(
                            $"\n[CAD-Agent] 전송 성공 (전체 도면, 선택 대상 {activeIds.Count}개). AI 분석이 시작되었습니다.");
                    }
                    else
                    {
                        ed?.WriteMessage($"\n[CAD-Agent] 전송 성공 (전체 도면). AI 분석이 시작되었습니다.");
                    }
                }
                else
                {
                    string errorDetail = await response.Content.ReadAsStringAsync();
                    CadDebugLog.Error($"ApiClient POST /cad/analyze HTTP {(int)response.StatusCode}: {errorDetail}");
                    ed?.WriteMessage($"\n[CAD-Agent] 전송 실패: {response.StatusCode}");
                    ed?.WriteMessage($"\n[상세 원인]: {errorDetail}");
                    ed?.WriteMessage($"\n[CAD-Agent] 전체 기록: CADAGENTLOG 명령 (로그: {CadDebugLog.GetLogFilePath()})");
                }
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                ed?.WriteMessage("\n[CAD-Agent] 도면 분석 전송이 중단되었습니다.");
            }
            catch (Exception ex)
            {
                CadDebugLog.Exception("ApiClient.SendCadDataAsync", ex);
                ed?.WriteMessage($"\n[CAD-Agent] 통신 오류: {ex.Message}");
                ed?.WriteMessage($"\n[스택/파일] CADAGENTLOG | {ex.GetType().Name}: {ex.StackTrace}\n");
            }
        }
    }
}
