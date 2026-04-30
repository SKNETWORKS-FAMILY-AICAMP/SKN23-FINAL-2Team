"""
File    : backend/services/agents/architecture/arch_agent.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15

Description :
    건축 도메인 에이전트 — 건축법 시행령 기준 CAD 도면 검토.

        1. LLM이 ARCH_SUB_AGENT_TOOLS 중 하나를 선택
        2. ArchWorkflowHandler가 선택된 툴 실행
        3. Review Pipeline: Parser → Compliance → Revision → Report
        4. pending_fixes 목록 반환 → HITL → ActionAgent → C# DrawingPatcher

Modification History :
    - 2026-04-07 (김지우)  : 초기 구조 생성
    - 2026-04-08 (양창일) : 클래스명 및 공통 실행 인터페이스 정리
    - 2026-04-13 (양창일) : 도메인별 기본 검토 라벨 추가
    - 2026-04-15 (김다빈) : 전면 재작성
"""

import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from backend.core.database import SessionLocal
from backend.services.agents.base import BaseAgent
from backend.services.agents.arch.schemas import ARCH_SUB_AGENT_TOOLS
from backend.services.agents.arch.workflow_handler import ArchWorkflowHandler
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


def _build_arch_context(payload: dict[str, Any]) -> dict[str, Any]:
    drawing_data = payload.get("drawing_data") or {}
    active_ids = list(payload.get("active_object_ids") or [])

    if active_ids and drawing_data and not should_preserve_full_entities(drawing_data):
        drawing_data = _filter_entities_by_selection(drawing_data, active_ids)

    has_drawing = bool(drawing_data and drawing_data.get("entities"))

    return {
        "org_id":          payload.get("org_id"),
        "spec_guid":       payload.get("spec_guid"),
        "drawing_data":    drawing_data,
        "active_object_ids": active_ids,
        "drawing_loaded":  bool(payload.get("drawing_loaded", has_drawing)),
        "pending_fixes":   list(payload.get("pending_fixes") or []),
    }


def _build_system_prompt(context: dict[str, Any], memory_text: str) -> str:
    drawing_status = "도면 로드됨" if context.get("drawing_loaded") else "도면 없음"
    pending        = context.get("pending_fixes") or []
    pending_status = f"수정 대기 {len(pending)}건" if pending else "수정 대기 없음"

    pending_hint = ""
    if pending:
        lines = "\n".join(
            f"  - {f.get('handle', '?')}: "
            f"{f.get('violation_type', '?')} / {f.get('action', '?')}"
            for f in pending[:5]
        )
        more = f"\n  ... 외 {len(pending) - 5}건" if len(pending) > 5 else ""
        pending_hint = f"\n\n[수정 대기 항목]\n{lines}{more}"

    return (
        f"당신은 20년 경력의 건축법 전문 AI 에이전트입니다.\n"
        f"현재 상태: {drawing_status} | {pending_status}"
        f"{pending_hint}"
        f"\n\n[대화 메모리]\n{memory_text}"
        f"\n\n[도구 선택 기준]\n"
        f"- call_query_agent  : 건축법 시행령·기준 조항 조회 요청\n"
        f"- call_review_agent : 도면 검토 및 건축법 위반 분석 (도면이 로드되어 있어야 함)\n"
        f"- call_action_agent : 수정 지시 실행 (pending_fixes 목록이 있어야 함)\n\n"
        f"반드시 하나의 도구를 선택하세요."
    )


def _make_fallback_call(message: str) -> dict:
    return {
        "function": {
            "name": "call_query_agent",
            "arguments": json.dumps({"query": message}, ensure_ascii=False),
        }
    }


class ArchAgent(BaseAgent):
    domain       = "arch"
    review_label = "건축 도면 검토"

    def __init__(self):
        super().__init__()

    async def run(self, payload: dict[str, Any], db: Session | None = None) -> dict[str, Any]:
        own_db = False
        if db is None:
            db = SessionLocal()
            own_db = True
        try:
            return await self._run_workflow(payload, db)
        finally:
            if own_db and db is not None:
                db.close()

    async def _run_workflow(self, payload: dict[str, Any], db: Session) -> dict[str, Any]:
        message     = str(payload.get("message") or payload.get("user_request") or "")
        context     = _build_arch_context(payload)
        memory_text = build_memory_prompt_from_state(payload)  # AgentState 필드 활용

        # LLM이 ARCH_SUB_AGENT_TOOLS 중 하나를 선택
        messages = [
            {"role": "system", "content": _build_system_prompt(context, memory_text)},
            {"role": "user",   "content": message},
        ]
        tool_calls = await llm_service.generate_answer(
            messages=messages,
            tools=ARCH_SUB_AGENT_TOOLS,
            tool_choice="auto",
        )

        # LLM이 도구 없이 텍스트로 직접 응답한 경우 → 일반 대화로 처리
        if isinstance(tool_calls, str) and tool_calls.strip():
            return self._format_direct_response(payload, tool_calls.strip())

        if not isinstance(tool_calls, list) or not tool_calls:
            logging.warning("[ArchAgent] LLM tool 미선택, query fallback 적용")
            tool_calls = [_make_fallback_call(message)]

        logging.info(
            "[ArchAgent] 선택된 tool: %s",
            [c["function"]["name"] for c in tool_calls],
        )

        # 선택된 tool(s) 실행
        workflow = ArchWorkflowHandler(session=context, db=db)
        workflow_results = await workflow.handle_tool_calls(tool_calls, context)
        return await self._format_agent_response(payload, workflow_results)

    def _format_direct_response(self, payload: dict[str, Any], message: str) -> dict[str, Any]:
        """LLM 직접 텍스트 응답 (일반 대화) 처리"""
        return {
            "domain": self.domain,
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

    async def handle_message(
        self,
        message: str,
        context: dict[str, Any] | None = None,
        db: Session | None = None,
    ):
        """사용자 메시지 처리 진입점 (테스트·직접 호출용)"""
        merged = dict(context or {})
        merged["message"] = message
        return await self.run(merged, db=db)

    async def _format_agent_response(
        self,
        payload: dict[str, Any],
        workflow_results: Any,
    ) -> dict[str, Any]:
        drawing_data     = payload.get("drawing_data") or {}
        retrieved_laws   = payload.get("retrieved_laws") or []
        active_object_ids = list(payload.get("active_object_ids") or [])

        if isinstance(workflow_results, dict) and workflow_results.get("error"):
            return {
                "domain":       self.domain,
                "current_step": "error",
                "drawing_data": drawing_data,
                "retrieved_laws": retrieved_laws,
                "review_result": {
                    "is_violation": False,
                    "violations":   [],
                    "suggestions":  [],
                    "referenced_laws": [],
                    "final_message": str(workflow_results.get("error", "Unknown error")),
                },
                "active_object_ids": active_object_ids,
                "received_payload":  payload,
            }

        violations:      list[dict] = []
        suggestions:     list[str]  = []
        pending_fixes:   list[dict] = []
        final_message  = ""
        current_step   = "agent_completed"
        referenced_laws: list[str]  = []

        blocks = workflow_results if isinstance(workflow_results, list) else []

        for block in blocks:
            agent  = block.get("agent")
            result = block.get("result")

            if agent == "query" and isinstance(result, list):
                parts = [r.get("content", "") for r in result[:5] if r.get("content")]
                if parts:
                    context_text = "\n\n---\n\n".join(parts)
                    prompt = (
                        f"다음은 검색된 건축법 및 규정 내용입니다. 이 정보를 바탕으로 사용자의 질문에 자연스럽고 친절하게 요약된 답변을 작성해주세요.\n"
                        f"답변은 Markdown 형식을 사용하여 보기 좋게 정리해주세요.\n\n"
                        f"[검색 결과]\n{context_text}\n\n"
                        f"사용자 질문: {payload.get('message') or payload.get('user_request') or ''}"
                    )
                    from backend.services import llm_service
                    summary = await llm_service.generate_answer([{"role": "user", "content": prompt}])
                    final_message = summary if isinstance(summary, str) else context_text
                    
                    refs = [f"«{r.get('doc_name') or '건축도면'}»#{r.get('chunk_index', '-')}" for r in result[:5]]
                    final_message += f"\n\n---\n[출처] 시방RAG {' · '.join(refs)} (총{len(result)}건)."
                else:
                    final_message = "관련 건축법 조항을 찾지 못했습니다."
                current_step = "query_completed"

            elif agent == "review" and isinstance(result, dict):
                report = result.get("report") or {}
                fixes  = result.get("fixes") or []

                violations    = self._violations_from_report_items(report.get("items") or [])
                pending_fixes = self._build_pending_fixes(fixes)
                suggestions   = [f["description"] for f in pending_fixes if f.get("description")]
                referenced_laws = list(
                    {v.get("legal_reference", "") for v in violations if v.get("legal_reference")}
                )
                total = report.get("total_violations", len(violations))
                final_message = (
                    f"건축 검토 완료: 위반 {total}건. 수정 항목을 확인하고 적용할 항목을 선택하세요."
                )
                current_step = "pending_fix_review"

            elif agent == "action":
                final_message = (
                    json.dumps(result, ensure_ascii=False)
                    if not isinstance(result, str)
                    else result
                )
                current_step = "action_ready"

        if not final_message:
            final_message = "처리가 완료되었습니다."

        return {
            "domain":       self.domain,
            "current_step": current_step,
            "drawing_data": drawing_data,
            "retrieved_laws": retrieved_laws,
            "review_result": {
                "is_violation":   len(violations) > 0,
                "violations":     violations,
                "suggestions":    suggestions,
                "referenced_laws": referenced_laws,
                "final_message":  final_message,
            },
            "pending_fixes":     pending_fixes,
            "active_object_ids": active_object_ids,
            "received_payload":  payload,
        }

    @staticmethod
    def _build_pending_fixes(fixes: list) -> list[dict]:
        """RevisionAgent fixes → HITL용 PendingFix 목록 변환.

        equipment_id 필드: API 스키마(PendingFixResponse) 및 confirm_fixes 호환을 위해
        handle 값을 equipment_id에도 동일하게 설정합니다.
        C# DrawingPatcher는 handle 필드를 우선 사용합니다.
        """
        result = []
        for fix in fixes:
            handle       = fix.get("handle", "")
            v_type       = fix.get("violation_type", "")
            proposed_fix = fix.get("proposed_fix") or {}
            action       = proposed_fix.get("action", "")
            result.append({
                "fix_id":         str(uuid.uuid4()),
                "handle":         handle,          # C# DrawingPatcher 사용
                "equipment_id":   handle,          # API 스키마 호환 (PendingFixResponse)
                "violation_type": str(v_type),
                "action":         str(action.value if hasattr(action, "value") else action),
                "description":    str(
                    proposed_fix.get("reason", "") or proposed_fix.get("note", "")
                ),
                "proposed_fix":   proposed_fix,
            })
        return result

    @staticmethod
    def _violations_from_report_items(items: list) -> list[dict]:
        out = []
        for item in items:
            req = item.get("required_value")
            out.append({
                "object_id":      str(item.get("handle") or ""),
                "violation_type": str(item.get("violation_type") or ""),
                "reason":         str(item.get("reason") or ""),
                "legal_reference": str(item.get("reference_rule") or ""),
                "severity":       str(item.get("severity") or "Minor"),
                "suggestion": (
                    f"required: {req}" if req else str(item.get("reason") or "")
                ),
                "current_value":  str(item.get("current_value") or ""),
                "required_value": str(item.get("required_value") or ""),
            })
        return out

