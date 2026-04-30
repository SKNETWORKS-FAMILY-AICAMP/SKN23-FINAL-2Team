"""
File    : backend/services/agents/base.py
Author  : 양창일
Create  : 2026-04-07
Description : 각 도메인 agent가 공통으로 상속받는 기본 클래스

Modification History :
    - 2026-04-07 (김지우) : 초기 구조 생성
    - 2026-04-08 (양창일) : 도메인 공통 stub 실행 로직 정리
    - 2026-04-13 (양창일) : 상태 반영용 구조화 실행 결과 반환 추가
    - 2026-04-13 (양창일) : 도메인별 review_result 생성 서비스 연동
"""

from typing import Any

from backend.services.review_service import build_review_result


class BaseAgent:
    domain: str = "unknown"
    review_label: str = "review"

    async def run(self, payload: dict[str, Any], db=None) -> dict[str, Any]:
        drawing_data = payload.get("drawing_data") or {}
        retrieved_laws = payload.get("retrieved_laws") or []
        active_object_ids = list(payload.get("active_object_ids") or [])
        final_message = str(
            payload.get("assistant_message")
            or payload.get("final_message")
            or f"{self.review_label} completed."
        )

        review_result = payload.get("review_result") or build_review_result(
            drawing_data=drawing_data,
            retrieved_laws=retrieved_laws,
            active_object_ids=active_object_ids,
            review_label=self.review_label,
            domain=self.domain,
        )
        review_result["final_message"] = final_message or review_result["final_message"]

        return {
            "domain": self.domain,
            "current_step": "review_completed",
            "drawing_data": drawing_data,
            "retrieved_laws": retrieved_laws,
            "review_result": review_result,
            "active_object_ids": active_object_ids,
            "received_payload": payload,
        }
