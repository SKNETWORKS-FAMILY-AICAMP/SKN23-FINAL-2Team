"""
File    : backend/services/agents/fire/workflow_handler.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방 도메인 서브 에이전트들의 실행 흐름 조율

Modification History:
    - 2026-04-15 (김민정) : 서브 에이전트 핸들링 및 리뷰 파이프라인 조율 로직 구현
    - 2026-04-17 (김민정) : 리뷰 파이프라인에서 규정 검색 시 user_request 활용하도록 수정
    - 2026-04-19 (김민정) : llm_client 제거 및 서브 에이전트 초기화 로직 수정
    - 2026-04-23       : piping 방식과 동일하게 통일 (AsyncSession, tool 파싱, 리뷰 파이프라인)
    - 2026-05-04       : Signal-to-Candidate 파이프라인 적용 (nearest_distances 기반 spacing_observed, applied_threshold 힌트)
"""

import asyncio
import json
import logging
from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.agents.fire.sub.query import QueryAgent
from backend.services.agents.fire.sub.review.parser import ParserAgent
from backend.services.agents.fire.sub.review.compliance import ComplianceAgent
from backend.services.agents.fire.sub.review.report import ReportAgent
from backend.services.agents.fire.sub.review.revision import RevisionAgent
from backend.services.agents.fire.sub.action import ActionAgent
from backend.services.agents.fire.rag_engine import FireRagEngine
from backend.services.agents.fire.sub.signal_extractor import SignalExtractor
from backend.services.agents.fire.sub.candidate_generator import CandidateGenerator
from backend.services.agents.fire.sub.query_builder import QueryBuilder
from backend.services.agents.fire.rule_slots.rule_slot import load_rule_slots


# ── 낙서/노이즈 엔티티 감지 ──────────────────────────────────────────────────
# LINE/CIRCLE 등 기하 도형은 제목 란·테두리 등 정상 도면 요소와 구분 불가 — 제외
# 타입 자체로 비설비임이 확실한 것만 잡는다
_NOISE_RAW_TYPES: frozenset[str] = frozenset({"DIMENSION", "HATCH", "VIEWPORT"})
_TEXT_TYPES: frozenset[str] = frozenset({"MTEXT", "TEXT", "ATTDEF"})


def _detect_noise_entities(elements: list) -> list:
    """
    파싱된 소방 엔티티 목록에서 노이즈 의심 항목을 탐지한다.
    반환값은 ComplianceAgent violations 스키마와 동일한 dict 리스트.
    proposed_action.type = "DELETE" → C# DrawingPatcher가 바로 처리 가능.

    감지 기준 (신뢰도 높은 타입 기반만):
      - DIMENSION / HATCH / VIEWPORT : 레이어 무관, 항상 비설비 객체
      - TEXT/MTEXT + fire_category=unknown : 소방과 무관한 텍스트
    """
    noise: list[dict] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        handle = str(el.get("handle") or el.get("id") or "").strip()
        if not handle:
            continue
        layer = str(el.get("layer") or "").strip()
        raw_type = str(el.get("raw_type") or el.get("type") or "").upper().strip()
        # fire_category 없으면 "unknown" 처리 — 소방 설비 미분류 텍스트로 취급하는 것이 의도된 동작
        fire_category = el.get("fire_category") or "unknown"
        current = f"layer={layer}, type={raw_type}"
        reason: str | None = None

        if raw_type in _NOISE_RAW_TYPES:
            reason = f"설비가 아닌 주석·표현 객체({raw_type})입니다."
        elif raw_type in _TEXT_TYPES and fire_category == "unknown":
            reason = f"소방 설비와 무관한 텍스트 객체({raw_type})입니다."

        if reason:
            noise.append({
                "equipment_id": handle,
                "violation_type": "noise_entity",
                "severity": "WARNING",
                "reason": reason,
                "current_value": current,
                "required_value": "소방 설비 레이어(SP-, FIRE 등)에 배치된 인식 가능한 설비",
                "proposed_action": {"type": "DELETE"},
            })
    return noise


def _build_spacing_candidate_violations(
    candidates: list,
    rag_results: list,
    existing_violations: list,
    *,
    limit: int = 20,
) -> list:
    # CandidateGenerator 경로가 spacing 위반을 전담하므로 비활성화.
    return []


def _fmt_mm(value: Any) -> str:
    try:
        return f"{float(value):.0f}mm"
    except (TypeError, ValueError):
        return str(value or "")


def _required_mm(value: Any) -> str:
    text = _fmt_mm(value)
    return f"{text} 이하" if text else ""


def _equipment_label(category: str) -> str:
    return {
        "detector": "감지기",
        "sprinkler": "스프링클러",
        "extinguisher": "소화기",
        "hydrant": "소화전",
    }.get(category, category or "설비")


def _violation_key(violation: dict) -> tuple[str, str]:
    return (
        str(violation.get("violation_type") or ""),
        str(violation.get("equipment_id") or ""),
    )


def _hard_candidate_to_violation(candidate: Any) -> dict | None:
    """Convert code-confirmed numeric candidates into violations."""
    if not getattr(candidate, "numeric_violation", False):
        return None

    evidence = getattr(candidate, "evidence", None)
    if evidence is None:
        return None

    equipment_id = str(getattr(evidence, "equipment_id", "") or "").strip()
    if not equipment_id:
        return None

    candidate_type = str(getattr(candidate, "candidate_type", "") or "")
    category = str(getattr(candidate, "equipment_category", "") or getattr(evidence, "fire_category", "") or "")
    observed = getattr(evidence, "observed_value", None)
    threshold = getattr(candidate, "applied_threshold", None) or getattr(evidence, "threshold", None)
    context = getattr(evidence, "context", None) or {}
    reference_rule = getattr(candidate, "rule_id", "") or getattr(candidate, "rule_topic", "")

    if candidate_type == "coverage":
        return {
            "equipment_id": equipment_id,
            "violation_type": "coverage_error",
            "fire_category": "extinguisher",
            "severity": getattr(candidate, "severity_hint", "") or "CRITICAL",
            "reference_rule": reference_rule,
            "reason": (
                f"소화기 커버리지 공백입니다. 이 지점에서 가장 가까운 소화기까지 "
                f"{_fmt_mm(observed)}로 기준 {_required_mm(threshold)}를 초과합니다. "
                "보행거리 20m 기준을 직선거리로 근사한 수치 확정 후보입니다."
            ),
            "current_value": _fmt_mm(observed),
            "required_value": _required_mm(threshold),
            "x": context.get("x"),
            "y": context.get("y"),
            "nearest_extinguisher_id": context.get("nearest_extinguisher_id", ""),
            "distance_mm": observed,
            "threshold_mm": threshold,
        }

    if candidate_type == "spacing":
        label = _equipment_label(category)
        return {
            "equipment_id": equipment_id,
            "violation_type": "spacing_error",
            "fire_category": category,
            "severity": getattr(candidate, "severity_hint", "") or "CRITICAL",
            "reference_rule": reference_rule,
            "reason": (
                f"{label} 간격이 {_fmt_mm(observed)}로 기준 "
                f"{_required_mm(threshold)}를 초과합니다. 코드가 계산한 수치 확정 후보입니다."
            ),
            "current_value": _fmt_mm(observed),
            "required_value": _required_mm(threshold),
            "nearest_head": context.get("nearest_head", ""),
            "distance_mm": observed,
            "threshold_mm": threshold,
        }

    return None


def _ensure_hard_candidate_violations(violations: list, candidates: list) -> list:
    result = list(violations or [])
    existing = {
        _violation_key(v)
        for v in result
        if isinstance(v, dict)
    }

    for candidate in candidates or []:
        violation = _hard_candidate_to_violation(candidate)
        if not violation:
            continue
        key = _violation_key(violation)
        if key in existing:
            continue
        result.append(violation)
        existing.add(key)

    return result


def _filter_spacing_violations(violations: list, hard_spacing: list) -> list:
    """LLM이 hard candidate 없이 생성한 spacing_error를 제거한다.

    equipment_id 우선 매칭 → fire_category 폴백 순서로 hard candidate와 연결.
    coverage_error 등 다른 violation_type은 무조건 통과.
    """
    _hard_by_id = {
        str(c.evidence.equipment_id): c.equipment_category
        for c in hard_spacing
        if c.evidence is not None
    }
    _hard_cats = {c.equipment_category for c in hard_spacing}

    def _keep(v: dict) -> bool:
        if v.get("violation_type") != "spacing_error":
            return True
        eq_id = str(v.get("equipment_id") or "")
        cat   = str(v.get("fire_category") or "")
        if eq_id and eq_id in _hard_by_id:
            return True
        if cat and cat in _hard_cats:
            return True
        return False

    return [v for v in violations if _keep(v)]


def _build_compact_review_payload(parsed: dict, spacing_candidates: list) -> dict:
    """
    Compliance LLM 입력 전용 축약 payload.
    원본 도면 elements는 수만 건일 수 있으므로 소방 설비와 간격 후보 주변 객체만 유지하고,
    전체 pairwise 거리(sprinkler_spacings)는 제거해 토큰 사용량을 줄인다.
    """
    if not isinstance(parsed, dict):
        return {"elements": []}

    fire_topology = dict(parsed.get("fire_topology") or {})
    candidate_heads = {
        str(item.get("head") or "")
        for item in spacing_candidates or []
        if isinstance(item, dict)
    }
    candidate_heads.update(
        str(item.get("nearest_head") or "")
        for item in spacing_candidates or []
        if isinstance(item, dict)
    )
    candidate_heads.discard("")

    compact_elements: list[dict] = []
    for el in parsed.get("elements") or []:
        if not isinstance(el, dict):
            continue
        handle = str(el.get("handle") or el.get("id") or "")
        category = str(el.get("fire_category") or "")
        if category in {"sprinkler", "detector", "hydrant", "extinguisher", "pump", "alarm", "panel", "pipe"} or handle in candidate_heads:
            compact_elements.append(el)

    if candidate_heads:
        compact_elements = [
            el for el in compact_elements
            if str(el.get("handle") or el.get("id") or "") in candidate_heads
            or el.get("fire_category") in {"sprinkler", "pipe"}
        ]

    # 전체 pairwise 배열은 크고, 서로 다른 구역의 원거리 쌍을 포함하므로 LLM 근거에서 제외한다.
    # sprinkler만 flat alias(nearest_sprinkler_distances)를 추가로 주입하는 이유:
    # compliance.py 시스템 프롬프트가 fire_topology.sprinkler.nearest_distances(중첩)와
    # 함께 flat 키도 참고할 수 있도록 구형 호환성을 유지하는 것.
    # extinguisher 등 다른 설비는 중첩 구조(fire_topology.extinguisher.*)만 사용한다.
    _spr = fire_topology.get("sprinkler") or {}
    fire_topology["sprinkler_spacings"] = []
    fire_topology["spacing_violation_candidates"] = list(spacing_candidates or [])[:20]
    fire_topology["nearest_sprinkler_distances"] = list(
        _spr.get("nearest_distances") or []
    )[:80]

    compact = {
        "elements": compact_elements[:300],
        "fire_topology": fire_topology,
    }
    for key in ("error", "metadata"):
        if key in parsed:
            compact[key] = parsed[key]
    return compact


class FireWorkflowHandler:
    def __init__(self, session, db: AsyncSession):
        """
        session : 채팅 세션 컨텍스트 (org_id, current_drawing_id, raw_layout_data 등 포함)
        db      : AsyncSession (vector_service 검색용)
        """
        self.session = session
        self.db = db

        self.query_agent      = QueryAgent(db)
        self.parser_agent     = ParserAgent()
        self.compliance_agent = ComplianceAgent()
        self.report_agent     = ReportAgent()
        self.revision_agent   = RevisionAgent()
        self.action_agent     = ActionAgent()
        self.rag_engine       = FireRagEngine(db)
        _rule_slots = load_rule_slots()
        self.signal_extractor    = SignalExtractor()
        self.candidate_generator = CandidateGenerator(rule_slots=_rule_slots)
        self.query_builder       = QueryBuilder()

    async def handle_tool_calls(
        self, tool_calls: list, context: Dict[str, Any]
    ) -> list:
        final_actions = []

        for call in tool_calls:
            func_name = call["function"]["name"]
            raw_args  = call["function"].get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            # ── call_query_agent ──────────────────────────────────────────
            if func_name == "call_query_agent":
                result = await self.query_agent.execute(
                    args.get("query", ""),
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    domain="fire",
                )
                logging.info(
                    "[FireDebug] call_query_agent chunks=%s",
                    len(result) if isinstance(result, list) else 0,
                )
                final_actions.append({"agent": "query", "result": result})

            # ── call_review_agent ─────────────────────────────────────────
            elif func_name == "call_review_agent":
                target_id = args.get("target_id") or args.get("review_context") or "ALL"
                raw_layout = context.get("raw_layout_data", "{}")

                # 1. 도면 파싱
                parsed = self.parser_agent.parse(
                    raw_layout,
                    mapping_table=context.get("mapping_table"),
                )
                elements = parsed.get("elements", [])
                fire_topology = parsed.get("fire_topology") or {}
                extinguisher_elements = [
                    el for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "extinguisher"
                ]
                ex_block_samples = [
                    {
                        "id": el.get("id"),
                        "handle": el.get("handle"),
                        "block_name": el.get("block_name"),
                        "layer": el.get("layer"),
                        "category": el.get("fire_category"),
                        "role": el.get("fire_object_role"),
                    }
                    for el in elements
                    if isinstance(el, dict)
                    and str(el.get("block_name") or "").upper().startswith("EX")
                ][:8]
                logging.debug(
                    "[FireExtDebug] extinguishers=%d coverage_gaps=%d EX_samples=%s",
                    len(extinguisher_elements),
                    len(((fire_topology.get("extinguisher") or {}).get("coverage_gaps") or [])),
                    ex_block_samples,
                )
                # 2a. Signal-to-Candidate 파이프라인 (Stage 1~3)
                signals    = self.signal_extractor.extract(parsed)
                candidates = self.candidate_generator.generate(signals)
                candidate_queries = self.query_builder.build_queries(candidates)
                _signal_counts: dict[str, int] = {}
                for _s in signals:
                    _signal_counts[_s.signal_type] = _signal_counts.get(_s.signal_type, 0) + 1
                _candidate_counts: dict[str, int] = {}
                for _c in candidates:
                    _candidate_counts[_c.candidate_type] = _candidate_counts.get(_c.candidate_type, 0) + 1
                _category_counts: dict[str, int] = {}
                for _el in elements:
                    if isinstance(_el, dict):
                        _cat = str(_el.get("fire_category") or "unknown")
                        _category_counts[_cat] = _category_counts.get(_cat, 0) + 1
                _candidate_category_counts: dict[str, int] = {}
                for _c in candidates:
                    _cat = str(_c.equipment_category or "unknown")
                    _candidate_category_counts[_cat] = _candidate_category_counts.get(_cat, 0) + 1
                _detector_samples = [
                    {
                        "id": el.get("id"),
                        "handle": el.get("handle"),
                        "block_name": el.get("block_name"),
                        "layer": el.get("layer"),
                        "role": el.get("fire_object_role"),
                    }
                    for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "detector"
                ][:6]
                _sprinkler_samples = [
                    {
                        "id": el.get("id"),
                        "handle": el.get("handle"),
                        "block_name": el.get("block_name"),
                        "layer": el.get("layer"),
                        "raw_type": el.get("raw_type"),
                        "role": el.get("fire_object_role"),
                    }
                    for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "sprinkler"
                ][:6]
                _ex_samples = [
                    {
                        "id": el.get("id"),
                        "handle": el.get("handle"),
                        "block_name": el.get("block_name"),
                        "layer": el.get("layer"),
                        "category": el.get("fire_category"),
                        "role": el.get("fire_object_role"),
                    }
                    for el in elements
                    if isinstance(el, dict)
                    and str(el.get("block_name") or "").upper().startswith("EX")
                ][:6]
                logging.debug(
                    "[FireReviewDebug] elements=%d extinguishers=%d coverage_gaps=%d "
                    "categories=%s signals=%s candidates=%s candidate_categories=%s queries=%d",
                    len(elements),
                    len(extinguisher_elements),
                    len(((fire_topology.get("extinguisher") or {}).get("coverage_gaps") or [])),
                    _category_counts, _signal_counts, _candidate_counts,
                    _candidate_category_counts, len(candidate_queries),
                )
                logging.debug(
                    "[FireReviewSamples] detectors=%s sprinklers=%s EX=%s",
                    _detector_samples, _sprinkler_samples, _ex_samples,
                )

                logging.info(
                    "[FireWorkflow] signals=%d candidates=%d candidate_queries=%d",
                    len(signals), len(candidates), len(candidate_queries),
                )

                if not elements:
                    logging.warning(
                        "[FireWorkflowHandler] 파싱된 요소 없음 — "
                        "drawing_data에 'entities' 키가 있는지 확인하세요."
                    )

                # 2. target 범위 결정
                active_ids = [str(x) for x in (context.get("active_object_ids") or []) if x]
                effective_target = target_id
                if target_id == "ALL":
                    if len(active_ids) == 1:
                        effective_target = active_ids[0]
                    else:
                        effective_target = "ALL"

                _spr_topo = fire_topology.get("sprinkler") or {}
                nearest_spacings   = _spr_topo.get("nearest_distances") or []
                spacing_candidates = (
                    (_spr_topo.get("violation_candidates") or []) +
                    ((fire_topology.get("detector") or {}).get("violation_candidates") or []) +
                    ((fire_topology.get("hydrant") or {}).get("violation_candidates") or [])
                )
                sprinkler_count = len([
                    el for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "sprinkler"
                ])

                # 3. 소방 법규 RAG 검색 (소방 전용 멀티쿼리 엔진)
                review_scope = "선택 영역" if active_ids else "전체 도면"
                if candidate_queries:
                    # 후보 기반 멀티쿼리: FIRE_SUB_QUERIES 고정 8개 대신 후보 타입 쿼리만 사용
                    query_text = candidate_queries[0]
                    rag_results = await self.rag_engine.retrieve(
                        query_text,
                        spec_guid=context.get("spec_guid"),
                        org_id=context.get("org_id"),
                        extra_queries=candidate_queries[1:] or None,
                        final_limit=8,
                        include_default_queries=False,
                    )
                else:
                    # 후보 없음 → 기존 폴백 쿼리
                    query_text = (
                        f"{review_scope} 소방 설비 도면 법규 검토 기준"
                        if effective_target == "ALL"
                        else f"{effective_target} 소방 설비 기준"
                    )
                    rag_results = await self.rag_engine.retrieve(
                        query_text,
                        spec_guid=context.get("spec_guid"),
                        org_id=context.get("org_id"),
                        final_limit=8,
                        include_default_queries=True,
                    )
                rag_preview = ""
                if rag_results:
                    first = rag_results[0] if isinstance(rag_results[0], dict) else {}
                    rag_preview = str(first.get("content") or "")[:300].replace("\n", " ")
                logging.debug(
                    "[FireDebug] target=%s query=%s org_id=%s spec_guid=%s "
                    "elements=%d sprinkler=%d unique_sprinkler=%s nearest=%d candidates=%d "
                    "max_nearest=%s "
                    "rag_count=%d rag_first=%s",
                    effective_target,
                    query_text,
                    context.get("org_id"),
                    context.get("spec_guid"),
                    len(elements),
                    sprinkler_count,
                    _spr_topo.get("unique_count"),
                    len(nearest_spacings),
                    len(spacing_candidates),
                    max((_x.get("distance_mm") or 0 for _x in nearest_spacings), default=None),
                    len(rag_results or []),
                    rag_preview,
                )
                sprinkler_head_sample = [
                    {
                        "handle": el.get("handle") or el.get("id"),
                        "layer": el.get("layer"),
                        "type": el.get("type"),
                        "position": el.get("position"),
                    }
                    for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "sprinkler"
                ][:10]
                logging.debug(
                    "[FireDebug] sprinkler_heads_sample=%s nearest_top10=%s",
                    sprinkler_head_sample,
                    nearest_spacings[:10],
                )
                spec_context = "\n".join(
                    r["content"] for r in rag_results if r.get("content")
                )
                logging.debug(
                    "[FireReviewDebug] rag_results=%d spec_context_chars=%d",
                    len(rag_results or []), len(spec_context or ""),
                )
                if not spec_context:
                    logging.warning(
                        "[FireWorkflowHandler] spec_context RAG 결과 없음 "
                        "(target_id=%s). compliance 검증을 건너뜁니다.",
                        effective_target,
                    )

                # 4. 규정 검증 (타임아웃 보호)
                violations = []
                hard_spacing   = [c for c in candidates if c.numeric_violation and c.candidate_type != "coverage"]
                coverage_cands = [c for c in candidates if c.candidate_type == "coverage"]
                if spec_context:
                    try:
                        compliance_parsed = _build_compact_review_payload(
                            parsed,
                            spacing_candidates,
                        )
                        # numeric_violation=True 후보에 대해 재판단 금지 힌트 생성
                        candidates_hint = ""
                        if hard_spacing or coverage_cands:
                            lines = ["\n\n[코드 사전 계산 결과 — 재판단 금지]"]
                            for c in hard_spacing:
                                ev = c.evidence
                                threshold_str = (
                                    f"{c.applied_threshold}mm"
                                    if c.applied_threshold is not None
                                    else "기준 미확인"
                                )
                                lines.append(
                                    f"  {c.candidate_id}: 실측값 {ev.observed_value}mm "
                                    f"— 기준 {threshold_str} 초과 위반 확정. "
                                    "NFSC 조항 인용 및 reason 작성만 하십시오."
                                )
                            if coverage_cands:
                                lines.append("\n[소화기 커버리지 공백 — 수치 확정, 재판단 금지]")
                                lines.append("coverage_error 후보는 코드가 계산한 수치 확정 결과입니다.")
                                lines.append("violation_type을 coverage_error로 고정하고 아래 필드를 그대로 출력에 유지하십시오:")
                                lines.append("  x, y, nearest_extinguisher_id, fire_category, distance_mm, threshold_mm")
                                for c in coverage_cands:
                                    ev   = c.evidence
                                    ctx  = ev.context or {}
                                    thr  = c.applied_threshold
                                    lines.append(
                                        f"  {c.candidate_id}: "
                                        f"equipment_id={ev.equipment_id}, "
                                        f"x={ctx.get('x')}, y={ctx.get('y')}, "
                                        f"nearest_extinguisher_id={ctx.get('nearest_extinguisher_id')}, "
                                        f"fire_category=extinguisher, "
                                        f"distance_mm={ev.observed_value}, threshold_mm={thr}. "
                                        "NFSC 조항 인용 및 reason 작성만 하십시오."
                                    )
                            candidates_hint = "\n".join(lines)
                        logging.debug(
                            "[FireDebug] compact_payload elements=%d nearest=%d candidates=%d hard=%d coverage=%d",
                            len(compliance_parsed.get("elements") or []),
                            len((compliance_parsed.get("fire_topology") or {}).get("nearest_sprinkler_distances") or []),
                            len((compliance_parsed.get("fire_topology") or {}).get("spacing_violation_candidates") or []),
                            len(hard_spacing),
                            len(coverage_cands),
                        )
                        violations = await asyncio.wait_for(
                            self.compliance_agent.check_compliance_parsed(
                                effective_target, spec_context, compliance_parsed,
                                candidates_hint=candidates_hint,
                            ),
                            timeout=120.0,
                        )
                        # coverage_error candidate → violation 필드 보장 merge
                        _cov_map = {
                            ("coverage_error", c.evidence.equipment_id): c
                            for c in candidates
                            if c.candidate_type == "coverage"
                        }
                        if _cov_map:
                            _CFIELDS = {
                                "fire_category", "x", "y", "nearest_extinguisher_id",
                                "distance_mm", "threshold_mm", "current_value", "required_value",
                            }
                            def _merge_cov(v: dict) -> dict:
                                if v.get("violation_type") != "coverage_error":
                                    return v
                                c = _cov_map.get(("coverage_error", v.get("equipment_id")))
                                if c is None:
                                    return v
                                ev, ctx = c.evidence, c.evidence.context or {}
                                fb = {
                                    "fire_category":           "extinguisher",
                                    "x":                       ctx.get("x"),
                                    "y":                       ctx.get("y"),
                                    "nearest_extinguisher_id": ctx.get("nearest_extinguisher_id", ""),
                                    "distance_mm":             ev.observed_value,
                                    "threshold_mm":            c.applied_threshold,
                                    "current_value": (
                                        f"{float(ev.observed_value):.0f}mm"
                                        if ev.observed_value is not None else ""
                                    ),
                                    "required_value": (
                                        f"{c.applied_threshold:.0f}mm 이하"
                                        if c.applied_threshold is not None else ""
                                    ),
                                }
                                for field in _CFIELDS:
                                    if (field not in v or v.get(field) in (None, "")) and fb.get(field) is not None:
                                        v[field] = fb[field]
                                return v
                            violations = [_merge_cov(v) for v in violations]
                        llm_violation_count = len(violations or [])
                        violations = _ensure_hard_candidate_violations(
                            violations,
                            hard_spacing + coverage_cands,
                        )
                        violations = _filter_spacing_violations(violations, hard_spacing)
                        logging.debug(
                            "[FireReviewDebug] llm_violations=%d final_violations=%d "
                            "hard_spacing=%d coverage=%d",
                            llm_violation_count, len(violations or []),
                            len(hard_spacing), len(coverage_cands),
                        )
                    except asyncio.TimeoutError:
                        logging.error(
                            "[FireWorkflowHandler] compliance 검증 타임아웃(120s) — 빈 violations 반환"
                        )
                        violations = _ensure_hard_candidate_violations(
                            [],
                            hard_spacing + coverage_cands,
                        )
                        violations = _filter_spacing_violations(violations, hard_spacing)

                # 4-1. 낙서/노이즈 자동 위반 추가는 비활성화.
                # TEXT/MTEXT/HATCH는 일반 도면 주석·해치로 정상 사용되는 경우가 많아
                # 소방 법규 검토 결과에 포함하면 오탐이 과도하게 발생한다.

                # 5. 리포트 생성
                if not spec_context:
                    violations = _ensure_hard_candidate_violations(
                        [],
                        hard_spacing + coverage_cands,
                    )
                    violations = _filter_spacing_violations(violations, hard_spacing)

                report = self.report_agent.generate(
                    violations, drawing_id=context.get("current_drawing_id", "")
                )

                # 6. 수정 대안 계산
                current_layout = {
                    (el.get("id") or ""): el.get("position", {})
                    for el in elements
                }
                neighbor_map = {
                    str(c.get("head") or ""): str(c.get("nearest_head") or "")
                    for c in spacing_candidates
                    if c.get("head") and c.get("nearest_head")
                }
                for v in violations or []:
                    if not isinstance(v, dict):
                        continue
                    if v.get("violation_type") != "spacing_error":
                        continue
                    _target = str(v.get("equipment_id") or "")
                    _nearest = str(v.get("nearest_head") or "")
                    if _target and _nearest:
                        neighbor_map.setdefault(_target, _nearest)
                element_meta = {
                    (el.get("id") or ""): {
                        "block_name": el.get("block_name") or "",
                        "layer": el.get("layer") or "",
                        "fire_category": el.get("fire_category") or "",
                    }
                    for el in elements
                }
                fixes = self.revision_agent.calculate_fix(
                    violations, current_layout,
                    neighbor_map=neighbor_map,
                    element_meta=element_meta,
                )

                logging.info(
                    "[FireDebug] call_review_agent report_items=%s fixes=%s",
                    len((report or {}).get("items") or []),
                    len(fixes or []),
                )
                final_actions.append({
                    "agent": "review",
                    "result": {
                        "report": report,
                        "fixes": fixes,
                        "rag_references": rag_results,
                        # KPI(recall_lower_bound) 자리 — fire deterministic_checker 도입 시 채움.
                        "deterministic_violations": [],
                    },
                })

            # ── call_action_agent ─────────────────────────────────────────
            elif func_name == "call_action_agent":
                result = await self.action_agent.analyze_and_fix(context, domain="fire")
                fixes = (result or {}).get("fixes") or []
                logging.info(
                    "[FireDebug] call_action_agent fixes=%d sample_handles=%s",
                    len(fixes),
                    [f.get("handle") for f in fixes[:5]],
                )
                final_actions.append({"agent": "action", "result": result})

            # ── get_cad_entity_info ───────────────────────────────────────
            elif func_name == "get_cad_entity_info":
                from backend.services.agents.common.tools.common_tools import get_cad_entity_info_tool
                import json as _json
                handle = args.get("handle", "")
                raw_layout = context.get("raw_layout_data", "{}")
                if isinstance(raw_layout, dict):
                    drawing_data_str = _json.dumps(raw_layout, ensure_ascii=False)
                else:
                    drawing_data_str = raw_layout or "{}"
                result_str = get_cad_entity_info_tool.invoke({"handle": handle, "drawing_data": drawing_data_str})
                final_actions.append({"agent": "cad_info", "result": result_str})

        return final_actions
