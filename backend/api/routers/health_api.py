"""
File    : backend/api/routers/health_api.py
Author  : 김민정
WBS     : API-05
Create  : 2026-04-07
Description : 서버 상태 확인 API

Modification History :
    - 2026-04-07 (김민정) : 
"""
from fastapi import APIRouter
from backend.services.health_service import HealthService

router = APIRouter()

@router.get("/health", tags=["health"])
async def get_server_health():
    """서버 상태 확인 (DB, vLLM 모델 포함)"""
    db_health = await HealthService.check_db_health()
    vllm_health = await HealthService.check_vllm_health()
    
    # 전체 상태 판단
    is_fully_healthy = (db_health["status"] == "ok") and (vllm_health["status"] == "ready")
    overall_status = "ok" if is_fully_healthy else "degraded"

    return {
        "status": overall_status,
        "database": db_health,
        "vllm": vllm_health,
        "api_version": "v1.0"
    }