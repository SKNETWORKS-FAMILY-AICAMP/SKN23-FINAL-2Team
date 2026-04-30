"""
File    : backend/api/schemas/chat.py
Author  : 김다빈
Create  : 2026-04-13
Description : REVIEW_SPEC.md JSON 계약 기반 Pydantic 스키마 정의
              Python 에이전트가 반환하는 annotated_entities 구조를
              Pydantic 모델로 선언한다. C#/React 연동 시 이 스키마가 기준.

              포함 모델:
                - AutoFix: 자동 수정 정보 (type, attribute_tag, new_value)
                - ViolationInfo: 위반 상세 (id, severity, rule, description, suggestion, auto_fix)
                - BoundingBox: 도면 좌표 (x1, y1, x2, y2)
                - AnnotatedEntity: 위반 엔티티 전체 (handle, type, layer, bbox, violation)
                - ChatResponse: 에이전트 최종 응답 (session_id, reply, annotated_entities)

Modification History :
    - 2026-04-13 (김다빈) : 초기 구현 — REVIEW_SPEC.md 기반, 전 도메인 공통 포맷
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class AutoFix(BaseModel):
    """자동 수정 정보 — auto_fix.type에 따라 C#이 도면 직접 수정"""
    model_config = ConfigDict(extra="allow")

    type: str
    # ATTRIBUTE 타입일 때 수정할 블록 속성 태그명
    attribute_tag: Optional[str] = None
    # 수정할 새 값
    new_value: Optional[str] = None
    new_layer: Optional[str] = None
    new_text: Optional[str] = None
    new_height: Optional[float] = None
    new_color: Optional[int] = None
    new_linetype: Optional[str] = None
    new_lineweight: Optional[float] = None
    delta_x: Optional[float] = None
    delta_y: Optional[float] = None
    base_x: Optional[float] = None
    base_y: Optional[float] = None
    stretch_side: Optional[str] = None
    new_width: Optional[float] = None
    new_start: Optional[dict[str, Any]] = None
    new_end: Optional[dict[str, Any]] = None
    new_center: Optional[dict[str, Any]] = None
    new_radius: Optional[float] = None
    new_block_name: Optional[str] = None
    new_vertices: Optional[list[dict[str, Any]]] = None
    modification_tier: Optional[int] = None


class ViolationInfo(BaseModel):
    """위반 항목 상세 정보"""
    id: str = Field(description="고유 ID — APPROVE_FIX/REJECT_FIX 매칭 키 (예: V001)")
    severity: Literal["Critical", "Major", "Minor"]
    rule: str = Field(description="위반 근거 법규 조항 (예: KDS 41 10 05 3.1)")
    description: str = Field(description="위반 내용 한 줄 요약")
    suggestion: str = Field(description="수정 방법 안내")
    auto_fix: Optional[AutoFix] = None


class BoundingBox(BaseModel):
    """RevCloud 그릴 도면 좌표 영역"""
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0


class AnnotatedEntity(BaseModel):
    """위반이 탐지된 CAD 엔티티 — REVIEW_SPEC.md annotated_entities 원소"""
    handle: str = Field(description="AutoCAD 객체 핸들 (16진수 문자열)")
    type: str = Field(description="엔티티 타입 (LINE / ARC / BLOCK / MTEXT / DIMENSION 등)")
    layer: str = Field(default="", description="레이어명")
    bbox: BoundingBox = Field(default_factory=BoundingBox)
    violation: ViolationInfo


class ChatResponse(BaseModel):
    """에이전트 최종 응답 — HTTP /api/v1/agent/execute 반환값"""
    session_id: str
    domain: str
    reply: str
    annotated_entities: list[AnnotatedEntity] = Field(default_factory=list)
