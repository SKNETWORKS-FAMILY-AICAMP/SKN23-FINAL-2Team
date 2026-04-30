/**
 * 에이전트/시방 응답이 한 줄에 붙은 GFM 표·<br> 때문에 깨질 때 보정.
 * - <br> → 셀 내 공백(단일 행 GFM 표에 맞춤)
 * - …_20260223 같은 줄 직후 바로 |로 표가 오면 빈 줄 삽입(remark 구문 인식)
 * - 3열(|…|…|…|) 행이 공백으로만 이어질 때 행마다 개행
 */
export function normalizeChatMarkdown(raw: string): string {
  if (!raw) return raw;
  let t = raw.replace(/<br\s*\/?>/gi, " ");

  t = t.replace(/(\d{8})\n(\|)/g, "$1\n\n$2");

  for (let i = 0; i < 64; i++) {
    const n = t.replace(/(\|[^|\n]*\|[^|\n]*\|[^|\n]*\|)\s+(?=\|)/g, "$1\n");
    if (n === t) break;
    t = n;
  }

  return t;
}
