"""
File    : backend/services/graph/nodes/annotation_gen.py
Author  : 양창일
WBS     : AI-01, RES-03, RES-04
Create  : 2026-04-03
Description :
    LangGraph 공통 노드 — 에이전트 위반 결과를 받아
    AutoCAD RevCloud 및 MText 주석 생성에 필요한 데이터를 만든다.

    출력 형식 (C# ResultHandler.cs가 소비):
        annotations: [
            {
                "violation_id": "...",
                "type": "revcloud" | "mtext",
                "layer": "AI_REVIEW",
                "color_aci": 1,          # Critical=1(빨강) / Major=30(주황) / Minor=50(노랑)
                "coordinates": {...},    # 위반 객체 좌표
                "text": "KEC 142.6 위반: 접지선 4.0SQ 이상 필요",
                "source_type": "law" | "spec"   # 법규=빨강 / 시방서=파랑 (DOC-06)
            }
        ]

TODO(양창일):
    - 심각도(severity) → ACI 색상 매핑 완성
    - 시방서 위반은 color_aci=5(파랑) 으로 분리 (DOC-06)
    - 도면 축척에 맞는 MText 폰트 크기 자동 계산
"""

from backend.services.graph.state import GraphState

SEVERITY_COLOR = {
    "critical": 1,   # 빨강 (ACI 1)
    "major": 30,     # 주황 (ACI 30)
    "minor": 50,     # 노랑 (ACI 50)
    "spec": 5,       # 파랑 (시방서 위반, ACI 5)
}


async def annotation_gen_node(state: GraphState) -> GraphState:
    """
    agent_results의 위반 목록을 RevCloud/MText 주석 데이터로 변환한다.
    """
    agent_results = state.get("agent_results", [])
    annotations = []

    for result in agent_results:
        for violation in result.get("violations", []):
            source_type = violation.get("source_type", "law")
            severity = violation.get("severity", "minor").lower()
            color = SEVERITY_COLOR.get("spec" if source_type == "spec" else severity, 50)

            annotations.append({
                "violation_id": violation.get("id", ""),
                "type": "revcloud",
                "layer": "AI_REVIEW",
                "color_aci": color,
                "coordinates": violation.get("coordinates", {}),
                "text": _build_annotation_text(violation),
                "source_type": source_type,
            })

    all_violations = [v for r in agent_results for v in r.get("violations", [])]
    all_sources = [s for r in agent_results for s in r.get("sources", [])]

    return {
        **state,
        "annotations": annotations,
        "violations": all_violations,
        "sources": all_sources,
        "final_reply": _build_summary(agent_results),
    }


def _build_annotation_text(violation: dict) -> str:
    """주석 텍스트 생성. 예: 'KEC 142.6 위반: 접지선 4.0SQ 이상 필요'"""
    code = violation.get("code", "")
    description = violation.get("description", "")
    action = violation.get("recommended_action", "")
    text = f"{code} 위반: {description}"
    if action:
        text += f"\n권장: {action}"
    return text


def _build_summary(agent_results: list) -> str:
    """최종 요약 메시지 생성. TODO(양창일): 포맷 정교화"""
    total = sum(len(r.get("violations", [])) for r in agent_results)
    return f"검토 완료 — 위반 사항 {total}건 발견"
