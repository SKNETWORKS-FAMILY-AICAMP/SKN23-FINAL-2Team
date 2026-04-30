"""
File    : backend/services/agents/electric/sub/review/report.py
Author  : 김지우
Description : ComplianceAgent 검증 결과를 바탕으로 사용자에게 제공할 보고서를 포맷팅합니다.
"""

from datetime import datetime

class ReportAgent:
    def generate(self, violations: list, drawing_id: str = "") -> dict:
        items = []
        for v in violations:
            item = {
                "equipment_id": v.get("equipment_id", "UNKNOWN"),
                "violation_type": v.get("violation_type", "general_error"),
                "severity": "High",
                "description": v.get("reason", ""),
                "required_value": v.get("required_value", ""),
                "current_value": v.get("current_value", ""),
                "reference": v.get("reference_rule", ""),
                "reference_rule": v.get("reference_rule", ""),
                "reason": v.get("reason", ""),
            }
            pa = v.get("proposed_action")
            if isinstance(pa, dict) and pa:
                item["proposed_action"] = pa
            items.append(item)
            
        summary = f"총 {len(items)}건의 규정 위반이 발견되었습니다."
        
        return {
            "drawing_id": drawing_id,
            "generated_at": datetime.utcnow().isoformat(),
            "summary": summary,
            "items": items,
        }
