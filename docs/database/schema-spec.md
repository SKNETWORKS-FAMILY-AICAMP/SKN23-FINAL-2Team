# 데이터베이스 설계 명세서 (Database Specification)

## 1. 개요

| 항목           | 내용                                                                                         |
| :------------- | :------------------------------------------------------------------------------------------- |
| **DBMS**       | PostgreSQL 15+                                                                               |
| **Extensions** | `uuid-ossp` (식별자 생성), `vector` (pgvector 임베딩 검색), **`pg_trgm` (오타/유사도 검색)** |

### 주요 특징 (Architecture Highlights)

- **B2B 멀티 테넌시 격리 구조:** 모든 기업 데이터와 매핑 규칙, 임시 시방서는 `org_id`를 기준으로 철저히 분리되어 타사 데이터 유출을 방지합니다.
- **하이브리드 RAG 이중화:** 전역 표준 문서(`documents_s3`)와 기업 커스텀 문서(`temp_documents`) 저장소를 분리하고, **BGE-M3 Dense 벡터(의미 검색)**와 PostgreSQL 내부 **`tsvector`(키워드 검색) 및 `pg_trgm`(오타/유사도 검색)**을 결합한 3중 하이브리드 검색을 지원합니다.
- **단일 진실 공급원(SSOT) 매핑 체계:** 현장의 비표준 은어/약어를 AI가 이해하는 단일 법적/표준 명칭(`standard_terms`)으로 강제 치환하는 무결성 맵핑을 구현했습니다.
- **과금 및 라이센스 추적:** C# 플러그인에서 접속하는 물리적 단말기(`devices`)와 토큰 사용량(`api_usage_logs`)을 일일 단위로 로깅하여 정확한 과금 근거를 확보합니다.
- **Seat Add-on / Plan Upgrade 과금 모델:** 상위 요금제 강제 업그레이드 없이 현재 플랜을 유지하면서 시트(단말기)를 추가 구매할 수 있으며, 플랜 업그레이드 시에도 남은 기간에 대한 차액만 일할 계산하여 청구합니다. 모든 결제 이력은 `payments.payment_type`으로 구분되어 단일 테이블에 저장됩니다.
- **요금제 마스터 분리:** 플랜별 가격·시트·한도 정보를 `subscription_plans` 테이블에서 관리합니다. 가격 인상·프로모션 시 코드 변경 없이 DB 레코드만 수정하면 됩니다.
- **실시간 사용량 관리:** 한도(`organizations.daily_*_limit`)와 사용량(`api_usage_logs`)을 분리하여 관리합니다. 매 API 요청 시 `api_usage_logs`에 집계하고 한도 초과 여부를 체크합니다.

---

## 2. 테이블 목록 (Table Inventory)

| 그룹               | 테이블명               | 논리명             | 설명                                            |
| :----------------- | :--------------------- | :----------------- | :---------------------------------------------- |
| **Admin & B2B**    | `system_admins`        | 시스템 관리자      | 플랫폼 총괄 운영자 계정                         |
|                    | `organizations`        | 고객사 (기업)      | B2B 서비스 가입 기업 기본 정보                  |
| **Billing & Auth** | `subscription_plans`   | 요금제 마스터      | 플랜별 가격·시트·한도 정보                      |
|                    | `licenses`             | API 라이선스       | 기업별 C# 플러그인 접속용 인증 키               |
|                    | `payments`             | 결제 이력          | 정기구독·시트추가·플랜업그레이드 결제 이력      |
|                    | `api_usage_logs`       | API 사용 로그      | 일자/라이센스별 API 호출 횟수 및 토큰 소비량    |
| **Interaction**    | `devices`              | 등록 단말기        | 라이센스가 적용된 실제 PC(HW) 정보              |
|                    | `chat_sessions`        | 채팅 세션          | 도메인별 대화방, Rolling 요약 메모리            |
|                    | `chat_messages`        | 개별 대화 메시지   | 사용자 발화 및 AI 도구 호출 로그                |
| **Hybrid RAG**     | `documents_s3`         | 전역 문서          | 모든 조직이 공유하는 규격 문서 메타데이터       |
|                    | `document_chunks`      | 전역 문서 청크     | 하이브리드 RAG용 벡터/텍스트 데이터             |
|                    | `temp_documents`       | 임시 문서 (기업용) | 특정 기업이 업로드한 보안 문서                  |
|                    | `temp_document_chunks` | 임시 문서 청크     | 임시 문서의 하이브리드 RAG용 벡터/텍스트 데이터 |
| **Semantic**       | `standard_terms`       | 표준 용어 마스터   | AI가 인지하는 단일 공식 명칭 (SSOT)             |
|                    | `mapping_rules`        | 사용자 매핑 규칙   | 기업별 현장 은어를 표준 용어에 1:N 연결         |
| **CAD Review**     | `review_results`       | AI 검토 결과       | 탐지된 위반 사항 및 HITL 수정 파라미터 대기열   |

---

## 3. 테이블 상세 명세

### 1) Admin & B2B (시스템 관리 및 고객사)

#### 3.1. `system_admins` (시스템 관리자)

| 컬럼명          | 데이터 타입    | PK/FK | Null | Default              | 설명                   |
| :-------------- | :------------- | :---: | :--: | :------------------- | :--------------------- |
| `id`            | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 관리자 고유 식별자     |
| `email`         | `VARCHAR(255)` |   -   |  N   | -                    | 로그인 이메일 (UNIQUE) |
| `password_hash` | `TEXT`         |   -   |  N   | -                    | 비밀번호 해시          |
| `role`          | `VARCHAR(50)`  |   -   |  Y   | `'super_admin'`      | 권한 등급              |
| `created_at`    | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 계정 생성 일시         |

#### 3.2. `organizations` (고객사/기업)

| 컬럼명                   | 데이터 타입    | PK/FK | Null | Default              | 설명                           |
| :----------------------- | :------------- | :---: | :--: | :------------------- | :----------------------------- |
| `id`                     | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 기업 고유 식별자               |
| `company_name`           | `VARCHAR(200)` |   -   |  N   | -                    | 기업명                         |
| `admin_email`            | `VARCHAR(255)` |   -   |  N   | -                    | 기업 최고 관리자 계정 (UNIQUE) |
| `password_hash`          | `TEXT`         |   -   |  N   | -                    | 관리자 비밀번호 해시           |
| `plan`                   | `VARCHAR(20)`  |   -   |  N   | `'basic'`            | 구독 플랜 등급                 |
| `max_seats`              | `INT`          |   -   |  Y   | -                    | 허용 최대 단말기 수            |
| `daily_token_limit`      | `INT`          |   -   |  Y   | `NULL`               | 자정 리셋 기준 원본 토큰 한도  |
| `remaining_daily_tokens` | `INT`          |   -   |  Y   | `NULL`               | 실시간 사용 잔여 토큰          |
| `business_reg_number`    | `VARCHAR(50)`  |   -   |  Y   | -                    | 사업자등록번호                 |
| `business_reg_s3_url`    | `TEXT`         |   -   |  Y   | -                    | 사업자등록증 사본 URL          |
| `verification_status`    | `VARCHAR(20)`  |   -   |  N   | `'pending'`          | 인증 상태                      |
| `verified_by`            | `UUID`         |  FK   |  Y   | -                    | 인증 처리한 관리자             |
| `verified_at`            | `TIMESTAMPTZ`  |   -   |  Y   | -                    | 인증 일시                      |
| `is_active`              | `BOOLEAN`      |   -   |  N   | `true`               | 계정 활성화 여부               |
| `created_at`             | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 가입 일시                      |

---

### 2) Billing & Auth (결제, 인증 및 과금 로깅)

#### 3.3. `subscription_plans` (요금제 마스터)

| 컬럼명                 | 데이터 타입     | PK/FK | Null | Default    | 설명                     |
| :--------------------- | :-------------- | :---: | :--: | :--------- | :----------------------- |
| `plan_code`            | `VARCHAR(20)`   |  PK   |  N   | -          | 플랜 고유 코드           |
| `plan_name`            | `VARCHAR(50)`   |   -   |  N   | -          | UI 표시명                |
| `billing_cycle`        | `VARCHAR(20)`   |   -   |  N   | `'yearly'` | 결제 주기                |
| `base_price`           | `DECIMAL(12,2)` |   -   |  N   | -          | 기본 구독 요금           |
| `base_seats`           | `INT`           |   -   |  N   | -          | 기본 제공 PC 수          |
| `addon_price_per_seat` | `DECIMAL(12,2)` |   -   |  N   | -          | 추가 시트 1개당 단가     |
| `daily_token_limit`    | `INT`           |   -   |  Y   | `NULL`     | 일일 토큰 한도           |
| `per_seat_token_bonus` | `INT`           |   -   |  Y   | `0`        | 시트 추가 시 토큰 증가분 |
| `is_active`            | `BOOLEAN`       |   -   |  N   | `true`     | 판매 중 여부             |

#### 3.4. `licenses` (API 라이센스)

| 컬럼명       | 데이터 타입   | PK/FK | Null | Default              | 설명             |
| :----------- | :------------ | :---: | :--: | :------------------- | :--------------- |
| `id`         | `UUID`        |  PK   |  N   | `uuid_generate_v4()` | 라이센스 식별자  |
| `org_id`     | `UUID`        |  FK   |  N   | -                    | 소속 기업        |
| `api_key`    | `VARCHAR(64)` |   -   |  N   | -                    | 발급 키 (UNIQUE) |
| `status`     | `VARCHAR(20)` |   -   |  N   | `'active'`           | 활성 상태        |
| `starts_at`  | `TIMESTAMPTZ` |   -   |  N   | `CURRENT_TIMESTAMP`  | 시작일           |
| `expires_at` | `TIMESTAMPTZ` |   -   |  Y   | -                    | 만료일           |
| `created_at` | `TIMESTAMPTZ` |   -   |  N   | `CURRENT_TIMESTAMP`  | 발급 일시        |

#### 3.5. `payments` (결제 이력)

| 컬럼명                 | 데이터 타입     | PK/FK | Null | Default             | 설명                  |
| :--------------------- | :-------------- | :---: | :--: | :------------------ | :-------------------- |
| `id`                   | `INT`           |  PK   |  N   | `IDENTITY`          | 자동 증가 결제 식별자 |
| `org_id`               | `UUID`          |  FK   |  N   | -                   | 결제 기업             |
| `plan_name`            | `VARCHAR(20)`   |   -   |  N   | -                   | 플랜명                |
| `payment_type`         | `VARCHAR(20)`   |   -   |  N   | `'subscription'`    | 결제 유형             |
| `seats`                | `INT`           |   -   |  Y   | -                   | 결제 시점 총 시트 수  |
| `added_seats`          | `INT`           |   -   |  Y   | `NULL`              | 추가 시트 수          |
| `proration_days`       | `INT`           |   -   |  Y   | `NULL`              | 일할 계산 남은 일수   |
| `amount`               | `DECIMAL(12,2)` |   -   |  N   | -                   | 청구 금액             |
| `payment_method`       | `VARCHAR(50)`   |   -   |  Y   | -                   | 결제 수단             |
| `pg_provider`          | `VARCHAR(50)`   |   -   |  Y   | -                   | PG사                  |
| `pg_transaction_id`    | `VARCHAR(100)`  |   -   |  Y   | -                   | 거래 번호             |
| `status`               | `VARCHAR(20)`   |   -   |  N   | `'pending'`         | 상태                  |
| `generated_license_id` | `VARCHAR`       |   -   |  Y   | -                   | 발급된 라이센스       |
| `billing_period_start` | `TIMESTAMPTZ`   |   -   |  Y   | -                   | 보장 시작일           |
| `billing_period_end`   | `TIMESTAMPTZ`   |   -   |  Y   | -                   | 보장 종료일           |
| `completed_at`         | `TIMESTAMPTZ`   |   -   |  Y   | -                   | 결제 완료 일시        |
| `created_at`           | `TIMESTAMPTZ`   |   -   |  N   | `CURRENT_TIMESTAMP` | 내역 생성 일시        |

#### 3.6. `api_usage_logs` (API 사용 로그)

| 컬럼명              | 데이터 타입   | PK/FK | Null | Default             | 설명                  |
| :------------------ | :------------ | :---: | :--: | :------------------ | :-------------------- |
| `id`                | `INT`         |  PK   |  N   | `IDENTITY`          | 자동 증가 로그 식별자 |
| `org_id`            | `UUID`        |  FK   |  N   | -                   | 소속 기업             |
| `license_id`        | `UUID`        |  FK   |  N   | -                   | 대상 라이센스         |
| `date_dt`           | `DATE`        |   -   |  N   | `CURRENT_DATE`      | 집계 기준 일자        |
| `total_requests`    | `INT`         |   -   |  Y   | `0`                 | 총 호출 횟수          |
| `total_tokens_used` | `INT`         |   -   |  Y   | `0`                 | 소모 토큰 총합        |
| `created_at`        | `TIMESTAMPTZ` |   -   |  N   | `CURRENT_TIMESTAMP` | 생성 일시             |
| `updated_at`        | `TIMESTAMPTZ` |   -   |  N   | `CURRENT_TIMESTAMP` | 업데이트 일시         |

---

### 3) Interaction (클라이언트 연동 및 채팅)

#### 3.7. `devices` (등록 단말기)

| 컬럼명       | 데이터 타입    | PK/FK | Null | Default              | 설명               |
| :----------- | :------------- | :---: | :--: | :------------------- | :----------------- |
| `id`         | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 단말기 내부 식별자 |
| `license_id` | `UUID`         |  FK   |  N   | -                    | 라이센스 ID        |
| `machine_id` | `VARCHAR(128)` |   -   |  N   | -                    | PC 고유 식별값     |
| `is_active`  | `BOOLEAN`      |   -   |  N   | `true`               | 활성 상태          |
| `first_seen` | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 최초 접속          |
| `last_seen`  | `TIMESTAMPTZ`  |   -   |  Y   | -                    | 최근 접속          |

#### 3.8. `chat_sessions` (채팅 세션)

| 컬럼명                | 데이터 타입    | PK/FK | Null | Default              | 설명                                       |
| :-------------------- | :------------- | :---: | :--: | :------------------- | :----------------------------------------- |
| `id`                  | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 세션 고유 식별자                           |
| `device_id`           | `UUID`         |  FK   |  Y   | -                    | 단말기 ID (앱/마이그레이션: nullable 허용) |
| `domain_type`         | `VARCHAR(20)`  |   -   |  N   | -                    | 대화 도메인                                |
| `session_title`       | `VARCHAR(200)` |   -   |  Y   | -                    | 노출 제목                                  |
| `summary_text`        | `TEXT`         |   -   |  N   | `''`                 | 누적 대화 요약                             |
| `recent_chat`         | `TEXT`         |   -   |  N   | `''`                 | 최근 5턴 원문                              |
| `langgraph_thread_id` | `VARCHAR(100)` |   -   |  Y   | -                    | 스레드 ID                                  |
| `expires_at`          | `TIMESTAMPTZ`  |   -   |  Y   | -                    | 만료 시간                                  |
| `created_at`          | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 생성 시간                                  |
| `updated_at`          | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 업데이트 시간                              |

#### 3.9. `chat_messages` (개별 대화 메시지)

| 컬럼명              | 데이터 타입    | PK/FK | Null | Default             | 설명                    |
| :------------------ | :------------- | :---: | :--: | :------------------ | :---------------------- |
| `id`                | `INT`          |  PK   |  N   | `IDENTITY`          | 자동 증가 메시지 식별자 |
| `session_id`        | `UUID`         |  FK   |  N   | -                   | 소속 세션               |
| `role`              | `VARCHAR(20)`  |   -   |  N   | -                   | 작성자                  |
| `content`           | `TEXT`         |   -   |  N   | -                   | 텍스트 내용             |
| `agent_name`        | `VARCHAR(50)`  |   -   |  Y   | -                   | 응답 에이전트 이름      |
| `tool_calls`        | `TEXT`         |   -   |  Y   | -                   | 도구 호출 내역          |
| `tool_call_id`      | `VARCHAR(100)` |   -   |  Y   | -                   | 도구 매핑 ID            |
| `approval_status`   | `VARCHAR(20)`  |   -   |  Y   | -                   | 사용자 승인 상태        |
| `active_object_ids` | `TEXT`         |   -   |  Y   | -                   | 선택 객체 ID            |
| `token_count`       | `INT`          |   -   |  Y   | `0`                 | 소모 토큰               |
| `metadata`          | `TEXT`         |   -   |  Y   | -                   | 메타데이터              |
| `created_at`        | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP` | 생성 시간               |

---

### 4) Hybrid RAG (임베딩 및 문서 지식 베이스)

#### 3.10. `documents_s3` (전역 문서 메타데이터)

| 컬럼명       | 데이터 타입    | PK/FK | Null | Default              | 설명             |
| :----------- | :------------- | :---: | :--: | :------------------- | :--------------- |
| `id`         | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 전역 문서 식별자 |
| `file_name`  | `VARCHAR(255)` |   -   |  N   | -                    | 원본 파일명      |
| `s3_url`     | `TEXT`         |   -   |  N   | -                    | 파일 경로        |
| `created_at` | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 생성 시간        |

#### 3.11. `document_chunks` (전역 문서 청크)

| 컬럼명            | 데이터 타입    | PK/FK | Null | Default            | 설명                  |
| :---------------- | :------------- | :---: | :--: | :----------------- | :-------------------- |
| `id`              | `INT`          |  PK   |  N   | `IDENTITY`         | 자동 증가 청크 식별자 |
| `document_id`     | `UUID`         |  FK   |  N   | -                  | 원본 문서             |
| `chunk_index`     | `INT`          |   -   |  Y   | -                  | 순서 인덱스           |
| `content`         | `TEXT`         |   -   |  N   | -                  | 텍스트 원문           |
| `domain`          | `TEXT`         |   -   |  Y   | -                  | 도메인                |
| `doc_name`        | `VARCHAR(255)` |   -   |  Y   | -                  | 문서명                |
| `category`        | `TEXT`         |   -   |  Y   | -                  | 카테고리              |
| `section_id`      | `TEXT`         |   -   |  Y   | -                  | 조항 번호             |
| `effective_date`  | `DATE`         |   -   |  Y   | -                  | 유효 일자             |
| `chunk_type`      | `TEXT`         |   -   |  Y   | -                  | 청크 종류             |
| `dense_embedding` | `VECTOR(1024)` |   -   |  Y   | -                  | Dense 벡터            |
| `search_vector`   | `TSVECTOR`     |   -   |  Y   | `GENERATED ALWAYS` | Sparse(키워드) 벡터   |

#### 3.12. `temp_documents` (기업 전용 커스텀 시방서)

| 컬럼명        | 데이터 타입    | PK/FK | Null | Default              | 설명             |
| :------------ | :------------- | :---: | :--: | :------------------- | :--------------- |
| `id`          | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 임시 문서 식별자 |
| `org_id`      | `UUID`         |  FK   |  N   | -                    | 소유 기업        |
| `device_id`   | `UUID`         |  FK   |  Y   | -                    | 단말기 ID        |
| `domain`      | `VARCHAR(50)`  |   -   |  Y   | -                    | 도메인 지정      |
| `file_name`   | `VARCHAR(255)` |   -   |  N   | -                    | 파일명           |
| `temp_s3_url` | `TEXT`         |   -   |  N   | -                    | 임시 저장소 URL  |
| `comment`     | `TEXT`         |   -   |  Y   | -                    | 비고             |
| `status`      | `VARCHAR(20)`  |   -   |  Y   | `'pending'`          | 처리 상태        |
| `expires_at`  | `TIMESTAMPTZ`  |   -   |  Y   | -                    | 파기 기한        |
| `created_at`  | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 생성 시간        |

#### 3.13. `temp_document_chunks` (기업 전용 청크)

| 컬럼명             | 데이터 타입    | PK/FK | Null | Default            | 설명                  |
| :----------------- | :------------- | :---: | :--: | :----------------- | :-------------------- |
| `id`               | `INT`          |  PK   |  N   | `IDENTITY`         | 자동 증가 청크 식별자 |
| `temp_document_id` | `UUID`         |  FK   |  N   | -                  | 임시 문서             |
| `org_id`           | `VARCHAR`      |   -   |  Y   | -                  | 인덱싱 기준 소속 기업 |
| `chunk_index`      | `INT`          |   -   |  Y   | -                  | 인덱스                |
| `content`          | `TEXT`         |   -   |  N   | -                  | 원문                  |
| `domain`           | `VARCHAR(50)`  |   -   |  Y   | -                  | 도메인                |
| `doc_name`         | `VARCHAR(255)` |   -   |  Y   | -                  | 문서명                |
| `category`         | `VARCHAR(100)` |   -   |  Y   | -                  | 카테고리              |
| `section_id`       | `VARCHAR(50)`  |   -   |  Y   | -                  | 조항 번호             |
| `effective_date`   | `DATE`         |   -   |  Y   | -                  | 유효 일자             |
| `chunk_type`       | `VARCHAR(50)`  |   -   |  Y   | -                  | 청크 타입             |
| `dense_embedding`  | `VECTOR(1024)` |   -   |  Y   | -                  | Dense 벡터            |
| `search_vector`    | `TSVECTOR`     |   -   |  Y   | `GENERATED ALWAYS` | Sparse(키워드) 벡터   |

---

### 5) Semantic Standardization (용어 표준화 및 매핑)

#### 3.14. `standard_terms` (표준 용어 마스터)

| 컬럼명            | 데이터 타입    | PK/FK | Null | Default              | 설명             |
| :---------------- | :------------- | :---: | :--: | :------------------- | :--------------- |
| `id`              | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 표준 용어 식별자 |
| `domain`          | `VARCHAR(50)`  |   -   |  N   | -                    | 적용 분야        |
| `category`        | `VARCHAR(100)` |   -   |  Y   | -                    | 카테고리         |
| `standard_name`   | `VARCHAR(255)` |   -   |  N   | -                    | 공식 명칭        |
| `aliases`         | `JSONB`        |   -   |  Y   | -                    | 동의어 리스트    |
| `unit_type`       | `VARCHAR(50)`  |   -   |  Y   | -                    | 기본 스펙 단위   |
| `legal_reference` | `VARCHAR(255)` |   -   |  Y   | -                    | 근거/규격 조항   |
| `description`     | `TEXT`         |   -   |  Y   | -                    | 설명             |
| `is_active`       | `BOOLEAN`      |   -   |  Y   | `TRUE`               | 활성화 여부      |
| `created_at`      | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 생성 일시        |
| `updated_at`      | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 수정 일시        |

#### 3.15. `mapping_rules` (기업 맞춤 사용자 매핑 규칙)

| 컬럼명             | 데이터 타입    | PK/FK | Null | Default              | 설명               |
| :----------------- | :------------- | :---: | :--: | :------------------- | :----------------- |
| `id`               | `UUID`         |  PK   |  N   | `uuid_generate_v4()` | 매핑 규칙 식별자   |
| `org_id`           | `UUID`         |  FK   |  N   | -                    | 룰 보유 기업       |
| `domain`           | `VARCHAR(20)`  |   -   |  N   | -                    | 도메인             |
| `rule_type`        | `VARCHAR(30)`  |   -   |  Y   | -                    | 규칙 유형          |
| `source_key`       | `VARCHAR(200)` |   -   |  N   | -                    | 현장 입력 용어     |
| `standard_term_id` | `UUID`         |  FK   |  Y   | -                    | 연결 표준 용어     |
| `style_config`     | `JSONB`        |   -   |  Y   | -                    | 렌더링 스타일 정보 |
| `is_active`        | `BOOLEAN`      |   -   |  Y   | `TRUE`               | 활성화 여부        |
| `updated_at`       | `TIMESTAMPTZ`  |   -   |  N   | `CURRENT_TIMESTAMP`  | 최근 업데이트      |

---

### 6) CAD Review (도면 검토 및 자동 수정)

#### 3.16. `review_results` (AI 검토 결과 및 대기열)

(실DB/DBeaver 기준 — `public` 스키마)

| 컬럼명               | 데이터 타입                    | PK/FK | Null | Default             | 설명                                                                                                  |
| :------------------- | :----------------------------- | :---: | :--: | :------------------ | :---------------------------------------------------------------------------------------------------- |
| `id`                 | `int4` / `IDENTITY` **Always** |  PK   |  N   | (자동)              | 앱 `INSERT`는 `id` 열 **생략** → DB 발급. `fix_id` = `id`를 문자열로 반환                             |
| `session_id`         | `UUID`                         |  FK   |  N   | -                   | `chat_sessions` (동일 키 타입; 앱은 UUID 문자열 바인딩)                                               |
| `target_handle`      | `TEXT`                         |   -   |  N   | -                   | 위반/수정 대상(핸들 등)                                                                               |
| `violation_type`     | `TEXT`                         |   -   |  N   | -                   | 위반 종류                                                                                             |
| `violation_level`    | `TEXT`                         |   -   |  Y   | `'WARNING'`         | DBeaver Not Null이 꺼진 경우도 있음 — 앱은 명시 `WARNING`                                             |
| `action`             | `TEXT`                         |   -   |  Y   | `''`                | 동작 유형                                                                                             |
| `description`        | `TEXT`                         |   -   |  Y   | -                   | 위반 설명                                                                                             |
| `proposed_fix`       | `TEXT`                         |   -   |  Y   | -                   | JSON 문자열(수정 제안)                                                                                |
| `status`             | `public.fixstatus` (ENUM)      |   -   |  Y   | `PENDING`           | `PENDING` / `CONFIRMED` / `APPLIED` / `REJECTED`                                                      |
| `created_at`         | `timestamptz`                  |   -   |  Y   | `CURRENT_TIMESTAMP` |                                                                                                       |
| `updated_at`         | `timestamptz`                  |   -   |  Y   | `CURRENT_TIMESTAMP` |                                                                                                       |
| `reference_chunk_id` | `int4`                         |  FK?  |  Y   | -                   | `document_chunks.id` 등 시방 근거 청크(마이그레이션 `add_review_results_reference_chunk_id.sql` 참고) |
