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
from backend.services.agents.elec.sub.elec_attr_extractor import inject_elec_data
from backend.services.agents.elec.sub.drawing_qa_checker import run_drawing_qa
from backend.services.agents.elec.sub.signal_extractor import ElecSignalExtractor
from backend.services.agents.elec.sub.candidate_generator import (
    ElecCandidateGenerator,
    candidate_to_dict,
)
from backend.services.agents.elec.sub.query_builder import ElecQueryBuilder
from backend.services.cad_progress import emit_pipeline_step

# ── 도메인별 RAG 쿼리 ─────────────────────────────────────────────────────────
_DOMAIN_RAG: dict[str, str] = {
    "HIGH_VOLTAGE": "고압 전기설비 이격거리 차단기 용량 기준",
    "LOW_VOLTAGE":  "저압 전선 허용전류 전압강하 한도 KEC 기준",
    "GROUNDING":    "접지선 굵기 접지저항 접지봉 피뢰설비 외함 접지 KEC 140 기준",
    "CONDUIT":      "전선관 점적률 설치 기준 KEC",
}

_HV_RE    = _re.compile(r"HV|HIGH.?VOLT|고압|PANEL.?H", _re.IGNORECASE)
_GND_RE   = _re.compile(r"GND|GROUND|접지|EARTH", _re.IGNORECASE)
_GROUNDING_RAG_RE = _re.compile(
    r"GND|GROUND|GRD|EARTH|접지|접지봉|접지선|접지도체|접지저항|외함\s*접지|피뢰|L\.?\s*A|E1|E2|1종|2종|3종|특별\s*3종|FGV",
    _re.IGNORECASE,
)
_COND_RE  = _re.compile(r"CONDUIT|전선관|RACEWAY", _re.IGNORECASE)
_ELEC_STANDARD_DOC_RE = _re.compile(
    r"\b(KEC|KCS\s*\d{2}\s*\d{2}\s*\d{2}|KDS\s*\d{2}\s*\d{2}\s*\d{2}|KS\s*[A-Z]?\s*\d+)\b",
    _re.IGNORECASE,
)
_ELEC_STANDARD_ALIASES = {
    "한국전기설비규정": "KEC",
    "전기설비기술기준": "KEC",
}


def _save_debug(filename: str, payload: Any) -> None:
    """ELEC 디버그 JSON을 UTF-8로 저장한다. 저장 실패는 검토 흐름을 막지 않는다."""
    try:
        safe_name = os.path.basename(str(filename or "debug.json"))
        base_dir = os.path.dirname(__file__)
        path = os.path.join(base_dir, safe_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logging.debug("[ElecWorkflow] debug 저장 실패 file=%s err=%s", filename, exc)


def _build_rag_query(elements: list[dict]) -> str:
    """레이어·타입 패턴으로 전기 도메인 추론 → RAG 쿼리 반환"""
    blob = " ".join(
        str(e.get(key, ""))
        for e in elements[:200]
        for key in ("layer", "text", "content", "effective_name", "block_name")
    )
    if _GROUNDING_RAG_RE.search(blob):
        return _DOMAIN_RAG["GROUNDING"]
    if _HV_RE.search(blob):
        return _DOMAIN_RAG["HIGH_VOLTAGE"]
    if _GND_RE.search(blob):
        return _DOMAIN_RAG["GROUNDING"]
    if _COND_RE.search(blob):
        return _DOMAIN_RAG["CONDUIT"]
    return _DOMAIN_RAG["LOW_VOLTAGE"]


def _normalize_elec_standard_doc(value: str | None, allow_free_text: bool = False) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).strip().split())
    if not text:
        return None
    for keyword, doc_name in _ELEC_STANDARD_ALIASES.items():
        if keyword in text:
            return doc_name
    match = _ELEC_STANDARD_DOC_RE.search(text)
    if match:
        return " ".join(match.group(1).upper().split())
    return text if allow_free_text else None


def _extract_target_doc(context: dict, args: dict | None = None) -> tuple[str | None, bool]:
    """전기 기준서 요청 범위를 추출한다. 명시된 기준이 있으면 다른 기준서와 섞지 않는다."""
    args = args or {}
    explicit = (
        args.get("target_doc")
        or context.get("target_doc")
        or context.get("standard_doc")
        or context.get("doc_name")
    )
    normalized = _normalize_elec_standard_doc(explicit, allow_free_text=True)
    if normalized:
        return normalized, True

    normalized = _normalize_elec_standard_doc(context.get("user_request"))
    if normalized:
        return normalized, True
    return None, False


def _handle_values(row: dict) -> set[str]:
    values: set[str] = set()
    for key in ("handle", "object_id", "equipment_id", "target_handle", "handle_a", "handle_b"):
        value = row.get(key)
        if value:
            values.add(str(value))
    for key in ("target_handles", "affected_handles", "related_handles"):
        raw = row.get(key)
        if isinstance(raw, list):
            values.update(str(v) for v in raw if v)
    return values


def _filter_to_selected_handles(violations: list[dict], selected_handles: set[str]) -> list[dict]:
    if not selected_handles:
        return violations
    return [v for v in violations if _handle_values(v) & selected_handles]


def _layout_to_dict(raw_layout: Any) -> dict:
    if isinstance(raw_layout, dict):
        return dict(raw_layout)
    if isinstance(raw_layout, str):
        try:
            data = json.loads(raw_layout)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _resolve_unit_to_mm_factor(context: dict, drawing_data: dict) -> float:
    for value in (
        context.get("unit_to_mm_factor"),
        (context.get("drawing_data") or {}).get("unit_to_mm_factor")
        if isinstance(context.get("drawing_data"), dict) else None,
        drawing_data.get("unit_to_mm_factor"),
    ):
        try:
            factor = float(value)
            if factor > 0:
                return factor
        except (TypeError, ValueError):
            pass

    context_drawing = context.get("drawing_data") if isinstance(context.get("drawing_data"), dict) else {}
    unit = str(
        drawing_data.get("drawing_unit")
        or context_drawing.get("drawing_unit")
        or ""
    ).strip().lower()
    return {
        "mm": 1.0,
        "millimeter": 1.0,
        "millimeters": 1.0,
        "cm": 10.0,
        "m": 1000.0,
        "meter": 1000.0,
        "meters": 1000.0,
        "inch": 25.4,
        "inches": 25.4,
        "feet": 304.8,
        "ft": 304.8,
    }.get(unit, 1.0)


def _rag_key(row: dict) -> tuple:
    return (
        str(row.get("source") or ""),
        str(row.get("document_id") or row.get("doc_name") or ""),
        str(row.get("section_id") or ""),
        str(row.get("chunk_index") or row.get("id") or ""),
    )


def _merge_rag_results(batches: list[list[dict] | dict | None]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple] = set()
    for batch in batches:
        rows = batch if isinstance(batch, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _rag_key(row)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
    return merged


def _format_spec_context(rag_results: list[dict]) -> str:
    chunks: list[str] = []
    for idx, row in enumerate(rag_results or [], 1):
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        header = (
            f"[근거자료 {idx}] source={row.get('source') or '-'} "
            f"doc={row.get('doc_name') or row.get('document_id') or '-'} "
            f"section={row.get('section_id') or '-'}"
        )
        chunks.append(f"{header}\n{content}")
    return "\n\n---\n\n".join(chunks)


def _candidate_hint(candidates: list) -> str:
    if not candidates:
        return ""
    rows = []
    hard_rows = [c for c in candidates if getattr(c, "numeric_violation", False)]
    soft_rows = [c for c in candidates if not getattr(c, "numeric_violation", False)]
    if hard_rows:
        rows.append("\n\n[코드 사전 계산 결과 — 재판단 금지]")
        for c in hard_rows[:20]:
            ev = c.evidence
            rows.append(
                f"- {c.candidate_id}: {c.reference_topic}, "
                f"equipment_id={ev.equipment_id}, observed={ev.observed_value}, "
                "도면 topology/geometry 엔진이 확정한 후보입니다. 조항 인용과 설명만 보강하십시오."
            )
    if soft_rows:
        rows.append("\n\n[전기 검토 후보 — RAG 근거가 있을 때만 위반화]")
        for c in soft_rows[:30]:
            ev = c.evidence
            rows.append(
                f"- {c.candidate_id}: {c.reference_topic}, "
                f"equipment_id={ev.equipment_id}, signal={ev.signal_type}, observed={ev.observed_value}"
            )
    return "\n".join(rows)


def _ensure_confidence(violations: list[dict], *, source: str) -> list[dict]:
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        if source == "deterministic":
            v.setdefault("confidence_score", 1.0)
            v.setdefault("confidence_reason", "deterministic_engine")
            v.setdefault("_source", "deterministic")
        else:
            try:
                score = float(v.get("confidence_score"))
            except (TypeError, ValueError):
                score = 0.65
            v["confidence_score"] = max(0.0, min(1.0, score))
            v.setdefault("confidence_reason", "llm_or_rag_evidence")
            v.setdefault("_source", source)
    return violations


_TOPOLOGY_VIOLATION_TYPES = {"open_circuit_error", "wire_disconnected", "device_not_connected"}


def _drop_llm_topology_violations(violations: list[dict]) -> list[dict]:
    """Topology continuity violations are owned by the deterministic checker."""
    filtered: list[dict] = []
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        if str(v.get("violation_type") or "") in _TOPOLOGY_VIOLATION_TYPES:
            continue
        filtered.append(v)
    return filtered


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
        self.signal_extractor = ElecSignalExtractor()
        self.candidate_generator = ElecCandidateGenerator()
        self.query_builder = ElecQueryBuilder()

    async def _dispatch_tool(
        self, func_name: str, args: dict, context: dict
    ) -> dict | None:

        # ── call_query_agent ──────────────────────────────────────────────────
        if func_name == "call_query_agent":
            target_doc, strict_target_doc = _extract_target_doc(context, args)
            result = await self.query_agent.execute(
                args.get("query", ""),
                spec_guid=context.get("spec_guid"),
                org_id=context.get("org_id"),
                domain="elec",
                limit=args.get("limit", 5),
                target_doc=target_doc,
                strict_target_doc=strict_target_doc,
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
            t0m = context.get("_progress_t0m") or time.monotonic()
            w0  = context.get("_progress_w0") or time.time()
            _sid = context.get("session_id") or ""
            _prog_last = time.monotonic()

            async def _wf_progress(stage: str, msg: str) -> None:
                nonlocal _prog_last
                _prog_last = await emit_pipeline_step(
                    session_id=_sid or None,
                    stage=stage,
                    message=msg,
                    t0_monotonic=t0m,
                    wall_start_ts=w0,
                    last_t=_prog_last,
                )

            target_id  = args.get("target_id", "ALL")
            raw_layout = context.get("raw_layout_data", "{}")

            # 1. 파싱
            parsed   = self.parser_agent.parse(raw_layout, mapping_table=context.get("mapping_table"))
            elements = parsed.get("elements", [])
            t_parse  = time.time() - t0
            await _wf_progress("elec_review_parse", f"도면 요소 파싱 완료 ({len(elements)}개 전기 요소)")

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

            # ── [핵심] 텍스트 기반 전기 속성 추출 + 전기 설비 분류 주입 ──────────────────
            # 도면 블록에 ATTDEF(속성 정의)가 없는 경우, 매핑된 텍스트 라벨에서
            # 전압, 위상, 주파수, 전선 굵기 등을 정규식으로 추출하여 주입한다.
            # 또한 effective_name / block_name / layer 기반으로 LIGHT, SWITCH, SOCKET,
            # PANEL, BREAKER, CABLE 등의 category를 주입한다.
            # category가 주입되어야 이후 topology의 connected_devices가 정확하게 채워진다.
            extracted_attrs: dict = {}
            if obj_mapping:
                extracted_attrs = inject_elec_data(elements, obj_mapping)
                if extracted_attrs:
                    parsed["elec_extracted_attrs"] = extracted_attrs
                    _save_debug("debug_extracted_attrs.json", extracted_attrs)
                    logging.info(
                        "[ElecWorkflow] 전기 속성 추출 및 설비 분류 주입 완료: %d개 블록",
                        len(extracted_attrs),
                    )
            # ─────────────────────────────────────────────────────────────────────

            if not elements:
                logging.warning("[ElecWorkflow] 파싱된 전기 요소 없음")

            effective_target = (
                elements[0]["id"] if elements and target_id == "ALL"
                else (target_id if target_id != "ALL" else context.get("current_drawing_id", "UNKNOWN"))
            )

            raw_drawing_data = _layout_to_dict(raw_layout)
            drawing_data = (
                dict(context.get("drawing_data"))
                if isinstance(context.get("drawing_data"), dict)
                else raw_drawing_data
            )
            if "entities" not in drawing_data and elements:
                drawing_data = dict(drawing_data)
                drawing_data["entities"] = elements
            unit_factor = _resolve_unit_to_mm_factor(context, drawing_data)

            # 2-4. topology(CPU) · geometry(CPU) · 기본 RAG(I/O) · Drawing QA 병렬 실행
            base_rag_query = _build_rag_query(elements)
            target_doc, strict_target_doc = _extract_target_doc(context, args)
            arch_reference_elements = [
                e for e in (context.get("arch_reference_entities") or [])
                if isinstance(e, dict)
            ]
            await _wf_progress("elec_review_rag_topology", "Topology/Geometry/RAG 병렬 처리 중...")
            topology, geo, base_rag_results, drawing_quality_issues = await asyncio.gather(
                asyncio.to_thread(self.topology_builder.build, elements, unit_factor),
                asyncio.to_thread(
                    self.geometry_proc.process,
                    elements,
                    unit_factor=unit_factor,
                    arch_elements=arch_reference_elements,
                ),
                self.query_agent.execute(
                    base_rag_query,
                    spec_guid=context.get("spec_guid"),
                    org_id=context.get("org_id"),
                    domain="elec",
                    limit=5,
                    target_doc=target_doc,
                    strict_target_doc=strict_target_doc,
                ),
                asyncio.to_thread(run_drawing_qa, drawing_data, unit_factor),
            )
            t_parallel_base = time.time() - t0 - t_parse

            # ── [DEBUG] topology 결과 저장 ─────────────────────────────────────
            _save_debug("debug_topology.json", topology)

            # topology 결과 주입
            parsed["elec_topology"] = topology
            logging.info(
                "[ElecWorkflow] topology runs=%d unconnected=%d panels=%d | parallel=%.2fs",
                topology.get("summary", {}).get("run_count", 0),
                topology.get("summary", {}).get("unconnected_wires", 0),
                topology.get("summary", {}).get("panel_count", 0),
                t_parallel_base,
            )

            # geometry 결과 주입
            parsed["conduit_clearances"] = geo.get("conduit_clearances", [])
            parsed["panel_clearances"]   = geo.get("panel_clearances", [])
            if geo.get("proxy_walls"):
                parsed.setdefault("arch_elements", geo["proxy_walls"])
            if geo.get("arch_walls"):
                parsed.setdefault("arch_reference_walls", geo["arch_walls"])

            # 신호 → 후보 → 후보 기반 RAG 쿼리
            signals = self.signal_extractor.extract(parsed)
            candidates = self.candidate_generator.generate(signals)
            self.query_builder.fill_candidate_queries(candidates)
            candidate_queries = self.query_builder.build_queries(
                candidates,
                fallback_query=base_rag_query,
                max_queries=8,
            )
            parsed["elec_signals"] = [
                {
                    "equipment_id": s.equipment_id,
                    "signal_type": s.signal_type,
                    "elec_category": s.elec_category,
                    "observed_value": s.observed_value,
                    "context": s.context,
                }
                for s in signals[:80]
            ]
            parsed["elec_candidates"] = [candidate_to_dict(c) for c in candidates[:80]]

            extra_rag_batches: list[list[dict]] = []
            for query in candidate_queries:
                if query == base_rag_query:
                    continue
                try:
                    extra_rag_batches.append(await self.query_agent.execute(
                        query,
                        spec_guid=context.get("spec_guid"),
                        org_id=context.get("org_id"),
                        domain="elec",
                        limit=4,
                        target_doc=target_doc,
                        strict_target_doc=strict_target_doc,
                    ))
                except Exception as exc:
                    logging.warning("[ElecWorkflow] 후보 기반 RAG 쿼리 실패 query=%r err=%s", query, exc)

            rag_results = _merge_rag_results([base_rag_results, *extra_rag_batches])
            spec_context = _format_spec_context(rag_results)
            fallback_spec_used = False
            if not spec_context:
                logging.warning(
                    "[ElecWorkflow] 전기 기준서 RAG 결과 없음 - 다른 기준 혼합 방지를 위해 LLM 법규 검토 생략 target_doc=%s",
                    target_doc or "-",
                )

            candidates_hint = _candidate_hint(candidates)
            t_parallel = time.time() - t0 - t_parse

            # ── [DEBUG] RAG 결과 저장 ─────────────────────────────────────────
            _save_debug("debug_rag_result.json", {
                "query_used": base_rag_query,
                "candidate_queries": candidate_queries,
                "target_doc": target_doc,
                "strict_target_doc": strict_target_doc,
                "chunks": rag_results,
                "spec_context_preview": spec_context[:500],
                "used_fallback": fallback_spec_used,
                "signal_count": len(signals),
                "candidate_count": len(candidates),
            })

            # 5. 규정 검증 — 120초 타임아웃
            await _wf_progress("elec_review_compliance", "시방·규정 검증 중 (최대 120초)...")
            if spec_context:
                try:
                    violations = await asyncio.wait_for(
                        self.compliance_agent.check_compliance_parsed(
                            effective_target, spec_context, parsed,
                            candidates_hint=candidates_hint,
                        ),
                        timeout=120.0,
                )
                except asyncio.TimeoutError:
                    logging.error("[ElecWorkflow] 법규 LLM 검토 타임아웃(120초) - LLM 위반 결과는 비워두고 deterministic 결과만 사용")
                    violations = []
            else:
                violations = []
            t_comp = time.time() - t0 - t_parse - t_parallel

            # ── [핵심] 확정적(코드 기반) 위반 검출 ──────────────────────────────
            # LLM 판단에 의존하지 않고, 추출된 전기 속성 + topology로
            # 전압 불일치, 접지 미연결 등을 직접 검출한다.
            await _wf_progress("elec_review_deterministic", "확정 규칙 검사 중...")
            from backend.services.agents.elec.sub.deterministic_checker import run_deterministic_checks

            det_violations = run_deterministic_checks(
                elements,
                extracted_attrs if obj_mapping else {},
                topology,
                unit_factor=unit_factor,
                qa_reference_elements=arch_reference_elements,
            )
            if topology.get("terminal_debug"):
                _save_debug("debug_symbol_semantics.json", topology.get("terminal_debug"))
                _save_debug("debug_topology.json", topology)
            det_violations = _ensure_confidence(det_violations, source="deterministic")
            violations = _ensure_confidence(violations, source="llm")
            before_topology_filter = len(violations)
            violations = _drop_llm_topology_violations(violations)
            if before_topology_filter != len(violations):
                logging.info(
                    "[ElecWorkflow] LLM topology violations filtered: %d -> %d (deterministic checker owns continuity)",
                    before_topology_filter,
                    len(violations),
                )
            if det_violations:
                logging.info(
                    "[ElecWorkflow] 확정적 위반 %d건 추가 (코드 기반)",
                    len(det_violations),
                )
                # 중복 제거: 같은 object_id + violation_type은 LLM 결과 우선
                def _violation_key(row: dict) -> tuple[str, str]:
                    return (
                        str(row.get("equipment_id") or row.get("object_id") or row.get("handle") or ""),
                        str(row.get("violation_type") or ""),
                    )
                existing_keys = {
                    _violation_key(v)
                    for v in violations
                }
                for dv in det_violations:
                    key = _violation_key(dv)
                    if key not in existing_keys:
                        violations.append(dv)

                # ── 확정적 위반의 legal_reference로 RAG 추가 조회 ────────────
                # 신호 기반 RAG에서 누락된 KEC 조항을 보완한다.
                det_refs = list({
                    str(dv.get("legal_reference") or "")
                    for dv in det_violations
                    if dv.get("legal_reference")
                })
                det_rag_batches: list[list[dict]] = []
                for ref in det_refs:
                    try:
                        batch = await self.query_agent.execute(
                            ref,
                            spec_guid=context.get("spec_guid"),
                            org_id=context.get("org_id"),
                            domain="elec",
                            limit=3,
                            target_doc=target_doc,
                            strict_target_doc=False,
                        )
                        if batch:
                            det_rag_batches.append(batch)
                            logging.info(
                                "[ElecWorkflow] 확정적 위반 RAG 보완 ref=%r chunks=%d",
                                ref, len(batch),
                            )
                    except Exception as exc:
                        logging.warning(
                            "[ElecWorkflow] 확정적 위반 RAG 조회 실패 ref=%r err=%s", ref, exc
                        )
                if det_rag_batches:
                    rag_results = _merge_rag_results([rag_results, *det_rag_batches])

            selected_handles = {str(x) for x in (context.get("active_object_ids") or []) if x}
            if selected_handles and target_id != "ALL":
                before_selected_filter = len(violations)
                violations = _filter_to_selected_handles(violations, selected_handles)
                logging.info(
                    "[ElecWorkflow] 선택 객체 기준 전기 검토 결과 필터 handles=%d violations=%d->%d",
                    len(selected_handles),
                    before_selected_filter,
                    len(violations),
                )
            # ─────────────────────────────────────────────────────────────────

            high_conf = [
                v for v in violations
                if float(v.get("confidence_score") or 1.0) >= 0.7
            ]
            low_conf = [
                v for v in violations
                if float(v.get("confidence_score") or 1.0) < 0.7
            ]

            # ── [DEBUG] compliance + 전체 파이프라인 결과 저장 ────────────────
            _save_debug("debug_review_result.json", {
                "is_violation": len(high_conf) > 0,
                "violations":   high_conf,
                "low_confidence_violations": low_conf,
                "drawing_quality_issues": drawing_quality_issues,
                "suggestions":  [v.get("reason", "") for v in high_conf],
                "referenced_laws": list({
                    v.get("reference_rule", "") for v in high_conf if v.get("reference_rule")
                }),
                "final_message": f"전기 검토 완료: 위반 {len(high_conf)}건. 수정 항목을 확인하고 적용할 항목을 선택하세요.",
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
                    "rag_query_count":  len(candidate_queries),
                    "signal_count":     len(signals),
                    "candidate_count":  len(candidates),
                    "drawing_quality_count": len(drawing_quality_issues or []),
                    "used_rag_fallback": fallback_spec_used,
                    "violations_found": len(high_conf),
                    "low_confidence_count": len(low_conf),
                },
            })
            # ─────────────────────────────────────────────────────────────────

            # 6. 리포트
            await _wf_progress("elec_review_report", f"검토 리포트 생성 중 (위반 {len(high_conf)}건)...")
            report = self.report_agent.generate(
                high_conf, drawing_id=context.get("current_drawing_id", "")
            )

            # 7. 수정안
            await _wf_progress("elec_review_revision", "수정안 계산 중...")
            current_layout = {
                (el.get("id") or el.get("handle") or ""): el.get("position", {})
                for el in elements
                if isinstance(el, dict)
            }
            fixes = self.revision_agent.calculate_fix(high_conf, current_layout)

            t_total = time.time() - t0
            logging.info(
                "[ElecTracker] parse=%.2fs parallel(topo+geo+rag)=%.2fs comp=%.2fs total=%.2fs",
                t_parse, t_parallel, t_comp, t_total,
            )
            return {
                "agent": "review",
                "result": {
                    "report": report,
                    "fixes": fixes,
                    "rag_references": rag_results,
                    "low_confidence_violations": low_conf,
                    "drawing_quality_issues": drawing_quality_issues,
                    # KPI(recall_lower_bound) 산출용 — det_violations 는 위 254행에서 violations 에
                    # 합쳐졌지만, 평가 모듈은 "결정론적 검사가 잡은 집합"을 따로 알아야 함.
                    "deterministic_violations": det_violations or [],
                    "meta": {
                        "unit_factor": unit_factor,
                        "rag_query_count": len(candidate_queries),
                        "rag_queries": candidate_queries,
                        "rag_result_count": len(rag_results),
                        "signal_count": len(signals),
                        "candidate_count": len(candidates),
                        "high_conf_count": len(high_conf),
                        "low_conf_count": len(low_conf),
                        "drawing_quality_count": len(drawing_quality_issues or []),
                        "rag_fallback_spec_used": fallback_spec_used,
                    },
                },
            }

        # ── call_action_agent ─────────────────────────────────────────────────
        if func_name == "call_action_agent":
            result = await self.action_agent.analyze_and_fix(context, domain="elec")
            fixes  = (result or {}).get("fixes") or []
            logging.info("[ElecDebug] call_action_agent fixes=%d", len(fixes))
            return {"agent": "action", "result": result}

        # ── validate_after_fix ────────────────────────────────────────────────
        if func_name == "validate_after_fix":
            result = await self._validate_after_fix(args, context)
            return {"agent": "validate", "result": result}

        # ── get_cad_entity_info ───────────────────────────────────────────────
        if func_name == "get_cad_entity_info":
            from backend.services.agents.common.tools.common_tools import get_cad_entity_info_tool
            handle     = args.get("handle", "")
            raw_layout = context.get("raw_layout_data", "{}")
            drawing_str = json.dumps(raw_layout, ensure_ascii=False) if isinstance(raw_layout, dict) else (raw_layout or "{}")
            result_str  = get_cad_entity_info_tool.invoke({"handle": handle, "drawing_data": drawing_str})
            return {"agent": "cad_info", "result": result_str}

        return None

    async def _validate_after_fix(self, args: dict, context: dict) -> dict:
        """
        수정 액션 적용 후 재검증 루프.

        입력 (args):
          updated_entities     : 수정 후 재추출된 엔티티 JSON 문자열 또는 리스트
          original_violations  : 최초 감지된 위반 목록
          applied_actions      : 적용된 수정 명령 (중복 방지용)

        반환:
          resolved             : 전체 위반 해결 여부
          remaining_violations : 미해결 위반 목록
          new_violations       : 수정으로 인해 새로 발생한 위반 목록
          re_fix_required      : 추가 수정 필요 여부
          summary              : 통계 요약
        """
        from backend.services.agents.elec.sub.deterministic_checker import run_deterministic_checks

        _MAX_RETRY = 3

        # 엔티티 파싱
        raw = args.get("updated_entities") or context.get("raw_layout_data") or "[]"
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = []
        if isinstance(raw, dict):
            raw = raw.get("elements") or raw.get("entities") or []
        updated_elements: list[dict] = [e for e in (raw or []) if isinstance(e, dict)]
        unit_factor = _resolve_unit_to_mm_factor(
            context,
            {"entities": updated_elements, **(
                context.get("drawing_data") if isinstance(context.get("drawing_data"), dict) else {}
            )},
        )

        # 원본 위반 목록
        original_violations = args.get("original_violations") or []
        if isinstance(original_violations, str):
            try:
                original_violations = json.loads(original_violations)
            except (json.JSONDecodeError, TypeError):
                original_violations = []

        # 적용된 액션 (중복 방지용 키 집합)
        applied_actions = args.get("applied_actions") or []
        if isinstance(applied_actions, str):
            try:
                applied_actions = json.loads(applied_actions)
            except (json.JSONDecodeError, TypeError):
                applied_actions = []
        applied_keys = {
            (str(a.get("handle") or a.get("target_handle") or ""), str(a.get("type") or a.get("action") or ""))
            for a in (applied_actions if isinstance(applied_actions, list) else [])
        }

        if not updated_elements:
            return {
                "resolved":              False,
                "remaining_violations":  original_violations,
                "new_violations":        [],
                "re_fix_required":       bool(original_violations),
                "validation_source":     None,
                "requires_updated_extract": True,
                "summary": {
                    "error": (
                        "updated_entities 없음 — AutoCAD 적용 후 C# 플러그인이 "
                        "재추출한 엔티티를 전달해야 재검증 가능"
                    )
                },
            }

        # topology + deterministic 재실행
        try:
            topology, _ = await asyncio.gather(
                asyncio.to_thread(self.topology_builder.build, updated_elements, unit_factor),
                asyncio.to_thread(
                    self.geometry_proc.process,
                    updated_elements,
                    unit_factor=unit_factor,
                    arch_elements=context.get("arch_reference_entities") or [],
                ),
            )
        except Exception as exc:
            logging.error("[ValidateAfterFix] topology/geometry 재실행 실패: %s", exc)
            topology = {}

        new_det_violations = run_deterministic_checks(
            updated_elements,
            {},
            topology,
            unit_factor=unit_factor,
            qa_reference_elements=context.get("arch_reference_entities") or [],
        )
        new_det_violations = _ensure_confidence(new_det_violations, source="deterministic")

        # 원본 위반의 (object_id, violation_type) 키 집합
        original_keys = {
            (str(v.get("object_id") or v.get("equipment_id") or ""), str(v.get("violation_type") or ""))
            for v in original_violations
        }
        new_keys = {
            (str(v.get("object_id") or ""), str(v.get("violation_type") or ""))
            for v in new_det_violations
        }

        remaining = [
            v for v in new_det_violations
            if (str(v.get("object_id") or ""), str(v.get("violation_type") or "")) in original_keys
        ]
        new_violations = [
            v for v in new_det_violations
            if (str(v.get("object_id") or ""), str(v.get("violation_type") or "")) not in original_keys
        ]
        resolved_count = len(original_keys) - len(remaining)

        re_fix_required = bool(remaining or new_violations)

        logging.info(
            "[ValidateAfterFix] 원본위반=%d 해결=%d 잔여=%d 신규=%d re_fix=%s",
            len(original_violations), resolved_count, len(remaining), len(new_violations), re_fix_required,
        )

        return {
            "resolved":                 not re_fix_required,
            "remaining_violations":     remaining,
            "new_violations":           new_violations,
            "re_fix_required":          re_fix_required,
            "max_retry":                _MAX_RETRY,
            "validation_source":        "updated_autocad_snapshot",
            "requires_updated_extract": True,
            "summary": {
                "original_count":  len(original_violations),
                "resolved_count":  resolved_count,
                "remaining_count": len(remaining),
                "new_count":       len(new_violations),
            },
        }
