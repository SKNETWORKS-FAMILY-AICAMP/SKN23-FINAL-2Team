/*
 * File      : src/components/chat/ChatStatusStrip.tsx
 * Author    : 김지우, 김다빈
 * Create    : 2026-04-27 (분리)
 */

import React, { useEffect, useMemo, useState } from "react";
import { Bot, Database, FileSearch, Loader2, Network, Sparkles } from "lucide-react";
import { useAgentStore } from "../../store/agentStore";
import { C } from "../../constants/theme";

const CLIENT_STATUS_ROTATE_MS = 1600;
const CLIENT_LIVE_UI_MS = 100;

function useClientStatusLines(agentName: string) {
  const nObj = useAgentStore((s) => s.activeObjectIds?.length ?? 0);
  const mode = useAgentStore((s) => s.extractionMode);

  return useMemo(() => {
    const label =
      (agentName && agentName !== "자유질의" ? agentName : "도메인") + " Agent";

    const lines: string[] = [
      `${label}(으)로 질의를 보내는 중…`,
      "생각 중…",
    ];

    if (nObj > 0) {
      lines.push(
        `선택한 ${nObj}개 도면 객체·핸들을 반영해 맥락을 구성하는 중…`,
        `${label} 규칙·시방과 대조하는 중…`
      );
    } else {
      lines.push(
        mode === "focus"
          ? "부분 선택 추출 맥락을 읽는 중…"
          : "도면·세션·규정 맥락을 연결하는 중…"
      );
    }

    lines.push(
      "시방·벡터 DB에서 관련 구절을 찾는 중…",
      "LLM이 응답을 작성하는 중…"
    );

    return lines;
  }, [agentName, nObj, mode]);
}

function useThinkingLines(agentName: string) {
  const reviewing = useAgentStore((s) => s.isAnalyzing);
  const line = useAgentStore((s) => s.progressLine);
  const progressLogs = useAgentStore((s) => s.progressLogs);
  const clientLines = useClientStatusLines(agentName);

  const [clientWaitStart, setClientWaitStart] = useState<number | null>(null);
  const [, setUiTick] = useState(0);

  const hasServer =
    Boolean(line?.message) || progressLogs.length > 0;

  useEffect(() => {
    if (reviewing && !hasServer) {
      setClientWaitStart((prev) => prev ?? Date.now());
    } else {
      setClientWaitStart(null);
    }
  }, [reviewing, hasServer]);

  useEffect(() => {
    if (!reviewing) return;
    const id = window.setInterval(() => setUiTick((n) => n + 1), CLIENT_LIVE_UI_MS);
    return () => window.clearInterval(id);
  }, [reviewing]);

  if (!reviewing) {
    return null;
  }

  if (hasServer) {
    return {
      stage: line?.stage,
      lines: progressLogs.length > 0 ? progressLogs : [line?.message || "처리 중…"],
    };
  }

  const t0 = clientWaitStart ?? Date.now();
  const stepIdx =
    clientLines.length > 0
      ? Math.floor((Date.now() - t0) / CLIENT_STATUS_ROTATE_MS) % clientLines.length
      : 0;

  return {
    stage: "thinking",
    lines: clientLines.slice(0, stepIdx + 1),
  };
}

function iconForLine(text: string) {
  if (text.includes("WebSocket") || text.includes("백엔드")) {
    return <Network className="h-3.5 w-3.5" />;
  }

  if (text.includes("DB") || text.includes("구절") || text.includes("시방")) {
    return <Database className="h-3.5 w-3.5" />;
  }

  if (text.includes("도면") || text.includes("객체") || text.includes("핸들")) {
    return <FileSearch className="h-3.5 w-3.5" />;
  }

  if (text.includes("LLM") || text.includes("응답")) {
    return <Sparkles className="h-3.5 w-3.5" />;
  }

  return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
}

function ThinkingLine({ text, active }: { text: string; active: boolean }) {
  return (
    <div
      className="flex min-w-0 items-center gap-2 rounded px-1.5 py-1"
      style={{
        background: active ? C.hover : "transparent",
        color: active ? C.textPrimary : C.textSub,
      }}
    >
      <span
        className="shrink-0"
        style={{ color: active ? C.accent : C.textMuted }}
      >
        {iconForLine(text)}
      </span>
      <span className="min-w-0 flex-1 truncate" title={text}>
        {text}
      </span>
    </div>
  );
}

export default function ChatStatusStrip({ agentName }: { agentName: string }) {
  const thinking = useThinkingLines(agentName);
  const [expanded, setExpanded] = useState(true);
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!thinking) return;
    const t0 = Date.now();
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - t0) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [!!thinking]);

  if (!thinking) return null;

  return (
    <div className="flex w-full flex-col px-4 mt-4 mb-6">
      {/* Accordion Header */}
      <button 
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-zinc-400 hover:text-zinc-300 transition-colors w-fit group"
      >
        <span className="text-[10px] font-semibold">
          Thinking for {elapsed}s
        </span>
        <svg 
          className={`w-4 h-4 transition-transform duration-200 ${expanded ? "rotate-180" : ""}`} 
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Logs container */}
      {expanded && (
        <div className="mt-2 pl-3 border-l border-zinc-700/50 py-0.5">
          <div className="flex flex-col gap-1 text-[11px] leading-relaxed text-zinc-500">
            {thinking.lines.map((text, i) => (
              <div 
                key={`${text}-${i}`} 
                className="animate-in fade-in slide-in-from-top-1"
              >
                {text}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}