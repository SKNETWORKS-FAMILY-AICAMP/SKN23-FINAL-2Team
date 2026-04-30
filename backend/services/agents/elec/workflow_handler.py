"""
File    : backend/services/agents/elec/workflow_handler.py
Author  : 김지우
Create  : 2026-04-23
Modified: 2026-04-24 (Phase 7 — topology/geometry 주입, RAG 도메인 추론, BaseWorkflowHandler 상속)
Description : ELEC_SUB_AGENT_TOOLS 기반 툴 호출 처리 및 서브 에이전트 연동
"""

import asyncio
import json
import logging
import os
import re as _re
import time
from typing import Any

# ── 디버그 JSON 저장 디렉토리 ───────────────────────────────────────────
_DEBUG_DIR = r"C:\Users\Playdata\Desktop\SKN23-FINAL-2TEAM\SKN23-FINAL\SKN23-FINAL-2TEAM\backend\services\agents\elec"


def _save_debug(filename: str, data: Any) -> None:
    """pipeline 중간 결과를 JSON 파일로 저장하는 디버그 헬퍼."""
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        path = os.path.join(_DEBUG_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        logging.debug("[ElecDebug] 저장 완료: %s", path)
    except Exception as e:
        logging.warning("[ElecDebug] 저장 실패 %s: %s", filename, e)

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.agents.common.base_workflow_handler import BaseWorkflowHandler
from backend.services.agents.elec.sub.query import QueryAgent
from backend.services.agents.elec.sub.review.parser import ParserAgent
from backend.services.agents.elec.sub.review.compliance import ComplianceAgent
from backend.services.agents.elec.sub.review.report import ReportAgent
from backend.services.agents.elec.sub.review.revision import RevisionAgent
from backend.services.agents.elec.sub.action import ActionAgent
from backend.services.agents.elec.sub.topology import ElecTopologyBuilder
from backend.services.agents.elec.sub.geometry import ElecGeometryPreprocessor
from backend.services.agents.elec.sub.elec_attr_extractor import inject_elec_attrs_from_mapping

# ── KEC 폴백 ─────────────────────────────────────────────────────────────────
_KEC_FALLBACK_SPEC = """\
[전기설비기술기준(KEC) 핵심 기준 — RAG 조회 실패 시 폴백]
1. 저압 전선 허용 전류 (KEC 212.4)
   - 1.5SQ: 최대 15A / 2.5SQ: 최대 20A / 4SQ: 최대 27A / 6SQ: 최대 34A
   - 10SQ: 최대 46A / 16SQ: 최대 62A / 25SQ: 최대 82A / 35SQ: 최대 99A
2. 전압강하 한도 (KEC 232.29)
   - 저압 간선: 인입점 기준 최대 2% / 분기회로: 추가 최대 2% (총 4% 이내)
3. 상 색상 기준 (KEC 121.2)
   - L1(R상): 갈색/적색, L2(S상): 흑색, L3(T상): 회색, N(중성): 청색, PE(보호): 황녹색
4. 차단기 용량 (KEC 212.5) — 전선 허용전류의 100% 이하
5. 이격거리 (KEC 232.7) — 저압 케이블 상호: 외경의 0.5배 이상, 가압부: 최소 50mm
6. 접지선 굵기 (KEC 142.4) — 상도체 16SQ 이하 → 접지 16SQ 이상
"""

# ── 도메인별 RAG 쿼리 ─────────────────────────────────────────────────────────
_DOMAIN_RAG: dict[str, str] = {
    "HIGH_VOLTAGE": "고압 전기설비 이격거리 차단기 용량 기준",
    "LOW_VOLTAGE":  "저압 전선 허용전류 전압강하 한도 KEC 기준",
    "GROUNDING":    "접지선 굵기 접지저항 기준 KEC 142",
    "CONDUIT":      "전선관 점적률 설치 기준 KEC",
}

_HV_RE    = _re.compile(r"HV|HIGH.?VOLT|고압|PANEL.?H", _re.IGNORECASE)
_GND_RE   = _re.compile(r"GND|GROUND|접지|EARTH", _re.IGNORECASE)
_COND_RE  = _re.compile(r"CONDUIT|전선관|RACEWAY", _re.IGNORECASE)


def _build_rag_query(elements: list[dict]) -> str:
    """레이어·타입 패턴으로 전기 도메인 추론 → RAG 쿼리 반환"""
    layers = " ".join(str(e.get("layer", "")) for e in elements[:80])
    if _HV_RE.search(layers):
        return _DOMAIN_RAG["HIGH_VOLTAGE"]
    if _GND_RE.search(layers):
        return _DOMAIN_RAG["GROUNDING"]
    if _COND_RE.search(layers):
        return _DOMAIN_RAG["CONDUIT"]
    return _DOMAIN_RAG["LOW_VOLTAGE"]


class ElecWorkflowHandler(BaseWorkflowHandler):
    def __init__(self, session: Any, db: AsyncSession):
        super().__init__(session, db)
        self.query_agent      = QueryAgent(db)
        self.parser_agent     = ParserAgent()
        self.compliance_agent = ComplianceAgent()
        self.report_agent     = ReportAgent()
        self.revision_agent   = RevisionAgent()
        self.action_agent     = ActionAgent()
        self.topology_builder = ElecTopologyBuilder()
        self.geometry_proc    = ElecGeometryPreprocessor()

    async def _dispatch_tool(
        self, func_name: str, args: dict, context: dict
    ) -> dict | None:

        # ── call_query_agent ──────────────────────────────────────────────────
        if func_name == "call_query_agent":
            result = await self.query_agent.execute(
                args.get("query", ""),
                spec_guid=context.get("spec_guid"),
                org_id=context.get("org_id"),
                domain="elec",
            )
            logging.info("[ElecDebug] call_query_agent chunks=%d", len(result) if isinstance(result, list) else 0)
            return {"agent": "query", "result": result}

        # ── call_review_agent ─────────────────────────────────────────────────
        # intent == "answer" 일 때 LLM이 review 도구를 잘못 선택한 경우 강제 전환
        if func_name == "call_review_agent" and context.get("intent") == "answer":
            logging.info("[ElecWorkflow] intent=answer → call_review_agent 차단, call_query_agent로 전환")
            return await self._dispatch_tool("call_query_agent", args, context)

        if func_name == "call_review_agent":
            t0 = time.time()
            target_id  = args.get("target_id", "ALL")
            raw_layout = context.get("raw_layout_data", "{}")

            # 1. 파싱
            parsed   = self.parser_agent.parse(raw_layout, mapping_table=context.get("mapping_table"))
            elements = parsed.get("elements", [])
            t_parse  = time.time() - t0

            # ── [핵심] object_mapping 주입: 블록 핸들 → 설비명(라벨) 매핑 정보 ──
            # compliance AI가 각 블록의 의미(설비명)를 알 수 있도록 매핑 결과를 주입한다.
            obj_mapping = context.get("object_mapping") or []
            if obj_mapping:
                # block_handle → label 조회 딕셔너리 생성
                handle_to_label: dict[str, list[str]] = {}
                for m in obj_mapping:
                    bh = str(m.get("block_handle", ""))
                    label = str(m.get("label", "")).strip()
                    if bh and label:
                        handle_to_label.setdefault(bh, []).append(label)

                # elements의 각 설비에 name 필드를 보강
                for el in elements:
                    el_handle = str(el.get("handle") or el.get("id") or "")
                    labels = handle_to_label.get(el_handle, [])
                    if labels and not el.get("name"):
                        el["name"] = " / ".join(labels[:3])  # 최대 3개 라벨

                # 요약 매핑 테이블도 주입 (AI 참조용)
                parsed["equipment_labels"] = {
                    bh: " / ".join(lbls[:3])
                    for bh, lbls in handle_to_label.items()
                }
                logging.info(
                    "[ElecWorkflow] object_mapping 주입 완료: %d쌍 → %d개 블록에 이름 보강",
                    len(obj_mapping), len(handle_to_label),
                )
            # ────────────────────────────────────────────────────────────────────

            # ── [핵심] 텍스트 기반 전기 속성 추출 → 블록에 주입 ──────────────────
            # 도면 블록에 ATTDEF(속성 정의)가 없는 경우, 매핑된 텍스트 라벨에서
            # 전압, 위상, 주파수, 전선 굵기 등을 정규식으로 추출하여 주입한다.
            extracted_attrs: dict = {}
            if obj_mapping:
                extracted_attrs = inject_elec_attrs_from_mapping(elements, obj_mapping)
                if extracted_attrs:
                    parsed["elec_extracted_attrs"] = extracted_attrs
                    _save_debug("debug_extracted_attrs.json", extracted_attrs)
                    logging.info(
                        "[ElecWorkflow] 텍스트 기반 전기 속성 추출: %d개 블록에 속성 주입",
                        len(extracted_attrs),
                    )
            # ─────────────────────────────────────────────────────────────────────

            if not elements:
                logging.warning("[ElecWorkflow] 파싱된 전기 요소 없음")

            effective_target = (
                elements[0]["id"] if elements and target_id == "ALL"
                else (target_id if target_id != "ALL" else context.get("current_drawing_id", "UNKNOWN"))
            )

            # 2-4. topology(CPU) · geometry(CPU) · RAG(I/O) 동시 실행 — gather 병렬화
            rag_query = _build_rag_query(elements)
            topology, geo, rag_results = await asyncio.gather(
                asyncio.to_thread(self.topology_builder.build, elements),
                asyncio.to_thread(self.geometry_proc.process, elements),
                self.query_agent.execute(
                    rag_query,
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    domain="elec",
                    limit=5,
                ),
            )
            t_parallel = time.time() - t0 - t_parse

            # ── [DEBUG] topology 결과 저장 ─────────────────────────────────────
            _save_debug("debug_topology.json", topology)

            # RAG 결과 조립 (debug_rag_result 저장보다 먼저 해야 spec_context 참조 가능)
            spec_context = "\n".join(r["content"] for r in rag_results if r.get("content"))
            if not spec_context:
                logging.warning("[ElecWorkflow] RAG 결과 없음 — KEC 폴백 적용")
                spec_context = _KEC_FALLBACK_SPEC

            # ── [DEBUG] RAG 결과 저장 ─────────────────────────────────────────
            _save_debug("debug_rag_result.json", {
                "query_used": rag_query,
                "chunks": rag_results,
                "spec_context_preview": spec_context[:500],
                "used_fallback": not bool(rag_results),
            })

            # topology 결과 주입
            parsed["elec_topology"] = topology
            logging.info(
                "[ElecWorkflow] topology runs=%d unconnected=%d panels=%d | parallel=%.2fs",
                topology.get("summary", {}).get("run_count", 0),
                topology.get("summary", {}).get("unconnected_wires", 0),
                topology.get("summary", {}).get("panel_count", 0),
                t_parallel,
            )

            # geometry 결과 주입
            parsed["conduit_clearances"] = geo.get("conduit_clearances", [])
            parsed["panel_clearances"]   = geo.get("panel_clearances", [])
            if geo.get("proxy_walls"):
                parsed.setdefault("arch_elements", geo["proxy_walls"])

            # 5. 규정 검증 — 120초 타임아웃
            try:
                violations = await asyncio.wait_for(
                    self.compliance_agent.check_compliance_parsed(
                        effective_target, spec_context, parsed
                    ),
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                logging.error("[ElecWorkflow] compliance 검증 타임아웃(120s) — 빈 violations 반환")
                violations = []
            t_comp = time.time() - t0 - t_parse - t_parallel

            # ── [핵심] 확정적(코드 기반) 위반 검출 ──────────────────────────────
            # LLM 판단에 의존하지 않고, 추출된 전기 속성 + topology로
            # 전압 불일치, 접지 미연결 등을 직접 검출한다.
            from backend.services.agents.elec.sub.deterministic_checker import run_deterministic_checks

            det_violations = run_deterministic_checks(
                elements,
                extracted_attrs if obj_mapping else {},
                topology,
            )
            if det_violations:
                logging.info(
                    "[ElecWorkflow] 확정적 위반 %d건 추가 (코드 기반)",
                    len(det_violations),
                )
                # 중복 제거: 같은 object_id + violation_type은 LLM 결과 우선
                existing_keys = {
                    (v.get("object_id"), v.get("violation_type"))
                    for v in violations
                }
                for dv in det_violations:
                    key = (dv.get("object_id"), dv.get("violation_type"))
                    if key not in existing_keys:
                        violations.append(dv)
            # ─────────────────────────────────────────────────────────────────

            # ── [DEBUG] compliance + 전체 파이프라인 결과 저장 ────────────────
            _save_debug("debug_review_result.json", {
                "is_violation": len(violations) > 0,
                "violations":   violations,
                "suggestions":  [v.get("reason", "") for v in violations],
                "referenced_laws": list({
                    v.get("reference_rule", "") for v in violations if v.get("reference_rule")
                }),
                "final_message": f"전기 검토 완료: 위반 {len(violations)}건. 수정 항목을 확인하고 적용할 항목을 선택하세요.",
            })
            _save_debug("debug_pipeline_summary.json", {
                "timing_sec": {
                    "parse":       round(t_parse, 2),
                    "parallel":    round(t_parallel, 2),
                    "compliance":  round(t_comp, 2),
                    "total":       round(time.time() - t0, 2),
                },
                "counts": {
                    "elements_sent_to_compliance": len(parsed.get("elements", [])),
                    "topology_runs":    topology.get("summary", {}).get("run_count", 0),
                    "broken_segments":  len(topology.get("broken_segments", [])),
                    "dangling_endpoints": len(topology.get("dangling_endpoints", [])),
                    "rag_chunks":       len(rag_results),
                    "used_rag_fallback": not bool(rag_results),
                    "violations_found": len(violations),
                },
            })
            # ─────────────────────────────────────────────────────────────────

            # 6. 리포트
            report = self.report_agent.generate(
                violations, drawing_id=context.get("current_drawing_id", "")
            )

            # 7. 수정안
            current_layout = {el["id"]: el.get("position", {}) for el in elements}
            fixes = self.revision_agent.calculate_fix(violations, current_layout)

            t_total = time.time() - t0
            logging.info(
                "[ElecTracker] parse=%.2fs parallel(topo+geo+rag)=%.2fs comp=%.2fs total=%.2fs",
                t_parse, t_parallel, t_comp, t_total,
            )
            return {
                "agent": "review",
                "result": {"report": report, "fixes": fixes, "rag_references": rag_results},
            }

        # ── call_action_agent ─────────────────────────────────────────────────
        if func_name == "call_action_agent":
            result = await self.action_agent.analyze_and_fix(context, domain="elec")
            fixes  = (result or {}).get("fixes") or []
            logging.info("[ElecDebug] call_action_agent fixes=%d", len(fixes))
            return {"agent": "action", "result": result}

        # ── get_cad_entity_info ───────────────────────────────────────────────
        if func_name == "get_cad_entity_info":
            from backend.services.agents.common.tools.common_tools import get_cad_entity_info_tool
            handle     = args.get("handle", "")
            raw_layout = context.get("raw_layout_data", "{}")
            drawing_str = json.dumps(raw_layout, ensure_ascii=False) if isinstance(raw_layout, dict) else (raw_layout or "{}")
            result_str  = get_cad_entity_info_tool.invoke({"handle": handle, "drawing_data": drawing_str})
            return {"agent": "cad_info", "result": result_str}

        return None
