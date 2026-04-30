"""
WebSocket 'ui' 그룹(React 팔레트)으로 파이프라인 진행을 전달합니다.
진행 문구·누적시간은 CAD-SLLM AGENT 대화창에만 표시(명령줄/플러그인 미사용).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from backend.core.socket_manager import manager

logger = logging.getLogger(__name__)

# LangGraph 노드명 → UI/명령줄용 한글 (미등록 시 "처리: {node}")
NODE_LABEL_KO: dict[str, str] = {
    "domain_node": "도메인 검토",
    "memory_summary_node": "대화 메모리 요약",
    "mapping": "도면·규정 매핑",
    "retrieval": "지식/시방 검색",
    "interpretation": "질의 해석",
    "evaluation": "요구사항 평가",
    "planning": "대응 계획",
    "approval": "승인/검토",
    "query": "Q&A",
    "__start__": "에이전트 파이프라인 시작",
    "cad_extraction_merged": "엔티티 청크 병합",
    "cad_normalize": "도면 JSON 정규화",
    "cad_s3_upload": "S3 저장 및 Redis 경로 등록",
    "cad_data_ready": "UI/CAD에 도면 준비 완료",
    "cad_validated": "도면 JSON 수신·검증",
    "cad_chunk_staged": "엔티티 청크 수신",
    "cad_receive": "도면 데이터 수신",
    "pipe_review_enter": "배관 검토 노드 진입",
    "pipe_intent": "배관 의도 분석",
    "pipe_layout_split": "도면 레이어·역할 분리",
    "pipe_mapping": "배관 이름·위치 매핑",
    "pipe_tool_select": "배관 도구 선택",
    "pipe_tool_run": "배관 서브 에이전트 실행",
    "pipe_result_format": "배관 결과 정리",
    "pipe_review_parse": "도면 요소 파싱",
    "pipe_review_rag_topology": "Topology/Geometry/RAG 병렬 처리",
    "pipe_review_compliance": "시방·규정 검증",
    "pipe_review_deterministic": "확정 규칙 검사",
    "pipe_review_qa": "도면 품질검사",
    "pipe_review_report": "검토 리포트 생성",
    "pipe_review_revision": "수정안 계산",
    "__done__": "에이전트 파이프라인 완료",
}


def label_for_stage(stage: str) -> str:
    return NODE_LABEL_KO.get(stage, f"처리: {stage}")


async def emit_pipeline_step(
    *,
    session_id: str | None,
    stage: str,
    message: str | None,
    t0_monotonic: float,
    wall_start_ts: float,
    last_t: float,
) -> float:
    """
    한 단계 완료 시점을 전송. 반환값은 다음 last_t(모노토닉).
    t0_monotonic / last_t: time.monotonic() — 경과(누적) 측정
    wall_start_ts: time.time() — 파이프라인 착수 시각(표시용 ISO)
    """
    now = time.monotonic()
    try:
        started_iso = datetime.fromtimestamp(wall_start_ts, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        payload = {
            "session_id": (session_id or "").strip(),
            "stage": stage,
            "message": (message or "").strip() or label_for_stage(stage),
            "pipeline_started_at": started_iso,
            "step_elapsed_ms": max(0, int((now - last_t) * 1000)),
            "total_elapsed_ms": max(0, int((now - t0_monotonic) * 1000)),
        }
        body = {"action": "PIPELINE_PROGRESS", "payload": payload}
        await manager.send_to_group(body, "ui")
    except Exception as e:
        logger.debug("[cad_progress] send skipped: %s", e)
    return now
