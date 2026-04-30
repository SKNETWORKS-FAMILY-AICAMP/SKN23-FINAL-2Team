"""
File    : backend/core/config.py
Author  : 김지우
Create  : 2026-04-07
Description : 환경변수 기반 애플리케이션 설정

Modification History :
    - 2026-04-07 (김지우) : .env 기반 DB 설정 및 연결 URL 조합 초기 구성
    - 2026-04-08 (김지우) : DATABASE_URL override 및 부가 설정 추가
    - 2026-04-08 (김민정) : vLLM 서버 및 SSH 터널 설정 추가
    - 2026-04-14 (양창일) : DB_HOST DB_PORT DB_NAME DB_USER 기반 연결을 우선 사용하도록 수정
    - 2026-04-15 (김지우) : RDS 배포 환경 대응 및 Langfuse 변수 충돌 원천 차단
    - 2026-04-23 (김지우) : CAD_JSON_DEBUG — Parser/Compliance JSON 키·샘플 추적
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트 경로 계산
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# .env 파일 강제 로드
if (ROOT_DIR / ".env").exists():
    load_dotenv(ROOT_DIR / ".env", override=True)
    print(f" [Config] .env loaded from {ROOT_DIR / '.env'}")
else:
    print(f" [Config] .env NOT FOUND at {ROOT_DIR / '.env'}")

class Settings(BaseSettings):
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "postgres"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_S3_BUCKET_NAME: str | None = None
    AWS_REGION: str = "ap-northeast-2"
    AWS_KMS_KEY_ID: str | None = None
    # S3(버킷 예: cadsllm-storage) — 코드: backend/utils/s3_manager.get_s3_key
    #  - 테넌트 CAD: org/{org_id}/{device_id}/cad/raw|analyzed/{session_id}.json
    #  - 조직 시방: org/{org_id}/spec/{domain}/*
    #  - 공통 표준 standards/{arch|elec|fire|pipe}/spec|standard/ 는 별도 동기화·미구현

    VLLM_SERVER_URL: str = "http://localhost:8000/v1"
    VLLM_API_KEY: str = "token-unused"
    VLLM_STATS_ENABLED: bool = True

    LLM_ENDPOINT_URL: str = "http://localhost:8000"
    LLM_API_KEY: str = "token-unused"
    LLM_MODEL_NAME: str = "qwen3.5-27b-qlora"

    USE_SSH_TUNNEL: bool = False
    SSH_HOST: str = ""
    SSH_PORT: int = 22
    SSH_USER: str = ""
    SSH_KEY_PATH: str = ""

    # Redis 설정
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None

    # RunPod 서버리스 설정
    RUNPOD_ENDPOINT_ID: str = ""
    RUNPOD_API_KEY: str = ""

    # OpenAI 설정 추가 (테스트용)
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL_NAME: str = "gpt-4o-mini"

    # 배관 검토: 휴리스틱 unknown 레이어를 MEP 검토 풀에 합칠지(False면 오검↓·누락↑)
    PIPING_REVIEW_INCLUDE_UNKNOWN: bool = True
    PIPING_SPATIAL_HINTS_MAX: int = 32
    NCS_DISCIPLINE_MEP_PREFIX: bool = True

    # 도면 JSON 파싱·규정 LLM JSON 응답 디버그 (true 시 INFO 로그에 키/샘플 출력)
    CAD_JSON_DEBUG: bool = False

    # DEV ONLY: 개발 중에는 라이선스/API Key/조직 소유권 검사를 우회한다.
    # 운영 배포 전 반드시 .env에서 AUTH_SECURITY_ENABLED=true 로 되돌릴 것.
    AUTH_SECURITY_ENABLED: bool = False

    # Qwen3-Reranker-0.6B — 비어 있으면 프로젝트 루트 models/Qwen__Qwen3-Reranker-0.6B 를 시도 후 HF id
    QWEN3_RERANKER_LOCAL_PATH: str | None = None
    # True: 캐시/로컬 폴만(기본). False: 허브에서 내려받기 허용(로컬 테스트·첫 셋업)
    RERANKER_HF_OFFLINE: bool = True

    # RAG: 시방 PDF 앞부목·목차 표가 질의어(배관·자재·시공 등)와 잘 겹쳐 상위에만 걸리는 것 완화
    RAG_FILTER_TOC_HEURISTIC: bool = True
    RAG_QUERY_PREFETCH_CAP: int = 64

    @property
    def REDIS_URL(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding='utf-8', 
        extra="ignore"
    )

settings = Settings()

print(f" [Config] Target Database Host: {settings.DB_HOST}:{settings.DB_PORT}")
