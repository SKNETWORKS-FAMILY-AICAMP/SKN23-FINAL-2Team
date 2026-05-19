"""
File    : backend/services/agents/elec/sub/query_builder.py
Description : 전기 검토 후보를 RAG 검색 쿼리 목록으로 변환합니다.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.agents.elec.sub.candidate_generator import ElecCandidateItem

log = logging.getLogger(__name__)


QUERY_REGISTRY: dict[tuple[str, str], list[str]] = {
    ("continuity", "continuity"): [
        "전선 접속 회로 연속성 배선 접속 기준 KEC",
        "전기 배선 접속함 접속부 시공 기준",
    ],
    ("grounding", "grounding"): [
        "KEC 접지시스템 접지선 굵기 접지저항 접지극 기준",
        "접지봉 접지도체 피뢰설비 외함 접지 기준 KEC 140",
        "1종 2종 접지 기존 용어 KEC 접지시스템 대응",
    ],
    ("high_voltage", "high_voltage"): [
        "고압 수변전설비 이격거리 차단기 보호 기준 KEC",
        "22.9kV 수변전 큐비클 전기설비 이격 기준",
    ],
    ("conduit", "conduit"): [
        "전선관 점유율 전선관 굵기 선정 기준 KEC",
        "케이블트레이 전선관 설치 이격 기준",
    ],
    ("conduit_clearance", "conduit"): [
        "케이블트레이 전선관 상호 이격거리 설치 기준",
        "전선관 배관 이격 교차 설치 기준 KEC",
    ],
    ("panel", "panel"): [
        "분전반 배전반 설치 위치 접근 공간 기준",
        "전기 패널 차단기 정격 회로 표시 기준",
    ],
    ("panel_clearance", "panel"): [
        "분전반 전면 유지관리 공간 이격거리 기준",
        "배전반 전면 작업공간 확보 기준 KEC",
    ],
    ("cable", "cable"): [
        "전선 규격 색상 상별 표시 기준 KEC",
        "전선 굵기 선정 허용전류 기준",
    ],
    ("cable_ampacity", "cable"): [
        "저압 전선 허용전류 전압강하 한도 KEC 기준",
        "전선 굵기 차단기 정격 허용전류 선정 기준",
    ],
    ("breaker_coordination", "breaker"): [
        "차단기 정격 전선 굵기 협조 허용전류 기준",
        "MCCB ELB 차단기 용량 전선 규격 선정 기준",
    ],
}

DEFAULT_QUERY = "전기 도면 검토 접지 전선 차단기 전선관 분전반 기준 KEC"


class ElecQueryBuilder:
    """ElecCandidateItem 목록에서 중복 없는 RAG 쿼리 목록을 생성합니다."""

    def build_queries(
        self,
        candidates: list["ElecCandidateItem"],
        *,
        fallback_query: str | None = None,
        max_queries: int = 8,
    ) -> list[str]:
        seen: set[str] = set()
        queries: list[str] = []

        def _add(query: str | None) -> None:
            if not query:
                return
            q = " ".join(str(query).split())
            if not q or q in seen:
                return
            seen.add(q)
            queries.append(q)

        for candidate in candidates or []:
            if not candidate.enabled:
                continue
            keys = [
                (candidate.candidate_type, candidate.elec_category),
                (candidate.candidate_type, candidate.evidence.elec_category),
            ]
            for key in keys:
                for query in QUERY_REGISTRY.get(key) or []:
                    _add(query)
        _add(fallback_query)
        if not queries:
            _add(DEFAULT_QUERY)

        out = queries[:max_queries]
        log.debug("[ElecQueryBuilder] queries=%d", len(out))
        return out

    def fill_candidate_queries(self, candidates: list["ElecCandidateItem"]) -> None:
        for candidate in candidates or []:
            keys = [
                (candidate.candidate_type, candidate.elec_category),
                (candidate.candidate_type, candidate.evidence.elec_category),
            ]
            queries: list[str] = []
            seen: set[str] = set()
            for key in keys:
                for query in QUERY_REGISTRY.get(key) or []:
                    if query not in seen:
                        seen.add(query)
                        queries.append(query)
            candidate.rag_queries = queries
