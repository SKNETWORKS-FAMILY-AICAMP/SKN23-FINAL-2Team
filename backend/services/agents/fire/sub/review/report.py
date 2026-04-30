"""
File    : backend/services/agents/fire/sub/review/report.py
Author  : 김민정
Create  : 2026-04-15
Description : 검토 결과를 최종 리포트 형식으로 정리합니다.

Modification History:
    - 2026-04-15 (김민정) : 위반 데이터 및 통계 정보를 포함한 리포트 생성 로직 구현
    - 2026-04-23       : piping 방식과 동일하게 통일 (generate 메서드, items 구조)
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
        for v in violations:
            vtype = v.get("violation_type", "UNKNOWN")
            counts[vtype] = counts.get(vtype, 0) + 1
        return counts

    @staticmethod
    def _format_item(violation: dict, seq: int) -> dict:
        return {
            "seq": seq,
            "handle": violation.get("handle") or violation.get("equipment_id"),
            "equipment_id": violation.get("equipment_id"),
            "violation_type": violation.get("violation_type"),
            "reference_rule": violation.get("reference_rule"),
            "current_value": violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "reason": violation.get("reason"),
            "severity": violation.get("severity", "WARNING"),
        }
