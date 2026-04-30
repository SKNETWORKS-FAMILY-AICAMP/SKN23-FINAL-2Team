"""
File    : backend/services/cad_service.py
Author  : 김지우
Create  : 2026-04-11
Description : CAD 데이터 처리 서비스

Modification History :
    - 2026-04-22 (김지우) : S3 매니저와 통합하여 CAD 데이터 처리 서비스 수정
    - 2026-04-22 : 포커스(부분) 도면 — Redis에 정규화 JSON만 TTL 보관, 분석 완료 시 삭제
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from backend.core.redis_client import get_redis_client
from backend.utils.s3_manager import S3Manager

# 파이썬 기본 재귀 한도를 늘려 깊은 중첩 객체(수만 개 엔티티) 처리 시 RecursionError 방지
sys.setrecursionlimit(5000)

logger = logging.getLogger(__name__)

def safe_json_dumps(data: Any) -> str:
    """
    무한 재귀나 직렬화 불가 객체로 인한 500 에러를 방지하는 안전한 dump 함수
    """
    try:
        # 기본적으로 ensure_ascii=False로 용량을 줄이고, 파이썬 객체는 문자열로 강제 변환
        return json.dumps(data, ensure_ascii=False, default=str)
    except RecursionError:
        logger.error("[CadDataService] JSON 직렬화 중 RecursionError 발생! 데이터를 강제 축소합니다.")
        if isinstance(data, dict):
            safe_data = {k: v for k, v in data.items() if k != "entities"}
            safe_data["entities"] = "데이터가 너무 커서 직렬화에 실패했습니다."
            return json.dumps(safe_data, ensure_ascii=False, default=str)
        return "{}"
    except Exception as e:
        logger.error(f"[CadDataService] JSON 직렬화 실패: {e}")
        return "{}"

class CadDataService:
    PATH_TTL = 259200  # 3일 — Redis에는 S3 키(또는 s3:// URI)만 보관, 본문은 S3 (OOM 방지)
    FOCUS_TTL = 86400  # 1일 — focus/temp.json 형태 정규화 JSON (에이전트 분석 후 명시적 삭제)
    EXTRACTION_STAGING_TTL = 3600  # C# 청크 분할 업로드 병합용 (1시간)

    def __init__(self):
        self.redis = get_redis_client()
        self.s3_manager = S3Manager()

    def _s3_key_from_uri(self, s3_uri: str) -> str:
        if not s3_uri:
            return ""
        prefix = f"s3://{self.s3_manager.bucket_name}/"
        return s3_uri[len(prefix):] if s3_uri.startswith(prefix) else s3_uri

    async def cache_drawing_path(
        self,
        session_id: str,
        s3_path: str,
        *,
        revision_hash: str | None = None,
    ) -> None:
        """
        Redis: drawing_path:{session_id} → S3 object key 또는 s3://bucket/key 문자열.
        대용량 JSON은 넣지 않음.
        """
        if s3_path:
            await self.redis.setex(f"drawing_path:{session_id}", self.PATH_TTL, s3_path)
            meta = {
                "s3_path": s3_path,
                "revision_hash": revision_hash or "",
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "dirty": False,
            }
            await self.redis.setex(
                f"drawing_meta:{session_id}",
                self.PATH_TTL,
                safe_json_dumps(meta),
            )

    async def save_to_s3(self, session_id: str, drawing_data: dict, org_id: str = "unknown", device_id: str = "unknown") -> str:
        """도면 원본을 S3에 업로드하고 s3:// URI를 반환합니다."""
        try:
            s3_key = self.s3_manager.get_s3_key(
                data_type="raw",
                org_id=org_id,
                device_id=device_id,
                uuid=session_id,
            )
            await self.s3_manager.upload_json_async(s3_key, drawing_data, use_kms=False)
            s3_uri = f"s3://{self.s3_manager.bucket_name}/{s3_key}"
            logger.info(f"[CadDataService] 도면 S3 저장 완료: {s3_uri}")
            return s3_uri
        except Exception as e:
            logger.error(f"[CadDataService] S3 저장 실패: {str(e)}")
            return ""

    async def process_and_cache(self, session_id: str, drawing_data: dict, org_id: str = "", device_id: str = "") -> bool:
        """S3에 저장 후, 그 경로만 Redis에 기록합니다."""
        s3_path = await self.save_to_s3(session_id, drawing_data, org_id, device_id)
        if not s3_path:
            logger.error(f"[CadDataService] S3 경로를 얻지 못해 Redis 기록을 취소합니다. Session: {session_id}")
            return False
        await self.cache_drawing_path(session_id, s3_path)
        return True

    async def get_drawing_data(self, session_id: str) -> dict:
        """
        drawing_path → S3 GetObject. Redis에는 키만 있으므로 OOM 없이 본문은 항상 S3에서.
        """
        try:
            s3_uri = await self.redis.get(f"drawing_path:{session_id}")
            if s3_uri:
                s3_uri_str = s3_uri.decode() if isinstance(s3_uri, bytes) else s3_uri
                prefix = f"s3://{self.s3_manager.bucket_name}/"
                if s3_uri_str.startswith(prefix):
                    s3_key = s3_uri_str[len(prefix) :]
                else:
                    s3_key = s3_uri_str
                drawing_data = await self.s3_manager.download_json_async(s3_key)
                if drawing_data:
                    logger.info(f"[CadDataService] S3에서 도면 데이터 로드 성공: {s3_key}")
                    return drawing_data
        except Exception as e:
            logger.error(f"[CadDataService] 데이터 조회 실패 (Session: {session_id}): {e}")

        logger.warning(f"[CadDataService] Session ID '{session_id}'에 해당하는 도면 데이터가 없습니다.")
        return {}

    async def cache_focus_drawing(self, session_id: str, normalized_focus: dict[str, Any]) -> None:
        """
        normalize_drawing_data 결과와 동일한 스키마의 '부분 도면' JSON만 Redis에 저장.
        Key: drawing_focus:{session_id} — S3 full blob과 별도로 소량 스냅샷용.
        """
        if not session_id or not normalized_focus:
            return
        raw = safe_json_dumps(normalized_focus)
        await self.redis.setex(f"drawing_focus:{session_id}", self.FOCUS_TTL, raw)

    async def get_focus_drawing(self, session_id: str) -> dict[str, Any]:
        try:
            blob = await self.redis.get(f"drawing_focus:{session_id}")
            if not blob:
                return {}
            s = blob.decode() if isinstance(blob, (bytes, bytearray)) else str(blob)
            data = json.loads(s)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning("[CadDataService] focus JSON 로드 실패 session=%s: %s", session_id, e)
            return {}

    async def delete_focus_drawing(self, session_id: str) -> None:
        try:
            await self.redis.delete(f"drawing_focus:{session_id}")
        except Exception as e:
            logger.warning("[CadDataService] focus 키 삭제 실패 session=%s: %s", session_id, e)

    async def invalidate_derived_caches(self, session_id: str) -> None:
        """
        CAD 도면이 수정되거나 재추출될 때 이전 분석/매핑 파생 캐시를 무효화합니다.
        원본 drawing_path는 새 도면 업로드 전까지 마지막 스냅샷으로 남겨둡니다.
        """
        if not session_id:
            return
        keys = [
            f"obj_mapping:{session_id}",
            f"drawing_focus:{session_id}",
            f"cad_extraction_staging:{session_id}",
            f"cad_extraction_entities:{session_id}",
            f"cad_extraction_entities:{session_id}_focus",
        ]
        try:
            async for key in self.redis.scan_iter(match=f"obj_mapping:{session_id}:*"):
                keys.append(key)
            await self.redis.delete(*keys)
            logger.info("[CadDataService] derived caches invalidated session=%s keys=%d", session_id, len(keys))
        except Exception as e:
            logger.warning("[CadDataService] derived cache invalidation failed session=%s: %s", session_id, e)

    async def delete_drawing_cache(self, session_id: str, *, delete_s3: bool = False) -> dict[str, Any]:
        """
        사용자가 CAD를 직접 수정한 경우 이전 도면 스냅샷과 파생 분석 캐시를 폐기합니다.
        delete_s3=True이면 Redis drawing_path가 가리키던 S3 원본도 함께 삭제합니다.
        """
        if not session_id:
            return {"deleted_keys": 0, "deleted_s3": False}

        s3_uri = None
        deleted_s3 = False
        try:
            raw = await self.redis.get(f"drawing_path:{session_id}")
            s3_uri = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        except Exception as exc:
            logger.warning("[CadDataService] drawing_path read failed session=%s: %s", session_id, exc)

        await self.invalidate_derived_caches(session_id)
        keys: list[Any] = [
            f"drawing_path:{session_id}",
            f"drawing_meta:{session_id}",
        ]
        try:
            async for key in self.redis.scan_iter(match=f"agent_cache:{session_id}:*"):
                keys.append(key)
            deleted_count = await self.redis.delete(*keys)
        except Exception as exc:
            deleted_count = 0
            logger.warning("[CadDataService] drawing cache delete failed session=%s: %s", session_id, exc)

        if delete_s3 and s3_uri:
            try:
                s3_key = self._s3_key_from_uri(str(s3_uri))
                await self.s3_manager.delete_object_async(s3_key)
                deleted_s3 = True
            except Exception as exc:
                logger.warning("[CadDataService] S3 stale drawing delete failed session=%s: %s", session_id, exc)

        return {"deleted_keys": int(deleted_count or 0), "deleted_s3": deleted_s3}

    async def set_extraction_staging(self, session_id: str, data: dict[str, Any]) -> None:
        """
        청크 병합 메타데이터 저장 (entities 배열 제외 — 별도 list key에 RPUSH).
        merged_cad 의 entities 키는 제거하고 merged_cad_meta 로 저장합니다.
        """
        if not session_id or not data:
            return
        meta: dict[str, Any] = {}
        for k, v in data.items():
            if k == "merged_cad" and isinstance(v, dict):
                meta["merged_cad_meta"] = {ek: ev for ek, ev in v.items() if ek != "entities"}
            else:
                meta[k] = v
        key = f"cad_extraction_staging:{session_id}"
        try:
            await self.redis.setex(
                key, self.EXTRACTION_STAGING_TTL, safe_json_dumps(meta)
            )
        except Exception as e:
            logger.error("[CadDataService] extraction staging 저장 실패 session=%s: %s", session_id, e)
            raise

    async def push_extraction_chunk_entities(self, session_id: str, entities: list) -> None:
        """청크 엔티티를 Redis list 에 RPUSH — 50개 배치로 나눠 전송해 Windows 소켓 버퍼 초과 방지."""
        if not session_id or not entities:
            return
        key = f"cad_extraction_entities:{session_id}"
        _BATCH = 50
        try:
            for i in range(0, len(entities), _BATCH):
                batch = entities[i : i + _BATCH]
                pipe = self.redis.pipeline(transaction=False)
                for ent in batch:
                    pipe.rpush(key, safe_json_dumps(ent))
                pipe.expire(key, self.EXTRACTION_STAGING_TTL)
                await pipe.execute()
        except Exception as e:
            logger.error("[CadDataService] entity chunk RPUSH 실패 session=%s: %s", session_id, e)
            raise

    async def pop_all_extraction_entities(self, session_id: str) -> list:
        """Redis list 에서 누적된 모든 엔티티를 읽어 반환."""
        key = f"cad_extraction_entities:{session_id}"
        try:
            raws = await self.redis.lrange(key, 0, -1)
            return [json.loads(r) for r in raws]
        except Exception as e:
            logger.warning("[CadDataService] entity list 조회 실패 session=%s: %s", session_id, e)
            return []

    async def get_extraction_staging(self, session_id: str) -> dict[str, Any] | None:
        key = f"cad_extraction_staging:{session_id}"
        try:
            raw = await self.redis.get(key)
            if not raw:
                return None
            s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            o = json.loads(s)
            return o if isinstance(o, dict) else None
        except Exception as e:
            logger.warning("[CadDataService] extraction staging 조회 실패 session=%s: %s", session_id, e)
            return None

    async def clear_extraction_staging(self, session_id: str) -> None:
        try:
            await self.redis.delete(
                f"cad_extraction_staging:{session_id}",
                f"cad_extraction_entities:{session_id}",
            )
        except Exception as e:
            logger.warning(
                "[CadDataService] extraction staging 삭제 실패 session=%s: %s", session_id, e
            )

cad_service = CadDataService()
