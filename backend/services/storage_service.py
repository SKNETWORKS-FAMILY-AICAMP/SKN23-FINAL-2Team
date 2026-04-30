import asyncio
import boto3
import logging
from datetime import datetime, timedelta, timezone
from backend.core.config import settings

logger = logging.getLogger(__name__)

def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION
    )

def _scan_and_delete_sync(bucket: str, threshold: datetime, prefixes: list[str] | None = None) -> int:
    """
    Bug 8: 동기 boto3 블로킹 I/O를 별도 함수로 분리.
    asyncio.to_thread()로 호출하여 이벤트 루프 블로킹을 방지합니다.
    """
    s3 = get_s3_client()
    expired = []
    paginator = s3.get_paginator("list_objects_v2")
    scan_prefixes = prefixes or [""]
    for prefix in scan_prefixes:
        kwargs = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = prefix
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key: str = obj["Key"]
                if "/cad/raw/" in key or "/cad/analyzed/" in key:
                    if obj["LastModified"] < threshold:
                        expired.append({"Key": key})

    if expired:
        # delete_objects 는 최대 1000개씩 처리
        for i in range(0, len(expired), 1000):
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": expired[i:i + 1000]},
            )
        return len(expired)
    return 0

async def cleanup_expired_s3_objects():
    """만료된 CAD 원본·분석 결과 파기 로직 (3일 TTL).

    대상 경로 패턴:
      - {org_id}/{device_id}/cad/raw/…
      - {org_id}/{device_id}/cad/analyzed/…

    Bug 8 수정: boto3 동기 블로킹 호출을 asyncio.to_thread()로 래핑하여
    FastAPI 이벤트 루프 블로킹을 방지합니다.
    """
    bucket = settings.AWS_S3_BUCKET_NAME
    if not bucket:
        logger.info("[StorageScheduler] AWS_S3_BUCKET_NAME 미설정 — CAD S3 cleanup 건너뜀")
        return 0
    threshold = datetime.now(timezone.utc) - timedelta(days=3)
    # 현재 키 구조가 org/{org_id}/{device_id}/cad/... 이므로 org/ prefix로 불필요한 전역 스캔을 줄입니다.
    return await asyncio.to_thread(_scan_and_delete_sync, bucket, threshold, ["org/"])
