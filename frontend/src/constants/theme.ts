// 지금 안씀.

export const C = {
  bg: "#1e1e1e",
  panel: "#252526",
  card: "#2d2d2d",
  border: "#3c3c3c",
  borderSubtle: "#333333",
  textPrimary: "#cccccc",
  textSub: "#858585",
  textMuted: "#555555",
  accent: "#0078d4",
  accentHover: "#1084d8",
  accentDim: "rgba(0,120,212,0.15)",
  hover: "rgba(255,255,255,0.04)",
  hoverStrong: "rgba(255,255,255,0.07)",
} as const;

export const AGENT_CARDS = [
  { id: "전기" as const, hint: "배전·배선" },
  { id: "배관" as const, hint: "배관·설비" },
  { id: "건축" as const, hint: "구조·도면" },
  { id: "소방" as const, hint: "소화·피난" },
] as const;

export const AGENTS = ["전기", "배관", "건축", "소방"] as const;