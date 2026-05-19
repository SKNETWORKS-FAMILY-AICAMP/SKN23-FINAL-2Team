"""
File    : backend/services/evaluation/eval_tracker.py
Author  : 김민정
Create  : 2026-04-17
Description : 에이전트 분석 결과에 대한 KPI 지표 측정 및 Langfuse 기록 전담 서비스.
              4개 도메인(arch/pipe/fire/elec) 모두 동일 진입점을 사용한다.

Modification History :
    - 2026-04-17 (김민정) : 초기 구조 생성 및 KPI 스코어러 연동
    - 2026-05-09 : 대시보드 스펙 정합 — agent_result 에서 escalation·parsing·errors·markings
                   신호를 끌어와 신규 4개 KPI(e2e/escalation/valid_escalation/markup_pos)와
                   기존 agent_error_rate/sllm_latency 가 의미있는 값을 갖도록 wiring.
    - 2026-05-09 : 실시간 정확도 추정 추가 —
                   ① retrieved_laws → RetrievedChunk 변환으로 rag_retrieval_relevance 활성
                   ② response_meta.deterministic_equipment_ids 로 recall_lower_bound 산출
                   ③ EVAL_LLM_JUDGE_ENABLED=true 시 LLM-as-Judge precision 호출
                   ④ response_meta.sllm_durations_ms 로 sllm_latency_ms 활성
"""

import time
import logging
from typing import Any

from typing import Awaitable, Callable

from backend.core.config import settings
from backend.services.evaluation.kpi_score import CADAgentScorer
from backend.services.evaluation.llm_judge import judge_violations, precision_from_verdicts
from backend.services.evaluation.self_consistency import precision_self_consistency
from backend.services.evaluation.schemas import (
    Violation,
    MarkupResult,
    RetrievedChunk,
)

# 공용 스코어러 인스턴스
_scorer = CADAgentScorer()


def _extract_position(v: dict) -> tuple[float, float]:
    """위반 항목에서 좌표 추출. 직접 (x,y) 가 없으면 proposed_action / bbox 에서 유추."""
    x = v.get("x")
    y = v.get("y")
    if x is not None and y is not None:
        try:
            return float(x), float(y)
        except (TypeError, ValueError):
            pass

    pa = v.get("proposed_action") or {}
    if isinstance(pa, dict):
        for key in ("anchor_position", "touch_point", "new_start", "cloud_from"):
            p = pa.get(key)
            if isinstance(p, dict) and "x" in p and "y" in p:
                try:
                    return float(p["x"]), float(p["y"])
                except (TypeError, ValueError):
                    continue

    bbox = v.get("bbox")
    if isinstance(bbox, dict):
        try:
            return (
                (float(bbox["x1"]) + float(bbox["x2"])) / 2,
                (float(bbox["y1"]) + float(bbox["y2"])) / 2,
            )
        except (KeyError, TypeError, ValueError):
            pass
    return 0.0, 0.0


def _is_low_confidence(v: dict) -> bool:
    score = v.get("confidence_score")
    try:
        return score is not None and float(score) < 0.7
    except (TypeError, ValueError):
        return False


def _laws_to_retrieved_chunks(retrieved_laws: list) -> list[RetrievedChunk]:
    """AgentState.retrieved_laws (LawReference dict 리스트) → RetrievedChunk 리스트."""
    out: list[RetrievedChunk] = []
    for law in retrieved_laws or []:
        if not isinstance(law, dict):
            continue
        ref = str(law.get("legal_reference") or "")
        # legal_reference 를 "source article" 형태로 분해 (없으면 통째로 source 에 넣음)
        parts = ref.split(maxsplit=1)
        source = parts[0] if parts else ref
        article = parts[1] if len(parts) > 1 else ""
        try:
            sim = float(law.get("score") or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        out.append(
            RetrievedChunk(
                chunk_id=str(law.get("chunk_id") or law.get("document_chunk_id") or ""),
                content=str(law.get("snippet") or ""),
                similarity=sim,
                source=source,
                article=article,
            )
        )
    return out


def _used_chunk_ids(violations: list, chunks: list[RetrievedChunk]) -> list[str]:
    """위반의 legal_reference 와 매칭되는 chunk_id 집합 (rag_retrieval_relevance 분자)."""
    if not chunks:
        return []
    used: set[str] = set()
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        cite = str(v.get("legal_reference") or v.get("reference_rule") or "").strip()
        if not cite:
            continue
        for c in chunks:
            full = f"{c.source} {c.article}".strip()
            if full and (full in cite or cite in full):
                used.add(c.chunk_id)
    return list(used)


async def track_cad_review_metrics(
    session_id: str,
    domain: str,
    agent_result: dict[str, Any],
    start_time: float,
    cad_payload: dict[str, Any] = None,
    drawing_data: dict[str, Any] = None,
    *,
    self_consistency_runner: Callable[[], Awaitable[list[dict]]] | None = None,
):
    """
    에이전트 분석 결과를 지표 규격으로 변환하고 Langfuse 에 기록한다.

    실시간 정확도 측정:
      - rag_retrieval_relevance : retrieved_laws 와 위반 인용 매칭으로 산출 (즉시 가능)
      - recall_lower_bound      : deterministic 위반 ⊂ LLM 위반 비율 (즉시 가능)
      - precision_judge         : LLM-as-Judge (settings.EVAL_LLM_JUDGE_ENABLED 시에만)
      - f1_estimate             : precision_judge 와 recall_lower_bound 모두 있을 때만

    골든셋 기반 KPI(f1_score / citation_accuracy / coord_error_mm 등)는 ground_truths 가
    없는 실시간 호출에서는 0 으로 기록된다 — test_extraction.py / test_rag.py 같은
    별도 평가에서만 의미있는 값이 나온다.
    """
    try:
        end_time = time.time()

        # 1. 위반 사항
        review_result = agent_result.get("review_result") or {}
        raw_violations = review_result.get("violations") or []
        predictions: list[Violation] = []
        for v in raw_violations:
            if not isinstance(v, dict):
                continue
            x, y = _extract_position(v)
            predictions.append(
                Violation(
                    id=str(v.get("object_id") or v.get("equipment_id") or ""),
                    description=str(v.get("reason") or v.get("suggestion") or ""),
                    citation=str(v.get("legal_reference") or v.get("reference_rule") or ""),
                    x=x,
                    y=y,
                )
            )

        # 2. 마킹 — pending_fixes 기반. RES-03/04 가 revcloud + mtext 둘 다 만들도록 설계되어 있어 True/True.
        pending_fixes = agent_result.get("pending_fixes") or []
        markup_results = [
            MarkupResult(
                violation_id=str(f.get("fix_id") or ""),
                has_revcloud=True,
                has_mtext=True,
            )
            for f in pending_fixes
            if isinstance(f, dict)
        ]

        # 3. 객체 개수
        total_cad_objects = 0
        if cad_payload and "entities" in cad_payload:
            total_cad_objects = len(cad_payload["entities"])
        elif drawing_data and "entities" in drawing_data:
            total_cad_objects = len(drawing_data["entities"])
        parsed_cad_objects = (
            len(drawing_data["entities"]) if drawing_data and "entities" in drawing_data else 0
        )

        # 4. 운영 안정성 신호 ─────────────────────────────────────────────
        current_step = str(agent_result.get("current_step") or "")
        is_error = current_step == "error"
        is_parsing_ok = parsed_cad_objects > 0
        is_marking_ok = (not predictions) or bool(pending_fixes)

        rmeta = agent_result.get("response_meta") or {}
        review_categories = (rmeta.get("review_categories") or {}) if isinstance(rmeta, dict) else {}
        low_conf_count = sum(1 for v in raw_violations if isinstance(v, dict) and _is_low_confidence(v))
        low_conf_meta = int(review_categories.get("low_confidence_count") or 0)
        escalation_triggered = bool(low_conf_count or low_conf_meta)

        # 정당한 에스컬레이션 / 마킹 위치 수락은 사후 사람 피드백 채널이 있어야 결정 가능
        is_valid_escalation = False
        accepted_count = 0
        total_markings = len(pending_fixes)

        errors = [{"step": current_step, "kind": "agent_error"}] if is_error else []
        total_steps = 1

        # sLLM 지연 — 도메인 노드(예: pipe_review_node) 가 response_meta.sllm_durations_ms 로 노출
        sllm_durations_ms = rmeta.get("sllm_durations_ms") if isinstance(rmeta, dict) else None
        if not isinstance(sllm_durations_ms, list):
            sllm_durations_ms = []

        runtime_meta = agent_result.get("runtime_meta") or {}
        try:
            cost = float(runtime_meta.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0

        model_name = settings.LLM_MODEL_NAME or ""

        # 5. RAG 신호 — retrieved_laws → chunks → used_chunk_ids
        retrieved_laws = agent_result.get("retrieved_laws") or []
        retrieved_chunks = _laws_to_retrieved_chunks(retrieved_laws)
        used_chunk_ids = _used_chunk_ids(raw_violations, retrieved_chunks)

        # 6. 실시간 정확도 추정 신호 ──────────────────────────────────────
        # 6-A. deterministic 커버리지 (recall_lower_bound)
        det_ids_raw = rmeta.get("deterministic_equipment_ids") if isinstance(rmeta, dict) else None
        deterministic_ids: set[str] | None = None
        prediction_ids: set[str] | None = None
        if isinstance(det_ids_raw, list) and det_ids_raw:
            deterministic_ids = {str(x) for x in det_ids_raw if x}
            prediction_ids = {p.id for p in predictions if p.id}

        # 6-B. LLM-as-Judge precision (MVP 한정 옵션)
        precision_judge: float | None = None
        if settings.EVAL_LLM_JUDGE_ENABLED and raw_violations:
            try:
                verdicts = await judge_violations(raw_violations, retrieved_laws)
                precision_judge = precision_from_verdicts(verdicts)
                if verdicts:
                    logging.info(
                        "[EvalTracker] LLM-judge: %d/%d valid (precision=%.3f) on %d sampled",
                        sum(1 for v in verdicts if v.is_valid), len(verdicts),
                        precision_judge or 0.0, len(verdicts),
                    )
            except Exception as exc:
                logging.warning("[EvalTracker] LLM-judge 실패: %s", exc)

        # 6-C. Self-consistency precision (sLLM 단독 시대 옵션)
        # judge 와 동시 사용 가능하지만, 보통 둘 중 하나만 켜는 것을 권장.
        if (
            settings.EVAL_SELF_CONSISTENCY_ENABLED
            and self_consistency_runner is not None
            and raw_violations
        ):
            try:
                runs = max(1, settings.EVAL_SELF_CONSISTENCY_RUNS)
                runners = [self_consistency_runner for _ in range(runs)]
                proxy, info = await precision_self_consistency(
                    raw_violations,
                    runners,
                    quorum=settings.EVAL_SELF_CONSISTENCY_QUORUM,
                )
                if proxy is not None:
                    # judge 가 이미 값이 있으면 덮어쓰지 않음 (둘 다 켠 경우 judge 우선).
                    if precision_judge is None:
                        precision_judge = proxy
                    logging.info("[EvalTracker] self-consistency proxy=%.3f info=%s", proxy, info)
            except Exception as exc:
                logging.warning("[EvalTracker] self-consistency 실패: %s", exc)

        # 7. 스코어러 호출 (Langfuse 기록 포함)
        _scorer.evaluate(
            trace_id=session_id,
            domain=domain,
            predictions=predictions,
            ground_truths=[],   # 실시간 분석에선 정답셋 없음 → 골든셋 KPI 는 0
            retrieved_chunks=retrieved_chunks,
            used_chunk_ids=used_chunk_ids,
            markup_results=markup_results,
            start_time=start_time,
            end_time=end_time,
            sllm_durations_ms=sllm_durations_ms,
            errors=errors,
            total_steps=total_steps,
            total_cad_objects=total_cad_objects,
            parsed_cad_objects=parsed_cad_objects,
            cost=cost,
            is_parsing_ok=is_parsing_ok,
            is_marking_ok=is_marking_ok,
            escalation_triggered=escalation_triggered,
            is_valid_escalation=is_valid_escalation,
            accepted_count=accepted_count,
            total_markings=total_markings,
            model_name=model_name,
            precision_judge=precision_judge,
            deterministic_violation_ids=deterministic_ids,
            prediction_ids_for_recall=prediction_ids,
        )

        logging.info(
            "[EvalTracker] KPI Scoring completed session=%s domain=%s "
            "violations=%d pending=%d esc=%s parse_ok=%s mark_ok=%s err=%s "
            "rag_chunks=%d det=%d judge=%s",
            session_id, domain, len(predictions), len(pending_fixes),
            escalation_triggered, is_parsing_ok, is_marking_ok, is_error,
            len(retrieved_chunks),
            len(deterministic_ids or ()),
            f"{precision_judge:.2f}" if precision_judge is not None else "off",
        )

    except Exception as e:
        logging.error(f"[EvalTracker] Error during KPI scoring: {str(e)}", exc_info=True)
