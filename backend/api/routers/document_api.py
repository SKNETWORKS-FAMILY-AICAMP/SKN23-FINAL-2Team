"""
File    : backend/api/routers/document_api.py
Author  : 김지우
Description : 임시 시방서 업로드 / 목록 / 삭제 / 기간연장 / 청크 조회 API

Modification History :
    - 2026-04-21 : API Key(org) 검증, GET temp/{id}/chunks
"""

import asyncio
import datetime
import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps.license_auth import get_authenticated_org_id, require_same_org
from backend.core.config import settings
from backend.core.database import get_db
from backend.services import document_service
from backend.utils.s3_manager import S3Manager

router = APIRouter()
s3_manager = S3Manager()
logger = logging.getLogger(__name__)


class ProjectSpecLinksRequest(BaseModel):
    org_id: str
    temp_document_ids: list[str] = Field(default_factory=list)


def _runpod_worker_error_message(result: dict) -> str | None:
    """RunPod runsync 본문에서 사용자/로그용 짧은 사유 추출."""
    err = result.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()[:800]
    out = result.get("output")
    if isinstance(out, dict):
        for key in ("error", "message", "detail", "stderr"):
            v = out.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:800]
            if isinstance(v, list) and v and isinstance(v[0], str):
                return v[0].strip()[:800]
    if isinstance(out, str) and out.strip():
        return out.strip()[:800]
    return None


def _runpod_completed_output_error_message(output: dict) -> str | None:
    """RunPod outer status=COMPLETED여도 worker 내부 실패/무청크 결과면 사유 반환."""
    if not isinstance(output, dict):
        return "RunPod 워커 output이 올바른 dict 형식이 아닙니다."

    worker_status = str(output.get("status") or "").strip().lower()
    if worker_status and worker_status != "success":
        return _runpod_worker_error_message({"output": output}) or f"RunPod 워커 status={worker_status}"

    processed_chunks = output.get("processed_chunks")
    if processed_chunks is None:
        return "RunPod 워커 output에 processed_chunks가 없습니다."
    try:
        if int(processed_chunks) <= 0:
            return f"RunPod 워커가 청크를 생성하지 않았습니다. processed_chunks={processed_chunks}"
    except (TypeError, ValueError):
        return f"RunPod 워커 output의 processed_chunks가 숫자가 아닙니다: {processed_chunks}"

    inserted_chunks = output.get("db_inserted_chunks")
    if inserted_chunks is not None:
        try:
            if int(inserted_chunks) <= 0:
                return f"RunPod 워커가 DB에 청크를 저장하지 않았습니다. db_inserted_chunks={inserted_chunks}"
        except (TypeError, ValueError):
            return f"RunPod 워커 output의 db_inserted_chunks가 숫자가 아닙니다: {inserted_chunks}"

    return None


async def _fetch_runpod_job_status(ep: str, rk: str, job_id: str) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"https://api.runpod.ai/v2/{ep}/status/{job_id}",
            headers={"Authorization": f"Bearer {rk}"},
        )
    if response.status_code != 200:
        return {
            "id": job_id,
            "status": "FAILED",
            "error": f"RunPod status HTTP {response.status_code}: {(response.text or '')[:500]}",
        }
    return response.json()


async def _wait_for_runpod_completion(
    ep: str,
    rk: str,
    initial_result: dict,
    *,
    poll_interval: float = 2.0,
    timeout_seconds: float = 900.0,
    status_getter=None,
) -> dict:
    result = initial_result
    job_id = str(result.get("id") or "")
    waiting_statuses = {"IN_QUEUE", "IN_PROGRESS", "RUNNING", "RETRYING"}

    if not job_id:
        return result

    if status_getter is None:
        async def status_getter(job_id: str) -> dict:
            return await _fetch_runpod_job_status(ep, rk, job_id)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while str(result.get("status") or "").upper() in waiting_statuses:
        if loop.time() >= deadline:
            return {
                **result,
                "status": "TIMED_OUT",
                "error": (
                    "RunPod job did not complete before backend timeout. "
                    f"last_status={result.get('status')}"
                ),
            }
        await asyncio.sleep(poll_interval)
        result = await status_getter(job_id)

    return result


@router.post("/upload/temp")
async def upload_temp_document(
    org_id: str = Form(...),
    device_id: str = Form(...),
    domain_type: str = Form(...),
    category: str = Form("spec"),
    comment: str | None = Form(None),
    retention_months: int = Form(1),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    try:
        require_same_org(org_id, auth_org_id)

        date_str = datetime.datetime.now().strftime("%Y%m%d")
        file_ext = file.filename.split(".")[-1]
        original_name = file.filename.rsplit(".", 1)[0]
        safe_filename = f"{domain_type}_{category}_{original_name}_{date_str}.{file_ext}"
        s3_key = f"org/{org_id}/spec/{domain_type}/{safe_filename}"

        storage_url = await s3_manager.upload_file_async(
            s3_key=s3_key,
            file_obj=file,
            use_kms=True,
        )

        doc_id = await document_service.create_temp_document(
            db=db,
            org_id=org_id,
            device_id=device_id,
            file_name=safe_filename,
            storage_url=storage_url,
            comment=comment,
            retention_months=retention_months,
            domain=domain_type,
        )

        presigned_url = s3_manager.generate_presigned_url(s3_key, expiration=600)

        ep = (settings.RUNPOD_ENDPOINT_ID or "").strip()
        rk = (settings.RUNPOD_API_KEY or "").strip()
        if not ep or not rk:
            await document_service.update_document_status(db, doc_id, "error")
            raise HTTPException(
                status_code=503,
                detail="RunPod가 설정되지 않았습니다. .env에 RUNPOD_ENDPOINT_ID, RUNPOD_API_KEY를 넣고 서버를 재시작하세요.",
            )

        max_retries = 3
        retry_delay = 2.0
        
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    response = await client.post(
                        f"https://api.runpod.ai/v2/{ep}/runsync",
                        headers={"Authorization": f"Bearer {rk}"},
                        json={
                            "input": {
                                "doc_type": "temp",
                                "file_url": presigned_url,
                                "doc_name": original_name,
                                "domain": domain_type,
                                "category": category,
                                "org_id": org_id,
                                "device_id": device_id,
                                "temp_document_id": str(doc_id),
                            }
                        },
                    )

                if response.status_code == 200:
                    break
                elif response.status_code in (500, 502, 503, 504) and attempt < max_retries - 1:
                    logger.warning(
                        "RunPod HTTP %s: %s, retrying in %ss... (attempt %s/%s)",
                        response.status_code, (response.text or "")[:100], retry_delay, attempt + 1, max_retries
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                else:
                    if response.status_code != 200:
                        await document_service.update_document_status(db, doc_id, "error")
                        body_preview = (response.text or "")[:500]
                        logger.error("RunPod HTTP %s: %s", response.status_code, body_preview)
                        raise HTTPException(
                            status_code=500,
                            detail=f"RunPod API 호출 실패 (HTTP {response.status_code}). 키·엔드포인트 ID·크레딧을 확인하세요.",
                        )
                    break
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                if attempt < max_retries - 1:
                    logger.warning("RunPod Network Error: %s, retrying in %ss... (attempt %s/%s)", exc, retry_delay, attempt + 1, max_retries)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    await document_service.update_document_status(db, doc_id, "error")
                    raise HTTPException(status_code=500, detail=f"RunPod API 통신 실패: {exc}")

        # RunPod 응답 검증 (for 루프 밖 — 정상 응답 경로)
        result = await _wait_for_runpod_completion(ep, rk, response.json())
        if result.get("status") != "COMPLETED":
            await document_service.update_document_status(db, doc_id, "error")
            reason = _runpod_worker_error_message(result)
            logger.error("RunPod 워커 비정상 종료 status=%s body=%s", result.get("status"), result)
            detail = (
                f"RunPod 워커 실패: {reason}"
                if reason
                else "RunPod 워커가 COMPLETED가 아닙니다. 엔드포인트 로그·GPU 워커 코드·입력 URL(S3 presigned)을 확인하세요."
            )
            raise HTTPException(status_code=500, detail=detail)

        worker_output = result.get("output", {})
        output_error = _runpod_completed_output_error_message(worker_output)
        if output_error:
            await document_service.update_document_status(db, doc_id, "error")
            logger.error("RunPod 워커 완료 후 유효 청크 없음: %s output=%s", output_error, worker_output)
            raise HTTPException(
                status_code=500,
                detail=f"RunPod 워커 실패: {output_error}",
            )

        chunk_count = worker_output.get("processed_chunks", 0)

        await document_service.backfill_chunks_org_id(db, str(doc_id), org_id)
        await document_service.update_document_status(db, doc_id, "completed")

        return {
            "status": "success",
            "document_id": str(doc_id),
            "filename": safe_filename,
            "chunk_count": chunk_count,
            "db_inserted_chunks": worker_output.get("db_inserted_chunks"),
            "table_markdown_stats": worker_output.get("table_markdown_stats"),
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Upload Error")
        raise HTTPException(
            status_code=500,
            detail="시방서 업로드 중 오류가 발생했습니다.",
        ) from exc


@router.get("/temp")
async def list_temp_documents(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    require_same_org(org_id, auth_org_id)
    rows = await document_service.get_temp_documents_list(db, org_id)
    return {"status": "success", "documents": rows}


@router.get("/temp/projects/{project_id}/specs")
async def get_project_temp_specs(
    project_id: UUID,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    require_same_org(org_id, auth_org_id)
    project_id_s = str(project_id)
    rows = await document_service.get_project_spec_links(db, org_id, project_id_s)
    return {
        "status": "success",
        "org_id": org_id,
        "project_id": project_id_s,
        "documents": rows,
    }


@router.put("/temp/projects/{project_id}/specs")
async def replace_project_temp_specs(
    project_id: UUID,
    body: ProjectSpecLinksRequest,
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    require_same_org(body.org_id, auth_org_id)
    project_id_s = str(project_id)
    try:
        await document_service.replace_project_spec_links(
            db,
            org_id=body.org_id,
            project_id=project_id_s,
            temp_document_ids=body.temp_document_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rows = await document_service.get_project_spec_links(db, body.org_id, project_id_s)
    return {
        "status": "success",
        "org_id": body.org_id,
        "project_id": project_id_s,
        "documents": rows,
    }


@router.get("/temp/{doc_id}/chunks")
async def get_temp_document_chunks(
    doc_id: str,
    org_id: str = Query(...),
    include_embedding: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    require_same_org(org_id, auth_org_id)
    doc_org = await document_service.get_temp_document_org(db, doc_id)
    if not doc_org:
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    if doc_org != org_id:
        raise HTTPException(status_code=403, detail="문서가 해당 org에 속하지 않습니다.")
    chunks = await document_service.list_temp_document_chunks_export(
        db, doc_id, org_id, include_embedding=include_embedding
    )
    return {"status": "success", "document_id": doc_id, "chunks": chunks}


@router.delete("/temp/{doc_id}")
async def delete_temp_document(
    doc_id: str,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    try:
        require_same_org(org_id, auth_org_id)
        doc_org = await document_service.get_temp_document_org(db, doc_id)
        if not doc_org:
            raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
        if doc_org != org_id:
            raise HTTPException(status_code=403, detail="문서가 해당 org에 속하지 않습니다.")
        await document_service.delete_temp_document(db, doc_id)
        return {"status": "success", "message": "삭제되었습니다."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/temp/{doc_id}/extend")
async def extend_temp_document(
    doc_id: str,
    extra_months: int = Query(..., ge=1, le=12),
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_org_id: str = Depends(get_authenticated_org_id),
):
    try:
        require_same_org(org_id, auth_org_id)
        doc_org = await document_service.get_temp_document_org(db, doc_id)
        if not doc_org:
            raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
        if doc_org != org_id:
            raise HTTPException(status_code=403, detail="문서가 해당 org에 속하지 않습니다.")
        await document_service.extend_document_retention(db, doc_id, extra_months)
        return {"status": "success", "message": f"{extra_months}개월 연장되었습니다."}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
