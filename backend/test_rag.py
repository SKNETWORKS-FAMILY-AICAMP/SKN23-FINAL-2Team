"""
File    : backend/test_rag.py
Author  : 김민정
WBS     : EVAL-03
Create  : 2026-04-12
Description : 공식 CADAgentScorer를 활용한 RAG 성능 평가 및 Langfuse 기록 스크립트

Modification History :
    - 2026-04-12 (김민정) : kpi_scorer.py 연동 및 Langfuse 기록 로직 반영
"""

import json
import time
import uuid
from evaluation.kpi_scorer import CADAgentScorer
from evaluation.schemas import Violation, RetrievedChunk, MarkupResult

# 1. 공식 스코어러 준비
# 이 객체가 내부적으로 Langfuse와 연결되어 점수를 보냅니다.
scorer = CADAgentScorer()

# 2. 정답지(JSON) 불러오기
try:
    with open('rag_test_dataset.json', 'r', encoding='utf-8') as f:
        test_data = json.load(f)
except FileNotFoundError:
    print("에러: rag_test_dataset.json 파일을 찾을 수 없습니다.")
    exit()

print(f"[EVAL-03] 공식 스코어러를 통한 {len(test_data)}문항 평가를 시작합니다...")

# 전체 평가를 하나로 묶어줄 고유 ID (Langfuse 트레이스용)
trace_id = str(uuid.uuid4())
start_time = time.time()

# 결과 저장을 위한 리스트들
all_predictions = []
all_ground_truths = []
all_retrieved_chunks = []

# 3. 테스트 루프 실행
for idx, item in enumerate(test_data):
    # 정답지 데이터 (Ground Truth)
    gt_violation = Violation(
        id=f"gt_{idx}",
        description="정답 내용",
        citation=item["ground_truth_rule"], # 예: "KEC 142.6"
        x=0.0, y=0.0 # RAG 전용 테스트이므로 좌표는 임시값
    )
    all_ground_truths.append(gt_violation)

    # ---------------------------------------------------------
    # 가짜 AI의 검색 결과 (나중에 실전 코드로 교체될 부분)
    # ---------------------------------------------------------
    # AI가 찾아온 법규 청크 (RetrievedChunk)
    mock_chunk = RetrievedChunk(
        chunk_id=f"chunk_{idx}",
        content="법규 본문 내용...",
        similarity=0.95,
        source=item["ground_truth_rule"].split()[0], # "KEC"
        article=" ".join(item["ground_truth_rule"].split()[1:]) # "142.6"
    )
    all_retrieved_chunks.append(mock_chunk)

    # AI가 내린 최종 위반 판단 결과 (Violation)
    pred_violation = Violation(
        id=f"pred_{idx}",
        description="AI의 판단",
        citation=item["ground_truth_rule"], # 정답을 맞춘 것으로 가정
        x=0.0, y=0.0
    )
    all_predictions.append(pred_violation)

# 4. 공식 스코어러를 통한 일괄 평가 및 Langfuse 전송
# kpi_scorer.py의 evaluate 함수가 모든 KPI를 계산하고 서버에 쏩니다.
eval_result = scorer.evaluate(
    trace_id=trace_id,
    domain="general",
    predictions=all_predictions,
    ground_truths=all_ground_truths,
    retrieved_chunks=all_retrieved_chunks,
    used_chunk_ids=[c.chunk_id for c in all_retrieved_chunks],
    markup_results=[], # 마킹 테스트는 생략
    start_time=start_time,
    end_time=time.time(),
    sllm_durations_ms=[150.0] * len(test_data), # 가짜 지연시간
    errors=[],
    total_steps=len(test_data)
)

# 5. 최종 결과 출력
print(f"[EVAL-03] 공식 평가 완료 (Trace ID: {trace_id})")
print("============================")
print(f"Hit@K (조항 적중률) : {eval_result.chunk_hit_rate * 100:.1f}%")
print(f"F1 Score (위반 탐지) : {eval_result.f1_score:.4f}")
print(f"평균 검색 유사도      : {eval_result.embedding_similarity_avg:.4f}")
print(f"총 응답 시간         : {eval_result.response_time_sec:.2f}s")
print("============================")
print("Langfuse 대시보드에 기록되었습니다.")