# 사진 식단 분석 정확도 평가

`POST /ai/meal/analyze-photo`의 음식명·그램·kcal 추정 정확도를 숫자로 측정한다.

## 왜 필요한가

`docs/adr/2026-06-23-vision-ai-photo-meal.md`의 ADR-1은 MFDS DB 조회(Option A)
대신 Vision 직접 추정(Option B)을 택했고, 근거의 핵심이 다음 주장이다.

> 영양소 오차의 주요 원인은 계수가 아니라 그램 추정 오차 → 두 방식의 정확도 차이 미미

**이 주장은 아직 측정된 적이 없다.** 이 하니스가 그것을 가른다. 결과에 따라
ADR-1을 근거와 함께 유지하거나 재검토한다.

### 무엇으로 가르는가

ADR-1은 두 방식의 **비교**인데 Option B만 재면 판정이 아니라 추정에 그친다.
그래서 두 축으로 나눠 본다.

**축 1 — 밀도(kcal/g) 오차.** kcal 오차는 그램 오차와 계수 오차의 합이고,
부호가 반대면 상쇄된다. 그램을 30% 과대추정하면서 계수를 25% 과소추정하면
kcal 오차는 작게 나온다 — 둘 다 큰데도. 밀도(kcal/g) 오차는 그램을 소거한
**순수 계수 오차**라서 ADR-1의 "계수가 아니라 그램" 주장이 여기서만 갈린다.

**축 2 — Option A를 같은 사진에 실제로 돌린다.** 매칭된 항목마다 Vision이
추정한 그램 × MFDS 밀도로 kcal을 다시 계산한다. 두 arm이 그램 추정을
공유하므로 차이는 계수에서만 나온다 — 논쟁 중인 변수 그 자체다.
Claude 호출은 늘지 않고, MFDS는 무료 공공 API라 추가 비용이 없다.

조회 이름 2가지 × 레코드 선택 규칙 2가지를 모두 잰다.

| 축 | 값 | 뜻 |
| --- | --- | --- |
| 이름 | `pred` | Vision이 감지한 이름 — Option A의 충실한 재현 |
| 이름 | `oracle` | 정답 이름 — 이름을 완벽히 맞혔을 때 |
| 선택 | `median` | 정확 일치 후보들의 중앙값 — 현실적으로 고를 수 있는 값 |
| 선택 | `oracle` | 정답 kcal에 가장 가까운 후보 — **달성 불가능한 상한** |

**오라클 규칙으로도 Option B를 못 이기면 ADR-1은 결정적으로 입증된다.**
반대로 상한은 좋은데 현실이 나쁘면, ADR-1의 결론은 맞되 근거가
"계수 차이 미미"가 아니라 "이름→레코드 매칭 불가"로 바뀐다.

### 정확 일치만 쓴다

MFDS 부분검색은 관련도순이 아니다. `닭가슴살`의 첫 결과는
`샌드위치_닭가슴살`(240 kcal/100g)이다. 그래서 `mfds_lookup`은 최대 5페이지
(500건)를 훑어 **이름이 정확히 일치하는** 레코드만 모으고, 없으면 조회 실패로
센다. 틀린 음식의 계수를 쓰느니 실패가 정직하다.

정확 일치도 한 건이 아니다. 실측(2026-09-11, 흔한 15개 조회):

| 조회명 | 정확일치 | kcal/100g 범위 |
| --- | --- | --- |
| 닭가슴살 · 샐러드 · 고구마 | **0건** | 1000건을 훑어도 없음 |
| 김치찌개 | 25건 | 16~140 (8.8배) |
| 된장찌개 | 12건 | 21~140 (6.7배) |
| 라면 | 6건 | 79~369 (4.7배) |
| 비빔밥 | 8건 | 112~320 (2.9배) |
| 바나나 | 1건 | 454 (건조 바나나. 생것은 89) |

**정확일치 실패 20%, 성공해도 후보끼리 5~9배 흩어진다.** 이 모호성 자체가
Option A의 실제 성능이므로 후보 수와 스프레드를 1급 지표로 기록한다.

| 측정 결과 | 해석 |
| --- | --- |
| 밀도 MAPE가 그램 MAPE의 절반 미만 | ADR-1이 옳다. 개선 방향은 그램 추정 프롬프트 |
| 밀도 MAPE가 그램 MAPE 이상 | 계수 오차가 실재한다. ADR-1의 전제가 깨진다 |
| kcal MAPE가 그램·밀도 둘 다보다 작음 | 두 오차가 상쇄된 착시. kcal MAPE로 판정 금지 |
| Option A(현실) < Option B × 0.8 | DB 조회가 유의하게 정확하다. "차이 미미" 반증 |
| Option A(상한)조차 Option B보다 나쁨 | ADR-1 결정적 입증. DB를 어떻게 붙여도 못 이긴다 |
| 상한은 좋은데 현실이 나쁨 | 결론은 유지, **근거를 "매칭 불가"로 고쳐 적는다** |
| 정확일치 실패율이 `composite`에서 급등 | ADR-1의 "복합 한식 DB 매칭 불가" 근거 성립 |
| 음식명 F1이 낮음 | 어느 설계든 무관한 상위 문제. 모델·프롬프트 이슈로 분리 |

## 평가셋 만들기

`eval/dataset/labels.jsonl`에 사진 1장당 한 줄. 사진은 `eval/dataset/images/`에 둔다.
(`eval/dataset/`과 `eval/results/`는 gitignore된다 — 개인 사진이고 용량도 크다.)

스키마는 `dataset.example.jsonl` 참고.

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `id` | O | 사진 식별자 |
| `image` | O | `eval/dataset/` 기준 상대경로 |
| `meal_type` | O | `BREAKFAST` / `LUNCH` / `DINNER` / `SNACK` |
| `kind` | O | `single` 단품 / `multi` 여러 접시 / `composite` 복합 한식 |
| `items[].name` | O | 정답 음식명 |
| `items[].grams` | O | 실제 무게. 저울 실측 또는 포장 표기 |
| `items[].kcal` | - | 있으면 kcal 오차도 측정. 없으면 그 항목만 제외 |
| `items[].kcal_source` | kcal 있으면 O | `package` / `scale+db` / `mfds` — 아래 참고 |
| `items[].aliases` | - | 모델이 다르게 부를 수 있는 이름들 |
| `note` | - | 무게 근거 등 메모 |

### `kcal_source`를 반드시 채워라

**kcal 정답을 MFDS에서 베껴오면 Option A 비교가 순환논증이 된다.** MFDS 밀도를
MFDS 유래 정답으로 채점하는 셈이라 Option A가 당연히 이긴다. 출처를 적어야
지표를 갈라 보고 그 항목을 판정에서 뺄 수 있다. 스크립트가 출처별로 분해해서
출력하고, `mfds` 항목이 섞여 있으면 경고한다.

| 값 | 의미 | 비교에 쓸 수 있나 |
| --- | --- | --- |
| `package` | 포장 표기 kcal | O — 가장 독립적 |
| `scale+db` | 저울 실측 무게 + 별도 출처 kcal | O |
| `mfds` | 식품안전처 DB에서 가져온 kcal | X — Option A 비교에서 제외됨 |

### 구성

**30~50장이면 충분하다.** 대신 `kind`를 섞어야 한다 — `composite`가 없으면
ADR-1의 핵심 주장 하나를 검증하지 못한다.

`composite`(비빔밥·찌개)는 저울로 무게는 재도 kcal 정답을 얻을 방법이 사실상
없다. 그램 전용 샘플만 쌓이면 복합 한식 주장은 또 미검증으로 남는다.
**편의점·프랜차이즈 도시락(비빔밥 도시락, 김밥)이 `composite` + 신뢰 가능한
kcal(`package`)을 동시에 주는 거의 유일한 경로다.** 라벨링 시작 전에 이 비중을
잡아둬라. 저울이 없으면 포장 표기가 있는 편의점 식품부터 시작하면 된다.

## 실행

```bash
cd ai

# 1. 파이프라인 점검 (Claude 호출 없음, mock 응답이라 지표는 무의미)
python -m eval.eval_photo_meal --dataset eval/dataset/labels.jsonl --dry-run

# 2. 실제 측정 — 비용이 발생하므로 --yes-real-api 를 명시해야 한다
python -m eval.eval_photo_meal \
    --dataset eval/dataset/labels.jsonl \
    --prompt prod --yes-real-api \
    --out eval/results/prod.json

# 3. 프롬프트 A/B
python -m eval.eval_photo_meal --dataset eval/dataset/labels.jsonl \
    --prompt hinted --yes-real-api --out eval/results/hinted.json
python -m eval.eval_photo_meal --compare eval/results/prod.json eval/results/hinted.json

# 4. 모델 비교 — 정확도만이 아니라 정확도/비용 트레이드오프가 나온다
python -m eval.eval_photo_meal --dataset eval/dataset/labels.jsonl \
    --prompt prod --model claude-haiku-4-5-20251001 --yes-real-api \
    --out eval/results/haiku.json
```

`--no-mfds`로 Option A arm을 끌 수 있다. 단 그러면 ADR-1 정면 비교가 빠지고
축 1(밀도 오차)만 남는다.

## 주의

**`ENV`는 스크립트가 직접 고정한다.** `app.config.settings`는 최초 import 시
한 번 생성되는 프로세스 전역 싱글턴이라 import 전에 정해야 한다.
`tests/conftest.py`가 `dev`를 강제하는 것과 같은 이유이고 방향만 반대다.
그래서 이 스크립트는 `tests/` 밖에 있다 — pytest로 수집되면 conftest가
`dev`를 고정해 실제 측정이 불가능하다.

**MFDS 키가 필요하다.** `ai/.env`의 `MFDS_API_KEY`(공공데이터포털
data.go.kr 발급, 무료). `ENV=prod`여도 값이 `mock-key`면 `search_food_mfds`가
mock DB를 반환해 Option A arm이 조용히 가짜가 된다 — 스크립트가 이 경우
경고한다. 루트 `.env`의 `FOOD_DB_API_KEY`는 FastAPI가 읽지 않으니 채워도
무효다(pydantic-settings는 필드명 `mfds_api_key`에 대응하는 `MFDS_API_KEY`만
본다). Option A arm 없이 돌리려면 `--no-mfds`.

조회량은 고유 음식명 1개당 최대 5회(페이지)다. 사진 30~50장이면 고유명
60개 내외 × 2개 arm이라 300회 안팎 — data.go.kr 개발계정 일일 쿼터 안에
들어간다. 같은 이름은 캐시되어 다시 묻지 않는다.

**비용 로깅.** `claude_service._log_claude_call`이 남기는 latency·토큰·비용을
그대로 주워 집계한다. `_PRICING_USD_PER_1M`에 등록되지 않은 모델은 `cost_usd`가
`None`으로 나가고 스크립트가 경고한다. vision 기본 모델
(`claude-opus-4-5-20251101`, $5/$25 per MTok)과 `claude-haiku-4-5-20251001`은
등록되어 있다.

**`prod` 프롬프트는 `app/routers/ai_meal.py:analyze_photo`의 사본이다.**
라우터 프롬프트를 바꾸면 `PROMPTS["prod"]`도 같이 바꿔야 측정이 의미를 갖는다.
`--max-tokens` 기본값(800)도 라우터와 맞춰져 있다.
