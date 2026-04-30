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


# ── 낙서/노이즈 엔티티 감지 ──────────────────────────────────────────────────
# LINE/CIRCLE 등 기하 도형은 제목 란·테두리 등 정상 도면 요소와 구분 불가 — 제외
# 타입 자체로 비설비임이 확실한 것만 잡는다
_NOISE_RAW_TYPES: frozenset[str] = frozenset({"DIMENSION", "HATCH", "VIEWPORT"})
_TEXT_TYPES: frozenset[str] = frozenset({"MTEXT", "TEXT", "ATTDEF"})
_SPRINKLER_RULE_KEYWORDS: tuple[str, ...] = (
    "스프링클러", "헤드", "살수", "방호반경", "간격", "2.3"
)


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
    """
    LLM이 누락하거나 rate limit으로 실패해도, Parser가 계산한 최근접 헤드 간격 후보를
    RAG 근거가 있는 경우에만 spacing_error로 보강한다.
    """
    if not candidates or not rag_results:
        return []

    rule_text = ""
    for result in rag_results:
        content = str((result or {}).get("content") or "")
        if any(k in content for k in _SPRINKLER_RULE_KEYWORDS):
            rule_text = content[:500].replace("\n", " ")
            break
    if not rule_text:
        return []

    existing_keys = {
        (str(v.get("equipment_id") or ""), str(v.get("violation_type") or ""))
        for v in existing_violations or []
        if isinstance(v, dict)
    }

    out: list[dict] = []
    for item in sorted(candidates, key=lambda x: x.get("distance_mm") or 0, reverse=True):
        head = str(item.get("head") or "")
        if not head or (head, "spacing_error") in existing_keys:
            continue
        dist = float(item.get("distance_mm") or 0)
        limit_mm = float(item.get("limit_mm") or 2300.0)
        nearest = str(item.get("nearest_head") or "")
        out.append({
            "equipment_id": head,
            "handle": head,
            "violation_type": "spacing_error",
            "reference_rule": rule_text,
            "current_value": f"최근접 헤드({nearest})와의 거리 {dist / 1000:.2f}m",
            "required_value": f"{limit_mm / 1000:.1f}m 이하",
            "reason": (
                "가장 가까운 스프링클러 헤드와의 거리가 기준을 초과한 후보입니다. "
                "전체 pairwise 최대거리가 아니라 최근접 거리 기준으로 판정했습니다."
            ),
            "severity": "CRITICAL",
        })
        if len(out) >= limit:
            break
    return out


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
        if category in {"sprinkler", "detector", "hydrant", "pump", "alarm", "panel", "pipe"} or handle in candidate_heads:
            compact_elements.append(el)

    if candidate_heads:
        compact_elements = [
            el for el in compact_elements
            if str(el.get("handle") or el.get("id") or "") in candidate_heads
            or el.get("fire_category") in {"sprinkler", "pipe"}
        ]

    # 전체 pairwise 배열은 크고, 서로 다른 구역의 원거리 쌍을 포함하므로 LLM 근거에서 제외한다.
    fire_topology["sprinkler_spacings"] = []
    fire_topology["spacing_violation_candidates"] = list(spacing_candidates or [])[:20]
    fire_topology["nearest_sprinkler_distances"] = list(
        fire_topology.get("nearest_sprinkler_distances") or []
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

                fire_topology = parsed.get("fire_topology") or {}
                sprinkler_spacings = fire_topology.get("sprinkler_spacings") or []
                nearest_spacings = fire_topology.get("nearest_sprinkler_distances") or []
                spacing_candidates = fire_topology.get("spacing_violation_candidates") or []
                sprinkler_count = len([
                    el for el in elements
                    if isinstance(el, dict) and el.get("fire_category") == "sprinkler"
                ])

                # 3. 소방 법규 RAG 검색 (소방 전용 멀티쿼리 엔진)
                review_scope = "선택 영역" if active_ids else "전체 도면"
                if spacing_candidates or sprinkler_count >= 2:
                    query_text = (
                        "스프링클러 헤드 간격 헤드와 헤드 사이 거리 "
                        "2.3m 이하 살수반경 방호반경 NFSC"
                    )
                else:
                    query_text = (
                        f"{review_scope} 소방 설비 도면 법규 검토 기준"
                        if effective_target == "ALL"
                        else f"{effective_target} 소방 설비 기준"
                    )
                rag_results = await self.rag_engine.retrieve(
                    query_text,
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    extra_queries=[
                        "스프링클러 헤드 간격 헤드와 헤드 사이 거리 기준 NFSC",
                        "스프링클러 살수반경 방호반경 헤드 배치 기준",
                    ] if sprinkler_count >= 2 else None,
                    final_limit=8,
                )
                if sprinkler_count >= 2:
                    sprinkler_keywords = ("스프링클러", "헤드", "살수", "방호반경", "간격", "2.3")
                    preferred = [
                        r for r in rag_results
                        if any(k in str((r or {}).get("content") or "") for k in sprinkler_keywords)
                    ]
                    if preferred:
                        rag_results = preferred[:3]
                rag_preview = ""
                if rag_results:
                    first = rag_results[0] if isinstance(rag_results[0], dict) else {}
                    rag_preview = str(first.get("content") or "")[:300].replace("\n", " ")
                logging.warning(
                    "[FireDebug] target=%s query=%s org_id=%s spec_guid=%s "
                    "elements=%d sprinkler=%d unique_sprinkler=%s spacings=%d nearest=%d candidates=%d "
                    "max_spacing=%s max_nearest=%s "
                    "rag_count=%d rag_first=%s",
                    effective_target,
                    query_text,
                    context.get("org_id"),
                    context.get("spec_guid"),
                    len(elements),
                    sprinkler_count,
                    fire_topology.get("unique_sprinkler_count"),
                    len(sprinkler_spacings),
                    len(nearest_spacings),
                    len(spacing_candidates),
                    fire_topology.get("max_spacing_mm"),
                    fire_topology.get("max_nearest_spacing_mm"),
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
                logging.warning(
                    "[FireDebug] sprinkler_heads_sample=%s nearest_top10=%s",
                    sprinkler_head_sample,
                    nearest_spacings[:10],
                )
                spec_context = "\n".join(
                    r["content"] for r in rag_results if r.get("content")
                )
                if not spec_context:
                    logging.warning(
                        "[FireWorkflowHandler] spec_context RAG 결과 없음 "
                        "(target_id=%s). compliance 검증을 건너뜁니다.",
                        effective_target,
                    )

                # 4. 규정 검증 (타임아웃 보호)
                violations = []
                if spec_context:
                    try:
                        compliance_parsed = _build_compact_review_payload(
                            parsed,
                            spacing_candidates,
                        )
                        logging.warning(
                            "[FireDebug] compact_payload elements=%d nearest=%d candidates=%d",
                            len(compliance_parsed.get("elements") or []),
                            len((compliance_parsed.get("fire_topology") or {}).get("nearest_sprinkler_distances") or []),
                            len((compliance_parsed.get("fire_topology") or {}).get("spacing_violation_candidates") or []),
                        )
                        violations = await asyncio.wait_for(
                            self.compliance_agent.check_compliance_parsed(
                                effective_target, spec_context, compliance_parsed
                            ),
                            timeout=120.0,
                        )
                    except asyncio.TimeoutError:
                        logging.error(
                            "[FireWorkflowHandler] compliance 검증 타임아웃(120s) — 빈 violations 반환"
                        )
                        violations = []

                spacing_fallbacks = _build_spacing_candidate_violations(
                    spacing_candidates,
                    rag_results,
                    violations,
                )
                if spacing_fallbacks:
                    logging.warning(
                        "[FireWorkflowHandler] 스프링클러 최근접 간격 후보 %d건을 규칙 기반으로 보강",
                        len(spacing_fallbacks),
                    )
                    violations = violations + spacing_fallbacks

                # 4-1. 낙서/노이즈 자동 위반 추가는 비활성화.
                # TEXT/MTEXT/HATCH는 일반 도면 주석·해치로 정상 사용되는 경우가 많아
                # 소방 법규 검토 결과에 포함하면 오탐이 과도하게 발생한다.

                # 5. 리포트 생성
                report = self.report_agent.generate(
                    violations, drawing_id=context.get("current_drawing_id", "")
                )

                # 6. 수정 대안 계산
                current_layout = {
                    el["id"]: el.get("position", {})
                    for el in elements
                }
                fixes = self.revision_agent.calculate_fix(violations, current_layout)

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
