"""
File    : backend/services/agents/fire/sub/review/compliance.py
Author  : 김민정
Create  : 2026-04-15
Description : 정제된 도면 데이터와 NFSC 기준을 비교하여 위반 사항을 진단합니다.

Modification History:
    - 2026-04-15 (김민정) : NFSC 기반 법규 준수 여부 진단 로직 구현
    - 2026-04-18 (김지우) : llm_service 연동으로 리팩터링
    - 2026-04-23       : piping 방식과 동일하게 통일 (check_compliance_parsed, 청크 분할, 병렬 LLM 호출)
"""

import asyncio
import json
import logging

from backend.core.config import settings
from backend.services import llm_service

_MAX_SPEC_CONTEXT_CHARS = 28_000
_MAX_LAYOUT_DATA_CHARS = 48_000
_MAX_LAYOUT_PER_CHUNK_CHARS = 32_000
_MAX_COMPLIANCE_LLM_CONCURRENT = 4

_FIRE_VIOLATION_TYPES_STR = (
    "spacing_error | radius_error | height_error | quantity_error | "
    "pressure_error | pipe_size_error | installation_missing | "
    "access_blocked | material_error | other_violation"
)

_SYSTEM_PROMPT = f"""당신은 CAD-SLLM Agent 파이프라인의 '소방설비 규정 검증 서브 에이전트'입니다.
오직 RAG를 통해 제공된 [NFSC 법규 규정]만을 근거로 [현재 도면 데이터]의 정합성을 평가하십시오.
사전 학습된 외부 지식을 단독으로 개입시켜서는 안 됩니다.

[도면 데이터 필드 설명]
각 설비(element)는 다음 필드를 가집니다:
  - id           : 설비 식별자 (TAG_NAME 또는 CAD handle — 위반 보고 시 반드시 이 값을 equipment_id로 사용)
  - handle       : 원본 CAD handle
  - type         : 설비 유형 (예: "스프링클러헤드", "소화전", "감지기")
  - raw_type     : 원본 CAD 엔티티 타입 (LINE / CIRCLE / BLOCK 등)
  - layer        : CAD 레이어명
  - position     : 설치 좌표 {{"x": float, "y": float}}
  - attributes   : BLOCK 속성 dict
  - fire_topology: 설비 유형별 최근접 거리 분석 결과
    - fire_topology.sprinkler.nearest_distances      : 각 헤드의 최근접 헤드 거리
    - fire_topology.sprinkler.violation_candidates   : 기준(2300mm) 초과 후보
    - fire_topology.detector.nearest_distances       : 각 감지기의 최근접 감지기 거리
    - fire_topology.detector.violation_candidates    : 기준(4500mm) 초과 후보
    - fire_topology.hydrant.nearest_distances        : 각 소화전의 최근접 소화전 거리
    - fire_topology.hydrant.violation_candidates     : 기준(25000mm) 초과 후보
    - violation_candidates가 비어 있으면 해당 설비의 간격 위반을 보고하지 마십시오.

[검증 지침]
1. 규정 수치 추출: NFSC 법규에서 이격 거리, 설치 반경, 높이 기준, 설치 수량, 압력 기준 등을 파악합니다.
2. 상태 비교: 도면 데이터의 실제 수치와 기준값을 대조하여 위반 여부를 계산합니다.
   수치가 0 또는 UNKNOWN인 경우 해당 항목의 위반 여부는 판단하지 마십시오.
3. 간격 검토: 각 설비의 간격 위반은 반드시 fire_topology.<설비유형>.violation_candidates 또는
   nearest_distances의 distance_mm 값만 사용하십시오.
   violation_candidates가 비어 있으면 해당 설비의 간격 위반을 보고하지 마십시오.
4. 무결성 확인: 위반 사항이 없다면 반드시 빈 violations 배열만 반환합니다.

[규정 적용 일관성 필수 준수 (오검출 방지 9계명)]
각 위반 항목(violation)을 작성할 때 반드시 아래 규칙을 지키십시오. 위반하면 결과 자체가 폐기됩니다.

1. 설비 유형 엄격 일치: reference_rule에 인용한 규정의 설비 유형(예: 스프링클러 헤드, 소화전)이
   equipment_id의 실제 설비 유형과 완벽히 일치해야 합니다. 유사 용도라도 교차 적용 금지.
2. 방향 일치 절대 준수: required_value의 방향(이하/이상)은 reference_rule의 기준값 방향과 동일해야 합니다.
   - 규정이 "X 이하/미만"이면 required_value도 "X 이하/미만"
   - 규정이 "X 이상/초과"이면 required_value도 "X 이상/초과"
3. 상식 개입 및 환각 금지 (Strict RAG): reference_rule은 반드시 제공된 [NFSC 법규 규정]에서 직접 인용해야 합니다.
   NFSC에 명시되지 않은 일반 소방 지식을 동원하여 위반을 창조하지 마십시오.
4. 정보 누락은 위반이 아님 (증거 우선주의): 비교해야 할 속성이 도면 데이터에 없거나 0이면
   위반 검증에서 제외하십시오. 증거 없는 위반 보고는 금지됩니다.
5. 조건부 규정의 엄격한 확인: 규정이 특정 조건("옥외 설치 시", "습식 스프링클러의 경우")을 전제할 때,
   현재 설비가 해당 조건에 부합한다는 명백한 데이터 근거가 없으면 위반으로 판정하지 마십시오.
6. fire_topology 근거 없는 이격 위반 금지: 설비 간격 위반은 반드시 fire_topology.<설비유형>.violation_candidates
   또는 nearest_distances의 최근접 distance_mm 수치로만 판단하십시오.
   좌표 추정, 임의의 pairwise 거리, violation_candidates 외 수치로 이격 위반을 보고하지 마십시오.
7. 정상 상태 보고 금지: 규정을 만족한 정상 설비는 violations 배열에 절대 포함하지 마십시오.
8. 단위 통일 환산 필수: 시방서 수치(m)와 도면 데이터(mm)의 단위가 다르면
   반드시 동일 단위로 환산하여 비교하십시오. 단순 문자열 비교로 인한 오검출 금지.
9. 0.0 및 데이터 부재 시 위반 보고 절대 금지: 속성이 0.0이거나 아예 없는 객체는
   분석 데이터가 부족한 것이지 '기준 미달'이 아닙니다. 절대 포함하지 마십시오.

[출력 JSON 스키마]
반드시 아래 구조의 JSON 객체만 반환하고, 부가 설명은 절대 포함하지 마십시오.
{{
  "violations": [
    {{
      "equipment_id":   "도면 데이터의 id 필드 값 (TAG_NAME 또는 handle)",
      "violation_type": "{_FIRE_VIOLATION_TYPES_STR} 중 정확히 하나",
      "reference_rule": "위반 근거 NFSC 원문 (직접 인용)",
      "current_value":  "현재 도면 수치 (예: 3.5m, 없음)",
      "required_value": "규정 요구 수치 (예: 2.3m 이하)",
      "reason":         "논리적 위반 사유 요약 (한국어)",
      "severity":       "CRITICAL 또는 WARNING"
    }}
  ]
}}"""


def _dedupe_violations(violations: list) -> list:
    seen: set[tuple] = set()
    out: list = []
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        reason = str(v.get("reason") or "")
        key = (
            str(v.get("equipment_id") or ""),
            str(v.get("violation_type") or ""),
            reason[:120],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def _split_elements_by_json_size(elements: list) -> list[list]:
    if not elements:
        return []
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
        """
        파싱 dict 전체에 대해 — 한 번에 컨텍스트 한도를 넘으면 elements 를 나눠
        check_compliance 를 여러 번 호출한 뒤 violations 를 합친다.
        """
        if not spec_context or not (spec_context or "").strip():
            return []
        elements = (parsed or {}).get("elements") or []
        if not elements:
            return []
        layout_str = json.dumps(parsed, ensure_ascii=False)
        spec_n = len(spec_context or "")
        lay_n = len(layout_str)
        if (
            lay_n <= _MAX_LAYOUT_DATA_CHARS
            and spec_n <= _MAX_SPEC_CONTEXT_CHARS
            and (lay_n + spec_n) < 70_000
        ):
            return await self.check_compliance(target_id, spec_context, layout_str)

        parts = _split_elements_by_json_size(elements)
        total = len(parts)
        if not parts:
            return []

        logging.info(
            "[FireComplianceAgent] LLM 다중 청크 검토(병렬, 동시최대=%s): elements=%s → %s회",
            _MAX_COMPLIANCE_LLM_CONCURRENT,
            len(elements),
            total,
        )
        sem = asyncio.Semaphore(_MAX_COMPLIANCE_LLM_CONCURRENT)

        async def _one(k: int, el_chunk: list) -> list:
            async with sem:
                sub: dict = {"elements": el_chunk}
                if k == 0 and isinstance(parsed, dict):
                    for key in ("error", "metadata", "fire_topology"):
                        if key in parsed and parsed.get(key) is not None:
                            sub[key] = parsed[key]
                sub_str = json.dumps(sub, ensure_ascii=False)
                t_id = target_id
                if (target_id or "") == "ALL" and el_chunk:
                    t_id = (
                        el_chunk[0].get("id")
                        if isinstance(el_chunk[0], dict)
                        else None
                    ) or target_id
                info = (
                    f"\n(검토 구간: {k + 1}/{total} 청크 — 이 JSON elements 안의 설비만 평가. "
                    f"다른 구간은 별도 병렬 호출.)"
                )
                return await self.check_compliance(
                    t_id, spec_context, sub_str, extra_user_suffix=info
                )

        chunk_results = await asyncio.gather(
            *(_one(k, parts[k]) for k in range(total)),
            return_exceptions=True,
        )
        merged: list = []
        for k, r in enumerate(chunk_results):
            if isinstance(r, Exception):
                logging.error(
                    "[FireComplianceAgent] chunk %s/%s LLM 실패: %s", k + 1, total, r
                )
                continue
            if not isinstance(r, list):
                continue
            merged.extend(r)
        return _dedupe_violations(merged)

    async def check_compliance(
        self, target_id: str, spec_context: str, layout_data: str, *, extra_user_suffix: str = ""
    ) -> list:
        spec_in = (spec_context or "")[:_MAX_SPEC_CONTEXT_CHARS]
        if len(spec_context or "") > _MAX_SPEC_CONTEXT_CHARS:
            spec_in += "\n\n[... NFSC 본문이 길어 앞부분만 포함되었습니다.]"

        layout_in = (layout_data or "")[:_MAX_LAYOUT_DATA_CHARS]
        if len(layout_data or "") > _MAX_LAYOUT_DATA_CHARS:
            layout_in += "\n\n[... 도면 elements JSON이 컨텍스트 한도로 잘렸습니다.]"

        user_prompt = (
            f"[대상 설비 ID]: {target_id}\n\n"
            f"[NFSC 법규 규정]\n{spec_in}\n\n"
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
            if "sLLM" in result or "연결" in result:
                logging.warning("[FireComplianceAgent] LLM 호출 실패, violations 생략: %s", (result or "")[:200])
                return []
            try:
                d = json.loads(result)
            except Exception:
                return []
            return d.get("violations", []) if isinstance(d, dict) else []
        return []
