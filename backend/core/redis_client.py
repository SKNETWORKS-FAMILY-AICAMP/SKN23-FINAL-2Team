#Redis 인스턴스 및 공통 캐시 로직 (인프라)
#backend/core/redis.py
import redis.asyncio as redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from backend.core.config import settings

# Windows ERROR_NETNAME_DELETED(64) 대응:
#   - health_check_interval=0 : on_connect 무한재귀(C-stack overflow) 방지
#   - socket_keepalive=True   : 유휴 TCP 연결을 Windows가 끊기 전에 keepalive probe 전송
#   - retry                   : ConnectionError/TimeoutError 발생 시 지수 백오프로 3회 재시도
redis_pool = redis.ConnectionPool.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    health_check_interval=0,
    socket_connect_timeout=10,
    socket_timeout=30,
    retry_on_timeout=True,
    socket_keepalive=True,
    retry=Retry(ExponentialBackoff(cap=2, base=0.1), retries=3),
    retry_on_error=[RedisConnectionError, RedisTimeoutError],
)

def get_redis_client():
    return redis.Redis(connection_pool=redis_pool)
