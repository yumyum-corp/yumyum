# F704 Multi-Agent 주간 코칭 설계 스펙

**Date:** 2026-06-23
**Updated:** 2026-09-09
**Feature:** 기록 상태에 따라 전문 Agent를 선택하는 규칙 기반 주간 코칭
**Scope:** FastAPI `POST /ai/coaching/weekly` 내부 오케스트레이션

---

## 배경과 목표

식단·운동·체중 데이터를 하나의 긴 프롬프트로 분석하면 각 영역의 해석이 피상적일 수 있다. F704는 Nutrition, Exercise, Goal Agent가 역할별 분석을 담당하고 Synthesis Agent가 실행된 결과를 종합한다.

초기 구현은 세 전문 Agent와 Synthesis를 고정 실행했다. 2026-09-09 변경에서는 기록이 없는 영역까지 함수 경로에 포함되는 문제를 줄이기 위해 구조화된 입력을 코드로 판정하는 `CoachingRouter`를 도입했다. 이 Router는 LLM Supervisor가 아니며 LLM, DB, 외부 API를 호출하지 않는 결정론적 순수 함수다.

기존 API 경로와 응답 7개 필드는 유지한다.

---

## 배치 실행 모델

```text
[Spring @Scheduled — 월요일 새벽]
  1. 지난 주 Program 완료 Member 목록 조회
  2. Member별 POST /ai/coaching/weekly 호출
  3. 결과를 WeeklyReport에 저장

[월요일 오전 9시]
  4. Member가 앱을 열면 저장된 WeeklyReport 조회
```

배치가 전체 요청을 중단시키지 않도록 각 Agent 실패는 고정 fallback으로 격리한다.

---

## 전체 데이터 흐름

```text
WeeklyCoachingRequest
        ↓
CoachingFacts 계산
  - 실제 영양 기록 일수
  - 유효 운동 기록 수
  - 영양소별 평균 달성률
  - 칼로리 달성률·달성일
  - 주당 체중 추세
        ↓
규칙 기반 CoachingRouter
        ↓
Nutrition Agent ─┐
                 ├─ 둘 다 선택된 경우 asyncio.gather 병렬 실행
Exercise Agent ──┘
        ↓
필요한 경우 Goal Agent
        ↓
전문 Agent가 하나 이상이면 Synthesis Agent
        ↓
WeeklyCoachingResponse
```

Nutrition Agent가 선택되면 내부에서 두 번째 규칙 기반 정책이 RAG 필요성을 판정한다.

```text
평균 단백질·탄수화물·지방 달성률 확인
        ├─ 모두 80% 이상: ChromaDB 미호출
        └─ 하나 이상 80% 미만: 가장 부족한 영양소로 최대 3개 검색
                                  ↓
                     검색 결과가 있을 때만 프롬프트 근거로 추가
```

---

## 유효 데이터와 선택 규칙

Spring은 영양 기록이 없는 날도 값이 0인 배열 항목을 보낼 수 있다. 따라서 배열 길이가 아니라 `kcal`, `protein_g`, `carb_g`, `fat_g` 중 하나라도 양수인지 검사한다.

- `nutrition_days >= 1`: Nutrition 선택
- `total_sets > 0`인 운동 기록이 1개 이상: Exercise 선택
- `weight_trend is not None`: Goal 선택
- Nutrition과 Exercise가 모두 선택됨: 교차 도메인 평가를 위해 Goal 선택
- 어떤 전문 Agent도 선택되지 않음: Synthesis도 생략하고 고정 데이터 부족 문구 반환

Agent 순서는 `nutrition`, `exercise`, `goal`로 고정한다. 동일 입력은 동일한 `RoutingDecision`을 반환한다.

### 조건부 RAG

- Nutrition이 선택되지 않거나 실제 영양 기록이 없으면 검색하지 않는다.
- 목표값이 0인 영양소는 부족률 비교에서 제외한다.
- 비교 가능한 영양소 중 평균 달성률이 80% 미만인 값이 있으면 검색한다.
- 가장 낮은 영양소를 `priority_nutrient`로 사용한다.
- 검색 쿼리: `{HealthGoal 한글명} 목표, {priority_nutrient} 보충에 적합한 한식 식품`
- 기존 ChromaDB 정책인 최대 3개, L2 거리 1.5 이하 문서를 재사용한다.
- 동기 검색은 `asyncio.to_thread()`로 실행한다.
- 검색 실패 또는 결과 없음은 Nutrition Agent 실패로 전파하지 않는다.
- 근거 문서가 없으면 구체적인 식품별 영양 수치를 생성하지 않도록 프롬프트에 제한한다.

---

## 조합별 실행 경로

| 데이터 상태 | 실행 Agent | LLM 호출 | RAG 호출 |
|---|---|---:|---:|
| 기록 없음 | 없음 | 0 | 0 |
| 영양만, 충분 | Nutrition + Synthesis | 2 | 0 |
| 영양만, 부족 | Nutrition + Synthesis | 2 | 1 |
| 운동만 | Exercise + Synthesis | 2 | 0 |
| 체중 추세만 | Goal + Synthesis | 2 | 0 |
| 영양 + 체중 추세 | Nutrition + Goal + Synthesis | 3 | 0~1 |
| 운동 + 체중 추세 | Exercise + Goal + Synthesis | 3 | 0 |
| 영양 + 운동 | Nutrition + Exercise + Goal + Synthesis | 4 | 0~1 |
| 전체 데이터 | Nutrition + Exercise + Goal + Synthesis | 4 | 0~1 |

영양과 운동을 함께 실행할 때만 `asyncio.gather()`를 사용한다. Goal은 선택된 선행 분석 결과만 받고, Synthesis 프롬프트에도 실제 실행된 분석만 포함한다.

---

## API 계약

### 엔드포인트

`POST /ai/coaching/weekly`

### 요청

```python
class WeeklyCoachingRequest(BaseModel):
    week_number: int
    health_goal: Literal["DIET", "MUSCLE", "HEALTH", "DISEASE"]
    daily_nutrition: List[DailyNutritionRecord]
    target_kcal: float
    target_protein_g: float
    target_carb_g: float
    target_fat_g: float
    routine_sessions: List[RoutineSessionRecord]
    weight_records: List[WeightRecord]
```

### 응답

```python
class WeeklyCoachingResponse(BaseModel):
    ai_comment: str
    nutrition_summary: str
    exercise_summary: str
    goal_summary: str
    avg_calorie_rate: float
    achievement_days: int
    weight_trend: float | None
```

외부 응답에 `routing` 같은 메타데이터를 추가하지 않는다. 생략된 Agent의 summary에는 다음 고정 문구를 반환한다.

```text
분석 미실행: 해당 주차 영양 기록이 없습니다.
분석 미실행: 해당 주차 운동 기록이 없습니다.
분석 미실행: 체중 추세 또는 교차 분석 데이터가 부족합니다.
```

기록이 전혀 없을 때의 `ai_comment`:

```text
이번 주에는 분석할 식단·운동·체중 기록이 부족합니다. 기록을 추가하면 맞춤 코칭을 제공할 수 있습니다.
```

---

## 결정론적 계산과 LLM의 경계

코드가 계산하는 값:

- 실제 기록 존재 여부와 Agent 선택
- 일별·평균 칼로리 달성률
- 목표 달성일 수(80~120%)
- 영양소별 평균 달성률과 우선 영양소
- 주당 체중 변화량
- RAG 호출 필요성

LLM이 생성하는 값:

- 영양 패턴 해석
- 운동 성과 해석
- 목표 달성 궤도 설명
- 실행된 분석의 최종 코칭

---

## 장애 격리

- Nutrition 실패: `영양 분석 불가`
- Exercise 실패: `운동 분석 불가`
- Goal 실패: `목표 분석 불가`
- Synthesis 실패: 고정 격려 문구
- 체중 추세 계산 실패: `weight_trend=null`
- RAG 검색 실패: 빈 근거로 Nutrition 분석 계속
- 하나의 Agent 실패: 선택된 나머지 Agent와 Synthesis 계속 실행

Spring은 FastAPI가 200을 반환하면 기존 7개 필드를 WeeklyReport에 저장한다.

---

## 관측성

요청마다 `coaching_run_id`를 생성한다. `coaching_route` 구조화 로그에는 다음만 기록하고 식단 수치, 체중값, 프롬프트 전문은 기록하지 않는다.

- 선택·생략 Agent
- 실제 실행 Agent 함수 수
- 실제 외부 LLM 호출 시도 수
- RAG 호출 여부와 검색 적중 문서 수
- 전체 wall-clock 지연
- 고정 reason code

각 `claude_call` 로그에도 선택적으로 같은 `coaching_run_id`와 `operation`을 추가해 호출별 latency, token, cost 로그를 요청 단위로 연결한다. 다른 호출자는 기본값을 사용하므로 기존 함수 계약을 유지한다.

---

## 파일 구조

| 파일 | 역할 |
|---|---|
| `ai/app/services/coaching_router.py` | `CoachingFacts`, 순수 라우팅 결정 |
| `ai/app/services/coaching_rag_policy.py` | 부족률 판정, 검색 쿼리, 비동기 RAG wrapper |
| `ai/app/services/coaching_service.py` | 선택 실행, fallback, 구조화 로그 |
| `ai/app/services/claude_service.py` | 선택적 코칭 로그 context 연결 |
| `ai/tests/test_coaching_router.py` | 데이터 조합별 Router 단위 테스트 |
| `ai/tests/test_coaching_rag_policy.py` | 조건부 검색과 실패 격리 테스트 |
| `ai/tests/test_coaching_service.py` | 호출 수, 병렬화, fallback, 로그 테스트 |
| `ai/tests/test_ai_coaching.py` | API 7개 필드 회귀 테스트 |
| `ai/eval/eval_coaching_routing.py` | 고정 DAG와 동적 라우팅 비교 하니스 |

---

## 검증 결과

2026-09-09 로컬 mock 기준:

- F704 관련 테스트: `46 passed in 1.67s`
- 전체 AI 테스트: `197 passed in 15.03s`
- 신규 변경에서 추가된 pytest 경고 없음

동일비중 8개 fixture, 시나리오별 3회, warm-up 없음, LLM 호출당 고정 10ms mock 지연:

| 지표 | 고정 DAG | 동적 라우팅 | 변화 |
|---|---:|---:|---:|
| 평균 실행 Agent 수 | 4.0 | 2.5 | 37.5% 감소 |
| 평균 LLM 호출 수 | 3.0 | 2.5 | 16.7% 감소 |
| 평균 RAG 호출 수 | 0.0 | 0.5 | Nutrition 부족 fixture에서만 신규 호출 |
| 평균 mock 지연 | 36.500ms | 35.125ms | 구조 비교값 |
| p50 mock 지연 | 32.000ms | 47.000ms | 3회 표본 참고값 |
| p90 mock 지연 | 47.000ms | 47.000ms | 구조 비교값 |
| Agent fallback률 | 0% | 0% | mock 정상 응답 |
| 200 유지율 | 100% | 100% | mock 정상 응답 |

mock 지연은 Windows 이벤트 루프 스케줄링과 오케스트레이션 구조를 함께 측정한 값이며 실제 모델 응답시간 개선으로 표현하지 않는다.

같은 8개 fixture를 시나리오별 3회 실제 API로 탐색 측정한 잠정 결과:

| 지표 | 고정 DAG | 동적 라우팅 | 잠정 변화 |
|---|---:|---:|---:|
| 평균 지연 | 11.104초 | 8.673초 | 21.9% 감소 |
| p50 지연 | 11.656초 | 9.547초 | 18.1% 감소 |
| p90 지연 | 12.281초 | 12.235초 | 0.4% 감소 |
| 평균 입력 토큰 | 275.8 | 800.5 | 190.2% 증가 |
| 평균 출력 토큰 | 800.0 | 658.7 | 17.7% 감소 |
| 요청당 추정 비용 | $0.004276 | $0.004094 | 4.3% 감소 |
| Agent fallback률 | 0% | 0% | 동일 |
| 200 유지율 | 100% | 100% | 동일 |

실제 수치는 다음 한계 때문에 성과 수치가 아닌 참고값으로만 사용한다.

- 시나리오별 3회라 p90은 사실상 최댓값이다.
- 측정 당시 고정 DAG 프롬프트가 이전 구현보다 축약돼 입력 토큰 비교가 공정하지 않았다. 평가 하니스의 프롬프트 재현은 측정 후 수정했다.
- 로컬에 `sentence-transformers`가 설치되지 않아 모든 조건부 RAG가 빈 근거 fallback으로 처리됐다.
- 호출 수 감소율만 결정론적 구조 검증값으로 사용한다.

실행 명령:

```powershell
cd ai
python -m eval.eval_coaching_routing --mode mock --runs 3 --skip-warmup
python -m eval.eval_coaching_routing --mode real --runs 3 --skip-warmup --yes-real-api
```

real 모드는 비용이 발생하므로 `--yes-real-api` 없이는 실행되지 않는다.

---

## 현재 한계

- 규칙 기반 Router이며 자연어 의도나 자율 계획을 수행하는 LLM Supervisor가 아니다.
- 지식 베이스 25개와 L2 임계값 1.5는 운영 전 평가·확장이 필요하다.
- 1일 기록만으로도 Agent가 선택되므로 프롬프트가 기록 일수를 명시하지만 결론 과잉 위험은 남는다.
- 운영 분포 가중 결과와 보정된 실제 모델 지연·토큰·비용은 아직 측정하지 않았다.
- Agent별 응답 품질과 groundedness 평가셋이 없다.
- Spring WeeklyReport stub의 AI 호출 실패 후 자동 재시도는 별도 과제다.
