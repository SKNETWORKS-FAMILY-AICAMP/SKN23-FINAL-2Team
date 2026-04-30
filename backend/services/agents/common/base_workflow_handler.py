"""
File    : backend/services/agents/common/base_workflow_handler.py
Author  : 송주엽
Create  : 2026-04-24
Description : 4개 도메인 WorkflowHandler의 공통 추상 기반 클래스.
              handle_tool_calls() 루프와 JSON 파싱 보일러플레이트를 제거하고
              도메인별 로직은 _dispatch_tool()에 위임한다.
              fire/arch의 sync Session과 pipe/elec의 AsyncSession 모두 지원한다.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any


class BaseWorkflowHandler(ABC):
    """
    도메인 워크플로우 핸들러 추상 기반.
    서브클래스는 _dispatch_tool()만 구현하면 된다.
    """

    def __init__(self, session: Any, db: Any):
        self.session = session
        self.db = db

    async def handle_tool_calls(
        self,
        tool_calls: list,
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        LLM tool_calls 목록을 순서대로 처리하고 결과 목록을 반환한다.
        dict 형식({function: {name, arguments}})과
        object 형식(function.name, function.arguments) 모두 지원한다.
        """
        final_actions: list[dict] = []

        for call in tool_calls:
            # dict / object 양쪽 지원
            if isinstance(call, dict):
                func = call.get("function", {})
                func_name = func.get("name", "") if isinstance(func, dict) else getattr(func, "name", "")
                raw_args  = func.get("arguments", "{}") if isinstance(func, dict) else getattr(func, "arguments", "{}")
            else:
                func = getattr(call, "function", None)
                func_name = getattr(func, "name", "") if func else ""
                raw_args  = getattr(func, "arguments", "{}") if func else "{}"

            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                logging.warning(
                    "[BaseWorkflowHandler] JSON decode 실패 tool=%s args=%r",
                    func_name, raw_args,
                )
                args = {}

            result = await self._dispatch_tool(func_name, args, context)
            if result is not None:
                final_actions.append(result)

        return final_actions

    @abstractmethod
    async def _dispatch_tool(
        self,
        func_name: str,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        단일 tool 호출을 처리한다.
        반환값이 None이면 결과 목록에 추가되지 않는다.
        """
        ...
