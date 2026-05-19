import React, { useState, useEffect, useRef, useCallback } from "react";
import { useSocket, LAST_CAD_SESSION_STORAGE_KEY } from "../hooks/useSocket";
import { useAgentStore } from "../store/agentStore";
import {
  fetchSessionList,
  fetchSessionMessages,
  createSession,
  addMessage as apiAddMessage,
  deleteSession,
  type SessionSummary,
} from "../api/chatApi";
import { agentApi } from "../api/agentApi";
import { C } from "../constants/theme";

// 하위 UI 컴포넌트 임포트
import AgentSelectGrid from "./chat/AgentSelectGrid";
import ChatStatusStrip from "./chat/ChatStatusStrip";
import ChatMessageRow from "./chat/ChatMessageRow";
import { Header, TabBar, InputBar } from "./chat/ChatLayout";
import { ViolationPane } from "./cad/ViolationReview";
import { getDocumentApiHeaders, isAgentApiRegistered } from "../utils/documentApiAuth";
import DogLoadingGame from "./chat/loading/DogLoadingGame";
import TetrisLoadingGame from "./chat/loading/TetrisLoadingGame";

type View = "idle" | "sessions" | "chat";
type TabId = "chat" | "violations";
type LoadingGameKind = "dog" | "tetris";

const fmtDate = (iso: string) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
};

const isStartReviewNotFound = (error: unknown) =>
  (error as any)?.response?.status === 404;

const startReviewErrorDetail = (error: unknown) => {
  const detail = (error as any)?.response?.data?.detail;
  return typeof detail === "string" && detail.trim() ? detail.trim() : "";
};

const DOCUMENT_API_BASE =
  ((import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ||
    "http://localhost:8000") + "/api/v1/documents";
const PROJECT_ID_STORAGE_KEY = "skn23_current_project_id";

const filterExistingTempSpecIds = async (ids: string[]) => {
  const orgId = localStorage.getItem("skn23_org_id") || "";
  if (localStorage.getItem("skn23_api_key_registered") !== "true") return [];
  const uniqueIds = [...new Set(ids.map((id) => id.trim()).filter(Boolean))];
  if (!orgId || uniqueIds.length === 0) return [];

  try {
    const res = await fetch(`${DOCUMENT_API_BASE}/temp?org_id=${encodeURIComponent(orgId)}`, {
      headers: { ...getDocumentApiHeaders() },
    });
    if (!res.ok) return [];
    const data = await res.json();
    const existing = new Set(
      (data.documents || []).map((doc: { id?: unknown }) => String(doc.id || "")),
    );
    return uniqueIds.filter((id) => existing.has(id));
  } catch {
    return [];
  }
};

const fetchProjectTempSpecIds = async (projectId: string) => {
  const orgId = localStorage.getItem("skn23_org_id") || "";
  if (localStorage.getItem("skn23_api_key_registered") !== "true") return [];
  if (!orgId || !projectId) return [];
  try {
    const res = await fetch(
      `${DOCUMENT_API_BASE}/temp/projects/${encodeURIComponent(projectId)}/specs?org_id=${encodeURIComponent(orgId)}`,
      { headers: { ...getDocumentApiHeaders() } },
    );
    if (!res.ok) return [];
    const data = await res.json();
    return (data.documents || [])
      .map((doc: { temp_document_id?: unknown }) => String(doc.temp_document_id || ""))
      .filter(Boolean);
  } catch {
    return [];
  }
};

export default function ChatApp() {
  const { sendMessage, isConnected, connectionStatus, viewCenter } = useSocket();
  const {
    messages: chat,
    reviewResults: violations,
    isAnalyzing: reviewing,
    setAnalyzing,
    addMessage,
    setMessages,
    clearResults,
    clearCadSessionId,
    activeObjectIds,
    selectedSpecIds,
    selectedTempSpecIds,
    setSelectedTempSpecIds,
    cadEvent,
    domainMismatch,
    clearDomainMismatch,
    progressLine,
    setActiveObjectIds,
    drawingState,
    fileFingerprint,
    pendingCreationProposals,
    removeCreationProposal,
  } = useAgentStore();

  const [view, setView] = useState<View>("idle");
  const [agent, setAgent] = useState("");
  const [dwg, setDwg] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [resolved, setResolved] = useState<Set<string>>(new Set());
  const [input, setInput] = useState("");
  const [activeTab, setActiveTab] = useState<TabId>("chat");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [showApiAlert, setShowApiAlert] = useState(false);
  const [showDomainDropdown, setShowDomainDropdown] = useState(false);
  const [buttonReviewing, setButtonReviewing] = useState(false);
  const [chatGenerating, setChatGenerating] = useState(false);
  const [loadingGameKind, setLoadingGameKind] = useState<LoadingGameKind>("dog");
  const [loadingGameOpen, setLoadingGameOpen] = useState(false);
  const [loadingDots, setLoadingDots] = useState("");

  const chatScrollRef = useRef<HTMLDivElement>(null);
  const chatRequestIdRef = useRef(0);
  const sessionIdRef = useRef("");
  const prevLoadingGameActiveRef = useRef(false);
  const reviewTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingReviewRef = useRef<{
    domain: string;
    mode: "KEC_ONLY" | "HYBRID";
    prompt: string;
  } | null>(null);
  // drawingReviewBlocked 해제 시 자동 검토 시작을 위한 펜딩 파라미터
  const pendingReviewOnReadyRef = useRef<{
    domain: string;
    mode: "KEC_ONLY" | "HYBRID";
    prompt: string;
  } | null>(null);
  const pendingReviewNoticeIdRef = useRef<string | null>(null);

  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  const pushAgent = useCallback((txt: string) => {
    addMessage({ id: Date.now().toString(), sender: "agent", text: txt, timestamp: new Date().toISOString() });
  }, [addMessage]);

  const upsertAgentNotice = useCallback((id: string, txt: string) => {
    const timestamp = new Date().toISOString();
    const messages = useAgentStore.getState().messages;
    if (messages.some((m) => m.id === id)) {
      setMessages(
        messages.map((m) =>
          m.id === id ? { ...m, text: txt, timestamp, ts: Date.now() } : m,
        ),
      );
      return;
    }
    addMessage({ id, sender: "agent", text: txt, timestamp, ts: Date.now() });
  }, [addMessage, setMessages]);

  const clearReviewTimeout = useCallback(() => {
    if (reviewTimeoutRef.current !== null) {
      clearTimeout(reviewTimeoutRef.current);
      reviewTimeoutRef.current = null;
    }
  }, []);

  const armReviewTimeout = useCallback(() => {
    clearReviewTimeout();
  }, [clearReviewTimeout]);

  const pendingFixCount = (violations as any[]).filter(
    (v) =>
      v?.violation?.id != null &&
      v.violation.id !== "" &&
      !resolved.has(String(v.violation.id)),
  ).length;
  const selectedObjectCount = activeObjectIds?.length || 0;
  const loadingGameActive = reviewing || chatGenerating;
  const drawingReviewBlocked =
    drawingState?.changeMode === "INCREMENTAL_PENDING" ||
    drawingState?.changeMode === "FULL_RESYNC_PENDING" ||
    drawingState?.changeMode === "FULL_RESYNC_RUNNING" ||
    drawingState?.status === "INCREMENTAL_PENDING" ||
    drawingState?.status === "FULL_RESYNC_PENDING" ||
    drawingState?.status === "FULL_RESYNC_RUNNING" ||
    (drawingState?.status === "RESYNC_FAILED" && !drawingState.canReviewStaleSnapshot);
  const reviewButtonDisabled = !isConnected;

  const isApiRegistered = () => isAgentApiRegistered();

  useEffect(() => {
    if (view !== "chat" || activeTab !== "chat") return;
    const el = chatScrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    const t = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(t);
  }, [view, activeTab, chat, reviewing, chatGenerating, progressLine]);

  // 분석 중 마침표 애니메이션 로직
  useEffect(() => {
    if (!buttonReviewing) {
      setLoadingDots("");
      return;
    }
    const interval = setInterval(() => {
      setLoadingDots((prev) => (prev.length >= 3 ? "" : prev + "."));
    }, 500);
    return () => clearInterval(interval);
  }, [buttonReviewing]);

  useEffect(() => {
    if (loadingGameActive && !prevLoadingGameActiveRef.current) {
      setLoadingGameKind(Math.random() < 0.5 ? "dog" : "tetris");
      setLoadingGameOpen(false);
    }
    if (!loadingGameActive) {
      setLoadingGameOpen(false);
    }
    prevLoadingGameActiveRef.current = loadingGameActive;
  }, [loadingGameActive]);

  useEffect(() => {
    if (view === "sessions") {
      setActiveObjectIds([], "full");
    }
  }, [view, setActiveObjectIds]);

  useEffect(() => {
    if (!reviewing) {
      pendingReviewRef.current = null;
      setButtonReviewing(false);
      setChatGenerating(false);
      clearReviewTimeout();
    }
  }, [reviewing, clearReviewTimeout]);

  useEffect(() => {
    if (!reviewing || !progressLine?.message) return;
    armReviewTimeout();
  }, [reviewing, progressLine?.message, progressLine?.stage, progressLine?.totalMs, armReviewTimeout]);

  const clearReviewUiFully = useCallback(() => {
    pendingReviewRef.current = null;
    pendingReviewOnReadyRef.current = null;
    pendingReviewNoticeIdRef.current = null;
    clearResults();
    setResolved(new Set());
    setActiveTab("chat");
    setButtonReviewing(false);
    setChatGenerating(false);
    if (window.chrome?.webview) {
      try {
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_CAD_REVIEW" }));
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_SELECTION_CACHE" }));
      } catch { }
    }
  }, [clearResults]);

  const clearProjectSpecLinkState = useCallback(() => {
    localStorage.removeItem(PROJECT_ID_STORAGE_KEY);
    setSelectedTempSpecIds([]);
  }, [setSelectedTempSpecIds]);

  const handleClearSelection = useCallback(() => {
    setActiveObjectIds([], "full");
    if (window.chrome?.webview) {
      try {
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_SELECTION_CACHE" }));
      } catch { }
    }
  }, [setActiveObjectIds]);

  const openAgent = useCallback(
    async (id: string, file: string) => {
      if (!isApiRegistered()) {
        setShowApiAlert(true);
        return;
      }
      setAgent(id);
      setDwg(file);
      clearReviewUiFully();
      setSelectedTempSpecIds([]);
      
      // 즉시 채팅창으로 이동하여 체감 속도 향상
      setView("chat");
      setInput("");
      setMessages([]);

      // 세션 목록만 로드 (세션은 첫 메시지/분석 시 생성)
      setSessionId("");
      try {
        const list = await fetchSessionList(id).catch(() => []);
        setSessions(list);
      } catch (e) {
        console.error("세션 목록 로드 실패:", e);
      }
    },
    [clearReviewUiFully, setMessages, setSelectedTempSpecIds],
  );

  useEffect(() => {
    const sendSessionInfoToCSharp = () => {
      const orgId = localStorage.getItem("skn23_org_id") || "";
      const deviceId = localStorage.getItem("skn23_device_id") || "";
      if (orgId && deviceId && window.chrome?.webview) {
        window.chrome.webview.postMessage(
          JSON.stringify({
            action: "SESSION_INFO",
            payload: { org_id: orgId, device_id: deviceId },
          }),
        );
      }
    };

    const handleCSharpMessage = (event: Event) => {
      try {
        const msg = JSON.parse((event as any).data);
        if (msg.action === "OPEN_AGENT") openAgent(msg.payload.agent, msg.payload.dwg);
        if (msg.action === "DWG_CHANGED") {
          clearProjectSpecLinkState();
          const nextDwg = String(msg.payload?.dwg || "").trim();
          if (nextDwg) setDwg(nextDwg);
        }
        if (msg.action === "MACHINE_INFO" && msg.payload) {
          localStorage.setItem("skn23_machine_id", msg.payload.machine_id || "");
          localStorage.setItem("skn23_hostname", msg.payload.hostname || "");
          localStorage.setItem("skn23_os_user", msg.payload.os_user || "");
          sendSessionInfoToCSharp();
        }
        if (msg.action === "TEMP_SPEC_LINK_LOADED") {
          const org = localStorage.getItem("skn23_org_id") || "";
          const payloadOrg = msg.payload.org_id as string | undefined;
          if (payloadOrg && org && payloadOrg !== org) {
            clearProjectSpecLinkState();
            return;
          }
          if (localStorage.getItem("skn23_api_key_registered") !== "true" || !org) {
            clearProjectSpecLinkState();
            return;
          }
          const projectId = String(msg.payload?.project_id || "").trim();
          if (projectId) {
            localStorage.setItem(PROJECT_ID_STORAGE_KEY, projectId);
            void fetchProjectTempSpecIds(projectId).then((ids) => {
              setSelectedTempSpecIds(ids);
            });
            return;
          }
          if (!Array.isArray(msg.payload?.documents)) return;
          const ids = (msg.payload.documents as { temp_document_id?: string }[])
            .map((d) => d.temp_document_id)
            .filter((id): id is string => !!id);
          if (ids.length === 0) {
            setSelectedTempSpecIds([]);
            return;
          }
          void filterExistingTempSpecIds(ids).then(setSelectedTempSpecIds);
        }
        if (msg.action === "TEMP_SPEC_LINK_CLEARED") {
          clearProjectSpecLinkState();
        }
        if (msg.action === "CAD_ANALYZE_COMPLETE" && msg.session_id) {
          const id = String(msg.session_id).trim();
          if (id) {
            useAgentStore.getState().setCadSessionId(id, []);
            try { localStorage.setItem(LAST_CAD_SESSION_STORAGE_KEY, id); } catch { }
          }
        }
        if (msg.action === "CLEAR_REVIEW_UI") {
          useAgentStore.getState().clearResults();
          setResolved(new Set());
          setActiveTab("chat");
        }
        if (msg.action === "CAD_SELECTION_CHANGED") {
          const ids: string[] = Array.isArray(msg.payload?.active_object_ids)
            ? msg.payload.active_object_ids
            : Array.isArray(msg.payload?.handles)
            ? msg.payload.handles
            : [];
          useAgentStore.getState().setActiveObjectIds(ids, "focus");
        }
        if (msg.action === "SHOW_VIOLATION_DETAIL") {
          const vid = msg.payload?.violation_id;
          if (vid) {
            (useAgentStore.getState() as any).setSelectedViolationId?.(String(vid));
            setActiveTab("violations");
          }
        }
      } catch { }
    };

    if (window.chrome?.webview) {
      window.chrome.webview.addEventListener("message", handleCSharpMessage);
      window.chrome.webview.postMessage(JSON.stringify({ action: "REACT_READY" }));
      sendSessionInfoToCSharp();
    }
    return () => window.chrome?.webview?.removeEventListener("message", handleCSharpMessage);
  }, [clearProjectSpecLinkState, openAgent, setSelectedTempSpecIds]);

  const pickSession = useCallback(
    async (sid: string) => {
      setSessionId(sid);
      clearResults();
      setResolved(new Set());
      setActiveTab("chat");
      const dbMessages = await fetchSessionMessages(sid).catch(() => []);
      if (dbMessages.length > 0) {
        setMessages(
          dbMessages
            .filter((m) => (m.role as string) !== "system") 
            .map((m) => ({
              id: m.id,
              sender: m.role === "user" ? "user" : "agent",
              text: m.content,
              timestamp: m.created_at,
            })),
        );
      } else {
        setMessages([]);
      }
      setView("chat");
    },
    [clearResults, setMessages],
  );

  const send = useCallback(
    async (txt: string) => {
      if (!isApiRegistered()) {
        setShowApiAlert(true);
        return;
      }
      if (!txt.trim() || !agent || !isConnected) return;

      const requestId = chatRequestIdRef.current + 1;
      chatRequestIdRef.current = requestId;
      const messageText = txt;
      
      // 1. 즉각적인 UI 업데이트 (Optimistic Update)
      addMessage({ id: Date.now().toString(), sender: "user", text: messageText, timestamp: new Date().toISOString() });
      setInput("");
      setView("chat");
      setChatGenerating(true);

      // 2. 세션 ID가 없으면 생성 시도
      let sid = sessionId;
      if (!sid) {
        try {
          const ns = await createSession(agent, dwg);
          sid = ns.id;
          setSessionId(sid);
          setSessions((p) => [ns, ...p]);
        } catch (e) {
          console.error("세션 생성 실패:", e);
          // 실패 시 sid는 ""로 유지됨
        }
      }

      // 3. 백엔드에 메시지 저장 시도
      try {
        await apiAddMessage(sid, "user", messageText);
      } catch (err) {
        console.error("Failed to save user message:", err);
      }

      const domainMap: Record<string, string> = {
        전기: "elec",
        배관: "pipe",
        건축: "arch",
        소방: "fire",
      };
      const chatDomain = domainMap[agent] || "pipe";

      let cadSid = (cadEvent?.sessionId || "").trim();
      if (!cadSid) {
        try {
          cadSid = (localStorage.getItem(LAST_CAD_SESSION_STORAGE_KEY) || "").trim();
        } catch {
          cadSid = "";
        }
      }

      if (chatRequestIdRef.current !== requestId) return;

      sendMessage("USER_CHAT", {
        text: txt,
        domain: chatDomain,
        session_id: sid,
        active_object_ids: activeObjectIds,
        cad_session_id: cadSid,
        ...(viewCenter ? { view_center_x: viewCenter.x, view_center_y: viewCenter.y } : {}),
      });
    },
    [
      sessionId,
      agent,
      dwg,
      isConnected,
      sendMessage,
      addMessage,
      activeObjectIds,
      cadEvent?.sessionId,
    ],
  );

  const cancelReview = useCallback(() => {
    pendingReviewRef.current = null;
    pendingReviewOnReadyRef.current = null;
    pendingReviewNoticeIdRef.current = null;
    let cadSid = (cadEvent?.sessionId || "").trim();
    if (!cadSid) {
      try {
        cadSid = (localStorage.getItem(LAST_CAD_SESSION_STORAGE_KEY) || "").trim();
      } catch {
        cadSid = "";
      }
    }
    sendMessage("CANCEL_REVIEW", {
      session_id: sessionId,
      cad_session_id: cadSid,
    });
    setButtonReviewing(false);
    setAnalyzing(false);
    addMessage({
      id: Date.now().toString(),
      sender: "system",
      text: "사용자에 의해 분석이 중단되었습니다.",
      timestamp: new Date().toISOString(),
    });
  }, [addMessage, cadEvent?.sessionId, sendMessage, sessionId, setAnalyzing]);

  const cancelChatGeneration = useCallback(() => {
    chatRequestIdRef.current += 1;
    sendMessage("CANCEL_CHAT", { session_id: sessionId });
    setChatGenerating(false);
    setAnalyzing(false);
    addMessage({
      id: Date.now().toString(),
      sender: "system",
      text: "에이전트 응답 생성을 중단했습니다.",
      timestamp: new Date().toISOString(),
    });
  }, [addMessage, sendMessage, sessionId, setAnalyzing]);

  useEffect(() => {
    if (!cadEvent || !pendingReviewRef.current) return;
    const { domain, mode, prompt } = pendingReviewRef.current;
    pendingReviewRef.current = null;
    const cadCacheId = cadEvent.sessionId;

    const doReview = async () => {
      let sid = sessionId;
      if (!sid) {
        try {
          const ns = await createSession(agent, dwg);
          sid = ns.id;
          setSessionId(sid);
          setSessions((p) => [ns, ...p]);
        } catch {
          pushAgent("세션 생성에 실패했습니다.");
          setAnalyzing(false);
          return;
        }
      }
      const latestStore = useAgentStore.getState();
      const requestSpecIds = latestStore.selectedSpecIds;
      const requestTempSpecIds = latestStore.selectedTempSpecIds;
      const requestProjectId = localStorage.getItem(PROJECT_ID_STORAGE_KEY) || "";
      agentApi
        .startReview({
          sessionId: sid,
          cadCacheId,
          drawingId: drawingState?.drawingId || undefined,
          fileFingerprint: fileFingerprint || undefined,
          domain,
          activeObjectIds: activeObjectIds,
          reviewMode: mode,
          userPrompt: prompt,
          specDocumentIds: requestSpecIds,
          tempSpecIds: requestTempSpecIds,
          projectId: requestProjectId || undefined,
          orgId: localStorage.getItem("skn23_org_id") || "",
        })
        .catch((error) => {
          if (requestTempSpecIds.length > 0 && isStartReviewNotFound(error)) {
            setSelectedTempSpecIds([]);
            pushAgent("선택된 임시 시방서를 찾을 수 없어 시방서 선택을 해제했습니다. 다시 검토를 실행해 주세요.");
          } else {
            pushAgent("분석 요청 중 오류가 발생했습니다.");
          }
          setAnalyzing(false);
        });
      armReviewTimeout();
    };
    doReview();
  }, [
    cadEvent,
    sessionId,
    agent,
    dwg,
    activeObjectIds,
    selectedSpecIds,
    selectedTempSpecIds,
    setSelectedTempSpecIds,
    setAnalyzing,
    clearCadSessionId,
  ]);

  const runDrawReviewExtract = async (preferredSessionId?: string) => {
    const domainMap: Record<string, string> = {
      전기: "elec",
      배관: "pipe",
      건축: "arch",
      소방: "fire",
    };
    const domainCode = domainMap[agent] || "elec";
    const latestStore = useAgentStore.getState();
    const requestSpecIds = latestStore.selectedSpecIds;
    const requestTempSpecIds = latestStore.selectedTempSpecIds;
    const requestProjectId = localStorage.getItem(PROJECT_ID_STORAGE_KEY) || "";
    const hasSpecs = requestSpecIds.length > 0 || requestTempSpecIds.length > 0 || !!requestProjectId;
    const mode = hasSpecs ? "HYBRID" : "KEC_ONLY";
    const nSel = activeObjectIds?.length || 0;
    const defaultReview =
      nSel > 0
        ? `CAD에서 선택한 ${nSel}개 객체에 대해 시방·규정 위반이 있는지 검토해 주세요.`
        : "도면 전체에 대해 시방·규정 위반을 전수 검토해 주세요.";

    if (drawingReviewBlocked) {
      pendingReviewOnReadyRef.current = { domain: domainCode, mode, prompt: defaultReview };
      const noticeId = pendingReviewNoticeIdRef.current ?? `review-wait-${Date.now()}`;
      pendingReviewNoticeIdRef.current = noticeId;
      setButtonReviewing(true);
      setAnalyzing(true);
      upsertAgentNotice(
        noticeId,
        "도면 변경사항 저장 또는 재동기화가 진행 중입니다. 완료되면 자동으로 검토를 시작합니다.",
      );
      return;
    }

    clearResults();
    setResolved(new Set());
    setActiveTab("chat");
    setButtonReviewing(true);
    setAnalyzing(true);
    if (window.chrome?.webview) {
      try {
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_CAD_REVIEW" }));
      } catch { }
    }

    if (
      drawingState?.changeMode === "FULL_RESYNC_PENDING" ||
      drawingState?.changeMode === "FULL_RESYNC_RUNNING"
    ) {
      pushAgent("도면 전체 재동기화가 필요하거나 진행 중입니다. 완료 후 다시 검토해 주세요.");
      setAnalyzing(false);
      setButtonReviewing(false);
      return;
    }

    const canSkipExtract =
      drawingState?.drawingId &&
      drawingState?.currentSnapshotId &&
      drawingState.status !== "DIRTY" &&
      drawingState.status !== "INCREMENTAL_PENDING" &&
      drawingState.status !== "FULL_RESYNC_PENDING" &&
      drawingState.status !== "FULL_RESYNC_RUNNING" &&
      drawingState.changeMode !== "INCREMENTAL_PENDING" &&
      drawingState.changeMode !== "FULL_RESYNC_PENDING" &&
      drawingState.changeMode !== "FULL_RESYNC_RUNNING";

    if (canSkipExtract) {
      // S3에 snapshot이 있으므로 CAD 재추출 없이 바로 검토 요청
      let sid = (preferredSessionId || sessionId || "").trim();
      if (!sid) {
        try {
          const ns = await createSession(agent, dwg);
          sid = ns.id;
          setSessionId(sid);
          setSessions((p) => [ns, ...p]);
        } catch {
          pushAgent("세션 생성에 실패했습니다.");
          setButtonReviewing(false);
          setAnalyzing(false);
          return;
        }
      }
      const orgId = localStorage.getItem("skn23_org_id") || "";
      agentApi.startReview({
        sessionId: sid,
        cadCacheId: cadEvent?.sessionId || undefined,
        drawingId: drawingState!.drawingId,
        fileFingerprint: fileFingerprint || undefined,
        domain: domainCode,
        activeObjectIds,
        reviewMode: mode,
        userPrompt: defaultReview,
        specDocumentIds: requestSpecIds,
        tempSpecIds: requestTempSpecIds,
        projectId: requestProjectId || undefined,
        orgId,
      }).catch((error) => {
        const detail = startReviewErrorDetail(error);
        pushAgent(detail ? `분석 요청 중 오류가 발생했습니다: ${detail}` : "분석 요청 중 오류가 발생했습니다.");
        setButtonReviewing(false);
        setAnalyzing(false);
      });
      // Keep a stall guard, but refresh it whenever backend progress arrives.
      armReviewTimeout();
    } else {
      // snapshot 없음 → CAD 전체 추출 필요
      pendingReviewRef.current = { domain: domainCode, mode, prompt: defaultReview };
      clearCadSessionId();
      sendMessage("EXTRACT_DATA", { domain: domainCode });
    }
  };

  useEffect(() => {
    if (drawingReviewBlocked || !pendingReviewOnReadyRef.current) return;
    pendingReviewNoticeIdRef.current = null;
    pendingReviewOnReadyRef.current = null;
    void runDrawReviewExtract(sessionId);
  }, [
    drawingReviewBlocked,
    drawingState?.changeMode,
    drawingState?.status,
    drawingState?.currentSnapshotId,
    sessionId,
  ]);

  const handleDomainContinue = async () => {
    if (!domainMismatch) return;
    clearDomainMismatch();
    clearResults();
    setResolved(new Set());
    setButtonReviewing(true);
    setAnalyzing(true);
    pushAgent(`${domainMismatch.original_domain_kr} 에이전트로 계속 진행합니다...`);

    let sid = sessionId;
    if (!sid) {
      try {
        const ns = await createSession(agent, dwg);
        sid = ns.id;
        setSessionId(sid);
        setSessions((p) => [ns, ...p]);
      } catch {
        pushAgent("세션 생성에 실패했습니다.");
        setButtonReviewing(false);
        setAnalyzing(false);
        return;
      }
    }
    const cadCacheId = cadEvent?.sessionId ?? "";
    const latestStore = useAgentStore.getState();
    const requestSpecIds = latestStore.selectedSpecIds;
    const requestTempSpecIds = latestStore.selectedTempSpecIds;
    const requestProjectId = localStorage.getItem(PROJECT_ID_STORAGE_KEY) || "";
    agentApi
      .confirmReview({
        sessionId: sid,
        cadCacheId,
        drawingId: drawingState?.drawingId || undefined,
        fileFingerprint: fileFingerprint || undefined,
        domain: domainMismatch.original_domain,
        activeObjectIds,
        reviewMode: (requestSpecIds.length > 0 || requestTempSpecIds.length > 0 || !!requestProjectId) ? "HYBRID" : "KEC_ONLY",
        specDocumentIds: requestSpecIds,
        tempSpecIds: requestTempSpecIds,
        projectId: requestProjectId || undefined,
        orgId: localStorage.getItem("skn23_org_id") || "",
      })
      .catch((error) => {
        if (requestTempSpecIds.length > 0 && isStartReviewNotFound(error)) {
          setSelectedTempSpecIds([]);
          pushAgent("선택된 임시 시방서를 찾을 수 없어 시방서 선택을 해제했습니다. 다시 검토를 실행해 주세요.");
        } else {
          pushAgent("분석 요청 중 오류가 발생했습니다.");
        }
        setAnalyzing(false);
      });
    armReviewTimeout();
  };

  const handleDomainReselect = () => {
    clearDomainMismatch();
    setShowDomainDropdown(true);
  };

  const newSession = async () => {
    if (!isApiRegistered()) { setShowApiAlert(true); return; }
    if (!agent || agent === "자유질의") {
      setView("idle");
      return;
    }
    // 세션은 첫 메시지/분석 시 생성 — 여기서는 UI만 초기화
    setSessionId("");
    setInput("");
    clearReviewUiFully();
    setSelectedTempSpecIds([]);
    setMessages([]);
    setView("chat");
  };

  const newSessionAndStartReview = async () => {
    if (!isApiRegistered()) { setShowApiAlert(true); return; }
    if (reviewing) return;
    if (!isConnected) return;
    if (!agent || agent === "자유질의") return;
    const ns = await createSession(agent, dwg);
    sessionIdRef.current = ns.id;
    setSessionId(ns.id);
    setSessions((p) => [ns, ...p]);
    clearReviewUiFully();
    setSelectedTempSpecIds([]);
    setMessages([]);
    setView("chat");
    void runDrawReviewExtract(ns.id);
  };

  const startReview = async (fromListSessionId?: string) => {
    if (!isApiRegistered()) { setShowApiAlert(true); return; }
    if (reviewing) return;
    if (typeof fromListSessionId === "string" && fromListSessionId) {
      setSessionId(fromListSessionId);
      const dbMessages = await fetchSessionMessages(fromListSessionId).catch(() => []);
      if (dbMessages.length > 0) {
        setMessages(
          dbMessages
            .filter((m) => (m.role as string) !== "system") 
            .map((m) => ({
              id: m.id,
              ts: new Date(m.created_at).getTime(),
              sender: m.role === "user" ? "user" : "agent",
              text: m.content,
            }))
        );
      } else {
        setMessages([]);
      }
      setView("chat");
    }
    void runDrawReviewExtract(fromListSessionId || sessionId);
  };

  const getViolationId = (item: any) =>
    item?.violation?.id != null ? String(item.violation.id) : "";

  const isReviewResultId = (id: string) => /^\d+$/.test(id.trim());

  const approve = async (vid: string) => {
    if (!sessionId) return;
    try {
      const targetIds =
        vid === "ALL"
          ? violations
            .map(getViolationId)
            .filter(Boolean)
        : [vid];
      const dbFixIds = targetIds.filter(isReviewResultId);

      if (vid === "ALL" && dbFixIds.length > 0) {
        await agentApi.confirmFixes({ session_id: sessionId, selected_fix_ids: dbFixIds });
      }
      sendMessage("APPROVE_FIX", { session_id: sessionId, violation_id: String(vid) });

      if (vid === "ALL") {
        setResolved(new Set(targetIds));
      } else {
        setResolved((p) => new Set([...p, String(vid)]));
      }
    } catch (error) {
      console.error("승인 처리 중 오류 발생:", error);
      alert("서버에 결과를 저장하는 중 문제가 발생했습니다.");
    }
  };

  const approveProposal = (proposalId: string) => {
    sendMessage("APPROVE_ENTITY", { proposal_id: proposalId });
    removeCreationProposal(proposalId);
  };

  const rejectProposal = (proposalId: string, handles: string[]) => {
    sendMessage("REJECT_ENTITY", { proposal_id: proposalId, handles });
    removeCreationProposal(proposalId);
  };

  const reject = async (vid: string) => {
    try {
      if (vid === "ALL" && sessionId) {
        await agentApi.confirmFixes({ session_id: sessionId, selected_fix_ids: [] });
      }

      sendMessage("REJECT_FIX", { session_id: sessionId, violation_id: String(vid) });
      if (vid === "ALL") {
        setResolved(
          new Set(
            violations
              .map(getViolationId)
              .filter(Boolean)
          )
        );
      } else {
        setResolved((p) => new Set([...p, String(vid)]));
      }
    } catch (error) {
      console.error("거부 처리 중 오류 발생:", error);
      alert("서버에 결과를 저장하는 중 문제가 발생했습니다.");
    }
  };

  const onKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send(input);
    }
  };

  return (
    <div className="h-screen flex flex-col bg-gradient-to-br from-[#020617] via-[#020817] to-[#07111f] text-white">
      {showApiAlert && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-[320px] rounded-2xl border border-slate-700 bg-[#07111f] p-6 shadow-2xl">
            <p className="text-sm font-semibold text-white mb-2">API 키 미등록</p>
            <p className="text-xs text-slate-400 leading-relaxed mb-6">
              에이전트를 사용하려면 먼저 API 키를 등록해야 합니다.
              <br />
              상단 메뉴 [Agent &gt; API키 관리...]에서 설정해주세요.
            </p>
            <button
              onClick={() => setShowApiAlert(false)}
              className="w-full py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-semibold text-white transition-all active:scale-95"
            >
              확인
            </button>
          </div>
        </div>
      )}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.5)" }}>
          <div className="rounded-lg shadow-xl px-5 py-4 w-56" style={{ background: C.card, border: `1px solid ${C.border}` }}>
            <p className="text-[13px] font-medium mb-1" style={{ color: C.textPrimary }}>대화 삭제</p>
            <p className="text-[11px] mb-4" style={{ color: C.textSub }}>이 대화를 삭제하시겠습니까?</p>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => setDeleteTarget(null)}
                className="px-3 py-1 rounded text-[12px] transition-colors"
                style={{ background: C.hover, color: C.textSub }}
              >
                취소
              </button>
              <button
                onClick={async () => {
                  const id = deleteTarget;
                  setDeleteTarget(null);
                  await deleteSession(id).catch(() => { });
                  setSessions((prev) => prev.filter((x) => x.id !== id));
                }}
                className="px-3 py-1 rounded text-[12px] font-medium transition-colors"
                style={{ background: "#c0392b", color: "#fff" }}
              >
                삭제
              </button>
            </div>
          </div>
        </div>
      )}

      {view === "idle" && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            <AgentSelectGrid selectedId="" onPick={(id) => void openAgent(id, "")} variant="idle" />
          </div>
        </div>
      )}

      {view === "sessions" && (
        <div className="flex flex-col flex-1 overflow-hidden">
          <Header
            agent={agent}
            dwg={dwg}
            activeCount={activeObjectIds?.length || 0}
            onAgentChange={(id: string) => openAgent(id, dwg)}
            onClearSelection={handleClearSelection}
            onNewChat={() => void newSession()}
            isConnected={isConnected}
            connectionStatus={connectionStatus}
          />
          <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
            {sessions.length === 0 && (
              <p className="text-center text-xs mt-10 text-slate-500">이전 대화가 없습니다.</p>
            )}
            {sessions.map((s) => {
              // 도메인별 이름 및 색상 매핑
              const domainMap: Record<string, { label: string; color: string }> = {
                elec: { label: "전기", color: "bg-yellow-500/10 text-yellow-500 border-yellow-500/20" },
                pipe: { label: "배관", color: "bg-blue-500/10 text-blue-500 border-blue-500/20" },
                arch: { label: "건축", color: "bg-emerald-500/10 text-emerald-500 border-emerald-500/20" },
                fire: { label: "소방", color: "bg-red-500/10 text-red-500 border-red-500/20" },
              };
              const domainInfo = domainMap[s.agent_type] || { label: s.agent_type, color: "bg-slate-500/10 text-slate-500 border-slate-500/20" };

              return (
                <div
                  key={s.id}
                  className="group flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900/40 px-4 py-3 hover:border-blue-500/50 transition-all duration-200"
                >
                  <button
                    type="button"
                    onClick={() => void pickSession(s.id)}
                    className="min-w-0 flex-1 text-left"
                  >
                    <div className="text-sm font-medium text-white truncate group-hover:text-blue-400 transition-colors">
                      {s.title || "새 대화"}
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5">
                      {s.dwg_filename ? `${s.dwg_filename} · ` : ""}{fmtDate(s.created_at)}
                    </div>
                  </button>
                  <div className="flex items-center gap-2 shrink-0 ml-2">
                    {/* 도메인 배지 */}
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${domainInfo.color} transition-all`}>
                      {domainInfo.label}
                    </span>
                    {/* 삭제 버튼 */}
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); setDeleteTarget(s.id); }}
                      className="opacity-0 group-hover:opacity-100 p-1.5 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-400/10 transition-all"
                      title="대화 삭제"
                    >
                      ✕
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

        </div>
      )}

      {view === "chat" && (
        <div className="flex flex-1 flex-col overflow-hidden">
          <Header
            agent={agent}
            onShowList={async () => {
              setView("sessions");
              setSessionId("");
              clearReviewUiFully();
              if (agent) {
                const list = await fetchSessionList(agent).catch(() => []);
                setSessions(list);
              }
            }}
            onNewChat={() => void newSession()}
            isConnected={isConnected}
            connectionStatus={connectionStatus}
          />
          <TabBar 
            activeTab={activeTab} 
            onTabChange={setActiveTab} 
            reviewPendingCount={pendingFixCount} 
            dwg={dwg}
            rightContent={null}
          />
          <div ref={chatScrollRef} className="flex-1 overflow-y-auto min-h-0 relative">
            {/* CHAT TAB */}
            <div className={`flex w-full flex-col gap-4 px-3.5 py-5 ${activeTab !== "chat" ? "hidden" : ""}`}>
              {pendingFixCount > 0 && (
                <div className="rounded-xl border border-blue-500/30 bg-blue-500/10 p-4 space-y-3 shadow-lg shadow-blue-500/5">
                  <p className="text-sm text-blue-200">
                    <span className="font-bold text-blue-400 mr-1">{pendingFixCount}건</span>
                    의 수정이 대기 중입니다. CAD에 반영하려면 수정 검토에서 승인하세요.
                  </p>
                  <button
                    type="button"
                    onClick={() => setActiveTab("violations")}
                    className="w-full py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-sm font-semibold text-white transition-all active:scale-95 shadow-lg shadow-blue-600/20"
                  >
                    수정 검토 이동
                  </button>
                </div>
              )}
              {chat.map((m, i) => (
                <ChatMessageRow
                  key={`${m.id}-${i}`}
                  message={m}
                  onSuggestQuery={(query) => void send(query)}
                />
              ))}
              {reviewing && (
                <div className="px-2" aria-live="polite">
                  <ChatStatusStrip agentName={agent} />
                </div>
              )}
              {loadingGameActive && (
                <div className="px-6">
                  <div className="rounded-lg border border-slate-800/70 bg-slate-950/20 px-3 py-2">
                    <div className="flex items-center justify-between gap-3">
                      <div className="flex min-w-0 items-center gap-2">
                        <span className="h-2 w-2 rounded-full bg-orange-500 shadow-[0_0_10px_rgba(249,115,22,0.75)]" />
                        <span className="truncate text-[12px] font-semibold text-orange-300">
                          대기 게임
                        </span>
                        <span className="truncate text-[11px] text-slate-500">
                          {loadingGameKind === "dog" ? "강아지 점프" : "테트리스"}
                        </span>
                      </div>
                      <button
                        type="button"
                        onClick={() => setLoadingGameOpen((value) => !value)}
                        className="shrink-0 rounded-md border border-slate-700/70 bg-slate-900/60 px-2.5 py-1 text-[11px] font-medium text-slate-300 transition hover:border-slate-600 hover:bg-slate-800 hover:text-slate-100"
                      >
                        {loadingGameOpen ? "접기" : "게임 열기"}
                      </button>
                    </div>

                    {loadingGameOpen && (
                      <div className="mt-3">
                        {loadingGameKind === "dog" ? (
                          <DogLoadingGame
                            active={loadingGameActive}
                            title="강아지 점프"
                            subtitle="Space로 점프하고 뼈다귀를 먹어 점수를 얻을 수 있습니다."
                          />
                        ) : (
                          <TetrisLoadingGame
                            active={loadingGameActive}
                            title="테트리스"
                            subtitle="방향키와 Space로 조작할 수 있습니다."
                          />
                        )}
                      </div>
                    )}
                  </div>
                </div>
              )}
              {domainMismatch && (
                <div className="rounded-xl border border-yellow-500/40 bg-yellow-500/10 p-4 space-y-3 shadow-lg shadow-yellow-500/5">
                  <p className="text-sm font-bold text-yellow-300">
                    {domainMismatch.message}
                  </p>
                  <p className="text-xs text-slate-400">
                    현재: <span className="text-white font-medium">{domainMismatch.original_domain_kr}</span>{" "}
                    → AI 추천: <span className="text-yellow-300 font-bold">{domainMismatch.predicted_domain_kr} ({(domainMismatch.probabilities[domainMismatch.predicted_domain] * 100).toFixed(1)}%)</span>
                  </p>
                  <div className="flex gap-2 pt-1">
                    <button type="button" onClick={handleDomainReselect} className="flex-1 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-sm font-medium transition-colors border border-slate-700">재선택</button>
                    <button type="button" onClick={handleDomainContinue} className="flex-1 py-2 rounded-lg bg-yellow-500 hover:bg-yellow-400 text-black font-bold text-sm transition-all active:scale-95">계속 진행</button>
                  </div>
                </div>
              )}
            </div>

            {/* VIOLATIONS TAB */}
            <div className={`h-full ${activeTab !== "violations" ? "hidden" : ""}`}>
              <ViolationPane
                violations={violations}
                resolved={resolved}
                onApprove={approve}
                onReject={reject}
                onZoom={(e: any) => {
                  const v = e?.violation || {};
                  const violationType = String(v.violation_type || v.type || v.issue_type || "");
                  const source = String(v._source || v.source || "");
                  sendMessage("ZOOM_TO_ENTITY", {
                    handle: e?.handle,
                    bbox: e?.bbox,
                    violation_type: violationType,
                    source,
                    prefer_bbox: source === "drawing_qa" || violationType.startsWith("drawing_quality_"),
                  });
                }}
                onApproveAll={() => approve("ALL")}
                onRejectAll={() => reject("ALL")}
                pendingProposals={pendingCreationProposals}
                onApproveProposal={approveProposal}
                onRejectProposal={rejectProposal}
              />
            </div>
          </div>
          {activeTab === "chat" && (
            <div className="shrink-0" style={{ borderTop: `1px solid ${C.border}` }}>
              <div className="flex items-center justify-between px-3 py-1.5 border-b border-slate-800 bg-slate-950/40 backdrop-blur-sm">
                <div className="flex items-center gap-2">
                  {agent !== "자유질의" && (
                    <button
                      onClick={() => {
                        if (reviewing || buttonReviewing) cancelReview();
                        else startReview();
                      }}
                      disabled={reviewButtonDisabled}
                      className="flex h-7 min-w-20 items-center justify-center gap-1.5 rounded-lg bg-[#0b4a94] px-3.5 text-center text-[13px] font-semibold text-white shadow-md transition-all hover:scale-[1.02] hover:bg-[#0d59b0] active:scale-95 disabled:pointer-events-none disabled:opacity-50"
                    >
                      {reviewing || buttonReviewing
                        ? `분석중${selectedObjectCount > 0 ? `(${selectedObjectCount}개)` : ""}${loadingDots}`
                        : `검토${selectedObjectCount > 0 ? `(${selectedObjectCount}개)` : ""}`}
                    </button>
                  )}

                  {selectedObjectCount > 0 && (
                    <div className="flex h-7 w-auto  items-center gap-1.5 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2.5 animate-in fade-in zoom-in-95 duration-200">
                      <span className="text-[11px] text-blue-300 font-semibold">
                        {selectedObjectCount}개 선택됨
                      </span>
                      <button
                        onClick={handleClearSelection}
                        className="text-blue-400 hover:text-white transition-colors ml-0.5"
                        title="선택 해제"
                      >
                        ✕
                      </button>
                    </div>
                  )}
                </div>

                <div className="relative">
                  <button
                    onClick={() => setShowDomainDropdown((v) => !v)}
                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-slate-300 hover:text-cyan-400 hover:bg-slate-800/50 rounded-lg transition-all"
                  >
                    {agent && agent !== "자유질의" ? `${agent}` : "도메인 선택"}
                    <span className="text-[10px] opacity-50">▼</span>
                  </button>
                  {showDomainDropdown && (
                    <>
                      <div className="fixed inset-0 z-40" onClick={() => setShowDomainDropdown(false)} />
                      <div className="absolute right-0 bottom-full mb-2 z-50 w-32 rounded-xl border border-slate-700 bg-[#07111f] shadow-xl backdrop-blur-md overflow-hidden flex flex-col">
                        {["전기", "배관", "건축", "소방"].map((d) => (
                          <button
                            key={d}
                            onClick={() => {
                              setShowDomainDropdown(false);
                              void openAgent(d, dwg);
                            }}
                            className={`px-4 py-3 text-sm font-semibold text-left transition-colors ${
                              agent === d
                                ? "bg-blue-500/20 text-blue-400"
                                : "text-slate-300 hover:bg-slate-800 hover:text-white"
                            }`}
                          >
                            {d}
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              </div>
              <InputBar
                input={input}
                setInput={setInput}
                onKey={onKey}
                onSend={() => send(input)}
                onStop={cancelChatGeneration}
                placeholder={isConnected ? "메시지를 입력하세요..." : "서버 연결 실패"}
                disabled={!isConnected}
                loading={chatGenerating}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
