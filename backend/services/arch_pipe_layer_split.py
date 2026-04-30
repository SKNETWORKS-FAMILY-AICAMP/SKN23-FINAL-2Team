"""
건축(A-/S-)·배관(MEP)·보조(aux) 레이어 분리 — 배관 검토 시 건축을 참조 기준으로 둡니다.

스키마 `piping_split_v1` 계약(요약):
  - arch_reference / mep_review: 엔티티에 **layer_role** 필드(arch|mep|aux|unknown)가 붙을 수 있음
  - layer_roles: 도면 내 **레이어명 → 역할** (역할의 단일 조회용)
  - layers_indexed: 레이어를 인덱스로 묶은 요약(토큰 절감·UI용)
  - spatial_hints: arch–MEP bbox **근접** 후보(거리, mm; 토큰 상한 PIPING_SPATIAL_HINTS_MAX)

레이어 역할 결정 우선순위:
  1. DB mapping_rules.layer_role (명시적 오버라이드) — org_id 제공 시
  2. NCS식 M/P 접두(설정) + 정규식 휴리스틱
  3. "unknown" → 검토 풀 포함 여부는 PIPING_REVIEW_INCLUDE_UNKNOWN
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Literal

from backend.services.agents.pipe.sub.mapping import _is_ignored_layer

LayerRole = Literal["arch", "mep", "aux", "unknown"]

_ARCH_PREFIX_RE = re.compile(r"^(A-|S-|AR-|ST-|C-|L-)", re.IGNORECASE)
_NCS_MEP_PREFIX_RE = re.compile(r"^(M-|P-)", re.IGNORECASE)
# 배관·기계/위생 추정 키워드
_MEP_HINT_RE = re.compile(
    r"(GAS|PIPE|PIPING|MEP|HVAC|SPRINK|CWS|HWS|CWR|HWR|"
    r"DRAIN|VAV|TEX$|DUAL|CHW|HOT|COLD|FW|RISER|VALVE|MANHOLE|DUCT|PUMP|"
    r"PFW|HFG|FIRE|FM-|FP-|ELEC-F|"
    r"가스|배관|급수|배수|소화|위생|기계|냉난방|덕트|펌프|밸브|배기|급기|환기)",
    re.IGNORECASE,
)
# 건축/구조 추정 키워드 (접두어가 없는 경우 대비)
_ARCH_HINT_RE = re.compile(
    r"(WALL|SLAB|COL|BEAM|DOOR|WINDOW|ROOM|STAIR|CEIL|FINISH|FURN|BASE|ELEV|SITE|CIVIL|"
    r"벽체|슬라브|기둥|보|문|창문|실명|계단|천장|마감|가구|부지|토목)",
    re.IGNORECASE,
)

# 표제란/도면 정보 추정 키워드 (배관 검토에서 제외)
_TITLE_HINT_RE = re.compile(
    r"(TITLE|FRAME|BORDER|표제|도면명|SCALE|DWG|NO|NAME|DATE|PROJECT|설계|축척|시행|도면번호)",
    re.IGNORECASE,
)

_VALID_ROLES: frozenset[str] = frozenset({"arch", "mep", "aux"})

_MEP_KEYWORDS = frozenset({
    "PIPE", "VALVE", "PUMP", "TANK", "FILTER", "STRAINER", "TRAP", "CHECK",
    "GATE", "BALL", "GLOBE", "BUTTERFLY", "RELIEF", "REGULATOR",
    "GAS", "가스", "배관", "밸브", "펌프", "수조", "탱크",
})

# 텍스트 내용 중 표제란임을 시사하는 키워드
_TITLE_CONTENT_KEYWORDS = frozenset({
    "평면도", "계통도", "단면도", "전개도", "SCALE", "DATE", "도면번호", "DWG NO", "PROJECT"
})

_TITLE_TEXT_RE = re.compile(
    r"평면도|계통도|단면도|전개도|SCALE|DATE|도면번호|DWG\s*NO|PROJECT|도면명|축척",
    re.IGNORECASE,
)
_PIPE_ANNOTATION_TEXT_RE = re.compile(
    r"가스|GAS|LPG|LNG|배관|누설|경보기|차단기|콕|후크콕|레듀샤|밸브|VALVE|"
    r"\bDN\s*\d+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*A\b|\bG\b",
    re.IGNORECASE,
)
_ROOM_LABEL_TEXT_RE = re.compile(
    r"거실|방\d*|침실|안방|주방|식당|욕실|화장실|현관|복도|계단|발코니|베란다|"
    r"다용도실|보일러실|세탁실|창고|드레스룸|팬트리|홀|로비|사무실|회의실|기계실|전기실",
    re.IGNORECASE,
)
_TITLE_GRAPHIC_NEAR_TOL = 120.0
_PIPE_ANNOTATION_GRAPHIC_NEAR_TOL = 260.0


def classify_layer_role(
    name: str,
    db_role_map: dict[str, str] | None = None,
    *,
    ncs_mep_prefix: bool = True,
) -> LayerRole:
    """
    레이어명 → 역할 분류.
    db_role_map: {source_key: "arch"|"mep"|"aux"} — 있으면 DB 규칙을 최우선 적용.
    """
    n = (name or "").strip()
    if not n:
        return "unknown"

    # 1. DB 매핑 규칙 최우선
    if db_role_map:
        db_role = db_role_map.get(n) or db_role_map.get(n.upper())
        if db_role in _VALID_ROLES:
            return db_role  # type: ignore[return-value]

    # 1.4. TEX 레이어는 배관 주석의 핵심이므로 mep로 확정
    if n.upper() == "TEX":
        return "mep"

    # 1.5. 표제란/도면 정보 레이어는 건축이 아니라 검토 제외 메타(aux)입니다.
    if bool(_TITLE_HINT_RE.search(n)):
        return "aux"

    # 2. 접두어 기반 (A-, S-, M-, P- 등)
    if _ARCH_PREFIX_RE.match(n):
        return "arch"
    if ncs_mep_prefix and _NCS_MEP_PREFIX_RE.match(n):
        return "mep"

    # 3. 키워드 기반 (배관/기계 힌트가 있으면 MEP 우선)
    if _MEP_HINT_RE.search(n):
        return "mep"
    
    # 4. 키워드 기반 (건축 힌트)
    if _ARCH_HINT_RE.search(n):
        return "arch"

    if _is_ignored_layer(n):
        return "aux"

    return "unknown"


def _is_grey_color(color: str) -> bool:
    """회색 ACI 색상 판정."""
    _ARCH_ACI_COLORS = {"8", "9", "250", "251", "252", "253", "254", "255"}
    return color in _ARCH_ACI_COLORS


def _score_entity_for_mep_in_l_layer(entity: dict) -> tuple[float, str]:
    """
    L-layer 내 엔티티의 MEP 특성 스코어 계산.

    반환: (mep_score: 0-1, indicator_reason: str)
    - 0.8+: 블록명/텍스트에 MEP 키워드
    - 0.7+: 텍스트에 배관 키워드
    - 0.5+: 유색(회색 아닌) 색상
    - 0.0: MEP 지표 없음
    """
    entity_type = str(entity.get("raw_type") or entity.get("type") or "").upper()

    # 1. 블록/심볼 분석
    if entity_type in ("INSERT", "BLOCK"):
        block_name = str(entity.get("name") or "").upper()
        if any(kw in block_name for kw in _MEP_KEYWORDS):
            return 0.8, f"block_name:{block_name}"

    # 2. 텍스트 내용 분석
    if entity_type in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
        txt = str(entity.get("text") or entity.get("content") or "").upper()
        mep_kw_count = sum(1 for kw in _MEP_KEYWORDS if kw in txt)
        if mep_kw_count >= 1:
            base_score = 0.7
            score = base_score + (0.1 * min(mep_kw_count, 3))
            return min(score, 1.0), f"text_keywords:{mep_kw_count}"

    # 3. 색상은 사용자/프로젝트별 표준 차이가 커서 단독 승격 근거로 쓰지 않는다.
    #    아래 점수는 debug/보조 힌트일 뿐, review 승격 조건으로 사용하지 않는다.
    color = str(entity.get("color") or "")
    if color and not _is_grey_color(color) and color not in ("7", ""):  # 7=흰색/검은색
        return 0.1, f"vivid_color_weak_hint:{color}"

    return 0.0, ""


def _entity_layer(e: dict) -> str:
    return str(e.get("layer") or "")


def entity_layer_role(
    e: dict,
    db_role_map: dict[str, str] | None = None,
    *,
    ncs_mep_prefix: bool = True,
) -> LayerRole:
    return classify_layer_role(
        _entity_layer(e), db_role_map=db_role_map, ncs_mep_prefix=ncs_mep_prefix
    )


def _entity_text(e: dict) -> str:
    return str(e.get("text") or e.get("content") or "").strip()


def _is_title_text_entity(e: dict) -> bool:
    t = str(e.get("raw_type") or e.get("type") or "").upper()
    if t not in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
        return False
    return bool(_TITLE_TEXT_RE.search(_entity_text(e)))


def _is_pipe_annotation_text_entity(e: dict) -> bool:
    t = str(e.get("raw_type") or e.get("type") or "").upper()
    if t not in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
        return False
    return bool(_PIPE_ANNOTATION_TEXT_RE.search(_entity_text(e)))


def _is_room_label_text_entity(e: dict) -> bool:
    t = str(e.get("raw_type") or e.get("type") or "").upper()
    if t not in ("TEXT", "MTEXT"):
        return False
    text = _entity_text(e)
    if not text or _is_pipe_annotation_text_entity(e) or _is_title_text_entity(e):
        return False
    return bool(_ROOM_LABEL_TEXT_RE.search(text))


def _entity_title_extents(e: dict) -> tuple[float, float, float, float] | None:
    b = e.get("bbox")
    if isinstance(b, dict):
        try:
            if "x1" in b:
                return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
            if "min_x" in b:
                return float(b["min_x"]), float(b["min_y"]), float(b["max_x"]), float(b["max_y"])
        except (KeyError, TypeError, ValueError):
            return None

    p = e.get("position") or e.get("insert_point") or e.get("center")
    if isinstance(p, dict) and "x" in p and "y" in p:
        try:
            x = float(p["x"])
            y = float(p["y"])
            return x, y, x, y
        except (TypeError, ValueError):
            return None
    return None


def _extents_near(
    a: tuple[float, float, float, float] | None,
    b: tuple[float, float, float, float] | None,
    tol: float = _TITLE_GRAPHIC_NEAR_TOL,
) -> bool:
    if not a or not b:
        return False
    ax1, ay1, ax2, ay2 = min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3])
    bx1, by1, bx2, by2 = min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3])
    return not (
        ax2 + tol < bx1
        or bx2 + tol < ax1
        or ay2 + tol < by1
        or by2 + tol < ay1
    )


def _is_title_graphic_entity(e: dict, title_extents: list[tuple[float, float, float, float]]) -> bool:
    t = str(e.get("raw_type") or e.get("type") or "").upper()
    if t not in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE"):
        return False
    e_ext = _entity_title_extents(e)
    return any(_extents_near(e_ext, title_ext) for title_ext in title_extents)


def _is_near_pipe_annotation(e: dict, pipe_extents: list[tuple[float, float, float, float]]) -> bool:
    t = str(e.get("raw_type") or e.get("type") or "").upper()
    if t not in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE", "INSERT", "BLOCK"):
        return False
    e_ext = _entity_title_extents(e)
    return any(_extents_near(e_ext, pipe_ext, _PIPE_ANNOTATION_GRAPHIC_NEAR_TOL) for pipe_ext in pipe_extents)


def split_entities_by_layer_role(
    entities: list[dict],
    db_role_map: dict[str, str] | None = None,
    *,
    drawing_data: dict | None = None,
    include_unknown_in_review: bool = True,
    ncs_mep_prefix: bool = True,
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, LayerRole]]:
    """
    (arch, mep[±unknown] 검토, aux, unknown만 통계용).
    
    [Content-based Analysis 추가]
    이름만으로 판별이 어려운 레이어(unknown)에 대해 내부 엔티티 구성을 분석하여
    건축 배경(많은 선분, 적은 텍스트/블록)인지 판단하는 2차 분류를 수행합니다.
    """
    # 1단계: 레이어별 통계 수집
    layer_stats: dict[str, dict[str, Any]] = {}
    for e in entities or []:
        ln = _entity_layer(e)
        if not ln: continue
        if ln not in layer_stats:
            layer_stats[ln] = {"total": 0, "lines": 0, "blocks": 0, "texts": 0}
        
        layer_stats[ln]["total"] += 1
        t = str(e.get("raw_type") or e.get("type") or "").upper()
        if t in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC"):
            layer_stats[ln]["lines"] += 1
        elif t in ("INSERT", "BLOCK"):
            layer_stats[ln]["blocks"] += 1
        elif t in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
            layer_stats[ln]["texts"] += 1
            # 텍스트/지시선 내용에 배관 키워드가 포함되어 있으면 해당 레이어는 배관으로 간주
            txt = str(e.get("text") or e.get("content") or "").upper()
            if any(kw in txt for kw in _MEP_KEYWORDS):
                layer_stats[ln]["has_mep_keyword"] = True
            
            # [표제란 방어] 텍스트 내용에 도면 제목 키워드(평면도 등)가 있으면 표제란으로 간주
            if any(kw in txt for kw in _TITLE_CONTENT_KEYWORDS):
                layer_stats[ln]["is_title_layer"] = True

    # 1.5단계: 도면 전체의 전역 배관 증거(Global MEP Evidence) 수집
    # 도면 전체에 어떤 배관 키워드가 존재하는지 먼저 파악하여 이후 가중치에 반영합니다.
    global_has_mep_keyword = False
    for ln, stats in layer_stats.items():
        # 레이어명에 키워드가 있거나
        if bool(_MEP_HINT_RE.search(ln)):
            global_has_mep_keyword = True
            break
        # 레이어 내부에 배관 키워드를 가진 텍스트/블록이 있다면
        if stats.get("has_mep_keyword"):
            global_has_mep_keyword = True
            break

    # 2단계: 레이어별 역할 확정
    layer_resolved_roles: dict[str, LayerRole] = {}
    
    # 레이어 메타데이터(색상 등) 미리 맵핑
    layer_color_map: dict[str, str] = {}
    if drawing_data:
        for l_info in (drawing_data.get("layers") or []):
            if isinstance(l_info, dict) and l_info.get("name"):
                layer_color_map[l_info["name"]] = str(l_info.get("color") or "")

    # ACI 건축 배경색 (회색 계열)
    _ARCH_ACI_COLORS = {"8", "9", "250", "251", "252", "253", "254", "255"}

    for ln, stats in layer_stats.items():
        # 기본 이름 기반 분류
        role = classify_layer_role(ln, db_role_map, ncs_mep_prefix=ncs_mep_prefix)
        
        # 이름이 unknown인 경우 내용 및 색상 분석 (Heuristic)
        if role == "unknown":
            total = stats["total"]
            line_ratio  = stats["lines"] / total if total > 0 else 0
            
            l_color = layer_color_map.get(ln, "").split("(")[0]
            
            # [A] 색상 힌트: 회색 계열이면 건축 배경일 확률 매우 높음
            if l_color in _ARCH_ACI_COLORS:
                role = "arch"
            elif total > 0:
                # [B] 배관 증거(MEP Evidence) 확인
                # 레이어명에 배관 키워드가 포함되어 있는지 체크 (대소문자 무시)
                up_n = ln.upper()
                has_mep_keyword = any(kw in up_n for kw in _MEP_KEYWORDS)
                
                # [특수 규칙] L1, L2... 처럼 L로 시작하는 레이어는 배관 키워드가 없으면 건축일 확률이 높음
                is_l_layer = bool(re.match(r"^L(?:\d|[-_])", ln, re.IGNORECASE))
                
                # [색상 기반 보호] 회색이 아닌 유색(녹색, 붉은색 등) 레이어는 선분이 많아도 건축물보다는 배관일 확률이 높음
                is_vivid_color = l_color not in _ARCH_ACI_COLORS and l_color != "" and l_color != "7" # 7은 흰색/검은색
                
                # [다중 요소 가중치 판정]
                # 표제 텍스트가 있다고 해서 레이어 전체를 arch로 바꾸지 않습니다.
                # 표제 객체 자체와 주변 그래픽만 아래 엔티티 분리 단계에서 aux로 제외합니다.
                if has_mep_keyword or (global_has_mep_keyword and is_vivid_color):
                    # 이 경우 배관 후보(unknown)로 남겨두어 검토 대상에 포함시킴
                    pass
                elif not is_vivid_color and (line_ratio > 0.60 or is_l_layer):
                    # 키워드도 없고 색상도 무채색인데 선분만 많으면 건축으로 확정
                    # 단, 블록(blocks)이 하나라도 있다면 설비 심볼일 수 있으므로 제외
                    if stats.get("blocks", 0) == 0:
                        role = "arch"
                    else:
                        # 블록이 있다면 unknown으로 남겨서 검토 받게 함
                        pass
                elif line_ratio > 0.95 and not is_vivid_color:
                    # 선분이 압도적이고 무채색이면 건축
                    # 단, blocks나 texts가 있으면 TEX처럼 별도 분류된 심볼/주석이 있을 수 있으므로 unknown 유지
                    if stats.get("blocks", 0) == 0 and stats.get("texts", 0) == 0:
                        role = "arch"
            else:
                has_mep_keyword = False
        
        layer_resolved_roles[ln] = role
        
        # 각 레이어에 포함된 타입 요약 (디버깅용)
        type_counts = {k: v for k, v in stats.items() if k in ("lines", "blocks", "texts")}
        
        # [DEBUG LOG] 레이어별 판별 근거 출력
        msg = f"[LayerSplit Debug] Layer: {ln} | Stats: {type_counts} | Total: {stats['total']} | LineRatio: {(stats['lines'] / stats['total'] if stats['total'] > 0 else 0):.2f} | FinalRole: {layer_resolved_roles[ln]}"
        logging.info(msg)
        
        # L 계열이거나 건축인 경우 상세 정보 콘솔 출력
        is_l_layer = bool(re.match(r"^L(?:\d|[-_])", ln, re.IGNORECASE))
        if is_l_layer or layer_resolved_roles[ln] == "arch":
            print(msg)

    # 3단계: 엔티티 분리
    arch: list[dict] = []
    aux: list[dict] = []
    review: list[dict] = []
    unknown: list[dict] = []
    title_extents = [
        ext
        for e in entities
        if isinstance(e, dict) and _is_title_text_entity(e)
        for ext in [_entity_title_extents(e)]
        if ext is not None
    ]
    pipe_annotation_extents = [
        ext
        for e in entities
        if isinstance(e, dict) and _is_pipe_annotation_text_entity(e)
        for ext in [_entity_title_extents(e)]
        if ext is not None
    ]

    for e in entities or []:
        if not isinstance(e, dict):
            continue
        ln = _entity_layer(e)
        role = layer_resolved_roles.get(ln, "unknown")

        if _is_title_text_entity(e) or _is_title_graphic_entity(e, title_extents):
            aux.append({**e, "layer_role": "aux", "metadata_role": "title_block"})
            continue

        if _is_room_label_text_entity(e):
            arch.append({**e, "layer_role": "arch", "metadata_role": "space_label"})
            continue

        e_type = str(e.get("raw_type") or e.get("type") or "").upper()
        if e_type in ("TEXT", "MTEXT", "MLEADER", "LEADER") and not _is_pipe_annotation_text_entity(e):
            aux.append({**e, "layer_role": "aux", "metadata_role": "general_text"})
            continue

        # [L-layer 내 엔티티 타입 분리]
        # L4처럼 한 레이어에 건축 배경과 배관 주석/선이 섞이는 도면이 있다.
        # L3처럼 레이어 전체가 unknown인 경우도 초록/빨강 등 유색 배관선이 섞인다.
        # 레이어 전체가 arch이면 LINE은 건축 기준선으로만 남긴다. 같은 L-layer 안의 BLOCK/TEXT만
        # 배관 문맥 후보로 승격하여 벽체/문선이 검토 박스로 잡히는 오탐을 막는다.
        if role in ("arch", "unknown") and re.match(r"^L(?:\d|[-_])", ln, re.IGNORECASE):
            is_line_type = e_type in ("LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE")
            is_block_text = e_type in ("INSERT", "BLOCK", "TEXT", "MTEXT", "MLEADER", "LEADER")

            if is_block_text:
                # BLOCK/TEXT는 MEP 점수 계산 후 review로 이동
                mep_score, indicator = _score_entity_for_mep_in_l_layer(e)
                near_pipe_annotation = _is_near_pipe_annotation(e, pipe_annotation_extents)
                if mep_score >= 0.7 or near_pipe_annotation:
                    e = {**e,
                         "entity_mep_score": mep_score,
                         "entity_mep_indicator": indicator or ("near_pipe_annotation" if near_pipe_annotation else ""),
                         "flag_for_piping_agent": True,
                         "source_layer_role": role,
                         "layer_role": "mep"}
                    logging.debug(
                        "[LayerSplit] L-layer block/text → review: Layer=%s, Type=%s, Entity=%s, Score=%.2f, NearAnn=%s",
                        ln, e_type, e.get("name", e.get("text", "?")), mep_score, near_pipe_annotation,
                    )
                    review.append(e)
                    continue
            if is_line_type and role != "arch":
                mep_score, indicator = _score_entity_for_mep_in_l_layer(e)
                near_pipe_annotation = _is_near_pipe_annotation(e, pipe_annotation_extents)
                # 색상 단독 판정 금지. unknown L-layer 선도 배관 주석/명시 근거 주변일 때만 살린다.
                if near_pipe_annotation:
                    e = {**e,
                         "entity_mep_score": mep_score,
                         "entity_mep_indicator": indicator or "near_pipe_annotation",
                         "flag_for_piping_agent": True,
                         "source_layer_role": role,
                         "layer_role": "mep"}
                    logging.debug(
                        "[LayerSplit] L-layer line near pipe annotation → review: Layer=%s, Type=%s, Handle=%s, Score=%.2f, Indicator=%s",
                        ln, e_type, e.get("handle", "?"), mep_score, indicator or "near_pipe_annotation",
                    )
                    review.append(e)
                    continue
            # arch L-layer LINE 계열은 아래 arch로 그대로 낙하

        if role == "arch":
            arch.append(e)
        elif role == "aux":
            aux.append(e)
        elif role == "mep":
            review.append(e)
        else:
            unknown.append(e)
            if include_unknown_in_review:
                review.append(e)

    if db_role_map and unknown:
        logging.debug(
            "[LayerSplit] unknown 레이어 %d건 — DB 오버라이드 없음(휴리스틱). mapping_rules.layer_role 권장.",
            len(unknown),
        )
    return arch, review, aux, unknown, layer_resolved_roles


def _filter_by_handles(entities: list[dict], handles: set[str]) -> list[dict]:
    if not handles:
        return list(entities)
    return [e for e in entities if str(e.get("handle", "")) in handles]


def _bbox_extents(b: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    if not b or not isinstance(b, dict):
        return None
    try:
        if "x1" in b and "x2" in b and "y1" in b and "y2" in b:
            return float(b["x1"]), float(b["y1"]), float(b["x2"]), float(b["y2"])
        if "min_x" in b and "max_x" in b and "min_y" in b and "max_y" in b:
            return (
                float(b["min_x"]),
                float(b["min_y"]),
                float(b["max_x"]),
                float(b["max_y"]),
            )
    except (TypeError, ValueError, KeyError):
        return None
    return None


def _separation_2d(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> float:
    """축맞춤 사각형 최소 이격(겹침이면 0)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    if ax1 > ax2:
        ax1, ax2 = ax2, ax1
    if ay1 > ay2:
        ay1, ay2 = ay2, ay1
    if bx1 > bx2:
        bx1, bx2 = bx2, bx1
    if by1 > by2:
        by1, by2 = by2, by1
    if ax2 < bx1:
        return bx1 - ax2
    if bx2 < ax1:
        return ax1 - bx2
    if ay2 < by1:
        return by1 - ay2
    if by2 < ay1:
        return ay1 - by2
    return 0.0


def _center(ext: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = ext
    return (x1 + x2) * 0.5, (y1 + y2) * 0.5


def compute_arch_mep_spatial_hints(
    arch_entities: list[dict],
    mep_entities: list[dict],
    *,
    max_pairs: int = 32,
) -> dict[str, Any]:
    """
    각 MEP 엔티티에 대해 가장 가까운 arch bbox와의 2D 이격·중심거리(근사)를 담는다.
    max_pairs=0 이면 빈 결과.
    """
    out: list[dict[str, Any]] = []
    if max_pairs <= 0 or not mep_entities or not arch_entities:
        return {
            "description": "arch bbox와 MEP bbox 근접(이격 0=겹침/접촉). mm 단위 추정; 도면 단위는 drawing_unit 참고",
            "pairs": [],
        }

    arch_ext: list[tuple[dict, tuple[float, float, float, float]]] = []
    for e in arch_entities:
        b = e.get("bbox")
        ext = _bbox_extents(b if isinstance(b, dict) else None)
        if ext:
            arch_ext.append((e, ext))
    room_ext: list[tuple[dict, tuple[float, float, float, float]]] = [
        (e, ext)
        for e, ext in arch_ext
        if str(e.get("metadata_role") or "") == "space_label" or _is_room_label_text_entity(e)
    ]

    if not arch_ext:
        return {
            "description": "arch 엔티티에 유효 bbox 없음",
            "pairs": [],
        }

    for mep in mep_entities:
        if len(out) >= max_pairs:
            break
        b = mep.get("bbox")
        me = _bbox_extents(b if isinstance(b, dict) else None)
        if not me:
            continue
        best_a: dict | None = None
        best_d = math.inf
        best_arch_ext = arch_ext[0][1]
        for ae, aext in arch_ext:
            d = _separation_2d(me, aext)
            if d < best_d:
                best_d = d
                best_a = ae
                best_arch_ext = aext
        if best_a is None:
            continue
        c_m = _center(me)
        c_a = _center(best_arch_ext)
        nearest_room: dict | None = None
        nearest_room_dist = math.inf
        for room, room_bbox in room_ext:
            c_r = _center(room_bbox)
            d_r = math.hypot(c_m[0] - c_r[0], c_m[1] - c_r[1])
            if d_r < nearest_room_dist:
                nearest_room_dist = d_r
                nearest_room = room
        dist_c = math.hypot(c_m[0] - c_a[0], c_m[1] - c_a[1])
        pair = {
            "mep_handle": str(mep.get("handle", "")),
            "mep_layer": str(mep.get("layer", "")),
            "nearest_arch_handle": str(best_a.get("handle", "")),
            "nearest_arch_layer": str(best_a.get("layer", "")),
            "edge_separation_drawing": round(best_d, 4),
            "center_distance_drawing": round(dist_c, 4),
        }
        if nearest_room is not None:
            pair.update({
                "nearest_space_label": _entity_text(nearest_room),
                "nearest_space_label_handle": str(nearest_room.get("handle", "")),
                "space_label_distance_drawing": round(nearest_room_dist, 4),
            })
        out.append(pair)
    return {
        "description": "건축 기준(arch) geometry와 MEP의 근접. 규정 이격 판정은 LLM+룰",
        "pairs": out,
    }


def _attach_role(e: dict, role: LayerRole) -> dict:
    if role in _VALID_ROLES or role == "unknown":
        return {**e, "layer_role": role}
    return {**e, "layer_role": "unknown"}


def _build_layer_name_roles(
    entities: list[dict],
    db_role_map: dict[str, str] | None,
    ncs_mep_prefix: bool,
) -> dict[str, str]:
    names: set[str] = set()
    for e in entities:
        ln = str(e.get("layer") or "").strip()
        if ln:
            names.add(ln)
    return {
        ln: classify_layer_role(ln, db_role_map, ncs_mep_prefix=ncs_mep_prefix)
        for ln in sorted(names)
    }


def _build_layers_indexed(
    drawing_data: dict[str, Any],
    layer_name_roles: dict[str, str],
) -> list[dict[str, Any]]:
    layers = drawing_data.get("layers") or []
    counts: dict[str, int] = {}
    for row in layers:
        if not isinstance(row, dict):
            continue
        n = str(row.get("name") or "")
        c = int(row.get("entity_count") or 0)
        counts[n] = c
    for ln in layer_name_roles:
        counts.setdefault(ln, counts.get(ln, 0))
    out: list[dict[str, Any]] = []
    for i, (name, role) in enumerate(sorted(layer_name_roles.items(), key=lambda x: x[0])):
        out.append(
            {
                "index": i,
                "name": name,
                "layer_role": role,
                "entity_count": counts.get(name, 0),
            }
        )
    return out


def build_pipe_review_layout(
    drawing_data: dict[str, Any],
    *,
    active_ids: set[str] | None = None,
    focus_entities: list[dict] | None = None,
    org_id: str | None = None,
    include_unknown_in_review: bool | None = None,
    ncs_mep_prefix: bool | None = None,
) -> dict[str, Any]:
    """
    - arch_reference: 건축·구조 — 항상 **전체** (선택/포커스와 무관)
    - mep_review: 배관·검토 대상
    - layer_roles / layers_indexed: JSON·UI에서 구분·토큰용
    - spatial_hints: arch–MEP 근접(옵션)

    include_unknown_in_review / ncs_mep_prefix 가 None이면 config 값 사용.
    """
    from backend.core.config import settings

    if include_unknown_in_review is None:
        include_unknown_in_review = bool(
            getattr(settings, "PIPING_REVIEW_INCLUDE_UNKNOWN", True)
        )
    if ncs_mep_prefix is None:
        ncs_mep_prefix = bool(getattr(settings, "NCS_DISCIPLINE_MEP_PREFIX", True))
    sp_max = int(getattr(settings, "PIPING_SPATIAL_HINTS_MAX", 32))

    db_role_map: dict[str, str] | None = None
    if org_id:
        try:
            from backend.services.agents.pipe.sub.mapping import get_layer_role_map

            db_role_map = get_layer_role_map(org_id) or None
        except Exception as exc:
            logging.warning("[LayerSplit] layer_role_map 로드 실패, 휴리스틱만 사용: %s", exc)

    raw = drawing_data.get("entities") or drawing_data.get("elements") or []
    entities: list[dict] = [e for e in raw if isinstance(e, dict)]

    arch_all, mep_all, aux_list, unknown_only, layer_resolved_roles = split_entities_by_layer_role(
        entities,
        db_role_map=db_role_map,
        drawing_data=drawing_data,
        include_unknown_in_review=include_unknown_in_review,
        ncs_mep_prefix=ncs_mep_prefix,
    )
    n_aux = len(aux_list)
    n_unknown = len(unknown_only)
    n_mep_only = sum(
        1
        for e in mep_all
        if entity_layer_role(
            e, db_role_map, ncs_mep_prefix=ncs_mep_prefix
        )
        == "mep"
    )
    text_role_counts_by_layer: dict[str, dict[str, int]] = {}

    def _bump_text_role(e: dict, role_name: str) -> None:
        t = str(e.get("raw_type") or e.get("type") or "").upper()
        if t not in ("TEXT", "MTEXT", "MLEADER", "LEADER"):
            return
        ln = _entity_layer(e) or "(no layer)"
        row = text_role_counts_by_layer.setdefault(ln, {})
        row[role_name] = row.get(role_name, 0) + 1

    for e in arch_all:
        if str(e.get("metadata_role") or "") == "space_label":
            _bump_text_role(e, "space_label")
    for e in aux_list:
        meta = str(e.get("metadata_role") or "")
        if meta in ("general_text", "title_block"):
            _bump_text_role(e, meta)
    for e in mep_all:
        if _is_pipe_annotation_text_entity(e):
            _bump_text_role(e, "pipe_annotation")

    target_mep = list(mep_all)
    if focus_entities:
        fh = {
            str(e.get("handle", ""))
            for e in focus_entities
            if isinstance(e, dict) and e.get("handle")
        }
        target_mep = _filter_by_handles(mep_all, fh)
    elif active_ids:
        target_mep = _filter_by_handles(mep_all, set(active_ids))

    layer_name_roles = _build_layer_name_roles(entities, db_role_map, ncs_mep_prefix)
    layers_indexed = _build_layers_indexed(drawing_data, layer_name_roles)

    arch_annot = [_attach_role(e, "arch") for e in arch_all]
    mep_annot = []
    for e in target_mep:
        ln = _entity_layer(e)
        if e.get("flag_for_piping_agent") and str(e.get("layer_role") or "") == "mep":
            r = "mep"
        else:
            r = layer_resolved_roles.get(ln, "unknown")
        mep_annot.append(_attach_role(e, r))
    sp = compute_arch_mep_spatial_hints(arch_annot, mep_annot, max_pairs=sp_max)

    out: dict[str, Any] = {
        "schema": "piping_split_v1",
        "layer_role_stats": {
            "arch_entities": len(arch_all),
            "mep_review_entities": len(target_mep),
            "mep_unfiltered": len(mep_all),
            "mep_only_entities": n_mep_only,
            "aux_skipped": n_aux,
            "unknown_count": n_unknown,
            "unknown_merged_into_review": n_unknown if include_unknown_in_review else 0,
            "include_unknown_in_review": include_unknown_in_review,
            "db_role_overrides": len(db_role_map) if db_role_map else 0,
            "text_role_counts_by_layer": text_role_counts_by_layer,
            "space_label_entities": sum(
                (row.get("space_label") or 0) for row in text_role_counts_by_layer.values()
            ),
            "pipe_annotation_text_entities": sum(
                (row.get("pipe_annotation") or 0) for row in text_role_counts_by_layer.values()
            ),
            "general_text_skipped": sum(
                (row.get("general_text") or 0) for row in text_role_counts_by_layer.values()
            ),
        },
        "layer_roles": layer_name_roles,
        "layers_indexed": layers_indexed,
        "spatial_hints": sp,
        "arch_reference": {
            "description": "건축·구조 — 배관·설비 기준(벽·슬라브·기둥). 엔티티에 layer_role=arch",
            "entities": arch_annot,
            "entity_count": len(arch_annot),
        },
        "mep_review": {
            "description": "배관·기계/위생 검토 대상. layer_role mep|unknown(옵션)",
            "entities": mep_annot,
            "entity_count": len(mep_annot),
        },
        "entities": mep_annot,
        "entity_count": len(mep_annot),
    }
    return out
