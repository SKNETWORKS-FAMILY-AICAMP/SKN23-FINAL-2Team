"""
File    : backend/services/agents/architecture/sub/query.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-04-25 (focus_area 파라미터 추가 → ArchRagEngine 전달)

Description :
    건축법 시방서/법규 검색 에이전트.
    공용 RagEngine을 통해 멀티-쿼리 분기 검색을 수행합니다.

    검색 흐름 (RagEngine 내부):
        메인 쿼리 + 도메인 고정 서브쿼리(arch 10개, focus_area 지정 시 관련 그룹만)
          → 각 쿼리 병렬 하이브리드 검색 (Dense + Sparse → RRF)
          → 후보 취합 + 중복 제거
          → Reranker (BGE-Reranker-v2-m3, 메인 쿼리 기준 1회)
          → 최종 final_limit개 반환

    focus_area 전달 경로:
        ArchWorkflowHandler.call_review_agent()
          → QueryAgent.execute(focus_area=focus_area)
            → ArchRagEngine.retrieve(focus_area=focus_area)
              → _filter_sub_queries(focus_area)  ← DB 호출 최대 64% 절감

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현 (직접 hybrid_search 호출)
    - 2026-04-15 (김다빈) : RagEngine으로 교체 (멀티-쿼리 + 공용화)
    - 2026-04-25 (김다빈) : focus_area 파라미터 추가 — ArchRagEngine 서브쿼리 필터링 연동
"""

from sqlalchemy.orm import Session

from backend.services.agents.arch.rag_engine import ArchRagEngine


class QueryAgent:
    FINAL_LIMIT = 8   # Reranker 최종 반환 수

    def __init__(self, db: Session):
        self.db = db
        self._engine = ArchRagEngine(db)

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "arch",
        limit: int | None = None,
        focus_area: str = "",
    ) -> list[dict]:
        """
        건축법 법규를 멀티-쿼리 하이브리드 검색으로 조회합니다.

        Parameters
        ----------
        query      : 메인 검색 쿼리 (예: "방화구획 면적 기준 건축법 시행령")
        spec_guid  : 영구 시방서 특정 문서 ID (None = arch 도메인 전체 검색)
        org_id     : 임시 시방서 조직 ID (None = 임시 시방서 검색 생략)
        domain     : 법규 도메인 (기본 "arch"; ArchRagEngine._ARCH_DB_DOMAINS 로 확장됨)
        limit      : 최종 반환 개수 (기본값: FINAL_LIMIT=8)
        focus_area : 검토 집중 영역 힌트 — _FOCUS_GROUPS 키워드와 매칭해
                     연관 서브쿼리만 실행함으로써 DB 호출을 최대 64% 절감.
                     빈 문자열이면 전체 10개 서브쿼리 실행 (하위 호환).

        Returns
        -------
        list[dict] — {source, document_id, content, domain,
                       category, doc_name, section_id, chunk_index}
        """
        return await self._engine.retrieve(
            main_query=query,
            document_id=spec_guid,
            spec_guid=spec_guid,
            org_id=org_id,
            final_limit=limit or self.FINAL_LIMIT,
            focus_area=focus_area,
        )
