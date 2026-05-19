# backend/services/drawing_state_service.py
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

log = logging.getLogger(__name__)

DIRTY_THRESHOLD       = int(os.getenv("DIRTY_THRESHOLD", "500"))
DELTA_COUNT_THRESHOLD = int(os.getenv("DELTA_COUNT_THRESHOLD", "10"))
DELTA_SIZE_THRESHOLD  = int(os.getenv("DELTA_SIZE_THRESHOLD", str(10 * 1024 * 1024)))
RESYNC_LOCK_TTL       = int(os.getenv("RESYNC_LOCK_TTL", "300"))
SNAPSHOT_TTL          = 60 * 60 * 24 * 90   # 90일
STATUS_TTL            = 60 * 60 * 24 * 7    # 7일
DIRTY_SET_TTL         = 60 * 60 * 24        # 24시간
PENDING_TTL           = 900                 # presign URL TTL (15분)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DirtyBatchIn:
    session_id:   str
    org_id:       str
    modified:     list[str] = field(default_factory=list)
    appended:     list[str] = field(default_factory=list)
    erased:       list[str] = field(default_factory=list)
    layer_added:  list[str] = field(default_factory=list)
    layer_deleted: list[str] = field(default_factory=list)


@dataclass
class DirtyBatchResult:
    status:                str          # accepted | deferred
    next_mode:             str          # INCREMENTAL | FULL_RESYNC | WAIT
    dirty_count:           int
    threshold:             int
    requires_upload:       str          # delta | snapshot | none
    reason:                str | None
    current_snapshot_id:   str | None
    current_snapshot_path: str | None


@dataclass
class DrawingState:
    drawing_id:              str
    status:                  str
    change_mode:             str
    dirty_count:             int
    delta_count:             int
    delta_size:              int
    current_snapshot_id:     str | None
    current_snapshot_path:   str | None
    latest_delta_path:       str | None
    pending_after_resync:    bool
    can_review_stale_snapshot: bool
    last_updated_at:         str | None
    last_error:              str | None


@dataclass
class PresignResult:
    resource_id: str       # snapshotId 또는 deltaId
    s3_key:      str       # HEAD 검증용 key
    s3_uri:      str       # Redis 저장용 s3://bucket/key
    upload_url:  str       # presigned PUT URL
    expires_in:  int


@dataclass
class ResyncStartResult:
    acquired:     bool
    resync_token: str | None


@dataclass
class ChangedHandles:
    modified:     list[str] = field(default_factory=list)
    appended:     list[str] = field(default_factory=list)
    erased:       list[str] = field(default_factory=list)
    layer_added:  list[str] = field(default_factory=list)
    layer_deleted: list[str] = field(default_factory=list)


class ConflictError(Exception):
    pass


class DrawingStateService:
    """
    Redis dirty set, status/changeMode, threshold 판단, S3 presign/commit, delta merge 담당.
    전체 엔티티 JSON은 절대 Redis에 저장하지 않는다.
    Redis client는 decode_responses=True 설정으로 생성되어야 한다 (문자열 반환 보장).
    """

    def __init__(self, redis, s3):
        self._redis = redis
        self._s3    = s3

    def _decide_next_mode(
        self, dirty_count: int, delta_count: int, delta_size: int
    ) -> tuple[str, str | None]:
        if dirty_count >= DIRTY_THRESHOLD:
            return "FULL_RESYNC", "DIRTY_THRESHOLD_EXCEEDED"
        if delta_count >= DELTA_COUNT_THRESHOLD:
            return "FULL_RESYNC", "DELTA_COUNT_EXCEEDED"
        if delta_size >= DELTA_SIZE_THRESHOLD:
            return "FULL_RESYNC", "DELTA_SIZE_EXCEEDED"
        return "INCREMENTAL", None

    async def _compute_dirty_count(self, drawing_id: str) -> int:
        keys = ["modified", "appended", "erased", "layerAdded", "layerDeleted"]
        counts = await asyncio.gather(
            *[self._redis.scard(f"drawing:{drawing_id}:dirty:{k}") for k in keys]
        )
        return sum(counts)

    async def apply_dirty_batch(
        self, drawing_id: str, batch: DirtyBatchIn
    ) -> DirtyBatchResult:
        lock_key = f"lock:drawing:{drawing_id}:resync"
        if await self._redis.exists(lock_key):
            await self._redis.set(
                f"drawing:{drawing_id}:pendingAfterResync", "1", ex=PENDING_TTL
            )
            return DirtyBatchResult(
                status="deferred", next_mode="WAIT",
                dirty_count=0, threshold=DIRTY_THRESHOLD,
                requires_upload="none", reason="FULL_RESYNC_RUNNING",
                current_snapshot_id=None, current_snapshot_path=None,
            )

        # pendingAfterResync가 있으면 이번 배치 수신 시 삭제 (재수집 완료 신호)
        await self._redis.delete(f"drawing:{drawing_id}:pendingAfterResync")

        mapping = {
            "modified": batch.modified,
            "appended": batch.appended,
            "erased":   batch.erased,
            "layerAdded": batch.layer_added,
            "layerDeleted": batch.layer_deleted,
        }
        for suffix, handles in mapping.items():
            if handles:
                await self._redis.sadd(
                    f"drawing:{drawing_id}:dirty:{suffix}", *handles,
                )
                await self._redis.expire(
                    f"drawing:{drawing_id}:dirty:{suffix}", DIRTY_SET_TTL
                )

        dirty_count = await self._compute_dirty_count(drawing_id)
        delta_count = int(await self._redis.get(f"drawing:{drawing_id}:deltaCount") or 0)
        delta_size  = int(await self._redis.get(f"drawing:{drawing_id}:deltaSize")  or 0)
        snapshot_id   = await self._redis.get(f"drawing:{drawing_id}:currentSnapshotId")
        snapshot_path = await self._redis.get(f"drawing:{drawing_id}:currentSnapshotPath")

        # Issue 5: snapshot 없으면 무조건 FULL_RESYNC 요구
        if not snapshot_id or not snapshot_path:
            now = _utcnow_iso()
            pipe = self._redis.pipeline(transaction=True)
            pipe.set(f"drawing:{drawing_id}:changeMode",    "FULL_RESYNC_PENDING", ex=STATUS_TTL)
            pipe.set(f"drawing:{drawing_id}:status",        "DIRTY",              ex=STATUS_TTL)
            pipe.set(f"drawing:{drawing_id}:dirtyCount",    str(dirty_count),     ex=STATUS_TTL)
            pipe.set(f"drawing:{drawing_id}:lastUpdatedAt", now,                  ex=STATUS_TTL)
            await pipe.execute()
            return DirtyBatchResult(
                status="accepted", next_mode="FULL_RESYNC",
                dirty_count=dirty_count, threshold=DIRTY_THRESHOLD,
                requires_upload="snapshot", reason="NO_SNAPSHOT",
                current_snapshot_id=None, current_snapshot_path=None,
            )

        next_mode, reason = self._decide_next_mode(dirty_count, delta_count, delta_size)
        requires_upload = "snapshot" if next_mode == "FULL_RESYNC" else "delta"

        change_mode = "FULL_RESYNC_PENDING" if next_mode == "FULL_RESYNC" else "INCREMENTAL_PENDING"
        await self._redis.set(f"drawing:{drawing_id}:changeMode",    change_mode,         ex=STATUS_TTL)
        await self._redis.set(f"drawing:{drawing_id}:status",        "DIRTY",              ex=STATUS_TTL)
        await self._redis.set(f"drawing:{drawing_id}:dirtyCount",    str(dirty_count),     ex=STATUS_TTL)
        await self._redis.set(f"drawing:{drawing_id}:lastUpdatedAt", _utcnow_iso(),         ex=STATUS_TTL)

        return DirtyBatchResult(
            status="accepted", next_mode=next_mode,
            dirty_count=dirty_count, threshold=DIRTY_THRESHOLD,
            requires_upload=requires_upload, reason=reason,
            current_snapshot_id=snapshot_id,
            current_snapshot_path=snapshot_path,
        )

    async def start_resync(self, drawing_id: str) -> ResyncStartResult:
        lock_key     = f"lock:drawing:{drawing_id}:resync"
        resync_token = str(uuid4())
        acquired     = await self._redis.set(
            lock_key, resync_token, nx=True, ex=RESYNC_LOCK_TTL
        )
        if not acquired:
            return ResyncStartResult(acquired=False, resync_token=None)

        pipe = self._redis.pipeline(transaction=True)
        now = _utcnow_iso()
        pipe.set(f"drawing:{drawing_id}:status",        "FULL_RESYNC_RUNNING", ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:changeMode",    "FULL_RESYNC_RUNNING", ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastUpdatedAt", now,                   ex=STATUS_TTL)
        await pipe.execute()
        return ResyncStartResult(acquired=True, resync_token=resync_token)

    async def release_resync_lock(self, drawing_id: str, token: str) -> None:
        """token 일치 확인 후 lock 삭제. decode_responses=True이므로 bytes decode 불필요."""
        stored = await self._redis.get(f"lock:drawing:{drawing_id}:resync")
        if stored == token:
            await self._redis.delete(f"lock:drawing:{drawing_id}:resync")

    async def mark_resync_failed(
        self, drawing_id: str, error: str, token: str
    ) -> None:
        """기존 currentSnapshotPath 유지. lock 해제 후 RESYNC_FAILED 표시."""
        stored = await self._redis.get(f"lock:drawing:{drawing_id}:resync")
        pipe = self._redis.pipeline(transaction=True)
        now = _utcnow_iso()
        pipe.set(f"drawing:{drawing_id}:status",        "RESYNC_FAILED",       ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:changeMode",    "FULL_RESYNC_PENDING", ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastError",     error[:500],           ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastUpdatedAt", now,                   ex=STATUS_TTL)
        if stored == token:
            pipe.delete(f"lock:drawing:{drawing_id}:resync")
        await pipe.execute()

    async def _assert_s3_object_exists(
        self, s3_key: str, expected_min_size: int = 1
    ) -> None:
        size = await asyncio.to_thread(self._s3.head_object_size, s3_key)
        if size is None:
            raise ValueError(f"S3 object not found: {s3_key}")
        if size < expected_min_size:
            raise ValueError(f"S3 object empty: {s3_key} (size={size})")

    async def presign_snapshot_upload(
        self, drawing_id: str, org_id: str
    ) -> PresignResult:
        org_id = (org_id or "unknown").strip() or "unknown"
        snapshot_id = f"snap_{uuid4().hex}"
        s3_key = (
            f"orgs/{org_id}/drawings/{drawing_id}"
            f"/snapshots/{snapshot_id}/entities.json"
        )
        s3_uri     = self._s3.s3_uri_from_key(s3_key)
        upload_url = self._s3.generate_presigned_put_url(s3_key, 900)
        await self._redis.set(
            f"drawing:{drawing_id}:pending_key:{snapshot_id}", s3_key, ex=PENDING_TTL
        )
        return PresignResult(
            resource_id=snapshot_id, s3_key=s3_key, s3_uri=s3_uri,
            upload_url=upload_url, expires_in=900,
        )

    async def presign_delta_upload(
        self, drawing_id: str, org_id: str
    ) -> PresignResult:
        org_id = (org_id or "unknown").strip() or "unknown"
        delta_id = f"delta_{uuid4().hex}"
        s3_key   = (
            f"orgs/{org_id}/drawings/{drawing_id}/deltas/{delta_id}.json"
        )
        s3_uri     = self._s3.s3_uri_from_key(s3_key)
        upload_url = self._s3.generate_presigned_put_url(s3_key, 900)
        await self._redis.set(
            f"drawing:{drawing_id}:pending_key:{delta_id}", s3_key, ex=PENDING_TTL
        )
        return PresignResult(
            resource_id=delta_id, s3_key=s3_key, s3_uri=s3_uri,
            upload_url=upload_url, expires_in=900,
        )

    async def commit_snapshot(
        self,
        drawing_id: str,
        snapshot_id: str,
        s3_key: str,
        s3_uri: str,
        entity_count: int,
        revision_hash: str,
        resync_token: str | None = None,
        session_id: str | None = None,
    ) -> None:
        # Issue 8: pending key 검증
        pending = await self._redis.get(
            f"drawing:{drawing_id}:pending_key:{snapshot_id}"
        )
        if pending is not None and pending != s3_key:
            raise ValueError(
                f"s3_key mismatch: expected {pending!r}, got {s3_key!r}"
            )
        if pending is not None:
            await self._redis.delete(
                f"drawing:{drawing_id}:pending_key:{snapshot_id}"
            )

        await self._assert_s3_object_exists(s3_key)

        pipe = self._redis.pipeline(transaction=True)
        now = _utcnow_iso()
        pipe.set(f"drawing:{drawing_id}:currentSnapshotId",        snapshot_id, ex=SNAPSHOT_TTL)
        pipe.set(f"drawing:{drawing_id}:currentSnapshotPath",      s3_uri,      ex=SNAPSHOT_TTL)
        pipe.set(f"drawing:{drawing_id}:status",                   "LATEST",    ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:changeMode",               "NONE",      ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:deltaCount",               "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:deltaSize",                "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:dirtyCount",               "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastSnapshotCommittedAt",  now,         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastUpdatedAt",            now,         ex=STATUS_TTL)
        pipe.delete(
            f"drawing:{drawing_id}:dirty:modified",
            f"drawing:{drawing_id}:dirty:appended",
            f"drawing:{drawing_id}:dirty:erased",
            f"drawing:{drawing_id}:dirty:layerAdded",
            f"drawing:{drawing_id}:dirty:layerDeleted",
            f"drawing:{drawing_id}:deltaPaths",
            f"drawing:{drawing_id}:latestDeltaPath",
            f"drawing:{drawing_id}:lastError",
        )
        # lock 해제: token 일치 시에만
        stored_token = await self._redis.get(f"lock:drawing:{drawing_id}:resync")
        if resync_token and stored_token == resync_token:
            pipe.delete(f"lock:drawing:{drawing_id}:resync")
        elif stored_token is not None and resync_token is None:
            # MVP fallback: token 없으면 무조건 삭제
            pipe.delete(f"lock:drawing:{drawing_id}:resync")
        await pipe.execute()

        log.info(
            "[DrawingStateService] snapshot committed drawing=%s snapshot=%s",
            drawing_id, snapshot_id,
        )

    async def commit_delta(
        self,
        drawing_id: str,
        delta_id: str,
        s3_key: str,
        s3_uri: str,
        delta_size: int,
        base_snapshot_id: str,
        changed_handles: ChangedHandles,
    ) -> None:
        current_snapshot_id = await self._redis.get(
            f"drawing:{drawing_id}:currentSnapshotId"
        )
        if current_snapshot_id and base_snapshot_id != current_snapshot_id:
            await self._redis.set(
                f"drawing:{drawing_id}:changeMode", "FULL_RESYNC_PENDING", ex=STATUS_TTL
            )
            raise ConflictError(
                f"Delta base {base_snapshot_id} != current {current_snapshot_id}"
            )

        # Issue 8: pending key 검증
        pending = await self._redis.get(
            f"drawing:{drawing_id}:pending_key:{delta_id}"
        )
        if pending is not None and pending != s3_key:
            raise ValueError(
                f"s3_key mismatch: expected {pending!r}, got {s3_key!r}"
            )
        if pending is not None:
            await self._redis.delete(
                f"drawing:{drawing_id}:pending_key:{delta_id}"
            )

        await self._assert_s3_object_exists(s3_key)

        print(
            f"[DELTA_COMMIT] drawing={drawing_id} delta_id={delta_id} s3={s3_uri} "
            f"modified={len(changed_handles.modified)} appended={len(changed_handles.appended)} "
            f"erased={len(changed_handles.erased)} layer_added={len(changed_handles.layer_added)} "
            f"layer_deleted={len(changed_handles.layer_deleted)}"
        )

        # delta list 추가 (RPUSH = 시간순)
        await self._redis.rpush(f"drawing:{drawing_id}:deltaPaths", s3_uri)
        await self._redis.expire(f"drawing:{drawing_id}:deltaPaths", STATUS_TTL)

        # 카운터 누적
        new_delta_count = await self._redis.incr(f"drawing:{drawing_id}:deltaCount")
        new_delta_size  = await self._redis.incrby(
            f"drawing:{drawing_id}:deltaSize", delta_size
        )
        await self._redis.expire(f"drawing:{drawing_id}:deltaCount", STATUS_TTL)
        await self._redis.expire(f"drawing:{drawing_id}:deltaSize",  STATUS_TTL)
        await self._redis.set(
            f"drawing:{drawing_id}:latestDeltaPath", s3_uri, ex=STATUS_TTL
        )

        # dirty set에서 커밋된 handle 제거
        handle_map = {
            "modified": changed_handles.modified,
            "appended": changed_handles.appended,
            "erased":   changed_handles.erased,
            "layerAdded": changed_handles.layer_added,
            "layerDeleted": changed_handles.layer_deleted,
        }
        for suffix, handles in handle_map.items():
            if handles:
                await self._redis.srem(
                    f"drawing:{drawing_id}:dirty:{suffix}", *handles
                )

        # 상태 결정
        dirty_count = await self._compute_dirty_count(drawing_id)
        status      = "REVIEW_REQUIRED" if dirty_count == 0 else "DIRTY"
        print(
            f"[DELTA_COMMIT] drawing={drawing_id} ✅ 저장 완료 "
            f"new_delta_count={new_delta_count} new_delta_size={new_delta_size} "
            f"dirty_count={dirty_count} status={status}"
        )

        # compaction 판단 (commit 직후 threshold 초과 시 FULL_RESYNC_PENDING)
        _, compaction_reason = self._decide_next_mode(
            dirty_count, new_delta_count, new_delta_size
        )
        if compaction_reason is not None:
            status      = "DIRTY"
            change_mode = "FULL_RESYNC_PENDING"
            log.info(
                "[DrawingStateService] compaction triggered drawing=%s reason=%s",
                drawing_id, compaction_reason,
            )
        else:
            change_mode = "INCREMENTAL_READY"

        now = _utcnow_iso()
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(f"drawing:{drawing_id}:status",              status,      ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:changeMode",          change_mode, ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:dirtyCount",          str(dirty_count), ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastDeltaCommittedAt", now,         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastUpdatedAt",        now,         ex=STATUS_TTL)
        await pipe.execute()

    @staticmethod
    def merge_snapshot_delta(snapshot: dict, delta: dict) -> dict:
        raw_entities = snapshot.get("entities") or snapshot.get("elements") or []
        entities: dict[str, dict] = {}
        for e in raw_entities:
            handle = (e.get("handle") or "").strip()
            if handle:
                entities[handle] = e

        layers: dict[str, dict] = {}
        for layer in snapshot.get("layers", []):
            name = (layer.get("name") or "").strip()
            if name:
                layers[name] = layer

        erased_set = {str(h) for h in delta.get("erased", []) if h}
        for handle in erased_set:
            entities.pop(handle, None)

        for entity in delta.get("appended", []):
            handle = (entity.get("handle") or "").strip()
            if handle and handle not in erased_set:
                entities[handle] = entity

        for entity in delta.get("modified", []):
            handle = (entity.get("handle") or "").strip()
            if handle and handle not in erased_set:
                entities[handle] = entity

        for name in delta.get("layer_deleted", []):
            layers.pop((name or "").strip(), None)

        for layer in delta.get("layer_added", []):
            name = (layer.get("name") or "").strip()
            if name:
                layers[name] = layer

        entity_list = list(entities.values())
        layer_list  = list(layers.values())
        return {
            **snapshot,
            "entities":     entity_list,
            "layers":       layer_list,
            "entity_count": len(entity_list),
            "layer_count":  len(layer_list),
        }

    @staticmethod
    def merge_snapshot_deltas(snapshot: dict, deltas: list[dict]) -> dict:
        result: dict = dict(snapshot)
        for delta in deltas:
            result = DrawingStateService.merge_snapshot_delta(result, delta)
        return result

    async def get_state(self, drawing_id: str) -> DrawingState:
        keys = [
            "status", "changeMode", "dirtyCount", "deltaCount", "deltaSize",
            "currentSnapshotId", "currentSnapshotPath", "latestDeltaPath",
            "pendingAfterResync", "lastUpdatedAt", "lastError",
        ]
        vals = await asyncio.gather(
            *[self._redis.get(f"drawing:{drawing_id}:{k}") for k in keys]
        )
        (status, change_mode, dirty_count, delta_count, delta_size,
         snap_id, snap_path, latest_delta, pending, last_updated, last_error) = vals

        has_snapshot = bool(snap_path)
        return DrawingState(
            drawing_id=drawing_id,
            status=status or "LATEST",
            change_mode=change_mode or "NONE",
            dirty_count=int(dirty_count or 0),
            delta_count=int(delta_count or 0),
            delta_size=int(delta_size or 0),
            current_snapshot_id=snap_id,
            current_snapshot_path=snap_path,
            latest_delta_path=latest_delta,
            pending_after_resync=bool(pending),
            can_review_stale_snapshot=has_snapshot and status == "RESYNC_FAILED",
            last_updated_at=last_updated,
            last_error=last_error,
        )

    async def load_merged_drawing(self, drawing_id: str) -> dict:
        state = await self.get_state(drawing_id)
        if not state.current_snapshot_path:
            raise ValueError(f"No snapshot for drawing {drawing_id}")

        snapshot = await self._s3.load_json(state.current_snapshot_path)

        delta_paths: list[str] = await self._redis.lrange(
            f"drawing:{drawing_id}:deltaPaths", 0, -1
        )
        snap_entity_count = len(snapshot.get("entities") or snapshot.get("elements") or [])
        if not delta_paths:
            print(f"[MERGE] drawing={drawing_id} delta 없음 → snapshot 단독 반환 entities={snap_entity_count}")
            return snapshot

        print(f"[MERGE] drawing={drawing_id} snapshot_entities={snap_entity_count} delta_count={len(delta_paths)} → merge 시작")
        deltas_raw = await asyncio.gather(
            *[self._s3.load_json(p) for p in delta_paths],
            return_exceptions=True,
        )
        load_errors = [d for d in deltas_raw if isinstance(d, BaseException)]
        if load_errors:
            raise load_errors[0]

        # base_snapshot_id가 현재 snapshot과 다른 stale delta는 제거
        valid_pairs = [
            (p, d) for p, d in zip(delta_paths, deltas_raw)
            if d.get("base_snapshot_id") == state.current_snapshot_id
        ]
        stale_count = len(delta_paths) - len(valid_pairs)
        if stale_count > 0:
            print(
                f"[MERGE] drawing={drawing_id} ⚠ stale delta {stale_count}개 제거 "
                f"(base_snapshot_id 불일치) valid={len(valid_pairs)}"
            )
            # deltaSize도 재계산 — stale 제거 후 inflate 방지 (compaction 임계치에 영향)
            # 이미 메모리에 로드된 valid_pairs의 직렬화 크기로 근사 계산
            import json as _json
            recalculated_size = sum(
                len(_json.dumps(d, ensure_ascii=False).encode()) for _, d in valid_pairs
            )
            pipe = self._redis.pipeline()
            delta_paths_key = f"drawing:{drawing_id}:deltaPaths"
            latest_delta_key = f"drawing:{drawing_id}:latestDeltaPath"
            pipe.delete(delta_paths_key)
            for p, _ in valid_pairs:
                pipe.rpush(delta_paths_key, p)
            if valid_pairs:
                pipe.set(latest_delta_key, valid_pairs[-1][0], ex=STATUS_TTL)
            else:
                pipe.delete(latest_delta_key)
            pipe.set(f"drawing:{drawing_id}:deltaCount", str(len(valid_pairs)), ex=STATUS_TTL)
            pipe.set(f"drawing:{drawing_id}:deltaSize", str(recalculated_size), ex=STATUS_TTL)
            await pipe.execute()

        if not valid_pairs:
            print(f"[MERGE] drawing={drawing_id} ⚠ 유효 delta 없음 → snapshot 단독 반환 entities={snap_entity_count}")
            return snapshot

        valid_deltas = [d for _, d in valid_pairs]
        merged = self.merge_snapshot_deltas(snapshot, valid_deltas)
        merged_entity_count = len(merged.get("entities") or merged.get("elements") or [])
        total_modified = sum(len(d.get("modified") or []) for d in valid_deltas)
        total_appended = sum(len(d.get("appended") or []) for d in valid_deltas)
        total_erased   = sum(len(d.get("erased")   or []) for d in valid_deltas)
        print(
            f"[MERGE] drawing={drawing_id} ✅ merge 완료 result_entities={merged_entity_count} "
            f"(modified={total_modified} appended={total_appended} erased={total_erased} across {len(valid_deltas)} deltas)"
        )
        return merged

    # ── file_fingerprint guard ─────────────────────────────────────────────────

    async def check_and_store_fingerprint(
        self, drawing_id: str, fingerprint: str
    ) -> str:
        """
        drawing_id별 파일 fingerprint를 확인하고 저장한다.
        반환값: "new" (최초 등록) | "match" (동일 파일) | "mismatch" (파일 변경)
        fingerprint는 C#에서 file_path + file_size + last_write_time 조합으로 생성.
        """
        key = f"drawing:{drawing_id}:fileFingerprint"
        stored = await self._redis.get(key)
        await self._redis.set(key, fingerprint, ex=SNAPSHOT_TTL)
        if stored is None:
            return "new"
        if stored == fingerprint:
            return "match"
        return "mismatch"

    async def clear_stale_caches(self, drawing_id: str) -> None:
        """
        fingerprint mismatch 시 이전 파일의 dirty set·delta 카운터를 정리한다.
        currentSnapshotPath 등 기존 스냅샷 포인터는 제거하지 않는다
        — 뒤따르는 register_initial_snapshot이 덮어쓰기 때문.
        """
        pipe = self._redis.pipeline(transaction=True)
        for suffix in ("modified", "appended", "erased", "layerAdded", "layerDeleted"):
            pipe.delete(f"drawing:{drawing_id}:dirty:{suffix}")
        pipe.delete(f"drawing:{drawing_id}:dirtyCount")
        pipe.delete(f"drawing:{drawing_id}:deltaPaths")
        pipe.delete(f"drawing:{drawing_id}:deltaCount")
        pipe.delete(f"drawing:{drawing_id}:deltaSize")
        pipe.delete(f"drawing:{drawing_id}:latestDeltaPath")
        await pipe.execute()
        log.info("[DrawingStateService] stale caches cleared drawing=%s", drawing_id)

    async def register_initial_snapshot(
        self,
        drawing_id: str,
        snapshot_id: str,
        s3_uri: str,
        entity_count: int,
        revision_hash: str,
        session_id: str | None = None,
    ) -> None:
        """
        /cad/analyze 완료 후 최초 snapshot 등록.
        Python이 직접 업로드한 파일이므로 S3 HEAD 검증 생략.
        legacy drawing_path:{session_id} 갱신은 cad_service 담당.
        """
        now = _utcnow_iso()
        pipe = self._redis.pipeline(transaction=True)
        pipe.set(f"drawing:{drawing_id}:currentSnapshotId",       snapshot_id, ex=SNAPSHOT_TTL)
        pipe.set(f"drawing:{drawing_id}:currentSnapshotPath",     s3_uri,      ex=SNAPSHOT_TTL)
        pipe.set(f"drawing:{drawing_id}:status",                  "LATEST",    ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:changeMode",              "NONE",      ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:deltaCount",              "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:deltaSize",               "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:dirtyCount",              "0",         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastSnapshotCommittedAt", now,         ex=STATUS_TTL)
        pipe.set(f"drawing:{drawing_id}:lastUpdatedAt",           now,         ex=STATUS_TTL)
        await pipe.execute()
        log.info(
            "[DrawingStateService] initial snapshot registered drawing=%s snapshot=%s",
            drawing_id, snapshot_id,
        )
