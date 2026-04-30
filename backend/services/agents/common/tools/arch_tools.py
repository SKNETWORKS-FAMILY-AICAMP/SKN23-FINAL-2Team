"""
File    : backend/services/agents/common/tools/arch_tools.py
Author  : 김다빈
Create  : 2026-04-15
Description : 건축 도메인 서브 에이전트 호출용 OpenAI Tool 스키마 정의.
              RevisionAction은 DrawingPatcher의 12종 AutoFix 타입과 1:1 대응.

              ── 사용 예 ──────────────────────────────────────────────────
              from backend.services.agents.common.tools.arch_tools import (
                  ARCH_SUB_AGENT_TOOLS,
              )
              ─────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현
    - 2026-04-17 (송주엽) : common/tools/ 패키지로 경로 이동
"""

ARCH_SUB_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "call_query_agent",
            "description": (
                "건축법 시행령·주택건설기준 등의 법규를 하이브리드 RAG로 검색합니다. "
                "특정 조항이나 기준 수치가 필요할 때 호출하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색할 건축법 조항 또는 기준 (예: 방화구획 면적 기준, 복도 최소 폭)",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_review_agent",
            "description": (
                "CAD 도면 데이터를 파싱·검증하고 건축법 위반 항목별 수정 대안을 계산한 뒤 "
                "최종 리뷰 리포트를 반환합니다. "
                "내부 순서: parser → compliance → revision → report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "focus_area": {
                        "type": "string",
                        "description": (
                            "검토 집중 영역 (예: 방화구획, 피난동선, 계단실 등). "
                            "비워두면 전체 도면을 검토합니다."
                        ),
                    },
                    "spec_context": {
                        "type": "string",
                        "description": "QueryAgent가 검색한 건축법 규정 원문 (없으면 내부에서 RAG 수행)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_action_agent",
            "description": (
                "리뷰 리포트의 수정 대안을 C# DrawingPatcher 제어 명령 JSON으로 직렬화합니다. "
                "handle 기반으로 AutoCAD 엔티티를 직접 수정합니다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "modifications": {
                        "type": "string",
                        "description": "적용할 수정 항목 JSON (없으면 세션의 pending_fixes 전체 사용)",
                    }
                },
                "required": [],
            },
        },
    },
]
