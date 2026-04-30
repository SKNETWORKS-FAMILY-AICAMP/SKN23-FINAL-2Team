"""
평가 데이터 구조 정의
- 에이전트 코드와 kpi_scorer 양쪽에서 import
- 순환 참조 방지를 위해 별도 파일로 분리
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
    similarity: float   # 코사인 유사도 (0~1)
    source: str          # 예: "NFSC_103"
    article: str         # 예: "2.4.1"


@dataclass
class MarkupResult:
    """CAD 마킹 결과 1건"""
    violation_id: str
    has_revcloud: bool
    has_mtext: bool


@dataclass
class EvalResult:
    """전체 평가 결과"""
    f1_score: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    citation_accuracy: float = 0.0
    coord_error_mm: float = 0.0
    false_positive_rate: float = 0.0
    false_negative_rate: float = 0.0
    
    # RAG 관련
    rag_retrieval_relevance: float = 0.0
    chunk_hit_rate: float = 0.0
    
    # 작업 효율 및 안정성
    markup_completeness: float = 0.0
    markup_position_acceptance_rate: float = 0.0  # 신규: 마킹 위치 수락률
    e2e_success_rate: float = 0.0           # 신규: 통합 성공률
    parsing_success_rate: float = 0.0
    
    # 판단 보류 관련 (신규)
    escalation_rate: float = 0.0           # AI의 사람 검토 요청 비율
    valid_escalation_rate: float = 0.0     # 정당한 검토 요청 비율
    
    # 성능 및 비용
    response_time_sec: float = 0.0
    sllm_latency_ms: float = 0.0
    agent_error_rate: float = 0.0
    review_consistency: float = 0.0
    model_name: str = ""                    # 복구: 모델명 (GPT vs sLLM 비교용)
    cost_per_review: float = 0.0