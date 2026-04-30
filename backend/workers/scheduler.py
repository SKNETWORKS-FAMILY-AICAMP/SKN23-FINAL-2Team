"""
File    : backend/workers/scheduler.py
Author  : 김지우
Create  : 2026-04-13
Description : 트리거 관리

Modification History :
    - 2026-04-13 (김지우) : 트리거 관리
    - 2026-04-21 (김지우) : AsyncSession 대응 — 토큰 리셋·만료 시방서 삭제를 async job으로 전환
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from backend.services.storage_service import cleanup_expired_s3_objects
from backend.services import document_service
import logging
from sqlalchemy import text
from backend.core.database import SessionLocal

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def reset_daily_tokens():
    """매일 자정(00:00)에 모든 고객사의 잔여 토큰 한도를 초기화"""
    try:
        async with SessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE organizations SET remaining_daily_tokens = daily_token_limit "
                    "WHERE daily_token_limit IS NOT NULL AND is_active = true"
                )
            )
            await db.commit()
            logger.info("자정 스케줄러: 모든 기업의 일일 토큰 한도가 리셋되었습니다.")
    except Exception as e:
        logger.error(f"자정 스케줄러 토큰 리셋 실패: {e}")


def setup_scheduler():
    scheduler.add_job(
        cleanup_expired_s3_objects,
        IntervalTrigger(hours=1),
        id="cleanup_expired_cad_s3",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )

    scheduler.add_job(
        reset_daily_tokens,
        CronTrigger(hour=0, minute=0, timezone="Asia/Seoul"),
        id="reset_daily_tokens_at_midnight",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    scheduler.add_job(
        _cleanup_expired_temp_docs,
        CronTrigger(hour=1, minute=0, timezone="Asia/Seoul"),
        id="cleanup_expired_temp_docs",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    return scheduler


async def _cleanup_expired_temp_docs():
    """만료된 임시 문서(temp_documents)와 청크를 일괄 삭제합니다."""
    try:
        async with SessionLocal() as db:
            deleted = await document_service.delete_expired_documents(db)
            logger.info(f"만료 시방서 삭제 완료: {deleted}건")
    except Exception as exc:
        logger.error(f"만료 시방서 삭제 실패: {exc}")

