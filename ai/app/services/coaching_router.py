from dataclasses import dataclass
from typing import Literal

from app.schemas.coaching import DailyNutritionRecord, WeeklyCoachingRequest
from app.services.coaching_rag_policy import needs_nutrition_rag
from app.services.trend_service import calc_weight_trend


AgentName = Literal["nutrition", "exercise", "goal"]

_ALL_AGENTS: tuple[AgentName, ...] = ("nutrition", "exercise", "goal")


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


def _has_nutrition(record: DailyNutritionRecord) -> bool:
    return any(
        value > 0
        for value in (
            record.kcal,
            record.protein_g,
            record.carb_g,
            record.fat_g,
        )
    )


def _average_rate(values: list[float], target: float) -> float | None:
    if target <= 0:
        return None
    if not values:
        return None
    return round(sum(value / target * 100 for value in values) / len(values), 1)


def _priority_nutrient(
    protein_rate: float | None,
    carb_rate: float | None,
    fat_rate: float | None,
) -> str | None:
    comparable = {
        "단백질": protein_rate,
        "탄수화물": carb_rate,
        "지방": fat_rate,
    }
    valid_rates = {name: rate for name, rate in comparable.items() if rate is not None}
    if not valid_rates:
        return None
    return min(valid_rates, key=valid_rates.get)


def _weight_trend(req: WeeklyCoachingRequest) -> float | None:
    try:
        sorted_records = sorted(req.weight_records, key=lambda record: record.date)
        return calc_weight_trend(
            [record.weight_kg for record in sorted_records],
            [record.date for record in sorted_records],
        )
    except Exception:
        return None


def build_facts(req: WeeklyCoachingRequest) -> CoachingFacts:
    """주간 코칭 입력에서 라우팅과 응답에 필요한 결정론적 사실을 계산한다."""
    calorie_rates = [
        round((record.kcal + record.calories_burned) / req.target_kcal * 100, 1)
        if req.target_kcal > 0
        else 100.0
        for record in req.daily_nutrition
    ]
    avg_calorie_rate = (
        round(sum(calorie_rates) / len(calorie_rates), 1) if calorie_rates else 0.0
    )
    achievement_days = sum(80 <= rate <= 120 for rate in calorie_rates)

    avg_protein_rate = _average_rate(
        [record.protein_g for record in req.daily_nutrition],
        req.target_protein_g,
    )
    avg_carb_rate = _average_rate(
        [record.carb_g for record in req.daily_nutrition],
        req.target_carb_g,
    )
    avg_fat_rate = _average_rate(
        [record.fat_g for record in req.daily_nutrition],
        req.target_fat_g,
    )

    return CoachingFacts(
        nutrition_days=sum(_has_nutrition(record) for record in req.daily_nutrition),
        exercise_count=sum(session.total_sets > 0 for session in req.routine_sessions),
        weight_record_count=len(req.weight_records),
        avg_calorie_rate=avg_calorie_rate,
        avg_protein_rate=avg_protein_rate,
        avg_carb_rate=avg_carb_rate,
        avg_fat_rate=avg_fat_rate,
        achievement_days=achievement_days,
        weight_trend=_weight_trend(req),
        priority_nutrient=_priority_nutrient(
            avg_protein_rate,
            avg_carb_rate,
            avg_fat_rate,
        ),
    )


def decide_route(facts: CoachingFacts) -> RoutingDecision:
    """동일한 사실에 항상 동일한 Agent 선택을 반환한다."""
    selected: list[AgentName] = []
    reasons: list[str] = []

    if facts.nutrition_days >= 1:
        selected.append("nutrition")
        reasons.append("NUTRITION_DATA_PRESENT")

    if facts.exercise_count >= 1:
        selected.append("exercise")
        reasons.append("EXERCISE_DATA_PRESENT")

    goal_needed = facts.weight_trend is not None
    if goal_needed:
        reasons.append("WEIGHT_TREND_AVAILABLE")

    if "nutrition" in selected and "exercise" in selected:
        goal_needed = True
        reasons.append("CROSS_DOMAIN_EVALUATION")

    if goal_needed:
        selected.append("goal")

    nutrient_rates = (
        facts.avg_protein_rate,
        facts.avg_carb_rate,
        facts.avg_fat_rate,
    )
    use_nutrition_rag = needs_nutrition_rag(
        nutrition_selected="nutrition" in selected,
        nutrition_days=facts.nutrition_days,
        nutrient_rates=nutrient_rates,
        priority_nutrient=facts.priority_nutrient,
    )
    if use_nutrition_rag:
        reasons.append("NUTRIENT_DEFICIT_RAG")

    if not selected:
        reasons.append("NO_USABLE_DATA")

    selected_agents = tuple(selected)
    skipped_agents = tuple(agent for agent in _ALL_AGENTS if agent not in selected_agents)
    return RoutingDecision(
        selected_agents=selected_agents,
        skipped_agents=skipped_agents,
        use_nutrition_rag=use_nutrition_rag,
        reason_codes=tuple(reasons),
    )
