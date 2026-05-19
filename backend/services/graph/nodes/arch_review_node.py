"""
File    : backend/services/graph/nodes/arch_review_node.py
Author  : 김다빈
Create  : 2026-04-25
Description : 건축 도메인 LangGraph 노드.
              AgentState를 입력받아 의도 분류 → 매핑 → tool 선택 → 서브 에이전트 실행 후
              review_result / current_step / assistant_response 를 채워 반환합니다.
              배관(pipe_review_node.py) 패턴과 동일하게 AgentState TypedDict를 완전히 반환합니다.
              건축은 arch 레이어가 1차 검토 대상이므로 레이어 분리(arch_pipe_layer_split)를 사용하지 않습니다.

Modification History :
    - 2026-04-25 (김다빈) : 신규 생성 — 공통 템플릿(review_agent_template.py)에서 분리.
                            의도 분류, intent_hint 빠른 경로, 이름/위치 병렬 매핑,
                            AgentState 완전 반환, RevCloud handle 정합 포함.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import SessionLocal
from backend.services import llm_service
from backend.services.agents.arch.arch_agent import ArchAgent
from backend.services.agents.arch.schemas import ARCH_SUB_AGENT_TOOLS
from backend.services.agents.arch.sub.mapping import MappingAgent
from backend.services.agents.arch.workflow_handler import ArchWorkflowHandler
from backend.services.agents.common.object_mapping_utils import run_object_mapping
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
from backend.services.graph.state import (
    AgentState,
    CurrentStep,
    LawReference,
    PendingFix,
    ReviewResult,
    ViolationItem,
)

logger = logging.getLogger(__name__)

_HAS_TABLE_REF_RE = re.compile(
    r"(?:표|별표|부표|다음\s*표|이\s*표|[Tt]able)\s*[\d\-\.가-힣]*"
    r"\s*(?:에\s*따른다|참조|참고|에\s*의함|에\s*의거|와\s*같다|을\s*적용|기준|에\s*준한다|에\s*나타낸|에\s*나타낸\s*바)",
    re.IGNORECASE,
)

_ARCH_TABLE_VALUE_HINTS = (
    "건축법", "시행령", "KCS", "KS", "방화구획", "방화문", "직통계단",
    "피난계단", "복도", "피난거리", "채광", "환기", "층고", "경사로",
    "mm", "m", "㎡", "제곱미터", "%",
)

# ── 정규식 상수 ───────────────────────────────────────────────────────────────

_DOMAIN_PREFIX_RE = re.compile(
    r"^(?:elec|pipe|arch|electric|piping|architecture|fire|firefighting|소방|mech|mechanical)_",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(r"_\d{8}$")
_BAD_TABLE_HEADER_RE = re.compile(r"^열\d+$")
_EMPTY_TABLE_VALUES = {"", "-", "–", "—", "1", "0", "None", "none", "NULL", "null", "?", "□", "■", ""}
_BROKEN_GLYPH_RE = re.compile(r"[■-□▣-▩▯▱▲▼-]")
_NOISE_TOKEN_RE = re.compile(r"^(?:[○●◎◯]\s*)+$|(?:[○●◎◯]\s*\d+\)?)|(?:dun\s*){2,}", re.IGNORECASE)
_CASUAL_RE = re.compile(
    r"^[\s!?.]*"
    r"(안녕|하이|hi|hello|반가워|반갑|고마워|감사|ㅎㅇ|ㅋ+|ㅠ+|ㅎ+|헬로|잘있어|bye|바이|"
    r"괜찮아|잘부탁|잘 부탁|맞아|그래|응|네|아니|알겠|좋아|오케이|ok|okay|"
    r"수고|수고해|잠깐|잠시만|이봐|여보세요|뭐야|뭐임|뭐에요|어|오|아)"
    r"[\s!?.]*$",
    re.IGNORECASE,
)

# ── 텍스트 클리너 ─────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"<br\s*/?>|</?p>|</?div>|</?li>|</?tr>|</?td>|</?th>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── doc_name 정제 ─────────────────────────────────────────────────────────────

def _pretty_doc_name(doc_name: str, category: str = "") -> str:
    """arch_KCS_41_XXXX 건축시방서_20240822 → 시방서 KCS 41 XXXX 건축시방서"""
    name = (doc_name or "").strip()
    if not name or name == "—":
        return name
    name = _DOMAIN_PREFIX_RE.sub("", name)
    name = _DATE_SUFFIX_RE.sub("", name)
    name = name.replace("_", " ")
    cat = (category or "").strip()
    if cat and not name.startswith(cat):
        name = f"{cat} {name}"
    return name.strip()


# ── RAG 출처 메타 ─────────────────────────────────────────────────────────────

def _chunk_to_meta_row(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source") or "permanent")
    raw_doc_name = str(row.get("doc_name") or "").strip() or "—"
    category = str(row.get("category") or "")
    return {
        "source": source,
        "source_label": "영구_시방_DB" if source == "permanent" else "임시_시방_DB",
        "doc_name": raw_doc_name,
        "display_name": _pretty_doc_name(raw_doc_name, category),
        "document_id": str(row.get("document_id") or ""),
        "chunk_index": row.get("chunk_index"),
        "domain": str(row.get("domain") or ""),
        "category": category,
    }


def _citation_line_from_rows(rows: list[dict[str, Any]], *, max_show: int = 3) -> str:
    if not rows:
        return ""
    show = rows[:max_show]
    parts: list[str] = []
    for meta in show:
        name = (meta.get("display_name") or meta.get("doc_name") or "—")[:60]
        parts.append(f"«{name}»#{meta.get('chunk_index', '-')}")
    more = f" +{len(rows) - len(show)}" if len(rows) > len(show) else ""
    return " · ".join(parts) + more


def _retrieval_block_compact(meta_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n_chunks": len(meta_rows),
        "summary": _citation_line_from_rows(meta_rows, max_show=3) + (f" (총{len(meta_rows)}건)" if meta_rows else ""),
        "refs": [
            {
                "doc": (m.get("display_name") or m.get("doc_name") or "")[:80],
                "i": m.get("chunk_index"),
                "db": m.get("source_label", ""),
            }
            for m in meta_rows[:4]
        ],
    }


def _format_rag_footer(rows: list[dict[str, Any]], *, n_chunks: int) -> str:
    if n_chunks <= 0:
        return "\n\n---\n[출처] RAG: 일치 시방 청크 없음."
    line = _citation_line_from_rows(rows, max_show=3).strip()
    if not line:
        return f"\n\n---\n[출처] 시방RAG (총{n_chunks}건, 문서 메타 생략)."
    return f"\n\n---\n[출처] 시방RAG {line} (총{n_chunks}건)."


def _format_direct_footer() -> str:
    return "\n\n---\n[출처] 벡터DB 미검색·LLM 직답."


def _format_review_rag_footer(rag_refs: list[Any]) -> str:
    rows = [_chunk_to_meta_row(x) for x in (rag_refs or []) if isinstance(x, dict)]
    if not rows:
        return "\n\n---\n[출처] 시방RAG: 참고 청크 없음"
    return f"\n\n---\n[출처] 검토 참고 {_citation_line_from_rows(rows, max_show=3)} (총{len(rows)}건)"


# ── 텍스트 → Markdown 변환 ────────────────────────────────────────────────────

def _lines_to_bullet_block(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    out: list[str] = []
    for line in re.split(r"\r?\n", text):
        item = line.strip()
        if not item:
            out.append("")
            continue
        if item.startswith(("-", "*", "•")):
            out.append("- " + item[1:].strip())
            continue
        m = re.match(r"^(\(?\d{1,2}\)?|[가-힣])[\).:]\s+(.+)$", item)
        if m:
            out.append(f"- **{m.group(1)}.** {m.group(2).strip()}")
            continue
        out.append(item)
    return "\n".join(out)


def _spec_text_to_readable_markdown(text: str) -> str:
    if not text or not text.strip():
        return text
    text = text.strip()
    parts = re.split(r"(?:\r?\n)\s*---\s*(?:\r?\n)?|\n\s*---\s*\n|\s+---\s+|\n---\n", text)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return text
    if len(parts) == 1:
        return _lines_to_bullet_block(parts[0])
    return "\n\n".join(f"### {i} · 요약\n\n{_lines_to_bullet_block(p)}" for i, p in enumerate(parts, 1))


# ── 표 처리 ───────────────────────────────────────────────────────────────────

def _split_markdown_table_row(line: str) -> list[str]:
    if not line:
        return []
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(line: str) -> bool:
    if not line or "|" not in line:
        return False
    cells = _split_markdown_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.strip()) for c in cells if c.strip())


def _clean_table_cell(value: str) -> str:
    value = _strip_html(str(value or "")).strip()
    value = _BROKEN_GLYPH_RE.sub("", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def _is_empty_or_noise_cell(value: str) -> bool:
    value = _clean_table_cell(value)
    if value in _EMPTY_TABLE_VALUES:
        return True
    if _BAD_TABLE_HEADER_RE.match(value):
        return True
    if _NOISE_TOKEN_RE.search(value):
        return True
    return bool(re.fullmatch(r"[○●◎◯①-⑳\s\)\(]+", value))


def _is_bad_markdown_table(markdown: str) -> bool:
    if not markdown or "|" not in markdown:
        return False
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    headers = _split_markdown_table_row(lines[0])
    bad_headers = sum(1 for h in headers if _BAD_TABLE_HEADER_RE.match(_clean_table_cell(h)) or _is_empty_or_noise_cell(h))
    meaningful_headers = sum(1 for h in headers if not _is_empty_or_noise_cell(h))
    data_cells: list[str] = []
    for line in lines[1:]:
        if not _is_markdown_separator(line):
            data_cells.extend(_split_markdown_table_row(line))
    empty_cells = sum(1 for c in data_cells if _is_empty_or_noise_cell(c))
    broken_ratio = len(_BROKEN_GLYPH_RE.findall(markdown)) / max(len(markdown), 1)
    return (
        bad_headers / max(len(headers), 1) >= 0.4
        or empty_cells / max(len(data_cells), 1) >= 0.55
        or broken_ratio >= 0.02
        or meaningful_headers <= 1
    )


def _clean_bad_markdown_table(markdown: str) -> str:
    markdown = _strip_html(markdown or "")
    if not markdown:
        return ""
    if not _is_bad_markdown_table(markdown):
        return markdown
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 3:
        return markdown
    headers = [_clean_table_cell(h) for h in _split_markdown_table_row(lines[0])]
    meaningful_cols = [i for i, h in enumerate(headers) if h and not _is_empty_or_noise_cell(h)]
    out: list[str] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if _is_markdown_separator(line):
            continue
        cells = [_clean_table_cell(c) for c in _split_markdown_table_row(line)]
        parts: list[str] = []
        for i in meaningful_cols:
            if i < len(cells) and not _is_empty_or_noise_cell(cells[i]):
                parts.append(f"{headers[i]}: {cells[i]}" if headers[i] else cells[i])
        if not parts:
            parts = [c for c in cells if c and not _is_empty_or_noise_cell(c)]
        if parts:
            line_text = " / ".join(parts).strip()
            if line_text and line_text not in seen:
                seen.add(line_text)
                out.append(f"- {line_text}")
    return "\n".join(out)


def _restore_collapsed_table_text(text: str) -> str:
    text = _strip_html(text or "")
    if "|" in text or len(text) < 120:
        return text
    if not any(k in text for k in ("재료명", "규격", "적용", "주의", "구분", "기준", "호칭", "관경", "폭", "높이", "면적")):
        return text
    parts = re.split(r"\s{2,}|(?<=다\.)\s+|(?<=\))\s+", text)
    rows = []
    for part in parts:
        cleaned = _clean_table_cell(part)
        if cleaned and not _is_empty_or_noise_cell(cleaned):
            rows.append(cleaned)
    return "\n".join(f"- {row}" for row in rows[:30]) if len(rows) >= 3 else text


def _get_rag_content_from_row(row: dict[str, Any]) -> str:
    if not isinstance(row, dict):
        return ""
    context_content = str(row.get("context_content") or "").strip()
    if context_content:
        return _clean_bad_markdown_table(context_content) if "|" in context_content and _is_bad_markdown_table(context_content) else _restore_collapsed_table_text(context_content)
    chunk_type = str(row.get("chunk_type") or "").strip()
    table_markdown = str(row.get("table_markdown") or "").strip()
    if chunk_type == "table" and table_markdown:
        return _clean_bad_markdown_table(table_markdown) if _is_bad_markdown_table(table_markdown) else _strip_html(table_markdown)
    for key in ("content", "raw_content"):
        content = str(row.get(key) or "").strip()
        if not content:
            continue
        if "|" in content and _is_bad_markdown_table(content):
            return _clean_bad_markdown_table(content)
        return _restore_collapsed_table_text(content)
    return ""


def _contains_table_value_hint(row: dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("content", "raw_content", "table_markdown"))
    return any(hint in text for hint in _ARCH_TABLE_VALUE_HINTS)


def _table_priority(row: dict[str, Any]) -> tuple[int, int, int, int]:
    if not isinstance(row, dict):
        return (9, 9, 9, 999999)
    chunk_type = str(row.get("chunk_type") or "")
    has_table = bool(str(row.get("table_markdown") or "").strip())
    try:
        idx = int(row.get("chunk_index") or 999999)
    except (TypeError, ValueError):
        idx = 999999
    return (
        0 if chunk_type == "table" or has_table else 1,
        0 if row.get("_neighbor_expanded") else 1,
        0 if _contains_table_value_hint(row) else 1,
        idx,
    )


def _rag_chunks_to_readable_markdown(chunks: list[dict[str, Any]], *, max_chunks: int = 8) -> str:
    out: list[str] = []
    for idx, row in enumerate(sorted([r for r in chunks if isinstance(r, dict)], key=_table_priority), start=1):
        if len(out) >= max_chunks:
            break
        raw_table = str(row.get("table_markdown") or "").strip()
        content = _get_rag_content_from_row(row)
        if not content:
            continue
        name = _pretty_doc_name(row.get("doc_name") or f"시방 발췌 {idx}", row.get("category") or "")
        if len(name) > 100:
            name = name[:97] + "..."
        chunk_type = str(row.get("chunk_type") or "").strip()
        tags = (" · table" if chunk_type == "table" else "") + (" · nearby" if row.get("_neighbor_expanded") else "")
        if chunk_type == "table" and raw_table and not _is_bad_markdown_table(raw_table):
            inner = content
        else:
            inner = _spec_text_to_readable_markdown(content) if "---" in content else _lines_to_bullet_block(content)
        out.append(f"#### {name}{tags}\n\n{inner}")
    return "\n\n".join(out) if out else "관련 시방 조항을 찾지 못했습니다."


# ── 답변 스타일 ───────────────────────────────────────────────────────────────

def _detect_answer_style(user_request: str) -> str:
    q = (user_request or "").strip()
    q_lower = q.lower()
    if any(k in q for k in ("자동 판정 조건", "자동판정", "CAD 판정", "자동 검토", "rule JSON", "JSON으로 변환")):
        return "auto_review_report"
    if any(k in q for k in ("도면", "검토", "위반", "판정", "표로", "표 형식", "테이블", "방화구획", "계단", "피난")) or "cad" in q_lower:
        return "review_table"
    if any(k in q for k in ("정리", "요약", "한눈에", "정돈", "레포트", "보고서", "리포트")):
        return "structured_summary"
    if any(k in q for k in ("이격", "거리", "폭", "높이", "면적", "수치", "얼마", "기준값", "규격", "층고", "반자")):
        return "focused_detail"
    if any(k in q for k in ("설명", "알려", "뭐야", "무엇", "대해")):
        return "simple_explain"
    return "structured_summary"


def _style_instruction(answer_style: str) -> str:
    if answer_style == "focused_detail":
        return (
            "[답변 형식 - focused_detail]\n"
            "표를 새로 만들지 말고 사용자가 물은 특정 항목만 간결하게 정리하세요.\n"
            "### 핵심 기준\n- 기준명/조항:\n- 적용 대상:\n\n"
            "### 세부 내용\n- 검색 결과의 실제 수치·단위·조건을 직접 적으세요.\n\n"
            "### 적용 및 주의사항\n- 예외와 추가 확인 사항만 검색 결과 기반으로 적으세요.\n"
        )
    if answer_style == "review_table":
        return (
            "[답변 형식 - cad_review]\n"
            "### 설계·시공 기준\n"
            "| 항목 | 적용 대상 | 조건 | 요구사항 | 정량 기준 | 예외/주의 |\n"
            "|---|---|---|---|---|---|\n"
            "| 검색 결과 기반 | ... | ... | 실제 값 명시 | 수치/정량값 또는 없음 | ... |\n\n"
            "### CAD 검토 포인트\n- 레이어, 블록 속성, 텍스트 주석, 색상 등 판정 가능한 요소로 변환하세요.\n\n"
            "### 자동 판정 가능 여부\n- 가능 / 부분 가능 / 불가로 구분하세요.\n\n"
            "### 한계\n- 도면만으로 판단 불가한 정보 부족 사항을 적으세요.\n"
        )
    if answer_style == "auto_review_report":
        return (
            "[답변 형식 - auto_validation]\n"
            "JSON, dict, 배열은 출력하지 마세요.\n"
            "### 자동 검토 가능 항목\n- CAD로 확인 가능한 항목을 나열하세요.\n\n"
            "### CAD 판정 방식\n- 레이어, 블록명, 속성값, 텍스트 주석 등 판정 방식을 적으세요.\n\n"
            "### 자동 판정 가능 여부\n| 항목 | 가능 여부 |\n|---|---|\n| 항목명 | 가능 / 부분 가능 / 불가 |\n\n"
            "### 필요 CAD 데이터\n- 필요한 CAD 속성을 적으세요.\n\n"
            "### 한계\n- 검색 결과에 없는 조건은 만들지 마세요.\n"
        )
    if answer_style == "simple_explain":
        return (
            "[답변 형식 - simple_explain]\n"
            "### 개념 및 목적\n- 사용자가 물은 기준/개념을 정의하세요.\n\n"
            "### 주요 기준 및 적용\n- 검색 결과에 있는 기준만 정리하고 실제 수치가 있으면 직접 포함하세요.\n\n"
            "### CAD 검토 포인트\n- CAD에서 확인 가능한 속성을 적으세요.\n\n"
            "### 적용 시 주의사항\n- 예외와 한계를 적으세요.\n"
        )
    return (
        "[답변 형식 - structured_summary]\n"
        "### 기준 요약\n- 기준명:\n- 적용범위:\n- 핵심 원칙:\n\n"
        "### 주요 기준\n- 검색 결과의 핵심 기준과 실제 수치·단위를 항목별로 정리하세요.\n\n"
        "### CAD 검토 가능성\n- 자동 검토 가능:\n- 부분 가능:\n- 도면만으로 판단 불가:\n\n"
        "### 적용 및 주의사항\n- 예외/주의사항/추가 확인 필요 사항을 적으세요.\n"
    )


def _build_rag_answer_prompt(*, user_request: str, context_text: str, answer_style: str) -> str:
    return (
        "당신은 건축법·시행령·건축 시방서를 근거 기반으로 정리하는 엔지니어링 AI입니다.\n"
        "아래 [검색 결과]에 실제로 포함된 내용만 근거로 답하세요.\n\n"
        "[절대 규칙]\n"
        "1. section=, chunk=, type=table 같은 내부 메타를 출력하지 마세요.\n"
        "2. 검색 결과에 없는 기준명, 수치, KS/KCS/건축법 조항 번호를 만들지 마세요.\n"
        "3. mm, m, %, ㎡, 제곱미터 같은 수치와 단위는 임의 변경하지 마세요.\n"
        "4. 표 내용과 일반 설명이 충돌하면 표의 값을 우선하세요.\n"
        "5. 정상 markdown table은 유지할 수 있지만 핵심 행 값은 본문 bullet에도 직접 반영하세요.\n"
        "6. 열1/열2/열3, □, ■, PUA 문자가 많은 깨진 표는 렌더링하지 말고 의미 있는 셀만 bullet로 복구하세요.\n"
        "7. '표에 따른다'만 반복하지 말고 실제 표 값이 있으면 답변에 포함하세요. 표 값이 없으면 없다고 밝히세요.\n"
        "8. 문서 표지, 목차, 연락처, 위원 명단은 제외하세요.\n\n"
        f"{_style_instruction(answer_style)}\n"
        f"[검색 결과]\n{context_text}\n\n"
        f"사용자 질문: {user_request}"
    )


# ── 후속 질문 버튼 ────────────────────────────────────────────────────────────

def _clean_followup_base(user_request: str) -> str:
    base = (user_request or "건축 기준").strip()
    remove_patterns = [
        r"\s*리포트\s*형식으로\s*정리\s*$",
        r"\s*보고서\s*형식으로\s*정리\s*$",
        r"\s*CAD\s*검토\s*기준\s*(중심으로)?\s*(설명|정리)?\s*$",
        r"\s*자동\s*검토\s*가능\s*조건\s*(으로)?\s*정리\s*$",
        r"\s*자동\s*판정\s*조건\s*(으로)?\s*변환\s*$",
        r"\s*정리\s*$",
        r"\s*요약\s*$",
        r"\s*설명\s*$",
    ]
    for pattern in remove_patterns:
        base = re.sub(pattern, "", base, flags=re.IGNORECASE).strip()
    base = re.sub(r"\s*(에\s*대해|에\s*대한|관련|기준을|기준|에\s*관해)\s*$", "", base).strip()
    return base or "건축 기준"


def _is_broad_rag_query(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in (
        "정리", "설명", "요약", "리포트", "보고서", "기준", "무엇", "대해",
        "검토 기준", "자동 판정", "자동 검토", "cad 판정", "rule", "규정", "시방",
        "explain", "summary", "report", "review", "validation", "criteria",
    ))


def _add_anchor_if_matched(topic: str, triggers: tuple[str, ...], anchors: list[str], bucket: list[str]) -> None:
    t = topic.lower()
    if any(k.lower() in t for k in triggers):
        bucket.extend(anchors)


def _build_search_anchor_from_topic(topic: str) -> str:
    topic = topic or ""
    anchors: list[str] = []
    _add_anchor_if_matched(topic, ("방화구획", "방화", "fire zone", "내화", "방화문"), ["방화구획", "면적 기준", "내화구조", "건축법 시행령"], anchors)
    _add_anchor_if_matched(topic, ("계단", "피난계단", "특별피난", "직통계단", "stair"), ["직통계단", "피난계단", "특별피난계단", "폭 단높이", "건축법"], anchors)
    _add_anchor_if_matched(topic, ("복도", "통로", "corridor", "피난경로", "유효폭"), ["복도", "통로 유효폭", "피난경로", "건축물 피난 방화구조 규칙"], anchors)
    _add_anchor_if_matched(topic, ("피난거리", "보행거리", "출구", "exit", "egress"), ["피난거리", "보행거리", "출구 간격", "수평거리"], anchors)
    _add_anchor_if_matched(topic, ("채광", "환기", "창", "window", "개구부", "거실"), ["거실", "채광창", "환기창", "바닥면적 비율", "건축법"], anchors)
    _add_anchor_if_matched(topic, ("층고", "반자", "ceiling", "높이"), ["층고", "반자높이", "최솟값", "거실 복도 계단실"], anchors)
    _add_anchor_if_matched(topic, ("장애", "편의", "경사로", "wheelchair", "접근"), ["장애인", "편의시설", "경사로", "접근로", "설치 기준"], anchors)
    anchors.extend(["건축법 시행령", "건축물의 피난 방화구조 등의 기준에 관한 규칙", "KCS", "KS"])
    seen: set[str] = set()
    uniq: list[str] = []
    for anchor in anchors:
        if anchor and anchor not in seen:
            seen.add(anchor)
            uniq.append(anchor)
    return " ".join(uniq)


def _expand_retrieval_query(display_query: str) -> str:
    query = (display_query or "").strip()
    anchor = _build_search_anchor_from_topic(query)
    if anchor and anchor not in query:
        return f"{query} {anchor}".strip()
    return query


def _make_followup_query(topic: str, suffix: str) -> str:
    return f"{(topic or '건축 기준').strip()} {suffix}".strip()


def _build_suggested_queries(user_request: str, answer_style: str) -> list[dict[str, str]]:
    topic = _clean_followup_base(user_request)
    suggestions: list[dict[str, str]] = []

    def item(label: str, suffix: str) -> dict[str, str]:
        display_query = _make_followup_query(topic, suffix)
        return {
            "label": label,
            "query": display_query,
            "display_query": display_query,
            "retrieval_query": _expand_retrieval_query(display_query),
        }

    if answer_style != "structured_summary":
        suggestions.append(item("리포트 형식으로 보기", "리포트 형식으로 정리"))
    if answer_style != "review_table":
        suggestions.append(item("도면 검토 기준으로 보기", "CAD 검토 기준 중심으로 정리"))
    if answer_style != "auto_review_report":
        suggestions.append(item("자동 판정 조건으로 변환", "자동 검토 가능 조건으로 정리"))
    return suggestions[:3]


# ── LangGraph 노드 진입점 ─────────────────────────────────────────────────────

async def arch_review_node(state: AgentState) -> AgentState:
    """
    건축 도메인 LangGraph 노드.

    [의무 반환 키]
      - review_result    : ReviewResult TypedDict
      - current_step     : "review_completed" | "query_completed" |
                           "pending_fix_review" | "action_ready" | "error"
      - assistant_response: 사용자에게 보여줄 최종 텍스트

    [자동 처리]
      - 메모리(summary_text, recent_chat) 저장은 후속 memory_summary_node 담당
    """
    async with SessionLocal() as db:
        try:
            _sid = state.get("session_id") or (state.get("session_meta") or {}).get("session_id")
            logging.info("[ArchGraph] domain_node(arch_review) ENTER session=%s", _sid)
            out = await _run_arch(state, db)
            logging.info(
                "[ArchGraph] domain_node(arch_review) EXIT session=%s step=%s",
                _sid,
                out.get("current_step"),
            )
            return out
        except Exception as exc:
            logging.error("[ArchNode] 처리 중 오류: %s", exc, exc_info=True)
            error_msg = f"건축 에이전트 처리 중 오류가 발생했습니다: {exc}"
            error_result: ReviewResult = {
                "is_violation":    False,
                "violations":      [],
                "suggestions":     [],
                "referenced_laws": [],
                "final_message":   error_msg,
            }
            return {
                **state,
                "review_result":      error_result,
                "current_step":       "error",
                "assistant_response": error_msg,
            }


# ── 내부 로직 ────────────────────────────────────────────────────────────────

async def _classify_intent(user_message: str) -> str:
    """
    사용자 메시지를 LLM으로 분류하여 3대 처리 갈래 중 하나를 반환합니다.

    Returns
    -------
    str
        "answer" : 일반 질문, 건축법 조회, 인사 등 — call_query_agent 또는 직답
        "review" : 도면 전체 위반 검토 — call_review_agent
        "action" : 특정 객체 수정/이동/삭제 — call_action_agent

    Notes
    -----
    - LLM 오류 또는 파싱 실패 시 "answer"로 fallback (최소한의 응답 보장).
    - intent_hint="review"이면 이 함수를 호출하지 않고 즉시 "review"로 고정.
      (agent/start 경로에서 LLM 분류 비용을 아끼기 위한 빠른 경로)
    - "answer"로 분류되어도 has_drawing=True + 건축 키워드 포함 시
      _run_arch()에서 "review"로 보정. (LLM 오분류 방어)
    """
    system_prompt = """당신은 건축 설계 AI 라우터입니다. 사용자의 요청을 다음 3가지 중 하나로 분류하세요:
- answer: 일반적인 질문, 인사, 건축법 조회, 법규 검색 등 (도면 객체 수정/검토가 아닌 경우)
- review: 도면 전체에 대한 건축법 위반 검토, 방화구획·복도폭·계단·피난거리 전수 조사
- action: 특정 객체에 대한 수정, 변경, 이동, 삭제 등 직접적인 액션 지시

응답은 반드시 JSON 형식으로만 하세요: {"intent": "answer" | "review" | "action"}"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    try:
        res = await llm_service.generate_answer(messages=messages, response_format={"type": "json_object"})
        if isinstance(res, dict):
            return res.get("intent", "answer")
        return "answer"
    except Exception:
        return "answer"


async def _run_arch(state: AgentState, db: AsyncSession) -> AgentState:
    """
    건축 도메인 에이전트 핵심 실행 로직 (arch_review_node의 내부 진입점).

    처리 단계:
        1. 의도 분석  : intent_hint=review이면 즉시 고정, 아니면 _classify_intent() LLM 호출.
                        has_drawing+건축 키워드 조합으로 LLM 오분류 보정.
        2. 컨텍스트   : drawing_data를 레이어 분리 없이 그대로 context["raw_layout_data"]에 전달.
                        (건축은 arch 레이어가 1차 검토 대상 — pipe처럼 arch/non-arch 분리 불필요)
        3. 매핑       : answer 의도가 아닐 때만 이름 매핑(MappingAgent) + 위치 매핑(run_object_mapping)
                        을 asyncio.gather로 병렬 실행.
        4. 빠른 경로  : intent==answer이고 _is_casual_message() 판정이면 RAG 없이 LLM 직답.
        5. LLM tool   : ARCH_SUB_AGENT_TOOLS 스키마로 tool_choice="auto" 호출.
                        tool 미선택 시 _make_fallback_call()로 intent 기반 기본 tool 선택.
        6. Tool 실행  : ArchWorkflowHandler.handle_tool_calls() — query/review/action 세 갈래.
        7. 결과 변환  : _format_state()로 AgentState 필수 키(review_result 등) 채워 반환.

    Parameters
    ----------
    state : AgentState
        LangGraph 공유 상태. user_request, drawing_data, intent_hint, spec_guid 등 포함.
    db : AsyncSession
        비동기 DB 세션. QueryAgent·WorkflowHandler에 전달.

    Returns
    -------
    AgentState
        review_result, current_step, assistant_response, pending_fixes 등이 채워진 상태.
    """
    import time as _time
    t0 = _time.time()
    def _lap(label: str, since: float) -> float:
        now = _time.time()
        print(f"[ArchNode TRACK]  {label:<30} {now - since:5.1f}s  (누적 {now - t0:5.1f}s)")
        return now

    message     = state.get("user_request") or ""
    memory_text = build_memory_prompt_from_state(state)

    drawing_data = state.get("drawing_data") or {}
    has_drawing  = bool(drawing_data.get("entities") or drawing_data.get("elements"))
    logging.info(
        "[ArchGraph] domain_node DWG entities=%s",
        len(drawing_data.get("entities") or drawing_data.get("elements") or []),
    )

    current_drawing_id = state.get("current_drawing_id") or ""
    org_id    = state.get("org_id")
    rm        = state.get("runtime_meta") or {}
    se        = state.get("session_extra") or {}
    spec_guid = state.get("spec_guid") or rm.get("spec_guid") or se.get("spec_guid")
    active_ids       = set(state.get("active_object_ids") or [])
    pending_fixes_in = state.get("pending_fixes") or []

    # ── 1. 의도 분석 ──────────────────────────────────────────────────────────
    hint = (str(state.get("intent_hint") or "")).strip()
    if hint == "review":
        intent = "review"
        print("[ArchNode ROUTE] intent_hint=review → 도면검토(/agent/start) 경로 고정")
    else:
        intent = await _classify_intent(message)
        # LLM이 answer로 오분류할 때 건축 키워드로 보정
        if has_drawing and intent == "answer" and message and (
            "전수 검토" in message
            or "전수검토" in message
            or "위반" in message
            or "방화구획" in message
            or ("계단" in message and "검토" in message)
            or ("복도" in message and "검토" in message)
            or ("피난" in message and "검토" in message)
            or ("도면" in message and "검토" in message)
        ):
            intent = "review"
            print(f"[ArchNode ROUTE] 키워드 보정 → review (LLM was answer)")
    print(f"[ArchNode ROUTE] 의도 분류 결과: {intent}")
    lap_t = _lap("1. 의도 분석", t0)

    # ── 2. 컨텍스트 준비 ──────────────────────────────────────────────────────
    # 건축은 arch 레이어가 1차 검토 대상 — 레이어 분리 없이 전체 도면 전달
    context: dict[str, Any] = {
        "org_id":             org_id,
        "spec_guid":          spec_guid,
        "active_object_ids":  active_ids,
        "retrieved_laws":     list(state.get("retrieved_laws") or []),
        "current_drawing_id": current_drawing_id,
        "drawing_loaded":     has_drawing,
        "pending_fixes":      pending_fixes_in,
        "intent":             intent,
        "user_request":       message,
        "drawing_data":       drawing_data,
        "raw_layout_data":    drawing_data,   # arch는 레이어 분리 불필요 — 전체 전달
        "mapping_table":      {},
        "object_mapping":     [],
        "is_mapped":          False,
        "layer_role_stats":   {},
    }

    # ── 3. 매핑 (answer면 생략) — 이름 매핑과 위치 매핑 병렬 실행 ──────────────
    if has_drawing and intent != "answer":
        import asyncio as _asyncio

        # [최적화] execute_async: rule 매핑 → LLM 배치 병렬 폴백 (미분류 항목만)
        # get_instance로 인스턴스 캐싱 + execute_async로 직접 await (thread wrapping 불필요)
        _mapper = MappingAgent.get_instance(org_id=org_id)

        mapping_result, obj_mappings = await _asyncio.gather(
            _mapper.execute_async(drawing_data),
            run_object_mapping(
                drawing_data,
                domain_hint="건축",
                log_prefix="[ArchNode]",
            ),
        )

        context["mapping_table"]  = mapping_result
        context["is_mapped"]      = True
        context["object_mapping"] = obj_mappings
        lap_t = _lap("3. 이름/위치 매핑 병렬", lap_t)

        auto_cnt = sum(1 for m in obj_mappings if m.get("method") == "auto")
        llm_cnt  = sum(1 for m in obj_mappings if m.get("method") == "llm_fallback")
        print(f"[ArchNode MAP]  객체 매핑 결과: 총={len(obj_mappings)}쌍 (자동={auto_cnt}, LLM={llm_cnt})")

    # ── 4. 일반 인사/단순 대화 빠른 경로 ─────────────────────────────────────
    if intent == "answer" and _is_casual_message(message):
        logging.info("[ArchNode] casual message detected, direct LLM answer (no RAG)")
        direct = await llm_service.generate_answer(
            messages=[
                {"role": "system", "content": "당신은 친절한 건축법 전문 AI 어시스턴트입니다. 짧고 자연스럽게 대화하세요."},
                {"role": "user", "content": message},
            ],
        )
        if isinstance(direct, str) and direct.strip():
            return await _format_state(state, [{"agent": "direct", "result": direct.strip()}])

    # ── 5. LLM tool 선택 ──────────────────────────────────────────────────────
    system_prompt = _build_system_prompt(context, memory_text)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": message},
    ]
    tool_calls = await llm_service.generate_answer(
        messages=messages,
        tools=ARCH_SUB_AGENT_TOOLS,
        tool_choice="auto",
    )
    lap_t = _lap("5. LLM 도구 선택", lap_t)

    # LLM이 도구 없이 텍스트로 직접 응답한 경우
    if isinstance(tool_calls, str) and tool_calls.strip():
        logging.info("[ArchNode] direct text answer (no tool) intent=%s chars=%s", intent, len(tool_calls.strip()))
        if intent == "answer" and not _is_casual_message(message):
            logging.info("[ArchNode] answer 의도 직답 감지 → call_query_agent 강제 라우팅")
            tool_calls = [_make_fallback_call(message, has_drawing, "answer")]
        else:
            return await _format_state(state, [{"agent": "direct", "result": tool_calls.strip()}])

    if not isinstance(tool_calls, list) or not tool_calls:
        logging.warning("[ArchNode] LLM tool 미선택, fallback 적용 (drawing=%s intent=%s)", has_drawing, intent)
        tool_calls = [_make_fallback_call(message, has_drawing, intent)]

    for tc in tool_calls:
        if tc.get("function", {}).get("name") != "call_query_agent":
            continue
        args_str = tc["function"].get("arguments", "{}")
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else dict(args_str or {})
            display_query = str(args.get("display_query") or args.get("query") or message or "").strip()
            retrieval_query = _expand_retrieval_query(display_query)
            args["display_query"] = display_query
            args["retrieval_query"] = retrieval_query
            args["query"] = retrieval_query
            args["limit"] = 15 if _is_broad_rag_query(display_query) else int(args.get("limit") or 8)
            tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
            logging.info("[ArchNode RAG] display_query=%r retrieval_query=%r limit=%s", display_query[:80], retrieval_query[:120], args["limit"])
        except Exception as exc:
            logging.debug("[ArchNode] query anchor injection skipped: %s", exc)

    tool_names = [c["function"]["name"] for c in tool_calls]
    print(f"[ArchNode TRACK]  선택된 tool: {tool_names}")

    # ── 6. Tool 실행 (WorkflowHandler) ────────────────────────────────────────
    workflow         = ArchWorkflowHandler(session=context, db=db)
    workflow_results = await workflow.handle_tool_calls(tool_calls, context)
    lap_t = _lap(f"6. Tool 실행 ({','.join(tool_names)})", lap_t)

    if "call_query_agent" in tool_names:
        try:
            workflow_results = await _expand_arch_table_neighbors(workflow_results, db)
            lap_t = _lap("7a. Table neighbor expand", lap_t)
        except Exception as exc:
            logger.warning("[ArchNode] table neighbor expand 실패: %s", exc)

    # ── 7. 결과 → AgentState 변환 후 반환 ─────────────────────────────────────
    result = await _format_state(state, workflow_results)
    print(f"[ArchNode TRACK] ■ arch_review_node 총 {_time.time() - t0:.1f}s")
    return result


# ── 시스템 프롬프트 ──────────────────────────────────────────────────────────

def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    """
    LLM tool 선택 단계에 사용할 시스템 프롬프트를 구성합니다.

    context에서 도면 상태(로드 여부, 매핑 완료 여부, 수정 대기 건수), 의도(intent),
    도면 ID, 매핑 테이블 샘플, pending_fixes 미리보기를 조합합니다.
    intent='answer'이면 call_review_agent 호출을 억제하는 문구를 포함합니다.
    intent='review'/'action'이면 해당 tool 사용을 권장합니다.
    """
    drawing_loaded = context.get("drawing_loaded", False)
    is_mapped      = context.get("is_mapped", False)
    pending        = context.get("pending_fixes") or []
    drawing_id     = context.get("current_drawing_id") or ""
    term_map       = (context.get("mapping_table") or {}).get("term_map", {})

    drawing_status = "도면 로드됨" if drawing_loaded else "도면 없음"
    mapping_status = "매핑 완료"   if is_mapped      else "매핑 미완료"
    pending_status = f"수정 대기 {len(pending)}건" if pending else "수정 대기 없음"

    equipment_hint = ""
    if drawing_loaded and term_map:
        sample = list(term_map.items())[:10]
        lines  = "\n".join(f"  - {k}: {v}" for k, v in sample)
        more   = f"\n  ... 외 {len(term_map) - 10}건" if len(term_map) > 10 else ""
        equipment_hint = f"\n\n[도면 구조 목록 (매핑 후, 최대 10건)]\n{lines}{more}"

    pending_hint = ""
    if pending:
        lines = "\n".join(
            f"  - {f.get('handle', f.get('equipment_id', '?'))}: "
            f"{f.get('violation_type', '?')} / {f.get('action', '?')}"
            for f in pending[:5]
        )
        more  = f"\n  ... 외 {len(pending) - 5}건" if len(pending) > 5 else ""
        pending_hint = f"\n\n[수정 대기 항목]\n{lines}{more}"

    drawing_id_hint = f"\n도면 ID: {drawing_id}" if drawing_id else ""

    intent = context.get("intent") or "answer"
    intent_line = (
        "사용자 의도(라우터): 일반 Q&A/인사/건축법 조회 — "
        "인사나 일상 대화에는 도구 없이 직접 짧은 텍스트로만 답하세요. "
        "건축법·기준 조항 등 기술 질문이면 call_query_agent를 쓰세요. "
        "call_review_agent(도면 검토)는 '도면 검토/위반/전수'를 명시할 때만 쓰세요."
        if intent == "answer"
        else
        f"사용자 의도(라우터): {intent} — review면 건축법 위반 전수 분석, action이면 call_action_agent로 선택 객체 수정 검토."
    )

    return (
        f"당신은 20년 경력의 건축법 전문 AI 에이전트입니다.\n"
        f"{intent_line}\n"
        f"현재 상태: {drawing_status} | {mapping_status} | {pending_status}"
        f"{drawing_id_hint}"
        f"{equipment_hint}"
        f"{pending_hint}"
        f"\n\n[대화 메모리]\n{memory_text}"
        f"\n\n[도구 선택 기준]\n"
        f"- call_query_agent  : 건축법 시행령·기준 조항 조회 요청\n"
        f"- call_review_agent : 도면 전체 건축법 위반 검토. focus_area 생략 시 전체 검토.\n"
        f"- call_action_agent : 수정 지시 실행. pending_fixes 목록 기반으로 C# DrawingPatcher 명령 생성.\n\n"
        f"[답변 형식] 도구 없이 직접 답할 때는 마크다운(### 소제목, - 글머리, **강조**)으로 정리하세요.\n\n"
        f"반드시 하나의 도구를 선택하세요."
    )


# ── 표 인접 청크 확장 ─────────────────────────────────────────────────────────

async def _expand_arch_table_neighbors(
    workflow_results: list[dict[str, Any]],
    db: Any,
    *,
    window: int = 3,
    max_added_per_block: int = 8,
) -> list[dict[str, Any]]:
    from sqlalchemy import cast as _sa_cast, select as _sa_select
    from sqlalchemy.dialects.postgresql import UUID as _PG_UUID
    from backend.models.schema import DocumentChunk as _DocumentChunk

    expanded: list[dict[str, Any]] = []
    for block in workflow_results or []:
        if block.get("agent") != "query" or not isinstance(block.get("result"), list):
            expanded.append(block)
            continue
        result = block["result"]
        existing = {
            (str(row.get("document_id") or ""), str(row.get("chunk_index") or ""))
            for row in result
            if isinstance(row, dict)
        }
        refs: list[dict[str, Any]] = []
        for row in result:
            if not isinstance(row, dict) or row.get("chunk_type") == "table":
                continue
            content = str(row.get("content") or row.get("raw_content") or "")
            if not _HAS_TABLE_REF_RE.search(content):
                continue
            doc_id = str(row.get("document_id") or "")
            try:
                chunk_idx = int(row.get("chunk_index") or 0)
            except (TypeError, ValueError):
                continue
            if doc_id:
                refs.append({"doc_id": doc_id, "chunk_idx": chunk_idx})
        if not refs:
            expanded.append(block)
            continue

        new_chunks: list[dict[str, Any]] = []
        for ref in refs:
            try:
                stmt = (
                    _sa_select(_DocumentChunk)
                    .where(_DocumentChunk.document_id == _sa_cast(str(ref["doc_id"]), _PG_UUID(as_uuid=False)))
                    .where(_DocumentChunk.chunk_index >= int(ref["chunk_idx"]) - window)
                    .where(_DocumentChunk.chunk_index <= int(ref["chunk_idx"]) + window)
                    .where(_DocumentChunk.chunk_type == "table")
                )
                rows = await db.execute(stmt)
                neighbors = rows.scalars().all()
            except Exception as exc:
                logger.warning("[ArchNode TABLE EXPAND] DB 조회 실패 doc=%s idx=%s err=%s", ref["doc_id"], ref["chunk_idx"], exc)
                try:
                    await db.rollback()
                except Exception:
                    pass
                continue
            for nb in neighbors:
                key = (str(nb.document_id), str(nb.chunk_index))
                if key in existing:
                    continue
                existing.add(key)
                table_markdown = str(nb.table_markdown or "")
                new_chunks.append({
                    "source": "permanent",
                    "id": nb.id,
                    "document_id": str(nb.document_id),
                    "chunk_index": nb.chunk_index,
                    "content": table_markdown if table_markdown else str(nb.content or ""),
                    "raw_content": str(nb.content or ""),
                    "table_markdown": table_markdown or None,
                    "chunk_type": nb.chunk_type,
                    "domain": nb.domain,
                    "category": nb.category,
                    "doc_name": nb.doc_name,
                    "section_id": nb.section_id,
                    "page_number": None,
                    "_neighbor_expanded": True,
                })
                if len(new_chunks) >= max_added_per_block:
                    break
            if len(new_chunks) >= max_added_per_block:
                break
        expanded.append({**block, "result": list(result) + new_chunks})
    return expanded


# ── AgentState 변환 ──────────────────────────────────────────────────────────

async def _format_state(state: AgentState, workflow_results: list) -> AgentState:
    """
    WorkflowHandler 결과를 AgentState 필수 키로 변환합니다.

    ArchAgent의 정적 헬퍼(_violations_from_report_items, _build_pending_fixes)를 재사용하므로
    handle → object_id 매핑과 equipment_id 양방 정합이 보장됩니다.
    """
    violations:       list[ViolationItem]    = []
    suggestions:      list[str]              = []
    pending_fixes:    list[PendingFix]       = []
    referenced_laws:  list[str]              = []
    retrieved_laws:   list[LawReference]     = list(state.get("retrieved_laws") or [])
    final_message                            = ""
    current_step:     CurrentStep            = "agent_completed"
    response_meta:    dict[str, Any]         = {}
    user_request: str = str(state.get("user_request") or "").strip()

    for block in (workflow_results if isinstance(workflow_results, list) else []):
        agent  = block.get("agent")
        result = block.get("result")

        # ── query 결과 ─────────────────────────────────────────────────────────
        if agent == "query" and isinstance(result, list):
            retrieved_laws = _to_law_references(result)
            ch_list = [
                row for row in result
                if isinstance(row, dict)
                and (
                    str(row.get("content") or "").strip()
                    or str(row.get("table_markdown") or "").strip()
                    or str(row.get("raw_content") or "").strip()
                    or str(row.get("context_content") or "").strip()
                )
            ]
            ch_list = sorted(ch_list, key=_table_priority)
            answer_style = _detect_answer_style(user_request)
            if ch_list:
                context_text = _rag_chunks_to_readable_markdown(ch_list)
                prompt = _build_rag_answer_prompt(
                    user_request=user_request,
                    context_text=context_text,
                    answer_style=answer_style,
                )
                try:
                    summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                    final_message = summary if isinstance(summary, str) and summary.strip() else context_text
                except Exception as exc:
                    logger.warning("[ArchNode] RAG synthesis 실패: %s", exc, exc_info=True)
                    final_message = context_text
            else:
                final_message = (
                    "관련 건축법·시행령 근거 문서를 찾지 못했습니다. "
                    "질문을 좀 더 구체적으로 입력하시거나, "
                    "건축법 관련 문서를 업로드해 주세요."
                )
            current_step = "query_completed"
            meta_rows = [_chunk_to_meta_row(row) for row in result if isinstance(row, dict)]
            final_message = _strip_html(final_message)
            final_message += _format_rag_footer(meta_rows, n_chunks=len(result))
            response_meta = {
                "answer_type": "rag_query",
                "used_rag": bool(result),
                "answer_style": answer_style,
                "suggested_queries": _build_suggested_queries(user_request, answer_style) if ch_list else [],
                "retrieval": _retrieval_block_compact(meta_rows),
            }

        # ── review 결과 ────────────────────────────────────────────────────────
        elif agent == "review" and isinstance(result, dict):
            report   = result.get("report") or {}
            fixes    = result.get("fixes") or []
            rag_refs = result.get("rag_references") or []
            det_items = result.get("deterministic_violations") or []

            # ArchAgent 정적 헬퍼 재사용:
            #   _violations_from_report_items: item.get("handle") → object_id (RevCloud 위치 결정)
            #   _build_pending_fixes: handle + equipment_id 양쪽 채움 (C#/API 호환)
            violations    = ArchAgent._violations_from_report_items(report.get("items") or [])
            pending_fixes = ArchAgent._build_pending_fixes(fixes)
            suggestions   = [f["description"] for f in pending_fixes if f.get("description")]
            referenced_laws = list({
                v.get("legal_reference", "")
                for v in violations
                if v.get("legal_reference")
            })

            total         = report.get("total_violations", len(violations))
            final_message = (
                f"건축 검토 완료: 위반 {total}건. "
                "수정 항목을 확인하고 적용할 항목을 선택하세요."
            )
            final_message += _format_review_rag_footer(rag_refs)

            current_step  = "pending_fix_review"
            rmeta = [_chunk_to_meta_row(x) for x in rag_refs if isinstance(x, dict)]
            response_meta = {
                "answer_type": "review",
                "used_rag": bool(rag_refs),
                "retrieval": _retrieval_block_compact(rmeta),
                "review_categories": {
                    "rag_reference_count": len(rag_refs),
                    "deterministic_count": len(det_items),
                },
                # KPI(recall_lower_bound) 산출용 — 결정론적 검사가 추가되면 자동으로 활성화.
                # 현재 arch 도메인은 deterministic_checker 미구현이라 빈 리스트.
                "deterministic_equipment_ids": sorted({
                    str(v.get("object_id") or v.get("equipment_id") or v.get("handle") or "")
                    for v in det_items
                    if isinstance(v, dict)
                    and (v.get("object_id") or v.get("equipment_id") or v.get("handle"))
                }),
                "sllm_durations_ms": list(result.get("sllm_durations_ms") or []),
            }

        # ── action 결과 ────────────────────────────────────────────────────────
        elif agent == "action":
            final_message = (
                json.dumps(result, ensure_ascii=False)
                if not isinstance(result, str)
                else result
            )
            final_message += "\n\n---\n[출처] 액션 분석(DrawingPatcher 명령 생성)"
            current_step  = "action_ready"
            response_meta = {"answer_type": "action_command", "used_rag": False}

        # ── 직접 텍스트 응답 (일반 대화) ───────────────────────────────────────
        elif agent == "direct" and isinstance(result, str):
            final_message = _spec_text_to_readable_markdown(result) + _format_direct_footer()
            current_step  = "agent_completed"
            response_meta = {"answer_type": "llm_direct", "used_rag": False, "note": "RAG 미사용·직답"}

    if not final_message:
        final_message = "처리가 완료되었습니다."
        if not response_meta:
            response_meta = {"answer_type": "empty", "used_rag": False}

    invoked = [
        block.get("agent")
        for block in (workflow_results or [])
        if isinstance(block, dict) and block.get("agent")
    ]
    if invoked:
        response_meta = {**response_meta, "invoked_workflow": invoked}

    # pending_fixes만 있고 violations가 비는 경우 정합
    if (not violations) and pending_fixes:
        violations = _violations_from_pending_fixes(pending_fixes)

    final_message = _strip_html(final_message)

    review_result: ReviewResult = {
        "is_violation":    len(violations) > 0,
        "violations":      violations,
        "suggestions":     suggestions,
        "referenced_laws": referenced_laws,
        "final_message":   final_message,
    }

    return {
        **state,
        "review_result":      review_result,
        "current_step":       current_step,
        "assistant_response": final_message,
        "retrieved_laws":     retrieved_laws,
        "pending_fixes":      pending_fixes,
        "response_meta":      response_meta,
    }


# ── 변환 헬퍼 ────────────────────────────────────────────────────────────────

def _to_law_references(query_result: list[dict]) -> list[LawReference]:
    """
    QueryAgent 결과(list[dict])를 AgentState.retrieved_laws 형식(list[LawReference])으로 변환합니다.

    LawReference는 chunk_id, document_id, legal_reference, snippet, score, source_type 필드를 가집니다.
    document_chunk_id는 PK가 int인 영구 시방서 청크에만 추가됩니다.
    """
    refs: list[LawReference] = []
    for r in query_result:
        rid = r.get("id")
        entry: LawReference = {
            "chunk_id":        str(rid) if rid is not None else str(r.get("section_id") or r.get("chunk_index") or ""),
            "document_id":     str(r.get("document_id") or ""),
            "legal_reference": str(r.get("section_id") or r.get("doc_name") or ""),
            "snippet":         str(_get_rag_content_from_row(r) or ""),
            "score":           float(r.get("score") or 0.0),
            "source_type":     str(r.get("source") or "permanent"),
        }
        if isinstance(rid, int):
            entry["document_chunk_id"] = rid
        refs.append(entry)
    return refs


def _violations_from_pending_fixes(pending: list) -> list[ViolationItem]:
    """pending_fixes만 있고 violations가 비는 경우 UI/CAD 정합용 합성."""
    out: list[ViolationItem] = []
    for f in pending or []:
        if not isinstance(f, dict):
            continue
        handle = str(f.get("handle") or f.get("equipment_id") or "")
        vtype  = str(f.get("violation_type") or "")
        desc   = str(f.get("description") or "")
        out.append({
            "object_id":       handle,
            "violation_type":  vtype,
            "reason":          desc or (f"{vtype} (수정 대기)" if vtype else "수정 대기 항목"),
            "legal_reference": "",
            "suggestion":      desc,
            "current_value":   "",
            "required_value":  "",
        })
    return out


def _is_casual_message(message: str) -> bool:
    return bool(_CASUAL_RE.match((message or "").strip())) and len((message or "").strip()) < 30


def _make_fallback_call(message: str, has_drawing: bool = False, intent: str = "answer") -> dict:
    """
    LLM이 tool을 선택하지 않을 경우 intent 기반으로 기본 tool 호출을 생성합니다.

    우선순위:
        intent="answer"  → call_query_agent (법규 조회 fallback)
        intent="review" + has_drawing → call_review_agent (도면 전체 검토)
        intent="action" + has_drawing → call_action_agent (수정 명령 생성)
        그 외             → call_query_agent (최소 응답 보장)
    """
    if intent == "answer":
        return {
            "function": {
                "name": "call_query_agent",
                "arguments": json.dumps({"query": message or "건축법 조항 조회"}, ensure_ascii=False),
            }
        }
    if has_drawing and intent == "review":
        return {
            "function": {
                "name": "call_review_agent",
                "arguments": json.dumps({"focus_area": ""}, ensure_ascii=False),
            }
        }
    if has_drawing and intent == "action":
        return {
            "function": {
                "name": "call_action_agent",
                "arguments": json.dumps({}, ensure_ascii=False),
            }
        }
    return {
        "function": {
            "name": "call_query_agent",
            "arguments": json.dumps({"query": message or "건축법 검토"}, ensure_ascii=False),
        }
    }
