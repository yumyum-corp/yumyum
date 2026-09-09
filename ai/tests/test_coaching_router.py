from dataclasses import replace

import pytest

from app.schemas.coaching import (
    DailyNutritionRecord,
    RoutineSessionRecord,
    WeeklyCoachingRequest,
    WeightRecord,
)
from app.services.coaching_router import CoachingFacts, build_facts, decide_route


def _request(**overrides) -> WeeklyCoachingRequest:
    values = {
        "week_number": 3,
        "health_goal": "MUSCLE",
        "daily_nutrition": [],
        "target_kcal": 2000.0,
        "target_protein_g": 120.0,
        "target_carb_g": 250.0,
        "target_fat_g": 65.0,
        "routine_sessions": [],
        "weight_records": [],
    }
    values.update(overrides)
    return WeeklyCoachingRequest(**values)


def _facts(**overrides) -> CoachingFacts:
    values = {
        "nutrition_days": 0,
        "exercise_count": 0,
        "weight_record_count": 0,
        "avg_calorie_rate": 0.0,
        "avg_protein_rate": None,
        "avg_carb_rate": None,
        "avg_fat_rate": None,
        "achievement_days": 0,
        "weight_trend": None,
        "priority_nutrient": None,
    }
    values.update(overrides)
    return CoachingFacts(**values)


def test_값이_모두_0인_7일_배열은_영양_기록이_아니다():
    records = [
        DailyNutritionRecord(
            date=f"2026-09-{day:02d}",
            kcal=0,
            protein_g=0,
            carb_g=0,
            fat_g=0,
        )
        for day in range(1, 8)
    ]

    facts = build_facts(_request(daily_nutrition=records))

    assert facts.nutrition_days == 0
    assert decide_route(facts).selected_agents == ()


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (_facts(nutrition_days=1), ("nutrition",)),
        (_facts(exercise_count=1), ("exercise",)),
        (_facts(weight_record_count=2, weight_trend=0.2), ("goal",)),
        (
            _facts(nutrition_days=1, exercise_count=1),
            ("nutrition", "exercise", "goal"),
        ),
        (
            _facts(nutrition_days=1, weight_record_count=2, weight_trend=-0.3),
            ("nutrition", "goal"),
        ),
        (
            _facts(exercise_count=1, weight_record_count=2, weight_trend=0.1),
            ("exercise", "goal"),
        ),
        (_facts(), ()),
    ],
)
def test_데이터_조합에_맞는_agent를_선택한다(facts, expected):
    assert decide_route(facts).selected_agents == expected


def test_selected와_skipped_agent는_겹치지_않고_전체를_이룬다():
    decision = decide_route(_facts(nutrition_days=1, weight_trend=0.1))

    assert set(decision.selected_agents).isdisjoint(decision.skipped_agents)
    assert set(decision.selected_agents + decision.skipped_agents) == {
        "nutrition",
        "exercise",
        "goal",
    }


def test_동일_입력은_항상_동일한_결정을_반환한다():
    facts = _facts(
        nutrition_days=3,
        avg_protein_rate=70.0,
        priority_nutrient="protein",
    )

    assert decide_route(facts) == decide_route(replace(facts))


def test_여러_goal_선택_근거는_reason_code에_모두_남긴다():
    decision = decide_route(
        _facts(nutrition_days=1, exercise_count=1, weight_trend=0.2)
    )

    assert "WEIGHT_TREND_AVAILABLE" in decision.reason_codes
    assert "CROSS_DOMAIN_EVALUATION" in decision.reason_codes


def test_목표값이_0이어도_나눗셈_오류가_발생하지_않는다():
    record = DailyNutritionRecord(
        date="2026-09-01",
        kcal=500,
        protein_g=30,
        carb_g=40,
        fat_g=10,
    )

    facts = build_facts(
        _request(
            daily_nutrition=[record],
            target_kcal=0,
            target_protein_g=0,
            target_carb_g=0,
            target_fat_g=0,
        )
    )

    assert facts.avg_calorie_rate == 100.0
    assert facts.avg_protein_rate is None
    assert facts.avg_carb_rate is None
    assert facts.avg_fat_rate is None
    assert facts.priority_nutrient is None
    assert decide_route(facts).use_nutrition_rag is False
