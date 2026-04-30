"""
CAD 수정 4단계 프로토콜 (Safe / Moderate / Controlled / Danger)
- LLM·백엔드가 auto_fix(또는 action)에 맞춰 modification_tier(1~4)를 붙이거나, 여기서 추론한다.
"""
from __future__ import annotations

# C# DrawingPatcher AutoFix.type + 2/3단계 전용
_TIER1_TYPES = frozenset(
    "ATTRIBUTE LAYER TEXT_CONTENT TEXT_HEIGHT COLOR LINETYPE LINEWEIGHT DELETE".split()
)
# 이동·회전: 원본 entity에 Matrix 적용(직접 수정). 4단계(제안 복제)와 구분
_TIER2_TYPES = frozenset("BLOCK_REPLACE ROTATE MOVE SCALE".split())
_TIER3_TYPES = frozenset("DYNAMIC_BLOCK_PARAM".split())
_CREATE_TYPES = frozenset("CREATE_ENTITY CREATE_LINE CREATE_CIRCLE CREATE_POLYLINE CREATE_BLOCK CREATE_TEXT".split())
_TIER4_TYPES = frozenset("GEOMETRY RECTANGLE_RESIZE STRETCH_RECT".split())  # 자유형상 좌표 직접 수정 — 원본 보존+AI_PROPOSAL


def infer_modification_tier(proposed: dict | None, action: str | None = None) -> int:
    """
    modification_tier 미지정 시 auto_fix / action으로 추론.
    1=속성, 2=블록/심볼(비구속), 3=동적블록 파라미터, 4=자유형상(제안 레이어)
    """
    if proposed and proposed.get("modification_tier") in (1, 2, 3, 4):
        return int(proposed["modification_tier"])
    type_str = str((proposed or {}).get("type", "") or "").strip().upper()
    action_str = str(action or "").strip().upper()
    t = type_str or action_str
    if t in _TIER1_TYPES:
        return 1
    if t in _TIER2_TYPES:
        return 2
    if t in _TIER3_TYPES:
        return 3
    if t in _CREATE_TYPES:
        return 2
    if t in _TIER4_TYPES or t in ("RESIZE",):
        return 4
    a = (action or "").strip().upper()
    if a in ("ROUTE_CHANGE", "WALL_MOVE", "FREE_GEOMETRY", "STRETCH"):
        return 4
    return 1


def merge_autofix_with_tier(af: dict | None, proposed: dict | None, action: str | None) -> dict | None:
    """C#로 보내는 auto_fix dict에 modification_tier를 병합한다."""
    if not af:
        return None
    out = dict(af)
    out["modification_tier"] = infer_modification_tier(
        {**(proposed or {}), **out}, action
    )
    return out
