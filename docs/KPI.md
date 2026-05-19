# CAD-Agent KPI 정리

> 최종 갱신: 2026-05-09
> 적용 범위: 4개 도메인 에이전트 (배관 / 건축 / 소방 / 전기) 공통

---

## 1. KPI 카탈로그

대시보드 스펙(2026-05-09)을 기준으로 **활성 22개 KPI**(기본 19 + 실시간 정확도 추정 3)와 **2개 Archived 항목**을 운영합니다.

### ① 핵심 성능 / 정확도 (15개)

| KPI 명 | 의미 | 실시간 측정 |
|---|---|---|
| `f1_score` | 위반 탐지 Precision·Recall 종합 | ❌ (골든셋 필요) |
| `precision` | 잡은 위반 중 진짜 위반 비율 | ❌ (골든셋 필요) |
| `recall` | 실제 위반 중 잡은 비율 | ❌ (골든셋 필요) |
| `citation_accuracy` | 인용 법규 조항이 실제와 일치한 비율 | ❌ (골든셋 필요) |
| `false_negative_rate` | 실제 위반인데 놓친 비율 (미검출률) | ❌ (골든셋 필요) |
| `false_positive_rate` | 위반 아닌데 위반으로 본 비율 (오검출률) | ❌ (골든셋 필요) |
| `coord_error_mm` | AI 위반 위치와 정답 위치 거리 오차(mm) | ❌ (골든셋 필요) |
| `parsing_success_rate` | CAD 객체를 데이터로 추출 성공한 비율 | ✅ |
| `chunk_hit_rate` | RAG 결과에 정답 법규가 포함된 비율 | ❌ (골든셋 필요) |
| `rag_retrieval_relevance` | 검색된 법규 중 실제 판정에 사용된 비율 | ✅ |
| `markup_completeness` | revcloud + mtext 둘 다 생성된 비율 | ✅ |
| `response_time_sec` | 도면 1장 검토 총 소요 시간 | ✅ |
| `sllm_latency_ms` | sLLM 1회 추론당 지연 시간 | ✅ (도메인 노드가 노출 시) |
| `agent_error_rate` | JSON 파싱 실패·타임아웃 등 에이전트 단계 오류율 | ✅ |
| `review_consistency` | 동일 도면 반복 검토 시 결과 일관성 | △ (다중 실행 시) |
| `cost_per_review` | 검토 1회당 비용 (토큰·GPU) | △ (cost > 0 일 때) |
| `model_name` | 사용된 모델 식별 명칭 (Langfuse metadata) | ✅ |

> "골든셋 필요" 표시된 정확도 KPI는 [test_extraction.py](../backend/test_extraction.py) / [test_rag.py](../backend/test_rag.py) 같은 **별도 골든셋 평가**에서만 의미있는 값이 나옵니다. 실시간에서는 §③ 의 추정 KPI 가 그 자리를 메웁니다.

### ② 운영 안정성 / 실무 효용성 (4개)

| KPI 명 | 의미 | 실시간 측정 |
|---|---|---|
| `e2e_success_rate` | 파싱~마킹 전 과정이 단절 없이 성공한 비율 | ✅ |
| `escalation_rate` | AI가 모호한 상황에서 사람 검토를 요청한 비율 | ✅ |
| `valid_escalation_rate` | 넘긴 케이스 중 실제로 사람 판단이 필요했던 비율 | ❌ (HITL 피드백 필요) |
| `markup_position_acceptance_rate` | 엔지니어가 마킹 위치를 수정 없이 수락한 비율 | ❌ (HITL 피드백 필요) |

### ③ 실시간 정확도 추정 (3개, 신규 — 정답셋 없이 산출)

| KPI 명 | 의미 | 측정 방식 |
|---|---|---|
| `precision_judge` | LLM-as-Judge 가 인정한 위반 비율 (실시간 precision 추정) | strong LLM(GPT-4o-mini)이 emit 위반을 binary 판정 — `EVAL_LLM_JUDGE_ENABLED=true` 시 |
| `recall_lower_bound` | (LLM 위반 ∩ deterministic 검사 위반) / \|deterministic\| | 항상 산출 (deterministic 검사가 있을 때) |
| `f1_estimate` | 위 둘로 산출한 f1 추정치 | precision_judge AND recall_lower_bound 모두 있을 때만 |

> 이 3개는 **"진짜 정확도"가 아닌 추정치**입니다. precision_judge 는 심판 LLM 이 본 정밀도, recall_lower_bound 는 deterministic 검사 범위 내 재현율 하한선입니다. 골든셋 평가의 `f1_score`/`precision`/`recall` 과는 의미가 다르므로 대시보드에서 이름으로 명확히 구분되어야 합니다.

### ④ Archived (대시보드에서 제외)

| KPI 명 | 제외 사유 |
|---|---|
| `embedding_similarity_avg` | 단순 벡터 유사도 — `chunk_hit_rate`가 더 실질적 |
| `violation_count` | 도면마다 위반 수가 달라 시스템 성능 지표로 부적합 |

---

## 2. 코드 구조

```
┌─ pipe / arch / fire / electric agent
│      │
│      ▼  agent_api.py:637, 821
│
├─ track_cad_review_metrics(domain, agent_result, ...)        ← eval_tracker.py
│      │
│      ├─ retrieved_laws → RetrievedChunk[]                   (rag_retrieval_relevance)
│      ├─ response_meta.deterministic_equipment_ids           (recall_lower_bound)
│      ├─ response_meta.sllm_durations_ms                     (sllm_latency_ms)
│      └─ EVAL_LLM_JUDGE_ENABLED → judge_violations()         ← llm_judge.py (옵션)
│
└─ CADAgentScorer.evaluate(...)                               ← kpi_score.py
       │
       ▼  Langfuse trace.score(...) + trace.update(metadata=...)
```

| 파일 | 역할 |
|---|---|
| [`schemas.py`](../backend/services/evaluation/schemas.py) | `Violation`, `RetrievedChunk`, `MarkupResult`, `EvalResult` 데이터 클래스 |
| [`kpi_score.py`](../backend/services/evaluation/kpi_score.py) | `CADAgentScorer.evaluate()` — 22 KPI 계산 + Langfuse emit |
| [`eval_tracker.py`](../backend/services/evaluation/eval_tracker.py) | `track_cad_review_metrics()` — agent_result → 스코어러 입력 변환, judge/self-consistency 호출 |
| [`llm_judge.py`](../backend/services/evaluation/llm_judge.py) | LLM-as-Judge precision (MVP 옵션, GPT 호출) |
| [`self_consistency.py`](../backend/services/evaluation/self_consistency.py) | sLLM 다회 호출 합의 기반 precision (sLLM 단독 시대 옵션) |
| [`agent_api.py`](../backend/api/routers/agent_api.py) | 4개 도메인 모두 동일 진입점에서 `track_cad_review_metrics` 호출 |

### 도메인별 커버리지 매트릭스

모든 도메인이 동일한 진입점(`track_cad_review_metrics`)과 동일한 KPI 세트, 동일한 `response_meta` 스키마를 공유합니다. 4개 도메인 노드(arch / pipe / fire / electric)는 모두 review 분기에서 `deterministic_equipment_ids` 와 `sllm_durations_ms` 를 response_meta 에 plumb 합니다.

| KPI 카테고리 | pipe | electric | arch | fire |
|---|---|---|---|---|
| ① 핵심 성능/정확도 (15) | ✅ | ✅ | ✅ | ✅ |
| ② 운영 안정성 (4) | ✅ | ✅ | ✅ | ✅ |
| `precision_judge` (LLM-judge, MVP) | ✅ flag | ✅ flag | ✅ flag | ✅ flag |
| `precision_self_consistency` (sLLM 단독) | ✅ flag | ✅ flag | ✅ flag | ✅ flag |
| `recall_lower_bound` plumb | ✅ | ✅ | ✅ (자리만, det 빈 리스트) | ✅ (자리만, det 빈 리스트) |
| `recall_lower_bound` 실값 | ✅ | ✅ | ⏳ deterministic 검사 도입 시 | ⏳ deterministic 검사 도입 시 |
| `f1_estimate` (둘 다 있을 때) | ✅ | ✅ | ⏳ | ⏳ |
| `sllm_durations_ms` plumb 자리 | ✅ | ✅ | ✅ | ✅ |
| `sllm_latency_ms` 실값 | ⏳ workflow_handler 가 채우면 활성 | ⏳ | ⏳ | ⏳ |

**의미 정리**:
- ✅ = 코드 wiring 완료, 신호만 들어오면 활성
- ⏳ = 외부 작업(deterministic_checker 신설 / sllm 호출 시간 누적) 필요

**arch / fire 의 `recall_lower_bound` 가 0 인 이유**:
- 두 도메인은 LLM 기반 [compliance.py](../backend/services/agents/arch/sub/review/compliance.py) 만 사용하고, 규칙 기반 deterministic_checker 가 미구현.
- 코드 wiring 은 완료 — workflow_handler 가 `deterministic_violations: []` 를 일관 반환하고, review_node 가 `deterministic_equipment_ids: []` 를 plumb.
- arch/fire 에 deterministic_checker 가 신설되면 wiring 변경 없이 자동 활성.

**`sllm_durations_ms` 실제 상태**:
- 4개 도메인 review 노드 모두 response_meta 에 자리 plumb 완료.
- 워크플로우(workflow_handler / sub agent) 가 LLM 호출 시간을 누적해서 `result["sllm_durations_ms"]` 에 채워주는 instrumentation 만 추가하면 자동 활성화.

### 조건부 emit

다음 KPI는 **신호가 있을 때만** 기록합니다 (단일 호출 dashboard 노이즈 방지):

| KPI | emit 조건 |
|---|---|
| `valid_escalation_rate` | escalation 발생 시 |
| `review_consistency` | 다중 실행 데이터(`multiple_run_violations`)가 있을 때 |
| `cost_per_review` | cost > 0 일 때 |
| `precision_judge` | `EVAL_LLM_JUDGE_ENABLED=true` 이고 심판 LLM 호출 성공 |
| `recall_lower_bound` | response_meta 에 `deterministic_equipment_ids` 가 있을 때 |
| `f1_estimate` | precision_judge AND recall_lower_bound 모두 있을 때 |

---

## 3. 자동 추출 신호

`eval_tracker.py`가 `agent_result` 에서 자동으로 끌어오는 신호:

| 신호 | 출처 | 비고 |
|---|---|---|
| `is_parsing_ok` | `len(drawing_data["entities"]) > 0` | |
| `is_marking_ok` | `위반 없음 OR pending_fixes 생성됨` | |
| `escalation_triggered` | `confidence_score < 0.7 위반 존재 OR low_confidence_count > 0` | |
| `errors` | `current_step == "error"` | |
| `total_markings` | `len(pending_fixes)` | |
| `model_name` | `settings.LLM_MODEL_NAME` | 환경 설정에서 |
| `cost` | `runtime_meta.cost_usd` | sLLM 자체 호스팅이라 보통 0 |
| `sllm_durations_ms` | `response_meta.sllm_durations_ms` | 도메인 노드가 노출 시 |
| `retrieved_chunks` | `agent_result.retrieved_laws` (LawReference) → 변환 | rag_retrieval_relevance 활성 |
| `used_chunk_ids` | 위반의 `legal_reference` 와 chunk source/article 매칭 | |
| `deterministic_violation_ids` | `response_meta.deterministic_equipment_ids` | recall_lower_bound 분모 |

---

## 4. 실시간 정확도 측정 전략

### 4.1 왜 실시간 recall은 본질적으로 불가능한가

`recall = (잡은 위반) / (실제 전체 위반)` 인데, **분모를 모릅니다**. "전체 위반"은 정답을 미리 만들어둔 골든셋이 있어야만 알 수 있어요. 놓친 걸 어떻게 측정할 방법이 본질적으로 없습니다.

반면 `precision = (진짜 위반) / (잡은 위반)` 은 잡은 것만 검증하면 되니 실시간 측정이 가능합니다.

### 4.2 채택한 우회 전략 (현재 구현됨)

#### A. LLM-as-Judge precision → `precision_judge` ✅ 구현됨
- 에이전트가 emit한 위반 각각을 **별도 strong judge LLM**(GPT-4o-mini)에 던져서 binary 판정.
- 비용: 위반 1건당 LLM 1콜 (도메인당 평균 +5콜).
- 비용 통제: `EVAL_LLM_JUDGE_MAX_SAMPLE` (기본 3) 건만 랜덤 샘플.
- **MVP 기간 한정 권장** — sLLM 단독 시대로 가면 self-judge 가 되어 신뢰도 급락.
- 활성화: `.env` 에 `EVAL_LLM_JUDGE_ENABLED=true`.
- 구현: [llm_judge.py](../backend/services/evaluation/llm_judge.py).

#### B. Deterministic 커버리지 → `recall_lower_bound` ✅ 구현됨
- [compliance.py](../backend/services/agents/pipe/sub/review/compliance.py)가 emit하는 `deterministic_violations`(규칙 기반 기하·topology 검사)는 **거의 무결한 부분 정답**.
- LLM 위반 ∩ deterministic / |deterministic| → recall 하한선.
- 진짜 recall이 0.9면 이 하한선은 0.7~0.8 정도로 찍힘 — 정확하진 않지만 **하한 보장**.
- LLM 종류와 무관하게 작동 (sLLM 단독 시대에도 그대로 유효).
- 비용: **무료** (이미 산출되는 데이터).

#### A + B 결합 → `f1_estimate` ✅ 구현됨
- `f1_estimate = 2·precision_judge·recall_lower_bound / (precision_judge + recall_lower_bound)`
- 둘 다 신호가 있을 때만 emit. 라벨이 "estimate" 임을 대시보드에서 명확히.

### 4.3 단계별 권장 운영

#### 지금 (MVP, GPT 사용 중)
1. `EVAL_LLM_JUDGE_ENABLED=true` 로 활성화
2. `precision_judge` 와 `recall_lower_bound` 가 매 호출 emit 됨
3. **GPT-judge 결과 raw verdict 를 골든셋 시드로 누적** (현재는 로그만 — 추후 DB 저장 권장)

#### 중기 (sLLM 단독 production) — **모듈 구현 완료, 활성화는 환경변수 토글**
1. `.env` 에서 `EVAL_LLM_JUDGE_ENABLED=false` 로 self-judge 비활성
2. `EVAL_SELF_CONSISTENCY_ENABLED=true` 로 자가 일관성 활성 — [self_consistency.py](../backend/services/evaluation/self_consistency.py) 가 sLLM 을 N회 호출(`EVAL_SELF_CONSISTENCY_RUNS`, 기본 3) 하고 다수결(`EVAL_SELF_CONSISTENCY_QUORUM`, 기본 2) 임계로 합의 위반만 인정
3. `recall_lower_bound` 그대로 유지 (LLM 종류 무관)
4. **호출자 추가 작업 필수**: eval_tracker 가 같은 도면을 N-1회 더 돌릴 수 있도록 [agent_api.py](../backend/api/routers/agent_api.py) 에서 `track_cad_review_metrics(..., self_consistency_runner=...)` 로 awaitable 팩토리를 넘겨야 한다. 워크플로우 재실행 함수가 모듈에 노출되면 한 줄 wiring.
5. MVP 동안 [eval_judge_verdicts](database/KPI-DB-REQUIREMENTS.md) 테이블로 누적된 골든셋으로 **진짜 f1** 별도 배치 평가 가능

#### 핵심 인사이트
GPT는 "지금만 쓰고 버리는" 게 아니라 **골든셋 생성기로 역할이 바뀌어 production 이후에도 살아남습니다**. 도면 1000장의 GPT 판정이 쌓이면 그게 진짜 정답셋이 됩니다.

---

## 5. 남은 작업 정리

코드 wiring 은 모두 완료됐고, 다음 항목들은 **외부 데이터 채널/SME 작업/DB 마이그레이션**이 들어와야 KPI 가 의미있는 값을 갖습니다.

### 5.1 DB 마이그레이션 필요 (우선순위 P0~P2)
[KPI-DB-REQUIREMENTS.md](database/KPI-DB-REQUIREMENTS.md) 참조. 3개 테이블 작업으로 다음이 풀립니다:
- **P0** `review_results` ALTER → HITL KPI 2개 활성 (`markup_position_acceptance_rate`, `valid_escalation_rate`)
- **P1** `eval_judge_verdicts` 신설 → 골든셋 시드 영구 보존
- **P2** `eval_review_summary` 신설 → 청구·SLA 영구 보존

### 5.2 도메인 인프라 격차 (SME / 별도 PR)
| 항목 | 어디 | 비고 |
|---|---|---|
| arch / fire `recall_lower_bound` 실값 | `backend/services/agents/{arch,fire}/sub/deterministic_checker.py` 신설 | 코드 wiring 은 완료 — checker 가 `deterministic_violations` list 만 채우면 자동 활성 |
| 4개 도메인 `sllm_latency_ms` 실값 | 각 workflow_handler / sub agent 가 LLM 호출 시간 누적 | review 노드 plumb 자리는 이미 통일됨. result["sllm_durations_ms"] 에 list[float] 만 넣으면 됨 |
| `cost_per_review` 실값 | `runtime_meta.cost_usd` 를 OpenAI 호출 추적기에서 채움 | 자체 호스팅 sLLM 환경에서는 자연스럽게 0 |

### 5.3 운영 wiring (코드만 추가하면 됨)
| 항목 | 어디 | 분량 |
|---|---|---|
| `self_consistency_runner` 팩토리 연결 | [agent_api.py:637, 821](../backend/api/routers/agent_api.py:637) | `track_cad_review_metrics(..., self_consistency_runner=lambda: agent_service.run(domain, state, ...))` 한 줄 |
| LLM-judge verdict → DB INSERT | [eval_tracker.py](../backend/services/evaluation/eval_tracker.py) judge 호출 직후 | DB 마이그레이션(P1) 후 ~10줄 |
| review_summary → DB INSERT | 동상 (마지막) | DB 마이그레이션(P2) 후 ~15줄 |

---

## 6. 변경 이력

| 날짜 | 작업 | 결과 |
|---|---|---|
| 2026-04-12 | 초기 KPI 스코어러 도입 (김민정, EVAL-03) | `kpi_scorer.py`, `test_rag.py`, `test_extraction.py` 생성 |
| 2026-04-17 | eval_tracker 도입 — 4개 도메인 통합 | API → tracker → scorer 파이프라인 완성 |
| 2026-05-09 | 대시보드 스펙 정합 작업 | review_consistency 연결, domain metadata 태깅, embedding_similarity_avg/violation_count 제거, agent_result 신호 wiring, test 파일 import 경로 수정 |
| 2026-05-09 | 실시간 정확도 추정 KPI 추가 | precision_judge / recall_lower_bound / f1_estimate 도입, llm_judge.py 신설, retrieved_laws 자동 변환, pipe 노드 deterministic plumb |
| 2026-05-09 | elec 도메인 deterministic 노출 + 도메인 매트릭스 명시 | elec/workflow_handler 가 deterministic_violations 별도 키로 반환, elec_review_node 에 동일 plumb. arch/fire 는 deterministic_checker 자체가 부재 — 신규 PR 대기 |
| 2026-05-09 | 4개 도메인 plumb 통일 + sLLM 시대 대비 모듈 추가 | arch/fire workflow_handler·review_node 에도 동일 response_meta 스키마 적용 (deterministic_equipment_ids/sllm_durations_ms 자리), self_consistency.py 신설, EVAL_SELF_CONSISTENCY_* 환경변수 도입, DB 요구사항 문서화, .env.example 작성 |

---

## 7. 운영 메모

### Langfuse 대시보드 필터
- `metadata.domain` = `pipe` / `arch` / `fire` / `electric` 으로 도메인별 필터링.
- `metadata.model_name` = `qwen3.5-27b-qlora` 또는 `gpt-4o-mini` 등 모델 비교용.

### 환경 변수 (.env)
```
# LLM-as-Judge (MVP 한정 — precision_judge 활성)
EVAL_LLM_JUDGE_ENABLED=true
EVAL_LLM_JUDGE_MODEL=gpt-4o-mini
EVAL_LLM_JUDGE_MAX_SAMPLE=3        # 도면당 최대 N건 샘플

# OpenAI 키 (LLM-judge 전용으로도 사용)
OPENAI_API_KEY=sk-...
```

### 테스트 스크립트
- `backend/test_extraction.py` — CAD 객체 추출 성능 (golden set 기반 F1)
- `backend/test_rag.py` — RAG 검색 성능 (golden set 기반 chunk_hit_rate)
- 두 스크립트 모두 import 경로는 `backend.services.evaluation.kpi_score` 로 통일됨.

### Score 사용 규칙
- Langfuse `trace.score()` 는 float만 받습니다. 문자열(예: `model_name`, `domain`)은 반드시 `trace.update(metadata=...)` 로 기록.

### 대시보드 위젯 권장 묶음

대시보드에서 KPI 를 그룹핑할 때 권장 구성:

1. **정확도 (골든셋)** — `f1_score`, `precision`, `recall`, `citation_accuracy`, `false_positive_rate`, `false_negative_rate`, `coord_error_mm`
2. **정확도 (실시간 추정)** — `precision_judge`, `recall_lower_bound`, `f1_estimate`
3. **RAG** — `chunk_hit_rate`, `rag_retrieval_relevance`
4. **운영 안정성** — `e2e_success_rate`, `agent_error_rate`, `parsing_success_rate`, `markup_completeness`
5. **사람 협업** — `escalation_rate`, `valid_escalation_rate`, `markup_position_acceptance_rate`
6. **성능/비용** — `response_time_sec`, `sllm_latency_ms`, `cost_per_review`, `review_consistency`
