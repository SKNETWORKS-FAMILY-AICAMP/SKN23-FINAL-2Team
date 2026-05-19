"""
File    : backend/services/document_service.py
Author  : 김지우
Create  : 2026-04-07
Description :
    문서(정규/임시) 및 텍스트 청크의 데이터베이스 입출력(CRUD) 로직을 담당합니다.
    - 유료 회원의 임시 시방서 등록 및 S3 경로 관리
    - PostgreSQL INTERVAL을 활용한 임시 문서의 자동 파기(Expiration) 설정
    - AsyncSession 전용 (FastAPI get_db와 동일 세션)

Modification History :
    - 2026-04-06 (김지우) : 초기 구조 생성
    - 2026-04-07 (김지우) : DB 구조에 따른 코드 수정
    - 2026-04-21 (김지우) : Session → AsyncSession 전환 (execute/commit await)
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List
import logging

from backend.utils.s3_manager import S3Manager

logger = logging.getLogger(__name__)


def _s3_key_from_url(storage_url: str | None) -> str:
    if not storage_url:
        return ""
    s3 = S3Manager()
    prefix = f"s3://{s3.bucket_name}/"
    return storage_url[len(prefix):] if storage_url.startswith(prefix) else storage_url


async def _delete_s3_url(storage_url: str | None) -> bool:
    s3_key = _s3_key_from_url(storage_url)
    if not s3_key:
        return False
    try:
        await S3Manager().delete_object_async(s3_key)
        return True
    except Exception as exc:
        logger.warning("[DocumentService] temp spec S3 delete failed key=%s: %s", s3_key, exc)
        return False


async def create_temp_document(
    db: AsyncSession,
    org_id: str,
    device_id: str,
    file_name: str,
    storage_url: str,
    comment: str | None,
    retention_months: int,
    domain: str | None = None,
):
    query = text("""
        INSERT INTO temp_documents (
            org_id, device_id, file_name, temp_s3_url,
            comment, status, expires_at, domain
        )
        VALUES (
            :org_id, :device_id, :file_name, :temp_s3_url,
            :comment, 'pending', NOW() + make_interval(0, :months), :domain
        )
        RETURNING id
    """)

    result = await db.execute(
        query,
        {
            "org_id": org_id,
            "device_id": device_id,
            "file_name": file_name,
            "temp_s3_url": storage_url,
            "comment": comment,
            "months": retention_months,
            "domain": domain,
        },
    )
    row = result.fetchone()
    await db.commit()
    if row is None:
        raise RuntimeError("create_temp_document: INSERT RETURNING returned no row")
    return row[0]


async def insert_document_chunk(
    db: AsyncSession,
    doc_id: str,
    content: str,
    dense_vec: List[float],
    is_temp: bool,
    chunk_index: int = 0,
):
    table_name = "temp_document_chunks" if is_temp else "document_chunks"
    id_column = "temp_document_id" if is_temp else "document_id"

    query = text(f"""
        INSERT INTO {table_name} ({id_column}, chunk_index, content, dense_embedding)
        VALUES (:doc_id, :chunk_index, :content, :dense_vec)
    """)

    await db.execute(
        query,
        {
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "content": content,
            "dense_vec": str(dense_vec),
        },
    )
    await db.commit()


async def update_document_status(db: AsyncSession, doc_id: str, status: str, is_temp: bool = True):
    table = "temp_documents" if is_temp else "documents_s3"
    query = text(f"UPDATE {table} SET status = :status WHERE id = :doc_id")
    await db.execute(query, {"status": status, "doc_id": doc_id})
    await db.commit()


async def backfill_chunks_org_id(db: AsyncSession, doc_id: str, org_id: str):
    """워커가 INSERT한 청크에 org_id를 백필합니다."""
    query = text("""
        UPDATE temp_document_chunks
        SET org_id = :org_id
        WHERE temp_document_id = :doc_id AND org_id IS NULL
    """)
    await db.execute(query, {"org_id": org_id, "doc_id": doc_id})
    await db.commit()


async def get_temp_documents_list(db: AsyncSession, org_id: str) -> list[dict]:
    query = text("""
        SELECT id, file_name, comment, status, domain,
               created_at as reg_date, expires_at as delete_date,
               temp_s3_url as storage_path
        FROM temp_documents
        WHERE org_id = :org_id
        ORDER BY created_at DESC
    """)
    result = await db.execute(query, {"org_id": org_id})
    return [dict(row) for row in result.mappings().all()]


async def get_temp_document_org(db: AsyncSession, doc_id: str) -> str | None:
    r = await db.execute(
        text("SELECT org_id::text FROM temp_documents WHERE id = :id"),
        {"id": doc_id},
    )
    row = r.fetchone()
    return str(row[0]) if row else None


def _dedupe_temp_document_ids(temp_document_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in temp_document_ids or []:
        doc_id = str(raw or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(doc_id)
    return out


async def replace_project_spec_links(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    temp_document_ids: list[str],
) -> list[str]:
    """Replace project-level temp spec links after verifying org ownership."""
    ids = _dedupe_temp_document_ids(temp_document_ids)

    if ids:
        owned = await db.execute(
            text("""
                SELECT id::text AS id
                FROM temp_documents
                WHERE org_id = :org_id
                  AND id::text = ANY(:ids)
            """),
            {"org_id": org_id, "ids": ids},
        )
        owned_ids = {str(row["id"]) for row in owned.mappings().all()}
        missing = [doc_id for doc_id in ids if doc_id not in owned_ids]
        if missing:
            raise ValueError(f"temp document not found for org: {', '.join(missing)}")

    await db.execute(
        text("""
            DELETE FROM project_spec_links
            WHERE org_id = :org_id AND project_id = :project_id
        """),
        {"org_id": org_id, "project_id": project_id},
    )

    for priority, doc_id in enumerate(ids):
        await db.execute(
            text("""
                INSERT INTO project_spec_links (
                    org_id, project_id, temp_document_id, priority
                )
                VALUES (
                    :org_id, :project_id, :temp_document_id, :priority
                )
                ON CONFLICT (org_id, project_id, temp_document_id)
                DO UPDATE SET
                    priority = EXCLUDED.priority,
                    updated_at = NOW()
            """),
            {
                "org_id": org_id,
                "project_id": project_id,
                "temp_document_id": doc_id,
                "priority": priority,
            },
        )

    await db.commit()
    return ids


async def get_project_spec_links(
    db: AsyncSession,
    org_id: str,
    project_id: str,
) -> list[dict]:
    result = await db.execute(
        text("""
            SELECT
                t.id::text AS temp_document_id,
                t.file_name,
                t.comment,
                t.status,
                t.domain,
                t.created_at AS reg_date,
                t.expires_at AS delete_date,
                t.temp_s3_url AS storage_path,
                l.priority
            FROM project_spec_links l
            JOIN temp_documents t
              ON t.id = l.temp_document_id
             AND t.org_id = l.org_id
            WHERE l.org_id = :org_id
              AND l.project_id = :project_id
            ORDER BY l.priority ASC, t.created_at DESC
        """),
        {"org_id": org_id, "project_id": project_id},
    )
    rows: list[dict] = []
    for row in result.mappings().all():
        d = dict(row)
        for key in ("reg_date", "delete_date"):
            value = d.get(key)
            if value is not None and hasattr(value, "isoformat"):
                d[key] = value.isoformat()
        rows.append(d)
    return rows


async def list_temp_document_chunks_export(
    db: AsyncSession,
    doc_id: str,
    org_id: str,
    include_embedding: bool = False,
) -> list[dict]:
    """temp_document_chunks 조회(API 응답용). dense_embedding 기본 제외."""
    base_cols = """
        id::text AS id, temp_document_id::text AS temp_document_id, org_id::text AS org_id,
        chunk_index, content, domain, category, doc_name,
        effective_date, session_id AS section_id, chunk_type, table_markdown
    """
    if include_embedding:
        q = text(f"""
            SELECT {base_cols}, dense_embedding::text AS dense_embedding
            FROM temp_document_chunks
            WHERE temp_document_id = :doc_id AND org_id = :org_id
            ORDER BY chunk_index NULLS LAST, id
        """)
    else:
        q = text(f"""
            SELECT {base_cols}
            FROM temp_document_chunks
            WHERE temp_document_id = :doc_id AND org_id = :org_id
            ORDER BY chunk_index NULLS LAST, id
        """)
    result = await db.execute(q, {"doc_id": doc_id, "org_id": org_id})
    rows: list[dict] = []
    for m in result.mappings().all():
        d = dict(m)
        ed = d.get("effective_date")
        if ed is not None and hasattr(ed, "isoformat"):
            d["effective_date"] = ed.isoformat()
        rows.append(d)
    return rows


async def delete_temp_document(db: AsyncSession, doc_id: str):
    """임시 문서 삭제 (CASCADE로 chunks도 함께 삭제)"""
    row = await db.execute(
        text("SELECT temp_s3_url FROM temp_documents WHERE id = :id"),
        {"id": doc_id},
    )
    storage_url = row.scalar_one_or_none()
    await _delete_s3_url(storage_url)
    await db.execute(text("DELETE FROM temp_documents WHERE id = :id"), {"id": doc_id})
    await db.commit()


async def extend_document_retention(db: AsyncSession, doc_id: str, extra_months: int):
    """보관기간 연장 (기존 만료일에 월 추가)"""
    await db.execute(
        text("UPDATE temp_documents SET expires_at = expires_at + make_interval(0, :m) WHERE id = :id"),
        {"m": extra_months, "id": doc_id},
    )
    await db.commit()


async def delete_expired_documents(db: AsyncSession) -> int:
    """만료된 임시 문서 일괄 삭제 (크론용). CASCADE로 chunks 자동 삭제. 삭제 건수 반환."""
    rows = await db.execute(
        text("SELECT id, temp_s3_url FROM temp_documents WHERE expires_at < NOW()")
    )
    expired = rows.mappings().all()
    for row in expired:
        await _delete_s3_url(row.get("temp_s3_url"))

    result = await db.execute(text("DELETE FROM temp_documents WHERE expires_at < NOW()"))
    await db.commit()
    return result.rowcount or 0
