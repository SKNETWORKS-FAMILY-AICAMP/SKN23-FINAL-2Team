"""
평가 데이터 구조 정의 — 에이전트 코드와 kpi_score 양쪽에서 import.
순환 참조 방지를 위해 별도 파일로 분리.

대시보드 KPI 스펙(2026-05-09):
  ① 핵심 성능/정확도 (15개)
  ② 운영 안정성/실무 효용성 (4개)
  ③ Archived: embedding_similarity_avg, violation_count
"""

from dataclasses import dataclass


@dataclass
class Violation:
    """위반 항목 1건"""
    id: str
    description: str
    citation: str       # 근거 법규 조항 (예: "NFSC 103 2.4.1")
    x: float            # 위반 위치 X좌표 (mm)
    y: float            # 위반 위치 Y좌표 (mm)


@dataclass
class RetrievedChunk:
    """RAG로 검색된 청크 1건"""
    chunk_id: str
    content: str
    similarity: float    # 코사인 유사도 (0~1)
    source: str          # 예: "NFSC_103"
    article: str         # 예: "2.4.1"


@dataclass
class MarkupResult:
    """CAD 마킹 결과 1건 — RES-03(revcloud) / RES-04(mtext) 생성 여부"""
    violation_id: str
    has_revcloud: bool
    has_mtext: bool


@dataclass
class EvalResult:
    """전체 평가 결과 — 대시보드 19개 KPI 와 1:1 매핑."""

    # ── ① 핵심 성능 / 정확도 ────────────────────────────────────────
    f1_score: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    citation_accuracy: float = 0.0
    false_negative_rate: float = 0.0
    false_positive_rate: float = 0.0
    coord_error_mm: float = 0.0
    parsing_success_rate: float = 0.0
    chunk_hit_rate: float = 0.0
    rag_retrieval_relevance: float = 0.0
    markup_completeness: float = 0.0
    response_time_sec: float = 0.0
    sllm_latency_ms: float = 0.0
    agent_error_rate: float = 0.0
    review_consistency: float = 0.0       # 동일 도면 다중 실행 시에만 의미
    cost_per_review: float = 0.0
    model_name: str = ""                   # GPT vs sLLM 비교용 — Langfuse 에는 metadata 로 기록

    # ── ② 운영 안정성 / 실무 효용성 (신규) ─────────────────────────
    e2e_success_rate: float = 0.0
    escalation_rate: float = 0.0
    valid_escalation_rate: float = 0.0
    markup_position_acceptance_rate: float = 0.0

    # ── ③ 실시간 정확도 추정 (정답셋 없이 산출) ────────────────────
    # precision_judge       : 별도 strong LLM 이 emit 위반을 검증 → 인정 비율
    # recall_lower_bound    : (LLM 위반 ∩ deterministic) / |deterministic|. 진짜 recall 의 하한.
    # f1_estimate           : 위 둘로 산출한 f1 추정치. "진짜 f1" 과는 구분.
    precision_judge: float = 0.0
    recall_lower_bound: float = 0.0
    f1_estimate: float = 0.0
