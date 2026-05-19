"""
File    : backend/services/evaluation/self_consistency.py
Author  : 송주엽
Create  : 2026-05-09
Description : sLLM 단독 production 시대용 — 같은 도면을 N회 호출 한 결과의 합의 비율로
              precision 을 추정한다. LLM-as-Judge 가 self-judge 가 되어 신뢰를 잃는
              시점의 대체 도구.

설계 메모:
  - 이미 emit 된 review_result.violations 와 함께 추가 N-1 회 sLLM 호출 결과가 필요.
    eval_tracker 가 같은 워크플로우를 N-1 번 더 돌리는 비용을 부담해야 하므로,
    settings.EVAL_SELF_CONSISTENCY_ENABLED=true 일 때만 활성. 비용은 N 배.
  - 합의 기준: 같은 (object_id, violation_type) 키가 다수 실행에서 반복 등장.
  - precision_proxy = 다수결 위반 / 1차 실행 위반.
  - "다수결" 임계는 EVAL_SELF_CONSISTENCY_QUORUM (기본 ceil(N/2)).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from typing import Any, Awaitable, Callable

from backend.core.config import settings


def _violation_key(v: dict) -> tuple[str, str]:
    """합의 비교용 키 — object_id + violation_type 으로 같은 위반인지 판정."""
    obj = str(v.get("object_id") or v.get("equipment_id") or v.get("handle") or "")
    vtype = str(v.get("violation_type") or "")
    return obj, vtype


async def _safe_run(runner: Callable[[], Awaitable[list[dict]]]) -> list[dict]:
    try:
        result = await runner()
    except Exception as exc:
        logging.warning("[SelfConsistency] 추가 실행 실패: %s", exc)
        return []
    return result if isinstance(result, list) else []


async def precision_self_consistency(
    primary_violations: list[dict],
    additional_runners: list[Callable[[], Awaitable[list[dict]]]],
    *,
    quorum: int | None = None,
) -> tuple[float | None, dict[str, int]]:
    """다회 실행 결과의 합의 기반 precision 추정.

    primary_violations: 이미 사용자에게 보여준 1차 실행 결과 (재사용, 추가 LLM 호출 없음).
    additional_runners: 추가로 N-1 번 sLLM 을 다시 호출하는 awaitable 팩토리. 각 호출은
        같은 도면/같은 컨텍스트로 sLLM 만 다시 돌려 list[dict] 위반을 반환해야 한다.
    quorum: 다수결 임계. None 이면 ceil(N/2).

    반환: (precision_proxy, debug_info). 추가 실행이 실패해 합의 데이터가 부족하면 (None, info).
    """
    if not settings.EVAL_SELF_CONSISTENCY_ENABLED:
        return None, {"reason": "disabled"}
    if not primary_violations:
        return None, {"reason": "no_primary_violations"}
    if not additional_runners:
        return None, {"reason": "no_additional_runners"}

    extra_results = await asyncio.gather(*(_safe_run(r) for r in additional_runners))
    runs = [primary_violations] + [r for r in extra_results if r]
    n_runs = len(runs)
    if n_runs < 2:
        return None, {"reason": "insufficient_runs", "n_runs": n_runs}

    q = quorum if quorum is not None else math.ceil(n_runs / 2)

    # 각 위반 키가 몇 회 실행에서 등장했는지 집계
    counter: Counter[tuple[str, str]] = Counter()
    for run_violations in runs:
        seen_in_this_run: set[tuple[str, str]] = set()
        for v in run_violations or []:
            if isinstance(v, dict):
                seen_in_this_run.add(_violation_key(v))
        for key in seen_in_this_run:
            counter[key] += 1

    primary_keys = {_violation_key(v) for v in primary_violations if isinstance(v, dict)}
    if not primary_keys:
        return None, {"reason": "primary_keys_empty"}

    consensus = sum(1 for key in primary_keys if counter.get(key, 0) >= q)
    proxy = consensus / len(primary_keys)
    return proxy, {
        "n_runs": n_runs,
        "quorum": q,
        "primary_count": len(primary_keys),
        "consensus_count": consensus,
    }
