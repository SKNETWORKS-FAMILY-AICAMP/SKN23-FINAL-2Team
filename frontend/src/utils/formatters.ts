export const fmtDate = (iso: string) => {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

export function getMessageTimeMs(m: { id: string; ts?: number }): number | null {
  if (m.ts != null && m.ts > 0) return m.ts;
  const n = parseInt(m.id, 10);
  if (!Number.isNaN(n) && n >= 1_000_000_000_000) return n;
  return null;
}

export function formatMessageWallTime(ms: number) {
  return new Date(ms).toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  const h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, "0");
  const t = `${h < 12 ? "오전" : "오후"} ${h % 12 || 12}:${m}`;
  if (d.toDateString() === now.toDateString()) return t;
  if (d.toDateString() === yesterday.toDateString()) return `어제 ${t}`;
  return `${d.getMonth() + 1}월 ${d.getDate()}일 ${t}`;
}