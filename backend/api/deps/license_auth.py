"""
File    : backend/api/deps/license_auth.py
Description : licenses.api_key → org_id 해석. X-API-Key 또는 Authorization: Bearer
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import settings
from backend.core.database import get_db

DEV_ORG_ID = "00000000-0000-0000-0000-000000000001"


async def get_authenticated_org_id(
    db: AsyncSession = Depends(get_db),
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
) -> str:
    # DEV ONLY: 개발 단계에서는 API Key 보안 검사를 잠시 우회한다.
    # 운영 배포 전 AUTH_SECURITY_ENABLED=true 로 바꾸면 아래 실제 검증 로직이 다시 활성화된다.
    if not settings.AUTH_SECURITY_ENABLED:
        return DEV_ORG_ID

    api_key = (x_api_key or "").strip()
    if not api_key and authorization:
        auth = authorization.strip()
        if auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="API Key가 필요합니다.")

    result = await db.execute(
        text(
            "SELECT CAST(l.org_id AS text) AS org_id FROM licenses l "
            "WHERE l.api_key = :key AND l.status = 'active' LIMIT 1"
        ),
        {"key": api_key},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="유효하지 않은 API Key입니다.")
    return str(row["org_id"])


def require_same_org(requested_org_id: str, authenticated_org_id: str) -> None:
    # DEV ONLY: 개발 단계에서는 조직 불일치 차단을 잠시 우회한다.
    if not settings.AUTH_SECURITY_ENABLED:
        return

    if requested_org_id != authenticated_org_id:
        raise HTTPException(
            status_code=403,
            detail="org_id가 라이선스 조직과 일치하지 않습니다.",
        )


async def require_session_org(
    db: AsyncSession,
    session_id: str,
    authenticated_org_id: str,
) -> None:
    """chat_session이 인증된 조직에 속하는지 확인합니다.

    신규 세션은 chat_sessions.org_id를 기준으로 검증하고, 과거 세션은
    device_id → licenses.org_id 경로로 한 번 더 확인합니다.
    """
    # DEV ONLY: 개발 단계에서는 세션 소유권 검사를 잠시 우회한다.
    if not settings.AUTH_SECURITY_ENABLED:
        return

    result = await db.execute(
        text(
            """
            SELECT
                CAST(cs.org_id AS text) AS session_org_id,
                CAST(l.org_id AS text) AS device_org_id
            FROM chat_sessions cs
            LEFT JOIN devices d ON d.id = cs.device_id
            LEFT JOIN licenses l ON l.id = d.license_id
            WHERE cs.id = :sid
            LIMIT 1
            """
        ),
        {"sid": session_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")

    owner_org = row.get("session_org_id") or row.get("device_org_id")
    if not owner_org:
        raise HTTPException(status_code=403, detail="세션 조직 정보를 확인할 수 없습니다.")
    require_same_org(str(owner_org), authenticated_org_id)
