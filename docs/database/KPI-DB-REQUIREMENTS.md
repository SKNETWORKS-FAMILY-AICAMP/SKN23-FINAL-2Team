# KPI 평가 인프라 — DB 요구사항

> 작성일: 2026-05-09
> 적용 DB: PostgreSQL 15+ (기존 schema-spec.md 와 동일 인스턴스)

KPI 평가 시스템이 완전 가동되려면 아래 3개 영역의 DB 변경이 필요합니다. 각 변경은 독립적이라 우선순위에 따라 분리 PR 가능합니다.

---

## 1. `eval_judge_verdicts` (신규 테이블) — 골든셋 시드 누적

**목적**: LLM-as-Judge 가 매 검토마다 emit 한 binary verdict 를 영구 저장. MVP 기간 누적된 데이터가 sLLM 단독 시대의 **진짜 골든셋**이 됩니다.

**왜 필요한가**: 현재 [llm_judge.py](../../backend/services/evaluation/llm_judge.py) 결과는 로그/Langfuse score 로만 남고 사라집니다. 도면 1000장의 GPT 판정이 쌓이면 그게 진짜 정답셋이 되는데, 저장소가 없으면 매번 다시 GPT 호출해야 함 → 비용 낭비.

```sql
CREATE TABLE eval_judge_verdicts (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    domain VARCHAR(20) NOT NULL,             -- 'pipe' | 'arch' | 'fire' | 'electric'
    drawing_fingerprint VARCHAR(32),         -- 같은 도면 중복 판정 식별 (없으면 NULL)
    -- 위반 식별
    violation_object_id TEXT NOT NULL,        -- 위반 대상 handle / equipment_id
    violation_type VARCHAR(100) NOT NULL,     -- 'pressure_violation', 'clearance_error' 등
    citation TEXT,                            -- legal_reference (인용 법규 조항)
    -- 심판 결과
    judge_model VARCHAR(50) NOT NULL,         -- 'gpt-4o-mini', 'claude-sonnet-4' 등
    is_valid BOOLEAN NOT NULL,                -- 심판이 진짜 위반으로 인정?
    confidence NUMERIC(4, 3),                 -- 0.000 ~ 1.000
    reason TEXT,                              -- 심판이 남긴 한 문장 사유
    -- 메타
    agent_model VARCHAR(50),                  -- 위반을 emit 한 sLLM/GPT 모델명
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_judge_verdicts_session ON eval_judge_verdicts(session_id);
CREATE INDEX idx_judge_verdicts_domain ON eval_judge_verdicts(domain, created_at DESC);
CREATE INDEX idx_judge_verdicts_drawing ON eval_judge_verdicts(drawing_fingerprint)
    WHERE drawing_fingerprint IS NOT NULL;
-- 같은 도면 + 같은 위반 (object_id, type) 의 다중 판정 시 최신 판정 빠른 조회
CREATE INDEX idx_judge_verdicts_violation ON eval_judge_verdicts(
    drawing_fingerprint, violation_object_id, violation_type, created_at DESC
);
```

**Wiring 위치**: [llm_judge.py](../../backend/services/evaluation/llm_judge.py) 의 `judge_violations()` 가 `JudgeVerdict` 반환 후, eval_tracker 가 DB 에 `INSERT`. 누적된 데이터로 별도 배치 평가(`backend/test_extraction.py` 등) 시 진짜 ground_truths 로 사용.

---

## 2. `eval_review_summary` (신규 테이블) — 검토 1회 = 행 1개

**목적**: 도면 1장 검토 단위의 평가 요약. Langfuse 가 trace 기반이라 시계열 분석/장기 보존에 약하므로 우리 DB 에도 핵심 KPI 만 별도 저장.

**왜 필요한가**: `cost_per_review` 와 `sllm_latency_ms` 같은 운영 KPI 는 Langfuse 만 보면 30일 보존 한도에 묶입니다. 청구 정산·SLA 분석에는 영구 저장이 필요.

```sql
CREATE TABLE eval_review_summary (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    org_id VARCHAR(50) NOT NULL,              -- 청구 집계용
    domain VARCHAR(20) NOT NULL,
    drawing_fingerprint VARCHAR(32),
    agent_model VARCHAR(50) NOT NULL,
    -- 카운트
    violation_count INTEGER DEFAULT 0,
    pending_fix_count INTEGER DEFAULT 0,
    deterministic_count INTEGER DEFAULT 0,
    rag_chunk_count INTEGER DEFAULT 0,
    -- 정확도 추정 (실시간)
    precision_judge NUMERIC(4, 3),            -- NULL 가능
    recall_lower_bound NUMERIC(4, 3),         -- NULL 가능
    f1_estimate NUMERIC(4, 3),                -- NULL 가능
    -- 운영
    response_time_sec NUMERIC(6, 2) NOT NULL,
    sllm_latency_ms_avg NUMERIC(8, 2),
    cost_usd NUMERIC(10, 6) DEFAULT 0,
    is_error BOOLEAN DEFAULT FALSE,
    escalation_triggered BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_review_summary_org_time ON eval_review_summary(org_id, created_at DESC);
CREATE INDEX idx_review_summary_domain ON eval_review_summary(domain, created_at DESC);
CREATE INDEX idx_review_summary_session ON eval_review_summary(session_id);
```

**Wiring 위치**: [eval_tracker.py:track_cad_review_metrics()](../../backend/services/evaluation/eval_tracker.py) 마지막에 한 줄 INSERT 추가.

> ⚠️ `violation_count` 는 시스템 성능 KPI 로는 archived 됐지만, **이 요약 테이블에서는 청구·도면 단위 메트릭**으로 의미가 다릅니다. 혼동 방지를 위해 컬럼명은 유지.

---

## 3. `review_results` 테이블 확장 — HITL 피드백 채널

**목적**: 사용자가 pending fix 를 승인/거부/수정한 시점·주체를 기록 → `markup_position_acceptance_rate` 와 `valid_escalation_rate` KPI 활성화.

**왜 필요한가**: 현재 [eval_tracker.py:147](../../backend/services/evaluation/eval_tracker.py:147) 의 `accepted_count = 0`, `is_valid_escalation = False` 가 항상 0/False 로 박혀 있는 이유 = HITL 피드백이 어디에도 저장되지 않기 때문.

기존 `review_results` 테이블에 컬럼만 추가하면 됩니다 (별도 테이블 X).

```sql
ALTER TABLE review_results
    ADD COLUMN accepted_at TIMESTAMPTZ,                    -- 사용자 승인 시각 (NULL 이면 미승인)
    ADD COLUMN accepted_by VARCHAR(50),                    -- user_id / system_admin_id
    ADD COLUMN position_modified BOOLEAN DEFAULT FALSE,    -- 마킹 위치를 사용자가 수정?
    ADD COLUMN escalation_review VARCHAR(20);              -- 'valid' | 'noise' | NULL (escalation 판정)

CREATE INDEX idx_review_results_accepted
    ON review_results(session_id, accepted_at)
    WHERE accepted_at IS NOT NULL;
```

**Wiring 위치**:
- 프론트엔드: 사용자가 위반 항목 "승인/무시" 클릭 시 PATCH `/api/review/{id}/accept` 호출.
- 백엔드: 기존 fix 적용 엔드포인트(`agent_api.py`)에서 `accepted_at = NOW()` 업데이트.
- eval_tracker: 동일 도면의 다음 검토 호출 시 이전 review_results 의 accepted 비율을 조회하여 `accepted_count` / `total_markings` 분자 채움.

---

## 4. 적용 우선순위

| 우선 | 작업 | 효과 |
|---|---|---|
| **P0** | `review_results` ALTER (3번) | HITL KPI 2개 (`markup_position_acceptance_rate`, `valid_escalation_rate`) 즉시 활성 |
| **P1** | `eval_judge_verdicts` 신설 (1번) | LLM-judge 결과 영구 보존 → 골든셋 시드 누적. sLLM 전환 시 진짜 f1 가능 |
| **P2** | `eval_review_summary` 신설 (2번) | 청구·운영 분석. Langfuse 30일 한도 우회 |

---

## 5. 마이그레이션 적용 순서

```bash
# P0 — 기존 테이블 확장 (다운타임 없음)
psql -f sql/2026_05_09_alter_review_results_hitl.sql

# P1 — 신규 테이블 (인덱스 포함)
psql -f sql/2026_05_09_create_eval_judge_verdicts.sql

# P2 — 운영 요약
psql -f sql/2026_05_09_create_eval_review_summary.sql
```

각 SQL 파일은 [backend/sql/](../../backend/sql/) 에 위치시킵니다. 기존 `chat_sessions_memory_refactor.sql` 등과 동일 디렉토리.

---

## 6. ORM 모델 추가

[backend/models/schema.py](../../backend/models/schema.py) 에 다음 클래스 추가 필요:

```python
class EvalJudgeVerdict(Base):
    __tablename__ = "eval_judge_verdicts"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(PGUUID, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    domain = Column(String(20), nullable=False)
    drawing_fingerprint = Column(String(32), nullable=True)
    violation_object_id = Column(Text, nullable=False)
    violation_type = Column(String(100), nullable=False)
    citation = Column(Text)
    judge_model = Column(String(50), nullable=False)
    is_valid = Column(Boolean, nullable=False)
    confidence = Column(Numeric(4, 3))
    reason = Column(Text)
    agent_model = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EvalReviewSummary(Base):
    __tablename__ = "eval_review_summary"
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(PGUUID, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    org_id = Column(String(50), nullable=False, index=True)
    domain = Column(String(20), nullable=False)
    drawing_fingerprint = Column(String(32))
    agent_model = Column(String(50), nullable=False)
    violation_count = Column(Integer, default=0)
    pending_fix_count = Column(Integer, default=0)
    deterministic_count = Column(Integer, default=0)
    rag_chunk_count = Column(Integer, default=0)
    precision_judge = Column(Numeric(4, 3))
    recall_lower_bound = Column(Numeric(4, 3))
    f1_estimate = Column(Numeric(4, 3))
    response_time_sec = Column(Numeric(6, 2), nullable=False)
    sllm_latency_ms_avg = Column(Numeric(8, 2))
    cost_usd = Column(Numeric(10, 6), default=0)
    is_error = Column(Boolean, default=False)
    escalation_triggered = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

기존 `ReviewResult` 클래스에는 컬럼 4개만 추가:

```python
class ReviewResult(Base):
    # ... 기존 컬럼 ...
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_by = Column(String(50), nullable=True)
    position_modified = Column(Boolean, default=False)
    escalation_review = Column(String(20), nullable=True)
```
