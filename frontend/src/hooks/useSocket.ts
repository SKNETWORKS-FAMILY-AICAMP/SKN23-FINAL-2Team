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

export const useSocket = () => {
  const ws = useRef<WebSocket | null>(null);
  const pingTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connId = useRef(0);
  const [isConnected, setIsConnected] = useState(false);
  const analysisStartedAt = useRef<number | null>(null);

  const setAnalyzing       = useAgentStore((state) => state.setAnalyzing);
  const setPipelineProgress = useAgentStore((state) => state.setPipelineProgress);
  const setReviewResults   = useAgentStore((state) => state.setReviewResults);
  const addMessage         = useAgentStore((state) => state.addMessage);
  const setActiveObjectIds = useAgentStore((s) => s.setActiveObjectIds);
  const setCadSessionId    = useAgentStore((state) => state.setCadSessionId);
  const setDomainMismatch  = useAgentStore((state) => state.setDomainMismatch);

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
    const socket = new WebSocket(WS_URL);
    ws.current = socket;

    socket.onopen = () => {
      if (connId.current !== myId) { socket.close(); return; }
      console.log("[React] WebSocket 연결 성공");
      setIsConnected(true);
      pingTimer.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ action: "PING" }));
        }
      }, PING_INTERVAL_MS);
    };

    socket.onmessage = (event) => {
      if (connId.current !== myId) return;
      const data = JSON.parse(event.data);

      switch (data.action) {
        case "PONG":
          break;

        case "CAD_DATA_READY": {
          console.log("Receive CAD_DATA_READY:", data);
          const extractedIds: string[] | undefined =
            data.payload?.active_object_ids || data.payload?.activeObjectIds;
          const rawType = data.payload?.extraction_type as string | undefined;
          const mode = rawType === "focus" ? "focus" : "full";

          if (extractedIds && Array.isArray(extractedIds)) {
            setActiveObjectIds(extractedIds, mode);
          } else {
            setActiveObjectIds([], "full");
          }

          if (data.session_id) {
            const unmappedLayers = Array.isArray(data.payload?.unmapped_layers)
              ? data.payload.unmapped_layers
              : undefined;
            setCadSessionId(data.session_id, unmappedLayers);
            try {
              localStorage.setItem(LAST_CAD_SESSION_STORAGE_KEY, String(data.session_id));
            } catch { /* noop */ }
          }
          break;
        }

        case "CAD_SELECTION_CHANGED":
        case "CAD_DATA_EXTRACTED": {
          const selIds: string[] | undefined =
            data.payload?.active_object_ids || data.payload?.activeObjectIds;
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
          const hasSources = Array.isArray(data.sources) && data.sources.length > 0;
          addMessage({
            id: `c-${t}`,
            ts: t,
            sender: "agent",
            text: data.message ?? data.payload?.message ?? "",
            timestamp: new Date().toISOString(),
            sources: hasSources ? data.sources : undefined,
            streaming: true,
            thinkingLogs: currentLogs,
            thinkingTime: thinkingTime > 0 ? thinkingTime : 0,
          });
          break;
        }

        case "ANALYSIS_STARTED": {
          analysisStartedAt.current = Date.now();
          setAnalyzing(true);
          setPipelineProgress({
            message: data.message || "분석 시작 중...",
            stepMs: 0,
            totalMs: 0,
            pipelineStartedAtIso: new Date().toISOString(),
          });
          break;
        }

        case "AGENT_PROGRESS": {
          if (data.message) {
            setPipelineProgress({
              message: data.message,
              stepMs: 0,
              totalMs: 0,
              pipelineStartedAtIso: new Date().toISOString(),
            });
          } else {
            setPipelineProgress(null);
          }
          break;
        }

        case "PIPELINE_PROGRESS": {
          const p = data.payload as {
            message?: string;
            total_elapsed_ms?: number;
            step_elapsed_ms?: number;
            stage?: string;
            pipeline_started_at?: string;
          } | null;
          if (!p?.message) break;
          if (p.stage === "cad_data_ready") {
            // ✅ 수정: cad_data_ready는 도면 데이터가 백엔드에 도착한 중간 단계.
            // 분석을 종료하지 않고, 진행 메시지만 업데이트 (isAnalyzing 유지).
            // 이전 코드: setAnalyzing(false) → pendingReviewRef 기반 검토 트리거가 차단되던 버그
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
          const currentLogs = useAgentStore.getState().progressLogs;
          const startTime = analysisStartedAt.current || Date.now();
          const thinkingTime = Math.floor((Date.now() - startTime) / 1000);

          const entities = data.payload?.annotated_entities || [];
          setReviewResults(data.payload);
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
              id: `r-${t}`, ts: t, sender: "agent",
              text: `${n}건의 검토·수정 항목이 있습니다.${tail}`,
              timestamp: new Date().toISOString(),
              thinkingLogs: currentLogs,
              thinkingTime: thinkingTime > 0 ? thinkingTime : 0,
            });
          } else {
            addMessage({
              id: `r-${t}`, ts: t, sender: "agent",
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
            id: `d-${t}`, ts: t, sender: "agent",
            text: "추출된 도면 데이터가 없습니다. 레이어가 켜져 있는지, 또는 선택된 객체가 유효한지 확인하세요.",
            timestamp: new Date().toISOString(),
          });
          setPipelineProgress(null);
          setAnalyzing(false);
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
          const p = data.payload || {};
          setAnalyzing(false);
          setDomainMismatch({
            predicted_domain:    p.predicted_domain,
            predicted_domain_kr: p.predicted_domain_kr,
            original_domain:     p.original_domain,
            original_domain_kr:  p.original_domain_kr,
            probabilities:       p.probabilities || {},
            session_id:          data.session_id || "",
            message:             p.message || "",
          });
          break;
        }

        case "FIX_RESULT": {
          const p = data.payload as {
            violation_id?: string;
            success?: boolean;
            applied_count?: number;
            total_count?: number;
            message?: string;
          } | null;
          const t = Date.now();
          addMessage({
            id: `fix-${t}`,
            ts: t,
            sender: "agent",
            text: p?.message ?? (p?.success
              ? "수정이 CAD에 반영되었습니다."
              : "자동 수정 항목이 없습니다. RevCloud 마크만 제거했습니다."),
            timestamp: new Date().toISOString(),
          });
          break;
        }

        case "CREATE_RESULT": {
          // DrawCommandParser → CAD_ACTION → C# HandleCadAction → CREATE_RESULT 피드백
          const p = data.payload as {
            success?: boolean;
            type?: string;
            layer?: string;
            message?: string;
          } | null;
          const t = Date.now();
          addMessage({
            id: `cr-${t}`,
            ts: t,
            sender: "agent",
            text: p?.message ?? (p?.success
              ? `새 객체가 CAD에 추가되었습니다 (레이어: ${p?.layer ?? "AI_PROPOSAL"}).`
              : "객체 생성 실패: 좌표 또는 블록명을 확인하세요."),
            timestamp: new Date().toISOString(),
          });
          break;
        }

        default:
          break;
      }
    };

    socket.onclose = () => {
      if (connId.current !== myId) return;
      console.log("[React] WebSocket 연결 끊김 — 3초 후 재연결 시도");
      setIsConnected(false);
      clearTimers();
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    socket.onerror = () => {
      setIsConnected(false);
      socket.close();
    };
  }, [
    addMessage,
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

  return { sendMessage, isConnected };
};
