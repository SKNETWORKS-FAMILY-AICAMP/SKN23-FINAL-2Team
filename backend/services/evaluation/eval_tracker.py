"""
File    : backend/services/evaluation/eval_tracker.py
Author  : 김민정
Create  : 2026-04-17
Description : 에이전트 분석 결과에 대한 KPI 지표 측정 및 Langfuse 기록 전담 서비스

Modification History :
    - 2026-04-17 (김민정) : 초기 구조 생성 및 KPI 스코어러 연동
"""

import time
import logging
from typing import Any

from backend.services.evaluation.kpi_score import CADAgentScorer
from backend.services.evaluation.schemas import Violation, MarkupResult

# 공용 스코어러 인스턴스
_scorer = CADAgentScorer()

async def track_cad_review_metrics(
    session_id: str,
    domain: str,
    agent_result: dict[str, Any],
    start_time: float,
    cad_payload: dict[str, Any] = None,
    drawing_data: dict[str, Any] = None,
):
    """
    에이전트 분석 결과를 지표 규격으로 변환하고 Langfuse에 기록합니다.
    """
    try:
        end_time = time.time()
        
        # 1. 에이전트 결과에서 위반 사항 추출 및 변환
        review_result = agent_result.get("review_result") or {}
        raw_violations = review_result.get("violations") or []
        
        predictions = [
            Violation(
                id=str(v.get("object_id") or v.get("equipment_id") or ""),
                description=v.get("reason") or v.get("suggestion") or "",
                citation=v.get("legal_reference") or v.get("reference_rule") or "",
                x=float(v.get("x") or 0.0),
                y=float(v.get("y") or 0.0)
            )
            for v in raw_violations
        ]
        
        # 2. 마킹 결과 추출 (Pending Fixes가 생성되었다면 마킹 준비 완료로 간주)
        pending_fixes = agent_result.get("pending_fixes") or []
        markup_results = [
            MarkupResult(
                violation_id=str(f.get("fix_id") or ""),
                has_revcloud=True,  # 분석 단계에서는 생성 준비 완료 상태
                has_mtext=True
            )
            for f in pending_fixes
        ]
        
        # 3. 객체 개수 파악
        total_cad_objects = 0
        if cad_payload and "entities" in cad_payload:
            # 캐드 원본 엔티티 개수
            total_cad_objects = len(cad_payload["entities"])
        elif drawing_data and "entities" in drawing_data:
            # 원본이 없을 경우 정규화된 데이터 기준
            total_cad_objects = len(drawing_data["entities"])
        
        parsed_cad_objects = 0
        if drawing_data and "entities" in drawing_data:
            parsed_cad_objects = len(drawing_data["entities"])
            
        # 4. 스코어러 호출 (Langfuse 기록 포함)
        _scorer.evaluate(
            trace_id=session_id,
            domain=domain,
            predictions=predictions,
            ground_truths=[], # 실시간 분석에서는 정답셋이 없으므로 빈 리스트
            retrieved_chunks=[], 
            used_chunk_ids=[],
            markup_results=markup_results,
            start_time=start_time,
            end_time=end_time,
            sllm_durations_ms=[], 
            errors=[],
            total_steps=1,
            total_cad_objects=total_cad_objects,
            parsed_cad_objects=parsed_cad_objects,
            model_name="qwen3.5-27b-qlora" 
        )
        
        logging.info(f"[EvalTracker] KPI Scoring completed for session_id={session_id}")
        
    except Exception as e:
        logging.error(f"[EvalTracker] Error during KPI scoring: {str(e)}")
