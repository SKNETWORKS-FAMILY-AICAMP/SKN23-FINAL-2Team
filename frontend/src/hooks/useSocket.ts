/// <reference types="vite/client" />
// src/hooks/useSocket.ts
import { useEffect, useRef, useCallback, useState } from "react";
import useAgentStore from "../store/agentStore";

const _http =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://localhost:8000";
const WS_URL = _http.replace(/^http/, "ws") + "/ws/ui";
const PING_INTERVAL_MS = 25_000;
const RECONNECT_DELAY_MS = 3_000;

/** Redis `drawing:{id}` 키 — 팔레트 리마운트 후에도 USER_CHAT에서 복구 */
export const LAST_CAD_SESSION_STORAGE_KEY = "skn23_last_cad_session_id";
export type SocketConnectionStatus = "connecting" | "connected" | "disconnected";

type AnyRecord = Record<string, any>;

const isRecord = (value: unknown): value is AnyRecord =>
  !!value && typeof value === "object" && !Array.isArray(value);

const firstString = (...values: unknown[]): string => {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
};

const firstRecord = (...values: unknown[]): AnyRecord | undefined => {
  for (const value of values) {
    if (isRecord(value)) return value;
  }
  return undefined;
};

const extractPayload = (data: AnyRecord): AnyRecord | undefined =>
  isRecord(data.payload) ? data.payload : undefined;

const extractResponseMeta = (data: AnyRecord): AnyRecord | undefined => {
  const payload = extractPayload(data);
  const state = firstRecord(payload?.state, data.state);
  const result = firstRecord(payload?.result, data.result);
  const reviewResult = firstRecord(
    payload?.review_result,
    payload?.reviewResult,
    data.review_result,
    data.reviewResult,
  );

  const meta = firstRecord(
    data.response_meta,
    data.responseMeta,
    payload?.response_meta,
    payload?.responseMeta,
    state?.response_meta,
    state?.responseMeta,
    result?.response_meta,
    result?.responseMeta,
    reviewResult?.response_meta,
    reviewResult?.responseMeta,
  );

  if (!meta) {
    const suggested =
      data.suggested_queries ??
      data.suggestedQueries ??
      payload?.suggested_queries ??
      payload?.suggestedQueries;
    if (Array.isArray(suggested)) return { suggested_queries: suggested };
    return undefined;
  }

  const directSuggested =
    meta.suggested_queries ??
    meta.suggestedQueries ??
    data.suggested_queries ??
    data.suggestedQueries ??
    payload?.suggested_queries ??
    payload?.suggestedQueries;

  if (Array.isArray(directSuggested) && !Array.isArray(meta.suggested_queries)) {
    return { ...meta, suggested_queries: directSuggested };
  }

  return meta;
};

const extractMessageText = (data: AnyRecord): string => {
  const payload = extractPayload(data);
  const state = firstRecord(payload?.state, data.state);
  const result = firstRecord(payload?.result, data.result);
  const reviewResult = firstRecord(
    payload?.review_result,
    payload?.reviewResult,
    data.review_result,
    data.reviewResult,
  );

  return firstString(
    data.message,
    data.assistant_response,
    data.assistantResponse,
    payload?.message,
    payload?.assistant_response,
    payload?.assistantResponse,
    state?.assistant_response,
    state?.assistantResponse,
    result?.assistant_response,
    result?.assistantResponse,
    result?.final_message,
    result?.finalMessage,
    reviewResult?.final_message,
    reviewResult?.finalMessage,
  );
};

const extractSources = (data: AnyRecord): unknown[] | undefined => {
  const payload = extractPayload(data);
  if (Array.isArray(data.sources)) return data.sources;
  if (Array.isArray(payload?.sources)) return payload.sources;
  return undefined;
};

export const useSocket = () => {
  const ws = useRef<WebSocket | null>(null);
  const pingTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connId = useRef(0);
  const [isConnected, setIsConnected] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<SocketConnectionStatus>("connecting");
  const [viewCenter, setViewCenter] = useState<{ x: number; y: number } | null>(null);
  const analysisStartedAt = useRef<number | null>(null);

  const setAnalyzing        = useAgentStore((state) => state.setAnalyzing);
  const setPipelineProgress = useAgentStore((state) => state.setPipelineProgress);
  const setReviewResults    = useAgentStore((state) => state.setReviewResults);
  const addMessage          = useAgentStore((state) => state.addMessage);
  const setMessages         = useAgentStore((state) => state.setMessages);
  const setActiveObjectIds  = useAgentStore((s) => s.setActiveObjectIds);
  const setCadSessionId     = useAgentStore((state) => state.setCadSessionId);
  const setDomainMismatch   = useAgentStore((state) => state.setDomainMismatch);
  const fixSummaryRef = useRef<{
    id: string;
    approvedCount: number;
    appliedCount: number;
    violationIds: Set<string>;
  } | null>(null);

  const clearTimers = () => {
    if (pingTimer.current) {
      clearInterval(pingTimer.current);
      pingTimer.current = null;
    }
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current);
      reconnectTimer.current = null;
    }
  };

  const connect = useCallback(() => {
    if (ws.current && ws.current.readyState < WebSocket.CLOSING) {
      ws.current.onclose = null;
      ws.current.close();
    }
    clearTimers();

    const myId = ++connId.current;
    setConnectionStatus("connecting");
    const socket = new WebSocket(WS_URL);
    ws.current = socket;

    socket.onopen = () => {
      if (connId.current !== myId) {
        socket.close();
        return;
      }
      console.log("[React] WebSocket 연결 성공");
      setIsConnected(true);
      setConnectionStatus("connected");
      pingTimer.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ action: "PING" }));
        }
      }, PING_INTERVAL_MS);
    };

    socket.onmessage = (event) => {
      if (connId.current !== myId) return;

      let data: AnyRecord;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }

      switch (data.action) {
        case "PONG":
          break;

        case "CAD_DATA_READY": {
          console.log("Receive CAD_DATA_READY:", data);
          const payload = extractPayload(data);
          const extractedIds: string[] | undefined =
            payload?.active_object_ids || payload?.activeObjectIds;
          const rawType = payload?.extraction_type as string | undefined;
          const mode = rawType === "focus" ? "focus" : "full";

          if (extractedIds && Array.isArray(extractedIds)) {
            setActiveObjectIds(extractedIds, mode);
          } else {
            setActiveObjectIds([], "full");
          }

          if (data.session_id) {
            const unmappedLayers = Array.isArray(payload?.unmapped_layers)
              ? payload.unmapped_layers
              : undefined;
            setCadSessionId(data.session_id, unmappedLayers);
            try {
              localStorage.setItem(LAST_CAD_SESSION_STORAGE_KEY, String(data.session_id));
            } catch {
              /* noop */
            }
          }

          const drawingId = payload?.drawing_id;
          const snapshotId = payload?.snapshot_id;
          if (drawingId) {
            useAgentStore.getState().setDrawingState({
              drawingId,
              status: "LATEST",
              changeMode: "NONE",
              dirtyCount: 0,
              deltaCount: 0,
              deltaSize: 0,
              currentSnapshotId: snapshotId ?? null,
              currentSnapshotPath: payload?.currentSnapshotPath ?? payload?.current_snapshot_path ?? null,
              latestDeltaPath: null,
              pendingAfterResync: false,
              canReviewStaleSnapshot: false,
              lastError: null,
            });
          }

          // file_fingerprint: CAD 파일 변경 감지용으로 저장 (없으면 null)
          const fp = typeof payload?.file_fingerprint === "string" && payload.file_fingerprint
            ? payload.file_fingerprint
            : null;
          useAgentStore.getState().setFileFingerprint(fp);

          break;
        }

        case "DRAWING_STATE_CHANGED": {
          const p = extractPayload(data);
          if (!p) break;
          useAgentStore.getState().setDrawingState({
            drawingId: p.drawing_id ?? "",
            status: p.status ?? "LATEST",
            changeMode: p.change_mode ?? "NONE",
            dirtyCount: p.dirty_count ?? 0,
            deltaCount: p.delta_count ?? 0,
            deltaSize: p.delta_size ?? 0,
            currentSnapshotId: p.current_snapshot_id ?? null,
            currentSnapshotPath: p.current_snapshot_path ?? null,
            latestDeltaPath: p.latest_delta_path ?? null,
            pendingAfterResync: p.pending_after_resync ?? false,
            canReviewStaleSnapshot: p.can_review_stale_snapshot ?? false,
            lastError: p.last_error ?? null,
          });
          break;
        }

        case "DRAWING_LOCAL_DIRTY": {
          const p = extractPayload(data);
          const state = useAgentStore.getState().drawingState;
          const drawingId = String(p?.drawing_id ?? state?.drawingId ?? "");
          if (!drawingId) break;
          if (state?.drawingId && p?.drawing_id && state.drawingId !== p.drawing_id) {
            break;
          }
          useAgentStore.getState().setDrawingState({
            drawingId,
            status: "DIRTY",
            changeMode: "NONE",
            dirtyCount: Math.max(state?.dirtyCount ?? 0, Number(p?.dirty_count ?? 1) || 1),
            deltaCount: state?.deltaCount ?? 0,
            deltaSize: state?.deltaSize ?? 0,
            currentSnapshotId: state?.currentSnapshotId ?? null,
            currentSnapshotPath: state?.currentSnapshotPath ?? null,
            latestDeltaPath: state?.latestDeltaPath ?? null,
            pendingAfterResync: state?.pendingAfterResync ?? false,
            canReviewStaleSnapshot: false,
            lastError: null,
          });
          break;
        }

        case "CAD_SELECTION_CHANGED":
        case "CAD_DATA_EXTRACTED": {
          const payload = extractPayload(data);
          const selIds: string[] | undefined =
            payload?.active_object_ids || payload?.activeObjectIds;
          if (selIds && Array.isArray(selIds)) {
            setActiveObjectIds(selIds, "focus");
          }
          break;
        }

        case "CHAT_RESPONSE": {
          const currentLogs = useAgentStore.getState().progressLogs;
          const startTime = analysisStartedAt.current || Date.now();
          const thinkingTime = Math.floor((Date.now() - startTime) / 1000);

          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;

          const t = Date.now();
          const sources = extractSources(data);
          const responseMeta = extractResponseMeta(data);
          const payload = extractPayload(data);
          const msgDomain = firstString(data.domain, payload?.domain);

          if (import.meta.env.DEV) {
            console.log("[WS CHAT_RESPONSE RAW]", data);
            console.log("[WS CHAT_RESPONSE response_meta]", responseMeta);
            console.log("[WS CHAT_RESPONSE suggested_queries]", responseMeta?.suggested_queries);
          }

          addMessage({
            id: `c-${t}`,
            ts: t,
            sender: "agent",
            text: extractMessageText(data),
            timestamp: new Date().toISOString(),
            sources,
            streaming: true,
            thinkingLogs: currentLogs,
            thinkingTime: thinkingTime > 0 ? thinkingTime : 0,
            response_meta: responseMeta as any,
            responseMeta: responseMeta as any,
            suggested_queries: responseMeta?.suggested_queries as any,
            suggestedQueries: responseMeta?.suggested_queries as any,
            domain: msgDomain || undefined,
          } as any);
          break;
        }

        case "ANALYSIS_STARTED": {
          analysisStartedAt.current = Date.now();
          setAnalyzing(true);
          setPipelineProgress({
            message: (data.message as string) || "분석 시작 중...",
            stepMs: 0,
            totalMs: 0,
            pipelineStartedAtIso: new Date().toISOString(),
          });
          break;
        }

        case "AGENT_PROGRESS": {
          if (data.message) {
            setPipelineProgress({
              message: data.message as string,
              stepMs: 0,
              totalMs: 0,
              pipelineStartedAtIso: new Date().toISOString(),
            });
          } else {
            setPipelineProgress(null);
          }
          break;
        }

        case "VIEW_CENTER": {
          const vc = extractPayload(data);
          if (typeof vc?.x === "number" && typeof vc?.y === "number") {
            setViewCenter({ x: vc.x, y: vc.y });
          }
          break;
        }

        case "PIPELINE_PROGRESS": {
          const p = extractPayload(data) as {
            message?: string;
            total_elapsed_ms?: number;
            step_elapsed_ms?: number;
            stage?: string;
            pipeline_started_at?: string;
          } | undefined;

          if (!p?.message) break;
          if (p.stage === "cad_data_ready") {
            setPipelineProgress({
              pipelineStartedAtIso: p.pipeline_started_at,
              message: "도면 데이터 준비 완료, 에이전트 분석 시작 중...",
              stage: p.stage,
              stepMs: p.step_elapsed_ms ?? 0,
              totalMs: p.total_elapsed_ms ?? 0,
            });
            break;
          }

          setPipelineProgress({
            pipelineStartedAtIso: p.pipeline_started_at,
            message: p.message,
            stage: p.stage,
            stepMs: p.step_elapsed_ms ?? 0,
            totalMs: p.total_elapsed_ms ?? 0,
          });
          setAnalyzing(true);
          break;
        }

        case "REVIEW_RESULT_UI": {
          fixSummaryRef.current = null;
          const currentLogs = useAgentStore.getState().progressLogs;
          const startTime = analysisStartedAt.current || Date.now();
          const thinkingTime = Math.floor((Date.now() - startTime) / 1000);

          const payload = extractPayload(data);
          const entities = Array.isArray(payload?.annotated_entities)
            ? payload.annotated_entities
            : [];
          setReviewResults({ annotated_entities: entities });
          setAnalyzing(false);
          analysisStartedAt.current = null;

          const t = Date.now();
          if (entities.length > 0) {
            const n = entities.length;
            const hasAutofix = entities.some(
              (e: { violation?: { auto_fix?: unknown } }) => e?.violation?.auto_fix != null,
            );
            const tail = hasAutofix
              ? " **수정 및 검토** 탭에서 **수정 승인**을 누르면 CAD에 반영됩니다."
              : " **수정 및 검토** 탭에서 내용을 확인하세요.";
            addMessage({
              id: `r-${t}`,
              ts: t,
              sender: "agent",
              text: `${n}건의 검토·수정 항목이 있습니다.${tail}`,
              timestamp: new Date().toISOString(),
              thinkingLogs: currentLogs,
              thinkingTime: thinkingTime > 0 ? thinkingTime : 0,
            });
          } else {
            addMessage({
              id: `r-${t}`,
              ts: t,
              sender: "agent",
              text: "검토 완료: 대기 항목이 없습니다.",
              timestamp: new Date().toISOString(),
              thinkingLogs: currentLogs,
              thinkingTime: thinkingTime > 0 ? thinkingTime : 0,
            });
          }
          break;
        }

        case "DRAWING_EMPTY": {
          const t = Date.now();
          addMessage({
            id: `d-${t}`,
            ts: t,
            sender: "agent",
            text: "추출된 도면 데이터가 없습니다. 레이어가 켜져 있는지, 또는 선택된 객체가 유효한지 확인하세요.",
            timestamp: new Date().toISOString(),
          });
          setPipelineProgress(null);
          setAnalyzing(false);
          break;
        }

        case "DRAWING_RESYNC": {
          const payload = extractPayload(data);
          const t = Date.now();
          addMessage({
            id: `sync-${t}`,
            ts: t,
            sender: "agent",
            text: firstString(
              data.message,
              payload?.message,
              "도면 변경사항 저장 또는 재동기화가 진행 중입니다. 완료 후 다시 시도해 주세요.",
            ),
            timestamp: new Date().toISOString(),
          });
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          break;
        }

        case "CAD_DISCONNECTED": {
          const isReviewWaiting = useAgentStore.getState().isAnalyzing;
          if (!isReviewWaiting) break;

          const payload = extractPayload(data);
          const t = Date.now();
          addMessage({
            id: `cad-off-${t}`,
            ts: t,
            sender: "agent",
            text: firstString(
              data.message,
              payload?.message,
              "AutoCAD 플러그인이 연결되어 있지 않습니다. CAD Agent를 다시 연결한 뒤 검토해 주세요.",
            ),
            timestamp: new Date().toISOString(),
          });
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          break;
        }

        case "REVIEW_CANCELLED": {
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          break;
        }

        case "CHAT_CANCELLED": {
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          break;
        }

        case "ERROR": {
          alert(`서버 에러: ${data.message}`);
          setPipelineProgress(null);
          setAnalyzing(false);
          break;
        }

        case "DOMAIN_MISMATCH": {
          const p = extractPayload(data) || {};
          setAnalyzing(false);
          setDomainMismatch({
            predicted_domain: p.predicted_domain,
            predicted_domain_kr: p.predicted_domain_kr,
            original_domain: p.original_domain,
            original_domain_kr: p.original_domain_kr,
            probabilities: p.probabilities || {},
            session_id: (data.session_id as string) || "",
            message: p.message || "",
          });
          break;
        }

        case "SELECTION_CLEARED": {
          setActiveObjectIds([], "full");
          break;
        }

        case "FIX_RESULT": {
          const p = extractPayload(data) as {
            violation_id?: string;
            success?: boolean;
            applied_count?: number;
            total_count?: number;
            message?: string;
          } | undefined;
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          const t = Date.now();
          const appliedCount = Math.max(0, Number(p?.applied_count ?? 0) || 0);
          const totalCount = Math.max(0, Number(p?.total_count ?? 0) || 0);
          if (p?.success) {
            const violationId = String(p?.violation_id ?? "");
            const prev = fixSummaryRef.current ?? {
              id: `fix-summary-${t}`,
              approvedCount: 0,
              appliedCount: 0,
              violationIds: new Set<string>(),
            };
            const id = prev?.id ?? `fix-summary-${t}`;
            const nextViolationIds = new Set(prev.violationIds);
            let approveIncrement = 0;
            if (violationId === "ALL") {
              approveIncrement = totalCount || appliedCount || 1;
            } else if (violationId && !nextViolationIds.has(violationId)) {
              nextViolationIds.add(violationId);
              approveIncrement = 1;
            } else if (!violationId) {
              approveIncrement = 1;
            }
            const nextApprovedCount = prev.approvedCount + approveIncrement;
            const nextAppliedCount = prev.appliedCount + appliedCount;
            const timestamp = new Date().toISOString();
            const text = appliedCount > 0 || nextAppliedCount > 0
              ? `수정 승인 완료: 총 ${nextApprovedCount}건 처리 (자동 반영 ${nextAppliedCount}건)`
              : `수정 승인 완료: 총 ${nextApprovedCount}건 처리`;
            fixSummaryRef.current = {
              id,
              approvedCount: nextApprovedCount,
              appliedCount: nextAppliedCount,
              violationIds: nextViolationIds,
            };

            const { messages } = useAgentStore.getState();
            const exists = messages.some((m) => m.id === id);
            setMessages(
              exists
                ? messages.map((m) =>
                  m.id === id
                    ? { ...m, ts: t, text, timestamp }
                    : m,
                )
                : [
                  ...messages,
                  {
                    id,
                    ts: t,
                    sender: "agent",
                    text,
                    timestamp,
                  },
                ],
            );
          } else {
            addMessage({
              id: `fix-${t}`,
              ts: t,
              sender: "agent",
              text: p?.message ?? (p?.success
                ? "수정이 CAD에 반영되었습니다."
                : "자동 수정 항목이 없습니다. RevCloud 마크만 제거했습니다."),
              timestamp: new Date().toISOString(),
            });
          }
          break;
        }

        case "CREATE_RESULT": {
          const p = extractPayload(data) as {
            success?: boolean;
            type?: string;
            layer?: string;
            message?: string;
            batch_proposal_id?: string;
          } | undefined;
          // 배치 생성의 일부인 경우 개별 채팅 메시지 표시 안 함 (BATCH_PROPOSAL에서 처리)
          if (p?.batch_proposal_id) break;
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          const t = Date.now();
          addMessage({
            id: `cr-${t}`,
            ts: t,
            sender: "agent",
            text: p?.message ?? (p?.success
              ? `요청하신 객체를 CAD에 추가했습니다. 새 객체는 ${p?.layer ?? "AI_PROPOSAL"} 레이어에서 확인할 수 있습니다.`
              : "객체를 만들지 못했습니다. 입력한 좌표나 블록명이 올바른지 한 번 확인해 주세요."),
            timestamp: new Date().toISOString(),
          });
          break;
        }

        case "BATCH_PROPOSAL": {
          const p = extractPayload(data);
          setPipelineProgress(null);
          setAnalyzing(false);
          analysisStartedAt.current = null;
          if (p?.proposal_id) {
            const ptype = (p.proposal_type as string | undefined) ?? "create";
            useAgentStore.getState().addCreationProposal({
              proposal_id: String(p.proposal_id),
              count: Number(p.count ?? 0),
              handles: Array.isArray(p.handles) ? p.handles.map(String) : [],
              layers: Array.isArray(p.layers) ? p.layers.map(String) : [],
              description: String(p.description ?? `새 객체 ${p.count ?? 0}개를 생성하는 제안을 준비했습니다.`),
              delete_count: p.delete_count != null ? Number(p.delete_count) : undefined,
              proposal_type: ptype as "create" | "replace" | "modify",
            });
            // 검토 탭으로 안내 메시지
            const t = Date.now();
            const noticeText =
              ptype === "replace"
                ? `기존 객체 ${p.delete_count ?? 0}개를 새 객체 ${p.count ?? 0}개로 교체하는 제안을 준비했습니다. **수정 및 검토** 탭에서 승인하거나 취소할 수 있어요.`
                : ptype === "modify"
                ? `선택한 객체를 수정하는 제안을 준비했습니다. **수정 및 검토** 탭에서 승인하거나 취소할 수 있어요.`
                : `새 객체 ${p.count ?? 0}개를 생성하는 제안을 준비했습니다. **수정 및 검토** 탭에서 승인하거나 취소할 수 있어요.`;
            addMessage({
              id: `bp-${t}`,
              ts: t,
              sender: "agent",
              text: noticeText,
              timestamp: new Date().toISOString(),
            });
          }
          break;
        }

        default:
          break;
      }
    };

    socket.onclose = () => {
      if (connId.current !== myId) return;
      console.log("[React] WebSocket 연결 끊김 — 3초 후 재연결 시도");
      if (useAgentStore.getState().isAnalyzing) {
        const t = Date.now();
        addMessage({
          id: `ws-close-${t}`,
          ts: t,
          sender: "agent",
          text: "서버 연결이 끊겨 진행 중인 요청을 중단했습니다. 연결이 복구되면 다시 시도해 주세요.",
          timestamp: new Date().toISOString(),
        });
      }
      setPipelineProgress(null);
      setAnalyzing(false);
      analysisStartedAt.current = null;
      setIsConnected(false);
      setConnectionStatus("disconnected");
      clearTimers();
      reconnectTimer.current = setTimeout(() => {
        setConnectionStatus("connecting");
        connect();
      }, RECONNECT_DELAY_MS);
    };

    socket.onerror = () => {
      setPipelineProgress(null);
      setAnalyzing(false);
      analysisStartedAt.current = null;
      setIsConnected(false);
      setConnectionStatus("disconnected");
      socket.close();
    };
  }, [
    addMessage,
    setMessages,
    setAnalyzing,
    setPipelineProgress,
    setReviewResults,
    setActiveObjectIds,
    setCadSessionId,
    setDomainMismatch,
  ]);

  useEffect(() => {
    connect();
    return () => {
      connId.current = -1;
      clearTimers();
      if (ws.current) {
        ws.current.onclose = null;
        ws.current.close();
      }
    };
  }, [connect]);

  const sendMessage = useCallback((action: string, payload: unknown) => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      const sessionId =
        payload &&
        typeof payload === "object" &&
        "session_id" in payload &&
        typeof (payload as { session_id?: unknown }).session_id === "string"
          ? (payload as { session_id: string }).session_id
          : undefined;
      ws.current.send(
        JSON.stringify({
          action,
          payload,
          ...(sessionId ? { session_id: sessionId } : {}),
        }),
      );
    } else {
      console.error("[React] 소켓 미연결 (readyState:", ws.current?.readyState, ")");
    }
  }, []);

  return { sendMessage, isConnected, connectionStatus, viewCenter };
};
