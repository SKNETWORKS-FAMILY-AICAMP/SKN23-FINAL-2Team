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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routers import (
    agent_api, document_api, health_api, 
    cad_interop, session_api, websocket,
    license_api
)
from backend.workers.scheduler import setup_scheduler
from backend.core.database import engine
from backend.startup.model_check import ensure_models_ready
from sqlalchemy import text

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module='paramiko')


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
    ]
    # skn23 시드: api_key='1234' → org(skn23) + license 보장
    # device는 API 등록 시 machine_id 기반 자동 생성
    # devices가 licenses.id를 FK로 참조할 수 있으므로 license 행은 DELETE하지 않고 upsert만 수행
    demo_seed = [
        "DELETE FROM devices WHERE machine_id LIKE 'browser-%'",
        """
        INSERT INTO organizations (id, company_name, admin_email, password_hash, plan, max_seats, is_active)
        VALUES ('00000000-0000-0000-0000-000000000001', 'skn23', 'admin@skn23.local', '', 'pro', 10, true)
        ON CONFLICT (id) DO UPDATE SET company_name = EXCLUDED.company_name, plan = 'pro', max_seats = 10
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

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
app.include_router(license_api.router, prefix="/api/v1/licenses", tags=["licenses"])

# 2. WebSocket 라우터 등록 (prefix 없이 직접 연결)
app.include_router(websocket.router)


