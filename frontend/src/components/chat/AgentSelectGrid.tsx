import React, { useState } from "react";
import { Building2, Check, Flame, Wrench, X, Zap } from "lucide-react";

const agents = [
  {
    id: "전기",
    icon: <Zap className="h-12 w-12" />,
    desc: "전기 배선, 조명, 전력 설비 등을 분석합니다.",
  },
  {
    id: "배관",
    icon: <Wrench className="h-12 w-12" />,
    desc: "급수, 배수, 위생, 소화 배관 등을 분석합니다.",
  },
  {
    id: "건축",
    icon: <Building2 className="h-12 w-12" />,
    desc: "건축 구조, 공간, 마감 등을 분석합니다.",
  },
  {
    id: "소방",
    icon: <Flame className="h-12 w-12" />,
    desc: "소화 설비, 감지기, 피난 동선 등을 분석합니다.",
  },
];

export default function AgentSelectGrid({
  selectedId,
  onPick,
  variant = "header",
}: {
  selectedId: string;
  onPick: (id: string) => void;
  variant?: "header" | "idle";
}) {
  const isIdle = variant === "idle";

  // 헤더 모드일 때는 콤팩트한 그리드 반환
  if (!isIdle) {
    return (
      <div className="grid w-full grid-cols-2 gap-2">
        {agents.map((agent) => {
          const active = selectedId === agent.id;
          return (
            <button
              key={agent.id}
              onClick={() => onPick(agent.id)}
              className={`flex flex-col items-center justify-center rounded-lg border p-3 text-center transition ${
                active
                  ? "border-blue-500 bg-blue-500/10"
                  : "border-slate-800 bg-slate-900/60 hover:border-slate-700"
              }`}
            >
              <div className={`mb-1 ${active ? "text-blue-400" : "text-slate-500"}`}>
                {React.cloneElement(agent.icon as React.ReactElement, { className: "h-6 w-6" })}
              </div>
              <span className={`text-[11px] font-bold ${active ? "text-white" : "text-slate-400"}`}>
                {agent.id}
              </span>
            </button>
          );
        })}
      </div>
    );
  }

  // Idle 모드일 때는 화면 전체를 차지하는 UI로 반환 (테두리 없음)
  return (
    <section className="flex h-full w-full flex-col bg-[#07111f] p-6 text-white overflow-y-auto no-scrollbar">
      <header className="mb-6 mt-8 flex flex-col items-center justify-center text-center">
        <h2 className="text-xl font-bold tracking-tight">도메인 에이전트 선택</h2>
        <p className="mt-2 text-xs text-slate-400">
          분석할 도메인 에이전트를 선택해주세요.
        </p>
      </header>

      <div className="mx-auto grid w-full max-w-2xl flex-1 grid-cols-2 gap-4 pb-8">
        {agents.map((agent) => {
          const active = selectedId === agent.id;

          return (
            <button
              key={agent.id}
              onClick={() => onPick(agent.id)}
              className={`relative flex flex-col items-center justify-center rounded-xl border p-5 text-center transition-all hover:-translate-y-1 hover:border-blue-500/60 hover:shadow-xl hover:shadow-blue-500/5 ${
                active
                  ? "border-blue-500 bg-blue-500/15"
                  : "border-slate-800 bg-slate-900/60 hover:bg-slate-800/80"
              }`}
            >
              {active && (
                <span className="absolute right-3 top-3 flex h-6 w-6 items-center justify-center rounded-full bg-blue-500 shadow-lg">
                  <Check className="h-4 w-4 text-white" />
                </span>
              )}

              <div className={`${active ? "text-blue-300" : "text-slate-400"} mb-1`}>
                {React.cloneElement(agent.icon as React.ReactElement, { className: "h-10 w-10" })}
              </div>

              <h3 className="mt-3 text-lg font-bold">{agent.id}</h3>
              <p className="mt-2 px-2 text-[11px] leading-relaxed text-slate-500 line-clamp-2">
                {agent.desc}
              </p>
            </button>
          );
        })}
      </div>
    </section>
  );
}