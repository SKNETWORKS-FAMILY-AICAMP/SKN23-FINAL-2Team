"""
File    : backend/services/agents/fire/sub/query.py
Author  : 김민정
Create  : 2026-04-15
Description : 소방 법규 및 기준을 위한 하이브리드 RAG 검색 에이전트.

Modification History:
    - 2026-04-15 (김민정) : 하이브리드 RAG 기반 법규 검색 로직 구현
    - 2026-04-18 (김지우) : llm_service 연동으로 리팩터링
    - 2026-04-19 (김민정) : 누락된 임포트 추가 및 하이브리드 RAG 런타임 오류 수정
    - 2026-04-23       : piping 방식과 동일하게 통일 (AsyncSession, vector_service 기반)
    - 2026-05-04       : QueryRagEngine 도입 — 멀티쿼리·domain 확장·reranker 적용
"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession
from backend.services.llm_service import generate_answer
from backend.services.agents.fire.rag_engine import FireRagEngine


class QueryAgent:
    FIRE_DOMAINS = ("fire", "소방")
    FINAL_LIMIT = 8

    def __init__(self, db: AsyncSession):
        self.db  = db
        self._rag = FireRagEngine(db)

    @staticmethod
    def _normalize_limit(limit, default: int = FINAL_LIMIT) -> int:
        try:
            value = int(limit)
            return max(1, min(value, 50))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fire_extra_queries(query: str) -> list[str]:
        q = (query or "").lower()
        extra: list[str] = []

        def add_if(triggers: tuple[str, ...], anchors: list[str]) -> None:
            if any(t.lower() in q for t in triggers):
                extra.extend(anchors)

        add_if(
            ("스프링클러", "sprinkler", "헤드", "살수", "방수구역", "간격"),
            ["스프링클러 헤드 설치 간격 살수반경 NFSC", "스프링클러 설비 배관 헤드 수량 배치 기준"],
        )
        add_if(
            ("감지기", "detector", "화재감지", "연기", "열감지"),
            ["감지기 설치 높이 감지 면적 설치 간격 NFSC", "자동화재탐지설비 감지기 기준"],
        )
        add_if(
            ("소화전", "hydrant", "옥내소화전", "방수압", "방수량"),
            ["옥내소화전 설치 기준 방수압력 방수량 이격거리 NFSC"],
        )
        add_if(
            ("소화기", "extinguisher", "보행거리", "능력단위"),
            ["소화기 설치 기준 보행거리 능력단위 소방시설법"],
        )
        add_if(
            ("펌프", "pump", "가압송수", "토출", "양정"),
            ["소방펌프 토출압력 양정 유량 기준 NFSC"],
        )
        add_if(
            ("배관", "밸브", "플랜지", "관경", "구경", "압력배관"),
            ["소방 배관 재질 구경 밸브 플랜지 KS KCS NFSC"],
        )
        add_if(
            ("방화문", "방화구획", "내화", "제연", "연기"),
            ["방화문 방화구획 제연설비 소방시설법 건축법 NFSC"],
        )

        extra.extend(["NFSC", "소방시설법 시행령 시행규칙", "화재안전기술기준 KCS KS"])
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
        domain: str = "fire",
        limit: int = 5,
        permanent_category: str | None = None,
        target_doc: str | None = None,
        strict_target_doc: bool = False,
        **kwargs,
    ) -> list[dict]:
        """
        소방 법규(NFSC) / 시방서를 FireRagEngine으로 검색합니다.

        공개 API는 기존과 동일. 내부에서 멀티쿼리·domain 확장·reranker를 적용합니다.
        결과가 없으면 빈 리스트를 반환하며, 상위 레이어에서 안내 메시지를 출력합니다.
        """
        query = (query or "").strip()
        if not query:
            logging.warning("[FireQueryAgent] 빈 query 입력")
            return []

        limit = self._normalize_limit(limit)
        logging.info(
            "[FireQueryAgent] query=%r domain=%s limit=%s target_doc=%r strict=%s category=%r ignored_kwargs=%s",
            query[:120],
            domain,
            limit,
            target_doc,
            strict_target_doc,
            permanent_category,
            sorted(kwargs.keys()) if kwargs else [],
        )

        return await self._rag.retrieve(
            main_query         = query,
            spec_guid          = spec_guid,
            org_id             = org_id,
            domain             = domain,
            extra_queries      = self._fire_extra_queries(query),
            final_limit        = max(limit, self.FINAL_LIMIT),
            include_default_queries = False,
            permanent_category = permanent_category,
            target_doc         = target_doc,
            strict_target_doc  = strict_target_doc,
        )

    async def resolve_terms(self, terms: list) -> dict:
        """
        도면의 레이어/블록명(terms)을 받아 표준 소방 타입으로 매핑합니다.
        """
        if not terms:
            return {}

        search_query = f"소방 도면 기호 및 용어 정의: {', '.join(terms[:10])}"
        knowledge_data = await self.execute(search_query)
        knowledge_context = "\n".join([r["content"] for r in knowledge_data if r.get("content")])

        system_prompt = """
        당신은 소방 설계 도면 해석 전문가입니다.
        [지식 베이스]를 활용하여 [도면 용어 리스트]를 소방 표준 타입으로 매핑하십시오.

        [표준 타입 목록]
        - SPRINKLER_HEAD, FIRE_HYDRANT, FIRE_HYDRANT_BOX, EXTINGUISHER, DETECTOR, ALARM_CONTROL_PANEL, FIRE_WALL, FIRE_DOOR 등

        [출력 스키마]
        { "mapping": { "용어": "표준_타입" } }
        """

        user_prompt = f"""
        [지식 베이스]: {knowledge_context}
        [도면 용어 리스트]: {json.dumps(terms, ensure_ascii=False)}
        """

        try:
            response_data = await generate_answer(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                response_format={"type": "json_object"}
            )
            return response_data.get("mapping", {})
        except Exception as e:
            logging.warning("[FireQueryAgent] 용어 매핑 오류: %s", e)
            return {}
