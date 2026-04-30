"""
소방 NFSC 위반 시나리오를 가진 도면 JSON 샘플을 생성하는 유틸.
실제 도면 JSON(dict)을 받아 deep copy 후 특정 수치를 위반 값으로 변조한다.
원본은 절대 변경하지 않는다.
"""

import copy


def _is_sprinkler(entity: dict) -> bool:
    text = " ".join([
        str(entity.get("block_name") or ""),
        str(entity.get("layer") or ""),
        str(entity.get("type") or ""),
    ]).upper()
    return any(k in text for k in ("SPK", "SPRIN", "헤드", "HEAD"))


def _is_detector(entity: dict) -> bool:
    text = " ".join([
        str(entity.get("block_name") or ""),
        str(entity.get("layer") or ""),
        str(entity.get("type") or ""),
    ]).upper()
    return any(k in text for k in ("DET", "FDH", "감지", "DETECT"))


def _is_hydrant(entity: dict) -> bool:
    text = " ".join([
        str(entity.get("block_name") or ""),
        str(entity.get("layer") or ""),
    ]).upper()
    return any(k in text for k in ("HYD", "소화전", "HYDRANT"))


def _is_pump(entity: dict) -> bool:
    text = " ".join([
        str(entity.get("block_name") or ""),
        str(entity.get("handle") or ""),
        str(entity.get("layer") or ""),
    ]).upper()
    return any(k in text for k in ("PUMP", "펌프", "FP-"))


def _get_position(entity: dict) -> dict:
    return (
        entity.get("center")
        or entity.get("insert_point")
        or entity.get("position")
        or {}
    )


def generate_sprinkler_spacing_violation(drawing_json: dict) -> dict:
    """
    스프링클러 헤드 2개의 간격을 2500mm(NFSC 2.3m 초과)로 벌린다.
    스프링클러 헤드가 2개 미만이면 원본 복사본을 그대로 반환.
    """
    result = copy.deepcopy(drawing_json)
    entities = result.get("entities") or result.get("elements") or []
    spks = [e for e in entities if _is_sprinkler(e)]
    if len(spks) >= 2:
        pos0 = _get_position(spks[0])
        x0 = float(pos0.get("x") or 0)
        y0 = float(pos0.get("y") or 0)
        pos_key = next(
            (k for k in ("center", "insert_point", "position") if k in spks[1]),
            None,
        )
        if pos_key is None:
            spks[1]["center"] = {"x": x0 + 2500.0, "y": y0}
        else:
            spks[1][pos_key]["x"] = x0 + 2500.0
            spks[1][pos_key]["y"] = y0
    return result


def generate_detector_height_violation(drawing_json: dict) -> dict:
    """
    감지기 설치 높이를 5000mm(NFSC 4m 이하 기준 초과)로 변조한다.
    """
    result = copy.deepcopy(drawing_json)
    entities = result.get("entities") or result.get("elements") or []
    for e in entities:
        if _is_detector(e):
            attrs = e.setdefault("attributes", {})
            attrs["INSTALL_HEIGHT"] = "5000"
            attrs["HEIGHT"] = "5000"
    return result


def generate_missing_hydrant_violation(drawing_json: dict) -> dict:
    """
    소화전 엔티티를 전부 제거하여 '필수 설비 누락' 위반을 만든다.
    """
    result = copy.deepcopy(drawing_json)
    for key in ("entities", "elements"):
        if key in result:
            result[key] = [e for e in result[key] if not _is_hydrant(e)]
    return result


def generate_pressure_violation(drawing_json: dict) -> dict:
    """
    소방 펌프 토출 압력을 0.1MPa(NFSC 0.17MPa 이상 기준 미달)로 낮춘다.
    """
    result = copy.deepcopy(drawing_json)
    entities = result.get("entities") or result.get("elements") or []
    for e in entities:
        if _is_pump(e):
            attrs = e.setdefault("attributes", {})
            attrs["PRESSURE"] = "0.1"
    return result
