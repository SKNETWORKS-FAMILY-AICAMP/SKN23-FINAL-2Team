"""
File    : mcp_server/tools/rag_tools.py
Author  : 송주엽
WBS     : DB-02, DB-03, DB-05
Create  : 2026-04-03
Description :
    MCP 툴 — pgvector 기반 법규 RAG 검색.
    에이전트가 도면 위반 여부를 판단하기 위해 관련 법규 조항을 검색한다.

    검색 대상 컬렉션 (DB-05):
        kec      → 전기 에이전트 (KEC 한국전기설비규정)
        building → 건축 에이전트 (건축법)
        nfsc     → 소방 에이전트 (NFSC/NFPA)
        wps      → 배관 에이전트 (WPS/ASME)

    검색 방식:
        pgvector cosine similarity, top-k=5
        검색 결과: 조항 내용 + 출처(법규명, 조항번호, 페이지) 포함

TODO(송주엽):
    - pgvector DB 연결 및 코사인 유사도 쿼리 구현 (DB-03)
    - 임베딩 모델 선택 후 쿼리 임베딩 생성 (nomic-embed-text 또는 bge-m3)
    - Hit@k, MRR 평가 스크립트 연동 (EVAL-03)
"""


def search_regulation(query: str, domain: str, top_k: int = 5) -> list[dict]:
    """
    법규 DB에서 질의와 가장 유사한 조항을 검색하여 반환한다.

    Args:
        query  : 검색 질의 (예: "접지선 최소 단면적")
        domain : 도메인 ("전기" | "건축" | "소방" | "배관")
        top_k  : 반환할 최대 결과 수

    Returns:
        [{"content": str, "source": str, "code": str, "score": float}]

    TODO(송주엽): pgvector 실제 검색 구현
    """
    return []
