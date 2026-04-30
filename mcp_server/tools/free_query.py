"""
File    : mcp_server/tools/free_query.py
Author  : 양창일
WBS     : AI-02, AI-03
Create  : 2026-04-03
Description :
    MCP 툴 — 자유질의 처리.
    요구사항 API-07: 자유질의는 FastAPI 직접 호출 대신 MCP 서버 경유.
    EXE-04: AutoCAD 플러그인 텍스트 입력창은 MCP 연결 상태 확인 UI로 대체.

    흐름:
        C# 플러그인 → MCP 서버(이 툴) → LLM → 응답 반환

    AI-03 재질문 로직:
        도메인 분류가 애매한 경우(예: 전기+배관 혼재) LLM에게 재질문 요청

TODO(양창일):
    - LLM 호출 구현 (llm_service.py 재사용 or 직접 httpx 호출)
    - AI-03: 도메인 불확실 감지 → 재질문 메시지 반환 로직 추가
    - 재질문 횟수 제한 (최대 2회) 및 기본값 처리
"""


def free_query(message: str, cad_context: dict | None = None) -> dict:
    """
    도메인 제한 없이 CAD 관련 질의에 답변한다.

    Args:
        message     : 사용자 질의 텍스트
        cad_context : 현재 도면 컨텍스트 (선택)

    Returns:
        {"reply": str, "needs_clarification": bool, "clarification_question": str | None}

    TODO(양창일): LLM 호출 및 재질문 로직 구현
    """
    return {
        "reply": "",
        "needs_clarification": False,
        "clarification_question": None,
    }
