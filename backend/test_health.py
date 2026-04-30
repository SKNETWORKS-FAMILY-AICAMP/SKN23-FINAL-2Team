"""
File    : backend/test_health.py
Author  : 김민정
WBS     : TEST-01
Create  : 2026-04-07
Description : HealthService 기능 통합 점검 스크립트 (DB & vLLM 연결 확인)

Modification History :
    - 2026-04-07 (김민정) : 초기 생성 및 HealthService 연동 테스트 로직 구현
"""
import asyncio
import os
import sys

# 프로젝트 루트를 path에 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.services.health_service import HealthService

async def run_functional_check():
    print("Starting functional HealthService check...")
    
    print("\n--- [1] Database Health Check ---")
    db_status = await HealthService.check_db_health()
    print(f"Result: {db_status}")
    
    print("\n--- [2] vLLM Health Check ---")
    vllm_status = await HealthService.check_vllm_health()
    print(f"Result: {vllm_status}")
    
    print("\nCheck finished.")

if __name__ == "__main__":
    asyncio.run(run_functional_check())
