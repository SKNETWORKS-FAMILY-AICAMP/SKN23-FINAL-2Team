"""
File    : backend/services/agents/piping/sub/review/report.py
Author  : 송주엽
Create  : 2026-04-09
Description : ComplianceAgent의 위반 목록을 구조화된 리뷰 리포트로 포맷팅

Modification History :
    - 2026-04-09 (송주엽) : 위반 항목 구조화 리뷰 서식 템플릿 구현
    - 2026-04-29 (송주엽) : confidence_score/confidence_reason 포함, low_confidence_count 통계 추가
"""

from datetime import datetime, timezone


class ReportAgent:
    def generate(self, violations: list, drawing_id: str = "") -> dict:
        """
        위반 목록을 받아 요약 통계와 항목별 상세 리포트를 반환합니다.
        """
        summary = self._summarize(violations)
        items = [self._format_item(v, idx + 1) for idx, v in enumerate(violations)]

        return {
            "drawing_id": drawing_id,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_violations": len(violations),
            "summary": summary,
            "items": items,
        }

    @staticmethod
    def _summarize(violations: list) -> dict:
        counts: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        by_source: dict[str, int] = {}
        low_confidence = 0
        for v in violations:
            vtype = v.get("violation_type", "UNKNOWN")
            counts[vtype] = counts.get(vtype, 0) + 1

            sc = v.get("confidence_score")
            if isinstance(sc, (int, float)) and sc < 0.7:
                low_confidence += 1

            # 도메인 추론 (equipment_id 레이어 or reason 키워드)
            reason = str(v.get("reason") or "")
            if "가스" in reason or "GAS" in reason.upper():
                dom = "GAS"
            elif "소화" in reason or "FIRE" in reason.upper() or "스프링클러" in reason:
                dom = "FIRE"
            elif "급수" in reason or "급탕" in reason or "WATER" in reason.upper():
                dom = "WATER"
            elif "냉난방" in reason or "HVAC" in reason.upper():
                dom = "HVAC"
            else:
                dom = "PIPE"
            by_domain[dom] = by_domain.get(dom, 0) + 1

            # 소스 (deterministic vs llm)
            src = v.get("_source", "llm")
            by_source[src] = by_source.get(src, 0) + 1

        return {
            "by_type":            counts,
            "by_domain":          by_domain,
            "by_source":          by_source,
            "low_confidence_count": low_confidence,
        }

    @staticmethod
    def _format_item(violation: dict, seq: int) -> dict:
        item = {
            "seq": seq,
            "equipment_id":   violation.get("equipment_id"),
            "violation_type": violation.get("violation_type"),
            "reference_rule": violation.get("reference_rule"),
            "current_value":  violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "reason":         violation.get("reason"),
            # Phase 1: confidence_score — UI 필터링용 (0.0=불확실, 1.0=확정)
            "confidence_score":  violation.get("confidence_score", 1.0),
            "confidence_reason": violation.get("confidence_reason", ""),
            # 위반 위치 — RevCloud / UI 하이라이트용
            "position":       violation.get("position"),
            # 검출 소스 — "llm" | "deterministic"
            "_source":        violation.get("_source", "llm"),
        }
        pa = violation.get("proposed_action")
        if isinstance(pa, dict) and pa:
            item["proposed_action"] = pa
        return item
