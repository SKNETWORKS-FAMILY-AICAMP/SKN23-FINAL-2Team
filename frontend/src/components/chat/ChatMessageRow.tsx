import React, { useState, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { C } from "../../constants/theme";
import { getMessageTimeMs, formatMessageWallTime, formatTimestamp } from "../../utils/formatters";
import { normalizeChatMarkdown } from "../../utils/normalizeChatMarkdown";
import type { Message } from "../../store/agentStore";
import { Bot, Sparkles, Send, List, Plus, ChevronDown } from "lucide-react";
import chatLogo from "../../assets/ui/chat_logo.png";

export function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text).catch(() => { });
        setCopied(true);
        setTimeout(() => setCopied(false), 1800);
      }}
      className="p-1 rounded text-zinc-600 hover:text-zinc-400 hover:bg-zinc-800 transition-all"
      title={copied ? "복사됨" : "복사"}
    >
      {copied ? (
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" /></svg>
      ) : (
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
      )}
    </button>
  );
}

export function useTypewriter(fullText: string, enabled: boolean) {
  const charsPerTick = Math.max(2, Math.ceil(fullText.length / 150));
  const [displayed, setDisplayed] = useState(enabled ? "" : fullText);
  const [done, setDone] = useState(!enabled);

  useEffect(() => {
    if (!enabled) return;
    let pos = 0;
    const id = setInterval(() => {
      pos += charsPerTick;
      if (pos >= fullText.length) {
        setDisplayed(fullText);
        setDone(true);
        clearInterval(id);
      } else {
        setDisplayed(fullText.slice(0, pos));
      }
    }, 10);
    return () => clearInterval(id);
  }, [fullText, enabled, charsPerTick]);

  return { displayed, done };
}

function cleanMarkdown(text: string) {
  if (!text) return "";
  let t = text.trim();
  
  // 1. 답변 중간이나 끝에 나타나는 모든 '단순 감싸기용' 코드 블록 제거
  // (실제 언어가 명시되지 않은 ``` 는 무조건 제거)
  t = t.replace(/^```markdown\s*\n?/gi, "");
  t = t.replace(/\n?```$/g, "");
  t = t.replace(/```\n?---\n?\[출처\]/g, "\n\n[출처]"); // 박스 안의 구분선과 출처 결합 처리
  t = t.replace(/```/g, ""); // 남은 모든 백틱 제거 (일반 문서형 답변을 위해)

  // 2. [출처] 혹은 --- [출처] 패턴을 찾아서 표준화
  t = t.replace(/\n?---\n?\[출처\]/g, "\n\n[출처]");
  t = t.replace(/^---\s*$/gm, ""); // 의미 없는 가로줄 기호 박멸

  // 3. 기호(#)와 글자 사이에 공백이 없으면 삽입 (ex: ##제목 -> ## 제목)
  t = t.replace(/^(#{1,6})([^#\s])/gm, "$1 $2");

  // 4. 헤더(#)나 리스트(-) 앞에 줄바꿈 강제 삽입
  t = t.replace(/([^\n])\n(#{1,6}\s)/g, "$1\n\n$2");
  t = t.replace(/([^\n])\n([-*]\s)/g, "$1\n\n$2");
  
  return t;
}

export const chatMdComponents = {
  h1: ({ children }: any) => (
    <h1 className="text-[17px] font-bold text-white mt-6 mb-3 first:mt-0 pb-2 border-b-2 border-blue-500/30 tracking-tight">
      {children}
    </h1>
  ),
  h2: ({ children }: any) => (
    <h2 className="text-[15px] font-bold text-cyan-300 mt-5 mb-2 first:mt-0 pb-1 border-b border-slate-700/50">
      {children}
    </h2>
  ),
  h3: ({ children }: any) => (
    <h3 className="text-[14px] font-semibold text-blue-200 mt-4 mb-1.5 first:mt-0">
      {children}
    </h3>
  ),
  p: ({ children }: any) => {
    const content = String(children);
    if (content.includes("[출처]")) {
      return <SourceAccordion>{children}</SourceAccordion>;
    }
    return <p className="text-[12.8px] mb-3 last:mb-0 leading-[1.8] text-slate-200">{children}</p>;
  },
  ul: ({ children }: any) => <ul className="list-disc pl-5 my-2.5 space-y-1.5 marker:text-cyan-500/60">{children}</ul>,
  ol: ({ children }: any) => <ol className="list-decimal pl-5 my-2.5 space-y-1.5 marker:text-cyan-500/60">{children}</ol>,
  li: ({ children }: any) => <li className="text-[12.8px] leading-relaxed text-slate-200">{children}</li>,
  strong: ({ children }: any) => <strong className="font-bold text-cyan-100 bg-cyan-950/30 px-1 rounded border border-cyan-800/30">{children}</strong>,
  hr: () => <hr className="my-5 border-t-2 border-slate-700/30" />,
  code: ({ children, className }: any) => (
    <code className={`${className || ""} rounded bg-slate-900 px-1.5 py-0.5 text-[11.5px] font-mono text-cyan-300 border border-slate-700/50 shadow-sm`}>
      {children}
    </code>
  ),
  pre: ({ children }: any) => (
    <pre className="my-4 rounded-xl bg-slate-950 border border-slate-800 px-4 py-3.5 overflow-x-auto text-[11.5px] font-mono leading-relaxed shadow-lg scrollbar-thin scrollbar-thumb-slate-700">
      {children}
    </pre>
  ),
  table: ({ children }: any) => (
    <div className="my-4 w-full overflow-x-auto rounded-xl border border-slate-700 shadow-md">
      <table className="w-full border-collapse text-[12px] bg-slate-900/30">{children}</table>
    </div>
  ),
  th: ({ children }: any) => <th className="border-b border-slate-700 bg-slate-800/50 px-4 py-2.5 text-left font-bold text-slate-100">{children}</th>,
  td: ({ children }: any) => <td className="border-b border-slate-800 px-4 py-2 text-slate-300">{children}</td>,
};

function SourceAccordion({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div className="mt-8 pt-4 border-t border-slate-700/50">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center justify-between w-full group hover:bg-blue-500/5 p-2 rounded-lg transition-colors"
      >
        <div className="flex items-center gap-2">
          <Sparkles className={`w-3.5 h-3.5 ${open ? "text-blue-400" : "text-slate-500 group-hover:text-blue-400"} transition-colors`} />
          <span className={`text-[11px] font-bold uppercase tracking-widest ${open ? "text-blue-400" : "text-slate-500 group-hover:text-blue-400"} transition-colors`}>
            출처
          </span>
        </div>
        <div className={`flex items-center justify-center w-5 h-5 rounded-full border ${open ? "bg-blue-500 border-blue-400 text-white rotate-45" : "border-slate-700 text-slate-500 group-hover:border-blue-500 group-hover:text-blue-400"} transition-all duration-300`}>
          <Plus className="w-3 h-3" />
        </div>
      </button>
      {open && (
        <div className="mt-3 rounded-xl bg-blue-500/5 border border-blue-500/10 p-4 shadow-inner italic text-[12px] leading-relaxed text-slate-400 animate-in fade-in slide-in-from-top-2 duration-300">
          {children}
        </div>
      )}
    </div>
  );
}

function Avatar() {
  return (
    <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-blue-500/30 bg-white shadow-inner overflow-hidden">
      <img src={chatLogo} alt="AI Avatar" className="h-full w-full object-cover" />
    </div>
  );
}

function ThinkingProcess({ logs, time }: { logs?: string[]; time?: number }) {
  const [expanded, setExpanded] = useState(false);
  if (!logs || logs.length === 0) return null;

  return (
    <div className="mb-2 w-full max-w-[95%]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-zinc-500 hover:text-zinc-400 transition-colors group"
      >
        <span className="text-[11px] font-medium flex items-center gap-1">
          <Sparkles className="w-3 h-3 text-blue-400/70" />
          Thinking for {time ?? 0}s
        </span>
        <svg
          className={`w-3 h-3 transition-transform duration-200 ${expanded ? "rotate-180" : ""}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {expanded && (
        <div className="mt-1.5 ml-1.5 pl-2.5 border-l border-zinc-800 py-0.5 space-y-1">
          {logs.map((log, i) => (
            <div key={i} className="text-[10.5px] text-zinc-600 leading-tight">
              {log}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ChatMessageRow({ message: m }: { message: Message }) {
  const wall = getMessageTimeMs(m);
  const timeStr = wall != null ? formatMessageWallTime(wall) : null;
  const { displayed, done } = useTypewriter(m.text, m.streaming === true);

  if (m.sender === "user") {
    return (
      <div className="flex w-full flex-col items-end gap-1 px-1.5">
        <div className="max-w-[85%] rounded-xl rounded-tr-sm bg-[#e5e5e5] px-3.5 py-2 text-[13px] font-medium leading-relaxed text-black shadow-md">
          {m.text}
        </div>
        <div className="flex items-center gap-2 pr-1">
          <CopyBtn text={m.text} />
          {timeStr && (
            <span className="text-[10px] text-slate-500 tabular-nums">{timeStr}</span>
          )}
        </div>
      </div>
    );
  }

  if (m.sender === "agent") {
    return (
      <div className="flex w-full items-start gap-2.5 px-1.5">
        <Avatar />
        <div className="flex flex-col gap-1 items-start min-w-0 flex-1">
          <ThinkingProcess logs={m.thinkingLogs} time={m.thinkingTime} />
          <div className="max-w-[92%] sm:max-w-[85%] rounded-xl rounded-tl-sm border border-slate-700/50 bg-slate-800/80 px-3.5 py-2.5 text-[13px] leading-relaxed text-slate-100 shadow-lg backdrop-blur-sm break-words overflow-hidden">
            <div className="markdown-container prose prose-invert prose-sm max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={chatMdComponents as any}>
                {cleanMarkdown(displayed)}
              </ReactMarkdown>
              {!done && (
                <span className="inline-block w-0.5 h-3.5 ml-0.5 bg-sky-400 align-middle animate-[blink_0.8s_step-end_infinite]" />
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 pl-1">
            {timeStr && (
              <span className="text-[10px] text-slate-600 tabular-nums">{timeStr}</span>
            )}
            <CopyBtn text={m.text} />
          </div>
        </div>
      </div>
    );
  }
  return null;
}
