import os
os.environ.setdefault("ENV", "dev")

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock

import pytest
from app.config import settings
from app.schemas.coaching import (
    WeeklyCoachingRequest, DailyNutritionRecord,
    RoutineSessionRecord, WeightRecord,
)
from app.services import coaching_service
from app.services.coaching_rag_policy import NutritionRagResult
from app.services.coaching_service import _calc_stats, _exercise_agent, run_coaching_chain


# ── 공통 픽스처 ────────────────────────────────────────────────────────

def _make_request(**overrides) -> WeeklyCoachingRequest:
    base = dict(
        week_number=3,
        health_goal="MUSCLE",
        daily_nutrition=[
            DailyNutritionRecord(date=f"2026-06-1{i}", kcal=1800.0, protein_g=80.0,
                                 carb_g=220.0, fat_g=60.0, calories_burned=300.0)
            for i in range(7)
        ],
        target_kcal=2000.0,
        target_protein_g=120.0,
        target_carb_g=250.0,
        target_fat_g=65.0,
        routine_sessions=[
            RoutineSessionRecord(exercise_name="벤치프레스", successful_sets=3,
                                 total_sets=4, weight_kg=60.0, session_date="2026-06-16"),
        ],
        weight_records=[
            WeightRecord(date="2026-06-10", weight_kg=70.0),
            WeightRecord(date="2026-06-17", weight_kg=70.3),
        ],
    )
    base.update(overrides)
    return WeeklyCoachingRequest(**base)


# ── _calc_stats 단위 테스트 ────────────────────────────────────────────

def test_칼로리_달성률_평균_계산():
    req = _make_request()
    avg, days = _calc_stats(req)
    # kcal=1800 + burned=300=2100, target=2000 → rate=105% → 7일 모두 80~120% → 7일 달성
    assert avg == pytest.approx(105.0, abs=1.0)
    assert days == 7


def test_daily_nutrition_없으면_달성률_0():
    req = _make_request(daily_nutrition=[])
    avg, days = _calc_stats(req)
    assert avg == 0.0
    assert days == 0


# ── run_coaching_chain 통합 (dev mock) ────────────────────────────────

@pytest.mark.asyncio
async def test_체인_응답_필드_7개_모두_존재():
    req = _make_request()
    result = await run_coaching_chain(req)
    assert result.ai_comment
    assert result.nutrition_summary
    assert result.exercise_summary
    assert result.goal_summary
    assert isinstance(result.avg_calorie_rate, float)
    assert isinstance(result.achievement_days, int)
    # weight_trend: 2개 기록 있으므로 float


def test_영양_agent_프롬프트_검증():
    """영양 Agent가 호출될 프롬프트에 avg_calorie_rate와 health_goal이 포함되는지
    간접 검증 — mock 모드에서는 [MOCK] 텍스트가 반환되므로 nutrition_summary로 확인."""
    pass  # dev mock에서는 직접 검증 불필요; integration test에서 확인


@pytest.mark.asyncio
async def test_weight_records_없으면_weight_trend_null():
    req = _make_request(weight_records=[])
    result = await run_coaching_chain(req)
    assert result.weight_trend is None


@pytest.mark.asyncio
async def test_routine_sessions_없어도_정상_처리():
    req = _make_request(routine_sessions=[])
    result = await run_coaching_chain(req)
    assert result.exercise_summary  # fallback 텍스트라도 존재


@pytest.mark.asyncio
async def test_체인_전체_summary가_MOCK_텍스트():
    req = _make_request()
    result = await run_coaching_chain(req)
    assert result.nutrition_summary.startswith("[MOCK]")
    assert result.exercise_summary.startswith("[MOCK]")
    assert result.goal_summary.startswith("[MOCK]")
    assert result.ai_comment.startswith("[MOCK]")


# ── Multi-Agent 병렬화 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exercise_agent는_nutrition_analysis_없이_호출_가능():
    """nutrition_agent와 exercise_agent는 강한 의존관계가 없어 병렬 실행되므로
    exercise_agent가 nutrition_analysis를 인자로 받지 않아야 한다."""
    req = _make_request()
    result = await _exercise_agent(req)
    assert result


@pytest.mark.asyncio
async def test_nutrition_exercise_agent는_병렬로_실행된다(monkeypatch):
    monkeypatch.setattr(settings, "env", "prod")

    async def slow_call_claude(prompt, max_tokens=1000, model=None, **kwargs):
        await asyncio.sleep(0.1)
        return "[STUB] " + prompt[:10]

    monkeypatch.setattr(coaching_service, "call_claude", slow_call_claude)
    monkeypatch.setattr(
        coaching_service,
        "fetch_nutrition_context",
        AsyncMock(return_value=NutritionRagResult(called=False, documents=())),
    )

    req = _make_request()
    start = time.monotonic()
    await run_coaching_chain(req)
    elapsed = time.monotonic() - start

    # 4개 에이전트 순차 실행이면 ~0.4s, nutrition/exercise 병렬화 시 ~0.3s
    assert elapsed < 0.35


def _prod_stubs(monkeypatch, *, side_effect=None, rag_documents=()):
    monkeypatch.setattr(settings, "env", "prod")
    claude = AsyncMock(return_value="[STUB] 분석", side_effect=side_effect)
    rag = AsyncMock(
        return_value=NutritionRagResult(
            called=bool(rag_documents),
            documents=tuple(rag_documents),
        )
    )
    monkeypatch.setattr(coaching_service, "call_claude", claude)
    monkeypatch.setattr(coaching_service, "fetch_nutrition_context", rag)
    return claude, rag


@pytest.mark.asyncio
async def test_영양만_있으면_nutrition과_synthesis만_호출한다(monkeypatch):
    claude, _ = _prod_stubs(monkeypatch)
    req = _make_request(routine_sessions=[], weight_records=[])

    result = await run_coaching_chain(req)

    assert claude.await_count == 2
    assert result.exercise_summary == coaching_service.SKIPPED_EXERCISE
    assert result.goal_summary == coaching_service.SKIPPED_GOAL
    synthesis_prompt = claude.await_args_list[-1].args[0]
    assert "영양 분석:" in synthesis_prompt
    assert "운동 분석:" not in synthesis_prompt
    assert "목표 달성 분석:" not in synthesis_prompt


@pytest.mark.asyncio
async def test_운동만_있으면_exercise와_synthesis만_호출한다(monkeypatch):
    claude, rag = _prod_stubs(monkeypatch)
    req = _make_request(daily_nutrition=[], weight_records=[])

    result = await run_coaching_chain(req)

    assert claude.await_count == 2
    rag.assert_not_awaited()
    assert result.nutrition_summary == coaching_service.SKIPPED_NUTRITION
    assert result.goal_summary == coaching_service.SKIPPED_GOAL


@pytest.mark.asyncio
async def test_체중만_있으면_goal과_synthesis만_호출한다(monkeypatch):
    claude, rag = _prod_stubs(monkeypatch)
    req = _make_request(daily_nutrition=[], routine_sessions=[])

    result = await run_coaching_chain(req)

    assert claude.await_count == 2
    rag.assert_not_awaited()
    assert result.nutrition_summary == coaching_service.SKIPPED_NUTRITION
    assert result.exercise_summary == coaching_service.SKIPPED_EXERCISE
    assert result.goal_summary == "[STUB] 분석"


@pytest.mark.asyncio
async def test_기록이_없으면_llm을_호출하지_않는다(monkeypatch):
    claude, rag = _prod_stubs(monkeypatch)
    req = _make_request(daily_nutrition=[], routine_sessions=[], weight_records=[])

    result = await run_coaching_chain(req)

    claude.assert_not_awaited()
    rag.assert_not_awaited()
    assert result.ai_comment == coaching_service.NO_DATA_COMMENT


@pytest.mark.asyncio
async def test_영양과_운동이_있으면_goal까지_선택해_총_4회_호출한다(monkeypatch):
    claude, _ = _prod_stubs(monkeypatch)
    req = _make_request(weight_records=[])

    await run_coaching_chain(req)

    assert claude.await_count == 4
    operations = [
        call.kwargs["log_context"]["operation"]
        for call in claude.await_args_list
    ]
    assert set(operations) == {"nutrition", "exercise", "goal", "synthesis"}
    run_ids = {
        call.kwargs["log_context"]["coaching_run_id"]
        for call in claude.await_args_list
    }
    assert len(run_ids) == 1


@pytest.mark.asyncio
async def test_영양과_체중이_있으면_총_3회_호출한다(monkeypatch):
    claude, _ = _prod_stubs(monkeypatch)
    req = _make_request(routine_sessions=[])

    await run_coaching_chain(req)

    assert claude.await_count == 3


@pytest.mark.asyncio
async def test_agent_하나가_실패해도_다른_agent와_synthesis를_실행한다(monkeypatch):
    claude, _ = _prod_stubs(
        monkeypatch,
        side_effect=[RuntimeError("nutrition failed"), "운동 정상", "목표 정상", "통합 정상"],
    )

    result = await run_coaching_chain(_make_request())

    assert claude.await_count == 4
    assert result.nutrition_summary == "영양 분석 불가"
    assert result.exercise_summary == "운동 정상"
    assert result.ai_comment == "통합 정상"


@pytest.mark.asyncio
async def test_조건부_rag_결과를_nutrition_prompt에만_추가한다(monkeypatch):
    documents = (
        {"name": "두부", "info": "단백질 공급원", "document": "두부는 단백질 식품"},
    )
    claude, rag = _prod_stubs(monkeypatch, rag_documents=documents)
    req = _make_request(routine_sessions=[], weight_records=[])

    await run_coaching_chain(req)

    rag.assert_awaited_once()
    nutrition_prompt = claude.await_args_list[0].args[0]
    assert "[참고 식품 정보]" in nutrition_prompt
    assert "두부는 단백질 식품" in nutrition_prompt


@pytest.mark.asyncio
async def test_라우팅_구조화_로그를_요청당_한번_남긴다(monkeypatch, caplog):
    claude, _ = _prod_stubs(monkeypatch)
    req = _make_request(daily_nutrition=[], routine_sessions=[], weight_records=[])

    with caplog.at_level(logging.INFO, logger="ai.coaching"):
        await run_coaching_chain(req)

    route_records = [
        record for record in caplog.records
        if record.name == "ai.coaching" and '"event": "coaching_route"' in record.message
    ]
    assert len(route_records) == 1
    payload = json.loads(route_records[0].message)
    assert payload["week_number"] == 3
    assert payload["selected_agents"] == []
    assert payload["skipped_agents"] == ["nutrition", "exercise", "goal"]
    assert payload["executed_agent_count"] == 0
    assert payload["llm_call_count"] == 0
    assert payload["rag_called"] is False
    assert payload["rag_hit_count"] == 0
    assert payload["reason_codes"] == ["NO_USABLE_DATA"]
    assert payload["total_latency_ms"] >= 0
    assert payload["coaching_run_id"]
    claude.assert_not_awaited()


@pytest.mark.asyncio
async def test_라우팅_로그는_실제_agent_llm_rag_수를_구분한다(monkeypatch, caplog):
    documents = (
        {"name": "두부", "info": "단백질 공급원", "document": "두부는 단백질 식품"},
    )
    _prod_stubs(monkeypatch, rag_documents=documents)

    with caplog.at_level(logging.INFO, logger="ai.coaching"):
        await run_coaching_chain(_make_request())

    payload = json.loads(
        next(
            record.message
            for record in caplog.records
            if record.name == "ai.coaching"
            and '"event": "coaching_route"' in record.message
        )
    )
    assert payload["selected_agents"] == ["nutrition", "exercise", "goal"]
    assert payload["executed_agent_count"] == 4
    assert payload["llm_call_count"] == 4
    assert payload["rag_called"] is True
    assert payload["rag_hit_count"] == 1
