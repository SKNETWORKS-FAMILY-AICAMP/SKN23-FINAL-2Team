"""
File    : backend/services/agents/electric/sub/review/compliance.py
Author  : 김지우
Date    : 2026-04-23
Description : 전기 시방서 기반 설비 배치 정합성 검증 에이전트 (RAG + LLM)
              [환각 방지] 엄격한 8계명 및 proposed_action (Auto-Fix) 스키마 적용
"""

import asyncio
import json
import logging
import re

from backend.core.config import settings
from backend.services.agents.elec.schemas import ElectricViolationType
from backend.services import llm_service

_MAX_SPEC_CONTEXT_CHARS = 28_000
_MAX_LAYOUT_DATA_CHARS = 48_000
_MAX_LAYOUT_PER_CHUNK_CHARS = 32_000
_MAX_COMPLIANCE_LLM_CONCURRENT = 4

# ── LLM 전송 전 element 경량화 ──────────────────────────────────────────────────
# 기하 좌표·내부 메타 필드: LLM 토큰 절감을 위해 LLM 전송 전 제거한다.
# BLOCK(INSERT)의 insert_point는 이 집합에 포함되지 않으므로 자동 보존된다.
_ELEMENT_GEO_STRIP: frozenset[str] = frozenset({
    "bbox", "extents",
    "coordinates", "raw_type", "tag_name",
    # "start", "end" : LINE 끝점은 topology builder가 단선 감지에 사용하므로 유지
    # "rotation", "position" 은 전기 설비 방향·위치 판단에 필요할 수 있으므로 유지
})

# BLOCK/INSERT 이외 객체에 적용하는 attributes 화이트리스트
# (전기 도메인 핵심 속성만 통과시켜 노이즈 차단)
_ATTRS_COMPLIANCE_KEYS: frozenset[str] = frozenset({
    "voltage", "capacity", "phase", "usage",
    "sq", "ampere", "pole", "mccb", "mounting", "ip_rate",
    "rated_current", "breaker_amps", "circuit_no", "panel_id",
    "kw", "kva", "pf", "circuit", "load_type",
})

# BLOCK/INSERT 타입은 모든 속성을 AI에게 전달해야 오진을 막을 수 있다.
# 예: 분전반 블록의 BREAKER_AMPS, CIRCUIT_NO 속성 누락 시 용량 위반 환각 발생
_BLOCK_TYPE_UPPER: frozenset[str] = frozenset({"BLOCK", "INSERT"})

# 전선으로 오인될 수 있는 기하 타입 (LINE, POLYLINE 등)
_WIRE_CANDIDATE_TYPES: frozenset[str] = frozenset({
    "LINE", "POLYLINE", "LWPOLYLINE", "ARC", "SPLINE",
})

# 전기 속성이 있다고 판단할 키 목록 (하나라도 있고 값이 0이 아니면 전선으로 간주)
_WIRE_ATTR_KEYS: frozenset[str] = frozenset({"sq", "voltage", "ampere", "cable_sqmm"})


def _is_electrical_wire(el: dict) -> bool:
    """이 엔티티가 전기적 속성이 있는 전선인지 판단한다.

    - 타입이 LINE/POLYLINE 계열이어야 함
    - 전기 속성(sq, voltage, ampere, cable_sqmm) 중 하나라도 0이 아닌 값이 있어야 함
    - 조건 미충족 → 표/도곽/건축 선으로 간주, compliance AI에게 전달하지 않음
    """
    etype = str(el.get("type") or el.get("raw_type") or "").upper()
    if etype not in _WIRE_CANDIDATE_TYPES:
        return True  # LINE/POLYLINE이 아니면 필터 대상 아님 → 그대로 통과

    # 속성 딕셔너리 또는 최상위 필드 확인
    attrs = el.get("attributes") or {}
    for key in _WIRE_ATTR_KEYS:
        # 최상위 필드 확인 (예: el["sq"])
        val = el.get(key) or attrs.get(key)
        try:
            if val is not None and float(val) != 0.0:
                return True  # 유효한 전기 속성 존재 → 전선으로 취급
        except (TypeError, ValueError):
            pass

    return False  # 전기 속성 없음 → 비전선(표/도곽/건축 선) → 필터 대상


def _slim_element(el: dict) -> dict:
    """
    element dict에서 기하·내부 필드를 제거하고 attributes를 필터링한다.

    ▸ BLOCK/INSERT 타입은 attributes 필터링을 완전히 생략한다.
      블록 속성에 담긴 전기 설비 메타(전압, 극수, 차단기 용량 등)가
      누락되면 AI가 증거 없는 위반을 생성하는 환각의 원인이 된다.
    ▸ 그 외 타입(LINE, TEXT 등)은 화이트리스트로 필터링한다.
    """
    slim = {k: v for k, v in el.items() if k not in _ELEMENT_GEO_STRIP}
    raw_attrs = slim.pop("attributes", None)

    if isinstance(raw_attrs, dict) and raw_attrs:
        etype = str(el.get("type") or el.get("raw_type") or "").upper()

        if etype in _BLOCK_TYPE_UPPER:
            # BLOCK/INSERT: 모든 속성 보존 (데이터 부재로 인한 오진 방지)
            slim["attributes"] = raw_attrs
        else:
            # 그 외: 전기 관련 핵심 키워드만 통과
            kept = {k: v for k, v in raw_attrs.items() if k.lower() in _ATTRS_COMPLIANCE_KEYS}
            if kept:
                slim["attributes"] = kept

    return slim


def _slim_parsed_for_llm(parsed: dict) -> dict:
    """check_compliance_parsed 진입 시 parsed 전체를 경량화.

    [핵심 필터] LINE/POLYLINE 중 전기 속성(sq, voltage 등)이 없는 객체는
    compliance AI에게 보내지 않는다. 이것이 '0.0SQ 위반 환각'의 근본 원인이었다:
    - 도면 내 표 테두리, 건축 선, 도곽 선 등이 sq=0 LINE으로 파싱됨
    - AI가 이를 '전선 굵기 0.0SQ → 허용전류 산정 불가' 로 오진
    - _is_electrical_wire() 로 전기 속성이 있는 선만 통과시켜 원천 차단
    """
    slimmed = dict(parsed)
    if "elements" in slimmed:
        raw_elements = slimmed["elements"] or []
        wire_filtered: list[dict] = []
        non_wire_count = 0

        for el in raw_elements:
            if not isinstance(el, dict):
                continue
            if not _is_electrical_wire(el):
                non_wire_count += 1
                continue  # 전기 속성 없는 선 → AI에게 전달 안 함
            wire_filtered.append(_slim_element(el))

        if non_wire_count:
            logging.debug(
                "[compliance] 비전선 LINE/POLYLINE %d개 제외 (0.0SQ 환각 방지)",
                non_wire_count,
            )
        slimmed["elements"] = wire_filtered
    return slimmed

_VIOLATION_TYPES_STR = " | ".join(v.value for v in ElectricViolationType)

_SYSTEM_PROMPT = f"""당신은 CAD-SLLM Agent 파이프라인의 '전기설비 규정 검증 서브 에이전트'입니다.
오직 RAG를 통해 제공된 [시방서 규정]만을 근거로 [현재 도면 데이터]의 정합성을 평가하십시오.
사전 학습된 외부 지식을 개입시켜서는 안 됩니다.

[도면 데이터 필드 설명]
각 설비(element)는 다음 필드를 가집니다:
  - id           : 설비 식별자 (TAG_NAME 또는 CAD handle)
  - handle       : 원본 CAD handle (위반 보고 시 반드시 이 값을 equipment_id로 사용)
  - type         : 설비 유형
  - layer        : CAD 레이어명
  - position     : 설치 좌표 {{"x": float, "y": float}}
  - start / end  : LINE 타입의 양 끝점 좌표 {{"x": float, "y": float}} (존재할 경우)
  - attributes   : BLOCK 속성 dict (전압, 용량, 극수, 차단기 용량, 방수 등급 등)
  - name         : 설비의 의미적 이름 (텍스트-블록 매핑으로 추출된 설비명, 예: "GND", "MAIN MOTOR 전류")
  - equipment_labels : 설비의 분류 태그 (※ equipment_labels 필드가 있으면 handle → 설비명 조회에 활용하십시오)

[전기 위상(Topology) 분석 필드]
JSON에 `elec_topology` 가 있으면 반드시 참조하십시오:
  - circuit_runs        : 연결된 전선 그룹 목록. 각 run에 handles(선 handle 목록), total_length, voltage, cable_sqmm 포함.
  - broken_segments     : 🚨 단선(끊어진 선) 후보 목록. handle_a·handle_b 두 선의 끝점이 gap_mm 만큼 떨어져 있고 연결되지 않은 경우.
  - dangling_endpoints  : 어떤 다른 선과도 연결되지 않은 고립 끝점 목록 (handle, side, x, y).
  - summary.broken_count: 감지된 단선 후보 수.

[검증 지침]
1. 규정 수치 추출: 시방서에서 전압 강하 한도, 허용 전류, 이격 거리, 상 색상 등을 파악합니다.
2. 상태 비교: 도면 데이터의 실제 수치와 기준값을 대조하여 위반 여부를 계산합니다.
3. 무결성 확인: 위반 사항이 없다면 반드시 빈 violations 배열만 반환합니다.
4. 단선(끊어진 선) 검사 — broken_segments 전용:
   - `elec_topology.broken_segments` 배열이 비어있지 않으면, 각 항목을 반드시 위반으로 보고하십시오.
   - `broken_segments` 의 각 항목은 `handle_a`와 `handle_b` 두 전선이 `gap_mm` 만큼의 간격으로 끊어져 있음을 의미합니다.
   - equipment_id로는 `handle_a` (끊어진 구간의 시작 선)를 사용하고, violation_type은 `open_circuit_error` 로 지정하십시오.
   - current_value: "끊어진 간격 {{gap_mm}}mm", required_value: "0mm (연속 연결)", reason: "전선이 끊어져 회로가 개방됨"
   - `dangling_endpoints` 는 참고 정보일 뿐이며, 단독으로 위반 보고해서는 안 됩니다. (회로 종단, 단말부, 노이즈 선 등 정상 사유가 대부분입니다.)
   - `broken_segments` 배열이 비어있으면 open_circuit_error 위반을 절대 생성하지 마십시오.

[규정 적용 일관성 필수 준수 (오검출 방지 8계명)]
각 위반 항목(violation)을 작성할 때 반드시 아래 규칙을 지키십시오. 위반하면 결과 자체가 폐기됩니다.

1. 설비 유형 엄격 일치: reference_rule에 인용한 규정의 설비 유형(예: 분전반, 콘센트)이 equipment_id의 실제 설비 유형과 완벽히 일치해야 합니다. 비슷한 용도라도 임의로 교차 적용하지 마십시오.
2. 방향 일치 절대 준수: required_value의 방향(이하/이상)은 reference_rule의 기준값 방향과 동일해야 합니다.
   - 규정이 "X 이하/미만"이면 required_value도 "X 이하/미만"
   - 규정이 "X 이상/초과"이면 required_value도 "X 이상/초과"
3. 상식 개입 및 환각 금지 (Strict RAG): reference_rule은 반드시 제공된 [시방서 규정]에서 직접 인용해야 합니다. 시방서에 명시되지 않은 일반 전기 공학 상식(KEC, 내선규정 등)을 동원하여 위반을 창조하지 마십시오.
4. 정보 누락은 위반이 아님 (증거 우선주의): current_value로 비교해야 할 속성(전압, 굵기 등)이 도면 데이터에 아예 존재하지 않거나 0인 경우, 이를 '기준 미달'로 억측하지 말고 위반 검증에서 제외하십시오. 증거가 부족한 억지 위반 보고는 금지됩니다.
5. 조건부 규정의 엄격한 확인: 시방서 규정이 특정 조건("옥외 설치 시", "380V 이상인 경우")을 전제할 때, 현재 도면 설비가 해당 조건에 부합한다는 명백한 데이터적 근거가 없다면 위반으로 판정하지 마십시오.
6. 비전기 설비 및 건축물 무시: 속성 및 레이어 상 건축 구조물(기둥, 벽)이거나 단순 기하 객체인 경우 전기 규정을 억지로 적용하지 말고 완전히 무시하십시오.
7. 정상 상태 보고 금지: 규정을 만족(Pass)한 정상 설비는 violations 배열에 절대 포함하지 마십시오. 오직 실패(Fail)한 항목만 담아야 합니다.
8. [단위 통일 환산 필수]: 시방서 수치(예: 0.5m)와 도면 데이터 수치(예: 500mm)의 단위가 다를 경우, 반드시 두 수치를 밀리미터(mm) 등 동일한 단위로 논리적으로 환산하여 비교하십시오. 단순 문자열 비교로 인한 오검출을 절대 금지합니다.
9. [0.0 및 데이터 부재 시 위반 보고 절대 금지]: 전선 굵기가 0.0SQ이거나 속성(sq, voltage 등)이 아예 없는 객체는 분석 데이터가 부족한 것이지 '기준 미달'이 아닙니다. 이러한 객체는 위반 목록에 절대 포함하지 마십시오. 이를 어길 시 에이전트 성능 평가에서 최하점을 받게 됩니다.
10. [측정 불가 항목 원천 금지]: current_value 또는 required_value가 제공된 도면 데이터(conduit_clearances, panel_clearances 등 별도 배열 포함)에서 실제 수치로 직접 확인되지 않는 항목은 violations에 절대 포함하지 마십시오.
    - 이격거리(clearance) 위반은 conduit_clearances/panel_clearances 배열에 해당 엔티티 쌍의 실측 거리가 수치로 명시된 경우에만 허용됩니다.
    - 2D 평면도에서 좌표 차이나 눈대중으로 이격거리를 추정하여 위반을 보고하는 행위는 엄격히 금지합니다.
    - 해당 수치를 확인할 수 없으면 위반 항목 생성을 완전히 포기하고 '데이터 부족으로 확인 불가' 상태로 간주하십시오.

[출력 JSON 스키마]
반드시 아래 구조의 JSON 객체만 반환하고, 부가 설명은 절대 포함하지 마십시오.
violation_type 은 반드시 아래 목록 중 정확히 하나의 소문자 문자열로 지정하십시오:
  {_VIOLATION_TYPES_STR}
current_value / required_value 는 빈 문자열 대신 실제 수치·단위를 포함한 문자열로 채우십시오.
{{
  "violations": [
    {{
      "equipment_id":   "도면 데이터의 id 필드 값 (CAD handle — 예: '1A5F')",
      "violation_type": "위 목록 중 정확히 하나 (소문자, 예: voltage_drop_error)",
      "reference_rule": "위반 근거 시방서 원문 (직접 인용)",
      "current_value":  "현재 도면 수치 (예: 2.5SQ, 220V, 500mm)",
      "required_value": "규정 요구 수치 (예: 4.0SQ, 380V, 500mm 이상)",
      "reason":         "논리적 위반 사유 요약 (한국어)",
      "proposed_action": {{
        "type": "CHANGE_CABLE_SIZE | CHANGE_COLOR | CHANGE_BREAKER_CAPACITY | LAYER | CREATE_ENTITY | BLOCK_REPLACE (아래 규칙에 따라 선택)",
        "required_size":  "CHANGE_CABLE_SIZE 타입 시: 목표 굵기 (예: 4.0SQ)",
        "required_color": "CHANGE_COLOR 타입 시: 목표 색상명 또는 번호 (예: RED, YELLOW)",
        "required_capacity": "CHANGE_BREAKER_CAPACITY 타입 시: 목표 차단기 용량 (예: 30A)",
        "new_layer":      "LAYER 타입 시: 올바른 레이어명 (예: E-POWER, E-LIGHT)",
        "new_block_name": "CREATE_ENTITY/BLOCK_REPLACE 타입 시: 삽입할 블록 정의명 (도면에 존재하는 블록만)",
        "base_x":         "CREATE_ENTITY/BLOCK_REPLACE 타입 시: 삽입 X 좌표 (도면 데이터에서 추출 가능한 경우만)",
        "base_y":         "CREATE_ENTITY/BLOCK_REPLACE 타입 시: 삽입 Y 좌표 (도면 데이터에서 추출 가능한 경우만)",
        "new_start":      {{"x": 0, "y": 0}},
        "new_end":        {{"x": 100, "y": 100}},
        "new_center":     {{"x": 0, "y": 0}},
        "new_radius":     0.0,
        "new_vertices":   [{{"x": 0, "y": 0, "bulge": 0}}]
      }}
    }}
  ]
}}

[proposed_action 사용 규칙 (C# DrawingPatcher 정렬)]
- type = "CHANGE_CABLE_SIZE": 전선의 굵기가 시방서 규정에 맞지 않아 두께를 변경해야 할 때 사용.
- type = "CHANGE_COLOR": 접지선이나 전선의 상 색상(R,S,T,N상)이 시방서 규정에 맞지 않을 때 사용.
- type = "CHANGE_BREAKER_CAPACITY": 분전반 내 차단기 용량이 규정에 맞지 않아 변경해야 할 때 사용.
- type = "LAYER"       : 설비가 잘못된 레이어에 있어 레이어명을 변경해야 할 때 사용.
- type = "CREATE_ENTITY": 규정상 필요한 설비가 도면에 누락되어 새로 추가해야 할 때 사용.
                          모형(블록) 삽입 시 new_block_name과 base_x, base_y 좌표를 제공하십시오.
                          폴리선이나 기하도형 삽입 시 new_vertices 등 해당 좌표 배열을 제공하십시오.
                          좌표를 계산할 수 없으면 이 타입을 사용하지 마십시오.
- type = "BLOCK_REPLACE": 삽입된 블록 심볼이 잘못되어 다른 블록으로 교체해야 할 때 사용.
- 불확실한 경우: proposed_action 필드 자체를 생략하십시오."""


def _dedupe_violations(violations: list) -> list:
    """
    [수정] 동일 객체(equipment_id)에 대해 동일한 위반 타입(violation_type)이 
    중복 보고되는 것을 원천 차단합니다.
    """
    seen: set[tuple] = set()
    out: list = []
    for v in violations or []:
        if not isinstance(v, dict): continue
        
        # 중복 체크 키에서 reason을 제거하여 AI의 문장 변형에 대응
        eid = str(v.get("equipment_id") or "").strip()
        vtype = str(v.get("violation_type") or "").strip()
        
        if not eid or not vtype: continue
        
        key = (eid, vtype) # 객체 ID와 위반 종류만으로 유일성 판단
        
        if key in seen: 
            logging.info(f"[ComplianceAgent] 중복 위반 제거됨: {eid} - {vtype}")
            continue
            
        seen.add(key)
        out.append(v)
    return out

# ── LLM 출력 후처리: 내부 모순 위반 제거 ────────────────────────────────────────
_NUM_RE     = re.compile(r"(\d+(?:\.\d+)?)")
_UPPER_KORE = re.compile(r"이하|미만|이내|초과하지\s*않|넘지\s*않")
_LOWER_KORE = re.compile(r"이상|초과(?!하지|되지)|이상이어야|이상으로")


def _direction(text: str) -> str | None:
    if _UPPER_KORE.search(text):
        return "upper"
    if _LOWER_KORE.search(text):
        return "lower"
    return None


def _validate_violations(violations: list) -> list:
    """reference_rule과 required_value의 방향·수치가 모순인 항목을 제거한다."""
    clean: list = []
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        ref  = str(v.get("reference_rule") or "")
        reqv = str(v.get("required_value")  or "")
        ref_dir  = _direction(ref)
        reqv_dir = _direction(reqv)
        if ref_dir and reqv_dir and ref_dir != reqv_dir:
            if set(_NUM_RE.findall(ref)) & set(_NUM_RE.findall(reqv)):
                logging.warning(
                    "[ComplianceAgent] 모순 위반 제거 — equipment_id=%s "
                    "ref_dir=%s reqv_dir=%s ref=%r reqv=%r",
                    v.get("equipment_id"), ref_dir, reqv_dir, ref[:80], reqv[:80],
                )
                continue
        clean.append(v)
    return clean


def _split_elements_by_json_size(elements: list) -> list[list]:
    if not elements: return []
    chunks: list[list] = []
    i, n = 0, len(elements)
    while i < n:
        best: list = [elements[i]]
        for size in range(1, n - i + 1):
            part = elements[i : i + size]
            s = json.dumps({"elements": part}, ensure_ascii=False)
            if len(s) <= _MAX_LAYOUT_PER_CHUNK_CHARS:
                best = part
            else:
                break
        chunks.append(best)
        i += len(best)
    return chunks

class ComplianceAgent:
    async def check_compliance_parsed(
        self, target_id: str, spec_context: str, parsed: dict
    ) -> list:
        if not (spec_context or "").strip():
            logging.warning("[ComplianceAgent] spec_context 비어 있음 — 빈 violations 반환")
            return []
        elements = (parsed or {}).get("elements") or []
        if not elements:
            return []

        # ── LLM 전송 전 경량화: 기하 좌표·내부 메타 제거, attributes 필터링 ────
        slim = _slim_parsed_for_llm(parsed)
        slim_elements = slim.get("elements", [])
        layout_str = json.dumps(slim, ensure_ascii=False)
        orig_chars = len(json.dumps(parsed, ensure_ascii=False))
        logging.info(
            "[ComplianceAgent] 경량화 완료: %d → %d chars (%.0f%% 절감)",
            orig_chars, len(layout_str),
            (1 - len(layout_str) / max(orig_chars, 1)) * 100,
        )

        spec_n = len(spec_context or "")
        lay_n  = len(layout_str)

        if lay_n <= _MAX_LAYOUT_DATA_CHARS and spec_n <= _MAX_SPEC_CONTEXT_CHARS and (lay_n + spec_n) < 70_000:
            result = await self.check_compliance(target_id, spec_context, layout_str)
            return _validate_violations(result)

        # 청크 분할도 slim된 elements 기준으로 수행
        parts = _split_elements_by_json_size(slim_elements)
        total = len(parts)
        if not parts:
            return []

        sem = asyncio.Semaphore(_MAX_COMPLIANCE_LLM_CONCURRENT)

        async def _one(k: int, el_chunk: list) -> list:
            async with sem:
                sub: dict = {"elements": el_chunk}
                if k == 0 and isinstance(slim, dict):
                    for key in ("error", "metadata", "elec_topology",
                                "conduit_clearances", "panel_clearances"):
                        if key in slim and slim.get(key) is not None:
                            sub[key] = slim[key]
                sub_str = json.dumps(sub, ensure_ascii=False)
                t_id = target_id
                if (target_id or "") == "ALL" and el_chunk:
                    t_id = (el_chunk[0].get("id") if isinstance(el_chunk[0], dict) else None) or target_id
                info = f"\n(검토 구간: {k + 1}/{total} 청크)"
                return await self.check_compliance(t_id, spec_context, sub_str, extra_user_suffix=info)

        chunk_results = await asyncio.gather(
            *(_one(k, parts[k]) for k in range(total)), return_exceptions=True
        )
        merged: list = []
        for r in chunk_results:
            if isinstance(r, list):
                merged.extend(r)
        return _validate_violations(_dedupe_violations(merged))

    async def check_compliance(
        self, target_id: str, spec_context: str, layout_data: str, *, extra_user_suffix: str = ""
    ) -> list:
        spec_in = (spec_context or "")[:_MAX_SPEC_CONTEXT_CHARS]
        layout_in = (layout_data or "")[:_MAX_LAYOUT_DATA_CHARS]

        user_prompt = (
            f"[대상 설비 ID]: {target_id}\n\n"
            f"[시방서 규정]\n{spec_in}\n\n"
            f"[현재 도면 데이터]\n{layout_in}\n\n"
            "위 데이터를 분석하여 JSON 스키마에 맞게 결과를 출력하십시오."
            f"{extra_user_suffix}"
        )

        result = await llm_service.generate_answer(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )

        if isinstance(result, dict):
            return result.get("violations", [])
        if isinstance(result, str):
            try:
                d = json.loads(result)
                return d.get("violations", []) if isinstance(d, dict) else []
            except Exception:
                return []
        return []