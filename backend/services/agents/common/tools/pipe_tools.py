"""
File    : backend/services/agents/common/tools/pipe_tools.py
Author  : 송주엽
Create  : 2026-04-09
Description : 배관 도메인 서브 에이전트 호출용 OpenAI Tool 스키마 정의.

C# AutoCAD 플러그인과의 대응(이름은 CadSllmAgent/PipingToolBridge.cs 의 PipingToolNames 와 동일):
  - call_query_agent: 서버 RAG(python) 전용, 플러그인이 직접 호출하지 않음.
  - call_review_agent: ApiClient + CadDataExtractor 로 전송된 JSON이 입력(백엔드 워크플로).
  - call_action_agent: C# 쪽 Socket(REVIEW_RESULT/APPROVE) 및 수정 JSON 소비.
  - get_cad_entity_info: handle 기준 — 도면 JSON과 동일 스키마(CadDataExtractor).

              ── 사용 예 ──────────────────────────────────────────────────
              from backend.services.agents.common.tools.pipe_tools import (
                  PIPE_SUB_AGENT_TOOLS,
              )
              ─────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-09 (송주엽) : 초기 도구 스키마 정의
    - 2026-04-15 (송주엽) : call_review_agent에서 spec_context·layout_data를 optional로 변경
    - 2026-04-17 (송주엽) : common/tools/ 패키지로 경로 이동
    - 2026-04-19 (송주엽) : CAD 엔티티 상세 조회를 위한 get_cad_entity_info 툴 스키마 추가
"""

PIPE_SUB_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "call_query_agent",
            "description": "AWS RDS에서 배관 시방서 및 설비 스펙 데이터를 하이브리드 RAG로 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색할 배관 규정이나 스펙 쿼리 (예: 급수 배관 행거 간격 기준)",
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
                "배관 도면 데이터를 파싱·검증하고 위반 항목별 수정 대안을 계산한 뒤 "
                "수정 결과가 포함된 최종 리뷰 리포트를 반환합니다. "
                "도면이 로드되어 있을 때만 호출하세요. "
                "내부 순서: parser → RAG 검색 → compliance → revision → report."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": (
                            "검토 대상 설비 ID. 시스템 프롬프트에 제공된 설비 목록에서 선택하거나, "
                            "전체 도면 검토 시 'ALL'을 입력하세요."
                        ),
                    },
                },
                # spec_context·layout_data는 workflow_handler가 직접 조회하므로 required에서 제외
                "required": ["target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_action_agent",
            "description": (
                "리뷰 리포트의 수정 좌표·대안을 C# 클라이언트 제어 명령 JSON으로 직렬화합니다. "
                "pending_fixes 목록이 있거나, 사용자가 특정 객체의 이동·삭제·레이어 변경·색상 변경·"
                "블록 교체·텍스트 변경 등 직접적인 CAD 수정 실행을 지시했을 때 호출하세요."
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
    {
        "type": "function",
        "function": {
            "name": "get_cad_entity_info",
            "description": (
                "CAD 도면에서 특정 handle의 엔티티 정보를 상세 조회합니다. "
                "반드시 context에 존재하는 실제 handle 값을 입력하세요. "
                "추측하거나 '선의 핸들값을 입력하세요'와 같은 플레이스홀더를 넣지 마세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "조회할 CAD 객체의 handle 값 (예: '28C')",
                    }
                },
                "required": ["handle"],
            },
        },
    },
]
