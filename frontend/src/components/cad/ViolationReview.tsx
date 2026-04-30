import React, { useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronRight,
  Eye,
  MousePointerClick,
  X,
} from "lucide-react";
import { C } from "../../constants/theme";
import { labelForAutoFixType } from "../../utils/cadUtils";

export function ViolationDetailModal({
  ent,
  onClose,
  onZoom,
  onApprove,
  onReject,
  resolved,
}: any) {
  const v = ent?.violation;
  const vid = v?.id != null && v.id !== "" ? String(v.id) : null;
  const isRes = vid ? resolved.has(vid) : true;
  const fixLabel = labelForAutoFixType(ent);

  const severityColor: Record<string, string> = {
    Critical: "#ef4444",
    Major: "#f97316",
    Minor: "#eab308",
  };

  const color = severityColor[v?.severity] || "#94a3b8";

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center sm:items-center"
      style={{ background: "rgba(0,0,0,0.68)" }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm overflow-hidden rounded-t-2xl shadow-2xl sm:rounded-2xl"
        style={{
          background: "#07111f",
          border: `1px solid ${C.border}`,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <header
          className="flex items-center justify-between px-4 py-3"
          style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
        >
          <div className="flex min-w-0 items-center gap-2">
            {v?.severity && (
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-bold"
                style={{
                  background: `${color}22`,
                  color,
                  border: `1px solid ${color}55`,
                }}
              >
                {v.severity}
              </span>
            )}
            <span
              className="truncate text-[13px] font-semibold"
              style={{ color: C.textPrimary }}
            >
              {v?.rule || v?.description || "위반 항목"}
            </span>
          </div>

          <button onClick={onClose} style={{ color: C.textMuted }}>
            <X className="h-4 w-4" />
          </button>
        </header>

        <main className="max-h-[60vh] space-y-4 overflow-y-auto px-4 py-4">
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px]">
            {ent?.handle && (
              <span style={{ color: C.textSub }}>
                Handle: <code className="text-slate-300">{ent.handle}</code>
              </span>
            )}
            {ent?.layer && (
              <span style={{ color: C.textSub }}>
                Layer: <code className="text-slate-300">{ent.layer}</code>
              </span>
            )}
            {ent?.type && (
              <span style={{ color: C.textSub }}>
                Type: <code className="text-slate-300">{ent.type}</code>
              </span>
            )}
          </div>

          {v?.description && (
            <section>
              <p className="mb-1 text-[10px]" style={{ color: C.textMuted }}>
                위반 내용
              </p>
              <p
                className="whitespace-pre-wrap text-[12px] leading-relaxed"
                style={{ color: C.textPrimary }}
              >
                {v.description}
              </p>
            </section>
          )}

          {v?.suggestion && (
            <section
              className="rounded-lg px-3 py-2"
              style={{
                background: "rgba(14,165,233,0.08)",
                border: `1px solid ${C.accent}44`,
              }}
            >
              <p className="mb-1 text-[10px] font-medium" style={{ color: C.accent }}>
                권고 조치
              </p>
              <p className="whitespace-pre-wrap text-[12px] leading-relaxed text-blue-200">
                {v.suggestion}
              </p>
            </section>
          )}

          {v?.auto_fix && (
            <section
              className="rounded-lg px-3 py-2"
              style={{
                background: "rgba(255,255,255,0.03)",
                border: `1px solid ${C.borderSubtle}`,
              }}
            >
              <p className="mb-1 text-[10px]" style={{ color: C.textMuted }}>
                자동 수정
              </p>

              <div className="flex items-center gap-2">
                {fixLabel && (
                  <span
                    className="rounded border px-1.5 py-0.5 text-[11px]"
                    style={{
                      borderColor: C.border,
                      color: C.textSub,
                    }}
                  >
                    {fixLabel}
                  </span>
                )}

                <span className="text-[11px]" style={{ color: C.textSub }}>
                  {v.auto_fix.type === "MOVE" && "이동 미리보기"}
                  {v.auto_fix.type === "ROTATE" && "회전 미리보기"}
                  {v.auto_fix.type === "DELETE" && "삭제 제안"}
                  {!["MOVE", "ROTATE", "DELETE"].includes(v.auto_fix.type || "") &&
                    "승인 시 CAD에 즉시 반영"}
                </span>
              </div>
            </section>
          )}
        </main>

        <footer
          className="flex gap-2 px-4 py-3"
          style={{ borderTop: `1px solid ${C.borderSubtle}` }}
        >
          <button
            onClick={() => {
              onZoom(ent);
              onClose();
            }}
            className="flex-1 rounded-lg py-2 text-[12px] font-medium"
            style={{
              background: C.hover,
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          >
            줌인
          </button>

          {!isRes && vid ? (
            <>
              <button
                onClick={() => {
                  onReject(vid);
                  onClose();
                }}
                className="rounded-lg px-3 py-2 text-[12px]"
                style={{ color: C.textSub }}
              >
                무시
              </button>

              <button
                onClick={() => {
                  onApprove(vid);
                  onClose();
                }}
                className="flex-1 rounded-lg py-2 text-[12px] font-medium text-white"
                style={{ background: C.accent }}
              >
                승인
              </button>
            </>
          ) : (
            <span className="flex-1 py-2 text-center text-[11px]" style={{ color: C.textMuted }}>
              처리 완료
            </span>
          )}
        </footer>
      </div>
    </div>
  );
}

export function ViolationPane({
  violations,
  resolved,
  onApprove,
  onReject,
  onZoom,
  onApproveAll,
  onRejectAll,
}: any) {
  const [detailEnt, setDetailEnt] = useState<any>(null);
  const [reviewListMode, setReviewListMode] = useState<"pending" | "completed">("pending");

  const { list, groups } = useMemo(() => {
    const rows = (violations || []).map((ent: any, idx: number) => {
      const rawId = ent.violation?.id;
      const vid = rawId != null && rawId !== "" ? String(rawId) : null;
      const isRes = vid ? resolved.has(vid) : true;
      return { ent, idx, vid, isRes };
    });

    const keyOf = (v: any) =>
      `${v?.rule || ""}|||${v?.description || ""}|||${v?.suggestion || ""}`;

    const map = new Map<string, typeof rows>();

    for (const row of rows) {
      const key = keyOf(row.ent.violation);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(row);
    }

    return {
      list: rows,
      groups: Array.from(map.entries()),
    };
  }, [violations, resolved]);

  const pending = list.filter((x: any) => x.vid && !x.isRes);
  const completed = list.filter((x: any) => x.isRes);
  const pendingGroups = groups.filter(([, items]) =>
    items.some((x: any) => x.vid && !x.isRes)
  );
  const completedGroups = groups.filter(([, items]) =>
    items.every((x: any) => x.isRes)
  );
  const visibleGroups = reviewListMode === "completed" ? completedGroups : pendingGroups;

  if (!violations || violations.length === 0) {
    return (
      <div className="p-4 text-center text-[12px]" style={{ color: C.textMuted }}>
        검토·수정 대기 항목이 없습니다.
      </div>
    );
  }

  const summary = {
    critical: list.filter((x: any) => x.ent.violation?.severity === "Critical").length,
    major: list.filter((x: any) => x.ent.violation?.severity === "Major").length,
    minor: list.filter((x: any) => x.ent.violation?.severity === "Minor").length,
    qa: list.filter((x: any) => {
      const source = x.ent.violation?._source || x.ent.violation?.source;
      return source === "drawing_qa";
    }).length,
  };
  const resolvedCount = completed.length;

  return (
    <>
      {detailEnt && (
        <ViolationDetailModal
          ent={detailEnt}
          onClose={() => setDetailEnt(null)}
          onZoom={onZoom}
          onApprove={onApprove}
          onReject={onReject}
          resolved={resolved}
        />
      )}

      <div
        className="space-y-4 p-4"
        style={{
          background: "#07111f",
          border: `1px solid ${C.border}`,
        }}
      >
        <section
          className="rounded-xl p-3"
          style={{
            background: "rgba(15,23,42,0.72)",
            border: `1px solid ${C.borderSubtle}`,
          }}
        >
          <h3 className="mb-3 text-[13px] font-semibold" style={{ color: C.textPrimary }}>
            실시간 검토 결과
          </h3>

          <div className="space-y-2">
            <SummaryRow
              icon={<AlertTriangle className="h-4 w-4" />}
              title="중대 위반"
              desc="즉시 확인이 필요한 항목입니다."
              value={`${summary.critical}건`}
              color="#ef4444"
              active={reviewListMode === "pending"}
              onClick={() => setReviewListMode("pending")}
            />
            <SummaryRow
              icon={<AlertTriangle className="h-4 w-4" />}
              title="주요 위반"
              desc="검토 후 수정 여부를 판단할 항목입니다."
              value={`${summary.major}건`}
              color="#f97316"
              active={reviewListMode === "pending"}
              onClick={() => setReviewListMode("pending")}
            />
            <SummaryRow
              icon={<Check className="h-4 w-4" />}
              title={summary.qa > 0 ? "경미/QA 이슈" : "경미 위반"}
              desc={summary.qa > 0 ? `도면 품질검사 ${summary.qa}건 포함` : "확인 후 처리할 경미 항목입니다."}
              value={`${summary.minor}건`}
              color="#eab308"
              active={reviewListMode === "pending"}
              onClick={() => setReviewListMode("pending")}
            />
            <SummaryRow
              icon={<Check className="h-4 w-4" />}
              title="처리 완료"
              desc="승인 또는 무시 처리된 항목입니다."
              value={`${resolvedCount}건`}
              color="#22d3ee"
              active={reviewListMode === "completed"}
              onClick={() => setReviewListMode("completed")}
            />
          </div>
        </section>

        <section
          className="rounded-xl p-3"
          style={{
            background: "rgba(15,23,42,0.72)",
            border: `1px solid ${C.borderSubtle}`,
          }}
        >
          <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="text-[13px] font-semibold" style={{ color: C.textPrimary }}>
              {reviewListMode === "completed" ? "처리 완료 항목" : "수정 제안"}
            </h3>
            {reviewListMode === "pending" && pending.length > 0 && (
              <div className="flex shrink-0 items-center gap-1.5">
                <button
                  type="button"
                  onClick={onRejectAll}
                  className="rounded px-2 py-1 text-[10px] transition-colors hover:bg-red-900/30"
                  style={{ color: "#fca5a5", border: "1px solid rgba(248,113,113,0.3)" }}
                >
                  전체 무시
                </button>
                <button
                  type="button"
                  onClick={onApproveAll}
                  className="rounded px-2.5 py-1 text-[10px] font-bold text-white transition-transform active:scale-95"
                  style={{ background: C.accent }}
                >
                  모두 승인
                </button>
              </div>
            )}
          </div>

          {visibleGroups.length === 0 ? (
            <div
              className="rounded-lg px-3 py-6 text-center text-[12px]"
              style={{
                color: C.textMuted,
                background: "rgba(2,6,23,0.28)",
                border: `1px solid ${C.borderSubtle}`,
              }}
            >
              {reviewListMode === "completed"
                ? "아직 처리 완료된 수정 제안이 없습니다."
                : "처리할 수정 제안이 없습니다."}
            </div>
          ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {visibleGroups.map(([key, items], index) => {
              const first = items[0];
              const v = first.ent.violation;
              const isResolved = items.every((x: any) => x.isRes);
              const vid = first.vid;
              const fixLabel = labelForAutoFixType(first.ent);

              return (
                <button
                  key={`${key}-${index}`}
                  onClick={() => setDetailEnt(first.ent)}
                  className="rounded-xl p-3 text-left transition hover:ring-1 hover:ring-blue-500/50"
                  style={{
                    background: "rgba(2,6,23,0.42)",
                    border: `1px solid ${C.borderSubtle}`,
                    opacity: 1,
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="text-[12px] font-semibold" style={{ color: C.textPrimary }}>
                        {v?.rule || v?.description || `수정 제안 ${index + 1}`}
                      </p>
                      {v?.suggestion && (
                        <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-slate-400">
                          {v.suggestion}
                        </p>
                      )}
                    </div>

                    <Eye className="h-4 w-4 shrink-0 text-blue-400" />
                  </div>

                  {reviewListMode === "completed" ? (
                    <div className="mt-3 flex justify-end">
                      <span
                        className="rounded px-2 py-1 text-[10px] font-semibold"
                        style={{
                          color: "#67e8f9",
                          background: "rgba(34,211,238,0.10)",
                          border: "1px solid rgba(34,211,238,0.25)",
                        }}
                      >
                        처리 완료
                      </span>
                    </div>
                  ) : !isResolved && vid && (
                    <div className="mt-3 flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onReject(vid);
                        }}
                        className="rounded px-2 py-1 text-[11px]"
                        style={{ color: C.textMuted }}
                      >
                        무시
                      </button>
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onApprove(vid);
                        }}
                        className="rounded px-3 py-1 text-[11px] font-medium text-white"
                        style={{ background: C.accent }}
                      >
                        적용
                      </button>
                    </div>
                  )}
                </button>
              );
            })}
          </div>
          )}

        </section>
      </div>
    </>
  );
}

function SummaryRow({
  icon,
  title,
  desc,
  value,
  color,
  active = false,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  value: string;
  color: string;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition hover:bg-white/[0.04]"
      style={{
        background: active ? `${color}12` : "rgba(30,41,59,0.58)",
        border: `1px solid ${active ? `${color}66` : C.borderSubtle}`,
      }}
    >
      <span style={{ color }}>{icon}</span>

      <div className="min-w-0 flex-1">
        <p className="text-[12px] font-semibold" style={{ color: C.textPrimary }}>
          {title}
        </p>
        <p className="text-[10px]" style={{ color: C.textMuted }}>
          {desc}
        </p>
      </div>

      <span className="text-[13px] font-bold" style={{ color }}>
        {value}
      </span>

      <ChevronRight className="h-4 w-4 text-slate-500" />
    </button>
  );
}
