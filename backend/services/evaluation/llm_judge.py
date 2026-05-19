"""
File    : backend/services/evaluation/llm_judge.py
Author  : 송주엽
Create  : 2026-05-09
Description : 실시간 precision 측정을 위한 LLM-as-Judge.
              에이전트가 emit 한 위반 각각을 별도 strong LLM(GPT-4o 계열)에 던져
              "진짜 위반인가" 를 binary 로 판정한다.

설계 메모:
  - MVP 한정 사용. sLLM 단독 production 으로 가면 self-judge 가 되어 신뢰도가 급락.
    그 시점에는 self_consistency.py 로 교체.
  - 비용 통제를 위해 도면당 최대 EVAL_LLM_JUDGE_MAX_SAMPLE 건만 호출 (랜덤 샘플).
  - 판정 결과는 골든셋 시드로 누적할 수 있도록 raw verdict 도 함께 반환.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass

import httpx

from backend.core.config import settings


@dataclass
class JudgeVerdict:
    violation_id: str
    is_valid: bool          # 심판이 "진짜 위반" 으로 인정?
    confidence: float       # 심판 자기 신뢰도 (0~1)
    reason: str             # 짧은 설명 (골든셋 시드용)


_JUDGE_SYSTEM_PROMPT = """당신은 한국 건설/설비 도면의 법규 검토를 감수하는 시니어 엔지니어입니다.

아래의 [위반 후보] 가 [근거 법규] 에 비추어 진짜 위반인지 판정합니다.
판정 결과는 binary 로만 답하고, 추가 설명은 reason 에 1문장 이내로 적습니다.

판정 기준:
  - 인용된 법규 조항이 해당 객체 유형/도메인에 실제로 적용되는가?
  - 도면 데이터(레이어, 속성)가 위반 사유의 전제 조건을 만족하는가?
  - "data_unavailable", "도면에 명시 없음" 같은 회피 사유로 만든 위반은 invalid.

응답 JSON 스키마:
{
  "is_valid": true | false,
  "confidence": 0.0 ~ 1.0,
  "reason": "한 문장으로 판정 근거"
}
"""


def _violation_to_prompt(violation: dict, retrieved_laws: list[dict]) -> str:
    """위반 1건 + 근거 법규 본문을 심판용 user 메시지로 정리."""
    parts: list[str] = []
    parts.append("[위반 후보]")
    for k in (
        "object_id", "violation_type", "reason",
        "legal_reference", "current_value", "required_value",
    ):
        v = violation.get(k)
        if v not in (None, "", 0):
            parts.append(f"  - {k}: {v}")

    cite = (violation.get("legal_reference") or "").strip()
    matched_laws: list[dict] = []
    if cite and retrieved_laws:
        for law in retrieved_laws:
            ref = str(law.get("legal_reference") or "")
            if ref and (ref in cite or cite in ref):
                matched_laws.append(law)

    parts.append("")
    parts.append("[근거 법규 본문]")
    if matched_laws:
        for i, law in enumerate(matched_laws[:2], 1):
            snippet = (law.get("snippet") or "")[:600]
            parts.append(f"  ({i}) {law.get('legal_reference', '')}")
            parts.append(f"      {snippet}")
    else:
        parts.append("  (해당 법규 본문이 RAG 검색 결과에 없음 — 인용 자체가 의심됨)")

    return "\n".join(parts)


async def _call_judge(messages: list[dict]) -> dict | None:
    """OpenAI Chat Completions 직접 호출. llm_service 와 분리한 이유:
    심판은 USE_SLLM 플래그와 무관하게 항상 강한 외부 모델을 사용해야 한다.
    """
    if not settings.OPENAI_API_KEY:
        logging.info("[LLM-Judge] OPENAI_API_KEY 미설정 → judge 호출 생략")
        return None

    payload = {
        "model": settings.EVAL_LLM_JUDGE_MODEL,
        "messages": messages,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 256,
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            if resp.status_code != 200:
                logging.warning("[LLM-Judge] API %s: %s", resp.status_code, resp.text[:200])
                return None
            content = resp.json()["choices"][0]["message"].get("content") or ""
            return json.loads(content)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
        logging.warning("[LLM-Judge] 호출 실패: %s", e)
        return None


async def judge_violations(
    violations: list[dict],
    retrieved_laws: list[dict],
    *,
    max_sample: int | None = None,
) -> list[JudgeVerdict]:
    """위반 후보를 strong LLM 으로 검증. 비활성/실패 시 빈 리스트 반환.

    호출자는 빈 리스트 → 심판 데이터 없음으로 간주, precision_judge 는 emit 생략하면 됨.
    """
    if not settings.EVAL_LLM_JUDGE_ENABLED:
        return []
    if not violations:
        return []

    cap = max_sample if max_sample is not None else settings.EVAL_LLM_JUDGE_MAX_SAMPLE
    if cap > 0 and len(violations) > cap:
        sample = random.sample(violations, cap)
    else:
        sample = list(violations)

    async def _one(v: dict) -> JudgeVerdict | None:
        user_msg = _violation_to_prompt(v, retrieved_laws)
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        result = await _call_judge(messages)
        if not isinstance(result, dict):
            return None
        try:
            return JudgeVerdict(
                violation_id=str(v.get("object_id") or v.get("equipment_id") or ""),
                is_valid=bool(result.get("is_valid", False)),
                confidence=float(result.get("confidence") or 0.0),
                reason=str(result.get("reason") or "")[:200],
            )
        except (TypeError, ValueError):
            return None

    results = await asyncio.gather(*(_one(v) for v in sample), return_exceptions=False)
    return [r for r in results if r is not None]


def precision_from_verdicts(verdicts: list[JudgeVerdict]) -> float | None:
    """심판 판정 → precision 추정치. 판정 없으면 None (emit 생략 신호)."""
    if not verdicts:
        return None
    return sum(1 for v in verdicts if v.is_valid) / len(verdicts)
