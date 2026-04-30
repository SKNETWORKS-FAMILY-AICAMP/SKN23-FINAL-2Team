
import os
from huggingface_hub import snapshot_download

model_id = "Qwen/Qwen3-Reranker-0.6B"
print(f"[*] '{model_id}' 모델 다운로드를 시작합니다...")

try:
    # 캐시 디렉토리에 다운로드 (RERANKER_HF_OFFLINE=False 와 같은 효과)
    path = snapshot_download(repo_id=model_id)
    print(f"\n[V] 다운로드 성공!")
    print(f"[V] 모델 저장 위치: {path}")
    print("\n이제 백엔드 서버를 다시 실행하시면 리랭커가 정상적으로 작동합니다.")
except Exception as e:
    print(f"\n[X] 다운로드 실패: {e}")
    print("인터넷 연결 상태를 확인해 주세요.")
