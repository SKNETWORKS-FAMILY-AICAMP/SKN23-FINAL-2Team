"""
Langfuse 인프라 모듈
- 싱글턴 클라이언트
- sub-agent 성능 비교용 트래킹 데코레이터
"""

import os
import time
import logging
from functools import wraps

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

try:
    from langfuse import Langfuse
except ImportError:
    Langfuse = None  # type: ignore[assignment]

# .env 명시적 로드 (os.getenv가 pydantic-settings보다 먼저 실행될 수 있음)
load_dotenv()

logger = logging.getLogger(__name__)

class _NoopLangfuseTrace:
    def generation(self, *args, **kwargs):
        return None

    def score(self, *args, **kwargs):
        return None

    def update(self, *args, **kwargs):
        return None


class _NoopLangfuseClient:
    def trace(self, *args, **kwargs):
        return _NoopLangfuseTrace()

    def flush(self, *args, **kwargs):
        return None


class _SafeLangfuseTrace:
    def __init__(self, trace):
        self._trace = trace

    def _call(self, method_name: str, *args, **kwargs):
        try:
            method = getattr(self._trace, method_name, None)
            if method is None:
                return None
            return method(*args, **kwargs)
        except Exception as exc:
            logger.warning("[Langfuse] trace.%s failed: %s", method_name, exc)
            return None

    def generation(self, *args, **kwargs):
        return self._call("generation", *args, **kwargs)

    def score(self, *args, **kwargs):
        return self._call("score", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._call("update", *args, **kwargs)


class _SafeLangfuseClient:
    def __init__(self, client):
        self._client = client

    def trace(self, *args, **kwargs):
        try:
            trace = self._client.trace(*args, **kwargs)
            if trace is None:
                return _NoopLangfuseTrace()
            return _SafeLangfuseTrace(trace)
        except Exception as exc:
            logger.warning("[Langfuse] trace creation failed: %s", exc)
            return _NoopLangfuseTrace()

    def flush(self, *args, **kwargs):
        try:
            return self._client.flush(*args, **kwargs)
        except Exception as exc:
            logger.warning("[Langfuse] flush failed: %s", exc)
            return None


def _build_langfuse_client():
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    host = os.getenv("LANGFUSE_HOST", "http://localhost:3000").strip() or "http://localhost:3000"
    if not public_key or not secret_key or Langfuse is None:
        return _NoopLangfuseClient()
    try:
        return _SafeLangfuseClient(
            Langfuse(host=host, public_key=public_key, secret_key=secret_key)
        )
    except Exception as exc:
        logger.warning("[Langfuse] client initialization failed: %s", exc)
        return _NoopLangfuseClient()


# ── 싱글턴 클라이언트 ──
langfuse_client = _build_langfuse_client()


# ── sub-agent 트래킹 데코레이터 ──
def track_sub_agent(domain: str, sub_agent_type: str):
    """
    도메인별 sub-agent 성능 비교용 데코레이터.

    사용법:
        @track_sub_agent(domain="elec", sub_agent_type="legal_search")
        async def search(self, request): ...

    Langfuse에 기록되는 항목:
        - trace: {domain}/{sub_agent_type}
        - score: latency, confidence
        - metadata: domain, sub_agent, latency_s, result_count
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.time()
            trace = langfuse_client.trace(
                name=f"{domain}/{sub_agent_type}",
                metadata={"domain": domain, "sub_agent": sub_agent_type},
                tags=[domain, sub_agent_type],
            )
            try:
                result = await func(*args, **kwargs)
                latency = time.time() - start

                trace.generation(
                    name=f"{sub_agent_type}_call",
                    metadata={
                        "domain": domain,
                        "latency_s": round(latency, 3),
                        "confidence": getattr(result, "confidence", None),
                        "result_count": len(getattr(result, "results", [])),
                    },
                )
                trace.score(name="latency", value=latency)
                if hasattr(result, "confidence"):
                    trace.score(name="confidence", value=result.confidence)

                return result
            except Exception as e:
                trace.score(name="error", value=1)
                trace.update(metadata={"error": str(e)})
                raise
        return wrapper
    return decorator
