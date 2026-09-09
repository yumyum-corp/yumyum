# F704 Multi-Agent 동적 라우팅 구현 플랜

> **상태:** 구현 완료 (3회 탐색 계측 완료)
> **작성일:** 2026-09-09
> **범위:** FastAPI의 `POST /ai/coaching/weekly` 내부 Agent 라우팅과 Nutrition Agent의 조건부 RAG 호출
> **제외:** LLM Supervisor, MCP, Tool Calling, Spring DTO 및 DB 변경

## Implementation Result

- F704 관련 테스트: `46 passed in 1.67s`
- 전체 AI 테스트: `197 passed in 15.03s`
- 동일비중 8개 fixture × 3회 mock 계측, warm-up 없음:
  - 평균 실행 Agent 수: `4.0 → 2.5` (37.5% 감소)
  - 평균 LLM 호출 수: `3.0 → 2.5` (16.7% 감소)
  - 평균 mock 지연: `36.500ms → 35.125ms`
  - p90 mock 지연: `47.000ms → 47.000ms`
- 동일 조건 실제 API 탐색 계측:
  - 평균 지연: `11.104초 → 8.673초` (잠정 21.9% 감소)
  - p90 지연: `12.281초 → 12.235초` (잠정 0.4% 감소)
  - 요청당 비용: `$0.004276 → $0.004094` (잠정 4.3% 감소)
- 실제 수치는 표본이 3회이고, 비교 당시 고정 DAG 프롬프트가 축약됐으며
  `sentence-transformers` 미설치로 RAG가 fallback되어 참고값으로만 사용한다.

## Goal

현재 모든 요청에서 영양·운동·목표 Agent를 고정 실행하는 F704 주간 코칭을, 실제 기록의 존재와 분석 필요성에 따라 필요한 Agent만 선택하는 **규칙 기반 동적 라우팅**으로 변경한다. Nutrition Agent가 선택된 경우에도 구체적인 식품 근거가 필요할 때만 기존 ChromaDB RAG를 호출한다.

기존 API 경로와 응답 7개 필드는 그대로 유지한다.

## Current State

```text
수치 계산
   ↓
Nutrition Agent ─┐
                 ├─ 항상 병렬 실행
Exercise Agent ──┘
   ↓
Goal Agent 항상 실행
   ↓
Synthesis Agent 항상 실행
```

- 고정 DAG
- 최대 LLM 호출 4회
- 기록이 없는 영역도 고정 문구 또는 Agent 실행 경로에 포함
- 어떤 Agent가 선택·생략됐는지 구조적으로 표현하지 않음

## Target State

```text
WeeklyCoachingRequest
        ↓
CoachingFacts 계산
        ↓
결정론적 CoachingRouter
        ↓
┌─────────────────────────┐
│ 선택된 Nutrition Agent   │
│ 선택된 Exercise Agent    │  ← 둘 다 선택되면 병렬 실행
└─────────────────────────┘
        ↓
Nutrition Agent 선택 시 RAG 필요성 판정
        ├─ 불필요: 수치 기반 분석만 수행
        └─ 필요: ChromaDB 검색 후 근거를 프롬프트에 포함
        ↓
필요한 경우 Goal Agent
        ↓
전문 Agent가 있으면 Synthesis Agent
        ↓
기존 WeeklyCoachingResponse 반환
```

## Routing Policy

### 유효 데이터 판정

Spring은 영양 기록이 없는 날도 값이 0인 일별 항목을 전송한다. 따라서 `daily_nutrition`의 길이가 아니라 실제 영양소 값을 검사한다.

```python
def has_nutrition(record) -> bool:
    return any([
        record.kcal > 0,
        record.protein_g > 0,
        record.carb_g > 0,
        record.fat_g > 0,
    ])
```

계산할 사실:

- `nutrition_days`: 실제 영양 기록이 있는 날짜 수
- `exercise_count`: `total_sets > 0`인 운동 기록 수
- `weight_record_count`: 체중 기록 수
- `weight_trend`: 기존 `calc_weight_trend()` 결과
- `avg_calorie_rate`: 기존 평균 칼로리 달성률
- `achievement_days`: 기존 목표 달성일 수

### Agent 선택 규칙

1. `nutrition_days >= 1`이면 Nutrition Agent를 선택한다.
2. `exercise_count >= 1`이면 Exercise Agent를 선택한다.
3. 다음 중 하나를 만족하면 Goal Agent를 선택한다.
   - `weight_trend is not None`
   - Nutrition과 Exercise가 모두 선택돼 교차 도메인 평가가 필요함
4. 선택된 전문 Agent가 하나도 없으면 Synthesis Agent도 호출하지 않는다.
5. 전문 Agent가 하나 이상 있으면 Synthesis Agent를 실행한다.

### 조건부 RAG 호출 규칙

RAG는 Nutrition Agent가 선택됐다는 이유만으로 항상 호출하지 않는다. `외부 식품 지식이 필요한 구체적인 추천인가`를 두 번째 라우팅 단계에서 코드로 판단한다.

1. Nutrition Agent가 선택되지 않았으면 RAG를 호출하지 않는다.
2. 실제 영양 기록이 없으면 RAG를 호출하지 않는다.
3. 단순 달성률 요약만 필요하면 RAG를 호출하지 않는다.
4. 평균 단백질·탄수화물·지방 달성률 중 하나가 80% 미만이면 RAG를 호출한다.
5. 가장 달성률이 낮은 영양소를 `priority_nutrient`로 결정한다.
6. 목표값이 0인 영양소는 부족률 비교 대상에서 제외한다.
7. 기존 검색 정책인 최대 3개 문서와 L2 거리 1.5 이하 기준을 재사용한다.
8. 검색 결과가 없거나 검색이 실패하면 근거 없는 구체적 식품 수치를 생성하지 않고 일반적인 영양 패턴 분석만 수행한다.

검색 쿼리 형식:

```text
{HealthGoal 한글명} 목표, {priority_nutrient} 보충에 적합한 한식 식품
```

예시:

```text
근육 증가 목표, 단백질 보충에 적합한 한식 식품
다이어트 목표, 탄수화물 보충에 적합한 한식 식품
```

### 예상 호출 수

| 데이터 상태 | 실행 Agent | LLM 호출 수 | RAG 호출 수 |
|---|---|---:|---:|
| 기록 없음 | 없음 | 0회 | 0회 |
| 영양만 있음, 영양소 충분 | Nutrition + Synthesis | 2회 | 0회 |
| 영양만 있음, 영양소 부족 | Nutrition + Synthesis | 2회 | 1회 |
| 운동만 있음 | Exercise + Synthesis | 2회 | 0회 |
| 체중 2개 이상만 있음 | Goal + Synthesis | 2회 | 0회 |
| 영양 + 체중 추세 | Nutrition + Goal + Synthesis | 3회 | 0~1회 |
| 운동 + 체중 추세 | Exercise + Goal + Synthesis | 3회 | 0회 |
| 영양 + 운동 | Nutrition + Exercise + Goal + Synthesis | 4회 | 0~1회 |
| 전체 데이터 | Nutrition + Exercise + Goal + Synthesis | 4회 | 0~1회 |

위 수치는 구현 후 실제 호출 mock으로 검증한다. RAG 검색은 LLM 호출 수에 포함하지 않는다. 구현 전에는 비용·지연 개선 실적으로 사용하지 않는다.

## Design Decisions

### 규칙 기반 Router

- 입력이 자연어가 아니라 구조화된 주간 기록이므로 LLM Router를 사용하지 않는다.
- 동일 입력에 동일 경로가 선택되도록 순수 함수로 구현한다.
- Router 자체에서는 설정, DB, 외부 API, LLM을 호출하지 않는다.
- 추가 LLM 호출 없이 라우팅할 수 있어 비용과 실패 지점이 늘지 않는다.

### 기존 API 계약 유지

`WeeklyCoachingResponse`의 다음 7개 필드를 변경하지 않는다.

1. `ai_comment`
2. `nutrition_summary`
3. `exercise_summary`
4. `goal_summary`
5. `avg_calorie_rate`
6. `achievement_days`
7. `weight_trend`

생략된 Agent의 summary에는 `null` 대신 고정 문구를 넣어 Spring DTO와 WeeklyReport 저장 흐름을 유지한다.

```text
nutrition_summary: 분석 미실행: 해당 주차 영양 기록이 없습니다.
exercise_summary: 분석 미실행: 해당 주차 운동 기록이 없습니다.
goal_summary: 분석 미실행: 체중 추세 또는 교차 분석 데이터가 부족합니다.
```

### 병렬 실행 유지

- Nutrition과 Exercise가 모두 선택되면 기존처럼 `asyncio.gather()`로 병렬 실행한다.
- 한 Agent만 선택되면 해당 Agent만 직접 실행한다.
- Goal은 선행 분석 결과가 필요한 후속 단계로 둔다.
- Synthesis는 실행된 전문 Agent 결과만 프롬프트에 포함한다.

### RAG는 Nutrition Agent 내부의 2차 라우팅

- 1차 Router는 어떤 전문 Agent를 실행할지 결정한다.
- 2차 `NutritionRagPolicy`는 Nutrition Agent가 외부 식품 지식을 필요로 하는지 결정한다.
- RAG 필요성은 LLM이 아닌 평균 영양소 달성률로 판정한다.
- 기존 동기 `rag_service.search()`는 `asyncio.to_thread()`로 감싸 이벤트 루프 차단을 피한다.
- RAG 실패는 Nutrition Agent 실패로 취급하지 않는다.
- 검색 문서가 있을 때만 Nutrition 프롬프트에 `[참고 식품 정보]`를 추가한다.

### 기록 없음 처리

전문 Agent가 하나도 선택되지 않으면 LLM을 호출하지 않고 다음 결정론적 응답을 반환한다.

```text
이번 주에는 분석할 식단·운동·체중 기록이 부족합니다. 기록을 추가하면 맞춤 코칭을 제공할 수 있습니다.
```

## Internal Models

`ai/app/services/coaching_router.py`에 내부 모델을 둔다.

```python
from dataclasses import dataclass
from typing import Literal

AgentName = Literal["nutrition", "exercise", "goal"]


@dataclass(frozen=True)
class CoachingFacts:
    nutrition_days: int
    exercise_count: int
    weight_record_count: int
    avg_calorie_rate: float
    avg_protein_rate: float | None
    avg_carb_rate: float | None
    avg_fat_rate: float | None
    achievement_days: int
    weight_trend: float | None
    priority_nutrient: str | None


@dataclass(frozen=True)
class RoutingDecision:
    selected_agents: tuple[AgentName, ...]
    skipped_agents: tuple[AgentName, ...]
    use_nutrition_rag: bool
    reason_codes: tuple[str, ...]
```

권장 reason code:

- `NUTRITION_DATA_PRESENT`
- `EXERCISE_DATA_PRESENT`
- `WEIGHT_TREND_AVAILABLE`
- `CROSS_DOMAIN_EVALUATION`
- `NUTRIENT_DEFICIT_RAG`
- `NO_USABLE_DATA`

라우팅 메타데이터는 이번 범위에서 외부 응답에 추가하지 않고 구조화 로그와 테스트에서만 사용한다.

## File Map

| 파일 | 변경 내용 |
|---|---|
| `ai/app/services/coaching_router.py` | 신규: CoachingFacts와 순수 라우팅 함수 |
| `ai/app/services/coaching_rag_policy.py` | 신규: RAG 필요성 판정, 검색 쿼리 생성, 비동기 검색 래퍼 |
| `ai/app/services/coaching_service.py` | 고정 실행을 선택 실행 오케스트레이터로 변경 |
| `ai/tests/test_coaching_router.py` | 신규: 데이터 조합별 라우팅 단위 테스트 |
| `ai/tests/test_coaching_rag_policy.py` | 신규: 조건부 RAG 호출과 실패 처리 테스트 |
| `ai/tests/test_coaching_service.py` | Agent 호출 여부, 병렬 실행, fallback 테스트 보강 |
| `ai/tests/test_ai_coaching.py` | 기존 응답 7개 필드와 데이터 조합별 API 회귀 테스트 |
| `ai/eval/eval_coaching_routing.py` | 신규: 고정 DAG와 동적 라우팅 호출 수·지연·토큰·비용 비교 |
| `docs/superpowers/specs/2026-06-23-f704-multi-agent-coaching-design.md` | 구현 완료 후 고정 DAG 설명을 동적 라우팅으로 갱신 |
| `docs/kb-ai-platform-application-materials.md` | 구현·계측 완료 후 정확한 수치 반영 |

## Task 0. Baseline 확인

- [ ] 사용자 또는 다른 작업자의 기존 변경사항을 확인한다.
- [ ] 현재 AI 전체 테스트를 실행한다.

```powershell
git status --short
cd ai
python -m pytest -q
```

Expected baseline: `165 passed`.

- [ ] F704 관련 테스트만 실행한다.

```powershell
python -m pytest tests/test_coaching_service.py tests/test_ai_coaching.py -q
```

Expected baseline: `14 passed`.

## Task 1. CoachingRouter 단위 테스트 작성

**Files:**

- Create: `ai/tests/test_coaching_router.py`

다음 테스트를 먼저 작성한다.

- [ ] 값이 모두 0인 7일 배열은 영양 기록 없음으로 판정한다.
- [ ] 영양 기록만 있으면 Nutrition만 선택한다.
- [ ] 운동 기록만 있으면 Exercise만 선택한다.
- [ ] 체중 추세만 있으면 Goal만 선택한다.
- [ ] 영양과 운동이 모두 있으면 Nutrition, Exercise, Goal을 선택한다.
- [ ] 영양과 체중 추세가 있으면 Nutrition, Goal을 선택한다.
- [ ] 운동과 체중 추세가 있으면 Exercise, Goal을 선택한다.
- [ ] 모든 기록이 없으면 어떤 Agent도 선택하지 않는다.
- [ ] `selected_agents`와 `skipped_agents`가 중복되지 않는다.
- [ ] 동일 입력은 항상 동일한 결정을 반환한다.
- [ ] 목표값이 0이어도 0으로 나누기 오류가 발생하지 않는다.

테스트 실행:

```powershell
cd ai
python -m pytest tests/test_coaching_router.py -q
```

Expected before implementation: import error or failing tests.

## Task 2. CoachingRouter 구현

**Files:**

- Create: `ai/app/services/coaching_router.py`

- [ ] `CoachingFacts`를 정의한다.
- [ ] `RoutingDecision`을 정의한다.
- [ ] `build_facts(req) -> CoachingFacts`를 구현한다.
- [ ] 기존 체중 추세 계산 실패 시 `weight_trend=None`을 유지한다.
- [ ] `decide_route(facts) -> RoutingDecision` 순수 함수를 구현한다.
- [ ] reason code를 고정된 값으로 반환한다.
- [ ] 외부 서비스에 의존하지 않는다.
- [ ] Task 1 테스트를 통과시킨다.

Acceptance:

- 모든 데이터 조합에서 선택 결과가 Routing Policy와 일치한다.
- Router 테스트는 mock 없이 실행된다.
- 기존 통계 계산 결과와 달성일 계산 규칙이 바뀌지 않는다.

## Task 3. Agent 호출 여부를 검증하는 테스트 추가

**Files:**

- Modify: `ai/tests/test_coaching_service.py`

`settings.env`를 `prod`로 monkeypatch하고 실제 LLM 대신 AsyncMock을 사용한다.

- [ ] 영양만 있을 때 Exercise·Goal Agent가 호출되지 않는지 검증한다.
- [ ] 운동만 있을 때 Nutrition·Goal Agent가 호출되지 않는지 검증한다.
- [ ] 체중만 있을 때 Nutrition·Exercise Agent가 호출되지 않는지 검증한다.
- [ ] 기록이 없을 때 `call_claude()`가 0회인지 검증한다.
- [ ] 영양+운동일 때 전문 Agent 2개와 Goal, Synthesis가 호출되는지 검증한다.
- [ ] 영양+체중일 때 총 3회 호출되는지 검증한다.
- [ ] 한 Agent 실패 후에도 선택된 다른 Agent와 Synthesis가 실행되는지 검증한다.
- [ ] 생략된 summary가 고정 문구인지 검증한다.

Acceptance:

- 동적 라우팅은 반환 문자열이 아니라 mock 호출 횟수와 호출 인자로 증명한다.

## Task 4. 선택 실행 오케스트레이터 구현

**Files:**

- Modify: `ai/app/services/coaching_service.py`

- [ ] 기존 `_calc_stats()`의 중복 계산을 `build_facts()`로 이동하거나 호환 wrapper로 유지한다.
- [ ] `run_coaching_chain()` 시작 시 RoutingDecision을 생성한다.
- [ ] 선택된 Agent만 실행한다.
- [ ] Nutrition과 Exercise가 둘 다 선택된 경우에만 `asyncio.gather()`를 사용한다.
- [ ] Goal이 선택된 경우 선행 결과와 체중 추세를 전달한다.
- [ ] Goal이 생략되면 고정 summary를 사용한다.
- [ ] 전문 Agent가 하나도 없으면 Synthesis를 호출하지 않는다.
- [ ] Synthesis 프롬프트에는 실제 실행된 분석만 포함한다.
- [ ] 기존 Agent별 예외 처리와 fallback을 유지한다.

구현 형태 예시:

```python
async def run_coaching_chain(req: WeeklyCoachingRequest) -> WeeklyCoachingResponse:
    facts = build_facts(req)
    decision = decide_route(facts)

    nutrition = SKIPPED_NUTRITION
    exercise = SKIPPED_EXERCISE
    goal = SKIPPED_GOAL

    if has_both(decision, "nutrition", "exercise"):
        nutrition, exercise = await asyncio.gather(
            _nutrition_agent(req, facts.avg_calorie_rate),
            _exercise_agent(req),
        )
    elif is_selected(decision, "nutrition"):
        nutrition = await _nutrition_agent(req, facts.avg_calorie_rate)
    elif is_selected(decision, "exercise"):
        exercise = await _exercise_agent(req)

    if is_selected(decision, "goal"):
        goal = await _goal_agent(req, facts.weight_trend, nutrition, exercise)

    if not decision.selected_agents:
        ai_comment = NO_DATA_COMMENT
    else:
        ai_comment = await _synthesis_agent(
            req.health_goal,
            nutrition,
            exercise,
            goal,
        )

    return WeeklyCoachingResponse(...)
```

실제 구현에서는 Synthesis 프롬프트가 생략 문구를 분석 결과로 오해하지 않도록 선택된 결과만 전달하는 helper를 둔다.

## Task 5. 라우팅 구조화 로그 추가

**Files:**

- Modify: `ai/app/services/coaching_service.py`
- Modify: `ai/tests/test_coaching_service.py`

요청당 한 번 다음 정보를 남긴다.

```json
{
  "event": "coaching_route",
  "coaching_run_id": "generated-per-request",
  "week_number": 3,
  "selected_agents": ["nutrition", "exercise", "goal"],
  "skipped_agents": [],
  "executed_agent_count": 4,
  "llm_call_count": 4,
  "rag_called": true,
  "rag_hit_count": 3,
  "total_latency_ms": 824.3,
  "reason_codes": [
    "NUTRITION_DATA_PRESENT",
    "EXERCISE_DATA_PRESENT",
    "CROSS_DOMAIN_EVALUATION",
    "NUTRIENT_DEFICIT_RAG"
  ]
}
```

- [ ] Python `logging`을 사용한다.
- [ ] 식단 수치, 체중값, 프롬프트 전문은 로그에 남기지 않는다.
- [ ] 요청 시작 시 로그 집계용 `coaching_run_id`를 생성한다.
- [ ] 선택·생략 Agent, 실제 실행 Agent 수, 실제 LLM 호출 수를 구분한다.
- [ ] RAG 호출 여부와 검색 결과 수를 기록한다.
- [ ] 전체 오케스트레이션의 `total_latency_ms`를 기록한다.
- [ ] 로그 테스트는 메시지 전문보다 구조 필드를 검증한다.

용어 정의:

- `executed_agent_count`: 실제 실행한 Nutrition·Exercise·Goal·Synthesis 함수 수
- `llm_call_count`: 실제 외부 LLM 호출 수. 데이터 없음 또는 함수 내부 조기 반환은 제외
- `rag_called`: ChromaDB 검색 함수 실행 여부
- `rag_hit_count`: 거리 임계값을 통과한 검색 문서 수
- `total_latency_ms`: Router 진입부터 WeeklyCoachingResponse 생성까지의 wall-clock 시간

현재 `claude_service`의 구조화 로그에는 호출별 지연시간·입력 토큰·출력 토큰·추정 비용이 이미 존재한다. 실제 비용 비교 시 동일 `coaching_run_id`로 묶을 수 있도록 선택적 로그 context 또는 operation 필드를 추가하되, 기존 호출자의 함수 계약은 깨지지 않게 기본값을 둔다.

## Task 6. API 회귀 테스트

**Files:**

- Modify: `ai/tests/test_ai_coaching.py`

- [ ] 기존 요청이 계속 200을 반환한다.
- [ ] 기존 응답 7개 필드가 모두 존재한다.
- [ ] `routing` 같은 신규 외부 필드가 추가되지 않았음을 검증한다.
- [ ] 빈 영양·운동·체중 요청도 200을 반환한다.
- [ ] 빈 요청의 `ai_comment`가 데이터 부족 안내인지 검증한다.
- [ ] `weight_records`가 2개 미만이면 `weight_trend=null`이다.
- [ ] 기존 HealthGoal validation이 유지된다.

## Task 7. 전체 테스트 및 동작 검증

```powershell
cd ai
python -m pytest tests/test_coaching_router.py tests/test_coaching_service.py tests/test_ai_coaching.py -q
python -m pytest -q
```

- [ ] 신규 동적 라우팅 테스트가 모두 통과한다.
- [ ] 기존 AI 테스트 165개가 회귀 없이 통과한다.
- [ ] 최종 테스트 수와 실행시간을 기록한다.
- [ ] 테스트 경고가 신규 변경에서 발생하지 않았는지 확인한다.

수동 검증 시나리오:

- [ ] 기록 없음 → LLM 0회
- [ ] 영양만 → Nutrition + Synthesis
- [ ] 운동만 → Exercise + Synthesis
- [ ] 체중만 → Goal + Synthesis
- [ ] 영양+체중 → Nutrition + Goal + Synthesis
- [ ] 운동+체중 → Exercise + Goal + Synthesis
- [ ] 영양+운동 → Nutrition·Exercise 병렬 + Goal + Synthesis
- [ ] Agent 하나 실패 → fallback으로 200 유지

## Task 8. 호출 수·응답시간·비용 Before/After 측정

**Files:**

- Create: `ai/eval/eval_coaching_routing.py`

동일 fixture를 기준으로 고정 DAG와 동적 라우팅의 호출 수, 응답시간, 토큰, 비용을 비교한다. 평균값은 입력 데이터 분포에 따라 달라지므로 측정에 사용한 fixture 구성과 반복 횟수를 결과에 반드시 함께 기록한다.

### 고정 비교 시나리오

다음 8개 fixture를 같은 비중으로 사용한다.

1. 기록 없음
2. 영양만 있음
3. 운동만 있음
4. 체중만 있음
5. 영양 + 체중
6. 운동 + 체중
7. 영양 + 운동
8. 영양 + 운동 + 체중

비용 제한에 따라 각 fixture를 3회 반복한 탐색 결과를 먼저 기록한다. 이 표본의 p90은 사실상 최댓값이므로 참고값으로만 사용한다. 실제 운영 데이터 분포를 확보하면 반복 수를 늘리고 동일 비중 결과와 운영 분포 가중 결과를 별도로 제시한다.

### 최소 측정 항목

- 요청당 평균 실행 Agent 수
- 요청당 평균 LLM 호출 수
- 요청당 평균 RAG 호출 수
- 평균 응답시간
- p50·p90 응답시간
- 평균 입력·출력 토큰
- 요청당 평균 추정 비용
- Agent fallback률
- 200 응답 유지율

### 1단계: Mock 구조 비교

- `call_claude()`를 고정 지연 AsyncMock으로 교체한다.
- Agent 선택 수와 LLM 호출 수는 정확히 비교할 수 있다.
- 고정 지연을 사용한 응답시간은 오케스트레이션 구조 비교값이며 실제 모델 성능으로 표현하지 않는다.
- RAG도 고정 결과 mock을 사용해 호출 여부만 비교한다.

현재 코드의 Agent 함수는 데이터가 없으면 외부 LLM 호출 전에 조기 반환한다. 따라서 고정 DAG의 `함수 실행 수`는 항상 최대 4개지만, 실제 `LLM 호출 수`는 데이터에 따라 2~4회다. 두 지표를 섞지 않는다.

동일 비중 8개 fixture에서 예상되는 구조적 기준:

| 지표 | 고정 DAG | 동적 라우팅 | 예상 변화 |
|---|---:|---:|---:|
| 평균 실행 Agent 수 | 4.0 | 2.5 | 37.5% 감소 |
| 평균 외부 LLM 호출 수 | 3.0 | 2.5 | 16.7% 감소 |

위 값은 현재 코드의 조기 반환 규칙과 제안된 라우팅 정책으로 계산한 **예상값**이다. 구현 후 mock 호출 계수로 다시 검증해 실측값으로 교체한다.

### 2단계: 실제 API 비교

- 실제 API 호출은 비용이 발생하므로 `--yes-real-api` 플래그가 없으면 실행하지 않는다.
- 동일 모델, 동일 fixture, 동일 반복 횟수를 사용한다.
- 호출별 구조화 로그에서 latency, input tokens, output tokens, cost를 `coaching_run_id`로 합산한다.
- 네트워크 변동을 고려해 평균뿐 아니라 p50과 p90을 함께 기록한다.
- 실제 API 비교가 불가능하면 비용·응답시간 개선 수치는 미측정으로 남긴다.

실행 인터페이스 예시:

```powershell
cd ai
python -m eval.eval_coaching_routing --mode mock --runs 3 --skip-warmup
python -m eval.eval_coaching_routing --mode real --runs 3 --skip-warmup --yes-real-api
```

결과 JSON 예시:

```json
{
  "dataset": "8-scenarios-equal-weight",
  "runs_per_scenario": 20,
  "fixed": {
    "avg_agent_calls": 4.0,
    "avg_llm_calls": 3.0,
    "latency_ms_mean": 0.0,
    "latency_ms_p90": 0.0,
    "cost_usd_mean": null
  },
  "dynamic": {
    "avg_agent_calls": 2.5,
    "avg_llm_calls": 2.5,
    "latency_ms_mean": 0.0,
    "latency_ms_p90": 0.0,
    "cost_usd_mean": null
  }
}
```

수치 사용 원칙:

- 호출 수 감소율은 mock 검증 결과를 사용할 수 있다.
- 응답시간 개선율은 동일 환경 반복 측정 후에만 사용한다.
- 실제 모델 비용 절감률은 실제 토큰·비용 로그가 없으면 주장하지 않는다.
- 8개 동일 비중 fixture 결과를 실제 사용자 트래픽의 평균이라고 표현하지 않는다.

## Task 9. 문서 갱신

- [ ] F704 설계 문서의 고정 DAG 설명을 동적 라우팅으로 변경한다.
- [ ] 실제 Agent 선택 규칙과 호출 수 표를 추가한다.
- [ ] 고정 DAG와 동적 라우팅의 평균 Agent 수·LLM 호출 수·응답시간·비용 비교 결과를 추가한다.
- [ ] mock 결과와 실제 API 결과를 명확히 구분한다.
- [ ] LLM Supervisor가 아닌 규칙 기반 Router임을 명시한다.
- [ ] 구현 후 측정된 호출 수와 테스트 수를 지원 소재 문서에 반영한다.

## Risks

| 위험 | 대응 |
|---|---|
| 0으로 채운 영양 배열을 실제 기록으로 오판 | 리스트 길이가 아니라 칼로리·영양소 양수 여부 검사 |
| Agent 생략으로 기존 응답 필드가 null | 고정 생략 문구로 기존 계약 유지 |
| 선택 조합 증가로 테스트 복잡도 상승 | Router를 순수 함수로 분리하고 조합별 parameterized test 사용 |
| Synthesis가 생략 문구를 실제 분석으로 오해 | 선택된 Agent 결과만 별도 helper로 프롬프트에 포함 |
| 체중 기록이 있어도 추세 계산 실패 | 기존처럼 `weight_trend=None`, Goal 선택 여부에 반영 |
| 1일 기록만으로 주간 결론이 과도함 | 프롬프트에 기록 일수를 포함하고 데이터 부족을 명시 |
| 평균 수치가 fixture 분포에 따라 달라짐 | 시나리오와 가중치를 결과에 함께 기록 |
| mock 지연을 실제 응답시간처럼 오해 | mock은 구조 비교, 실제 API는 별도 opt-in 측정 |
| 비용 로그를 요청별로 묶기 어려움 | `coaching_run_id`로 호출 로그를 연계 |

## Definition of Done

- [ ] 데이터 상태에 따라 Nutrition·Exercise·Goal Agent가 선택 또는 생략된다.
- [ ] Nutrition Agent가 선택돼도 영양소 부족 조건에서만 RAG가 호출된다.
- [ ] RAG 검색 실패가 Nutrition Agent 또는 전체 요청 실패로 전파되지 않는다.
- [ ] Nutrition과 Exercise가 함께 선택되면 병렬 실행된다.
- [ ] 기록이 없는 요청은 LLM을 호출하지 않는다.
- [ ] Agent 하나의 실패가 전체 요청 실패로 전파되지 않는다.
- [ ] 기존 API 경로와 응답 7개 필드가 유지된다.
- [ ] 동적 라우팅 선택 결과가 단위 테스트와 호출 mock으로 검증된다.
- [ ] 기존 165개 AI 테스트와 신규 테스트가 모두 통과한다.
- [ ] 데이터 조합별 Before/After 호출 수가 문서화된다.
- [ ] 평균 Agent 호출 수, 평균 LLM 호출 수, 평균·p90 응답시간을 고정 fixture 기준으로 기록한다.
- [ ] 실제 API를 실행한 경우에만 토큰과 비용 변화율을 기록한다.
- [ ] 구현 문서에서 규칙 기반 Router와 LLM Supervisor를 구분한다.

## Suggested Commits

1. `test(coaching): 동적 라우팅 정책 시나리오 추가`
2. `feat(coaching): 데이터 기반 CoachingRouter 구현`
3. `feat(coaching): Nutrition Agent 조건부 RAG 호출 추가`
4. `test(coaching): 선택 Agent와 RAG 호출 조건 검증`
5. `refactor(coaching): 고정 DAG를 동적 선택 실행으로 변경`
6. `feat(coaching): 라우팅 구조화 로그 추가`
7. `test(coaching): 동적 라우팅 API 회귀 테스트 보강`
8. `feat(eval): 동적 라우팅 호출 수와 비용 비교 도구 추가`
9. `docs(coaching): F704 동적 라우팅 계측 결과 기록`
