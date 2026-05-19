"""
File    : backend/api/routers/agent_api.py
Author  : 양창일, 김지우
Description : 도메인별 agent 실행 요청을 받고 LangGraph review workflow 를 호출하는 API 라우터.
              AI의 proposed_action을 CAD 플러그인용 auto_fix 명령으로 변환하는 로직 포함.
"""

import time
import json
import logging
import re
from uuid import UUID
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks

from backend.api.deps.license_auth import get_authenticated_org_id, require_same_org
from backend.services import document_service
from backend.api.schemas.agent import (
    AgentExecuteRequest,
    AgentExecuteResponse,
    AgentFixesConfirmRequest,
    AgentFixesConfirmResponse,
    AgentLawContextRequest,
    AgentLawContextResponse,
    PendingFixResponse,
)
from backend.core.config import settings
from backend.core.database import get_db, SessionLocal
from backend.services.arch_pipe_layer_split import entity_layer_role
from backend.core.socket_manager import manager
from backend.services.agent_service import AgentService
from backend.services.cad_service import cad_service
from backend.services.payload_service import (
    CONTEXT_MODE_FULL_WITH_FOCUS,
    normalize_agent_payload,
    normalize_retrieved_laws,
)
from backend.services.vector_service import hybrid_search_permanent_chunks_with_rerank
from backend.services.cad_modification_tiers import (
    infer_modification_tier,
    merge_autofix_with_tier,
)
from backend.services.review_cancel import (
    clear_review_cancel,
    is_review_cancelled,
    mark_review_cancelled,
)
from backend.services.state_service import (
    apply_agent_execution_result,
    append_chat_message,
    append_turn_summary,
    apply_user_request,
    build_initial_state,
    confirm_pending_fixes,
    create_chat_session,
    load_agent_state,
    merge_agent_payload_with_state,
    push_recent_chat_history,
    save_agent_state,
    tool_calls_from_workflow_steps,
)
from backend.services.evaluation import eval_tracker
from backend.services.llm_service import generate_answer
from backend.services.drawing_state_service import DrawingStateService, ConflictError
from backend.core.redis_client import get_redis_client
from backend.utils.s3_manager import S3Manager as _S3Manager

router = APIRouter()
agent_service = AgentService()

REVIEW_BLOCKING_CHANGE_MODES = {
    "INCREMENTAL_PENDING",
    "FULL_RESYNC_PENDING",
    "FULL_RESYNC_RUNNING",
}
REVIEW_BLOCKING_STATUSES = {
    "INCREMENTAL_PENDING",
    "FULL_RESYNC_PENDING",
    "FULL_RESYNC_RUNNING",
}


async def _load_drawing_for_review(
    drawing_id: str, session_id: str
) -> tuple[dict | None, bool]:
    """
    drawing_id 기반으로 최신 도면 로드. Returns (data, is_blocked).
    is_blocked=True 이면 FULL_RESYNC 진행 중 → 검토 차단.
    """
    try:
        _s3 = _S3Manager()
        _dss = DrawingStateService(redis=get_redis_client(), s3=_s3)
        state = await _dss.get_state(drawing_id)

        print(
            f"[S3_LOAD] drawing_id={drawing_id} status={state.status} change_mode={state.change_mode} "
            f"snapshot_id={state.current_snapshot_id} delta_count={state.delta_count} delta_size={state.delta_size}"
        )

        if (
            state.change_mode in REVIEW_BLOCKING_CHANGE_MODES
            or state.status in REVIEW_BLOCKING_STATUSES
        ):
            print(f"[S3_LOAD] drawing_id={drawing_id} ⛔ BLOCKED — change_mode={state.change_mode}")
            return None, True

        if state.dirty_count > 0:
            can_use_committed_deltas = (
                state.change_mode == "INCREMENTAL_READY"
                and state.delta_count > 0
                and bool(state.current_snapshot_path)
            )
            if can_use_committed_deltas:
                print(
                    f"[S3_LOAD] drawing_id={drawing_id} ⚠ stale dirty_count={state.dirty_count} "
                    "but INCREMENTAL_READY; using committed snapshot+deltas"
                )
            else:
                print(f"[S3_LOAD] drawing_id={drawing_id} BLOCKED dirty_count={state.dirty_count}")
                return None, True

        if state.delta_count > 0:
            try:
                print(f"[S3_LOAD] drawing_id={drawing_id} delta_count={state.delta_count} → load_merged_drawing 시작")
                data = await _dss.load_merged_drawing(drawing_id)
                entity_count = len(data.get("entities") or data.get("elements") or [])
                print(f"[S3_LOAD] drawing_id={drawing_id} ✅ snapshot+delta merge 완료 entities={entity_count}")
                return data, False
            except ConflictError:
                print(f"[S3_LOAD] drawing_id={drawing_id} ⚠ ConflictError — FULL_RESYNC_PENDING 전환됨")
                return None, True

        if state.current_snapshot_path:
            print(f"[S3_LOAD] drawing_id={drawing_id} delta 없음 → snapshot 단독 로드 path={state.current_snapshot_path}")
            data = await _s3.load_json(state.current_snapshot_path)
            entity_count = len(data.get("entities") or data.get("elements") or [])
            print(f"[S3_LOAD] drawing_id={drawing_id} ✅ snapshot 로드 완료 entities={entity_count}")
            return data, False

        print(f"[S3_LOAD] drawing_id={drawing_id} ❌ snapshot_path 없음 → 로드 실패")

    except Exception as _e:
        print(f"[S3_LOAD] drawing_id={drawing_id} session_id={session_id} ❌ 예외={_e}")

    return None, False


async def cancel_agent_review(session_id: str) -> None:
    sid = (session_id or "").strip()
    if not sid:
        return
    mark_review_cancelled(sid)
    await manager.send_to_group(
        {
            "action": "REVIEW_CANCELLED",
            "session_id": sid,
            "message": "사용자에 의해 분석이 중단되었습니다.",
        },
        "ui",
    )


def _is_review_cancelled(session_id: str) -> bool:
    return is_review_cancelled(session_id)


def _clear_review_cancel(session_id: str) -> None:
    clear_review_cancel(session_id)

# DomainClassifier 모듈 레벨 싱글톤 (매 요청마다 joblib.load 방지)
try:
    from backend.services.agents.common.domain_classifier.classifier import DomainClassifier as _DC
    _domain_classifier: "_DC | None" = _DC()
    if not _domain_classifier.is_loaded:
        logging.info("[agent_api] DomainClassifier ML 미로드 — RuleClassifier 전용 조기 분류 사용")
except Exception as _dc_err:
    _domain_classifier = None
    logging.info("[agent_api] DomainClassifier 비활성화: %s", _dc_err)

# 도메인 코드 → 한글 (DOMAIN_MISMATCH 메시지·로그용)
_DOMAIN_KR = {"arch": "건축", "elec": "전기", "fire": "소방", "pipe": "배관"}

class AgentReviewRequest(BaseModel):
    session_id: str
    cad_cache_id: Optional[str] = None
    drawing_id: Optional[str] = None    # 신규 추가
    file_fingerprint: Optional[str] = None  # 파일 변경 감지용 (선택)
    domain: str
    active_object_ids: List[str]
    spec_document_ids: Optional[List[str]] = None
    temp_spec_ids: Optional[List[str]] = None
    project_id: Optional[UUID] = None
    review_mode: str = "KEC_ONLY"
    user_prompt: Optional[str] = None
    org_id: Optional[str] = None
    machine_id: Optional[str] = None
    skip_classification: bool = False

class AgentReviewResponse(BaseModel):
    status: str
    job_id: str
    message: str

class ChecklistRequest(BaseModel):
    spec_document_ids: List[str]

@router.post("/agent/checklist/generate")
async def generate_checklist(body: ChecklistRequest, db: AsyncSession = Depends(get_db)):
    try:
        retrieved_texts = []
        if body.spec_document_ids:
            for doc_id in body.spec_document_ids:
                chunks = await hybrid_search_permanent_chunks_with_rerank(
                    db=db,
                    query="도면 검토 시 확인해야 할 필수 체크리스트 항목",
                    document_id=doc_id,
                    domain="common",
                    rrf_limit=10,
                    final_limit=3
                )
                retrieved_texts.extend([chunk.content for chunk in chunks])

        spec_context = "\n".join(retrieved_texts) if retrieved_texts else "기본 KEC/NFSC 법규"

        system_prompt = """
        제공된 시방서 및 규정 데이터를 바탕으로, 설계자가 도면 검토 시 반드시 확인해야 할 
        'AI 자동 생성 체크리스트'를 JSON 형태로 5개 이내로 생성하십시오.

        [출력 스키마]
        {
            "checklist": [
                {
                    "id": "chk_01",
                    "category": "이격거리 | 허용전류 | 배치 | 기타",
                    "question": "체크리스트 질문 (예: 변압기 간 이격거리가 600mm 이상 확보되었습니까?)",
                    "reference": "관련 근거 규정"
                }
            ]
        }
        """

        response_data = await generate_answer(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"[참고 자료]:\n{spec_context}"}
            ],
            response_format={"type": "json_object"}
        )

        return response_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/agent/start", response_model=AgentReviewResponse)
async def start_agent_review(
    body: AgentReviewRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    try:
        body.session_id = (body.session_id or "").strip()
        if not body.session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        try:
            UUID(body.session_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="session_id must be a valid UUID")

        _clear_review_cancel(body.session_id)

        # file_fingerprint guard: drawing_id + fingerprint 모두 있으면 Redis 저장값과 비교
        if body.drawing_id and body.file_fingerprint:
            _stored_fp = await get_redis_client().get(
                f"drawing:{body.drawing_id}:fileFingerprint"
            )
            if _stored_fp and _stored_fp != body.file_fingerprint:
                logging.warning(
                    "[agent/start] file_fingerprint mismatch drawing_id=%s — "
                    "도면 파일이 변경됐을 수 있습니다. 최신 /cad/analyze 결과를 로드하세요.",
                    body.drawing_id,
                )

        try:
            state = await load_agent_state(db, body.session_id)
        except ValueError:
            state = build_initial_state(
                session_id=body.session_id,
                domain_type=body.domain,
                session_title=f"{body.domain} 검토",
                user_request="",
            )
        state["active_object_ids"] = body.active_object_ids

        n_sel = len(body.active_object_ids or [])
        if n_sel > 0:
            _default_review = (
                f"CAD에서 선택한 {n_sel}개 객체에 대해 {body.domain} 시방·규정 위반이 있는지 검토해 주세요."
            )
        else:
            _default_review = (
                f"도면 전체에 대해 {body.domain} 시방·규정 위반을 전수 검토해 주세요."
            )
        u = (body.user_prompt or "").strip()
        query = u or _default_review
        retrieved_specs = []

        if body.review_mode == "HYBRID" and body.spec_document_ids:
            for doc_id in body.spec_document_ids:
                chunks = await hybrid_search_permanent_chunks_with_rerank(
                    db=db,
                    query=query,
                    document_id=doc_id,
                    domain=body.domain,
                    rrf_limit=20,
                    final_limit=5
                )
                retrieved_specs.extend([
                    {"document_id": doc_id, "content": chunk.content, "section_id": chunk.section_id}
                    for chunk in chunks
                ])

        org_id = body.org_id or ""
        if not org_id and body.machine_id:
            from backend.api.routers.cad_interop import _resolve_org_device
            resolved_org, _ = await _resolve_org_device(db, body.machine_id)
            org_id = resolved_org
            logging.info(f"[agent/start] org_id 누락 → machine_id='{body.machine_id}'로 DB 조회 → org_id='{org_id}'")

        if org_id:
            require_same_org(org_id, auth_org_id)
        else:
            org_id = auth_org_id

        temp_spec_ids = [x for x in (body.temp_spec_ids or []) if x]
        project_id = str(body.project_id) if body.project_id else None
        if not temp_spec_ids and project_id and org_id:
            linked_specs = await document_service.get_project_spec_links(db, org_id, project_id)
            temp_spec_ids = [
                str(row.get("temp_document_id") or "")
                for row in linked_specs
                if row.get("temp_document_id")
            ]

        if temp_spec_ids:
            for tid in temp_spec_ids:
                doc_org = await document_service.get_temp_document_org(db, tid)
                if not doc_org:
                    raise HTTPException(status_code=404, detail=f"임시 시방서를 찾을 수 없습니다: {tid}")
                if doc_org != auth_org_id:
                    raise HTTPException(status_code=403, detail="temp_spec_id가 인증된 org와 일치하지 않습니다.")

        spec_guid = temp_spec_ids if temp_spec_ids else None

        payload = {
            "session_id": body.session_id,
            "device_id": body.machine_id or "unknown",
            "active_object_ids": body.active_object_ids,
            "message": query,
            "retrieved_specs": retrieved_specs,
            "review_mode": body.review_mode,
            "org_id": org_id,
            "spec_guid": spec_guid,
            "project_id": project_id,
            "intent_hint": "review",
            "skip_classification": body.skip_classification,
        }

        try:
            await save_agent_state(db, body.session_id, state)
        except Exception:
            pass

        job_id = f"job_{body.session_id}_{int(time.time())}"
        cad_cache_id = body.cad_cache_id or body.session_id

        await manager.send_to_group(
            {"action": "ANALYSIS_STARTED", "session_id": body.session_id},
            "ui",
        )

        # ── 도메인 조기 검사 (LangGraph 시작 전) ─────────────────────────────
        # skip_classification=True 이면 사용자가 이미 확인한 것 → 검사 생략
        if not body.skip_classification:
            try:
                _cad_data = None
                _early_drawing_id = (body.drawing_id or "").strip()
                if _early_drawing_id:
                    _early_data, _early_blocked = await _load_drawing_for_review(
                        _early_drawing_id, cad_cache_id or body.session_id
                    )
                    if not _early_blocked and _early_data:
                        _cad_data = _early_data
                if not _cad_data:
                    _cad_data = await cad_service.get_drawing_data(cad_cache_id)
                _entities = (
                    (_cad_data.get("entities") or _cad_data.get("elements") or [])
                    if _cad_data else []
                )
                if _entities:
                    if _domain_classifier is not None:
                        _proba = _domain_classifier.predict_proba(_cad_data)
                        _pred  = max(_proba, key=_proba.get)
                        _pred_p = _proba.get(_pred, 0.0)
                        _user_p = _proba.get(body.domain, 0.0)

                        if _pred != body.domain and (_pred_p - _user_p) > 0.50:
                            _pred_kr = _DOMAIN_KR.get(_pred, _pred)
                            _user_kr = _DOMAIN_KR.get(body.domain, body.domain)
                            await manager.send_to_group(
                                {
                                    "action": "DOMAIN_MISMATCH",
                                    "session_id": body.session_id,
                                    "payload": {
                                        "predicted_domain": _pred,
                                        "predicted_domain_kr": _pred_kr,
                                        "original_domain": body.domain,
                                        "original_domain_kr": _user_kr,
                                        "probabilities": _proba,
                                        "message": (
                                            f"{_pred_kr} 도면으로 예측됩니다. "
                                            "계속 진행하시겠습니까?"
                                        ),
                                    },
                                },
                                "ui",
                            )
                            logging.info(
                                "[agent/start] 도메인 불일치 조기 감지 — "
                                "user=%s(%.2f) pred=%s(%.2f) diff=%.2f → LangGraph 미시작",
                                body.domain, _user_p, _pred, _pred_p, _pred_p - _user_p,
                            )
                            return AgentReviewResponse(
                                status="DOMAIN_MISMATCH",
                                job_id="",
                                message=f"{_pred_kr} 도면으로 예측됩니다.",
                            )
            except Exception as _clf_err:
                logging.warning("[agent/start] 조기 분류 실패 (무시): %s", _clf_err)
        # ─────────────────────────────────────────────────────────────────────

        background_tasks.add_task(
            run_agent_background_task,
            session_id=body.session_id,
            domain=body.domain,
            payload=payload,
            job_id=job_id,
            cad_cache_id=cad_cache_id,
            skip_classification=body.skip_classification,
            drawing_id=body.drawing_id or "",
        )

        return AgentReviewResponse(
            status="SUCCESS",
            job_id=job_id,
            message="에이전트 검토가 백그라운드에서 시작되었습니다."
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logging.exception("[/agent/start] 예기치 않은 오류")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/agent/cancel/{job_id}")
async def cancel_agent_review_by_job(job_id: str):
    parts = (job_id or "").split("_")
    if len(parts) < 3 or parts[0] != "job":
        raise HTTPException(status_code=400, detail="Invalid job_id")
    session_id = "_".join(parts[1:-1])
    await cancel_agent_review(session_id)
    return {"status": "cancelled", "job_id": job_id, "session_id": session_id}


async def _send_progress(session_id: str, step: str, message: str):
    """UI에 실시간 진행 메시지 브로드캐스트"""
    await manager.send_to_group(
        {"action": "AGENT_PROGRESS", "session_id": session_id, "step": step, "message": message},
        "ui",
    )


async def run_agent_background_task(
    session_id: str,
    domain: str,
    payload: dict,
    job_id: str,
    cad_cache_id: str | None = None,
    skip_classification: bool = False,
    drawing_id: str = "",
):
    async with SessionLocal() as db:
        try:
            if _is_review_cancelled(session_id):
                return
            await manager.send_to_group(
                {"action": "ANALYSIS_STARTED", "session_id": session_id, "message": "분석 준비 중..."},
                "ui",
            )
            try:
                state = await load_agent_state(db, session_id)
            except ValueError:
                logging.info("[run_agent] session_id not in DB (%s) — 자동 생성", session_id)
                await create_chat_session(
                    db, session_id, domain, session_title=f"{domain} 검토"
                )
                state = build_initial_state(
                    session_id=session_id,
                    domain_type=domain,
                    session_title=f"{domain} 검토",
                    user_request="",
                )
            _persist = True

            normalized_payload = normalize_agent_payload(payload)
            normalized_payload = merge_agent_payload_with_state(state, normalized_payload)

            org_id = payload.get("org_id") or ""
            device_id = payload.get("device_id") or "unknown"
            
            spec_guid = payload.get("spec_guid") or ""
            meta_update = {"org_id": org_id, "device_id": device_id}
            if spec_guid:
                meta_update["spec_guid"] = spec_guid

            if org_id:
                state["runtime_meta"] = {**(state.get("runtime_meta") or {}), **meta_update}
            else:
                print(f"[Agent DEBUG] ⚠ org_id 비어있음 — 프론트엔드 localStorage(skn23_org_id) 확인 필요")

            user_request = str(normalized_payload.get("message") or "")
            active_object_ids = list(normalized_payload.get("active_object_ids") or [])

            state = apply_user_request(state, user_request, active_object_ids)
            
            runtime_drawing_data = normalized_payload.get("drawing_data") or {}
            cache_key = cad_cache_id or session_id
            _drawing_data_source = "payload" if runtime_drawing_data else "none"

            drawing_id_req = (drawing_id or "").strip()
            if drawing_id_req:
                _loaded, _blocked = await _load_drawing_for_review(drawing_id_req, cache_key)
                if _blocked:
                    await manager.send_to_group(
                        {
                            "action": "DRAWING_RESYNC",
                            "session_id": session_id,
                            "message": "도면 변경사항 저장 또는 동기화가 진행 중입니다. 완료 후 다시 시도해 주세요.",
                        },
                        "ui",
                    )
                    return
                if _loaded:
                    runtime_drawing_data = _loaded  # payload drawing_data를 최신본으로 override
                    _drawing_data_source = "s3_snapshot_or_merge"

            if not runtime_drawing_data:
                runtime_drawing_data = await cad_service.get_drawing_data(cache_key)
                if runtime_drawing_data:
                    _drawing_data_source = "s3_legacy_cache"

            _entity_count = len(runtime_drawing_data.get("entities") or runtime_drawing_data.get("elements") or [])
            print(
                f"[DRAWING_LOAD] session={session_id} drawing_id={drawing_id_req or '(none)'} "
                f"source={_drawing_data_source} entities={_entity_count}"
            )
            if _is_review_cancelled(session_id):
                return
                
            if not (runtime_drawing_data.get("focus_extraction") or {}):
                red_focus = await cad_service.get_focus_drawing(str(cache_key))
                if red_focus:
                    runtime_drawing_data = dict(runtime_drawing_data)
                    runtime_drawing_data["focus_extraction"] = red_focus
                    if not runtime_drawing_data.get("context_mode"):
                        runtime_drawing_data["context_mode"] = CONTEXT_MODE_FULL_WITH_FOCUS
            
            runtime_retrieved_laws = normalized_payload.get("retrieved_laws") or []
            state["drawing_data"] = runtime_drawing_data
            state["retrieved_laws"] = runtime_retrieved_laws
            state["retrieved_specs"] = payload.get("retrieved_specs", [])

            entities = (runtime_drawing_data.get("entities") or runtime_drawing_data.get("elements") or [])
            if entities:
                await _send_progress(session_id, "data_loaded", f"도면 데이터 확인 중... ({len(entities)}개 엔티티)")
            if not runtime_drawing_data or not entities:
                warn_msg = (
                    f"[Agent WARN] session={session_id}: drawing_data가 비어 있습니다. "
                    f"CAD에서 도면을 먼저 추출·전송해 주세요. (cache_key={cache_key})"
                )
                logging.warning(warn_msg)
                await manager.send_to_group(
                    {
                        "action": "DRAWING_EMPTY",
                        "session_id": session_id,
                        "message": "도면 데이터가 없습니다. AutoCAD에서 도면을 먼저 전송해 주세요.",
                    },
                    "ui",
                )
                return  # 도면 없이 에이전트 실행 불가

            if entities:
                sample = entities[0]
                print(f"[Agent DEBUG] entity sample: type={sample.get('type')}, layer={sample.get('layer')}, keys={list(sample.keys())[:8]}")

            if user_request and _persist:
                message_id = await append_chat_message(
                    db=db,
                    session_id=session_id,
                    role="user",
                    content=user_request,
                    tool_calls=[],
                    active_object_ids=active_object_ids,
                    message_metadata={
                        "source": "agent_start_bg",
                        "domain": domain,
                        "job_id": job_id,
                    },
                )
                state = push_recent_chat_history(
                    state=state,
                    message_id=message_id,
                    role="user",
                    content=user_request,
                    active_object_ids=active_object_ids,
                )

            domain_names = {"pipe": "배관", "electric": "전기", "arch": "건축", "fire": "소방"}
            domain_kr = domain_names.get(domain, domain)
            await _send_progress(session_id, "agent_running", f"AI {domain_kr} 에이전트 실행 중... (최대 2분 소요)")

            start_time = time.time()
            result = await agent_service.run(
                domain,
                state,
                normalized_payload,
                db=db,
            )
            if _is_review_cancelled(session_id):
                await cad_service.delete_focus_drawing(str(session_id))
                return
            await _send_progress(session_id, "finalizing", "결과 정리 중...")

            await eval_tracker.track_cad_review_metrics(
                session_id=session_id,
                domain=domain,
                agent_result=result,
                start_time=start_time,
                drawing_data=runtime_drawing_data
            )
            
            state = apply_agent_execution_result(state, result)

            assistant_message = str(
                normalized_payload.get("assistant_message")
                or state.get("review_result", {}).get("final_message")
                or f"{domain} 검토가 완료되었습니다."
            )

            if _persist:
                resp_meta = state.get("response_meta") or result.get("response_meta") or {}
                wf = resp_meta.get("invoked_workflow")
                assistant_message_id = await append_chat_message(
                    db=db,
                    session_id=session_id,
                    role="assistant",
                    content=assistant_message,
                    tool_calls=tool_calls_from_workflow_steps(wf),
                    active_object_ids=state.get("active_object_ids", []),
                    agent_name=f"{domain}_graph",
                    message_metadata=resp_meta if resp_meta else None,
                )
                state = push_recent_chat_history(
                    state=state,
                    message_id=assistant_message_id,
                    role="assistant",
                    content=assistant_message,
                    active_object_ids=state.get("active_object_ids", []),
                )

            if "review_result" in state:
                state["review_result"]["final_message"] = assistant_message

            state = append_turn_summary(
                state=state,
                user_intent=user_request,
                reviewed_object_ids=state.get("active_object_ids", []),
                retrieved_law_refs=[law.get("legal_reference", "") for law in state.get("retrieved_laws", [])],
                violations_found=[item.get("reason", "") for item in state.get("review_result", {}).get("violations", [])],
                suggested_actions=state.get("review_result", {}).get("suggestions", []),
            )
            if _persist:
                await save_agent_state(db, session_id, state)

            await _send_review_websocket(session_id, state, assistant_message)
            await cad_service.delete_focus_drawing(str(session_id))

        except Exception as e:
            import traceback
            print(f"[Agent ERROR] [{job_id}]: {e}")
            traceback.print_exc()
            await manager.send_to_group(
                {"action": "ERROR", "session_id": session_id, "message": str(e)}, "ui"
            )

@router.post("/agent/context/laws", response_model=AgentLawContextResponse)
async def update_agent_law_context(
    body: AgentLawContextRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        await load_agent_state(db, body.session_id)
        normalized_laws = normalize_retrieved_laws({"retrieved_laws": body.retrieved_laws})

        return AgentLawContextResponse(
            status="success",
            message="Retrieved laws validated. In TEXT memory mode, pass them again in /agent/execute payload.",
            session_id=body.session_id,
            current_step="laws_retrieved",
            referenced_laws=[law["legal_reference"] for law in normalized_laws],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/agent/fixes/confirm", response_model=AgentFixesConfirmResponse)
async def confirm_fixes(
    body: AgentFixesConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        confirmed = await confirm_pending_fixes(db, body.session_id, body.selected_fix_ids)
        if confirmed:
            audit_rows = []
            for fix in confirmed:
                pf = fix.get("proposed_fix") or {}
                tier = infer_modification_tier(pf, fix.get("action"))
                audit_rows.append({
                    "fix_id": fix.get("fix_id"),
                    "object_id": fix.get("equipment_id"),
                    "action": fix.get("action"),
                    "modification_tier": tier,
                    "proposed_fix": pf,
                })
            await append_chat_message(
                db=db,
                session_id=body.session_id,
                role="system",
                content="[CAD 수정 감사] 승인된 수정 (객체·티어·auto_fix)\n" + json.dumps(
                    audit_rows, ensure_ascii=False, indent=2
                ),
                message_metadata={"kind": "cad_modification_audit", "tier_protocol": "1-4"},
            )

        return AgentFixesConfirmResponse(
            status="success",
            session_id=body.session_id,
            selected_count=len(confirmed),
            pending_fixes=[
                PendingFixResponse(
                    fix_id=fix["fix_id"],
                    equipment_id=fix["equipment_id"],
                    violation_type=fix["violation_type"],
                    action=fix["action"],
                    description=fix["description"],
                )
                for fix in confirmed
            ],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@router.post("/agent/execute", response_model=AgentExecuteResponse)
async def execute_agent(body: AgentExecuteRequest, db: AsyncSession = Depends(get_db)):
    try:
        state = await load_agent_state(db, body.session_id)
        normalized_payload = normalize_agent_payload(body.payload)
        normalized_payload = merge_agent_payload_with_state(state, normalized_payload)

        user_request = str(
            normalized_payload.get("message")
            or normalized_payload.get("user_request")
            or ""
        )
        active_object_ids = list(normalized_payload.get("active_object_ids") or [])

        state = apply_user_request(state, user_request, active_object_ids)

        runtime_drawing_data = normalized_payload.get("drawing_data") or {}
        if not (runtime_drawing_data.get("focus_extraction") or {}):
            red_focus = await cad_service.get_focus_drawing(str(body.session_id))
            if red_focus:
                runtime_drawing_data = dict(runtime_drawing_data)
                runtime_drawing_data["focus_extraction"] = red_focus
                if not runtime_drawing_data.get("context_mode"):
                    runtime_drawing_data["context_mode"] = CONTEXT_MODE_FULL_WITH_FOCUS
        runtime_retrieved_laws = normalized_payload.get("retrieved_laws") or []
        state["drawing_data"] = runtime_drawing_data
        state["retrieved_laws"] = runtime_retrieved_laws

        message_id = await append_chat_message(
            db=db,
            session_id=body.session_id,
            role="user",
            content=user_request,
            tool_calls=[],
            active_object_ids=active_object_ids,
            message_metadata={
                "source": "agent_execute",
                "domain": str(body.domain),
            },
        )
        state = push_recent_chat_history(
            state=state,
            message_id=message_id,
            role="user",
            content=user_request,
            active_object_ids=active_object_ids,
        )

        start_time = time.time()
        result = await agent_service.run(
            body.domain,
            state,
            normalized_payload,
            db=db,
        )

        await eval_tracker.track_cad_review_metrics(
            session_id=body.session_id,
            domain=body.domain,
            agent_result=result,
            start_time=start_time,
            drawing_data=runtime_drawing_data
        )
        state = apply_agent_execution_result(state, result)

        review_result_safe = state.get("review_result") or {}
        assistant_message = str(
            normalized_payload.get("assistant_message")
            or review_result_safe.get("final_message")
            or f"{body.domain} agent graph execution completed."
        )

        active_object_ids_safe = state.get("active_object_ids") or []
        retrieved_laws_safe = state.get("retrieved_laws") or []

        resp_meta_exec = state.get("response_meta") or result.get("response_meta") or {}
        wf_exec = resp_meta_exec.get("invoked_workflow")
        assistant_message_id = await append_chat_message(
            db=db,
            session_id=body.session_id,
            role="assistant",
            content=assistant_message,
            tool_calls=tool_calls_from_workflow_steps(wf_exec),
            active_object_ids=active_object_ids_safe,
            agent_name=f"{body.domain}_graph",
            message_metadata=resp_meta_exec if resp_meta_exec else None,
        )
        state = push_recent_chat_history(
            state=state,
            message_id=assistant_message_id,
            role="assistant",
            content=assistant_message,
            active_object_ids=active_object_ids_safe,
        )

        if isinstance(state.get("review_result"), dict):
            state["review_result"]["final_message"] = assistant_message
        state = append_turn_summary(
            state=state,
            user_intent=user_request,
            reviewed_object_ids=active_object_ids_safe,
            retrieved_law_refs=[
                law.get("legal_reference", "") for law in retrieved_laws_safe
            ],
            violations_found=[
                item.get("reason", "") for item in review_result_safe.get("violations") or []
            ],
            suggested_actions=review_result_safe.get("suggestions") or [],
        )
        await save_agent_state(db, body.session_id, state)
        await cad_service.delete_focus_drawing(str(body.session_id))

        _review_result = state.get("review_result") or {}
        return AgentExecuteResponse(
            status="success",
            message=f"{body.domain} agent graph executed successfully",
            data={
                "session_id": body.session_id,
                "domain": result.get("domain", body.domain),
                "current_step": state.get("current_step", ""),
                "active_object_ids": state.get("active_object_ids", []),
                "referenced_laws": _review_result.get("referenced_laws", []),
                "review_result": _review_result,
                "pending_fixes": state.get("pending_fixes", []),
                "response_meta": state.get("response_meta")
                or result.get("response_meta")
                or {},
                "received_payload": result.get("received_payload", {}),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _entity_by_object_id(object_id: str, entities: list, entity_by: dict) -> dict:
    if object_id is None:
        return {}
    oid = str(object_id).strip()
    if not oid:
        return {}
    # 1차: 정확한 키 매칭
    e = entity_by.get(oid) or entity_by.get(object_id)
    if isinstance(e, dict) and e:
        return e
    # 2차: 대소문자 무시 (LLM이 hex 핸들을 소문자로 출력하는 경우 대비)
    oid_upper = oid.upper()
    oid_lower = oid.lower()
    if oid_upper != oid:
        e = entity_by.get(oid_upper)
        if isinstance(e, dict) and e:
            return e
    if oid_lower != oid:
        e = entity_by.get(oid_lower)
        if isinstance(e, dict) and e:
            return e
    # 3차: 전체 목록 선형 탐색 (대소문자 무시)
    for ent in entities or []:
        if not isinstance(ent, dict):
            continue
        tags = (ent.get("attributes") or {}).get("TAG_NAME")
        for k in (ent.get("handle"), ent.get("id"), ent.get("block_name"), tags):
            if k is not None and str(k).strip().upper() == oid_upper:
                return ent
    return {}


def _bbox_from_entity(ent: dict) -> dict | None:
    """엔티티 dict에서 bbox를 추출하거나 위치 좌표로 합성합니다.
    C# 추출 데이터는 항상 bbox를 포함하지만, 파서가 변환한 데이터는 없을 수 있으므로 방어적 처리.
    """
    bbox = ent.get("bbox")
    if isinstance(bbox, dict) and bbox:
        # 키 유효성 확인
        if all(k in bbox for k in ("x1", "y1", "x2", "y2")):
            return bbox

    # bbox가 없으면 위치 좌표에서 합성 (작은 영역으로 지정)
    _SYNTH_HALF = 50.0  # 100mm × 100mm 합성 영역
    
    # ==== 테스트 0428'1230 ====
    
    # 1순위: LINE 객체는 start/end의 중간점(Midpoint)을 중심으로 사용
    if ent.get("type") == "LINE":
        s = ent.get("start")
        e = ent.get("end")
        if isinstance(s, dict) and isinstance(e, dict):
            try:
                cx = (float(s.get("x", 0)) + float(e.get("x", 0))) / 2
                cy = (float(s.get("y", 0)) + float(e.get("y", 0))) / 2
                return {
                    "x1": cx - _SYNTH_HALF, "y1": cy - _SYNTH_HALF,
                    "x2": cx + _SYNTH_HALF, "y2": cy + _SYNTH_HALF
                }
            except (TypeError, ValueError): pass
    # ===============================

    # 2순위: 기타 객체 (INSERT, CIRCLE 등)
    for pos_key in ("insert_point", "center", "position", "start"):
        pos = ent.get(pos_key)
        if isinstance(pos, dict):
            try:
                cx = float(pos.get("x", 0) or 0)
                cy = float(pos.get("y", 0) or 0)
                if cx or cy:
                    return {
                        "x1": cx - _SYNTH_HALF,
                        "y1": cy - _SYNTH_HALF,
                        "x2": cx + _SYNTH_HALF,
                        "y2": cy + _SYNTH_HALF,
                    }
            except (TypeError, ValueError):
                pass
    return None


def _is_drawing_quality_violation_for_cad(v: dict) -> bool:
    source = str(v.get("_source") or v.get("source") or "")
    kind = str(v.get("violation_type") or v.get("issue_type") or "")
    return source == "drawing_qa" or kind.startswith("drawing_quality_")


def _point_xy_for_bbox(point: object) -> tuple[float, float] | None:
    if isinstance(point, dict):
        try:
            return float(point.get("x")), float(point.get("y"))
        except (TypeError, ValueError):
            return None
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        try:
            return float(point[0]), float(point[1])
        except (TypeError, ValueError):
            return None
    return None


def _bbox_from_points_for_cad(points: list[tuple[float, float]], pad: float = 50.0) -> dict | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {
        "x1": min(xs) - pad,
        "y1": min(ys) - pad,
        "x2": max(xs) + pad,
        "y2": max(ys) + pad,
    }


def _bbox_from_violation_geometry(v: dict, fix: dict | None) -> dict | None:
    sources: list[dict] = []
    pa = v.get("proposed_action")
    if isinstance(pa, dict):
        sources.append(pa)
    proposed_fix = (fix or {}).get("proposed_fix")
    if isinstance(proposed_fix, dict):
        sources.append(proposed_fix)

    for source in sources:
        for a_key, b_key in (
            ("cloud_from", "cloud_to"),
            ("touch_point", "overshoot_end"),
            ("new_start", "new_end"),
            ("new_center", "new_center"),
        ):
            pts = [
                p
                for p in (
                    _point_xy_for_bbox(source.get(a_key)),
                    _point_xy_for_bbox(source.get(b_key)),
                )
                if p is not None
            ]
            bbox = _bbox_from_points_for_cad(pts)
            if bbox:
                return bbox
        vertices = source.get("new_vertices")
        if isinstance(vertices, list):
            pts = [p for p in (_point_xy_for_bbox(x) for x in vertices) if p is not None]
            bbox = _bbox_from_points_for_cad(pts)
            if bbox:
                return bbox
    return None


def _iter_key_values(value: object):
    if value is None or value == "":
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield item
    else:
        yield value


def _candidate_entity_keys(v: dict, fix: dict | None) -> list[str]:
    raw_values: list[object] = [
        v.get("object_id"),
        v.get("equipment_id"),
        (fix or {}).get("equipment_id"),
        (fix or {}).get("handle"),
    ]

    for source in (v, fix or {}):
        raw_values.extend(_iter_key_values(source.get("related_handles")) or [])
        raw_values.append(source.get("display_object_id"))

    for source in (v.get("proposed_action"), (fix or {}).get("proposed_fix")):
        if not isinstance(source, dict):
            continue
        for key in ("target_handle", "handle", "equipment_id"):
            raw_values.append(source.get(key))
        for key in ("target_handles", "related_handles", "symbol_cluster_handles"):
            raw_values.extend(_iter_key_values(source.get(key)) or [])

    candidates: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for value in _iter_key_values(raw) or []:
            text = str(value or "").strip()
            if not text:
                continue
            parts = [text]
            if "<->" in text or "," in text or ";" in text or " " in text:
                parts.extend(p for p in re.split(r"<->|[,;\s]+", text) if p)
            for part in parts:
                key = str(part or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(key)
    return candidates


def _resolve_entity_for_violation(
    v: dict, fix: dict, entities: list, entity_by: dict
) -> dict:
    for raw in _candidate_entity_keys(v, fix):
        e = _entity_by_object_id(raw, entities, entity_by)
        if e:
            return e
    return {}


def _violations_from_pending_for_cad(pending_fixes: list) -> list[dict]:
    out: list[dict] = []
    for f in pending_fixes or []:
        if not isinstance(f, dict):
            continue
        eid = str(f.get("equipment_id") or "")
        desc = str(f.get("description") or "")
        vtype = str(f.get("violation_type") or "")
        out.append({
            "object_id": eid,
            "violation_type": vtype,
            "reason": desc or (f"{vtype} (수정 대기)" if vtype else "수정 대기"),
            "legal_reference": "",
            "suggestion": desc,
            "current_value": "",
            "required_value": "",
        })
    return out


async def _send_review_websocket(session_id: str, state: dict, reply: str) -> None:
    drawing_data = state.get("drawing_data") or {}
    entities: list = drawing_data.get("entities") or drawing_data.get("elements") or []
    violations: list = (state.get("review_result") or {}).get("violations") or []
    pending_fixes: list = state.get("pending_fixes") or []
    if (not violations) and pending_fixes:
        violations = _violations_from_pending_for_cad(pending_fixes)

    entity_by: dict = {}
    for ent in entities:
        tags = (ent.get("attributes") or {}).get("TAG_NAME")
        for key in (ent.get("handle"), ent.get("id"), ent.get("block_name"), tags):
            if key and key not in entity_by:
                entity_by[key] = ent

    fix_by_equip: dict = {}
    fix_by_pair: dict[tuple[str, str], dict] = {}
    fix_by_group: dict[str, dict] = {}

    def _index_fix_key(key: object, fix: dict) -> None:
        key_s = str(key or "").strip()
        if key_s:
            fix_by_equip.setdefault(key_s, fix)

    for f in pending_fixes or []:
        if not isinstance(f, dict):
            continue
        eq = str(f.get("equipment_id") or "").strip()
        vtype = str(f.get("violation_type") or "").strip()
        group = str(f.get("group_id") or "").strip()
        if eq and vtype:
            fix_by_pair.setdefault((eq, vtype), f)
        if group:
            fix_by_group.setdefault(group, f)
        _index_fix_key(eq, f)
        _index_fix_key(f.get("handle"), f)
        _index_fix_key(f.get("display_object_id"), f)
        for key in f.get("related_handles") or []:
            _index_fix_key(key, f)
        proposed_fix = f.get("proposed_fix") or {}
        if isinstance(proposed_fix, dict):
            _index_fix_key(proposed_fix.get("target_handle"), f)
            for key in proposed_fix.get("target_handles") or []:
                _index_fix_key(key, f)
            for key in proposed_fix.get("related_handles") or []:
                _index_fix_key(key, f)
            for key in proposed_fix.get("symbol_cluster_handles") or []:
                _index_fix_key(key, f)

    domain_type = (state.get("session_meta") or {}).get("domain_type")
    org_for_role = state.get("org_id")
    db_layer_roles: dict | None = None
    if domain_type == "pipe" and org_for_role:
        try:
            from backend.services.agents.pipe.sub.mapping import get_layer_role_map
            db_layer_roles = get_layer_role_map(str(org_for_role)) or None
        except Exception as exc:
            logging.debug("[AgentCAD] get_layer_role_map: %s", exc)
    ncs_mep = getattr(settings, "NCS_DISCIPLINE_MEP_PREFIX", True)

    annotated_entities = []
    skipped_arch = 0
    unresolved = 0
    print(
        f"[AgentCAD REVIEW] session={session_id} violations={len(violations)} "
        f"pending={len(pending_fixes)} entities={len(entities)}"
    )
    for v in violations:
        if not isinstance(v, dict):
            continue
        obj_id = str(v.get("object_id", "") or "").strip()
        vtype = str(v.get("violation_type", "") or "").strip()
        group = str(v.get("group_id") or "").strip()
        fix = (
            fix_by_group.get(group)
            or fix_by_pair.get((obj_id, vtype))
            or fix_by_equip.get(obj_id)
            or {}
        )
        ent = _resolve_entity_for_violation(v, fix, entities, entity_by)
        is_drawing_quality = _is_drawing_quality_violation_for_cad(v)
        if not ent:
            unresolved += 1

        if (
            domain_type == "pipe"
            and isinstance(ent, dict)
            and ent
            and not is_drawing_quality
            and entity_layer_role(ent, db_layer_roles, ncs_mep_prefix=ncs_mep) == "arch"
        ):
            skipped_arch += 1
            continue
        
        proposed = fix.get("proposed_fix") or {}
        af = _resolve_autofix_for_cad(fix.get("action", ""), proposed)
        if af:
            af = merge_autofix_with_tier(af, proposed, fix.get("action"))

        # ✨ [핵심 수정] proposed_action fallback 로직 적용
        if af is None:
            pa = v.get("proposed_action")
            if isinstance(pa, dict) and pa.get("type"):
                af = _resolve_autofix_for_cad(str(pa.get("type", "")), pa)
                logging.info(
                    "[AgentCAD] proposed_action 변환 완료 및 사용: obj_id=%s type=%s",
                    obj_id, pa.get("type")
                )
                
        tier = infer_modification_tier(
            {
                **(proposed or {}),
                **(af or {}),
                "modification_tier": (
                    v.get("modification_tier")
                    or (proposed or {}).get("modification_tier")
                    or (af or {}).get("modification_tier")
                ),
            },
            fix.get("action"),
        )

        _violation_dict: dict = {
            "id":          fix.get("fix_id") or obj_id,
            "type":        v.get("violation_type", ""),
            "violation_type": v.get("violation_type", ""),
            "source":      v.get("_source", "llm"),
            "_source":     v.get("_source", "llm"),
            "severity":    v.get("severity") or _severity(v.get("violation_type", "")),
            "rule":        v.get("legal_reference", ""),
            "description": v.get("reason", ""),
            "suggestion":  v.get("suggestion", ""),
            "confidence_score": v.get("confidence_score"),
            "confidence_reason": v.get("confidence_reason"),
            "modification_tier": tier,
            "auto_fix":    af,
        }
        if v.get("proposed_action"):
            _violation_dict["proposed_action"] = v["proposed_action"]
        for extra_key in ("related_handles", "group_id", "display_object_id"):
            if v.get(extra_key) is not None:
                _violation_dict[extra_key] = v.get(extra_key)
            
        # handle: 실제 CAD hex 핸들 우선. 엔티티를 못 찾은 경우 obj_id가 fallback이 되어
        # C#에서 Convert.ToInt64(hex, 16) 실패 가능성이 있으므로 가능한 한 실제 handle을 사용.
        _handle = ent.get("handle") or fix.get("handle") or obj_id

        # bbox: C# RevCloud/ZoomToEntity 양쪽 모두 null이면 완전히 무기능 → 반드시 채움
        _bbox = None
        for stored in (
            v.get("bbox"),
            v.get("target_bbox"),
            (fix.get("proposed_fix") or {}).get("_entity_bbox") if fix else None,
            fix.get("bbox") if fix else None,
        ):
            if isinstance(stored, dict) and all(k in stored for k in ("x1", "y1", "x2", "y2")):
                _bbox = stored
                break
        if _bbox is None and is_drawing_quality:
            _bbox = _bbox_from_violation_geometry(v, fix)
        if _bbox is None:
            _bbox = _bbox_from_entity(ent)
        if _bbox is None:
            _bbox = _bbox_from_violation_geometry(v, fix)

        annotated_entities.append({
            "handle": _handle,
            "type":   ent.get("type", ""),
            "layer":  ent.get("layer", ""),
            "bbox":   _bbox,
            "violation": _violation_dict,
        })

    sample = ""
    if annotated_entities:
        first = annotated_entities[0]
        sample = (
            f" handle={first.get('handle')} "
            f"type={(first.get('violation') or {}).get('violation_type')}"
        )
    print(
        f"[AgentCAD REVIEW] annotated={len(annotated_entities)} "
        f"skipped_arch={skipped_arch} unresolved={unresolved}{sample}"
    )

    payload = {
        "session_id":         session_id,
        "reply":              reply,
        "annotated_entities": annotated_entities,
        "response_meta":     state.get("response_meta") or {},
        "review_domain":     domain_type,
    }

    await manager.send_to_group({"action": "REVIEW_RESULT",    "payload": payload}, "cad")
    await manager.send_to_group({"action": "REVIEW_RESULT_UI", "payload": payload}, "ui")


def _severity(violation_type: str) -> str:
    if violation_type in {
        "pressure_overload", "fire_penetration_error", "seismic_support_error",
        "open_circuit_error",               # 미결선 — 전원 미공급
        "wire_count_violation",             # 1가닥 불가 (Critical 케이스)
    }:
        return "Critical"
    if violation_type in {
        "pipe_size_mismatch", "material_mismatch", "slope_error",
        "expansion_joint_missing", "fire_compartment_area",
        "exit_distance_error", "voltage_drop_exceeded",
        "grounding_rod_spacing_violation",  # KEC 140.6 접지봉 이격거리
        "grounding_rod_count_mismatch",     # 접지봉 수량 부족
        "breaker_pole_mismatch",            # 차단기 극수 불일치
        "nonstandard_voltage",              # 비표준 전압
        "outlet_height_violation",          # KEC 232.56 콘센트 설치 높이
        "wire_count_violation",             # 배선 가닥 수 불일치
    }:
        return "Major"
    return "Minor"


_CAD_AUTOFIX_TYPES = frozenset(
    "ATTRIBUTE LAYER TEXT_CONTENT TEXT_HEIGHT COLOR LINETYPE LINEWEIGHT "
    "DELETE MOVE ROTATE SCALE GEOMETRY BLOCK_REPLACE DYNAMIC_BLOCK_PARAM "
    "RECTANGLE_RESIZE STRETCH_RECT "
    "CREATE_ENTITY CREATE_LINE CREATE_CIRCLE CREATE_POLYLINE CREATE_BLOCK CREATE_TEXT".split()
)

# ✨ [핵심 수정] proposed_action 을 받아 C#이 이해하는 auto_fix 구조로 정규화
def _resolve_autofix_for_cad(action: str, proposed: dict) -> dict | None:
    if isinstance(proposed, dict):
        t = str(proposed.get("type", "") or "").upper()
        if t in _CAD_AUTOFIX_TYPES:
            out: dict = {**proposed, "type": t}
            if t == "COLOR" and out.get("new_color") is not None:
                try:
                    out["new_color"] = int(out["new_color"])
                except (TypeError, ValueError):
                    return _to_autofix(action, proposed)
            return out

        # proposed_fix 안에 nested auto_fix 서브딕트가 있으면 우선 사용한다.
        # (revision.py의 _dispatch 핸들러들이 {"action":..., "auto_fix":{...}} 형태로 반환)
        nested = proposed.get("auto_fix")
        if isinstance(nested, dict):
            nt = str(nested.get("type", "") or "").upper()
            if nt in _CAD_AUTOFIX_TYPES:
                out = {**nested, "type": nt}
                if nt == "COLOR" and out.get("new_color") is not None:
                    try:
                        out["new_color"] = int(out["new_color"])
                    except (TypeError, ValueError):
                        pass
                return out

    return _to_autofix(action, proposed or {})


def _to_autofix(action: str, proposed: dict) -> dict | None:
    a = action.upper()

    # [핵심 로직] 전선 굵기 변경 (CHANGE_CABLE_SIZE -> LAYER)
    if a == "CHANGE_CABLE_SIZE":
        req = str(proposed.get("required_size", "")).strip()
        if req:
            import re as _re
            m = _re.search(r"(\d+(?:\.\d+)?)", req)
            sq = m.group(1) if m else req
            return {"type": "LAYER", "new_layer": f"Cable_{sq}SQ"}
        return {"type": "ATTRIBUTE", "attribute_tag": "SQ", "new_value": req}

    # [핵심 로직] 색상 변경 (CHANGE_COLOR -> COLOR ACI 매핑)
    if a == "CHANGE_COLOR":
        color_map = {"RED": 1, "빨강": 1, "YELLOW": 2, "노랑": 2, "GREEN": 3, "초록": 3,
                     "CYAN": 4, "청록": 4, "BLUE": 5, "파랑": 5, "WHITE": 7, "흰색": 7,
                     "BLACK": 0, "검정": 0, "BROWN": 30, "갈색": 30}
        req_color = str(proposed.get("required_color", "")).strip().upper()
        aci = color_map.get(req_color)
        if aci is not None:
            return {"type": "COLOR", "new_color": aci}
        try:
            return {"type": "COLOR", "new_color": int(req_color)}
        except (ValueError, TypeError):
            pass
        return {"type": "ATTRIBUTE", "attribute_tag": "COLOR", "new_value": req_color}

    # [핵심 로직] 차단기 용량 변경
    if a == "CHANGE_BREAKER_CAPACITY":
        req = str(proposed.get("required_capacity", "")).strip()
        return {"type": "ATTRIBUTE", "attribute_tag": "CAPACITY", "new_value": req}

    # [핵심 로직] 신규 도형/블록 생성
    if a == "CREATE_ENTITY":
        return {
            "type": "CREATE_ENTITY",
            "new_block_name": str(proposed.get("new_block_name", "")),
            "base_x": proposed.get("base_x"),
            "base_y": proposed.get("base_y"),
            "new_start": proposed.get("new_start"),
            "new_end": proposed.get("new_end"),
            "new_center": proposed.get("new_center"),
            "new_radius": proposed.get("new_radius"),
            "new_vertices": proposed.get("new_vertices"),
            "new_layer": str(proposed.get("new_layer", "AI_PROPOSAL"))
        }

    # ── 기타 기존 로직들 ──
    if a in ("MOVE", "MOVE_ENTITY"):
        return {"type": "MOVE", "delta_x": proposed.get("delta_x", 0.0), "delta_y": proposed.get("delta_y", 0.0)}
    if a == "REPLACE_MATERIAL":
        return {"type": "ATTRIBUTE", "attribute_tag": "MATERIAL", "new_value": str(proposed.get("required_material", ""))}
    if a == "DELETE":
        return {"type": "DELETE", "modification_tier": 4}
    if a == "LAYER":
        return {"type": "LAYER", "new_layer": str(proposed.get("new_layer", ""))}
    if a in ("SCALE", "RESIZE"):
        sf = proposed.get("scale_factor", 1.0)
        return {"type": "SCALE", "scale_x": proposed.get("scale_x", sf), "scale_y": proposed.get("scale_y", sf)}
    
    if a in ("TEXT_CONTENT", "CHANGE_TEXT", "SET_TEXT", "MTEXT"):
        nt = proposed.get("new_text") or proposed.get("text") or proposed.get("value")
        if nt is None:
            return None
        return {"type": "TEXT_CONTENT", "new_text": str(nt)}
    if a in ("TEXT_HEIGHT",) and proposed.get("new_height") is not None:
        try:
            return {"type": "TEXT_HEIGHT", "new_height": float(proposed.get("new_height"))}
        except (TypeError, ValueError):
            return None
    if a in ("LINETYPE",) and proposed.get("new_linetype"):
        return {"type": "LINETYPE", "new_linetype": str(proposed.get("new_linetype", ""))}
    if a in ("LINEWEIGHT",) and proposed.get("new_lineweight") is not None:
        try:
            return {"type": "LINEWEIGHT", "new_lineweight": float(proposed.get("new_lineweight"))}
        except (TypeError, ValueError):
            return None
            
    if proposed.get("new_text") is not None:
        return {"type": "TEXT_CONTENT", "new_text": str(proposed["new_text"])}
        
    if a in ("BLOCK_REPLACE",) and proposed.get("new_block_name"):
        return {
            "type": "BLOCK_REPLACE",
            "new_block_name": str(proposed.get("new_block_name", "")),
        }
    if a in ("DYNAMIC_BLOCK_PARAM",) and (proposed.get("param_name") or proposed.get("parameter_name")):
        return {
            "type": "DYNAMIC_BLOCK_PARAM",
            "param_name": str(proposed.get("param_name") or proposed.get("parameter_name", "")),
            "param_value": str(proposed.get("param_value", proposed.get("value", ""))),
        }

    if a == "UPDATE_ATTRIBUTE":
        tag = str(proposed.get("attribute_tag", "VALUE")).strip()
        val = str(proposed.get("new_value", proposed.get("required_value", ""))).strip()
        return {"type": "ATTRIBUTE", "attribute_tag": tag, "new_value": val}

    if a == "MANUAL_REVIEW":
        return None

    return None
