"""
File    : backend/services/agents/common/mapping.py
Author  : 송주엽
Create  : 2026-04-24
Modified: 2026-04-28 — 환각 방지 및 도메인 태깅 강화
    ① 공격적 사전 필터링 : DIMENSION, HATCH 등 비설비 엔티티 완벽 제거
    ② 확정 매핑 강화   : _QUICK_EXACT_MAP 확장으로 LLM 추측(환각) 원천 차단
    ③ 도메인 태깅 추가 : 매핑 결과에 도메인 정보를 명시하여 교차 검토 오류 방지
Description : 4개 도메인(pipe/elec/fire/arch) 공통 CAD 레이어·블록 매핑 엔진.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

# ─── _resolve() 전용 모듈 레벨 컴파일 정규식 ─────────────────────────────────
_PREFIX_PATTERN = re.compile(r"^([A-Za-z]+)")
_SIZE_PATTERN   = re.compile(r"(\d+)$")

# ─── MappingAgent 인스턴스 캐시 ───────────────────────────────────────────────
_agent_instance_cache: dict[tuple[str, str], "BaseMappingAgent"] = {}
_agent_instance_lock = threading.Lock()


# ─── DB 룰 엔트리 ────────────────────────────────────────────────────────────
@dataclass
class _RuleEntry:
    """mapping_rules 테이블의 단일 행."""
    id: str
    source_key: str
    standard_name: str
    rule_type: str          # "PREFIX" | "EXACT" | "LAYER" | "BLOCK" | "ENTITY_TYPE"
    style_config: dict = field(default_factory=dict)
    standard_term_id: str = ""
    layer_role: str = ""    # "arch" | "mep" | "aux" | "" (pipe 도메인 전용)


# ─── UUID 검증 ────────────────────────────────────────────────────────────────
def _is_valid_uuid_str(s: str) -> bool:
    try:
        uuid.UUID(str(s).strip())
        return True
    except (ValueError, TypeError, AttributeError):
        return False


# ─── 싱글턴 동기 엔진 ─────────────────────────────────────────────────────────
_sync_engine = None
_sync_engine_lock = threading.Lock()


def _get_sync_engine():
    global _sync_engine
    if _sync_engine is None:
        with _sync_engine_lock:
            if _sync_engine is None:
                from sqlalchemy import create_engine
                from backend.core.config import settings
                _sync_engine = create_engine(
                    settings.DATABASE_URL.replace("postgresql+asyncpg", "postgresql"),
                    pool_pre_ping=True,
                    pool_size=3,
                    max_overflow=2,
                )
                logging.info("[BaseMappingAgent] 싱글턴 동기 DB 엔진 초기화 완료 (pool_size=3)")
    return _sync_engine


# ─── lru_cache 기반 캐시 ──────────────────────────────────────────────────────
@lru_cache(maxsize=400)
def _fetch_rules_cached(domain: str, org_id: str) -> tuple:
    """
    (domain, org_id) 단위 DB 매핑 룰 조회 및 캐싱.
    """
    info = _fetch_rules_cached.cache_info()
    logging.info(
        "[BaseMappingAgent] 캐시 miss — DB 조회 (domain=%s org_id=%s hits=%d misses=%d)",
        domain, org_id, info.hits, info.misses,
    )
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from backend.models.schema import MappingRule, StandardTerm

        engine = _get_sync_engine()
        with Session(engine) as db:
            if domain == "pipe":
                stmt = (
                    select(
                        MappingRule.id,
                        MappingRule.source_key,
                        MappingRule.rule_type,
                        MappingRule.style_config,
                        MappingRule.standard_term_id,
                        MappingRule.layer_role,
                        StandardTerm.standard_name,
                    )
                    .join(StandardTerm, MappingRule.standard_term_id == StandardTerm.id)
                    .where(MappingRule.domain == domain)
                    .where(MappingRule.is_active == True)
                    .where(MappingRule.org_id == org_id)
                )
                rows = db.execute(stmt).all()
                rules = tuple(
                    _RuleEntry(
                        id=row.id,
                        source_key=row.source_key,
                        standard_name=row.standard_name,
                        rule_type=row.rule_type or "PREFIX",
                        style_config=row.style_config or {},
                        standard_term_id=row.standard_term_id or "",
                        layer_role=row.layer_role or "",
                    )
                    for row in rows
                )
            else:
                stmt = (
                    select(
                        MappingRule.id,
                        MappingRule.source_key,
                        MappingRule.rule_type,
                        MappingRule.style_config,
                        MappingRule.standard_term_id,
                        StandardTerm.standard_name,
                    )
                    .join(StandardTerm, MappingRule.standard_term_id == StandardTerm.id)
                    .where(MappingRule.domain == domain)
                    .where(MappingRule.is_active == True)
                    .where(MappingRule.org_id == org_id)
                )
                rows = db.execute(stmt).all()
                rules = tuple(
                    _RuleEntry(
                        id=row.id,
                        source_key=row.source_key,
                        standard_name=row.standard_name,
                        rule_type=row.rule_type or "PREFIX",
                        style_config=row.style_config or {},
                        standard_term_id=row.standard_term_id or "",
                    )
                    for row in rows
                )
        logging.info(
            "[BaseMappingAgent] DB 조회 완료 — %d개 룰 (domain=%s org_id=%s)",
            len(rules), domain, org_id,
        )
        return rules
    except Exception as e:
        logging.warning("[BaseMappingAgent] DB 매핑 로드 실패, 기본값 사용: %s", e)
        return ()


def invalidate_mapping_cache(org_id: str | None = None) -> None:
    before = _fetch_rules_cached.cache_info()
    _fetch_rules_cached.cache_clear()
    logging.info(
        "[BaseMappingAgent] 매핑 캐시 전체 무효화 (org_id=%s) — hits=%d misses=%d size=%d",
        org_id or "ALL", before.hits, before.misses, before.currsize,
    )


def get_mapping_cache_stats() -> dict:
    info = _fetch_rules_cached.cache_info()
    total = info.hits + info.misses
    return {
        "hits": info.hits,
        "misses": info.misses,
        "hit_rate_pct": round(info.hits / total * 100, 1) if total else 0.0,
        "currsize": info.currsize,
        "maxsize": info.maxsize,
    }


# ════════════════════════════════════════════════════════════════════════════════
# [환각 방지 ①] 공격적 사전 필터링 상수
# ════════════════════════════════════════════════════════════════════════════════

# 설비와 전혀 무관한 CAD 엔티티 타입 — 추출 단계에서 완전 배제
_SKIP_ENTITY_TYPES: frozenset[str] = frozenset({
    "DIMENSION",        # 치수선
    "HATCH",            # 해치/패턴
    "VIEWPORT",         # 뷰포트
    "OLE2FRAME",        # OLE 객체
    "WIPEOUT",          # 마스킹
    "IMAGE",            # 래스터 이미지
    "UNDERLAY",         # PDF/DGN 언더레이
    "RTEXT",            # 참조 텍스트
    "MLEADER",          # 다중 지시선
    "LEADER",           # 지시선
    "MTEXT",            # 다중행 텍스트 (주석)
    "TEXT",             # 단일행 텍스트 (주석)
    "ACAD_PROXY_ENTITY",
    "TABLE",            # 도면 표
})

# 치수·주석·도면테두리·보조선 전용 레이어명 패턴 조기 제거
_DIM_LAYER_PRE_RE = re.compile(
    r"^(?:"
    r"DIM(?:S|ENSION)?|"        # DIM, DIMS, DIMENSION
    r"ANNO(?:TATION)?|"         # ANNO, ANNOTATION
    r"TXT[-_]?|TEXT[-_]?|"      # TXT, TEXT
    r"TITLE(?:BLOCK)?|"         # TITLE, TITLEBLOCK
    r"(?:FRAME|BORDER)(?:[-_].*)?|"  # FRAME, BORDER
    r"GRID(?:[-_].*)?|"         # GRID
    r"VIEWPORT|VPORT|"          # 뷰포트
    r"XREF(?:[-_].*)?|"         # XREF
    r"(?:NOTE|NOTES?)(?:[-_].*)?|"   # NOTE, NOTES
    r"LABEL(?:[-_].*)?|"        # LABEL
    r"(?:SECTION|DETAIL)(?:[-_].*)?|"
    r"DEFPOINTS|"
    r"HAT(?:CH)?|"              # HAT, HATCH
    r"(?:CENTER|CENTRE|CEN)(?:[-_].*)?|" # 중심선
    r"(?:HIDDEN|DASH|PHANTOM)(?:[-_].*)?" # 가상선/은선
    r")$",
    re.IGNORECASE,
)


# ════════════════════════════════════════════════════════════════════════════════
# [환각 방지 ②] 확정 매핑 확장 사전 (_QUICK_EXACT_MAP)
# ════════════════════════════════════════════════════════════════════════════════

# 도메인 태깅을 위한 구조: { "약어": ("표준명", "domain_tag") }
_QUICK_EXACT_MAP_WITH_DOMAIN: dict[str, tuple[str, str]] = {
    # ── 배관 (pipe) ────────────────────────────────────────────────────────
    "GAS": ("가스배관", "pipe"), "CW": ("냉수배관", "pipe"), "HW": ("온수배관", "pipe"),
    "SWR": ("오수배관", "pipe"), "DW": ("음용수배관", "pipe"), "FW": ("소방수배관", "pipe"),
    "V": ("밸브", "pipe"), "VLV": ("밸브", "pipe"), "P": ("펌프", "pipe"),
    "TK": ("탱크", "pipe"), "EQ": ("장비", "pipe"),

    # ── 전기 (elec) ────────────────────────────────────────────────────────
    "EL": ("전기배선", "elec"), "ELEC": ("전기설비", "elec"), 
    "LT": ("조명", "elec"), "LTG": ("조명설비", "elec"),
    "PWR": ("전원배선", "elec"), "GRD": ("접지", "elec"), "GND": ("접지", "elec"),
    "PB": ("풀박스", "elec"), "ELP": ("전기배관", "elec"),

    # ── 소방 (fire) ────────────────────────────────────────────────────────
    "FA": ("화재경보", "fire"), "FS": ("소방설비", "fire"), 
    "SP": ("스프링클러", "fire"), "SPK": ("스프링클러헤드", "fire"),
    "FD": ("방화댐퍼", "fire"), "SD": ("연기댐퍼", "fire"),

    # ── 건축/구조 (arch) - 전기 검토시 무시됨 ─────────────────────────────────
    "WL": ("벽체", "arch"), "WALL": ("벽체", "arch"),
    "COL": ("기둥", "arch"), "COLUMN": ("기둥", "arch"),
    "BM": ("보", "arch"), "SLB": ("슬래브", "arch"), "FND": ("기초", "arch"),
    
    # 공통/기타
    "SITE": ("부지", "common"), "PLOT": ("부지경계", "common"),
}

# 기존 코드와의 호환성을 위해 이름만 추출한 딕셔너리 생성
_QUICK_EXACT_MAP: dict[str, str] = {k: v[0] for k, v in _QUICK_EXACT_MAP_WITH_DOMAIN.items()}


# ── 기존 기본 상수 ─────────────────────────────────────────────────────────────
_DEFAULT_PREFIX_MAP: dict[str, str] = {
    "GV":   "게이트밸브",
    "BV":   "볼밸브",
    "CV":   "체크밸브",
    "BFV":  "버터플라이밸브",
    "RV":   "릴리프밸브",
    "PRV":  "감압밸브",
    "PMP":  "펌프",
    "HX":   "열교환기",
    "STR":  "스트레이너",
    "FLG":  "플랜지",
    "ELB":  "엘보",
    "TEE":  "티",
    "RED":  "리듀서",
    "CAP":  "캡",
    "EXP":  "신축이음",
    "HGR":  "행거",
    "SHG":  "내진행거",
    "INS":  "보온재",
    "FSL":  "방화충전",
}

_DEFAULT_ENTITY_TYPE_MAP: dict[str, str] = {
    "LINE":        "선",
    "CIRCLE":      "원",
    "ARC":         "호",
    "POLYLINE":    "폴리선",
    "SPLINE":      "스플라인",
    "ELLIPSE":     "타원",
    "BLOCK":       "블록",
    "MTEXT":       "다중행문자",
    "TEXT":        "문자",
    "DIMENSION":   "치수",
    "HATCH":       "해치",
    "SOLID":       "솔리드",
    "MLEADER":     "다중지시선",
}

_EQUIPMENT_PREFIXES: set[str] = {"PMP", "HX", "STR"}

# 조기 필터로 걸러지지 않은 잔여물 2차 제거
_IGNORE_LAYERS: set[str] = {
    "DEFPOINTS",
    "DIM", "DIMENSION", "DIMS",
    "CEN", "CENTER", "CENTRE",
    "HAT", "HATCH",
    "ANNO", "ANNOTATION",
    "TITLE", "TITLEBLOCK", "FRAME", "BORDER",
    "GRID",
    "FINISH",
    "VIEWPORT", "VPORT",
    "XREF", "X-REF",
    "HIDDEN", "PHANTOM", "DASHED",
    "NOTE", "NOTES", "LABEL",
    "AI_REVIEW", "AI_RESULT", "AI_CLOUD", "AI_PROPOSAL",
    "SECTION", "DETAIL",
    "CURTAIN", "MULLION",
}

_IGNORE_LAYER_RE = re.compile(
    r"^(?:"
    r"Z_\w+|"
    r"AI_\w+|"
    r"\$\d+\$\w+|"
    r"\w+\$\d*\$\w+|"
    r"A-\w+|"
    r"S-\w+"
    r")$",
    re.IGNORECASE,
)


def _is_ignored_layer(name: str) -> bool:
    upper = name.upper()
    return upper in _IGNORE_LAYERS or bool(_IGNORE_LAYER_RE.match(upper))


# ── 이름 추출 헬퍼 ────────────────────────────────────────────────────────────
def _to_name(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("name") or item.get("layer_name") or item.get("block_name") or "")
    return str(item)


# ════════════════════════════════════════════════════════════════════════════════
# [최적화 ③] LLM 배치 병렬 폴백
# ════════════════════════════════════════════════════════════════════════════════

_LLM_BATCH_SIZE = 15

_llm_sem: asyncio.Semaphore | None = None
_llm_sem_lock = threading.Lock()


def _get_llm_sem() -> asyncio.Semaphore:
    global _llm_sem
    if _llm_sem is None:
        with _llm_sem_lock:
            if _llm_sem is None:
                _llm_sem = asyncio.Semaphore(8)
    return _llm_sem


_llm_result_cache: dict[str, str | None] = {}


async def _classify_batch_with_llm(names: list[str], domain: str) -> dict[str, str | None]:
    from backend.services import llm_service

    domain_hint = {
        "pipe": "배관/플랜트",
        "elec": "전기",
        "fire": "소방",
        "arch": "건축",
    }.get(domain, "건설")

    items_str = "\n".join(f"- {n}" for n in names)
    system_prompt = (
        "당신은 CAD 도면 레이어·블록 명칭을 한국어 표준 설비명으로 변환하는 분류기입니다.\n"
        "레이어명·색상·약어 규칙은 사용자/프로젝트마다 다를 수 있습니다. "
        "도메인 힌트는 참고만 하고, 명칭 자체가 치수·주석·건축·전기·소방·심볼로 보이면 "
        "배관/해당 도메인 설비로 억지 분류하지 마십시오.\n"
        "오직 JSON만 반환하고, 설명·부가 텍스트는 절대 포함하지 마십시오."
    )
    user_prompt = (
        f"다음은 CAD 도면의 {domain_hint} 설비 레이어·블록 명칭 목록입니다.\n"
        f"각 항목을 한국어 표준 설비명으로 변환하거나,\n"
        f"설비와 완전히 무관한 항목(치수선, 해치, 주석, 도면 테두리 등)은 null을 반환하세요.\n\n"
        f"명칭 목록:\n{items_str}\n\n"
        f'반드시 아래 JSON 형식으로만 응답하세요 (results 키 필수):\n'
        f'{{"results": {{"원래명칭": "표준명 또는 null"}}}}'
    )

    try:
        raw = await llm_service.generate_answer(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        mapping: dict = {}
        if isinstance(raw, dict):
            mapping = raw.get("results", {})
        elif isinstance(raw, str):
            d = json.loads(raw)
            mapping = d.get("results", {})

        out: dict[str, str | None] = {}
        for n in names:
            val = mapping.get(n)
            if val and str(val).strip() not in {"", "null", "None"}:
                out[n] = str(val).strip()
            else:
                out[n] = None
        return out

    except Exception as exc:
        logging.warning("[BaseMappingAgent] LLM 배치 분류 실패 (%d건): %s", len(names), exc)
        return {n: None for n in names}


# ════════════════════════════════════════════════════════════════════════════════
# 기반 매핑 에이전트
# ════════════════════════════════════════════════════════════════════════════════
class BaseMappingAgent:
    DOMAIN: str = ""

    def __init__(
        self,
        org_id: str | None = None,
        custom_term_map: dict[str, str] | None = None,
        db=None,
    ):
        oid = (org_id or "").strip()
        self._db_rules: list[_RuleEntry] = (
            self._get_rules(oid) if oid and _is_valid_uuid_str(oid) else []
        )

        self._exact_map:       dict[str, _RuleEntry] = {}
        self._prefix_map:      dict[str, _RuleEntry] = {}
        self._entity_type_map: dict[str, _RuleEntry] = {}

        for rule in self._db_rules:
            rt = (rule.rule_type or "PREFIX").upper()
            if rt in {"EXACT", "LAYER", "BLOCK"}:
                self._exact_map[rule.source_key] = rule
            elif rt == "ENTITY_TYPE":
                self._entity_type_map[rule.source_key.upper()] = rule
            else:
                self._prefix_map[rule.source_key.upper()] = rule

        db_prefix_terms = {k: v.standard_name for k, v in self._prefix_map.items()}
        domain_defaults = self._get_domain_prefix_map()

        self.term_map: dict[str, str] = {
            **_QUICK_EXACT_MAP,
            **_DEFAULT_PREFIX_MAP,
            **domain_defaults,
            **db_prefix_terms,
            **(custom_term_map or {}),
        }

        db_entity_terms = {k: v.standard_name for k, v in self._entity_type_map.items()}
        self.entity_type_map: dict[str, str] = {
            **_DEFAULT_ENTITY_TYPE_MAP,
            **db_entity_terms,
        }

    def _get_domain_prefix_map(self) -> dict[str, str]:
        return {}

    @classmethod
    def _get_rules(cls, org_id: str) -> list[_RuleEntry]:
        return list(_fetch_rules_cached(cls.DOMAIN, org_id))

    # ── [환각 방지 ①] 이름 수집 + 비설비 엔티티 철저한 사전 필터링 ─────────────────────
    @staticmethod
    def _collect_names(data: dict) -> set[str]:
        raw_layers: list = data.get("layers", [])
        raw_blocks: list = data.get("blocks", [])

        if not raw_blocks:
            raw_blocks = [
                e.get("block_name") or e.get("name", "")
                for e in data.get("entities", [])
                if e.get("type", "").upper() not in _SKIP_ENTITY_TYPES
                and (e.get("block_name") or e.get("name", ""))
            ]

        all_names: set[str] = set()

        for x in raw_layers:
            name = _to_name(x).strip()
            if not name:
                continue
            if _DIM_LAYER_PRE_RE.match(name):   # 치수·주석·보조선 조기 필터
                continue
            all_names.add(name.upper())

        for x in raw_blocks:
            name = _to_name(x).strip()
            if name:
                all_names.add(name.upper())

        return all_names

    def execute(self, layer_block_data: str | dict) -> dict:
        """
        동기 rule-based 매핑.
        반환: {"term_map": {}, "style_map": {}, "entity_type_map": {}, "domain_tags": {}, "unmapped": []}
        """
        try:
            data = (
                json.loads(layer_block_data)
                if isinstance(layer_block_data, str)
                else layer_block_data
            )
        except (json.JSONDecodeError, TypeError):
            return {"term_map": {}, "style_map": {}, "entity_type_map": {}, "domain_tags": {}, "unmapped": [], "error": "파싱 실패"}

        all_names = self._collect_names(data)

        resolved: dict[str, str] = {}
        style_map: dict[str, dict] = {}
        domain_tags: dict[str, str] = {} # [환각 방지 ③] 도메인 태깅
        unmapped: list[str] = []
        skipped: list[str] = []

        for name in all_names:
            if _is_ignored_layer(name):
                skipped.append(name)
                continue
            
            label, style = self._resolve(name)
            if label:
                resolved[name] = label
                if style:
                    style_map[name] = style
                
                # 도메인 태그 부여 (QUICK_EXACT_MAP_WITH_DOMAIN 활용)
                if name in _QUICK_EXACT_MAP_WITH_DOMAIN:
                    domain_tags[name] = _QUICK_EXACT_MAP_WITH_DOMAIN[name][1]
                else:
                    domain_tags[name] = self.DOMAIN # 기본적으로 현재 에이전트 도메인 부여
            else:
                unmapped.append(name)

        if skipped:
            logging.info("[BaseMappingAgent] 보조 레이어 %d건 스킵: %s", len(skipped), skipped[:10])

        entity_resolved: dict[str, str] = {}
        for et in data.get("entity_types", []):
            et_upper = et.upper()
            label = self.entity_type_map.get(et_upper)
            if label:
                entity_resolved[et] = label

        return {
            "term_map":        resolved,
            "style_map":       style_map,
            "entity_type_map": entity_resolved,
            "domain_tags":     domain_tags,
            "unmapped":        unmapped,
        }

    async def execute_async(self, layer_block_data: str | dict) -> dict:
        result = self.execute(layer_block_data)
        unmapped: list[str] = result.get("unmapped", [])
        if not unmapped:
            return result

        to_query: list[str] = []
        cache = _llm_result_cache
        for name in unmapped:
            if name in cache:
                val = cache[name]
                if val is not None:
                    result["term_map"][name] = val
                    result["domain_tags"][name] = self.DOMAIN # LLM이 찾은 것도 현재 도메인으로 태깅
            else:
                to_query.append(name)

        still_unmapped: list[str] = [n for n in unmapped if n not in result["term_map"]]

        if not to_query:
            result["unmapped"] = still_unmapped
            return result

        batches = [
            to_query[i : i + _LLM_BATCH_SIZE]
            for i in range(0, len(to_query), _LLM_BATCH_SIZE)
        ]
        sem = _get_llm_sem()

        async def _run_batch(batch: list[str]) -> dict[str, str | None]:
            async with sem:
                return await _classify_batch_with_llm(batch, self.DOMAIN)

        batch_results = await asyncio.gather(
            *(_run_batch(b) for b in batches),
            return_exceptions=True,
        )

        llm_resolved: dict[str, str] = {}
        for batch, res in zip(batches, batch_results):
            if isinstance(res, Exception):
                logging.warning("[BaseMappingAgent] 배치 LLM 오류: %s", res)
                continue
            for name in batch:
                val = res.get(name)
                cache[name] = val
                if val is not None:
                    llm_resolved[name] = val

        result["term_map"].update(llm_resolved)
        # LLM이 해결한 항목들도 도메인 태그 추가
        for name in llm_resolved:
            result["domain_tags"][name] = self.DOMAIN
            
        result["unmapped"] = [n for n in still_unmapped if n not in llm_resolved]

        if llm_resolved or len(result["unmapped"]) < len(unmapped):
            logging.info(
                "[BaseMappingAgent] LLM 폴백 완료 — 신규 분류 %d건, 미분류 잔존 %d건 "
                "(배치 %d개 병렬, 캐시 %d건)",
                len(llm_resolved),
                len(result["unmapped"]),
                len(batches),
                len(unmapped) - len(to_query),
            )
        return result

    @classmethod
    def get_instance(cls, org_id: str | None = None) -> "BaseMappingAgent":
        key = (cls.__name__, (org_id or "").strip())
        cached = _agent_instance_cache.get(key)
        if cached is not None:
            return cached
        with _agent_instance_lock:
            cached = _agent_instance_cache.get(key)
            if cached is None:
                cached = cls(org_id=org_id)
                _agent_instance_cache[key] = cached
                logging.info(
                    "[BaseMappingAgent] 인스턴스 캐시 저장 (cls=%s org_id=%s)",
                    cls.__name__, key[1] or "—",
                )
        return cached

    def _resolve(self, name: str) -> tuple[str | None, dict]:
        if name in self._exact_map:
            rule = self._exact_map[name]
            return rule.standard_name, rule.style_config

        quick = self.term_map.get(name)
        if quick:
            return quick, {}

        match = _PREFIX_PATTERN.match(name)
        if not match:
            return None, {}

        prefix = match.group(1).upper()
        db_prefix_rule = self._prefix_map.get(prefix)
        term = db_prefix_rule.standard_name if db_prefix_rule else self.term_map.get(prefix)
        if not term:
            return None, {}

        style = db_prefix_rule.style_config if db_prefix_rule else {}
        size_match = _SIZE_PATTERN.search(name)
        if size_match:
            size = size_match.group(1)
            suffix = f" #{size}" if prefix in _EQUIPMENT_PREFIXES else f" {size}A"
            return f"{term}{suffix}", style

        return term, style
