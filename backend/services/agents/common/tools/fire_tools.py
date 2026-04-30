"""
File    : backend/services/agents/common/tools/fire_tools.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방 도메인 서브 에이전트 호출용 LangChain @tool 스텁 및 OpenAI Tool 스키마 정의.

              workflow_handler.py 가 tool 이름(string) 기반으로 서브 에이전트를 직접 디스패치하므로
              @tool 함수는 LLM 바인딩용 스텁으로 사용됩니다.

              ── 사용 예 ──────────────────────────────────────────────────
              from backend.services.agents.common.tools.fire_tools import (
                  FIRE_SUB_AGENT_TOOLS,
                  call_query_agent,
                  call_review_agent,
                  call_action_agent,
              )
              ─────────────────────────────────────────────────────────────

Modification History :
    - 2026-04-15 (김민정) : 소방 서브 에이전트 호출 스키마 최초 작성
    - 2026-04-17 (송주엽) : common/tools/ 패키지로 경로 이동
"""

from langchain_core.tools import tool


@tool
def call_query_agent(query: str):
    """
    국가화재안전기준(NFSC) 및 소방 시방서 데이터를 RAG를 통해 조회합니다.
    조회할 규정이나 기준 관련 질문을 입력으로 받습니다.
    """
    pass


@tool
def call_review_agent(target_id: str = "ALL"):
    """
    도면 내 소방 설비들에 대해 NFSC 기준 준수 여부를 종합 검토하고 위반사항을 분석합니다.
    검토할 설비 ID를 입력받으며, 전체 도면 검토 시 ALL을 사용합니다.
    """
    pass


@tool
def call_action_agent(modifications: str = ""):
    """
    분석된 위반사항에 대해 도면 수정 명령(RevCloud, 마킹 등)을 생성하여 C# 클라이언트로 전송 대기합니다.
    수정 대상 및 명령 정보를 포함한 JSON 데이터를 입력으로 받습니다.
    """
    pass


@tool
def get_cad_entity_info(handle: str):
    """
    CAD 도면에서 특정 handle의 엔티티 정보를 상세 조회합니다.
    """
    pass


# LLM 바인딩용 LangChain tool 목록
FIRE_LANGCHAIN_TOOLS = [
    call_query_agent,
    call_review_agent,
    call_action_agent,
    get_cad_entity_info,
]

# workflow_handler / llm_service.generate_answer 호출 시 사용하는 OpenAI Tool Dict 스키마
FIRE_SUB_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "call_query_agent",
            "description": "국가화재안전기준(NFSC) 및 소방 시방서 데이터를 RAG를 통해 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "조회할 규정이나 기준 관련 질문"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_review_agent",
            "description": "도면 내 소방 설비들에 대해 NFSC 기준 준수 여부를 종합 검토하고 위반사항을 분석합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "검토 대상 설비 ID. 전체 도면 검토 시 'ALL'을 입력하세요.",
                    }
                },
                "required": ["target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_action_agent",
            "description": "분석된 위반사항에 대해 도면 수정 명령(RevCloud, 마킹 등)을 생성하여 C# 클라이언트로 전송 대기합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "modifications": {
                        "type": "string",
                        "description": "수정 대상 및 명령 정보를 포함한 JSON 데이터",
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
            "description": "CAD 도면에서 특정 handle의 엔티티 정보를 상세 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "조회할 CAD 객체의 handle 값",
                    }
                },
                "required": ["handle"],
            },
        },
    },
]
