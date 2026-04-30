"""
CAD Agent — KPI 스코어링 모듈
15개 커스텀 KPI를 계산하고 Langfuse에 기록.

사용법:
    from backend.services.evaluation.kpi_scorer import CADAgentScorer

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

        # 정당한 에스컬레이션 비율은 발생 시에만 기록
        if v_esc_rate is not None:
            scores["valid_escalation_rate"] = v_esc_rate

        for name, value in scores.items():
            trace.score(name=name, value=value)

        if model_name:
            # Langfuse score는 float만 허용 → 모델명은 trace metadata에 기록
            trace.update(metadata={"model_name": model_name})
        if cost > 0:
            trace.score(name="cost_per_review", value=round(cost, 4))

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
            model_name=model_name, cost_per_review=cost,
        )