"""
File    : backend/scripts/index_dictionaries.py
Author  : 김다빈
Create  : 2026-04-15
Description :
    도메인별 레이어/블록명 사전을 document_chunks 테이블에 인덱싱합니다.
    category="dictionary"로 저장되어 workflow_handler가 직접 조회합니다.

    사전 파일 위치: data/dictionaries/
    파일 명명 규칙: {domain}_{type}_dict.txt
      예) arch_layer_dict.txt, arch_block_dict.txt

Usage:
    # 프로젝트 루트에서 실행
    python -m backend.scripts.index_dictionaries
    python -m backend.scripts.index_dictionaries --domain arch
    python -m backend.scripts.index_dictionaries --reset  # 기존 사전 청크 삭제 후 재삽입
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.database import SessionLocal
from backend.models.schema import DocumentChunk, DocumentS3
from backend.services.vector_service import get_embedding

DICT_DIR = ROOT / "data" / "dictionaries"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def _parse_domain_from_filename(fname: str) -> str:
    return fname.split("_")[0]


def _parse_type_from_filename(fname: str) -> str:
    parts = fname.replace(".txt", "").split("_")
    return parts[1] if len(parts) > 1 else "general"


def _make_document_id(fname: str) -> str:
    return "dict_" + fname.replace(".txt", "")


async def _index_file(db, dict_path: Path, reset: bool) -> None:
    fname   = dict_path.name
    domain  = _parse_domain_from_filename(fname)
    dict_type = _parse_type_from_filename(fname)
    doc_id  = _make_document_id(fname)
    doc_name = f"[사전] {domain} {dict_type}"

    content = dict_path.read_text(encoding="utf-8").strip()
    if not content:
        log.warning("빈 파일 건너뜀: %s", fname)
        return

    # 기존 청크 삭제 (reset 또는 재인덱싱)
    existing = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc_id).first()
    if existing:
        if reset:
            db.query(DocumentChunk).filter(DocumentChunk.document_id == doc_id).delete()
            db.query(DocumentS3).filter(DocumentS3.id == doc_id).delete()
            db.commit()
            log.info("기존 사전 청크 삭제: %s", doc_id)
        else:
            log.info("이미 인덱싱된 사전 건너뜀: %s (--reset 으로 재인덱싱 가능)", fname)
            return

    # documents_s3 가상 레코드 생성
    doc_s3 = DocumentS3(
        id=doc_id,
        file_name=fname,
        s3_url=f"local://data/dictionaries/{fname}",
        file_type="dictionary",
    )
    db.add(doc_s3)
    db.flush()

    # Sparse 임베딩 제거: Dense 임베딩만 생성
    log.info("임베딩 생성 중: %s ...", fname)
    dense_vec = await get_embedding(content)

    # document_chunks 삽입 (search_vector는 DB가 자동 생성)
    chunk = DocumentChunk(
        document_id     = doc_id,
        chunk_index     = 0,
        content         = content,
        dense_embedding = dense_vec,
        domain          = domain,
        category        = "dictionary",
        doc_name        = doc_name,
        section_id      = dict_type,
        chunk_type      = "dictionary",
    )
    db.add(chunk)
    db.commit()
    log.info("인덱싱 완료: %s (domain=%s, category=dictionary)", fname, domain)


async def main(domain_filter: str | None, reset: bool) -> None:
    if not DICT_DIR.exists():
        log.error("사전 디렉토리 없음: %s", DICT_DIR)
        sys.exit(1)

    dict_files = sorted(DICT_DIR.glob("*.txt"))
    if domain_filter:
        dict_files = [f for f in dict_files if f.name.startswith(domain_filter + "_")]

    if not dict_files:
        log.warning("인덱싱할 사전 파일 없음 (domain=%s)", domain_filter or "*")
        return

    db = SessionLocal()
    try:
        for dict_path in dict_files:
            await _index_file(db, dict_path, reset=reset)
    finally:
        db.close()

    log.info("=== 사전 인덱싱 완료 (총 %d개 파일) ===", len(dict_files))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="도메인 사전을 document_chunks에 인덱싱")
    parser.add_argument("--domain", default=None, help="특정 도메인만 처리 (예: arch)")
    parser.add_argument("--reset", action="store_true", help="기존 사전 청크 삭제 후 재삽입")
    args = parser.parse_args()

    asyncio.run(main(domain_filter=args.domain, reset=args.reset))