"""
File    : backend/models/schema.py
Author  : 김지우
Create  : 2026-04-13
Description : Cadence AI Agent 전체 시스템 데이터베이스 모델 (SQLAlchemy ORM)

Modification History :
    - 2026-04-16 (송주엽) : Seat Add-on 과금 모델 반영, api_usage_logs 인덱스 추가
    - 2026-04-19 (김지우) : cad_entities 삭제, search_vector(tsvector) 및 pg_trgm 인덱스 추가
    - 2026-04-22 (김지우) : 대용량 테이블 PK Integer(Identity) 변환, sparse_embedding 제거
    - 2026-04-22 (김지우) : 최신 DBeaver ERD 기준 스키마 완벽 동기화 (domain 컬럼명 통일, 길이 제한 적용, 미사용 컬럼 정리)
    - 2026-04-23 : review_results.reference_chunk_id + 부분인덱스 (backend/sql/add_review_results_reference_chunk_id.sql 와 정합)
    - 2026-04-23 : ReviewResult.id — DB가 GENERATED ALWAYS AS IDENTITY (sync_pending_fixes 는 id 열 생략)
    - 2026-04-23 : DBeaver 기준 — organizations/licenses/devices/chat_sessions PK·FK 를 PostgreSQL UUID 와 정합 (as_uuid=False, 앱은 str)
    - 2026-04-23 : mapping_rules.layer_role + idx_mapping_rules_org_layer_role (backend/sql/add_mapping_rules_layer_role.sql 정합)
"""

import enum
import uuid
from sqlalchemy import Boolean, Column, DateTime, Numeric, ForeignKey, Index, Integer, String, Text, Enum, Identity, Date, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.sql import func

from pgvector.sqlalchemy import Vector

from backend.core.database import Base

# 실 DB(Postgres uuid) ↔ Python str (asyncpg/HTTP 경로와 호환)
PGUUID = UUID(as_uuid=False)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    plan_code               = Column(String(20), primary_key=True)
    plan_name               = Column(String(50), nullable=False)
    billing_cycle           = Column(String(20), nullable=False, default="yearly")
    base_price              = Column(Numeric(12, 2), nullable=False)
    base_seats              = Column(Integer, nullable=False)
    addon_price_per_seat    = Column(Numeric(12, 2), nullable=False)
    daily_token_limit       = Column(Integer, nullable=True)
    is_active               = Column(Boolean, nullable=False, default=True)
    per_seat_token_bonus    = Column(Integer, nullable=True, default=0)


class SystemAdmin(Base):
    __tablename__ = "system_admins"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    email = Column(String(255), unique=True, index=True)
    password_hash = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    role = Column(String(50), default="super_admin")


class Organization(Base):
    __tablename__ = "organizations"
    id = Column(PGUUID, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    company_name = Column(String(200))
    admin_email = Column(String(255), unique=True)
    password_hash = Column(Text)
    plan = Column(String(20), default="basic")
    max_seats = Column(Integer)
    business_reg_number = Column(String(50))
    business_reg_s3_url = Column(Text)
    verification_status = Column(String(20), default="pending")
    verified_by = Column(String, nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    daily_token_limit = Column(Integer, nullable=True)
    remaining_daily_tokens = Column(Integer, nullable=True)


class License(Base):
    __tablename__ = "licenses"
    id = Column(PGUUID, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    org_id = Column(PGUUID, ForeignKey("organizations.id", ondelete="CASCADE"))
    api_key = Column(String(64), unique=True, index=True)
    status = Column(String(20), default="active")
    starts_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"
    id = Column(PGUUID, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    license_id = Column(PGUUID, ForeignKey("licenses.id", ondelete="CASCADE"))
    machine_id = Column(String(128), unique=True)
    is_active = Column(Boolean, default=True)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), onupdate=func.now())
    display_name = Column(Text)


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, Identity(always=True), primary_key=True, index=True)
    org_id = Column(PGUUID, ForeignKey("organizations.id", ondelete="CASCADE"))
    plan_name = Column(String(20))
    seats = Column(Integer, nullable=True)
    amount = Column(Numeric(12, 2))
    payment_method = Column(String(50), nullable=True)
    pg_provider = Column(String(50), nullable=True)
    pg_transaction_id = Column(String(100), nullable=True)
    status = Column(String(20), default="pending")
    generated_license_id = Column(String, nullable=True)
    billing_period_start = Column(DateTime(timezone=True), nullable=True)
    billing_period_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    payment_type = Column(String(20), nullable=False, default="subscription")
    added_seats = Column(Integer, nullable=True)
    proration_days = Column(Integer, nullable=True)


class ApiUsageLog(Base):
    __tablename__ = "api_usage_logs"
    id = Column(Integer, Identity(always=True), primary_key=True, index=True)
    org_id = Column(PGUUID, ForeignKey("organizations.id", ondelete="CASCADE"))
    license_id = Column(PGUUID, ForeignKey("licenses.id", ondelete="CASCADE"))
    date_dt = Column(Date)
    total_requests = Column(Integer, default=0)
    total_tokens_used = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        Index("idx_api_usage_logs_org_date", "org_id", "date_dt"),
        Index("idx_api_usage_logs_license_date", "license_id", "date_dt", unique=True),
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(PGUUID, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    org_id = Column(PGUUID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    device_id = Column(PGUUID, ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    domain_type = Column(String(20), nullable=False)
    session_title = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    langgraph_thread_id = Column(
        String(100), nullable=True, comment="예약(미사용): LangGraph thread id"
    )
    expires_at = Column(
        DateTime(timezone=True), nullable=True, comment="예약(미사용): 세션 만료"
    )
    summary_text = Column(Text, nullable=False, server_default=text("''"), default="")
    recent_chat = Column(Text, nullable=False, server_default=text("''"), default="")


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, Identity(always=True), primary_key=True, index=True)
    session_id = Column(PGUUID, ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role = Column(String(20))
    content = Column(Text)
    tool_calls = Column(Text, nullable=True)
    active_object_ids = Column(
        Text,
        nullable=True,
        comment="JSON 배열: 해당 메시지 시점에 도면에서 선택(포커스)되었던 엔티티 ID 목록",
    )
    token_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    agent_name = Column(
        String(50), nullable=True, comment="예약(미사용): 호출한 서브에이전트명"
    )
    tool_call_id = Column(
        String(100),
        nullable=True,
        comment="OpenAI: tool 응답이 가리킬 id; 내부: assistant 행이면 tool_calls[0].id(워크플로 1단계) 상관",
    )
    approval_status = Column(
        String(20), nullable=True, comment="예약(미사용): HITL 승인 단계"
    )
    metadata_ = Column(
        "metadata", Text, nullable=True, comment="예약(미사용): 메시지 부가 JSON"
    )


class DocumentS3(Base):
    __tablename__ = "documents_s3"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    file_name = Column(String(255))
    s3_url = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id = Column(Integer, Identity(always=True), primary_key=True)
    document_id = Column(String, ForeignKey("documents_s3.id", ondelete="CASCADE"))
    chunk_index = Column(Integer, nullable=True)
    content = Column(Text)
    dense_embedding = Column(Vector(1024))
    domain = Column(Text, nullable=True)
    category = Column(Text, nullable=True)
    doc_name = Column(String(255), nullable=True)
    effective_date = Column(Date, nullable=True)
    section_id = Column(Text, nullable=True)
    chunk_type = Column(Text, nullable=True)
    search_vector = Column(TSVECTOR, nullable=True)
    __table_args__ = (
        Index("idx_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("idx_document_chunks_section_trgm", "section_id", postgresql_using="gin", postgresql_ops={"section_id": "gin_trgm_ops"}),
        Index(
            "ix_perm_chunks_embedding_hnsw",
            dense_embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"dense_embedding": "vector_cosine_ops"},
        ),
    )


class TempDocument(Base):
    __tablename__ = "temp_documents"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    org_id = Column(PGUUID, ForeignKey("organizations.id", ondelete="CASCADE"))
    device_id = Column(PGUUID, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    file_name = Column(String(255))
    temp_s3_url = Column(Text)
    comment = Column(Text, nullable=True)
    status = Column(String(20), default="pending")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    domain = Column(String(50), nullable=True)


class TempDocumentChunk(Base):
    __tablename__ = "temp_document_chunks"
    id = Column(Integer, Identity(always=True), primary_key=True)
    temp_document_id = Column(String, ForeignKey("temp_documents.id", ondelete="CASCADE"))
    chunk_index = Column(Integer, nullable=True)
    content = Column(Text)
    dense_embedding = Column(Vector(1024))
    domain = Column(String(50), nullable=True)
    category = Column(String(100), nullable=True)
    doc_name = Column(String(255), nullable=True)
    effective_date = Column(Date, nullable=True)
    section_id = Column(String(50), nullable=True)
    chunk_type = Column(String(50), nullable=True)
    org_id = Column(PGUUID, index=True)
    search_vector = Column(TSVECTOR, nullable=True)
    __table_args__ = (
        Index("idx_temp_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index("idx_temp_document_chunks_section_trgm", "section_id", postgresql_using="gin", postgresql_ops={"section_id": "gin_trgm_ops"}),
        Index(
            "ix_temp_chunks_embedding_hnsw",
            dense_embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"dense_embedding": "vector_cosine_ops"},
        ),
    )


class StandardTerm(Base):
    __tablename__ = "standard_terms"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    domain = Column(String(50))                      # ERD 기준: domain_type -> domain 변경
    category = Column(String(100), nullable=True)
    standard_name = Column(String(255))
    aliases = Column(JSONB, nullable=True)
    unit_type = Column(String(50), nullable=True)
    legal_reference = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



class FixStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class ReviewResult(Base):
    __tablename__ = "review_results"
    __table_args__ = (
        Index(
            "idx_review_results_reference_chunk_id",
            "reference_chunk_id",
            postgresql_where=text("reference_chunk_id IS NOT NULL"),
        ),
    )

    id = Column(Integer, Identity(always=True), primary_key=True, index=True)
    session_id = Column(PGUUID, ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, nullable=False)
    target_handle = Column(Text, index=True, nullable=False)
    violation_type = Column(Text, nullable=False)
    violation_level = Column(Text, default="WARNING", index=True, nullable=False)
    action = Column(Text, nullable=True, default="")
    description = Column(Text, nullable=True)
    # 시방 RAG 근거: document_chunks.id (NULL 가능) — SQL 마이그레이션: backend/sql/add_review_results_reference_chunk_id.sql
    reference_chunk_id = Column(
        Integer,
        ForeignKey("document_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    proposed_fix = Column(Text, nullable=True)
    status = Column(Enum(FixStatus), default=FixStatus.PENDING, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class SupportInquiry(Base):
    """
    고객 지원 문의 및 관리자 답변 관리 테이블
    """
    __tablename__ = "support_inquiries"

    id = Column(Integer, Identity(always=True), primary_key=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    inquiry_type = Column(String(50), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    file_path = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    assigned_admin_id = Column(String, ForeignKey("system_admins.id", ondelete="SET NULL"), nullable=True)
    answer_content = Column(Text, nullable=True)
    answered_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now(), 
        nullable=True
    )

class MappingRule(Base):
    """
    기업 맞춤 사용자 매핑 규칙 테이블.
    현장 은어를 표준 용어로 연결하며, 레이어의 역할을 명시적으로 오버라이드할 수 있습니다.
    """
    __tablename__ = "mapping_rules"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()), index=True)
    org_id = Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    domain = Column(String(20), nullable=False) # arch, elec, fire, pipe 등
    rule_type = Column(String(30), nullable=True)
    source_key = Column(String(200), nullable=False) # 현장 레이어명 또는 키워드
    
    # 신규 추가: 레이어 역할 오버라이드 컬럼
    layer_role = Column(
        String(20), 
        nullable=True, 
        comment="Layer role override: arch | mep | aux | NULL(미지정 시 휴리스틱 분류 사용)"
    )
    
    style_config = Column(JSONB, nullable=True)
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    standard_term_id = Column(String, ForeignKey("standard_terms.id", ondelete="RESTRICT"), nullable=True)

    # 인덱스 설정
    __table_args__ = (
        # org_id + layer_role 조합의 부분 인덱스 (SQL의 WHERE 조건 반영)
        Index(
            "idx_mapping_rules_org_layer_role",
            "org_id",
            "layer_role",
            postgresql_where=text("layer_role IS NOT NULL")
        ),
    )
