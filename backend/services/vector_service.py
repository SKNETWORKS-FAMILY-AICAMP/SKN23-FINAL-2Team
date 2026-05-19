"""
File    : backend/services/vector_service.py
Author  : 김지우
Date    : 2026-04-14
Description : 임시 및 영구 시방서 테이블 검색 로직 (Dense + Sparse 하이브리드 RAG)

Modification History :
    - 2026-04-14 (김지우) : Dense + Sparse RRF 하이브리드 검색 초기 구현
    - 2026-04-15 (김다빈) : BGE Reranker 추가 — RRF 이후 Cross-Encoder 재정렬 단계 도입
    - 2026-04-17 (김지우) : _sparse_score_subquery함수에 대해 상수 관리로 변경 (가독성 위함)
    - 2026-04-18 (김지우) : local_files_only=True 추가 (모델 로컬 저장소 사용)
    - 2026-04-20 (김지우) : Reranker → Qwen3-Reranker-0.6B 교체 (CUDA 우선, CPU 폴백)
    - 2026-04-24 : Qwen3 로컬 models/·QWEN3_RERANKER_LOCAL_PATH·RERANKER_HF_OFFLINE
    - 2026-05-11 (김지우) : table chunk context 생성 시 table_markdown 우선 사용
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
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from FlagEmbedding import BGEM3FlagModel
from transformers import AutoTokenizer, AutoModelForCausalLM

from backend.core.config import settings
from backend.models.schema import DocumentChunk, TempDocumentChunk


# ──────────────────────────────────────────────
# RAG context helpers
# ──────────────────────────────────────────────

def get_chunk_context_content(chunk) -> str:
    """
    LLM/Reranker에 전달할 context용 텍스트를 반환한다.

    원칙:
    - 검색/임베딩은 기존 content 기준 유지
    - table chunk는 table_markdown이 있으면 표 형태 보존을 위해 table_markdown 우선 사용
    - table_markdown이 없거나 text chunk면 content 사용
    """
    if chunk is None:
        return ""

    chunk_type = getattr(chunk, "chunk_type", None)
    table_markdown = getattr(chunk, "table_markdown", None)
    content = getattr(chunk, "content", None)

    if chunk_type == "table" and table_markdown:
        return table_markdown

    return content or ""


def attach_context_content(chunk):
    """
    기존 코드 호환용.
    SQLAlchemy ORM 객체에 context_content 속성을 동적으로 붙인다.

    주의:
    - 기존 chunk.content는 변경하지 않는다.
    - dense/search_vector 검색 기준은 계속 content를 사용한다.
    - query.py에서 getattr(chunk, "context_content", chunk.content)로 쓰면 됨.
    """
    try:
        setattr(chunk, "context_content", get_chunk_context_content(chunk))
    except Exception:
        pass
    return chunk


def attach_context_content_all(chunks: list) -> list:
    return [attach_context_content(chunk) for chunk in chunks]


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

_use_fp16 = (_device == "cuda")

embedding_model = BGEM3FlagModel(
    "BAAI/bge-m3",
    use_fp16=_use_fp16,
    device=_device,
)


# ──────────────────────────────────────────────
# Qwen3-Reranker
# ──────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_RERANKER_MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
_RERANKER_MAX_LEN = 4096
_RERANKER_SYSTEM = (
    "Judge whether the Document is relevant to the Query. "
    "Reply 'yes' if relevant, 'no' if not."
)


def _resolve_qwen_reranker_ref() -> tuple[str, bool]:
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
    def __init__(self, model_id: str, device: str, *, local_files_only: bool = True):
        dtype = torch.float16 if device == "cuda" else torch.float32
        self.device = device

        with _reranker_load_env():
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_id,
                padding_side="left",
                local_files_only=local_files_only,
                trust_remote_code=True,
                use_fast=True,
            )
            self.model = AutoModelForCausalLM.from_pretrained(
                model_id,
                torch_dtype=dtype,
                local_files_only=local_files_only,
                trust_remote_code=True,
            ).to(device).eval()

        self._yes_id = self.tokenizer("yes", add_special_tokens=False)["input_ids"][-1]
        self._no_id = self.tokenizer("no", add_special_tokens=False)["input_ids"][-1]

    def _build_prompt(self, query: str, doc: str) -> str:
        messages = [
            {"role": "system", "content": _RERANKER_SYSTEM},
            {
                "role": "user",
                "content": f"<query>{query}</query>\n<document>{doc[:2000]}</document>",
            },
        ]
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        ) + "yes"

    def compute_score(self, pairs: list, normalize: bool = True) -> list[float]:
        scores: list[float] = []

        for query, doc in pairs:
            prompt = self._build_prompt(query, doc)
            inputs = self.tokenizer(
                prompt,
                return_tensors="pt",
                truncation=True,
                max_length=_RERANKER_MAX_LEN,
            ).to(self.device)

            with torch.no_grad():
                logits = self.model(**inputs).logits[:, -1, :]
                yes_s = logits[0, self._yes_id].float().item()
                no_s = logits[0, self._no_id].float().item()
                score = torch.softmax(torch.tensor([yes_s, no_s]), dim=0)[0].item()

            scores.append(score)

        return scores


def _load_reranker() -> _Qwen3Reranker | None:
    try:
        ref, lfo = _resolve_qwen_reranker_ref()
        target_device = "cpu" if _device == "mps" else _device

        r = _Qwen3Reranker(ref, target_device, local_files_only=lfo)

        logging.info(
            "[vector_service] Qwen3-Reranker 로드 완료 ref=%s local_files_only=%s device=%s original=%s",
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
# Dense 임베딩 생성
# ──────────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    def _encode() -> list[float]:
        result = embedding_model.encode(
            [text],
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return result["dense_vecs"][0].tolist()

    return await asyncio.to_thread(_encode)


def _normalize_spec_guids(spec_guid) -> list[str]:
    if not spec_guid:
        return []

    if isinstance(spec_guid, str):
        raw_values = spec_guid.split(",") if "," in spec_guid else [spec_guid]
    elif isinstance(spec_guid, (list, tuple, set)):
        raw_values = list(spec_guid)
    else:
        raw_values = [spec_guid]

    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# ──────────────────────────────────────────────
# Dense 검색
# ──────────────────────────────────────────────

async def search_temp_chunks(
    db: AsyncSession,
    query_vector: list,
    spec_guid,
    org_id: str,
    limit: int = 5,
) -> list[TempDocumentChunk]:
    q = select(TempDocumentChunk).where(TempDocumentChunk.org_id == org_id)

    spec_guids = _normalize_spec_guids(spec_guid)
    if spec_guids:
        q = q.where(TempDocumentChunk.temp_document_id.in_(spec_guids))

    q = q.order_by(TempDocumentChunk.dense_embedding.cosine_distance(query_vector)).limit(limit)

    result = await db.execute(q)
    chunks = result.scalars().all()
    return attach_context_content_all(chunks)


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
    chunks = result.scalars().all()
    return attach_context_content_all(chunks)


# ──────────────────────────────────────────────
# Lexical 검색
# ──────────────────────────────────────────────

async def search_temp_chunks_lexical(
    db: AsyncSession,
    query: str,
    spec_guid,
    org_id: str,
    limit: int = 5,
) -> list[TempDocumentChunk]:
    where_clauses = [
        "c.org_id = :org_id",
        "(c.search_vector @@ websearch_to_tsquery('simple', :query) OR c.session_id % :query OR c.doc_name ILIKE :query_like)",
    ]
    params: dict = {
        "query": query,
        "query_like": f"%{query}%",
        "org_id": org_id,
        "limit": limit,
    }

    spec_guids = _normalize_spec_guids(spec_guid)
    if spec_guids:
        where_clauses.append("c.temp_document_id IN :spec_guids")
        params["spec_guids"] = spec_guids

    where_sql = " AND ".join(where_clauses)

    stmt = text(f"""
        SELECT c.id,
               (
                   ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', :query)) +
                    COALESCE(similarity(c.session_id, :query), 0) +
                   CASE WHEN c.doc_name ILIKE :query_like THEN 1.0 ELSE 0.0 END
               ) AS lexical_score
        FROM temp_document_chunks c
        WHERE {where_sql}
        ORDER BY lexical_score DESC
        LIMIT :limit
    """)
    if spec_guids:
        stmt = stmt.bindparams(bindparam("spec_guids", expanding=True))

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

    chunks = sorted(chunks, key=lambda c: id_rank.get(c.id, 9999))
    return attach_context_content_all(chunks)


async def search_permanent_chunks_lexical(
    db: AsyncSession,
    query: str,
    document_id: str | None = None,
    domain: str | None = None,
    category: str | None = None,
    doc_name: str | None = None,
    limit: int = 5,
) -> list[DocumentChunk]:
    where_clauses = [
        "(c.search_vector @@ websearch_to_tsquery('simple', :query) OR c.section_id % :query OR c.doc_name ILIKE :query_like)"
    ]
    params: dict = {
        "query": query,
        "query_like": f"%{query}%",
        "limit": limit,
    }

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
               (
                   ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', :query)) +
                   COALESCE(similarity(c.section_id, :query), 0) +
                   CASE WHEN c.doc_name ILIKE :query_like THEN 1.0 ELSE 0.0 END
               ) AS lexical_score
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

    chunks = sorted(chunks, key=lambda c: id_rank.get(c.id, 9999))
    return attach_context_content_all(chunks)


# ──────────────────────────────────────────────
# Hybrid RAG
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
    merged = [item["chunk"] for item in sorted_items[:limit]]

    return attach_context_content_all(merged)


async def hybrid_search_temp_chunks(
    db: AsyncSession,
    query: str,
    spec_guid=None,
    org_id: str = "",
    limit: int = 5,
) -> list[TempDocumentChunk]:
    dense_vec = await get_embedding(query)

    dense_results = await search_temp_chunks(
        db,
        dense_vec,
        spec_guid,
        org_id,
        limit=limit * 2,
    )
    lexical_results = await search_temp_chunks_lexical(
        db,
        query,
        spec_guid,
        org_id,
        limit=limit * 2,
    )

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

    dense_results = await search_permanent_chunks(
        db,
        dense_vec,
        document_id=document_id,
        domain=domain,
        category=category,
        doc_name=doc_name,
        limit=limit * 2,
    )
    lexical_results = await search_permanent_chunks_lexical(
        db,
        query,
        document_id=document_id,
        domain=domain,
        category=category,
        doc_name=doc_name,
        limit=limit * 2,
    )

    return _rrf_merge(dense_results, lexical_results, limit=limit)


# ──────────────────────────────────────────────
# Reranker
# ──────────────────────────────────────────────

async def rerank_chunks(query: str, chunks: list, limit: int) -> list:
    return await _rerank(query, chunks, limit)


async def _rerank(query: str, chunks: list, limit: int) -> list:
    if not chunks:
        return chunks

    chunks = attach_context_content_all(chunks)

    model = _get_reranker()
    if model is None:
        return chunks[:limit]

    # table chunk는 table_markdown 기반으로 rerank한다.
    # text chunk는 content 기반.
    pairs = [
        [query, get_chunk_context_content(chunk)]
        for chunk in chunks
    ]

    def _score() -> list[float]:
        return model.compute_score(pairs, normalize=True)

    try:
        scores = await asyncio.to_thread(_score)
    except Exception as e:
        logging.warning("[vector_service] reranker 오류, RRF 순서 유지: %s", e)
        return chunks[:limit]

    ranked = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
    ranked_chunks = [chunk for _, chunk in ranked[:limit]]

    return attach_context_content_all(ranked_chunks)


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

    dense_results = await search_permanent_chunks(
        db,
        dense_vec,
        document_id=document_id,
        domain=domain,
        category=category,
        doc_name=doc_name,
        limit=rrf_limit * 2,
    )
    lexical_results = await search_permanent_chunks_lexical(
        db,
        query,
        document_id=document_id,
        domain=domain,
        category=category,
        doc_name=doc_name,
        limit=rrf_limit * 2,
    )

    rrf_results = _rrf_merge(dense_results, lexical_results, limit=rrf_limit)
    return await _rerank(query, rrf_results, limit=final_limit)


async def hybrid_search_temp_chunks_with_rerank(
    db: AsyncSession,
    query: str,
    spec_guid=None,
    org_id: str = "",
    rrf_limit: int = 20,
    final_limit: int = 5,
) -> list[TempDocumentChunk]:
    dense_vec = await get_embedding(query)

    dense_results = await search_temp_chunks(
        db,
        dense_vec,
        spec_guid,
        org_id,
        limit=rrf_limit * 2,
    )
    lexical_results = await search_temp_chunks_lexical(
        db,
        query,
        spec_guid,
        org_id,
        limit=rrf_limit * 2,
    )

    rrf_results = _rrf_merge(dense_results, lexical_results, limit=rrf_limit)
    return await _rerank(query, rrf_results, limit=final_limit)


async def get_dictionary_chunks(db: AsyncSession, domain: str) -> list[DocumentChunk]:
    query = (
        select(DocumentChunk)
        .filter(
            DocumentChunk.domain == domain,
            DocumentChunk.category == "dictionary",
        )
        .order_by(DocumentChunk.section_id)
    )
    result = await db.execute(query)
    chunks = result.scalars().all()
    return attach_context_content_all(chunks)
