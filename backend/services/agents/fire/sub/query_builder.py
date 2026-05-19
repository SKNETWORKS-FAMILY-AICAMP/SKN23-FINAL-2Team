"""
File    : backend/services/agents/fire/sub/query_builder.py
Description : CandidateItem[] → RAG 쿼리 목록 생성기.
              QUERY_REGISTRY: (candidate_type, equipment_category) → list[str]
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.agents.fire.sub.candidate_generator import CandidateItem

log = logging.getLogger(__name__)

QUERY_REGISTRY: dict[tuple[str, str], list[str]] = {
    ("spacing", "sprinkler"): [
        "스프링클러 헤드 간격 2.3m 이하 기준 NFSC",
        "스프링클러 헤드 수평거리 설치 기준",
        "프로젝트 시방서 스프링클러 헤드 배치 간격",
    ],
    ("spacing", "detector"): [
        "감지기 설치 간격 수평거리 기준 NFSC",
        "감지기 배치 면적 기준",
    ],
    ("spacing", "hydrant"): [
        "옥내소화전 이격 거리 설치 기준 NFSC",
        "소화전 보행거리 기준",
    ],
    ("spacing", "extinguisher"): [
        "소화기 보행거리 20m 설치 기준 NFSC",
        "소화기 설치 간격 기준",
    ],
    ("height", "detector"): [
        "감지기 부착 높이 설치 높이 기준 천장 NFSC",
        "감지기 설치 높이 구간 기준",
    ],
    ("height", "sprinkler"): [
        "스프링클러 헤드 반사판 높이 기준 NFSC",
    ],
    ("missing", "extinguisher"): [
        "소화기 설치 의무 장소 기준 NFSC",
        "소화기 설치 대상 기준",
    ],
    ("missing", "hydrant"): [
        "옥내소화전 설치 대상 기준 NFSC",
    ],
    ("missing", "detector"): [
        "감지기 설치 의무 대상 기준 NFSC",
    ],
    ("coverage", "extinguisher"): [
        "소화기 보행거리 20m 설치 기준 NFSC",
        "소화기 커버리지 보행거리 20m 기준",
        "소화기 설치 위치 보행거리 기준",
    ],
    ("coverage", "sprinkler"): [
        "스프링클러 헤드 설치 수량 바닥면적 기준",
        "스프링클러 헤드 배치 커버리지 기준",
    ],
    ("attribute", "sprinkler"): [
        "스프링클러 헤드 규격 형식 승인 기준 NFSC",
    ],
    ("attribute", "hydrant"): [
        "소화전 방수량 방수압력 기준 NFSC",
    ],
}


class QueryBuilder:
    """CandidateItem 목록에서 중복 없는 RAG 쿼리 목록을 생성한다."""

    def build_queries(self, candidates: list["CandidateItem"]) -> list[str]:
        seen: set[str] = set()
        queries: list[str] = []
        for candidate in candidates:
            if not candidate.enabled:
                continue
            key = (candidate.candidate_type, candidate.equipment_category)
            for q in QUERY_REGISTRY.get(key) or []:
                if q not in seen:
                    seen.add(q)
                    queries.append(q)
        log.debug("[QueryBuilder] 생성된 쿼리 %d건", len(queries))
        return queries

    def fill_candidate_queries(self, candidates: list["CandidateItem"]) -> None:
        """각 CandidateItem.rag_queries 를 인플레이스 채운다 (후보별 독립 목록, 전역 중복제거 없음)."""
        for candidate in candidates:
            if not candidate.enabled:
                continue
            key = (candidate.candidate_type, candidate.equipment_category)
            candidate.rag_queries = list(QUERY_REGISTRY.get(key) or [])
