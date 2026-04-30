"""
File    : backend/api/routers/license_api.py
Description : API Key(License) 검증 및 디바이스 자동 등록 라우터
"""

import os
import platform
import socket
import uuid as _uuid
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from backend.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# ── 서버 시작 시 1회만 읽어서 캐싱 ────────────────────────────────────
_CACHED_MACHINE_GUID: str | None = None


def _get_local_machine_guid() -> str:
    """Windows 레지스트리에서 MachineGuid를 읽는다 (winreg 직접 사용)."""
    global _CACHED_MACHINE_GUID
    if _CACHED_MACHINE_GUID:
        return _CACHED_MACHINE_GUID

    if platform.system() == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
            _CACHED_MACHINE_GUID = str(value)
            return _CACHED_MACHINE_GUID
        except Exception as e:
            print(f"[License] winreg 실패: {e}")
    # Linux / Mac 폴백
    _CACHED_MACHINE_GUID = f"{_uuid.getnode():012x}"
    return _CACHED_MACHINE_GUID


def _get_local_hostname() -> str:
    return socket.gethostname()


def _get_local_os_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME", os.environ.get("USER", "unknown"))


async def _upsert_device(
    db: AsyncSession,
    license_id: str,
    machine_id: str,
    hostname: str | None,
    os_user: str | None,
) -> str:
    """machine_id 기준 devices upsert. 있으면 갱신, 없으면 INSERT."""
    result = await db.execute(
        text("SELECT CAST(id AS text) AS id FROM devices WHERE machine_id = :mid LIMIT 1"),
        {"mid": machine_id},
    )
    row = result.mappings().first()

    if row:
        device_id = str(row["id"])
        # hostname / os_user 를 NULL 로 덮어쓰지 않음(요청에 빠지면 None → 기존 행 보존)
        h = (hostname or "").strip() or None
        u = (os_user or "").strip() or None
        if h and u:
            await db.execute(
                text(
                    "UPDATE devices SET last_seen = NOW(), hostname = :h, os_user = :u, "
                    "display_name = :dn WHERE id = CAST(:did AS uuid)"
                ),
                {"h": h, "u": u, "dn": h, "did": device_id},
            )
        elif h:
            await db.execute(
                text(
                    "UPDATE devices SET last_seen = NOW(), hostname = :h, display_name = :dn "
                    "WHERE id = CAST(:did AS uuid)"
                ),
                {"h": h, "dn": h, "did": device_id},
            )
        elif u:
            await db.execute(
                text("UPDATE devices SET last_seen = NOW(), os_user = :u WHERE id = CAST(:did AS uuid)"),
                {"u": u, "did": device_id},
            )
        else:
            await db.execute(
                text("UPDATE devices SET last_seen = NOW() WHERE id = CAST(:did AS uuid)"),
                {"did": device_id},
            )
    else:
        device_id = str(_uuid.uuid4())
        display = hostname or machine_id
        await db.execute(
            text(
                """
                INSERT INTO devices
                    (id, license_id, machine_id, hostname, os_user, display_name,
                     is_active, last_seen)
                VALUES
                    (CAST(:id AS uuid), CAST(:lid AS uuid), :mid, :h, :u, :dn, true, NOW())
                """
            ),
            {
                "id":  device_id,
                "lid": license_id,
                "mid": machine_id,
                "h":   hostname,
                "u":   os_user,
                "dn":  display,
            },
        )

    await db.commit()
    return device_id


@router.get("/machine-info")
async def get_machine_info():
    """현재 서버(로컬) 머신의 식별 정보를 반환한다."""
    guid = _get_local_machine_guid()
    host = _get_local_hostname()
    user = _get_local_os_user()
    print(f"[License] machine-info → guid={guid}, host={host}, user={user}")
    return {"machine_id": guid, "hostname": host, "os_user": user}


@router.post("/verify")
async def verify_license(payload: dict, db: AsyncSession = Depends(get_db)):
    """API Key 검증. machine_id 포함 시 devices 자동 upsert."""
    api_key    = payload.get("api_key")
    machine_id = payload.get("machine_id", "").strip() or None
    hostname   = payload.get("hostname")  or None
    os_user    = payload.get("os_user")   or None

    # 프론트엔드에서 machine_id가 없거나 브라우저 폴백이면 서버에서 직접 수집
    if not machine_id or machine_id.startswith("browser-"):
        machine_id = _get_local_machine_guid()
        hostname   = hostname or _get_local_hostname()
        os_user    = os_user  or _get_local_os_user()

    print(f"[License] verify: api_key={api_key}, machine_id={machine_id}, hostname={hostname}")

    if not api_key:
        raise HTTPException(status_code=400, detail="API Key is required.")

    try:
        result = await db.execute(
            text(
                "SELECT CAST(l.id AS text) AS license_id, CAST(l.org_id AS text) AS org_id, "
                "o.plan, o.max_seats, o.company_name "
                "FROM licenses l "
                "JOIN organizations o ON o.id = l.org_id "
                "WHERE l.api_key = :key AND l.status = 'active' "
                "LIMIT 1"
            ),
            {"key": api_key},
        )
        row = result.mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail="유효하지 않은 API Key입니다.")

        license_id   = str(row["license_id"])
        org_id       = str(row["org_id"])
        plan         = row["plan"] or "basic"
        max_seats    = row["max_seats"] or 5
        company_name = row["company_name"] or ""

        device_id: str | None = None
        if machine_id:
            logger.info(f"device upsert 시작: license_id={license_id}, machine_id={machine_id}")
            device_id = await _upsert_device(db, license_id, machine_id, hostname, os_user)
            logger.info(f"device upsert 완료: device_id={device_id}")
        else:
            res2 = await db.execute(
                text(
                    "SELECT CAST(id AS text) AS id FROM devices "
                    "WHERE license_id = CAST(:lid AS uuid) AND is_active = true "
                    "ORDER BY last_seen DESC NULLS LAST LIMIT 1"
                ),
                {"lid": license_id},
            )
            r2 = res2.mappings().first()
            if r2:
                device_id = str(r2["id"])
                await db.execute(
                    text("UPDATE devices SET last_seen = NOW() WHERE id = CAST(:did AS uuid)"),
                    {"did": device_id},
                )
                await db.commit()

        return {
            "status":       "success",
            "message":      "인증 성공",
            "org_id":       org_id,
            "device_id":    device_id,
            "machine_id":   machine_id,
            "hostname":     hostname,
            "plan":         plan,
            "max_seats":    max_seats,
            "company_name": company_name,
        }

    except HTTPException:
        raise
    except OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        raise HTTPException(status_code=503, detail="DB 연결 실패.")
    except Exception as e:
        logger.error(f"License verification error: {e}")
        raise HTTPException(status_code=500, detail="서버 내부 오류.")


@router.get("/devices")
async def list_devices(org_id: str, db: AsyncSession = Depends(get_db)):
    """org_id 기준 디바이스 목록 반환."""
    if not org_id:
        raise HTTPException(status_code=400, detail="org_id가 필요합니다.")

    try:
        result = await db.execute(
            text(
                "SELECT CAST(d.id AS text) AS id, d.machine_id, d.display_name, d.hostname, d.os_user, "
                "       d.is_active, d.first_seen, d.last_seen "
                "FROM devices d "
                "JOIN licenses l ON l.id = d.license_id "
                "WHERE l.org_id = CAST(:oid AS uuid) "
                "ORDER BY d.first_seen DESC"
            ),
            {"oid": org_id},
        )
        rows = result.mappings().all()

        devices = []
        for r in rows:
            fs = r["first_seen"]
            ls = r["last_seen"]
            mid = (r["machine_id"] or "").strip()
            label = (r["display_name"] or r["hostname"] or "").strip()
            if not label and mid:
                label = f"PC-{mid[:8]}"
            if not label:
                label = "알 수 없음"
            devices.append({
                "id":           str(r["id"]),
                "machine_id":   mid,
                "display_name": label,
                "hostname":     r["hostname"]  or "",
                "os_user":      r["os_user"]   or "",
                "is_active":    r["is_active"],
                "first_seen":   fs.isoformat() if hasattr(fs, "isoformat") else str(fs or ""),
                "last_seen":    ls.isoformat() if hasattr(ls, "isoformat") else str(ls or ""),
            })
        return devices

    except Exception as e:
        logger.error(f"디바이스 목록 조회 오류: {e}")
        raise HTTPException(status_code=500, detail="서버 내부 오류.")


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, db: AsyncSession = Depends(get_db)):
    """디바이스 영구 삭제."""
    result = await db.execute(
        text("DELETE FROM devices WHERE id = CAST(:did AS uuid)"),
        {"did": device_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="디바이스를 찾을 수 없습니다.")
    return {"status": "success"}
