"""
File    : backend/services/health_service.py
Author  : 김민정
WBS     : API-05
Create  : 2026-04-07
Description : DB, vLLM(런팟) 서버의 상태를 점검하는 서비스 레이어

Modification History :
    - 2026-04-07 (김민정) : DB, vLLM 서버 상태 확인
"""
import anyio
import httpx
import time
from sqlalchemy import text
from backend.core.database import engine
from backend.core.config import settings

class HealthService:
    @staticmethod
    async def check_db_health():
        """데이터베이스 연결 상태 확인 (동기 엔진을 비동기 스레드에서 실행)"""
        def _ping_db():
            # SQLAlchemy 2.0 동기 연결 테스트
            conn = engine.connect()
            try:
                conn.execute(text("SELECT 1"))
            finally:
                conn.close()

        try:
            start_time = time.time()
            import asyncio
            # anyio.to_thread.run_sync를 사용하여 동기 DB 작업을 별도 스레드에서 실행
            # SSH 터널 환경 고려하여 타임아웃을 10초로 연장
            await asyncio.wait_for(anyio.to_thread.run_sync(_ping_db), timeout=10.0)
            latency = (time.time() - start_time) * 1000
            return {"status": "ok", "latency_ms": round(latency, 2)}
        except asyncio.TimeoutError:
            return {"status": "error", "message": "DB connection timed out (10s)"}
        except Exception as e:
            # 에러 타입을 포함하여 상세하게 리턴하여 원인 파악
            return {"status": "error", "message": f"{type(e).__name__}: {str(e)}"}

    @staticmethod
    async def check_vllm_health():
        """vLLM 서버 상태 (Basic + Detailed 하이브리드) 확인"""
        vllm_url = settings.VLLM_SERVER_URL.rstrip("/")
        
        # 1. 상세 모니터링 시도 (/stats 또는 /metrics)
        if settings.VLLM_STATS_ENABLED:
            detailed_info = await HealthService._get_vllm_detailed_stats(vllm_url)
            if detailed_info:
                return detailed_info

        # 2. 기본 연결성 확인 (/v1/models)
        return await HealthService._get_vllm_basic_health(vllm_url)

    @staticmethod
    async def _get_vllm_detailed_stats(vllm_url: str):
        """vLLM 상세 지표(/stats) 호출 시도"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # vLLM의 /stats 또는 /metrics 확인 (vLLM 버전마다 다를 수 있음)
                response = await client.get(f"{vllm_url}/stats")
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "status": "ready",
                        "mode": "detailed",
                        "gpu_stats": data, # vLLM이 직접 제공하는 stats 정보
                        "message": "Fetched from vLLM /stats"
                    }
        except Exception:
            # 실패 시 조용히 Basic으로 넘어가기 위해 None 반환
            return None

    @staticmethod
    async def _get_vllm_basic_health(vllm_url: str):
        """vLLM 연결 및 모델 로드 기본 상태 확인"""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                start_time = time.time()
                # 표준 OpenAI 호환 엔드포인트인 /v1/models 확인
                headers = {"Authorization": f"Bearer {settings.VLLM_API_KEY}"}
                response = await client.get(f"{vllm_url}/v1/models", headers=headers)
                latency = (time.time() - start_time) * 1000

                if response.status_code == 200:
                    models_data = response.json()
                    model_names = [m["id"] for m in models_data.get("data", [])]
                    return {
                        "status": "ready",
                        "mode": "basic",
                        "latency_ms": round(latency, 2),
                        "loaded_models": model_names,
                        "message": "Connected to vLLM via /v1/models"
                    }
                else:
                    return {
                        "status": "loading",
                        "mode": "basic",
                        "http_code": response.status_code,
                        "message": "vLLM server is up but models may not be ready"
                    }
        except httpx.ConnectError:
            return {"status": "offline", "message": "Could not connect to vLLM server"}
        except Exception as e:
            return {"status": "error", "message": f"Unexpected vLLM check error: {str(e)}"}