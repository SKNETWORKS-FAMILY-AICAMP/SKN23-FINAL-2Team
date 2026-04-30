"""
File    : backend/core/database.py
Author  : 김지우
Create  : 2026-04-07
Description : SQLAlchemy 설정 및 DB 세션 생성기

Modification History :
    - 2026-04-07 (김지우) : DB 연결 및 개설
    - 2026-04-07 (김민정) : SSH 터널링 기능 추가 및 로컬 개발 환경 대응
    - 2026-04-17 (김지우) : 동기 세션을 비동기로 전환 및 URL 파싱 순서 수정
"""
import os

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from backend.core.config import settings

# --- SSH 터널링 초기화 (API-05 연동 및 로컬 개발 대응) ---
tunnel = None
# 1. 기본 DB URL을 먼저 가져옵니다.
db_url = settings.DATABASE_URL

# USE_SSH_TUNNEL=True
if settings.USE_SSH_TUNNEL:
    from sshtunnel import SSHTunnelForwarder

    # SSH 키 파일 존재 여부 확인
    if not os.path.exists(settings.SSH_KEY_PATH):
        print(f"SSH 키 파일을 찾을 수 없습니다: {settings.SSH_KEY_PATH}")
    else:
        try:
            tunnel = SSHTunnelForwarder(
                (settings.SSH_HOST, settings.SSH_PORT),
                ssh_username=settings.SSH_USER,
                ssh_pkey=settings.SSH_KEY_PATH,
                remote_bind_address=(settings.DB_HOST, settings.DB_PORT),
                local_bind_address=('127.0.0.1', 0)  # 가용한 포트 자동 할당
            )
            tunnel.start()

            # 터널을 통한 새로운 DB 접속 URL 생성
            db_url = (
                f"postgresql://{settings.DB_USER}:{settings.DB_PASSWORD}@"
                f"127.0.0.1:{tunnel.local_bind_port}/{settings.DB_NAME}"
            )
            print(
                f"SSH 터널 활성화: 127.0.0.1:{tunnel.local_bind_port} -> "
                f"{settings.DB_HOST}:{settings.DB_PORT}"
            )
        except Exception as e:
            print(f"SSH 터널 시작 실패: {e}")

# 2. SSH 터널링이 모두 끝난 최종 db_url을 비동기(asyncpg)용으로 변환합니다.
async_db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")

# 3. 비동기 엔진(통로) 생성
engine = create_async_engine(async_db_url, echo=False)

# 4. 비동기 세션 설정
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession
)

# 5. DB 모델들의 부모 클래스
Base = declarative_base()

# 6. FastAPI에서 DB 세션을 안전하게 사용하기 위한 비동기 제너레이터
async def get_db():
    async with SessionLocal() as db:
        try:
            yield db
        finally:
            await db.close()