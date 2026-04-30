"""
File    : backend/services/agents/piping/workflow_handler.py
Author  : 송주엽
Create  : 2026-04-09
Description : PIPE_SUB_AGENT_TOOLS 스키마 기반 툴 호출 처리 및 서브 에이전트 연동

Modification History :
    - 2026-04-09 (송주엽) : 서브 에이전트 연동 워크플로우 통제 로직 초기 작성
    - 2026-04-14 (송주엽) : 생성자 인자 통일(session, db) 및 async 처리
    - 2026-04-15 (송주엽) : layout_data 폴백, target_id='ALL' 처리
    - 2026-04-19 (김지우) : get_cad_entity_info 툴 연동
    - 2026-04-22 : call_review — ComplianceAgent.check_compliance_parsed
    - 2026-04-29 (송주엽) : #1 DeterministicChecker 통합
                            #3 topology unit_factor 주입, total_length_mm
                            #4 _build_domain_rag_queries 병렬 멀티쿼리
                            #5 confidence 기반 violations 분리 출력
"""

import asyncio
import json
import logging
import re as _re
from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession
# from sqlalchemy.orm import Session (제거)

from backend.services.agents.pipe.sub.query import QueryAgent
from backend.services.agents.pipe.sub.review.parser import ParserAgent
from backend.services.agents.pipe.sub.review.compliance import ComplianceAgent
from backend.services.agents.pipe.sub.review.report import ReportAgent
from backend.services.agents.pipe.sub.review.revision import RevisionAgent
from backend.services.agents.pipe.sub.action import ActionAgent
from backend.services.agents.pipe.sub.topology import PipeTopologyBuilder
from backend.services.agents.pipe.sub.geometry import GeometryPreprocessor
from backend.services.agents.pipe.sub.deterministic_checker import run_deterministic_checks
from backend.services.agents.pipe.sub.drawing_qa_checker import run_drawing_qa_checks
from backend.services.cad_progress import emit_pipeline_step


_GAS_LAYER_RE = _re.compile(r"GAS|가스", _re.IGNORECASE)


def _enrich_with_object_mapping(elements: list[dict], obj_mapping: list[dict]) -> None:
    """object_mapping label(텍스트 주석)로 elements의 diameter_mm·material을 보강.

    object_mapping 구조: [{"block_handle": str, "label": str, ...}, ...]
    - 숫자 label (예: "20") → diameter_mm = 20.0
    - "G" 접두어 또는 가스 레이어 → material = "GAS"
    """
    if not obj_mapping:
        return
    handle_map: dict[str, dict] = {
        m["block_handle"]: m for m in obj_mapping if m.get("block_handle")
    }
    for el in elements:
        layer = str(el.get("layer") or "")
        if el.get("material") == "UNKNOWN" and _GAS_LAYER_RE.search(layer):
            el["material"] = "GAS"

        m = handle_map.get(str(el.get("handle") or ""))
        if not m:
            continue
        label = str(m.get("label") or "")
        if el.get("diameter_mm", 0) == 0:
            nm = _re.search(r"(\d+(?:\.\d+)?)", label)
            if nm:
                el["diameter_mm"] = float(nm.group(1))
        if el.get("material") == "UNKNOWN" and _re.match(r"^G", label, _re.IGNORECASE):
            el["material"] = "GAS"


_DOMAIN_RAG: dict[str, str] = {
    "GAS":   "가스 배관 안전규정 KGS 가스누설차단기 밸브 이격거리 설치위치 기준",
    "WATER": "급수 급탕 배관 이격거리 직경 재질 기준 규정",
    "FIRE":  "소화 스프링클러 배관 이격거리 헤드 간격 기준 규정",
    "HVAC":  "냉난방 공조 덕트 배관 이격거리 설치 기준 규정",
}
_DOMAIN_LAYER_RE = _re.compile(
    r"GAS|가스|FIRE|소화|SPRINK|CWS|HWS|급수|급탕|HVAC|냉난방", _re.IGNORECASE
)

_FIRE_LAYER_WH_RE = _re.compile(r"FIRE|SP[-_]|소화|SPRINK", _re.IGNORECASE)
_WATER_LAYER_WH_RE = _re.compile(r"CWS|HWS|급수|급탕|WATER", _re.IGNORECASE)
_HVAC_LAYER_WH_RE  = _re.compile(r"HVAC|냉난방|공조|DUCT", _re.IGNORECASE)

# drawing_title 키워드 → 도메인 매핑
_TITLE_DOMAIN_MAP: list[tuple[_re.Pattern, str]] = [
    (_re.compile(r"가스|GAS",            _re.IGNORECASE), "GAS"),
    (_re.compile(r"소화|스프링클러|FIRE|SPRINK", _re.IGNORECASE), "FIRE"),
    (_re.compile(r"급수|급탕|WATER|CWS|HWS",   _re.IGNORECASE), "WATER"),
    (_re.compile(r"냉난방|공조|HVAC|덕트|DUCT",  _re.IGNORECASE), "HVAC"),
]


def _domain_from_title(drawing_title: str | None) -> str | None:
    """도면 제목(사용자 입력)에서 도메인 코드 추출."""
    if not drawing_title:
        return None
    for pattern, domain in _TITLE_DOMAIN_MAP:
        if pattern.search(drawing_title):
            return domain
    return None


def _extract_tex_keywords(elements: list[dict]) -> list[str]:
    """TEX 레이어 TEXT 엔티티에서 장비명·규격 키워드 추출 (상위 8개)."""
    seen: set[str] = set()
    keywords: list[str] = []
    for e in elements:
        if str(e.get("layer") or "").upper() != "TEX":
            continue
        text = str(e.get("text") or e.get("content") or "").strip()
        if text and text not in seen:
            seen.add(text)
            keywords.append(text)
            if len(keywords) >= 8:
                break
    return keywords

# confidence 임계값 — violations 분리 기준
_CONFIDENCE_THRESHOLD = 0.7


async def _emit_review_progress(context: dict, stage: str, message: str) -> None:
    """Send sub-step progress for the pipe review workflow when timing context exists."""
    session_id = str(context.get("progress_session_id") or "")
    t0_monotonic = context.get("progress_t0_monotonic")
    wall_start_ts = context.get("progress_wall_start_ts")
    last_t = context.get("progress_last_t")
    if not session_id or t0_monotonic is None or wall_start_ts is None or last_t is None:
        return
    try:
        context["progress_last_t"] = await emit_pipeline_step(
            session_id=session_id,
            stage=stage,
            message=message,
            t0_monotonic=float(t0_monotonic),
            wall_start_ts=float(wall_start_ts),
            last_t=float(last_t),
        )
    except Exception:
        logging.debug("[WorkflowHandler] progress emit skipped", exc_info=True)


def _build_domain_rag_queries(
    elements: list[dict],
    domain_hint: str | None = None,
    drawing_title: str | None = None,
    tex_keywords: list[str] | None = None,
) -> list[str]:
    """멀티쿼리: domain_hint + drawing_title + TEX 키워드 + elements 레이어/재질로 도메인 감지.

    우선순위:
    1. domain_hint (타이틀 블록 자동 추출)
    2. drawing_title (사용자 입력 도면명)
    3. TEX 레이어 장비명·규격 키워드
    4. elements material / layer 패턴
    """
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    # 1순위: domain_hint (타이틀 블록 자동 추출)
    if domain_hint and domain_hint.upper() in _DOMAIN_RAG:
        _add(_DOMAIN_RAG[domain_hint.upper()])

    # 2순위: drawing_title (사용자 입력 도면명 — "1층 가스 배관 평면도" 등)
    title_domain = _domain_from_title(drawing_title)
    if title_domain:
        _add(_DOMAIN_RAG[title_domain])

    # 3순위: TEX 레이어 장비명·규격 → 도메인 특화 쿼리 보강
    if tex_keywords:
        tex_str = " ".join(tex_keywords[:5])
        # TEX 텍스트 자체도 도메인 판별에 활용
        for pattern, domain in _TITLE_DOMAIN_MAP:
            if pattern.search(tex_str):
                _add(_DOMAIN_RAG[domain])
        # TEX 키워드를 쿼리에 직접 포함 (구체적인 장비 규정 검색)
        _add(f"배관 설비 법규 설치기준 규정 {tex_str}")

    # 4순위: elements material / layer 패턴
    materials = {str(e.get("material") or "").upper() for e in elements}
    layers    = [str(e.get("layer") or "") for e in elements]
    layer_str = " ".join(layers)

    if "GAS" in materials or _GAS_LAYER_RE.search(layer_str):
        _add(_DOMAIN_RAG["GAS"])
    if _FIRE_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["FIRE"])
    if _WATER_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["WATER"])
    if _HVAC_LAYER_WH_RE.search(layer_str):
        _add(_DOMAIN_RAG["HVAC"])

    # 감지된 도메인 없음 → 기존 단일 쿼리 폴백
    if not queries:
        _add(_build_rag_query(elements))

    return queries


def _build_rag_query(elements: list[dict]) -> str:
    """elements의 material·layer에서 도메인을 추론하여 특화 RAG 쿼리를 생성한다."""
    materials = {str(e.get("material") or "").upper() for e in elements}
    layers    = {str(e.get("layer") or "") for e in elements}

    for domain, query in _DOMAIN_RAG.items():
        if domain in materials:
            return query
    # 레이어명으로 폴백
    for layer in layers:
        m = _DOMAIN_LAYER_RE.search(layer)
        if m:
            kw = m.group(0).upper()
            for domain, query in _DOMAIN_RAG.items():
                if kw in query.upper() or kw in domain:
                    return query
    # 일반 배관
    type_set = list({e.get("type", "") for e in elements if e.get("type")})[:4]
    return f"배관 설비 이격거리 설치위치 직경 기준 규정: {', '.join(type_set)}"


class PipeWorkflowHandler:
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

    async def handle_tool_calls(
        self, tool_calls: list, context: Dict[str, Any]
    ) -> list:
        import json
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
                    domain="pipe",
                )
                logging.info(
                    "[PipingDebug] call_query_agent chunks=%s",
                    len(result) if isinstance(result, list) else 0,
                )
                final_actions.append({"agent": "query", "result": result})

            # ── call_review_agent ─────────────────────────────────────────
            elif func_name == "call_review_agent":
                target_id = args.get("target_id", "ALL")
                import time
                t_start = time.time()

                # layout_data: LLM 인자 → context fallback (LLM은 spec_context·layout_data를 제공하지 않아도 됨)
                raw_layout = context.get("raw_layout_data", "{}")
                # [DEBUG] 원본 데이터에 존재하는 모든 레이어명 확인
                try:
                    raw_json = json.loads(raw_layout)
                    raw_ents = raw_json.get("entities") or raw_json.get("elements") or []
                    all_raw_layers = {str(e.get("layer") or "0") for e in raw_ents if isinstance(e, dict)}
                    logging.info("[Workflow Raw Debug] ALL layers sent from CAD: %s", sorted(list(all_raw_layers)))
                except Exception as exc:
                    logging.debug("[Workflow Raw Debug] raw layer logging skipped: %s", exc)

                # 1. 도면 파싱 (C# 엔티티 형식 → 정규화)
                t0 = time.time()
                parsed = self.parser_agent.parse(
                    raw_layout,
                    mapping_table=context.get("mapping_table"),
                    layer_resolved_roles=context.get("layer_resolved_roles"),
                )
                elements = parsed.get("elements", [])
                _enrich_with_object_mapping(elements, context.get("object_mapping") or [])
                await _emit_review_progress(
                    context,
                    "pipe_review_parse",
                    f"도면 요소 파싱 완료 — 검토 대상 {len(elements)}개",
                )
                
                # [DEBUG LOG] 검증기로 넘어가기 전 최종 데이터 상태
                final_layers = {e.get("layer") for e in elements}
                logging.info(
                    "[Workflow Debug] Final elements count for checker: %d | Remaining Layers: %s",
                    len(elements), sorted(list(final_layers))
                )
                t_parser = time.time() - t0

                if not elements:
                    logging.warning(
                        "[WorkflowHandler] 파싱된 요소 없음 — "
                        "drawing_data에 'entities' 키가 있는지 확인하세요."
                    )

                # 1b+1c+RAG — topology(CPU)·geometry(CPU)·RAG(I/O) 동시 실행
                effective_target = target_id
                _drawing_data = context.get("drawing_data") or {}
                _raw_unit = _drawing_data.get("unit_to_mm_factor")
                if _raw_unit is None:
                    logging.warning(
                        "[WorkflowHandler] unit_to_mm_factor 없음 — 기본값 1.0 적용 (mm 단위 가정). "
                        "도면 단위가 인치/feet라면 치수 검사 결과가 부정확할 수 있습니다."
                    )
                _unit_factor = float(_raw_unit or 1.0)

                # #4 멀티쿼리: domain_hint + drawing_title + TEX 키워드 통합
                domain_hint    = _drawing_data.get("domain_hint") or None
                drawing_title  = _drawing_data.get("drawing_title") or None
                tex_keywords   = _extract_tex_keywords(elements)
                if tex_keywords:
                    logging.info("[WorkflowHandler] TEX keywords for RAG: %s", tex_keywords)
                rag_queries = _build_domain_rag_queries(
                    elements,
                    domain_hint=domain_hint,
                    drawing_title=drawing_title,
                    tex_keywords=tex_keywords,
                )
                logging.info(
                    "[WorkflowHandler] RAG queries (multi-domain, %d): %s",
                    len(rag_queries), rag_queries,
                )
                await _emit_review_progress(
                    context,
                    "pipe_review_rag_topology",
                    f"Topology/Geometry/RAG 병렬 처리 시작 — RAG 쿼리 {len(rag_queries)}개",
                )

                t0_parallel = time.time()

                # 병렬 실행: topology + geometry + 도메인별 RAG(동시)
                parallel_tasks = [
                    asyncio.to_thread(
                        PipeTopologyBuilder().build, elements, _unit_factor  # #3
                    ),
                    asyncio.to_thread(
                        GeometryPreprocessor(unit_factor=_unit_factor).process, elements
                    ),
                    *[
                        self.query_agent.execute(
                            q,
                            spec_guid=context.get("spec_guid"),
                            org_id=context.get("org_id"),
                            domain="pipe",
                            limit=5,
                        )
                        for q in rag_queries
                    ],
                ]
                parallel_results = await asyncio.gather(*parallel_tasks, return_exceptions=True)

                # topology / geo 실패 시 방어적 기본값
                topology = (
                    parallel_results[0]
                    if not isinstance(parallel_results[0], Exception)
                    else {"pipe_runs": [], "summary": {"run_count": 0, "unconnected_lines": 0, "block_count": 0}}
                )
                if isinstance(parallel_results[0], Exception):
                    logging.error("[WorkflowHandler] topology 빌드 실패: %s", parallel_results[0])

                geo = (
                    parallel_results[1]
                    if not isinstance(parallel_results[1], Exception)
                    else {"mep_clearances": [], "wall_clearances": [], "proxy_walls": []}
                )
                if isinstance(parallel_results[1], Exception):
                    logging.error("[WorkflowHandler] geometry 빌드 실패: %s", parallel_results[1])

                # 멀티 RAG 결과 병합 + 중복 제거 (content 기준) — 개별 실패 무시
                _seen_rag: set[str] = set()
                rag_results: list[dict] = []
                for _rag_batch in parallel_results[2:]:
                    if isinstance(_rag_batch, Exception):
                        logging.warning("[WorkflowHandler] RAG query failed: %s", _rag_batch)
                        continue
                    for _r in (_rag_batch or []):
                        if not isinstance(_r, dict):
                            continue
                        _key = str(_r.get("content") or "")[:120]
                        if _key not in _seen_rag:
                            _seen_rag.add(_key)
                            rag_results.append(_r)
                t_parallel = time.time() - t0_parallel
                await _emit_review_progress(
                    context,
                    "pipe_review_rag_topology",
                    (
                        "Topology/Geometry/RAG 병렬 처리 완료 "
                        f"(runs={topology['summary'].get('run_count', 0)}, "
                        f"unconnected={topology['summary'].get('unconnected_lines', 0)}, "
                        f"RAG={len(rag_results)}건)"
                    ),
                )

                # topology 결과 주입
                parsed["pipe_topology"] = topology
                logging.info(
                    "[WorkflowHandler] topology runs=%d unconnected=%d blocks=%d | parallel=%.2fs",
                    topology["summary"]["run_count"],
                    topology["summary"]["unconnected_lines"],
                    topology["summary"]["block_count"],
                    t_parallel,
                )

                # geometry 결과 주입
                parsed["mep_clearances"]  = geo["mep_clearances"]
                parsed["wall_clearances"] = geo["wall_clearances"]
                if not parsed.get("arch_elements") and geo["proxy_walls"]:
                    parsed["arch_elements"] = geo["proxy_walls"]
                    logging.info(
                        "[WorkflowHandler] arch_elements 없음 → proxy_walls %d개 주입",
                        len(geo["proxy_walls"]),
                    )

                # RAG 결과
                spec_context = "\n".join(
                    r["content"] for r in rag_results if r.get("content")
                )

                if not spec_context:
                    logging.warning(
                        "[WorkflowHandler] spec_context RAG 결과 없음 "
                        "(target_id=%s). compliance 검증을 건너뜁니다.",
                        effective_target,
                    )

                # 4. 규정 검증 (spec_context 없으면 건너뜀) — 120초 타임아웃 적용
                t0 = time.time()
                try:
                    llm_violations = (
                        await asyncio.wait_for(
                            self.compliance_agent.check_compliance_parsed(
                                effective_target, spec_context, parsed
                            ),
                            timeout=120.0,
                        )
                        if spec_context
                        else []
                    )
                except asyncio.TimeoutError:
                    logging.error(
                        "[WorkflowHandler] compliance 검증 타임아웃(120s) target=%s — 빈 violations 반환",
                        effective_target,
                    )
                    llm_violations = []
                t_compliance = time.time() - t0
                await _emit_review_progress(
                    context,
                    "pipe_review_compliance",
                    f"시방·규정 검증 완료 — LLM 후보 {len(llm_violations)}건",
                )

                # #1 확정적 위반 검출 (LLM 없이 코드 기반)
                t0_det = time.time()
                det_violations = run_deterministic_checks(
                    elements, topology, geo, unit_factor=_unit_factor,
                )
                t_det = time.time() - t0_det
                await _emit_review_progress(
                    context,
                    "pipe_review_deterministic",
                    f"확정 규칙 검사 완료 — {len(det_violations)}건",
                )

                # 도면 품질검사(QA): 법규 위반과 별도로 drafting issue를 산출하되
                # report/fixes 흐름에 보이도록 violation-like schema로 함께 병합한다.
                t0_qa = time.time()
                qa_issues = run_drawing_qa_checks(
                    elements, topology, geo, unit_factor=_unit_factor,
                )
                t_qa = time.time() - t0_qa
                await _emit_review_progress(
                    context,
                    "pipe_review_qa",
                    f"도면 품질검사 완료 — 품질 이슈 {len(qa_issues)}건",
                )

                # LLM + 확정적 violations + QA issues 병합 (handle+type 중복 제거)
                _seen_viol: set[tuple] = {
                    (str(v.get("equipment_id") or ""), str(v.get("violation_type") or ""))
                    for v in llm_violations
                }
                merged_violations = list(llm_violations)
                for dv in [*det_violations, *qa_issues]:
                    key = (str(dv.get("equipment_id") or ""), str(dv.get("violation_type") or ""))
                    if key not in _seen_viol:
                        merged_violations.append(dv)
                        _seen_viol.add(key)

                # #5 confidence 기준 분리
                high_conf = [
                    v for v in merged_violations
                    if float(v.get("confidence_score") or 1.0) >= _CONFIDENCE_THRESHOLD
                ]
                low_conf = [
                    v for v in merged_violations
                    if float(v.get("confidence_score") or 1.0) < _CONFIDENCE_THRESHOLD
                ]
                logging.info(
                    "[WorkflowHandler] violations total=%d (high=%d low=%d det=%d qa=%d)",
                    len(merged_violations), len(high_conf), len(low_conf), len(det_violations), len(qa_issues),
                )

                # 5. 리포트 생성 (high confidence 기준)
                t0 = time.time()
                drawing_id = (
                    context.get("current_drawing_id")
                    or _drawing_data.get("drawing_number")
                    or ""
                )
                report = self.report_agent.generate(high_conf, drawing_id=drawing_id)
                t_report = time.time() - t0
                await _emit_review_progress(
                    context,
                    "pipe_review_report",
                    f"검토 리포트 생성 완료 — 표시 항목 {len((report or {}).get('items') or [])}건",
                )

                # 6. 수정 대안 계산
                t0 = time.time()
                current_layout = {
                    el.get("id") or el.get("handle", ""): el.get("position") or {}
                    for el in elements
                }
                fixes = self.revision_agent.calculate_fix(high_conf, current_layout)
                t_revision = time.time() - t0
                await _emit_review_progress(
                    context,
                    "pipe_review_revision",
                    f"수정안 계산 완료 — 수정 후보 {len(fixes)}건",
                )

                t_total = time.time() - t_start
                logging.info(
                    "[PipingTracker] Parser=%.2fs Parallel=%.2fs Compliance=%.2fs Det=%.2fs QA=%.2fs Report=%.2fs Revision=%.2fs | Total=%.2fs",
                    t_parser, t_parallel, t_compliance, t_det, t_qa, t_report, t_revision, t_total,
                )
                logging.info(
                    "[PipingDebug] call_review_agent report_items=%s fixes=%s low_conf=%s",
                    len((report or {}).get("items") or []),
                    len(fixes or []),
                    len(low_conf),
                )
                final_actions.append({
                    "agent": "review",
                    "result": {
                        "report":  report,
                        "fixes":   fixes,
                        "rag_references": rag_results,
                        # #5 confidence 분리 — UI에서 탭 분리 가능
                        "low_confidence_violations": low_conf,
                        # #1 확정적 위반 — 별도 표시용
                        "deterministic_violations": det_violations,
                        # 도면 품질검사 — 법규/시방과 별도 표시용
                        "drawing_quality_issues": qa_issues,
                        "meta": {
                            "unit_factor":        _unit_factor,
                            "rag_domain_count":   len(rag_queries),
                            "total_violations":   len(merged_violations),
                            "high_conf_count":    len(high_conf),
                            "low_conf_count":     len(low_conf),
                            "deterministic_count": len(det_violations),
                            "drawing_quality_count": len(qa_issues),
                        },
                    },
                })

            # ── call_action_agent ─────────────────────────────────────────
            # 결과 fixes[].handle + auto_fix → state.pending_fixes → (별도) REVIEW_RESULT UI/C# RevCloud, 적용은 APPROVE_FIX
            elif func_name == "call_action_agent":
                result = await self.action_agent.analyze_and_fix(context, domain="pipe")
                fixes = (result or {}).get("fixes") or []
                logging.info(
                    "[PipingDebug] call_action_agent fixes=%d sample_handles=%s",
                    len(fixes),
                    [f.get("handle") for f in fixes[:5]],
                )
                final_actions.append({"agent": "action", "result": result})

            # ── get_cad_entity_info ───────────────────────────────────────
            elif func_name == "get_cad_entity_info":
                from backend.services.agents.common.tools.common_tools import get_cad_entity_info_tool
                import json as _json
                handle = args.get("handle", "")
                # 건축/배관 분리 후 raw_layout은 mep subset일 수 있음 — 핸들 조회는 전체 drawing_data 우선
                full_dd = context.get("drawing_data")
                if isinstance(full_dd, dict) and (full_dd.get("entities") or full_dd.get("elements")):
                    drawing_data_str = _json.dumps(
                        {"entities": full_dd.get("entities") or full_dd.get("elements") or []},
                        ensure_ascii=False,
                    )
                else:
                    raw_layout = context.get("raw_layout_data", "{}")
                    if isinstance(raw_layout, dict):
                        drawing_data_str = _json.dumps(raw_layout, ensure_ascii=False)
                    else:
                        drawing_data_str = raw_layout or "{}"
                # get_cad_entity_info_tool 은 LangChain @tool 이므로 .invoke 로 호출
                result_str = get_cad_entity_info_tool.invoke({"handle": handle, "drawing_data": drawing_data_str})
                final_actions.append({"agent": "cad_info", "result": result_str})

        return final_actions
