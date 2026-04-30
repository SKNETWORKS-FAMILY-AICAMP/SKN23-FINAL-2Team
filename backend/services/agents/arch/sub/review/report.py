"""
File    : backend/services/agents/architecture/sub/review/report.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15

Description :
    ComplianceAgent의 위반 목록을 구조화된 건축 검토 리포트로 포맷팅.
    severity(Critical / Major / Minor) 기반 통계를 포함합니다.
"""

from datetime import datetime, timezone


class ReportAgent:
    def generate(self, violations: list, focus_area: str = "") -> dict:
        summary = self._summarize(violations)
        items   = [self._format_item(v, idx + 1) for idx, v in enumerate(violations)]

        return {
            "focus_area":       focus_area or "전체 도면",
            "generated_at":     datetime.now(tz=timezone.utc).isoformat(),
            "total_violations": len(violations),
            "summary":          summary,
            "items":            items,
        }

    @staticmethod
    def _summarize(violations: list) -> dict:
        by_type:     dict[str, int] = {}
        by_severity: dict[str, int] = {"Critical": 0, "Major": 0, "Minor": 0}

        for v in violations:
            vtype = v.get("violation_type", "UNKNOWN")
            by_type[vtype] = by_type.get(vtype, 0) + 1

            sev = v.get("severity", "Minor")
            if sev in by_severity:
                by_severity[sev] += 1

        return {"by_type": by_type, "by_severity": by_severity}

    @staticmethod
    def _format_item(violation: dict, seq: int) -> dict:
        return {
            "seq":            seq,
            "handle":         violation.get("handle"),
            "entity_type":    violation.get("entity_type"),
            "layer":          violation.get("layer"),
            "violation_type": violation.get("violation_type"),
            "reference_rule": violation.get("reference_rule"),
            "current_value":  violation.get("current_value"),
            "required_value": violation.get("required_value"),
            "severity":       violation.get("severity", "Minor"),
            "reason":         violation.get("reason"),
        }
