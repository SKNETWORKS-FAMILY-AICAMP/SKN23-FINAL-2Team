# backend/api/routers/drawing_state_router.py
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.redis_client import get_redis_client
from backend.core.socket_manager import manager
from backend.services.drawing_state_service import (
    ChangedHandles,
    ConflictError,
    DirtyBatchIn,
    DrawingStateService,
    ResyncStartResult,
)
from backend.utils.s3_manager import S3Manager

log = logging.getLogger(__name__)
router = APIRouter()


def get_drawing_state_service() -> DrawingStateService:
    return DrawingStateService(redis=get_redis_client(), s3=S3Manager())


async def _broadcast_state_changed(drawing_id: str, svc: DrawingStateService) -> None:
    try:
        state = await svc.get_state(drawing_id)
        await manager.send_to_group(
            {
                "action": "DRAWING_STATE_CHANGED",
                "payload": {
                    "drawing_id":               state.drawing_id,
                    "status":                   state.status,
                    "change_mode":              state.change_mode,
                    "dirty_count":              state.dirty_count,
                    "delta_count":              state.delta_count,
                    "delta_size":               state.delta_size,
                    "current_snapshot_id":      state.current_snapshot_id,
                    "current_snapshot_path":    state.current_snapshot_path,
                    "latest_delta_path":        state.latest_delta_path,
                    "can_review_stale_snapshot": state.can_review_stale_snapshot,
                    "pending_after_resync":     state.pending_after_resync,
                    "last_updated_at":          state.last_updated_at,
                    "last_error":               state.last_error,
                },
            },
            "ui",
        )
    except Exception as exc:
        log.warning("[drawing_state_router] DRAWING_STATE_CHANGED 발송 실패: %s", exc)


# ── Request models ───────────────────────────────────────────────────────────

class DirtyBulkIn(BaseModel):
    session_id:   str
    org_id:       str
    modified:     list[str] = Field(default_factory=list)
    appended:     list[str] = Field(default_factory=list)
    erased:       list[str] = Field(default_factory=list)
    layer_added:  list[str] = Field(default_factory=list)
    layer_deleted: list[str] = Field(default_factory=list)


class PresignIn(BaseModel):
    org_id: str


class SnapshotCommitIn(BaseModel):
    snapshot_id:   str
    s3_key:        str
    s3_uri:        str
    entity_count:  int
    revision_hash: str
    resync_token:  str | None = None
    session_id:    str | None = None
    org_id:        str = ""


class DeltaCommitIn(BaseModel):
    delta_id:         str
    s3_key:           str
    s3_uri:           str
    delta_size:       int
    base_snapshot_id: str
    changed_handles:  dict[str, list[str]] = Field(default_factory=dict)
    session_id:       str | None = None
    org_id:           str = ""


class ResyncFailIn(BaseModel):
    resync_token: str
    error:        str = "upload failed"


# ── Endpoints ─────────────────────────────────────────────────────────────────

class CadDebugLogIn(BaseModel):
    message: str
    drawing_id: str = ""
    session_id: str = ""

@router.post("/debug/log")
async def cad_debug_log(body: CadDebugLogIn):
    print(f"[CAD_DEBUG] drawing_id={body.drawing_id or '(none)'} session={body.session_id or '(none)'} | {body.message}")
    return {"ok": True}


@router.get("/{drawing_id}/state")
async def get_state(
    drawing_id: str,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    state = await svc.get_state(drawing_id)
    return {
        "drawing_id":               state.drawing_id,
        "status":                   state.status,
        "change_mode":              state.change_mode,
        "dirty_count":              state.dirty_count,
        "delta_size":               state.delta_size,
        "delta_count":              state.delta_count,
        "current_snapshot_id":      state.current_snapshot_id,
        "current_snapshot_path":    state.current_snapshot_path,
        "latest_delta_path":        state.latest_delta_path,
        "pending_after_resync":     state.pending_after_resync,
        "can_review_stale_snapshot": state.can_review_stale_snapshot,
        "last_updated_at":          state.last_updated_at,
        "last_error":               state.last_error,
    }


@router.post("/{drawing_id}/dirty/bulk")
async def dirty_bulk(
    drawing_id: str,
    body: DirtyBulkIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    print(
        f"[DIRTY_BULK] drawing_id={drawing_id} session={body.session_id} "
        f"modified={len(body.modified)} appended={len(body.appended)} "
        f"erased={len(body.erased)}"
    )
    batch = DirtyBatchIn(
        session_id=body.session_id, org_id=body.org_id,
        modified=body.modified, appended=body.appended, erased=body.erased,
        layer_added=body.layer_added, layer_deleted=body.layer_deleted,
    )
    result = await svc.apply_dirty_batch(drawing_id, batch)
    print(
        f"[DIRTY_BULK] drawing_id={drawing_id} → next_mode={result.next_mode} "
        f"snapshot_id={result.current_snapshot_id} dirty_count={result.dirty_count}"
    )
    await _broadcast_state_changed(drawing_id, svc)
    return {
        "status":               result.status,
        "next_mode":            result.next_mode,
        "dirty_count":          result.dirty_count,
        "threshold":            result.threshold,
        "requires_upload":      result.requires_upload,
        "reason":               result.reason,
        "current_snapshot_id":  result.current_snapshot_id,
        "current_snapshot_path": result.current_snapshot_path,
    }


@router.post("/{drawing_id}/snapshots/presign")
async def snapshot_presign(
    drawing_id: str,
    body: PresignIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    result = await svc.presign_snapshot_upload(drawing_id, body.org_id)
    return {
        "snapshot_id": result.resource_id,
        "s3_key":      result.s3_key,
        "s3_uri":      result.s3_uri,
        "upload_url":  result.upload_url,
        "expires_in":  result.expires_in,
    }


@router.post("/{drawing_id}/snapshots/commit")
async def snapshot_commit(
    drawing_id: str,
    body: SnapshotCommitIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    try:
        await svc.commit_snapshot(
            drawing_id=drawing_id,
            snapshot_id=body.snapshot_id,
            s3_key=body.s3_key,
            s3_uri=body.s3_uri,
            entity_count=body.entity_count,
            revision_hash=body.revision_hash,
            resync_token=body.resync_token,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _broadcast_state_changed(drawing_id, svc)
    return {"status": "committed", "snapshot_id": body.snapshot_id}


@router.post("/{drawing_id}/deltas/presign")
async def delta_presign(
    drawing_id: str,
    body: PresignIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    result = await svc.presign_delta_upload(drawing_id, body.org_id)
    return {
        "delta_id":   result.resource_id,
        "s3_key":     result.s3_key,
        "s3_uri":     result.s3_uri,
        "upload_url": result.upload_url,
        "expires_in": result.expires_in,
    }


@router.post("/{drawing_id}/deltas/commit")
async def delta_commit(
    drawing_id: str,
    body: DeltaCommitIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    ch = ChangedHandles(
        modified=body.changed_handles.get("modified", []),
        appended=body.changed_handles.get("appended", []),
        erased=body.changed_handles.get("erased", []),
        layer_added=body.changed_handles.get("layer_added", []),
        layer_deleted=body.changed_handles.get("layer_deleted", []),
    )
    try:
        await svc.commit_delta(
            drawing_id=drawing_id,
            delta_id=body.delta_id,
            s3_key=body.s3_key,
            s3_uri=body.s3_uri,
            delta_size=body.delta_size,
            base_snapshot_id=body.base_snapshot_id,
            changed_handles=ch,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await _broadcast_state_changed(drawing_id, svc)
    return {"status": "committed", "delta_id": body.delta_id}


@router.post("/{drawing_id}/resync/start")
async def resync_start(
    drawing_id: str,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    result: ResyncStartResult = await svc.start_resync(drawing_id)
    if not result.acquired:
        raise HTTPException(status_code=409, detail="FULL_RESYNC already in progress")
    await _broadcast_state_changed(drawing_id, svc)
    return {"acquired": True, "resync_token": result.resync_token}


@router.post("/{drawing_id}/resync/fail")
async def resync_fail(
    drawing_id: str,
    body: ResyncFailIn,
    svc: DrawingStateService = Depends(get_drawing_state_service),
):
    await svc.mark_resync_failed(drawing_id, body.error, body.resync_token)
    await _broadcast_state_changed(drawing_id, svc)
    return {"status": "failed", "drawing_id": drawing_id}
