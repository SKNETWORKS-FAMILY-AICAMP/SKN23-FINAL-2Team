"""
File    : backend/services/agents/fire/fire_agent.py
Author  : 양창일
Create  : 2026-04-07
Description : 소방 도메인 agent

Modification History :
    - 2026-04-07 (김지우) : 초기 구조 생성
    - 2026-04-08 (양창일) : 클래스명 및 공통 실행 인터페이스 정리
    - 2026-04-13 (양창일) : 도메인별 기본 검토 라벨 추가
    - 2026-04-13 (양창일) : 도메인별 review 설정 연동용 라벨 정리
    - 2026-04-15 (김민정) : RAG 기반 도면 매핑 및 용어 해석 체계 도입 (QueryAgent 연동)
    - 2026-04-19 (김민정) : workflow_handler 호출 로직 최적화 및 llm_client 제거
"""

import json
import logging
import uuid
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.database import SessionLocal
from backend.services.agents.base import BaseAgent
from backend.services.agents.fire.schemas import FIRE_SUB_AGENT_TOOLS
from backend.services.agents.fire.sub.mapping import MappingAgent
from backend.services.agents.fire.sub.query import QueryAgent
from backend.services.agents.fire.workflow_handler import FireWorkflowHandler
from backend.services import llm_service
from backend.services.graph.prompt_utils import build_memory_prompt_from_state
from backend.services.payload_service import should_preserve_full_entities


def _filter_entities_by_selection(drawing_data: dict, active_ids: list[str]) -> dict:
    """active_object_ids가 있으면 해당 엔티티만 남기고, 없으면 전체 반환"""
    if not active_ids:
        return drawing_data
    id_set = set(active_ids)
    entities = drawing_data.get("entities") or drawing_data.get("elements") or []
    filtered = [e for e in entities if e.get("handle") in id_set or e.get("id") in id_set]
    if not filtered:
        return drawing_data
    return {**drawing_data, "entities": filtered}


def _build_fire_context(payload: dict[str, Any]) -> dict[str, Any]:
    """
    도면 데이터 및 세션 정보를 바탕으로 소방 분석에 필요한 컨텍스트 객체를 생성합니다.
    """
    drawing_data = payload.get("drawing_data") or {}
    active_ids = list(payload.get("active_object_ids") or [])

    if active_ids and drawing_data and not should_preserve_full_entities(drawing_data):
        drawing_data = _filter_entities_by_selection(drawing_data, active_ids)

    raw = payload.get("raw_layout_data")
    if raw is None:
        raw = json.dumps(drawing_data, ensure_ascii=False) if drawing_data else "{}"
    elif isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False)
    
    has_drawing = bool(drawing_data) or (raw and raw != "{}")

    return {
        "user_request": str(payload.get("message") or ""),
        "org_id": payload.get("org_id"),
        "spec_guid": payload.get("spec_guid"),
        "raw_layout_data": raw,
        "active_object_ids": active_ids,
        "current_drawing_id": str(payload.get("current_drawing_id") or drawing_data.get("drawing_id") or ""),
        "mapping_table": payload.get("mapping_table"),
        "is_mapped": bool(payload.get("is_mapped", False) or payload.get("mapping_table")),
        "drawing_loaded": bool(payload.get("drawing_loaded", has_drawing)),
        "pending_fixes": list(payload.get("pending_fixes") or []),
    }


def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    """
    현재 도면 상태와 대화 메모리를 결합하여 시스템 프롬프트를 생성합니다. [가이드 1번 준수]
    """
    drawing_status = "도면 로드됨" if context.get("drawing_loaded") else "도면 없음"
    mapping_status = "매핑 완료" if context.get("is_mapped") else "매핑 미완료"
    pending = context.get("pending_fixes") or []
    pending_status = f"수정 대기 {len(pending)}건" if pending else "수정 대기 없음"

    return f"""당신은 20년 경력의 소방 설비 및 안전 진단 전문 엔지니어 AI 에이전트입니다.

[현재 상태]
- 도면: {drawing_status} | 매핑: {mapping_status} | {pending_status}

[이전 대화 메모리]
{memory_text}

[도구 선택 기준]
- call_query_agent  : NFSC 법규·시방서·소방 기준 정보 조회 요청
- call_review_agent : 소방 도면 검토 및 위반사항 분석 요청 (도면이 로드되어 있어야 함)
- call_action_agent : 수정 지시 실행 (pending_fixes 목록이 있어야 함)

반드시 사용자의 의도에 맞는 하나의 도구를 선택하세요."""


class FireAgent(BaseAgent):
    domain = "fire"
    review_label = "fire safety review"

    async def run(self, payload: dict[str, Any], db: AsyncSession | None = None) -> dict[str, Any]:
        """
        소방 에이전트 실행 진입점입니다.
        """
        if db is None:
            async with SessionLocal() as db:
                return await self._run_workflow(payload, db)
        return await self._run_workflow(payload, db)

    async def _run_workflow(self, payload: dict[str, Any], db: AsyncSession) -> dict[str, Any]:
        """
        전체 업무 흐름을 실행하고 가이드에 정의된 필수 규격으로 반환합니다.
        """
        user_request = str(payload.get("message") or payload.get("user_request") or "")
        context = _build_fire_context(payload)

        # 1. 도면 로드 시 지능형 RAG 매핑
        if context.get("drawing_loaded") and not context.get("is_mapped", False):
            mapping_agent = MappingAgent()
            scan_result = mapping_agent.execute(context.get("raw_layout_data", "{}"))
            query_agent = QueryAgent(db=db)
            term_map = await query_agent.resolve_terms(scan_result.get("unique_terms", []))
            
            context["mapping_table"] = term_map
            context["is_mapped"] = True
            
            entities = scan_result.get("entities", [])
            for ent in entities:
                ent["standard_type"] = term_map.get(ent.get("layer") or ent.get("block_name"))
            context["raw_layout_data"] = json.dumps({"entities": entities}, ensure_ascii=False)

        # 2. 공통 함수로 메모리 불러오기 및 프롬프트 조립 [가이드 1번]
        memory_text = build_memory_prompt_from_state(payload)
        system_content = _build_system_prompt(context, memory_text)

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_request},
        ]        

        tool_calls = await llm_service.generate_answer(
            messages=messages,
            tools=FIRE_SUB_AGENT_TOOLS,
            tool_choice="auto",
        )

        # LLM이 도구 없이 텍스트로 직접 응답한 경우 → 일반 대화로 처리
        if isinstance(tool_calls, str) and tool_calls.strip():
            return self._format_direct_response(payload, tool_calls.strip())

        if not isinstance(tool_calls, list) or not tool_calls:
            tool_calls = [{"function": {"name": "call_query_agent", "arguments": json.dumps({"query": user_request}, ensure_ascii=False)}}]

        # 3. 워크플로우 실행
        workflow = FireWorkflowHandler(session=context, db=db)
        workflow_results = await workflow.handle_tool_calls(tool_calls, context)
        
        # 4. 필수 규격 반환
        return await self._format_agent_response(payload, workflow_results)

    def _format_direct_response(self, payload: dict[str, Any], message: str) -> dict[str, Any]:
        """LLM 직접 텍스트 응답 (일반 대화) 처리"""
        return {
            "drawing_data": payload.get("drawing_data") or {},
            "retrieved_laws": payload.get("retrieved_laws") or [],
            "review_result": {
                "is_violation": False, "violations": [], "suggestions": [],
                "referenced_laws": [], "final_message": message,
            },
            "current_step": "agent_completed",
            "assistant_response": message,
            "active_object_ids": list(payload.get("active_object_ids") or []),
            "pending_fixes": [],
        }

    async def _format_agent_response(self, payload: dict[str, Any], workflow_results: list) -> dict[str, Any]:
        """
        결과를 review_completed 상태와 assistant_response 키를 포함하여 반환합니다.
        """
        drawing_data = payload.get("drawing_data") or {}
        retrieved_laws = payload.get("retrieved_laws") or []
        active_object_ids = list(payload.get("active_object_ids") or [])

        violations = []
        pending_fixes = []
        assistant_response = ""
        current_step = "review_completed"  # 가이드 준수
        referenced_laws = []

        for block in workflow_results:
            agent = block.get("agent")
            result = block.get("result")
            
            if agent == "query" and isinstance(result, list):
                parts = [r.get("content", "") for r in result[:5] if r.get("content")]
                if parts:
                    context_text = "\n\n---\n\n".join(parts)
                    prompt = (
                        f"다음은 검색된 소방 시방서 및 규정 내용입니다. 이 정보를 바탕으로 사용자의 질문에 자연스럽고 친절하게 요약된 답변을 작성해주세요.\n"
                        f"답변은 Markdown 형식을 사용하여 보기 좋게 정리해주세요.\n\n"
                        f"[검색 결과]\n{context_text}\n\n"
                        f"사용자 질문: {payload.get('message') or payload.get('user_request') or ''}"
                    )
                    from backend.services import llm_service
                    summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                    assistant_response = summary if isinstance(summary, str) else context_text
                    
                    refs = [f"«{r.get('doc_name') or '소방시방서'}»#{r.get('chunk_index', '-')}" for r in result[:5]]
                    assistant_response += f"\n\n---\n[출처] 시방RAG {' · '.join(refs)} (총{len(result)}건)."
                else:
                    assistant_response = "관련 소방 규정을 찾지 못했습니다."
                
            elif agent == "review" and isinstance(result, dict):
                report = result.get("report") or {}
                # ReportAgent에서 'results' 키로 변경된 부분 대응
                items = report.get("results") or report.get("items") or []
                fixes = result.get("fixes") or []
                
                violations = self._format_violations(items)
                pending_fixes = self._build_pending_fixes(fixes, items)
                referenced_laws = list({v.get("legal_reference") for v in violations if v.get("legal_reference")})
                
                summary = report.get("summary", {})
                total = summary.get("total_count", len(violations))
                assistant_response = f"소방 안전 진단을 완료했습니다. 총 {total}건의 위반 사항이 발견되었습니다. 상세 내용을 확인해 주세요."
                
            elif agent == "action":
                assistant_response = result.get("message", "명령 전송 준비가 완료되었습니다.")

        if not assistant_response:
            assistant_response = "요청하신 처리가 완료되었습니다."

        # 가이드라인 필수 Key 구성
        return {
            "domain": self.domain,
            "current_step": current_step,
            "assistant_response": assistant_response,
            "review_result": {
                "is_violation": len(violations) > 0,
                "violations": violations,
                "suggestions": [f["description"] for f in pending_fixes],
                "referenced_laws": referenced_laws,
                "final_message": assistant_response,
            },
            "pending_fixes": pending_fixes,
            "drawing_data": drawing_data,
            "active_object_ids": active_object_ids,
            "received_payload": payload,
        }

    def _format_violations(self, items: list) -> list:
        """
        리포트 아이템들을 내부 위반 사항 스키마에 맞춰 포맷팅합니다.
        """
        return [
            {
                "object_id": i.get("equipment_id"),
                "violation_type": i.get("violation_type"),
                "reason": i.get("reason"),
                "legal_reference": i.get("reference_rule"),
                "suggestion": i.get("required_value") or i.get("reason"),
                "current_value": i.get("current_value"),
                "required_value": i.get("required_value"),
            }
            for i in items
        ]

    def _build_pending_fixes(self, fixes: list, report_items: list) -> list:
        """
        수정 제안 사항들을 사용자 승인 대기(Pending Fix) 목록으로 변환합니다.
        """
        violation_map = {i.get("equipment_id"): i for i in report_items}
        result = []
        for fix in fixes:
            eq_id = fix.get("equipment_id")
            violation = violation_map.get(eq_id, {})
            result.append({
                "fix_id": str(uuid.uuid4()),
                "equipment_id": eq_id,
                "violation_type": violation.get("violation_type"),
                "action": fix.get("proposed_fix", {}).get("action"),
                "description": violation.get("reason") or fix.get("proposed_fix", {}).get("reason"),
                "proposed_fix": fix.get("proposed_fix"),
            })
        return result

