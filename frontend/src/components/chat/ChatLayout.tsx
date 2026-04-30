import React from "react";
import { ArrowRight, List, Plus, Square } from "lucide-react";

import mainLogo from "../../assets/ui/main_logo.png";

/* ================= HEADER ================= */

export function Header({
  agent,
  onShowList,
  onNewChat,
  isConnected,
}: any) {
  const domainMap: Record<string, { label: string }> = {
    전기: { label: "전기" },
    배관: { label: "배관" },
    건축: { label: "건축" },
    소방: { label: "소방" },
  };
  const domain = typeof agent === "string" ? domainMap[agent] : undefined;
  const statusClassName = isConnected
    ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
    : "border-red-500/25 bg-red-500/10 text-red-300";
  const statusDotClassName = isConnected ? "bg-emerald-300 animate-pulse" : "bg-red-500";

  return (
    <header className="relative z-50 flex h-14 items-center justify-between border-b border-slate-800 bg-[#07111f] px-2 backdrop-blur-md">
      <div className="flex items-center gap-3">
        <img
          src={mainLogo}
          alt="Cadence AI Logo"
          className="h-16 object-contain select-none"
          draggable={false}
          style={{
            filter: "invert(1) hue-rotate(180deg) brightness(1.2)",
            mixBlendMode: "screen",
            userSelect: "none",
            pointerEvents: "none",
          }}
        />

      </div>

      <div className="flex items-center gap-1.5">
        {domain && (
          <span
            className={`flex shrink-0 items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-semibold leading-none ${statusClassName}`}
            title={`현재 도메인: ${domain.label} · ${isConnected ? "연결됨" : "연결 끊김"}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${statusDotClassName}`} />
            {domain.label}
          </span>
        )}

        {onShowList && (
          <button
            onClick={onShowList}
            className="flex h-7 items-center gap-1 rounded-lg border border-slate-700 bg-slate-800/60 px-2.5 text-[11px] text-slate-300 transition hover:bg-slate-700 hover:text-white"
          >
            <List className="h-3.5 w-3.5" />
            목록
          </button>
        )}

        {onNewChat && (
          <button
            onClick={onNewChat}
            className="flex h-7 items-center gap-1 rounded-lg bg-[#0b4a94] px-2.5 text-[11px] font-semibold text-white shadow-md transition hover:scale-105 hover:bg-[#0d59b0]"
          >
            <Plus className="h-3.5 w-3.5" />
            새 채팅
          </button>
        )}
      </div>
    </header>
  );
}

/* ================= TAB ================= */

export function TabBar({
  activeTab,
  onTabChange,
  reviewPendingCount,
  rightContent,
  dwg,
}: any) {
  return (
    <div className="flex items-center justify-between border-b border-slate-800 bg-[#020817] px-2">
      <div className="flex gap-1">
        {(["chat", "violations"] as const).map((tab) => {
          const active = activeTab === tab;

          return (
            <button
              key={tab}
              onClick={() => onTabChange(tab)}
              className={`relative px-3 py-1.5 text-[13px] font-medium transition ${
                active ? "text-white" : "text-slate-500 hover:text-white"
              }`}
            >
              {tab === "chat" ? "채팅" : "수정 및 검토"}

              {tab === "violations" && reviewPendingCount > 0 && (
                <span className="ml-2 rounded-full bg-amber-500 px-2 py-[2px] text-[10px] text-white">
                  {reviewPendingCount}
                </span>
              )}

              {active && (
                <div className="absolute bottom-0 left-0 h-[2px] w-full bg-gradient-to-r from-blue-500 to-cyan-400" />
              )}
            </button>
          );
        })}
      </div>

      <div className="flex min-w-0 items-center gap-2 pr-1">
        {dwg && (
          <span className="max-w-[170px] truncate text-[10px] font-mono text-slate-500">{dwg}</span>
        )}
        <div className="flex items-center gap-2">{rightContent}</div>
      </div>
    </div>
  );
}

/* ================= INPUT ================= */

export function InputBar({
  input,
  setInput,
  onKey,
  onSend,
  onStop,
  placeholder,
  disabled = false,
  loading = false,
}: any) {
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  React.useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [input]);

  return (
    <div className="bg-[#020617] p-3">
      <div
        className={`relative flex items-end gap-2.5 rounded-[22px] px-4 py-2.5 transition-all duration-300 ${
          disabled
            ? "border border-slate-800/50 bg-slate-900/20 opacity-50"
            : "border border-slate-700/50 bg-[#0f172a]"
        }`}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          placeholder={placeholder}
          disabled={disabled}
          rows={1}
          className="max-h-48 flex-1 resize-none border-none bg-transparent py-1 text-sm leading-relaxed text-slate-100 outline-none placeholder:text-slate-500 focus:ring-0 focus-visible:!outline-none no-scrollbar"
          style={{ boxShadow: "none" }}
        />

        <div className="mb-0.5 flex shrink-0 items-center">
          {loading ? (
            <button
              type="button"
              onClick={onStop}
              className="flex h-8 w-8 items-center justify-center rounded-full border border-red-500/30 bg-red-500/10 text-red-500 transition-all hover:bg-red-500/20 active:scale-90"
            >
              <Square className="h-4 w-4 fill-red-500" />
            </button>
          ) : (
            <button
              onClick={() => {
                if (!disabled && input.trim()) onSend();
              }}
              disabled={disabled || !input.trim()}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-[#0b4a94] text-white shadow-md transition-all hover:scale-105 hover:bg-[#0d59b0] active:scale-95 disabled:scale-100 disabled:opacity-20 disabled:grayscale"
            >
              <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
