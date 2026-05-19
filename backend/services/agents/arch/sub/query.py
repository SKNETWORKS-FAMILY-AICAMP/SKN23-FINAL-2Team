"""
File    : backend/services/agents/architecture/sub/query.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-05-11 (table_markdown 우선 context 적용)

Description :
    건축법 시방서/법규 검색 에이전트.
    공용 RagEngine을 통해 멀티-쿼리 분기 검색을 수행합니다.
"""

from sqlalchemy.orm import Session

from backend.services.agents.arch.rag_engine import ArchRagEngine


class QueryAgent:
    FINAL_LIMIT = 8

    def __init__(self, db: Session):
        self.db = db
        self._engine = ArchRagEngine(db)

    @staticmethod
    def _normalize_limit(limit, default: int = FINAL_LIMIT) -> int:
        try:
            value = int(limit)
            return max(1, min(value, 50))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _arch_extra_queries(query: str) -> list[str]:
        q = (query or "").lower()
        extra: list[str] = []

        def add_if(triggers: tuple[str, ...], anchors: list[str]) -> None:
            if any(t.lower() in q for t in triggers):
                extra.extend(anchors)

        add_if(
            ("방화구획", "방화", "fire zone", "fire compartment", "내화", "방화문"),
            ["방화구획 면적 기준 건축법 시행령", "방화문 내화구조 차연 차열 성능 기준"],
        )
        add_if(
            ("계단", "피난계단", "특별피난", "직통계단", "stair"),
            ["직통계단 피난계단 특별피난계단 설치 기준 폭 단높이 단너비"],
        )
        add_if(
            ("복도", "통로", "corridor", "피난경로", "유효폭"),
            ["복도 통로 유효폭 피난경로 기준 건축물 피난 방화구조 규칙"],
        )
        add_if(
            ("피난거리", "보행거리", "출구", "exit", "egress"),
            ["피난거리 보행거리 출구 간격 수평거리 기준"],
        )
        add_if(
            ("채광", "환기", "창", "window", "개구부", "거실"),
            ["거실 채광창 환기창 면적 바닥면적 비율 건축법"],
        )
        add_if(
            ("층고", "반자", "ceiling", "높이"),
            ["층고 반자높이 최솟값 거실 복도 계단실 기준"],
        )
        add_if(
            ("장애", "편의", "경사로", "wheelchair", "접근"),
            ["장애인 편의시설 경사로 접근로 설치 기준"],
        )

        extra.extend(["건축법 시행령", "건축물의 피난 방화구조 등의 기준에 관한 규칙", "KCS KS"])
        seen: set[str] = set()
        deduped: list[str] = []
        for item in extra:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped[:6]

    async def execute(
        self,
        query: str,
        spec_guid: str | None = None,
        org_id: str | None = None,
        domain: str = "arch",
        limit: int | None = None,
        focus_area: str = "",
        permanent_category: str | None = None,
        target_doc: str | None = None,
        strict_target_doc: bool = False,
        **kwargs,
    ) -> list[dict]:
        """
        건축법 법규를 멀티-쿼리 하이브리드 검색으로 조회합니다.

        Returns
        -------
        list[dict] — {
            source,
            document_id,
            content,
            raw_content,
            table_markdown,
            chunk_type,
            domain,
            category,
            doc_name,
            section_id,
            chunk_index,
            page_number
        }
        """
        query = (query or "").strip()
        if not query:
            return []

        final_limit = self._normalize_limit(limit)

        return await self._engine.retrieve(
            main_query=query,
            document_id=None,
            spec_guid=spec_guid,
            org_id=org_id,
            extra_queries=self._arch_extra_queries(query),
            final_limit=final_limit,
            focus_area=focus_area,
            domain=domain,
            permanent_category=permanent_category,
            target_doc=target_doc,
            strict_target_doc=strict_target_doc,
        )
