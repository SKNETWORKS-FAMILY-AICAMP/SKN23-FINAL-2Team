"""
File    : mcp_server/tools/regulation_tools.py
Author  : 송주엽
WBS     : DB-02
Create  : 2026-04-03
Description :
    MCP 툴 — 규정 코드로 단건 조회.
    에이전트가 위반 항목의 근거 조항 원문을 가져올 때 사용한다.
    예: "KEC-142.6" 코드로 조회 → 조항 제목 + 전문 반환

TODO(송주엽):
    - regulations 테이블에서 code 컬럼 조회 구현
    - 조항 원문 링크(URL) 필드 추가
"""


def lookup_regulation(code: str, domain: str) -> dict:
    """
    규정 코드로 조항 상세 내용을 반환한다.

    Args:
        code   : 규정 코드 (예: "KEC-142.6", "건축법-49조")
        domain : 도메인 ("전기" | "건축" | "소방" | "배관")

    Returns:
        {"code": str, "title": str, "content": str, "source": str}

    TODO(송주엽): DB 조회 구현
    """
    raise NotImplementedError(f"lookup_regulation({code}) — TODO(송주엽)")
