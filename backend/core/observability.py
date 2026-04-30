"""
Langfuse 인프라 모듈
- 싱글턴 클라이언트
- sub-agent 성능 비교용 트래킹 데코레이터
"""

import os
import time
from functools import wraps
from dotenv import load_dotenv
from langfuse import Langfuse

# .env 명시적 로드 (os.getenv가 pydantic-settings보다 먼저 실행될 수 있음)
load_dotenv()

# ── 싱글턴 클라이언트 ──
langfuse_client = Langfuse(
    host=os.getenv("LANGFUSE_HOST", "http://localhost:3000"),
    public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
    secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
)


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