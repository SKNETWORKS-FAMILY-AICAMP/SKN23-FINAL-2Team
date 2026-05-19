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
            equipment_id = v.get("equipment_id") or v.get("object_id") or v.get("handle") or "UNKNOWN"
            item = {
                "equipment_id": equipment_id,
                "object_id": v.get("object_id") or equipment_id,
                "violation_type": v.get("violation_type", "general_error"),
                "severity": v.get("severity", "High"),
                "description": v.get("reason", ""),
                "required_value": v.get("required_value", ""),
                "current_value": v.get("current_value", ""),
                "reference": v.get("reference_rule") or v.get("legal_reference", ""),
                "reference_rule": v.get("reference_rule") or v.get("legal_reference", ""),
                "legal_reference": v.get("legal_reference", ""),
                "reason": v.get("reason", ""),
                "suggestion": v.get("suggestion") or v.get("reason", ""),
            }
            for key in (
                "category",
                "bbox",
                "target_bbox",
                "ref_bbox",
                "midpoint",
                "affected_handles",
                "terminal_candidate_id",
                "confidence_score",
                "confidence_reason",
                "modification_tier",
            ):
                if key in v:
                    item[key] = v[key]
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
