"""
File    : backend/services/llm_service.py
Author  : 김지우
Date    : 2026-04-14
Description : 로컬/원격 서빙 중인 Qwen(sLLM) 모델과의 통신을 담당하는 통합 인터페이스

Modification History :
    - 2026-04-14 (김지우) : 로컬 테스트를 위해 openai gpt 이용하도록 변경 (기본값)
    - 2026-04-19 (김지우) : USE_SLLM 환경 변수 기반 OpenAI/sLLM 동적 전환 로직 구현
"""
import httpx
import json
from backend.core.config import settings


async def generate_answer(
    messages: list,
    temperature: float = 0.1,
    tools: list | None = None,
    tool_choice: str | dict | None = None,
    response_format: dict | None = None,
) -> str | dict | list:
    """
    각 에이전트가 조립한 messages를 Qwen 추론 서버로 전송합니다.

    - tools/tool_choice 제공 시: 첫 번째 tool call의 파싱된 arguments(dict)를 반환합니다.
    - response_format 제공 시: JSON 파싱된 dict를 반환합니다.
    - 그 외: 응답 content 문자열을 반환합니다.
    """
    # USE_SLLM 환경변수가 세팅되어 있으면 런팟(sLLM), 없으면 로컬 테스트용 OpenAI GPT를 사용합니다.
    use_sllm = getattr(settings, "USE_SLLM", False)
    
    url = f"{settings.LLM_ENDPOINT_URL}/v1/chat/completions" if use_sllm else "https://api.openai.com/v1/chat/completions"
    api_key = settings.LLM_API_KEY if use_sllm else settings.OPENAI_API_KEY
    model_name = settings.LLM_MODEL_NAME if use_sllm else settings.OPENAI_MODEL_NAME

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # OpenAI 공식 API 최신 모델(gpt-5.x 등)은 max_tokens 대신 max_completion_tokens만 허용.
    # vLLM/sLLM 호환 서버는 기존 max_tokens를 유지.
    payload: dict = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "top_p": 0.9,
    }
    if use_sllm:
        payload["max_tokens"] = 2048
    else:
        payload["max_completion_tokens"] = 2048

    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if response_format:
        payload["response_format"] = response_format

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                print(f"sLLM Error Response: {response.text}")
                return "sLLM 서버 응답 오류가 발생했습니다."

            result = response.json()
            message = result["choices"][0]["message"]

            # Tool calling 응답 처리
            if tools and message.get("tool_calls"):
                return message["tool_calls"]

            content = message.get("content", "")

            # JSON 응답 파싱 (response_format 지정 시)
            if response_format and response_format.get("type") == "json_object":
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    return content

            return content

    except Exception as e:
        print(f"Connection Error to sLLM: {str(e)}")
        return "AI 서비스 연결에 실패했습니다. 서버 상태를 확인해주세요."
