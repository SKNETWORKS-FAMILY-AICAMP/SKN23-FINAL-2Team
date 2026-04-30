"""
File    : backend/test_extraction.py
Author  : 김민정
WBS     : EVAL-02
Description : CAD에서 추출된 JSON과 정답지를 비교하여 F1 Score를 계산하고 Langfuse에 기록
"""

import json
import time
import uuid
from evaluation.kpi_scorer import CADAgentScorer
from evaluation.schemas import Violation

# 1. 공식 스코어러 준비
scorer = CADAgentScorer()

def load_json_data(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"에러: {file_path} 파일을 찾을 수 없습니다.")
        return None

# 2. 데이터 로드
# CAD에서 demoextract로 뽑은 결과물
pred_data = load_json_data('cad_extract_test.json')
# 미리 작성한 정답지
gt_data = load_json_data('ground_truth_extraction.json')

if pred_data is None or gt_data is None:
    exit()

print(f"[EVAL-02] 도면 추출 성능 평가를 시작합니다...")

trace_id = str(uuid.uuid4())
start_time = time.time()

# 3. 데이터를 Violation 객체 리스트로 변환
# kpi_scorer가 계산할 수 있도록 schemas.py 형식을 맞춥니다.
predictions = [Violation(**item) for item in pred_data]
ground_truths = [Violation(**item) for item in gt_data]

# 4. 공식 스코어러 실행
# 이 함수가 내부적으로 calc_f1을 호출하여 F1, Precision, Recall을 계산합니다.
eval_result = scorer.evaluate(
    trace_id=trace_id,
    domain="extraction_test",
    predictions=predictions,
    ground_truths=ground_truths,
    retrieved_chunks=[],    # 추출 테스트이므로 비움
    used_chunk_ids=[],
    markup_results=[],
    start_time=start_time,
    end_time=time.time(),
    sllm_durations_ms=[],
    errors=[],
    total_steps=1,
    total_cad_objects=len(ground_truths), # 전체 정답 객체 수
    parsed_cad_objects=len(predictions)    # AI가 인식한 객체 수
)

# 5. 결과 출력
print("============================")
print(f"[EVAL-02] 추출 평가 완료 (Trace ID: {trace_id})")
print("============================")
print(f"F1 Score (종합 정확도) : {eval_result.f1_score:.4f}")
print(f"Precision (정밀도)    : {eval_result.precision:.4f}")
print(f"Recall (재현율)       : {eval_result.recall:.4f}")
print(f"좌표 오차 평균        : {eval_result.coord_error_mm:.2f} mm")
print("============================")
print("상세 지표가 Langfuse 대시보드에 전송되었습니다.")
print("============================")