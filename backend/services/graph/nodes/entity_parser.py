"""
File    : backend/services/graph/nodes/entity_parser.py
Author  : 양창일
WBS     : AI-01, EXT-02
Create  : 2026-04-03
Description :
    LangGraph 공통 노드 — C#에서 전달된 CAD JSON을 파싱하여
    에이전트가 사용할 수 있는 구조화된 엔티티로 변환한다.

    처리 대상 (요구사항 EXT-02):
        - Layer 목록 및 Entity 수
        - Entity 종류별 추출: LINE, ARC, CIRCLE, POLYLINE, BLOCK, MTEXT, DIMENSION
        - Block Reference의 Attribute (Tag/Value)
        - Dimension 실측값

    입력  : GraphState.cad_json  (C# WebViewMessageHandler가 직렬화한 JSON)
    출력  : GraphState.entities  (파싱된 구조체)

TODO(양창일):
    - 각 Entity 타입별 파싱 로직 구현
    - 블록 속성(Attribute) Tag/Value 매핑 구현
    - 치수(Dimension) 실측값 추출 구현
"""

from backend.services.graph.state import GraphState


async def entity_parser_node(state: GraphState) -> GraphState:
    """
    CAD JSON → 구조화 엔티티 변환 노드.
    """
    cad_json = state.get("cad_json", {})

    entities = {
        "layers": cad_json.get("layers", []),
        "lines": _filter(cad_json, "LINE"),
        "arcs": _filter(cad_json, "ARC"),
        "circles": _filter(cad_json, "CIRCLE"),
        "polylines": _filter(cad_json, "POLYLINE"),
        "blocks": _parse_blocks(cad_json),
        "texts": _filter(cad_json, "MTEXT"),
        "dimensions": _parse_dimensions(cad_json),
    }

    return {**state, "entities": entities}


def _filter(cad_json: dict, entity_type: str) -> list:
    return [e for e in cad_json.get("entities", []) if e.get("type") == entity_type]


def _parse_blocks(cad_json: dict) -> list:
    """Block Reference + Attribute Tag/Value 추출. TODO(양창일): 구현"""
    return [e for e in cad_json.get("entities", []) if e.get("type") == "BLOCK"]


def _parse_dimensions(cad_json: dict) -> list:
    """Dimension 실측값 추출. TODO(양창일): measurement 필드 파싱"""
    return [e for e in cad_json.get("entities", []) if e.get("type") == "DIMENSION"]
