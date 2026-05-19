"""
File    : backend/api/routers/plugin_api.py
Author  : AutoGen
Create  : 2026-04-28
Description : 플러그인 버전 체크 및 바이너리 스트리밍 다운로드 API

    백엔드(EC2)가 S3에서 플러그인 zip을 읽어 클라이언트에게 바이트 스트림으로
    직접 전송하는 방식. 클라이언트는 S3에 직접 접근하지 않으므로 버킷을
    완전히 비공개(Private)로 유지 가능.

Endpoints:
    GET  /api/v1/plugin/version-check  — 최신 버전 정보 조회
    GET  /api/v1/plugin/download       — 플러그인 zip 바이너리 스트리밍
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.utils.s3_manager import S3Manager

logger = logging.getLogger(__name__)

router = APIRouter()

# ── S3 키 규칙 ──────────────────────────────────────────────────────────
PLUGIN_META_KEY = "plugin/latest.json"  # {"latest_version":"1.0.0","zip_key":"plugin/CadSllmAgent_v1.0.0.zip","release_notes":"...","file_size":123456}
STREAM_CHUNK_SIZE = 1024 * 256  # 256 KB per chunk


def _get_s3() -> S3Manager:
    return S3Manager()


# ── 1. 버전 체크 ─────────────────────────────────────────────────────────
@router.get("/version-check")
async def version_check():
    """S3의 plugin/latest.json을 읽어 최신 버전 정보를 반환한다."""
    try:
        s3 = _get_s3()
        meta = await s3.download_json_async(PLUGIN_META_KEY)
        logger.info("[Plugin] version-check")
        return {
            "latest_version": meta.get("latest_version", "0.0.0"),
            "release_notes": meta.get("release_notes", ""),
            "file_size": meta.get("file_size", 0),
        }
    except Exception as e:
        logger.error(f"[Plugin] version-check 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"버전 정보 조회 실패: {str(e)}"
        )


# ── 2. 바이너리 스트리밍 다운로드 ──────────────────────────────────────
@router.get("/download")
async def download_plugin():
    """S3에서 최신 플러그인 zip을 읽어 HTTP 응답 본문에 바이트 스트림으로 전송."""
    try:
        s3 = _get_s3()

        # 1) latest.json 에서 zip 키 가져오기
        meta = await s3.download_json_async(PLUGIN_META_KEY)
        zip_key = meta.get("zip_key")
        if not zip_key:
            raise HTTPException(status_code=404, detail="배포된 플러그인이 없습니다.")

        version = meta.get("latest_version", "unknown")
        file_size = meta.get("file_size", 0)

        # 2) S3에서 오브젝트를 가져오기 (동기 I/O → 스레드로 비동기 처리)
        def _get_s3_object():
            return s3.s3_client.get_object(
                Bucket=s3.bucket_name,
                Key=zip_key,
            )

        s3_obj = await asyncio.to_thread(_get_s3_object)
        body = s3_obj["Body"]
        content_length = s3_obj.get("ContentLength", file_size)

        def _stream_generator():
            """S3 Body를 청크 단위로 읽어 yield"""
            try:
                while True:
                    chunk = body.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        filename = f"CadSllmAgent_v{version}.zip"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Plugin-Version": version,
        }
        if content_length:
            headers["Content-Length"] = str(content_length)

        logger.info(f"[Plugin] download: {zip_key} ({content_length} bytes)")

        return StreamingResponse(
            _stream_generator(),
            media_type="application/zip",
            headers=headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Plugin] download 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"플러그인 다운로드 실패: {str(e)}"
        )

