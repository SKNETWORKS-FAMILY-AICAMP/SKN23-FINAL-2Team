"""
File    : backend/services/vector_service.py
Author  : 김지우
Date    : 2026-04-14
Description : 임시 및 영구 시방서 테이블 검색 로직 (Dense + Sparse 하이브리드 RAG)

Modification History :
    - 2026-04-14 (김지우) : Dense + Sparse RRF 하이브리드 검색 초기 구현
    - 2026-04-15 (김다빈) : BGE Reranker 추가 — RRF 이후 Cross-Encoder 재정렬 단계 도입
                           hybrid_search_*_with_rerank() 함수 추가 (기존 함수 호환 유지)
    - 2026-04-17 (김지우) : _sparse_score_subquery함수에 대해 상수 관리로 변경 (가독성 위함)
    - 2026-04-18 (김지우) : local_files_only=True 추가 (모델 로컬 저장소 사용)
    - 2026-04-20 (김지우) : Reranker → Qwen3-Reranker-0.6B 교체 (CUDA 우선, CPU 폴백)
    - 2026-04-24 : Qwen3 로컬 models/·QWEN3_RERANKER_LOCAL_PATH·RERANKER_HF_OFFLINE
"""
import contextlib
import os
os.environ["HF_HUB_OFFLINE"] = "1"
# os.environ["TRANSFORMERS_OFFLINE"] = "1"

import asyncio
import logging
import threading
from pathlib import Path

import torch
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from FlagEmbedding import BGEM3FlagModel
from transformers import AutoTokenizer, AutoModelForCausalLM

from backend.core.config import settings
from backend.models.schema import DocumentChunk, TempDocumentChunk

# ──────────────────────────────────────────────
# 디바이스 선택
# ──────────────────────────────────────────────
def _get_optimal_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    elif torch.backends.mps.is_available():
        return "mps"
    return "cpu"

_device = _get_optimal_device()
logging.info("[vector_service] 디바이스: %s", _device)

# Mac(MPS) 환경에서 fp16 사용 시 mps.add 타입 충돌 에러가 잦으므로 mps일 경우 fp16 비활성화
_use_fp16 = (_device == "cuda")  # CUDA에서만 fp16 사용, MPS/CPU는 fp32 사용

embedding_model = BGEM3FlagModel(
    "BAAI/bge-m3",
    use_fp16=_use_fp16,
    device=_device,
)

# ──────────────────────────────────────────────
# Qwen3-Reranker (생성형 Yes/No 리랭커)
# ──────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RERANKER_MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
_RERANKER_MAX_LEN = 4096
_RERANKER_SYSTEM = (
    "Judge whether the Document is relevant to the Query. "
    "Reply 'yes' if relevant, 'no' if not."
)


def _resolve_qwen_reranker_ref() -> tuple[str, bool]:
    """
    (model_id_or_path, local_files_only)
    1) QWEN3_RERANKER_LOCAL_PATH 가 유효한 디렉터리면 그 경로
    2) <프로젝트>/models/Qwen__Qwen3-Reranker-0.6B 가 있으면 사용 (gitignore된 로컬 가중치)
    3) 아니면 HF id — RERANKER_HF_OFFLINE True면 캐시만, False면 허브 다운로드 허용
    """
    env_p = (settings.QWEN3_RERANKER_LOCAL_PATH or "").strip()
    if env_p:
        p = Path(env_p).expanduser()
        if p.is_dir():
            logging.info("[vector_service] Qwen3-Reranker 로컬: %s", p)
            return str(p), True
        logging.warning("[vector_service] QWEN3_RERANKER_LOCAL_PATH 가 디렉터리가 아님: %s", p)

    default_dir = _REPO_ROOT / "models" / "Qwen__Qwen3-Reranker-0.6B"
    if default_dir.is_dir():
        logging.info("[vector_service] Qwen3-Reranker 로컬(기본): %s", default_dir)
        return str(default_dir), True

    lfo = bool(settings.RERANKER_HF_OFFLINE)
    logging.info(
        "[vector_service] Qwen3-Reranker HF id=%s local_files_only=%s",
        _RERANKER_MODEL_ID,
        lfo,
    )
    return _RERANKER_MODEL_ID, lfo


@contextlib.contextmanager
def _reranker_load_env():
    """RERANKER_HF_OFFLINE=False 이면 모듈 전역 OFFLINE 을 잠시 해제해 허브에서 받을 수 있게 함."""
    if settings.RERANKER_HF_OFFLINE:
        yield
        return
    keys = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Qwen3Reranker:
    """FlagReranker와 동일한 compute_score() 인터페이스를 제공하는 Qwen3 래퍼."""

    def __init__(self, model_id: str, device: str, *, local_files_only: bool = True):
        dtype = torch.float16 if device == "cuda" else torch.float32
        self.device = device
        with _reranker_load_env():
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_id, padding_side="left", local_files_only=local_files_only,
                trust_remote_code=True,
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                model_id, torch_dtype=dtype, local_files_only=local_files_only,
                trust_remote_code=True,
            ).to(device).eval()
        # 정답 토큰 ID (단어 단위 마지막 토큰)
        self._yes_id = self.tokenizer("yes", add_special_tokens=False)["input_ids"][-1]
        self._no_id  = self.tokenizer("no",  add_special_tokens=False)["input_ids"][-1]

    def _build_prompt(self, query: str, doc: str) -> str:
        messages = [
            {"role": "system", "content": _RERANKER_SYSTEM},
            {"role": "user",   "content": f"<query>{query}</query>\n<document>{doc[:2000]}</document>"},
        ]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        ) + "yes"

    def compute_score(self, pairs: list, normalize: bool = True) -> list[float]:
        scores: list[float] = []
        for query, doc in pairs:
            prompt  = self._build_prompt(query, doc)
            inputs  = self.tokenizer(
                prompt, return_tensors="pt",
                truncation=True, max_length=_RERANKER_MAX_LEN,
            ).to(self.device)
            with torch.no_grad():
                logits = self.model(**inputs).logits[:, -1, :]
                yes_s  = logits[0, self._yes_id].float().item()
                no_s   = logits[0, self._no_id].float().item()
                score  = torch.softmax(torch.tensor([yes_s, no_s]), dim=0)[0].item()
            scores.append(score)
        return scores


def _load_reranker() -> _Qwen3Reranker | None:
    try:
        ref, lfo = _resolve_qwen_reranker_ref()
        # Mac(MPS) 환경에서 데이터 타입 충돌(mps.add f16 vs f32) 에러 방지를 위해 Reranker는 CPU 사용 강제
        target_device = "cpu" if _device == "mps" else _device
        r = _Qwen3Reranker(ref, target_device, local_files_only=lfo)
        logging.info(
            "[vector_service] Qwen3-Reranker 로드 완료 ref=%s local_files_only=%s device=%s (original=%s)",
            ref,
            lfo,
            target_device,
            _device,
        )
        return r
    except Exception as e:
        logging.warning("[vector_service] Qwen3-Reranker 로드 실패, rerank 비활성화: %s", e)
        return None


_reranker_model: _Qwen3Reranker | None = None
_reranker_attempted: bool = False
_reranker_lock = threading.Lock()


def _get_reranker() -> _Qwen3Reranker | None:
    """첫 rerank 호출 시에만 HF/로컬 캐시에서 로드 (시동 시 경고·네트워크 시도 방지)."""
    global _reranker_model, _reranker_attempted
    if _reranker_attempted:
        return _reranker_model
    with _reranker_lock:
        if _reranker_attempted:
            return _reranker_model
        _reranker_model = _load_reranker()
        _reranker_attempted = True
        return _reranker_model

# ──────────────────────────────────────────────
# Dense 임베딩 생성 (Sparse 기능 제거)
# ──────────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """Dense 임베딩 반환 (1024차원)"""
    def _encode() -> list[float]:
        result = embedding_model.encode(
            [text],
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return result["dense_vecs"][0].tolist()

    return await asyncio.to_thread(_encode)


# ──────────────────────────────────────────────
# Dense 검색
# ──────────────────────────────────────────────

async def search_temp_chunks(
    db: AsyncSession, query_vector: list, spec_guid: str | None, org_id: str, limit: int = 5
) -> list[TempDocumentChunk]:
    q = select(TempDocumentChunk).where(TempDocumentChunk.org_id == org_id)
    if spec_guid:
        q = q.where(TempDocumentChunk.temp_document_id == spec_guid)
    q = q.order_by(TempDocumentChunk.dense_embedding.cosine_distance(query_vector)).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


async def search_permanent_chunks(
    db: AsyncSession,
    query_vector: list,
    document_id: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    doc_name: str | None = None,
    limit: int = 5,
) -> list[DocumentChunk]:
    q = select(DocumentChunk)
    if document_id:
        q = q.where(DocumentChunk.document_id == document_id)
    if domain:
        q = q.where(DocumentChunk.domain == domain)
    if category:
        q = q.where(DocumentChunk.category == category)
    if doc_name:
        q = q.where(DocumentChunk.doc_name.ilike(f"%{doc_name}%"))
    q = q.order_by(DocumentChunk.dense_embedding.cosine_distance(query_vector)).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


# ──────────────────────────────────────────────
# Lexical 검색 (tsvector + pg_trgm 하이브리드)
# ──────────────────────────────────────────────

async def search_temp_chunks_lexical(
    db: AsyncSession,
    query: str,
    spec_guid: str | None,
    org_id: str,
    limit: int = 5,
) -> list[TempDocumentChunk]:
    """tsvector(키워드) 및 pg_trgm(오타/유사도) 기반 임시 청크 검색"""
    where_clauses = [
        "c.org_id = :org_id",
        "(c.search_vector @@ websearch_to_tsquery('simple', :query) OR c.section_id % :query OR c.doc_name ILIKE :query_like)",
    ]
    params: dict = {"query": query, "query_like": f"%{query}%", "org_id": org_id, "limit": limit}

    if spec_guid:
        where_clauses.append("c.temp_document_id = :spec_guid")
        params["spec_guid"] = spec_guid

    where_sql = " AND ".join(where_clauses)
    stmt = text(f"""
        SELECT c.id,
               (ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', :query)) +
                COALESCE(similarity(c.section_id, :query), 0) +
                CASE WHEN c.doc_name ILIKE :query_like THEN 1.0 ELSE 0.0 END) AS lexical_score
        FROM temp_document_chunks c
        WHERE {where_sql}
        ORDER BY lexical_score DESC
        LIMIT :limit
    """)
    result = await db.execute(stmt, params)
    rows = result.fetchall()

    if not rows:
        return []

    ids = [r[0] for r in rows]
    id_rank = {id_: rank for rank, id_ in enumerate(ids)}
    result_chunks = await db.execute(
        select(TempDocumentChunk).where(TempDocumentChunk.id.in_(ids))
    )
    chunks = result_chunks.scalars().all()
    return sorted(chunks, key=lambda c: id_rank.get(c.id, 9999))


async def search_permanent_chunks_lexical(
    db: AsyncSession,
    query: str,
    document_id: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    doc_name: str | None = None,
    limit: int = 5,
) -> list[DocumentChunk]:
    """tsvector(키워드) 및 pg_trgm(오타/유사도) 기반 영구 청크 검색"""
    where_clauses = ["(c.search_vector @@ websearch_to_tsquery('simple', :query) OR c.section_id % :query OR c.doc_name ILIKE :query_like)"]
    params: dict = {"query": query, "query_like": f"%{query}%", "limit": limit}

    if document_id:
        where_clauses.append("c.document_id = :document_id")
        params["document_id"] = document_id
    if domain:
        where_clauses.append("c.domain = :domain")
        params["domain"] = domain
    if category:
        where_clauses.append("c.category = :category")
        params["category"] = category
    if doc_name:
        where_clauses.append("c.doc_name ILIKE :doc_name")
        params["doc_name"] = f"%{doc_name}%"

    where_sql = " AND ".join(where_clauses)
    stmt = text(f"""
        SELECT c.id,
               (ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', :query)) +
                COALESCE(similarity(c.section_id, :query), 0) +
                CASE WHEN c.doc_name ILIKE :query_like THEN 1.0 ELSE 0.0 END) AS lexical_score
        FROM document_chunks c
        WHERE {where_sql}
        ORDER BY lexical_score DESC
        LIMIT :limit
    """)
    result = await db.execute(stmt, params)
    rows = result.fetchall()

    if not rows:
        return []

    ids = [r[0] for r in rows]
    id_rank = {id_: rank for rank, id_ in enumerate(ids)}
    result_chunks = await db.execute(
        select(DocumentChunk).where(DocumentChunk.id.in_(ids))
    )
    chunks = result_chunks.scalars().all()
    return sorted(chunks, key=lambda c: id_rank.get(c.id, 9999))


# ──────────────────────────────────────────────
# Hybrid RAG (Dense + Lexical → RRF 융합)
# ──────────────────────────────────────────────

def _rrf_merge(
    dense_results: list,
    lexical_results: list,
    k: int = 60,
    limit: int = 5,
) -> list:
    scores: dict[int, dict] = {}

    for rank, chunk in enumerate(dense_results):
        scores.setdefault(chunk.id, {"chunk": chunk, "score": 0.0})
        scores[chunk.id]["score"] += 1.0 / (k + rank + 1)

    for rank, chunk in enumerate(lexical_results):
        scores.setdefault(chunk.id, {"chunk": chunk, "score": 0.0})
        scores[chunk.id]["score"] += 1.0 / (k + rank + 1)

    sorted_items = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    return [item["chunk"] for item in sorted_items[:limit]]


async def hybrid_search_temp_chunks(
    db: AsyncSession,
    query: str,
    spec_guid: str | None = None,
    org_id: str = "",
    limit: int = 5,
) -> list[TempDocumentChunk]:
    dense_vec = await get_embedding(query)

    # Dense 검색과 Lexical(SQL) 검색을 분리하여 실행
    dense_results   = await search_temp_chunks(db, dense_vec, spec_guid, org_id, limit=limit * 2)
    lexical_results = await search_temp_chunks_lexical(db, query, spec_guid, org_id, limit=limit * 2)

    return _rrf_merge(dense_results, lexical_results, limit=limit)


async def hybrid_search_permanent_chunks(
    db: AsyncSession,
    query: str,
    document_id: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    doc_name: str | None = None,
    limit: int = 5,
) -> list[DocumentChunk]:
    dense_vec = await get_embedding(query)

    dense_results   = await search_permanent_chunks(
        db, dense_vec, document_id=document_id, domain=domain, category=category, doc_name=doc_name, limit=limit * 2
    )
    lexical_results = await search_permanent_chunks_lexical(
        db, query, document_id=document_id, domain=domain, category=category, doc_name=doc_name, limit=limit * 2
    )

    return _rrf_merge(dense_results, lexical_results, limit=limit)


# ──────────────────────────────────────────────
# Reranker (Cross-Encoder 재정렬)
# ──────────────────────────────────────────────

async def rerank_chunks(query: str, chunks: list, limit: int) -> list:
    return await _rerank(query, chunks, limit)

async def _rerank(query: str, chunks: list, limit: int) -> list:
    if not chunks:
        return chunks
    model = _get_reranker()
    if model is None:
        return chunks[:limit]

    pairs = [[query, chunk.content] for chunk in chunks]

    def _score() -> list[float]:
        return model.compute_score(pairs, normalize=True)

    try:
        scores = await asyncio.to_thread(_score)
    except Exception as e:
        logging.warning("[vector_service] reranker 오류, RRF 순서 유지: %s", e)
        return chunks[:limit]

    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
    return [chunk for _, chunk in ranked[:limit]]


async def hybrid_search_permanent_chunks_with_rerank(
    db: AsyncSession,
    query: str,
    document_id: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    doc_name: str | None = None,
    rrf_limit: int = 20,
    final_limit: int = 5,
) -> list[DocumentChunk]:
    dense_vec = await get_embedding(query)

    dense_results   = await search_permanent_chunks(
        db, dense_vec, document_id=document_id, domain=domain, category=category, doc_name=doc_name, limit=rrf_limit * 2
    )
    lexical_results = await search_permanent_chunks_lexical(
        db, query, document_id=document_id, domain=domain, category=category, doc_name=doc_name, limit=rrf_limit * 2
    )

    rrf_results = _rrf_merge(dense_results, lexical_results, limit=rrf_limit)
    return await _rerank(query, rrf_results, limit=final_limit)


async def hybrid_search_temp_chunks_with_rerank(
    db: AsyncSession,
    query: str,
    spec_guid: str | None = None,
    org_id: str = "",
    rrf_limit: int = 20,
    final_limit: int = 5,
) -> list[TempDocumentChunk]:
    dense_vec = await get_embedding(query)

    dense_results   = await search_temp_chunks(db, dense_vec, spec_guid, org_id, limit=rrf_limit * 2)
    lexical_results = await search_temp_chunks_lexical(db, query, spec_guid, org_id, limit=rrf_limit * 2)

    rrf_results = _rrf_merge(dense_results, lexical_results, limit=rrf_limit)
    return await _rerank(query, rrf_results, limit=final_limit)


async def get_dictionary_chunks(db: AsyncSession, domain: str) -> list[DocumentChunk]:
    query = (
        select(DocumentChunk)
        .filter(
            DocumentChunk.domain    == domain,
            DocumentChunk.category  == "dictionary",
        )
        .order_by(DocumentChunk.section_id)
    )
    result = await db.execute(query)
    return result.scalars().all()