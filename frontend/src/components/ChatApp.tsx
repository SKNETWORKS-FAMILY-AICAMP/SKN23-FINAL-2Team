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
import UnmappedLayersModal from "./cad/UnmappedLayersModal";

type View = "idle" | "sessions" | "chat";
type TabId = "chat" | "violations";

const fmtDate = (iso: string) => {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
};

export default function ChatApp() {
  const { sendMessage, isConnected } = useSocket();
  const {
    messages: chat,
    reviewResults: violations,
    isAnalyzing: reviewing,
    setAnalyzing,
    addMessage,
    setMessages,
    clearResults,
    clearCadUnmapped,
    activeObjectIds,
    selectedSpecIds,
    selectedTempSpecIds,
    setSelectedTempSpecIds,
    cadEvent,
    domainMismatch,
    clearDomainMismatch,
    progressLine,
    setActiveObjectIds,
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
  const [unmappedModalDismissed, setUnmappedModalDismissed] = useState(false);
  const [buttonReviewing, setButtonReviewing] = useState(false);
  const [chatGenerating, setChatGenerating] = useState(false);
  const [loadingDots, setLoadingDots] = useState("");

  const chatScrollRef = useRef<HTMLDivElement>(null);
  const chatRequestIdRef = useRef(0);
  const pendingReviewRef = useRef<{
    domain: string;
    mode: "KEC_ONLY" | "HYBRID";
    prompt: string;
  } | null>(null);

  const pendingFixCount = (violations as any[]).filter(
    (v) =>
      v?.violation?.id != null &&
      v.violation.id !== "" &&
      !resolved.has(String(v.violation.id)),
  ).length;
  const selectedObjectCount = activeObjectIds?.length || 0;
  const domainMismatchConfidence =
    domainMismatch
      ? domainMismatch.probabilities?.[domainMismatch.predicted_domain]
      : undefined;
  const domainMismatchPercent =
    typeof domainMismatchConfidence === "number" &&
    Number.isFinite(domainMismatchConfidence)
      ? ` (${(domainMismatchConfidence * 100).toFixed(1)}%)`
      : "";

  const isApiRegistered = () => localStorage.getItem("skn23_api_key_registered") === "true";

  useEffect(() => {
    if (view !== "chat" || activeTab !== "chat") return;
    const el = chatScrollRef.current;
    if (!el) return;
    const t = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(t);
  }, [view, activeTab, chat, reviewing, progressLine]);

  useEffect(() => {
    setUnmappedModalDismissed(false);
  }, [cadEvent?.sessionId, cadEvent?.ts, (cadEvent?.unmappedLayers ?? []).join("\u0000")]);

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
    if (view === "sessions") {
      setActiveObjectIds([], "full");
    }
  }, [view, setActiveObjectIds]);

  const showUnmappedModal = (cadEvent?.unmappedLayers?.length ?? 0) > 0 && !unmappedModalDismissed;

  useEffect(() => {
    if (!reviewing) {
      setButtonReviewing(false);
      setChatGenerating(false);
    }
  }, [reviewing]);

  const clearReviewUiFully = useCallback(() => {
    clearResults();
    setResolved(new Set());
    setActiveTab("chat");
    if (window.chrome?.webview) {
      try {
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_CAD_REVIEW" }));
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_SELECTION_CACHE" }));
      } catch { }
    }
  }, [clearResults]);

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
      if (!localStorage.getItem("skn23_api_key_registered")) {
        setShowApiAlert(true);
        return;
      }
      setAgent(id);
      setDwg(file);
      clearReviewUiFully();
      
      // 즉시 채팅창으로 이동하여 체감 속도 향상
      setView("chat");
      setInput("");
      setMessages([]);

      // 백그라운드에서 세션 로드 및 생성
      try {
        const list = await fetchSessionList(id).catch(() => []);
        setSessions(list);
        const ns = await createSession(id, file).catch(() => null);
        if (ns) {
          setSessionId(ns.id);
          setSessions((p) => [ns, ...p]);
        } else {
          setSessionId("");
        }
      } catch (e) {
        console.error("세션 초기화 실패:", e);
      }
    },
    [clearReviewUiFully, setMessages],
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
          const nextDwg = String(msg.payload?.dwg || "").trim();
          if (nextDwg) setDwg(nextDwg);
        }
        if (msg.action === "MACHINE_INFO" && msg.payload) {
          localStorage.setItem("skn23_machine_id", msg.payload.machine_id || "");
          localStorage.setItem("skn23_hostname", msg.payload.hostname || "");
          localStorage.setItem("skn23_os_user", msg.payload.os_user || "");
          sendSessionInfoToCSharp();
        }
        if (msg.action === "TEMP_SPEC_LINK_LOADED" && Array.isArray(msg.payload?.documents)) {
          const org = localStorage.getItem("skn23_org_id") || "";
          const payloadOrg = msg.payload.org_id as string | undefined;
          if (payloadOrg && org && payloadOrg !== org) return;
          const ids = (msg.payload.documents as { temp_document_id?: string }[])
            .map((d) => d.temp_document_id)
            .filter((id): id is string => !!id);
          setSelectedTempSpecIds(ids);
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
      } catch { }
    };

    if (window.chrome?.webview) {
      window.chrome.webview.addEventListener("message", handleCSharpMessage);
      window.chrome.webview.postMessage(JSON.stringify({ action: "REACT_READY" }));
      sendSessionInfoToCSharp();
    }
    return () => window.chrome?.webview?.removeEventListener("message", handleCSharpMessage);
  }, [openAgent, setSelectedTempSpecIds]);

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
          addMessage({
            id: Date.now().toString(),
            sender: "agent",
            text: "새 대화가 시작되었습니다.",
            timestamp: new Date().toISOString(),
          });
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

  const pushAgent = (txt: string) => {
    addMessage({ id: Date.now().toString(), sender: "agent", text: txt, timestamp: new Date().toISOString() });
  };

  const cancelReview = useCallback(() => {
    pendingReviewRef.current = null;
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
    const sid = sessionId || cadEvent.sessionId;
    const cadCacheId = cadEvent.sessionId;
    if (!sessionId) setSessionId(sid);
    agentApi
      .startReview({
        sessionId: sid,
        cadCacheId,
        domain,
        activeObjectIds: activeObjectIds,
        reviewMode: mode,
        userPrompt: prompt,
        specDocumentIds: selectedSpecIds,
        tempSpecIds: selectedTempSpecIds,
        orgId: localStorage.getItem("skn23_org_id") || "",
      })
      .catch(() => {
        pushAgent("분석 요청 중 오류가 발생했습니다.");
        setAnalyzing(false);
      });
  }, [
    cadEvent,
    sessionId,
    activeObjectIds,
    selectedSpecIds,
    selectedTempSpecIds,
    setAnalyzing,
  ]);

  const runDrawReviewExtract = () => {
    const domainMap: Record<string, string> = {
      전기: "elec",
      배관: "pipe",
      건축: "arch",
      소방: "fire",
    };
    const domainCode = domainMap[agent] || "elec";
    const hasSpecs = selectedSpecIds.length > 0 || selectedTempSpecIds.length > 0;
    const mode = hasSpecs ? "HYBRID" : "KEC_ONLY";
    const nSel = activeObjectIds?.length || 0;
    const defaultReview =
      nSel > 0
        ? `CAD에서 선택한 ${nSel}개 객체에 대해 시방·규정 위반이 있는지 검토해 주세요.`
        : "도면 전체에 대해 시방·규정 위반을 전수 검토해 주세요.";

    pendingReviewRef.current = {
      domain: domainCode,
      mode,
      prompt: defaultReview,
    };
    setButtonReviewing(true);
    setAnalyzing(true);
    clearResults();
    setResolved(new Set());
    setActiveTab("chat");
    if (window.chrome?.webview) {
      try {
        window.chrome.webview.postMessage(JSON.stringify({ action: "CLEAR_CAD_REVIEW" }));
      } catch { }
    }
    sendMessage("EXTRACT_DATA", { domain: domainCode });
  };

  const handleDomainContinue = () => {
    if (!domainMismatch) return;
    clearDomainMismatch();
    setButtonReviewing(true);
    setAnalyzing(true);
    pushAgent(`${domainMismatch.original_domain_kr} 에이전트로 계속 진행합니다...`);

    const sid = sessionId || (cadEvent?.sessionId ?? "");
    const cadCacheId = cadEvent?.sessionId || sid;
    agentApi
      .confirmReview({
        sessionId: sid,
        cadCacheId,
        domain: domainMismatch.original_domain,
        activeObjectIds,
        reviewMode: (selectedSpecIds.length > 0 || selectedTempSpecIds.length > 0) ? "HYBRID" : "KEC_ONLY",
        specDocumentIds: selectedSpecIds,
        tempSpecIds: selectedTempSpecIds,
        orgId: localStorage.getItem("skn23_org_id") || "",
      })
      .catch(() => {
        pushAgent("분석 요청 중 오류가 발생했습니다.");
        setAnalyzing(false);
      });
  };

  const handleDomainReselect = () => {
    clearDomainMismatch();
    setShowDomainDropdown(true);
  };

  const newSession = async () => {
    if (!isApiRegistered()) { setShowApiAlert(true); return; }
    if (!agent || agent === "자유질의") {
      // 에이전트가 없으면 홈으로 이동 유도
      setView("idle");
      return;
    }
    try {
      const ns = await createSession(agent, dwg);
      setSessionId(ns.id);
      setSessions((p) => [ns, ...p]);
    } catch (e) {
      console.error("New session failed:", e);
    }
    
    setInput("");
    clearReviewUiFully();
    setMessages([]);
    setView("chat");
  };

  const newSessionAndStartReview = async () => {
    if (!isApiRegistered()) { setShowApiAlert(true); return; }
    if (reviewing) return;
    if (!isConnected) return;
    if (!agent || agent === "자유질의") return;
    const ns = await createSession(agent, dwg);
    setSessionId(ns.id);
    setSessions((p) => [ns, ...p]);
    clearReviewUiFully();
    setMessages([]);
    setView("chat");
    runDrawReviewExtract();
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
    runDrawReviewExtract();
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
      console.error("수정 제안 무시 중 오류 발생:", error);
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

      <UnmappedLayersModal
        open={showUnmappedModal}
        onClose={() => setUnmappedModalDismissed(true)}
        layerNames={cadEvent?.unmappedLayers || []}
        onSaved={() => {
          clearCadUnmapped();
          addMessage({
            id: Date.now().toString(),
            sender: "agent",
            text: "선택하신 레이어·블록명 매핑을 저장했습니다. 분석을 이어서 진행합니다.",
          });
          // 매핑 저장 후 자동으로 분석 재시작
          runDrawReviewExtract();
        }}
      />

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
            <div className={`flex flex-col gap-4 px-3.5 py-5 max-w-3xl w-full mx-auto ${activeTab !== "chat" ? "hidden" : ""}`}>
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
                <ChatMessageRow key={`${m.id}-${i}`} message={m} />
              ))}
              {reviewing && (
                <div className="px-2" aria-live="polite">
                  <ChatStatusStrip agentName={agent} />
                </div>
              )}
              {domainMismatch && (
                <div className="rounded-xl border border-yellow-500/40 bg-yellow-500/10 p-4 space-y-3 shadow-lg shadow-yellow-500/5">
                  <p className="text-sm font-bold text-yellow-300">
                    {domainMismatch.message}
                  </p>
                  <p className="text-xs text-slate-400">
                    현재: <span className="text-white font-medium">{domainMismatch.original_domain_kr}</span>{" "}
                    → AI 추천: <span className="text-yellow-300 font-bold">{domainMismatch.predicted_domain_kr}{domainMismatchPercent}</span>
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
                onZoom={(e: any) =>
                  sendMessage("ZOOM_TO_ENTITY", {
                    session_id: sessionId,
                    handle: e.handle,
                    layer: e.layer,
                    type: e.type,
                    bbox: e.bbox,
                  })
                }
                onApproveAll={() => approve("ALL")}
                onRejectAll={() => reject("ALL")}
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
                      disabled={!isConnected}
                      className="flex h-7 w-20 items-center justify-center gap-1.5 rounded-lg bg-[#0b4a94] px-3.5 text-center text-[13px] font-semibold text-white shadow-md transition-all hover:scale-[1.02] hover:bg-[#0d59b0] active:scale-95 disabled:pointer-events-none disabled:opacity-50"
                    >
                      {reviewing || buttonReviewing
                        ? `분석중${selectedObjectCount > 0 ? `(${selectedObjectCount}개)` : ""}${loadingDots}`
                        : `검토${selectedObjectCount > 0 ? `(${selectedObjectCount}개)` : ""}`}
                    </button>
                  )}

                  {selectedObjectCount > 0 && (
                    <div className="flex h-7 w-auto items-center gap-1.5 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2.5 animate-in fade-in zoom-in-95 duration-200">
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
                placeholder={isConnected ? "메시지를 입력하세요..." : "서버 연결 중..."}
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
