# backend/core/llm.py
from langchain_openai import ChatOpenAI
from backend.core.config import settings

# OpenAI 테스트용 LLM 객체
print(f"KEY CHECK: {settings.OPENAI_API_KEY[:10]}...")
llm = ChatOpenAI(
    model=settings.OPENAI_MODEL_NAME,
    api_key=settings.OPENAI_API_KEY,  # sk-proj-...
    temperature=0.1
)

# if settings.RUNPOD_ENDPOINT_ID:
#     # 런팟 모드
#     llm = ChatOpenAI(
#         model=settings.LLM_MODEL_NAME,
#         openai_api_key=settings.RUNPOD_API_KEY,
#         openai_api_base=settings.RUNPOD_VLLM_URL
#     )
# else:
#     # OpenAI 모드 (런팟 ID가 없을 때)
#     llm = ChatOpenAI(
#         model=settings.OPENAI_MODEL_NAME,
#         api_key=settings.OPENAI_API_KEY
#     )


# --- 실제 사용 코드 ---
# config.py에 RUNPOD_API_KEY, RUNPOD_ENDPOINT_ID 필드를 추가 안되어있으면 하삼

# settings에서 값을 읽어와서 객체 생성
# llm = ChatOpenAI(
#     model=settings.LLM_MODEL_NAME,
#     openai_api_key=settings.RUNPOD_API_KEY,
#     openai_api_base=settings.RUNPOD_VLLM_URL,
#     temperature=0.1
# )
# -------------------