"""
File    : mcp_server/tools/cad_tools.py
Author  : 양창일
WBS     : EXT-01
Create  : 2026-04-03
Description :
    MCP 툴 — C#에서 전달된 CAD JSON을 파싱하여 구조화된 엔티티 요약을 반환한다.
    AutoCAD 플러그인이 도면 데이터 추출 후 이 툴을 호출한다.

    처리 항목 (EXT-02):
        - Layer 목록 및 Entity 수
        - Entity 종류별: LINE, ARC, CIRCLE, POLYLINE, BLOCK, MTEXT, DIMENSION
        - Block Attribute (Tag/Value)
        - Dimension 실측값

TODO(양창일):
    - 엔티티 타입별 상세 파싱 로직 구현
    - 추출 소요 시간 측정 및 반환
"""

from typing import Any


def extract_cad_data(cad_json: dict[str, Any]) -> dict[str, Any]:
    """
    CAD JSON을 파싱하여 구조화된 요약을 반환한다.

    Args:
        cad_json: C# WebViewMessageHandler가 직렬화한 도면 JSON
            {
              "layers": [...],
              "entities": [{"type": "LINE|ARC|...", "layer": "...", ...}]
            }

    Returns:
        {
          "layer_count": int,
          "entity_count": int,
          "by_type": {"LINE": N, "BLOCK": N, ...},
          "layers": [...],
          "summary": str
        }
    """
    layers = cad_json.get("layers", [])
    entities = cad_json.get("entities", [])

    by_type: dict[str, int] = {}
    for e in entities:
        t = e.get("type", "UNKNOWN")
        by_type[t] = by_type.get(t, 0) + 1

    return {
        "layer_count": len(layers),
        "entity_count": len(entities),
        "by_type": by_type,
        "layers": layers,
        "summary": f"레이어 {len(layers)}개, 엔티티 {len(entities)}개 감지됨 ({by_type})",
    }
