"""
File    : backend/api/routers/cad_interop.py
Author  : 김지우
Create  : 2026-04-11
Description : C# AutoCAD 플러그인과 직접 통신하는 REST API 라우터 (데이터 수신 및 저장 전용)

Modification History :
    - 2026-04-06 (김지우) : C# 연동 초기 구조 생성
    - 2026-04-13 (양창일) : session 기반 CAD 데이터 처리 로직 추가 및 FT-01 로직 통합
    - 2026-04-15 (양창일) : TEXT 메모리 구조에 맞게 CAD 데이터 영속 저장 전제를 제거
    - 2026-04-15 (김다빈) : CAD_DATA_READY WebSocket 추가
    - 2026-04-16 (김지우) : C# AutoCAD 플러그인과 통신하여 데이터를 받고 하이브리드 저장소 캐싱
    - 2026-04-17 (김지우) : AI 분석 로직 제거 (UI의 WebSocket 트리거 방식으로 흐름 일원화)
    - 2026-04-18 (김지우) : active_object_ids 추출 및 WebSocket 페이로드에 포함
    - 2026-04-20 (김지우) : 매핑 디버그 API 추가 (GET /debug/mapping/{session_id})
    - 2026-04-22 : extraction_chunk 분할 업로드 — Redis 누적, 마지막 청크에서 S3·CAD_DATA_READY
"""

import json
import logging
import time
import uuid
import hashlib
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sa_text, select

from backend.core.database import get_db
from backend.core.socket_manager import manager
from backend.services.payload_service import (
    CONTEXT_MODE_FULL,
    CONTEXT_MODE_FULL_WITH_FOCUS,
    normalize_drawing_data,
    recompute_layer_entity_counts,
)
from backend.services.cad_service import cad_service
from backend.services.cad_progress import emit_pipeline_step
from backend.services.agents.pipe.sub.mapping import (
    compute_unmapped_layer_names,
    invalidate_mapping_cache,
)
from backend.models.schema import MappingRule, Organization, StandardTerm

router = APIRouter()


class SelectionBroadcastIn(BaseModel):
    """CAD에서 선택이 바뀔 때마다 UI의 activeObjectIds를 동기화."""

    active_object_ids: list[str] = Field(default_factory=list)


@router.post("/selection")
async def broadcast_cad_selection(body: SelectionBroadcastIn):
    """C# ImpliedSelectionChanged → React `CAD_SELECTION_CHANGED` (선택 해제 시 빈 배열)."""
    await manager.send_to_group(
        {
            "action": "CAD_SELECTION_CHANGED",
            "payload": {"active_object_ids": body.active_object_ids},
        },
        "ui",
    )
    return {"status": "success", "count": len(body.active_object_ids)}


class LayerMapItemIn(BaseModel):
    source_key: str = Field(..., min_length=1, max_length=200, description="레이어(또는 매핑할) 이름")
    standard_term_id: str = Field(..., min_length=1)
    layer_role: str | None = Field(
        default=None,
        description="arch | mep | aux (선택) — 휴리스틱 대신 DB 오버라이드",
    )


class LayerMapBatchIn(BaseModel):
    org_id: str
    domain: str = "pipe"
    mappings: list[LayerMapItemIn] = Field(..., min_length=1, max_length=200)


@router.post("/mapping-rules/layer-batch")
async def save_layer_mapping_batch(
    body: LayerMapBatchIn,
    db: AsyncSession = Depends(get_db),
):
    """
    미매핑 레이어에 대해 org별 LAYER 규칙을 `mapping_rules`에 등록(동일 키면 갱신)합니다.
    `standard_terms`에 존재하는 용어 id만 허용합니다.
    """
    org_row = await db.execute(select(Organization).where(Organization.id == body.org_id))
    if org_row.scalars().first() is None:
        raise HTTPException(status_code=404, detail="organization not found")
    d = (body.domain or "pipe").strip()
    saved = 0
    for it in body.mappings:
        sk = (it.source_key or "").strip()
        if not sk:
            continue
        t_row = await db.execute(
            select(StandardTerm).where(
                StandardTerm.id == it.standard_term_id.strip(),
                StandardTerm.domain == d,
                StandardTerm.is_active == True,  # noqa: E712
            )
        )
        st = t_row.scalars().first()
        if st is None:
            raise HTTPException(
                status_code=400,
                detail=f"standard_term not found or domain mismatch: {it.standard_term_id!r} (domain={d})",
            )
        r_row = await db.execute(
            select(MappingRule).where(
                MappingRule.org_id == body.org_id,
                MappingRule.domain == d,
                MappingRule.rule_type == "LAYER",
                MappingRule.source_key == sk,
            )
        )
        ex = r_row.scalars().first()
        lr = (it.layer_role or "").strip().lower() if getattr(it, "layer_role", None) else None
        if lr and lr not in ("arch", "mep", "aux"):
            lr = None
        if ex:
            ex.standard_term_id = st.id
            ex.is_active = True
            if lr is not None:
                ex.layer_role = lr
        else:
            db.add(
                MappingRule(
                    id=str(uuid.uuid4()),
                    org_id=body.org_id,
                    domain=d,
                    rule_type="LAYER",
                    source_key=sk,
                    standard_term_id=st.id,
                    is_active=True,
                    layer_role=lr,
                )
            )
        saved += 1
    await db.commit()
    invalidate_mapping_cache(str(body.org_id))
    return {"status": "success", "saved": saved, "org_id": body.org_id, "domain": d}


async def _resolve_org_device(
    db: AsyncSession, machine_id: str
) -> tuple[str, str]:
    """machine_id로 devices → licenses → organizations 조인하여 org_id, device_id 반환."""
    result = await db.execute(
        sa_text(
            "SELECT CAST(d.id AS text) AS device_id, "
            "       CAST(l.org_id AS text) AS org_id "
            "FROM devices d "
            "JOIN licenses l ON l.id = d.license_id "
            "WHERE d.machine_id = :mid AND d.is_active = true "
            "ORDER BY d.last_seen DESC NULLS LAST "
            "LIMIT 1"
        ),
        {"mid": machine_id},
    )
    row = result.mappings().first()
    if row:
        return str(row["org_id"]), str(row["device_id"])
    return "", ""

@router.post("/analyze")
async def analyze_cad(
    payload: dict,
    db: AsyncSession = Depends(get_db),
):
    """
    C# 플러그인으로부터 도면 데이터를 수신하는 메인 엔드포인트.
    수신 데이터는 S3에 저장한 뒤 Redis에는 경로(포인터)만 두고, UI에 완료 알림을 보냅니다.
    """
    action = payload.get("action")
    raw_payload = payload.get("payload") or {}
    cad_data = raw_payload.get("drawing_data") or raw_payload
    focus_raw = raw_payload.get("focus_drawing_data")
    active_object_ids = raw_payload.get("active_object_ids", [])
    session_id = payload.get("session_id")
    # C# ApiClient: review_tool(신규) / piping_tool(구형 호환), domain_type, dwg_compare_mode (추적용)
    _pt = raw_payload.get("review_tool") or raw_payload.get("piping_tool")
    _dc = raw_payload.get("dwg_compare_mode")
    _dt = raw_payload.get("domain_type")
    if _pt or _dc or _dt:
        logging.info(
            "[CAD Interop][client] review_tool=%s domain_type=%s dwg_compare_mode=%s session_id=%s",
            _pt,
            _dt,
            _dc,
            session_id,
        )

    # 1. 유효성 검사
    if action != "CAD_DATA_EXTRACTED" or not cad_data:
        logging.warning(f"[CAD Interop] 잘못된 요청 수신: session_id={session_id}")
        raise HTTPException(status_code=400, detail="Invalid CAD payload.")

    try:
        t0m = time.monotonic()
        w0 = time.time()
        lt = t0m
        sid = str(session_id) if session_id else ""
        lt = await emit_pipeline_step(
            session_id=sid or None,
            stage="cad_validated",
            message="도면 JSON 수신·검증 완료",
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=lt,
        )
        # 1b) C# 분할 전송(entities 청크) — Redis에 누적, 마지막 청크에서 S3+WS
        extraction_chunk = raw_payload.get("extraction_chunk")
        if isinstance(extraction_chunk, dict) and extraction_chunk.get("count") is not None:
            c_t = int(extraction_chunk.get("count") or 0)
            i_x = int(
                extraction_chunk.get("index")
                if extraction_chunk.get("index") is not None
                else extraction_chunk.get("Index", 0)
            )
            if c_t < 1 or i_x < 0 or i_x >= c_t:
                raise HTTPException(
                    status_code=400, detail="Invalid extraction_chunk index/count",
                )
            if not isinstance(cad_data, dict):
                raise HTTPException(
                    status_code=400, detail="Invalid drawing_data for extraction chunk.",
                )
            if i_x == 0:
                # 첫 청크: entities 제외한 메타데이터만 staging에 저장
                meta_cad = {k: v for k, v in cad_data.items() if k != "entities"}
                # focus_drawing_data도 entities를 분리 — 통째로 SETEX 하면 소켓 버퍼 초과
                focus_raw_0 = raw_payload.get("focus_drawing_data")
                focus_meta_0 = None
                if isinstance(focus_raw_0, dict):
                    focus_ents_0 = focus_raw_0.get("entities") or []
                    focus_meta_0 = {k: v for k, v in focus_raw_0.items() if k != "entities"}
                    if focus_ents_0:
                        await cad_service.push_extraction_chunk_entities(
                            f"{session_id}_focus", focus_ents_0
                        )
                st0 = {
                    "merged_cad": meta_cad,
                    "staged_focus_meta": focus_meta_0,
                }
                await cad_service.set_extraction_staging(str(session_id), st0)
            else:
                # 중간 청크: staging 존재 여부만 확인 (entities는 list에만 추가)
                st_check = await cad_service.get_extraction_staging(str(session_id))
                if not st_check:
                    raise HTTPException(
                        status_code=400,
                        detail="청크 순서 오류(이전 구간 누락). 동일 session_id로 순차 전송하세요.",
                    )

            # 모든 청크: entities를 Redis list에 RPUSH (단일 값이 커지지 않음)
            await cad_service.push_extraction_chunk_entities(
                str(session_id), cad_data.get("entities") or []
            )

            if i_x < c_t - 1:
                logging.info(
                    "[CAD Interop] extraction chunk %s/%s staged (S3·CAD_DATA_READY는 마지막) session=%s",
                    i_x + 1,
                    c_t,
                    session_id,
                )
                lt = await emit_pipeline_step(
                    session_id=sid or None,
                    stage="cad_chunk_staged",
                    message=f"엔티티 청크 {i_x + 1}/{c_t} 수신(스테이징)",
                    t0_monotonic=t0m,
                    wall_start_ts=w0,
                    last_t=lt,
                )
                return {
                    "status": "chunk_staged",
                    "session_id": session_id,
                    "chunk_index": i_x,
                    "chunk_count": c_t,
                }

            # 마지막 청크: 메타데이터 + 누적 엔티티 list 병합 (Python 메모리에서)
            st_final = await cad_service.get_extraction_staging(str(session_id))
            all_entities = await cad_service.pop_all_extraction_entities(str(session_id))
            await cad_service.clear_extraction_staging(str(session_id))
            if not st_final:
                raise HTTPException(
                    status_code=500,
                    detail="청크 병합 실패(스테이징 없음).",
                )
            merged_meta = st_final.get("merged_cad_meta") or st_final.get("merged_cad") or {}
            merged_meta["entities"] = all_entities
            cad_data = merged_meta
            recompute_layer_entity_counts(cad_data)
            # focus 복원: meta(소량) + entities list(배치 저장된 것) 재결합
            focus_meta_saved = st_final.get("staged_focus_meta")
            focus_ents_saved = await cad_service.pop_all_extraction_entities(f"{session_id}_focus")
            try:
                await cad_service.redis.delete(f"cad_extraction_entities:{session_id}_focus")
            except Exception:
                pass
            if focus_meta_saved is not None:
                focus_raw = {**focus_meta_saved, "entities": focus_ents_saved}
            else:
                focus_raw = st_final.get("staged_focus")  # 구형 호환
            logging.info(
                "[CAD Interop] extraction chunks merged n_entities=%s session=%s",
                len(cad_data.get("entities") or []),
                session_id,
            )
            lt = await emit_pipeline_step(
                session_id=sid or None,
                stage="cad_extraction_merged",
                message=f"청크 병합 완료 (엔티티 {len(cad_data.get('entities') or [])}개)",
                t0_monotonic=t0m,
                wall_start_ts=w0,
                last_t=lt,
            )

        # 2. DWG JSON: 전체 정규화 → (선택 시) 부분 정규화 후 병합 — 에이전트는 S3 본문에서 둘 다 사용
        drawing_data = normalize_drawing_data({"cad_data": cad_data})
        entity_count = drawing_data.get("entity_count", 0)
        lt = await emit_pipeline_step(
            session_id=sid or None,
            stage="cad_normalize",
            message=f"도면 JSON 정규화 완료 (엔티티 {entity_count}개)",
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=lt,
        )
        logging.info(
            "[CAD Interop][DWG] step1_full_normalized entities=%s session=%s",
            entity_count,
            session_id,
        )

        # 전체 도면 + 선택 구간 스냅샷 동시 전송 시: LLM이 전체 맥락과 관심 영역을 비교·검토
        if focus_raw:
            focus_norm = normalize_drawing_data({"cad_data": focus_raw})
            drawing_data["focus_extraction"] = focus_norm
            drawing_data["context_mode"] = CONTEXT_MODE_FULL_WITH_FOCUS
            fe_cnt = focus_norm.get("entity_count", 0)
            await cad_service.cache_focus_drawing(str(session_id), focus_norm)
            logging.info(
                "[CAD Interop][DWG] step2_focus_normalized focus_entities=%s (vs full=%s) active_handles=%s session=%s",
                fe_cnt,
                entity_count,
                len(active_object_ids),
                session_id,
            )
            logging.info(
                "[CAD Interop] full_with_focus: 전체 엔티티=%s, "
                "focus 엔티티=%s, active_handles=%s",
                entity_count,
                fe_cnt,
                len(active_object_ids),
            )
        else:
            drawing_data["context_mode"] = CONTEXT_MODE_FULL
            logging.info("[CAD Interop][DWG] step2_no_focus context=full_only session=%s", session_id)

        # 3. OOM 방지: Redis에는 S3 키(또는 s3:// URI)만 — 본문 JSON은 S3 단일 소스.
        #    에이전트가 get_drawing_data로 읽을 때 S3에 객체가 있어야 하므로 업로드를 먼저 await.
        org_id    = raw_payload.get("org_id", "")
        device_id = raw_payload.get("device_id", "")
        machine_id = raw_payload.get("machine_id", "")

        if (not org_id or not device_id) and machine_id:
            logging.info(f"[CAD Interop] org_id/device_id 누락 — machine_id='{machine_id}'로 DB 조회")
            org_id, device_id = await _resolve_org_device(db, machine_id)

        logging.info(f"[CAD Interop] org_id='{org_id}', device_id='{device_id}' (machine_id='{machine_id}')")

        o = (org_id or "unknown").strip() or "unknown"
        d = (device_id or "unknown").strip() or "unknown"
        revision_hash = hashlib.sha256(
            json.dumps(drawing_data, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        s3_uri = await cad_service.save_to_s3(str(session_id), drawing_data, o, d)
        if not s3_uri:
            raise HTTPException(
                status_code=502,
                detail="S3에 도면을 저장하지 못했습니다. 자격 증명·버킷·네트워크를 확인하세요.",
            )
        await cad_service.cache_drawing_path(
            str(session_id),
            s3_uri,
            revision_hash=revision_hash,
        )
        # 도면이 갱신됐으므로 이전 위치 매핑 캐시 무효화 (수정된 도면에 구 캐시 적용 방지)
        await cad_service.invalidate_derived_caches(str(session_id))
        try:
            await db.execute(
                sa_text(
                    "DELETE FROM review_results "
                    "WHERE session_id = :session_id "
                    "AND status IN ('PENDING', 'CONFIRMED', 'FAILED')"
                ),
                {"session_id": str(session_id)},
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logging.warning("[CAD Interop] pending review cache invalidation failed: %s", exc)
        lt = await emit_pipeline_step(
            session_id=sid or None,
            stage="cad_s3_upload",
            message="S3 저장·Redis 캐시 경로 반영",
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=lt,
        )

        _dt_l = (str(_dt or "pipe")).lower()
        if _dt_l in ("pipe", ""):
            unmapped_layers = compute_unmapped_layer_names(
                drawing_data, o if o != "unknown" else ""
            )
        else:
            unmapped_layers = []
        if unmapped_layers:
            logging.info(
                "[CAD Interop] unmapped_layers count=%s sample=%s",
                len(unmapped_layers),
                unmapped_layers[:5],
            )

        # 4. React UI에 도면 수신 완료 알림 전송 (WebSocket)
        # UI는 이 신호를 받고 '분석 시작' 버튼을 활성화하거나 사용자에게 알립니다.
        ws_payload: dict = {
            "active_object_ids": active_object_ids,
            "context_mode": drawing_data.get("context_mode", CONTEXT_MODE_FULL),
            "unmapped_layers": unmapped_layers,
            "org_id": o,
            "drawing_revision": revision_hash,
        }
        fe = drawing_data.get("focus_extraction")
        if isinstance(fe, dict):
            ws_payload["focus_entity_count"] = fe.get("entity_count", 0)

        await manager.send_to_group(
            {
                "action": "CAD_DATA_READY",
                "session_id": session_id,
                "entity_count": entity_count,
                "payload": ws_payload,
            },
            "ui",
        )
        lt = await emit_pipeline_step(
            session_id=sid or None,
            stage="cad_data_ready",
            message="UI에 CAD_DATA_READY 전송(도면 사용 가능)",
            t0_monotonic=t0m,
            wall_start_ts=w0,
            last_t=lt,
        )

        logging.info(f"[CAD Interop] 데이터 처리 성공: session_id={session_id}, entities={entity_count}")

        return {
            "status": "success",
            "session_id": session_id,
            "message": "CAD data received and storage process started."
        }

    except Exception as e:
        logging.error(f"[CAD Interop] 데이터 처리 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error during data processing.")


# ── 매핑 디버그 API ─────────────────────────────────────────────────────────────

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sa_text
from backend.core.database import get_db
from fastapi import Depends, Query


@router.get("/debug/standard-terms")
async def debug_standard_terms(
    domain: str = Query(default="pipe", description="도메인 (pipe/elec/arch/fire)"),
    db: AsyncSession = Depends(get_db),
):
    """
    standard_terms 테이블의 등록된 표준 용어를 조회합니다.
    매핑 파이프라인이 사용하는 SSOT(단일 진실 공급원) 확인용.
    """
    result = await db.execute(
        sa_text("""
            SELECT id, domain, category, standard_name, aliases,
                   unit_type, legal_reference, is_active
            FROM standard_terms
            WHERE domain = :domain AND is_active = true
            ORDER BY category, standard_name
        """),
        {"domain": domain},
    )
    rows = result.mappings().all()
    return {
        "domain": domain,
        "total": len(rows),
        "terms": [
            {
                "id": str(r["id"]),
                "category": r["category"],
                "standard_name": r["standard_name"],
                "aliases": r["aliases"],
                "unit_type": r["unit_type"],
                "legal_reference": r["legal_reference"],
            }
            for r in rows
        ],
    }


@router.get("/debug/mapping-rules")
async def debug_mapping_rules(
    domain: str = Query(default="pipe"),
    org_id: str = Query(default="", description="조직 ID (비어있으면 전체 조회)"),
    db: AsyncSession = Depends(get_db),
):
    """
    mapping_rules + standard_terms JOIN 조회.
    MappingAgent가 사용하는 DB 매핑 규칙의 실제 상태를 확인합니다.
    """
    if org_id:
        result = await db.execute(
            sa_text("""
                SELECT mr.id, mr.org_id, mr.domain, mr.rule_type,
                       mr.source_key, mr.style_config, mr.is_active, mr.layer_role,
                       st.standard_name, st.category, st.aliases
                FROM mapping_rules mr
                JOIN standard_terms st ON mr.standard_term_id = st.id
                WHERE mr.domain = :domain AND mr.org_id = :org_id AND mr.is_active = true
                ORDER BY mr.rule_type, mr.source_key
            """),
            {"domain": domain, "org_id": org_id},
        )
    else:
        result = await db.execute(
            sa_text("""
                SELECT mr.id, mr.org_id, mr.domain, mr.rule_type,
                       mr.source_key, mr.style_config, mr.is_active, mr.layer_role,
                       st.standard_name, st.category, st.aliases
                FROM mapping_rules mr
                JOIN standard_terms st ON mr.standard_term_id = st.id
                WHERE mr.domain = :domain AND mr.is_active = true
                ORDER BY mr.org_id, mr.rule_type, mr.source_key
            """),
            {"domain": domain},
        )
    rows = result.mappings().all()

    by_type: dict[str, int] = {}
    for r in rows:
        rt = r["rule_type"] or "PREFIX"
        by_type[rt] = by_type.get(rt, 0) + 1

    return {
        "domain": domain,
        "org_id": org_id or "(전체)",
        "total": len(rows),
        "by_rule_type": by_type,
        "rules": [
            {
                "id": str(r["id"]),
                "org_id": str(r["org_id"]),
                "rule_type": r["rule_type"],
                "source_key": r["source_key"],
                "layer_role": r["layer_role"],
                "standard_name": r["standard_name"],
                "category": r["category"],
                "style_config": r["style_config"],
            }
            for r in rows
        ],
    }


@router.get("/debug/mapping-test")
async def debug_mapping_test(
    org_id: str = Query(default="", description="조직 ID"),
    db: AsyncSession = Depends(get_db),
):
    """
    현재 org_id의 매핑 룰을 로드하여 MappingAgent 내부 상태를 확인합니다.
    실제 에이전트가 사용하는 것과 동일한 경로로 룰을 로드합니다.
    """
    from backend.services.agents.pipe.sub.mapping import MappingAgent

    mapper = MappingAgent(org_id=org_id or None, db=db)

    db_rules = [
        {
            "source_key": r.source_key,
            "standard_name": r.standard_name,
            "rule_type": r.rule_type,
            "style_config": r.style_config,
            "layer_role": r.layer_role,
        }
        for r in mapper._db_rules
    ]

    return {
        "org_id": org_id or "(없음)",
        "db_rules_loaded": len(mapper._db_rules),
        "db_rules": db_rules,
        "prefix_map_keys": list(mapper._prefix_map.keys()),
        "exact_map_keys": list(mapper._exact_map.keys()),
        "entity_type_map_keys": list(mapper._entity_type_map.keys()),
        "final_term_map": mapper.term_map,
        "final_entity_type_map": mapper.entity_type_map,
    }


@router.get("/debug/drawing-stats/{session_id}")
async def debug_drawing_stats(session_id: str):
    """
    캐시된 도면 데이터의 엔티티 통계를 조회합니다 (Redis 또는 메모리 폴백).
    """
    drawing = await cad_service.get_drawing_data(session_id)
    if not drawing:
        raise HTTPException(
            status_code=404,
            detail=f"세션 '{session_id}'의 도면 데이터가 캐시에 없습니다.",
        )
    entities = drawing.get("entities") or drawing.get("elements") or []

    type_counts: dict[str, int] = {}
    layer_counts: dict[str, int] = {}
    for ent in entities:
        t = ent.get("type", "UNKNOWN")
        lyr = ent.get("layer", "UNKNOWN")
        type_counts[t] = type_counts.get(t, 0) + 1
        layer_counts[lyr] = layer_counts.get(lyr, 0) + 1

    # 블록명 수집
    block_names: dict[str, int] = {}
    for ent in entities:
        bn = ent.get("block_name") or ent.get("name", "")
        if ent.get("type") in ("INSERT", "BLOCK") and bn:
            block_names[bn] = block_names.get(bn, 0) + 1

    return {
        "session_id": session_id,
        "total_entities": len(entities),
        "by_type": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        "by_layer": dict(sorted(layer_counts.items(), key=lambda x: -x[1])),
        "block_names": dict(sorted(block_names.items(), key=lambda x: -x[1])),
        "layers_list": sorted(layer_counts.keys()),
        "sample_entities": entities[:5],
    }
