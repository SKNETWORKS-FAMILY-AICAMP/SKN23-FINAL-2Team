"""
File    : backend/app.py
Author  : 김지우
Create  : 2026-04-06
Description : FastAPI 앱 생성 및 API 라우터 등록

Modification History :
    - 2026-04-06 (김지우) : 초기 구조 생성
    - 2026-04-08 (양창일) : 라우터 등록 구조 및 주석 정리
    - 2026-04-08 (김민정) : health_api 추가
    - 2026-04-13 (김다빈) : WebSocket 릴레이 구현, import 정리
    - 2026-04-13 (양창일) : session_api 등록 및 머지 충돌 정리
    - 2026-04-15 (김지우) : WebSocket 라우터 등록
    - 2026-04-17 (김지우) : redis 서버 연결 확인 테스트 코드 추가
"""
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routers import (
    agent_api, document_api, health_api,
    cad_interop, session_api, websocket,
    license_api,
    drawing_state_router,
    plugin_api,
)
from backend.workers.scheduler import setup_scheduler
from backend.core.database import engine
from backend.startup.model_check import ensure_models_ready
from sqlalchemy import text

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module='paramiko')


def _app_env() -> str:
    return (
        os.getenv("APP_ENV")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
        or "development"
    ).lower()


def _is_production() -> bool:
    return _app_env() in {"prod", "production"}


def _demo_seed_enabled() -> bool:
    configured = os.getenv("ENABLE_DEMO_SEED")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes"}
    return not _is_production()


async def _run_migrations():
    """앱 시작 시 필요한 DB 스키마 패치를 적용합니다."""
    schema_migrations = [
        "ALTER TABLE chat_sessions ALTER COLUMN device_id DROP NOT NULL",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS org_id UUID REFERENCES organizations(id) ON DELETE CASCADE",
        "CREATE INDEX IF NOT EXISTS idx_chat_sessions_org_id ON chat_sessions(org_id)",
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS hostname TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS os_user TEXT",
        "ALTER TABLE devices ADD COLUMN IF NOT EXISTS display_name TEXT",
        "ALTER TYPE fixstatus ADD VALUE IF NOT EXISTS 'FAILED'",
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'temp_document_chunks' AND column_name = 'section_id'
            ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'temp_document_chunks' AND column_name = 'session_id'
            ) THEN
                ALTER TABLE temp_document_chunks RENAME COLUMN section_id TO session_id;
            ELSIF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'temp_document_chunks' AND column_name = 'section_id'
            ) AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'temp_document_chunks' AND column_name = 'session_id'
            ) THEN
                UPDATE temp_document_chunks
                SET session_id = COALESCE(session_id, section_id::text)
                WHERE session_id IS NULL;
            END IF;
        END $$;
        """,
        "ALTER TABLE temp_document_chunks ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE temp_document_chunks ADD COLUMN IF NOT EXISTS chunk_type TEXT",
        "ALTER TABLE temp_document_chunks ADD COLUMN IF NOT EXISTS search_vector TSVECTOR",
        "ALTER TABLE temp_document_chunks ADD COLUMN IF NOT EXISTS table_markdown TEXT",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE INDEX IF NOT EXISTS idx_temp_document_chunks_session_trgm ON temp_document_chunks USING gin (session_id gin_trgm_ops)",
    ]
    demo_seed = []
    if _demo_seed_enabled():
        # 개발 환경에서만 skn23 테스트 라이선스를 생성합니다.
        demo_seed = [
            "DELETE FROM devices WHERE machine_id LIKE 'browser-%'",
            """
            INSERT INTO organizations (id, company_name, admin_email, password_hash, plan, max_seats, is_active)
            VALUES ('00000000-0000-0000-0000-000000000001', 'skn23', 'admin@skn23.local', '', 'Pro', 10, true)
            ON CONFLICT (id) DO UPDATE SET company_name = EXCLUDED.company_name, plan = 'Pro', max_seats = 10
            """,
            """
            INSERT INTO licenses (id, org_id, api_key, status)
            VALUES ('00000000-0000-0000-0000-000000000002',
                    '00000000-0000-0000-0000-000000000001',
                    '1234', 'active')
            ON CONFLICT (id) DO UPDATE SET
                org_id = EXCLUDED.org_id,
                api_key = EXCLUDED.api_key,
                status = EXCLUDED.status
            """,
        ]
    # 각 SQL을 독립 트랜잭션으로 실행 (하나 실패해도 나머지 진행)
    for sql in schema_migrations + demo_seed:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception as e:
            print(f"[Migration] 무시: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ensure_models_ready)
    await _run_migrations()
    sc = setup_scheduler()
    sc.start()
    yield
    sc.shutdown()

app = FastAPI(title="AutoCAD sLLM Agent API", lifespan=lifespan)

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]


def _cors_origins() -> list[str]:
    configured = os.getenv("CORS_ORIGINS", "")
    if not configured.strip():
        if not _is_production():
            return ["*"]
        return DEFAULT_CORS_ORIGINS
    return [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]


# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. REST API 라우터 등록
app.include_router(document_api.router, prefix="/api/v1/documents", tags=["documents"])
app.include_router(agent_api.router, prefix="/api/v1", tags=["agents"])
app.include_router(session_api.router, prefix="/api/v1", tags=["sessions"])
app.include_router(health_api.router, prefix="/api/v1", tags=["health"])
app.include_router(cad_interop.router, prefix="/api/v1/cad", tags=["cad"])
app.include_router(drawing_state_router.router, prefix="/api/v1/cad/drawings", tags=["drawing-state"])
app.include_router(license_api.router, prefix="/api/v1/licenses", tags=["licenses"])
app.include_router(plugin_api.router, prefix="/api/v1/plugin", tags=["plugin"])

# 2. WebSocket 라우터 등록 (prefix 없이 직접 연결)
app.include_router(websocket.router)


