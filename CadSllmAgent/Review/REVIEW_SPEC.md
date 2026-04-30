# CAD-SLLM 검토 결과 출력 시스템 명세

> 담당: 김다빈 | WBS: RES-01 ~ RES-07  
> 관련 파일: `CadSllmAgent/Review/`

---

## 1. 전체 데이터 흐름

```
[AutoCAD 도면]
     │  C# 추출 (EXT-01, 양창일)
     ▼
[CAD JSON]  ──→  React (WebView2)
                      │  HTTP POST /api/v1/chat
                      ▼
               [Python 백엔드]
                      │  LLM + RAG 분석
                      ▼
               ChatResponse
               └─ annotated_entities   ← ★ 위반 정보 포함
                      │  WebView2 메시지: REVIEW_RESULT
                      ▼
               [C# WebViewMessageHandler]
                      │
              ┌───────┴────────┐
              ▼                ▼
       RevCloudDrawer    (위반 목록 패널 — RES-01)
       (RevCloud 생성)
              │
              ▼
       사용자: 승인(APPROVE_FIX) / 거절(REJECT_FIX)
              │
       승인 → DrawingPatcher.ApplyFix()  → 도면 직접 수정 + RevCloud 제거
       거절 → RevCloudDrawer.RemoveCloud() → RevCloud만 제거
```

---

## 2. JSON 계약 (Python ↔ C# 공통 스키마)

Python `ChatResponse` 에 추가된 `annotated_entities` 필드.  
**Python 에이전트 담당자는 이 구조로 응답을 채워야 합니다.**

```json
{
  "session_id": "uuid",
  "reply": "3건의 위반이 발견되었습니다.",
  "violations": [],
  "sources": [],
  "annotated_entities": [
    {
      "handle": "1A2B", // AutoCAD 객체 핸들 (16진수 문자열)
      "type": "BLOCK", // LINE / ARC / BLOCK / MTEXT / DIMENSION
      "layer": "E-CABLE",
      "bbox": {
        // RevCloud 생성 위치 (도면 좌표)
        "x1": 100.0,
        "y1": 200.0,
        "x2": 300.0,
        "y2": 350.0
      },
      "violation": {
        "id": "V001", // 고유 ID — 승인/거절 시 이 값으로 매칭
        "severity": "Critical", // Critical | Major | Minor
        "rule": "KEC 142.6", // 근거 법규 조항
        "description": "접지선 단면적 부족 (2.5SQ → 최소 4.0SQ)",
        "suggestion": "CABLE_SQ 속성을 4.0으로 변경",
        "auto_fix": {
          // null 이면 자동 수정 불가 (수동 처리)
          "type": "ATTRIBUTE", // ATTRIBUTE | LAYER | TEXT
          "attribute_tag": "CABLE_SQ",
          "new_value": "4.0"
        }
      }
    }
  ]
}
```

### auto_fix.type 종류

| type        | 설명                     | 필요 필드                    |
| ----------- | ------------------------ | ---------------------------- |
| `ATTRIBUTE` | 블록 속성값 변경         | `attribute_tag`, `new_value` |
| `LAYER`     | entity 레이어 변경       | `new_value` (레이어명)       |
| `TEXT`      | MText / DBText 내용 변경 | `new_value`                  |

---

## 3. IPC 메시지 프로토콜 (React ↔ C#)

모든 메시지 형식: `{ "action": string, "payload": any }`

### React → C# (신규 추가)

| action          | payload                                                       | 시점                   |
| --------------- | ------------------------------------------------------------- | ---------------------- |
| `REVIEW_RESULT` | `ReviewResult` 객체 전체                                      | HTTP 응답 수신 후 즉시 |
| `APPROVE_FIX`   | `{ "violation_id": "V001" }` 또는 `{ "violation_id": "ALL" }` | 승인 버튼 클릭 시      |
| `REJECT_FIX`    | `{ "violation_id": "V001" }`                                  | 거절 버튼 클릭 시      |

### C# → React (신규 추가)

| action        | payload                                   | 시점                            |
| ------------- | ----------------------------------------- | ------------------------------- |
| `FIX_APPLIED` | `{ "violation_id": "V001" }` 또는 `"ALL"` | 도면 수정/RevCloud 제거 완료 후 |

> **React 담당 (김민정)**: `listenFromCad`로 `FIX_APPLIED` 수신 시 해당 위반 항목의 승인/거절 버튼 UI 제거

---

## 4. 파일별 코드 설명

### `ReviewModels.cs` — 데이터 모델

Python JSON을 C#에서 받기 위한 역직렬화 클래스들.

```
ReviewResult
  └─ List<AnnotatedEntity>
        ├─ Handle, Type, Layer      : 원본 CAD entity 정보
        ├─ BoundingBox              : RevCloud 그릴 위치 (x1, y1, x2, y2)
        └─ ViolationInfo
              ├─ Id, Severity, Rule, Description, Suggestion
              └─ AutoFix?           : C#이 자동 수정할 내용 (없으면 null)
```

### `RevCloudDrawer.cs` — RevCloud 생성/제거 (RES-03, RES-04)

**핵심 동작:**

- `DrawAll(entities)` — 위반 entity 전체를 한 번의 트랜잭션으로 처리
- `RemoveCloud(violationId)` — XData에 저장된 violation_id로 정확히 해당 객체만 삭제

**RevCloud 원리:**  
AutoCAD .NET API에는 별도의 RevCloud 객체가 없음.  
→ `Polyline`의 각 vertex에 `bulge = 0.5` 를 주면 볼록 호(arc)가 생성됨.  
→ 직사각형 bbox의 각 변을 `arcStep` 간격으로 잘게 나눠 여러 호를 이어 붙임.

**레이어 규칙:**

- 모든 RevCloud + MText는 `AI_REVIEW` 전용 레이어에만 저장
- 원본 도면 레이어에는 영향 없음

**심각도별 색상 (ACI):**

| Severity | 색상 | ACI 코드 |
| -------- | ---- | -------- |
| Critical | 빨강 | 1        |
| Major    | 주황 | 30       |
| Minor    | 노랑 | 50       |

**XData (숨은 메타데이터):**  
각 RevCloud + MText에 `CADSLLM` 앱 이름으로 `violation_id` 를 XData에 저장.  
→ `RemoveCloud` 에서 violation_id로 정확히 해당 객체만 찾아 삭제 가능.

### `DrawingPatcher.cs` — 도면 직접 수정 (Q3)

**핵심 동작:**

- `ApplyFix(entity)` — `auto_fix.type` 에 따라 도면 entity 직접 수정
- 수정 성공 시 자동으로 `RevCloudDrawer.RemoveCloud()` 호출 (위반 해소 표시)
- `Handle` → `ObjectId` 변환으로 도면에서 정확한 객체를 찾음

**핸들이란?**  
AutoCAD의 모든 객체는 고유한 16진수 Handle을 가짐.  
Python이 EXT-01에서 추출한 handle 값을 그대로 넘겨주면 C#이 그 객체를 찾아 수정.

### `WebViewMessageHandler.cs` — IPC 메시지 라우터

새로 추가된 액션 처리 흐름:

```
REVIEW_RESULT  → _pendingEntities 저장 → RevCloudDrawer.DrawAll()
APPROVE_FIX    → DrawingPatcher.ApplyFix() → FIX_APPLIED 전송
REJECT_FIX     → RevCloudDrawer.RemoveCloud() → FIX_APPLIED 전송
```

`_pendingEntities` 는 인스턴스 필드로 세션 동안 유지.  
`APPROVE_FIX` 에서 `violation_id: "ALL"` 수신 시 전체 위반 일괄 승인.

---

## 5. 데모 테스트 방법 (백엔드 없이 확인)

### 사전 준비

1. AutoCAD 닫기
2. 빌드: `cd CadSllmAgent && dotnet build`
3. AutoCAD 열기
4. 명령창: `NETLOAD` → `CadSllmAgent/bin/Debug/net10.0-windows/CadSllmAgent.dll` 선택

### 데모 실행

```
DEMOREVIEW   ← 3개 위반 RevCloud 생성 + 화면 자동 맞춤
```

화면에 아래 3개가 나타나야 함:

| 위치 (도면 좌표)    | 색상 | 내용                         |
| ------------------- | ---- | ---------------------------- |
| (0,0) ~ (120,60)    | 빨강 | KEC 142.6 접지선 단면적 부족 |
| (160,0) ~ (320,90)  | 주황 | ASME B31.3 배관 두께 미달    |
| (0,130) ~ (200,220) | 노랑 | 건축법 복도 폭 미달          |

### 승인 테스트

```
DEMOFIX      ← 빨강(Critical) RevCloud 제거 확인
```

명령창 로그: `[DEMO] DEMO-V001 RevCloud 제거 완료`  
→ 빨강 RevCloud + MText가 도면에서 사라지면 정상

### 초기화

```
CLEANDEMO    ← AI_REVIEW 레이어 전체 삭제
```

---

## 6. Python 에이전트 담당자 연동 가이드

`backend/services/agents/` 의 각 에이전트에서 `ChatResponse` 반환 시:

```python
from backend.api.schemas.chat import (
    ChatResponse, AnnotatedEntity, ViolationInfo, BoundingBox, AutoFix
)

# BaseAgent.run() 에서 반환하는 ChatResponse에 annotated_entities 추가
return ChatResponse(
    session_id=req.session_id,
    reply="2건의 위반이 발견되었습니다.",
    violations=[...],
    sources=[...],
    annotated_entities=[
        AnnotatedEntity(
            handle="1A2B",           # EXT-01 추출값 그대로 사용
            type="BLOCK",
            layer="E-CABLE",
            bbox=BoundingBox(x1=100, y1=200, x2=300, y2=350),
            violation=ViolationInfo(
                id="V001",
                severity="Critical",
                rule="KEC 142.6",
                description="접지선 단면적 부족",
                suggestion="CABLE_SQ 속성을 4.0으로 변경",
                auto_fix=AutoFix(
                    type="ATTRIBUTE",
                    attribute_tag="CABLE_SQ",
                    new_value="4.0"
                )
            )
        )
    ]
)
```

**bbox 좌표는 EXT-01(양창일)이 추출하는 entity의 실제 도면 좌표를 그대로 사용.**  
entity별 bbox는 EXT-01 출력 JSON의 `extents` 또는 `geometry` 필드에서 읽어옴.

---

## 7. React 연동 가이드 (김민정)

```typescript
// 1. HTTP 응답에서 annotated_entities 수신 후 C#에 전송
const response = await fetch('/api/v1/chat', { ... });
const data = await response.json();

// C#에 검토 결과 전달 (RevCloud 생성 트리거)
sendToCad('REVIEW_RESULT', {
  session_id: data.session_id,
  reply: data.reply,
  annotated_entities: data.annotated_entities,
});

// 2. 위반 항목별 승인/거절 버튼 렌더링
data.annotated_entities.forEach(entity => {
  if (!entity.violation) return;
  // 승인 버튼
  // sendToCad('APPROVE_FIX', { violation_id: entity.violation.id })
  // 거절 버튼
  // sendToCad('REJECT_FIX', { violation_id: entity.violation.id })
});

// 3. C#에서 FIX_APPLIED 수신 시 버튼 제거
listenFromCad((msg) => {
  if (msg.action === 'FIX_APPLIED') {
    const id = msg.payload.violation_id;
    // id === 'ALL' 이면 전체 제거, 아니면 해당 항목만 제거
  }
});
```
