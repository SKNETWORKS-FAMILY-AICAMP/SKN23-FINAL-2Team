export function autoFixTypeOfEntity(ent: any): string {
  return String(ent?.violation?.auto_fix?.type ?? "").trim().toUpperCase();
}

export function labelForAutoFixType(ent: any): string | null {
  const t = autoFixTypeOfEntity(ent);
  if (!t) return null;
  const m: Record<string, string> = {
    ATTRIBUTE: "속성",
    LAYER: "레이어",
    TEXT_CONTENT: "텍스트",
    TEXT_HEIGHT: "글자크기",
    COLOR: "색",
    LINETYPE: "선종류",
    LINEWEIGHT: "선두께",
    BLOCK_REPLACE: "블록 교체",
    ROTATE: "회전",
    DYNAMIC_BLOCK_PARAM: "동적블록",
    DELETE: "삭제 제안",
    MOVE: "이동",
    GEOMETRY: "형상 제안",
    SCALE: "배율",
  };
  return m[t] ?? t;
}