# Backend Dockerfile — 빌드 시 compose에서 context: 레포 루트, dockerfile: backend/Dockerfile 로 사용
# 담당: 송주엽 (INF-01)
#
# CPU 전용 이미지 (기본). GPU EC2 인스턴스는 backend/Dockerfile.gpu 사용.
# AI 모델(BGE-M3, Qwen3-Reranker)은 도커 볼륨(model_cache)으로 주입.
# 실행 전 scripts/download_models.py 로 호스트에서 한 번 다운로드 필요.

FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# HuggingFace 모델 캐시 경로 — docker-compose에서 ./models 볼륨을 /app/models 에 마운트
ENV HF_HOME=/app/models
ENV HF_HUB_OFFLINE=1
ENV TRANSFORMERS_OFFLINE=1

# 모델 디렉터리 생성 (볼륨 마운트 전 기본 경로 보장)
RUN mkdir -p /app/models

RUN pip install --no-cache-dir uv

COPY backend/requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

COPY backend /app/backend

EXPOSE 8000

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
