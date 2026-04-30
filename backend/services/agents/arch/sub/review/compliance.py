"""
File    : backend/services/agents/architecture/sub/review/compliance.py
Author  : 김다빈
WBS     : AI-05 (건축 도메인 에이전트)
Create  : 2026-04-15
Modified: 2026-04-25 (O(n) greedy 청크 분할 + _dedupe_violations 개선)

Description :
    건축법 시행령 기준 도면 정합성 검증 에이전트 (RAG 문서 + LLM 추론).
    ParserAgent가 반환한 ArchDrawingContext + RAG 검색된 법규 원문을 기반으로
    위반 항목을 추출합니다.

    ComplianceAgent는 오직 제공된 [건축법 규정]만을 근거로 판단하며,
    사전 학습 지식을 단독으로 사용해서는 안 됩니다.

    청크 분할 전략:
        - _split_elements_by_json_size(): elements[]를 JSON 크기(_MAX_LAYOUT_PER_CHUNK_CHARS) 기준으로
          O(n) greedy packing 분할. 기존 O(n²) 대비 대규모 도면(500+ 요소)에서 수십배 빠름.
          각 원소 크기를 1회만 직렬화하고, overhead + sep(콤마 2바이트)를 누적하여 분할점 결정.
        - 분할된 청크는 _MAX_COMPLIANCE_LLM_CONCURRENT(=4) 병렬로 LLM 호출.

    중복 제거 전략:
        - _dedupe_violations(): (handle, violation_type) 쌍을 key로,
          reason 문자열이 더 긴 항목(= 더 상세한 설명)을 우선 유지.

Modification History :
    - 2026-04-15 (김다빈) : 초기 구현
    - 2026-04-25 (김다빈) : O(n) greedy 청크 분할(_split_elements_by_json_size) 교체.
                            _dedupe_violations 도입으로 중복 위반 항목 제거.
"""

import asyncio
import json
import logging

from backend.services.agents.arch.schemas import ArchViolationType
from backend.services import llm_service

_MAX_SPEC_CONTEXT_CHARS = 40_000
_MAX_LAYOUT_DATA_CHARS = 200_000
_MAX_LAYOUT_PER_CHUNK_CHARS = 50_000
_MAX_COMPLIANCE_LLM_CONCURRENT = 4

def _dedupe_violations(violations: list) -> list:
    """
    (handle, violation_type) 쌍을 키로 위반 항목 중복을 제거합니다.

    복수 LLM 청크 호출 시 동일 엔티티에 대한 위반이 중복으로 반환될 수 있습니다.
    reason 문자열 길이가 더 긴 항목(= 더 상세한 설명)을 우선 보존합니다.

    Parameters
    ----------
    violations : list[dict]
        LLM이 반환한 원시 위반 항목 목록 (청크별 누적).

    Returns
    -------
    list[dict]
        중복 제거된 위반 항목 목록. 입력 순서(먼저 등장한 항목) 기준으로 정렬.
    """
    best: dict[tuple, dict] = {}
    for v in violations or []:
        if not isinstance(v, dict): continue
        key = (
            str(v.get("handle") or ""),
            str(v.get("violation_type") or ""),
        )
        existing = best.get(key)
        if existing is None or len(str(v.get("reason") or "")) > len(str(existing.get("reason") or "")):
            best[key] = v
    return list(best.values())

def _split_elements_by_json_size(elements: list) -> list[list]:
    """
    elements 리스트를 _MAX_LAYOUT_PER_CHUNK_CHARS 이하의 청크로 greedy 분할합니다.

    알고리즘 : O(n) greedy packing
        1. 모든 원소를 json.dumps로 1회 직렬화하여 크기 캐시(el_sizes) 생성.
        2. 누적 크기(overhead + separator + 원소)가 한도를 초과하면 현재 청크를 확정하고 새 청크 시작.
        3. separator는 JSON 배열 원소 구분자인 ", " (2바이트).

    기존 O(n²) 방식과의 차이:
        - 기존: 원소를 추가할 때마다 전체 청크를 json.dumps하여 크기 측정 → n번 재직렬화
        - 현재: 원소 크기를 미리 1회 계산하고 누적합으로 분할점 결정 → n번 직렬화 1회

    Parameters
    ----------
    elements : list
        분할할 elements 목록 (이미 _slim_el()로 경량화된 dict).

    Returns
    -------
    list[list]
        각 서브리스트는 JSON 직렬화 시 _MAX_LAYOUT_PER_CHUNK_CHARS 이하임이 보장.
        빈 elements이면 빈 리스트 반환.
    """
    if not elements:
        return []
    # 각 원소 크기 미리 계산 (1회 직렬화)
    overhead = len('{"elements": []}') + 2
    el_sizes = [len(json.dumps(e, ensure_ascii=False)) for e in elements]

    chunks: list[list] = []
    current: list = []
    current_size = overhead

    for e, s in zip(elements, el_sizes):
        sep = 2 if current else 0  # 콤마 + 공백
        if current and current_size + sep + s > _MAX_LAYOUT_PER_CHUNK_CHARS:
            chunks.append(current)
            current = [e]
            current_size = overhead + s
        else:
            current.append(e)
            current_size += sep + s

    if current:
        chunks.append(current)
    return chunks

_VIOLATION_TYPES_STR = " | ".join(v.value for v in ArchViolationType)

_SYSTEM_PROMPT = f"""당신은 CAD-SLLM Agent 파이프라인의 '건축법 규정 검증 서브 에이전트'입니다.
오직 RAG를 통해 제공된 [건축법 규정]만을 근거로 [도면 데이터]의 정합성을 평가하십시오.
사전 학습된 외부 지식을 단독으로 개입시켜서는 안 됩니다.

[검증 지침]
1. 규정 수치 추출: 방화구획 면적, 복도·통로 최소 폭, 피난 거리, 계단 폭·단높이·단너비,
   층고 최솟값, 채광·환기창 면적 비율, 방화문 규격, 벽체 두께, 최소 실 면적 등을 파악합니다.
2. 상태 비교: 도면 수치(mm² / mm)와 기준값을 직접 대조하여 위반 여부를 판단합니다.
3. handle 식별: 위반 엔티티의 handle 값(CAD 고유 ID)을 반드시 포함합니다.
4. 무결성 확인: 위반 사항이 없다면 violations를 빈 배열로 반환합니다.

[출력 JSON 스키마]
반드시 아래 구조의 JSON 객체만 반환하고, 부가 설명은 절대 포함하지 마십시오.
{{
  "violations": [
    {{
      "handle": "위반 엔티티 handle (CAD JSON의 handle 값 그대로)",
      "entity_type": "DIMENSION | HATCH | INSERT | LWPOLYLINE 등",
      "layer": "레이어 이름",
      "violation_type": "{_VIOLATION_TYPES_STR} 중 하나",
      "reference_rule": "위반 근거 법규 원문 (조항명 포함)",
      "current_value": "현재 도면 수치 (예: 800mm, 1200mm²)",
      "required_value": "규정 요구 수치 (예: 1200mm 이상, 3000mm² 이하)",
      "severity": "Critical | Major | Minor",
      "reason": "논리적 위반 사유 요약"
    }}
  ]
}}"""


class ComplianceAgent:
    async def check_compliance(
        self,
        drawing_context: dict,
        spec_context: str,
        focus_area: str = "",
    ) -> list:
        """
        Parameters
        ----------
        drawing_context : ArchParserAgent.parse() 반환값
        spec_context    : QueryAgent가 검색한 건축법 규정 원문
        focus_area      : 검토 집중 영역 힌트 (빈 문자열 = 전체)
        """
        elements = (drawing_context or {}).get("elements", [])
        ctx_json = json.dumps(drawing_context, ensure_ascii=False)
        spec_n = len(spec_context or "")
        lay_n = len(ctx_json)

        if lay_n <= _MAX_LAYOUT_DATA_CHARS and spec_n <= _MAX_SPEC_CONTEXT_CHARS and (lay_n + spec_n) < 250_000:
            return await self._check_chunk(ctx_json, spec_context, focus_area)

        parts = _split_elements_by_json_size(elements)
        total = len(parts)
        if not parts:
            return []

        sem = asyncio.Semaphore(_MAX_COMPLIANCE_LLM_CONCURRENT)

        async def _one(k: int, el_chunk: list) -> list:
            async with sem:
                sub: dict = {"elements": el_chunk}
                # Preserve summary or other context in the first chunk if needed
                if k == 0 and isinstance(drawing_context, dict):
                    for key in ("drawing_unit", "unit_factor", "classified_summary"):
                        if key in drawing_context:
                            sub[key] = drawing_context[key]
                sub_str = json.dumps(sub, ensure_ascii=False)
                info = focus_area + f"\n(검토 구간: {k + 1}/{total} 청크)"
                return await self._check_chunk(sub_str, spec_context, info)

        chunk_results = await asyncio.gather(*(_one(k, parts[k]) for k in range(total)), return_exceptions=True)
        merged: list = []
        for r in chunk_results:
            if isinstance(r, list):
                merged.extend(r)
        
        return _dedupe_violations(merged)

    async def _check_chunk(self, ctx_json: str, spec_context: str, focus_area: str) -> list:
        focus_hint = f"\n[검토 집중 영역]: {focus_area}" if focus_area else ""

        user_prompt = f"""
[건축법 규정]:
{spec_context[:_MAX_SPEC_CONTEXT_CHARS]}

[도면 데이터 요약]:
{ctx_json[:_MAX_LAYOUT_DATA_CHARS]}
{focus_hint}

위 데이터를 분석하여 JSON 스키마에 맞게 위반 결과를 출력하십시오.
위반이 없으면 violations를 빈 배열로 반환하십시오.
"""
        try:
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
                return json.loads(result).get("violations", [])
        except Exception as e:
            logging.error(f"[ComplianceAgent] Arch check error: {e}")
            
        return []
