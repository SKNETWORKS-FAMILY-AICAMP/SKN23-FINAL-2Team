"""
File    : backend/services/agents/arch/workflow_handler.py
Author  : 김다빈
Create  : 2026-04-15
Modified: 2026-04-24 (Phase 7 — topology/geometry 주입, AsyncSession 전환, BaseWorkflowHandler 상속)
         2026-04-25 (Phase 8 — parse 후 4-way 병렬화: topology+geometry(to_thread) + RAG+dict청크(async) 동시 실행)

Description : ARCH_SUB_AGENT_TOOLS 기반 툴 호출 처리 및 서브 에이전트 연동.

    call_review_agent 처리 흐름:
        1. ArchParserAgent.parse()          — elements 추출 (동기, 직렬)
        2. 4-way asyncio.gather             — 가장 비용이 큰 4단계를 동시 실행:
             a. asyncio.to_thread(topology_builder.build)  — CPU-bound Union-Find
             b. asyncio.to_thread(geometry_proc.process)   — CPU-bound 기하 계산
             c. query_agent.execute(rag_query, focus_area) — I/O-bound RAG
             d. _safe_get_dict_chunks()                    — I/O-bound DB 조회
        3. _build_compliance_ctx()          — 경량 컨텍스트 조립 (_slim_el로 40~60% 절감)
        4. ComplianceAgent.check_compliance() — LLM 위반 검증 (120s timeout)
        5. ReportAgent / RevisionAgent      — 리포트 + 수정안 생성

    모듈 수준 헬퍼:
        _slim_el()             : element dict에서 bbox·좌표 등 대용량 필드 제거, 핵심 측정값만 유지.
        _build_compliance_ctx(): drawing_ctx + topology + geometry → compliance 전용 경량 dict 조립.
        _safe_get_dict_chunks(): dictionary 카테고리 청크 조회, 실패 시 빈 리스트 반환.

    사전 청크 주입:
        document_chunks.category="dictionary" 청크를 spec_context 앞에 주입합니다.
        비표준 레이어/블록명도 ComplianceAgent가 올바르게 해석합니다.

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현
    - 2026-04-24 (김다빈) : Phase 7 — ArchTopologyBuilder, ArchGeometryPreprocessor 주입.
                            AsyncSession 전환, BaseWorkflowHandler 상속.
    - 2026-04-25 (김다빈) : Phase 8 — parse 완료 후 4-way asyncio.gather 병렬화.
                            _slim_el() + _build_compliance_ctx() 도입으로
                            compliance_ctx JSON 크기 40~60% 절감.
                            focus_area를 query_agent.execute()에 전달하여 RAG 서브쿼리 필터링 연동.
"""

import asyncio
import json
import logging
import re as _re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.agents.common.base_workflow_handler import BaseWorkflowHandler
from backend.services.agents.arch.sub.query import QueryAgent
from backend.services.agents.arch.sub.review.parser import ArchParserAgent
from backend.services.agents.arch.sub.review.compliance import ComplianceAgent
from backend.services.agents.arch.sub.review.report import ReportAgent
from backend.services.agents.arch.sub.review.revision import RevisionAgent
from backend.services.agents.arch.sub.action import ActionAgent
from backend.services.agents.arch.sub.topology import ArchTopologyBuilder
from backend.services.agents.arch.sub.geometry import ArchGeometryPreprocessor
from backend.services import vector_service

# ── 도메인별 RAG 쿼리 ─────────────────────────────────────────────────────────
_DOMAIN_RAG: dict[str, str] = {
    "FIRE_ZONE": "방화구획 면적 기준 건축법 시행령 46조",
    "CORRIDOR":  "복도 유효폭 건축물 피난방화구조 기준",
    "STAIR":     "계단 유효폭 단높이 단너비 기준 건축법",
    "OPENING":   "방화문 개구부 기준 건축법",
}

_FIRE_RE  = _re.compile(r"방화|FIRE.ZONE|방화구획|FP-\d", _re.IGNORECASE)
_CORR_RE  = _re.compile(r"복도|CORR|HALL|CORRIDOR", _re.IGNORECASE)
_STAIR_RE = _re.compile(r"계단|STAIR|STR-", _re.IGNORECASE)
_OPEN_RE  = _re.compile(r"DOOR|WIN|문|창|개구", _re.IGNORECASE)


def _build_rag_query(elements: list[dict]) -> str:
    """레이어·타입 패턴으로 건축 도메인 추론 → RAG 쿼리 반환"""
    layers = " ".join(str(e.get("layer", "")) for e in elements[:80])
    types  = " ".join(str(e.get("type", "")) for e in elements[:80])
    text   = layers + " " + types
    if _FIRE_RE.search(text):
        return _DOMAIN_RAG["FIRE_ZONE"]
    if _CORR_RE.search(text):
        return _DOMAIN_RAG["CORRIDOR"]
    if _STAIR_RE.search(text):
        return _DOMAIN_RAG["STAIR"]
    if _OPEN_RE.search(text):
        return _DOMAIN_RAG["OPENING"]
    return _DOMAIN_RAG["FIRE_ZONE"]  # 기본 건축


class ArchWorkflowHandler(BaseWorkflowHandler):
    def __init__(self, session: Any, db: AsyncSession):
        super().__init__(session, db)
        self.query_agent      = QueryAgent(db)
        self.parser_agent     = ArchParserAgent()
        self.compliance_agent = ComplianceAgent()
        self.report_agent     = ReportAgent()
        self.revision_agent   = RevisionAgent()
        self.action_agent     = ActionAgent()
        self.topology_builder = ArchTopologyBuilder()
        self.geometry_proc    = ArchGeometryPreprocessor()

    async def _dispatch_tool(
        self, func_name: str, args: dict, context: dict
    ) -> dict | None:

        # ── call_query_agent ──────────────────────────────────────────────────
        if func_name == "call_query_agent":
            result = await self.query_agent.execute(
                args.get("query", ""),
                spec_guid=context.get("spec_guid"),
                org_id=context.get("org_id"),
                domain="arch",
            )
            return {"agent": "query", "result": result}

        # ── call_review_agent ─────────────────────────────────────────────────
        if func_name == "call_review_agent":
            t0 = time.time()
            focus_area   = args.get("focus_area", "")
            drawing_data = context.get("drawing_data") or context.get("raw_layout_data") or {}

            # 1. 파싱 (elements 추출 — 후속 병렬 작업의 공통 입력)
            drawing_ctx = self.parser_agent.parse(drawing_data)
            elements    = drawing_ctx.get("elements", [])
            t_parse     = time.time() - t0

            # 2. rag_query 확정 (parse 완료 시점에 바로 계산 가능)
            rag_query = (
                f"건축법 {focus_area} 기준" if focus_area
                else _build_rag_query(elements)
            )

            # 3. 4-way 병렬 실행
            #    - topology, geometry : CPU-bound sync → asyncio.to_thread 로 스레드풀 실행
            #    - RAG, dict청크      : I/O-bound async → 직접 코루틴
            t_parallel_start = time.time()
            (topology, geo), rag_results, dict_chunks = await asyncio.gather(
                asyncio.gather(
                    asyncio.to_thread(self.topology_builder.build, elements),
                    asyncio.to_thread(self.geometry_proc.process, elements),
                ),
                self.query_agent.execute(
                    rag_query,
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    domain="arch",
                    limit=5,
                    focus_area=focus_area,
                ),
                self._safe_get_dict_chunks(),
            )
            t_parallel = time.time() - t_parallel_start

            logging.info(
                "[ArchWorkflow] 4-way parallel done: topology spaces=%d fire_zones=%d | "
                "rag=%d chunks | dict=%d chunks (%.2fs)",
                topology.get("summary", {}).get("space_count", 0),
                topology.get("summary", {}).get("fire_zone_count", 0),
                len(rag_results),
                len(dict_chunks),
                t_parallel,
            )

            # 4. compliance 전용 경량 컨텍스트 구성
            #    elements[]는 walls/spaces/openings 등을 flat하게 합친 중복 리스트.
            #    원본을 그대로 보내면 동일 데이터를 2배 전송 → JSON 크기 초과 → 청크 분할 → LLM N회 호출.
            #    핵심 측정값만 남긴 slim elements + topology/geometry 구조화 결과로 대체.
            compliance_ctx = _build_compliance_ctx(drawing_ctx, topology, geo)
            comp_json_kb = len(json.dumps(compliance_ctx, ensure_ascii=False)) // 1024
            logging.info("[ArchWorkflow] compliance_ctx 경량화: ~%dKB (raw drawing_ctx 대비 40~60%% 감소)", comp_json_kb)

            # 5. spec_context 조합 (사전 청크 선두 주입)
            spec_context = "\n\n".join(r["content"] for r in rag_results if r.get("content"))
            if dict_chunks:
                dict_context = "\n\n".join(c.content for c in dict_chunks)
                spec_context = (
                    "[레이어/블록명 사전]\n" + dict_context
                    + "\n\n---\n\n[건축법 법규]\n" + spec_context
                )
                logging.info("[ArchWorkflow] 사전 청크 %d개 주입", len(dict_chunks))

            if not spec_context:
                logging.warning("[ArchWorkflow] RAG 결과 없음 — compliance 건너뜀")

            # 6. 규정 검증 — 120초 타임아웃
            t_comp_start = time.time()
            try:
                violations = (
                    await asyncio.wait_for(
                        self.compliance_agent.check_compliance(compliance_ctx, spec_context, focus_area),
                        timeout=120.0,
                    )
                    if spec_context else []
                )
            except asyncio.TimeoutError:
                logging.error("[ArchWorkflow] compliance 검증 타임아웃(120s) — 빈 violations 반환")
                violations = []
            t_comp = time.time() - t_comp_start

            # 7. 리포트
            report = self.report_agent.generate(violations, focus_area=focus_area)

            # 8. 수정안
            fixes = self.revision_agent.calculate_fix(violations)

            t_total = time.time() - t0
            logging.info(
                "[ArchTracker] parse=%.2fs parallel(topo+geo+rag+dict)=%.2fs comp=%.2fs total=%.2fs",
                t_parse, t_parallel, t_comp, t_total,
            )
            return {
                "agent": "review",
                "result": {"report": report, "fixes": fixes, "rag_references": rag_results},
            }

        # ── call_action_agent ─────────────────────────────────────────────────
        if func_name == "call_action_agent":
            raw = args.get("modifications")
            modifications = []
            if raw:
                try:
                    modifications = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    pass
            else:
                modifications = list(context.get("pending_fixes") or [])
            result = self.action_agent.generate_command(modifications)
            return {"agent": "action", "result": result}

        return None

    async def _safe_get_dict_chunks(self) -> list:
        """
        domain="건축" 사전 청크를 조회합니다.

        asyncio.gather 안에서 실행되므로, 예외가 발생해도 빈 리스트를 반환하여
        나머지 3-way(topology, geometry, RAG) 실행을 중단하지 않습니다.
        사전 청크가 없어도 법규 검색 결과만으로 compliance 실행은 계속됩니다.
        """
        try:
            chunks = await vector_service.get_dictionary_chunks(self.db, domain="건축")
            return chunks or []
        except Exception as e:
            logging.warning("[ArchWorkflow] 사전 청크 조회 실패 (계속): %s", e)
            return []


# ── 모듈 수준 헬퍼 ─────────────────────────────────────────────────────────────

def _slim_el(e: dict) -> dict:
    """
    element dict를 compliance 전달용 경량 형식으로 변환합니다.

    제거 필드 (크기 절감):
        bbox, start_point, end_point, insert_point, position, points[] 등
        좌표/기하 원시 데이터 — topology/geometry 단계에서 이미 구조화했으므로 중복.

    유지 필드 (핵심 측정값):
        handle, layer, type, area_mm2, thickness_mm, length_mm, measurement_mm,
        radius_mm, span_deg, block_name, category, is_closed, dim_text, text, arc_length_mm

    효과:
        elements[]를 그대로 전달할 경우 동일 데이터가 flat 합본으로 2배 전송됨.
        slim 버전으로 교체하면 JSON 크기 40~60% 감소 → LLM 청크 분할 횟수 감소.
    """
    slim: dict = {
        "handle": e.get("handle", ""),
        "layer":  e.get("layer", ""),
        "type":   e.get("category") or e.get("type", ""),
    }
    for k in (
        "area_mm2", "thickness_mm", "length_mm", "measurement_mm",
        "radius_mm", "span_deg", "block_name", "category",
        "is_closed", "dim_text", "text", "arc_length_mm",
    ):
        if k in e:
            slim[k] = e[k]
    return slim


def _build_compliance_ctx(drawing_ctx: dict, topology: dict, geo: dict) -> dict:
    """
    compliance 전용 경량 컨텍스트.

    제거 항목 (크기 절감):
        - elements[]     : walls/spaces/openings 등 flat 합본 (중복, 가장 큼) → slim 버전으로 교체
        - unclassified[] : 분류 실패 노이즈
        - columns[]      : 건축법 주요 위반 항목 아님
        - curved_walls[] : 직접 위반 항목 아님

    추가 항목 (정확도 향상):
        - topology_spaces : 공간 면적 + boundary_handles (방화구획 면적 판정에 직접 사용)
        - fire_zones      : 방화구획 그룹 + 누적 면적 (법규 대조 핵심)
        - opening_analysis: 개구부 유효폭 mm (복도폭/문폭 위반 판정)
        - corridor_widths : 복도 구간별 폭 (피난 경로 위반 판정)
        - wall_clearances : 벽체 이격 거리 (구조 간격 위반 판정)
        - ceiling_heights : 층고 추정값 (층고 위반 판정)
    """
    return {
        "drawing_unit":       drawing_ctx.get("drawing_unit"),
        "unit_factor":        drawing_ctx.get("unit_factor"),
        "classified_summary": drawing_ctx.get("classified_summary"),
        # 핵심 측정값만 남긴 경량 elements (splitter에도 사용)
        "elements":           [_slim_el(e) for e in drawing_ctx.get("elements", [])],
        # 직접 handle이 필요한 카테고리 (원본 유지 — 단, walls는 두께+handle만)
        "walls": [
            {"handle": w.get("handle", ""), "layer": w.get("layer", ""),
             "thickness_mm": w.get("thickness_mm"), "length_mm": w.get("length_mm")}
            for w in drawing_ctx.get("walls", [])
        ],
        "spaces":      drawing_ctx.get("spaces", []),
        "openings":    drawing_ctx.get("openings", []),
        "dimensions":  drawing_ctx.get("dimensions", []),
        "annotations": drawing_ctx.get("annotations", []),
        # topology 구조화 결과
        "topology_spaces": topology.get("spaces", []),
        "fire_zones":      topology.get("fire_zones", []),
        # geometry 구조화 결과
        "opening_analysis": geo.get("opening_analysis", []),
        "corridor_widths":  geo.get("corridor_widths", []),
        "wall_clearances":  geo.get("wall_clearances", []),
        "ceiling_heights":  geo.get("ceiling_heights", []),
    }
