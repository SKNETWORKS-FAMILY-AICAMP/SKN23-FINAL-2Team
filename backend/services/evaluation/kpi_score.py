"""
CAD Agent — KPI 스코어링 모듈

대시보드 스펙(2026-05-09 기준)을 따른다:
  ① 핵심 성능/정확도 (15개): f1_score, citation_accuracy, false_negative_rate,
     false_positive_rate, coord_error_mm, parsing_success_rate, chunk_hit_rate,
     rag_retrieval_relevance, markup_completeness, response_time_sec,
     sllm_latency_ms, agent_error_rate, review_consistency, cost_per_review,
     model_name
  ② 운영 안정성/실무 효용성 (4개): e2e_success_rate, escalation_rate,
     valid_escalation_rate, markup_position_acceptance_rate
  ③ Archived: embedding_similarity_avg, violation_count

review_consistency / valid_escalation_rate / cost_per_review 는 입력 신호가
있을 때만 emit (값 0 으로 dashboard 노이즈를 만들지 않기 위함).

사용법:
    from backend.services.evaluation.kpi_score import CADAgentScorer

    scorer = CADAgentScorer()
    result = scorer.evaluate(trace_id="xxx", domain="fire", ...)
"""

import math

from backend.core.observability import langfuse_client
from backend.services.evaluation.schemas import (
    Violation,
    RetrievedChunk,
    MarkupResult,
    EvalResult,
)


class CADAgentScorer:

    def __init__(self, langfuse=None):
        """
        langfuse: 외부 주입 가능 (테스트 시 mock).
                  미지정 시 core/observability.py 싱글턴 사용.
        """
        self.langfuse = langfuse or langfuse_client

    
    # 내부: 좌표 기반 위반 매칭
    

    def _match_violations(
        self,
        predictions: list[Violation],
        ground_truths: list[Violation],
        threshold_mm: float = 500.0,
    ) -> tuple[list[tuple], list[Violation], list[Violation]]:
        matched = []
        used_gt_ids = set()
        unmatched_preds = []

        for pred in predictions:
            best_gt = None
            best_dist = float("inf")
            for gt in ground_truths:
                if gt.id in used_gt_ids:
                    continue
                dist = math.sqrt((pred.x - gt.x) ** 2 + (pred.y - gt.y) ** 2)
                if dist < best_dist:
                    best_dist = dist
                    best_gt = gt
            if best_gt and best_dist <= threshold_mm:
                matched.append((pred, best_gt))
                used_gt_ids.add(best_gt.id)
            else:
                unmatched_preds.append(pred)

        unmatched_gts = [gt for gt in ground_truths if gt.id not in used_gt_ids]
        return matched, unmatched_preds, unmatched_gts

    
    # KPI 계산 메서드들
    

    def calc_f1(
        self, predictions: list[Violation], ground_truths: list[Violation],
    ) -> tuple[float, float, float]:
        matched, _, _ = self._match_violations(predictions, ground_truths)
        precision = len(matched) / len(predictions) if predictions else 0.0
        recall = len(matched) / len(ground_truths) if ground_truths else 0.0
        if precision + recall == 0:
            return 0.0, 0.0, 0.0
        f1 = 2 * precision * recall / (precision + recall)
        return f1, precision, recall

    def calc_citation_accuracy(
        self, predictions: list[Violation], ground_truths: list[Violation],
    ) -> float:
        matched, _, _ = self._match_violations(predictions, ground_truths)
        if not matched:
            return 0.0
        correct = sum(
            1 for pred, gt in matched
            if pred.citation.strip() == gt.citation.strip()
        )
        return correct / len(matched)

    def calc_coord_error(
        self, predictions: list[Violation], ground_truths: list[Violation],
    ) -> float:
        matched, _, _ = self._match_violations(predictions, ground_truths)
        if not matched:
            return 0.0
        total = sum(
            math.sqrt((p.x - g.x) ** 2 + (p.y - g.y) ** 2)
            for p, g in matched
        )
        return total / len(matched)

    def calc_false_positive_rate(
        self, predictions: list[Violation], ground_truths: list[Violation],
    ) -> float:
        _, unmatched_preds, _ = self._match_violations(predictions, ground_truths)
        return len(unmatched_preds) / len(predictions) if predictions else 0.0

    def calc_false_negative_rate(
        self, predictions: list[Violation], ground_truths: list[Violation],
    ) -> float:
        _, _, unmatched_gts = self._match_violations(predictions, ground_truths)
        return len(unmatched_gts) / len(ground_truths) if ground_truths else 0.0

    def calc_rag_relevance(
        self, retrieved_chunks: list[RetrievedChunk], used_chunk_ids: list[str],
    ) -> float:
        if not retrieved_chunks:
            return 0.0
        used = sum(1 for c in retrieved_chunks if c.chunk_id in used_chunk_ids)
        return used / len(retrieved_chunks)

    def calc_chunk_hit_rate(
        self, retrieved_chunks: list[RetrievedChunk], ground_truth_citations: list[str],
    ) -> float:
        if not ground_truth_citations:
            return 0.0
        retrieved_sources = {
            f"{c.source} {c.article}".strip() for c in retrieved_chunks
        }
        hits = sum(
            1 for citation in ground_truth_citations
            if citation.strip() in retrieved_sources
        )
        return hits / len(ground_truth_citations)

    def calc_e2e_success_rate(self, is_parsing_ok: bool, is_marking_ok: bool) -> float:
        """파싱부터 마킹까지 전 과정이 모두 성공했는지 확인"""
        return 1.0 if is_parsing_ok and is_marking_ok else 0.0

    def calc_escalation_rate(self, escalation_triggered: bool) -> float:
        """AI의 판단 보류(사람 검토 요청) 발생 여부"""
        return 1.0 if escalation_triggered else 0.0

    def calc_valid_escalation_rate(self, escalation_triggered: bool, is_valid_escalation: bool) -> float | None:
        """판단 보류가 정당했는지 확인 (보류 발생 시에만 계산)"""
        if not escalation_triggered:
            return None
        return 1.0 if is_valid_escalation else 0.0

    def calc_markup_position_acceptance_rate(self, accepted_count: int, total_markings: int) -> float:
        """AI가 도면에 그린 마킹 위치를 엔지니어가 그대로 수락한 비율"""
        if total_markings == 0:
            return 0.0
        return accepted_count / total_markings

    def calc_markup_completeness(
        self, markup_results: list[MarkupResult],
    ) -> float:
        if not markup_results:
            return 0.0
        complete = sum(1 for m in markup_results if m.has_revcloud and m.has_mtext)
        return complete / len(markup_results)

    def calc_response_time(self, start_time: float, end_time: float) -> float:
        return end_time - start_time

    def calc_sllm_latency(self, sllm_durations_ms: list[float]) -> float:
        if not sllm_durations_ms:
            return 0.0
        return sum(sllm_durations_ms) / len(sllm_durations_ms)

    def calc_agent_error_rate(self, errors: list[dict], total_steps: int) -> float:
        if total_steps == 0:
            return 0.0
        return len(errors) / total_steps

    def calc_parsing_success_rate(
        self, total_objects: int, parsed_objects: int,
    ) -> float:
        if total_objects == 0:
            return 0.0
        return parsed_objects / total_objects

    def calc_review_consistency(
        self, multiple_run_violations: list[set[str]],
    ) -> float:
        if len(multiple_run_violations) < 2:
            return 1.0
        similarities = []
        for i in range(len(multiple_run_violations)):
            for j in range(i + 1, len(multiple_run_violations)):
                a, b = multiple_run_violations[i], multiple_run_violations[j]
                union = a | b
                similarities.append(len(a & b) / len(union) if union else 1.0)
        return sum(similarities) / len(similarities)

    
    # 통합 평가 + Langfuse 기록
    

    def evaluate(
        self,
        trace_id: str,
        domain: str,
        predictions: list[Violation],
        ground_truths: list[Violation],
        retrieved_chunks: list[RetrievedChunk],
        used_chunk_ids: list[str],
        markup_results: list[MarkupResult],
        start_time: float,
        end_time: float,
        sllm_durations_ms: list[float],
        errors: list[dict],
        total_steps: int,
        total_cad_objects: int = 0,
        parsed_cad_objects: int = 0,
        cost: float = 0.0,
        # 신규 지표용 인자
        is_parsing_ok: bool = True,
        is_marking_ok: bool = True,
        escalation_triggered: bool = False,
        is_valid_escalation: bool = False,
        accepted_count: int = 0,
        total_markings: int = 0,
        model_name: str = "",
        # 일관성 — 동일 도면 다중 실행 시에만 의미. 단일 실행에서는 None/빈 값으로 두면 emit 생략.
        multiple_run_violations: list[set[str]] | None = None,
        # 실시간 정확도 추정 — 신호 있을 때만 emit
        precision_judge: float | None = None,        # LLM-as-Judge 결과 비율 (0~1)
        deterministic_violation_ids: set[str] | None = None,  # |deterministic|
        prediction_ids_for_recall: set[str] | None = None,    # LLM 위반 id 집합
    ) -> EvalResult:

        # 계산
        f1, precision, recall = self.calc_f1(predictions, ground_truths)
        citation_acc = self.calc_citation_accuracy(predictions, ground_truths)
        coord_err = self.calc_coord_error(predictions, ground_truths)
        fpr = self.calc_false_positive_rate(predictions, ground_truths)
        fnr = self.calc_false_negative_rate(predictions, ground_truths)
        rag_rel = self.calc_rag_relevance(retrieved_chunks, used_chunk_ids)
        gt_citations = [gt.citation for gt in ground_truths]
        hit_rate = self.calc_chunk_hit_rate(retrieved_chunks, gt_citations)
        markup_comp = self.calc_markup_completeness(markup_results)
        resp_time = self.calc_response_time(start_time, end_time)
        sllm_lat = self.calc_sllm_latency(sllm_durations_ms)
        err_rate = self.calc_agent_error_rate(errors, total_steps)
        parse_rate = self.calc_parsing_success_rate(total_cad_objects, parsed_cad_objects)
        
        # 신규 지표 계산
        e2e_success = self.calc_e2e_success_rate(is_parsing_ok, is_marking_ok)
        esc_rate = self.calc_escalation_rate(escalation_triggered)
        v_esc_rate = self.calc_valid_escalation_rate(escalation_triggered, is_valid_escalation)
        pos_acc_rate = self.calc_markup_position_acceptance_rate(accepted_count, total_markings)

        # 일관성: 다중 실행 입력이 있을 때만 계산
        consistency: float | None = None
        if multiple_run_violations and len(multiple_run_violations) >= 2:
            consistency = self.calc_review_consistency(multiple_run_violations)

        # 실시간 정확도 추정 (정답셋 없이) — deterministic 커버리지 + LLM-judge precision
        recall_lb: float | None = None
        if deterministic_violation_ids:
            preds = prediction_ids_for_recall or set()
            covered = len(deterministic_violation_ids & preds)
            recall_lb = covered / len(deterministic_violation_ids)

        f1_est: float | None = None
        if precision_judge is not None and recall_lb is not None:
            denom = precision_judge + recall_lb
            f1_est = (2 * precision_judge * recall_lb / denom) if denom > 0 else 0.0

        # Langfuse 기록
        trace = self.langfuse.trace(id=trace_id)

        scores = {
            "f1_score": round(f1, 4),
            "citation_accuracy": round(citation_acc, 4),
            "coord_error_mm": round(coord_err, 2),
            "false_positive_rate": round(fpr, 4),
            "false_negative_rate": round(fnr, 4),
            "rag_retrieval_relevance": round(rag_rel, 4),
            "chunk_hit_rate": round(hit_rate, 4),
            "markup_completeness": round(markup_comp, 4),
            "markup_position_acceptance_rate": round(pos_acc_rate, 4),
            "e2e_success_rate": e2e_success,
            "escalation_rate": esc_rate,
            "response_time_sec": round(resp_time, 2),
            "sllm_latency_ms": round(sllm_lat, 2),
            "agent_error_rate": round(err_rate, 4),
            "parsing_success_rate": round(parse_rate, 4),
        }

        # 입력 신호가 있을 때만 추가 — 단일 호출 dashboard 노이즈 방지
        if v_esc_rate is not None:
            scores["valid_escalation_rate"] = v_esc_rate
        if consistency is not None:
            scores["review_consistency"] = round(consistency, 4)
        if cost > 0:
            scores["cost_per_review"] = round(cost, 4)
        if precision_judge is not None:
            scores["precision_judge"] = round(precision_judge, 4)
        if recall_lb is not None:
            scores["recall_lower_bound"] = round(recall_lb, 4)
        if f1_est is not None:
            scores["f1_estimate"] = round(f1_est, 4)

        for name, value in scores.items():
            trace.score(name=name, value=value)

        # Langfuse score 는 float 만 허용 → 모델명·도메인은 trace metadata 로 기록.
        meta: dict[str, str] = {}
        if model_name:
            meta["model_name"] = model_name
        if domain:
            meta["domain"] = domain
        if meta:
            trace.update(metadata=meta)

        self.langfuse.flush()

        return EvalResult(
            f1_score=f1, precision=precision, recall=recall,
            citation_accuracy=citation_acc, coord_error_mm=coord_err,
            false_positive_rate=fpr, false_negative_rate=fnr,
            rag_retrieval_relevance=rag_rel, chunk_hit_rate=hit_rate,
            markup_completeness=markup_comp,
            markup_position_acceptance_rate=pos_acc_rate,
            e2e_success_rate=e2e_success,
            escalation_rate=esc_rate, valid_escalation_rate=v_esc_rate or 0.0,
            response_time_sec=resp_time, sllm_latency_ms=sllm_lat,
            agent_error_rate=err_rate, parsing_success_rate=parse_rate,
            review_consistency=consistency or 0.0,
            model_name=model_name, cost_per_review=cost,
            precision_judge=precision_judge or 0.0,
            recall_lower_bound=recall_lb or 0.0,
            f1_estimate=f1_est or 0.0,
        )