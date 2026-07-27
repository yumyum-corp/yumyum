import asyncio
from typing import Optional
from app.config import settings
from app.services.claude_service import call_claude
from app.services.trend_service import calc_weight_trend
from app.schemas.coaching import WeeklyCoachingRequest, WeeklyCoachingResponse


def _calc_stats(req: WeeklyCoachingRequest) -> tuple[float, int]:
    """칼로리 달성률 평균(%), 목표 달성일 수 반환."""
    if not req.daily_nutrition:
        return 0.0, 0

    def rate(actual: float, target: float) -> float:
        return round(actual / target * 100, 1) if target > 0 else 100.0

    rates = [rate(d.kcal + d.calories_burned, req.target_kcal) for d in req.daily_nutrition]
    avg = round(sum(rates) / len(rates), 1)
    achievement_days = sum(1 for r in rates if 80 <= r <= 120)
    return avg, achievement_days


async def _nutrition_agent(req: WeeklyCoachingRequest, avg_calorie_rate: float) -> str:
    if settings.env == "dev":
        return "[MOCK] 칼로리 달성률 85%, 단백질 섭취 다소 부족합니다."

    if not req.daily_nutrition:
        return "영양 기록 없음"

    protein_rates = [
        round(d.protein_g / req.target_protein_g * 100, 1) if req.target_protein_g > 0 else 100.0
        for d in req.daily_nutrition
    ]
    avg_protein = round(sum(protein_rates) / len(protein_rates), 1)

    prompt = (
        f"[영양 분석] 건강 목표: {req.health_goal}\n"
        f"칼로리 달성률 평균: {avg_calorie_rate:.0f}%\n"
        f"단백질 달성률 평균: {avg_protein:.0f}%\n"
        f"7일 칼로리 기록: {[d.kcal for d in req.daily_nutrition]}\n"
        "이 데이터를 바탕으로 이번 주 영양 섭취 패턴을 2문장으로 분석하세요."
    )
    try:
        return await call_claude(prompt, max_tokens=200)
    except Exception:
        return "영양 분석 불가"


async def _exercise_agent(req: WeeklyCoachingRequest) -> str:
    if settings.env == "dev":
        return "[MOCK] 세션 성공률 양호, 단백질 보충 권장합니다."

    if not req.routine_sessions:
        return "이번 주 운동 기록 없음"

    session_summary = "\n".join(
        f"- {s.exercise_name}: {s.successful_sets}/{s.total_sets}세트 성공 ({s.weight_kg}kg)"
        for s in req.routine_sessions
    )
    prompt = (
        f"[운동 분석] 건강 목표: {req.health_goal}\n"
        f"세션 기록:\n{session_summary}\n"
        "위 데이터를 바탕으로 이번 주 운동 성과를 2문장으로 분석하세요."
    )
    try:
        return await call_claude(prompt, max_tokens=200)
    except Exception:
        return "운동 분석 불가"


async def _goal_agent(
    req: WeeklyCoachingRequest,
    weight_trend: Optional[float],
    nutrition_analysis: str,
    exercise_analysis: str,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 체중 추세 안정적, 현재 궤도 유지하세요."

    trend_text = f"{weight_trend:+.2f}kg/주" if weight_trend is not None else "기록 없음"
    prompt = (
        f"[목표 달성 분석] 건강 목표: {req.health_goal}\n"
        f"체중 추세: {trend_text}\n"
        f"영양 분석: {nutrition_analysis}\n"
        f"운동 분석: {exercise_analysis}\n"
        f"위 데이터를 바탕으로 {req.health_goal} 목표 달성 궤도에 있는지 2문장으로 평가하세요."
    )
    try:
        return await call_claude(prompt, max_tokens=200)
    except Exception:
        return "목표 분석 불가"


async def _synthesis_agent(
    health_goal: str,
    nutrition_analysis: str,
    exercise_analysis: str,
    goal_analysis: str,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 이번 주 전반적으로 잘 하셨습니다. 단백질 섭취를 조금 늘리면 더욱 효과적입니다."

    prompt = (
        f"[통합 코칭] 건강 목표: {health_goal}\n"
        f"영양 분석: {nutrition_analysis}\n"
        f"운동 분석: {exercise_analysis}\n"
        f"목표 달성 분석: {goal_analysis}\n"
        "세 분석을 종합해 회원에게 격려와 다음 주 실천 방향을 4~5문장 한국어로 코칭해주세요."
    )
    try:
        return await call_claude(prompt, max_tokens=400)
    except Exception:
        return "일부 분석에 문제가 있었습니다. 꾸준히 노력하고 계신 점은 훌륭합니다."


async def run_coaching_chain(req: WeeklyCoachingRequest) -> WeeklyCoachingResponse:
    avg_calorie_rate, achievement_days = _calc_stats(req)

    try:
        sorted_records = sorted(req.weight_records, key=lambda w: w.date)
        weight_trend = calc_weight_trend(
            [w.weight_kg for w in sorted_records],
            [w.date for w in sorted_records],
        )
    except Exception:
        weight_trend = None

    nutrition_analysis, exercise_analysis = await asyncio.gather(
        _nutrition_agent(req, avg_calorie_rate),
        _exercise_agent(req),
    )
    goal_analysis = await _goal_agent(req, weight_trend, nutrition_analysis, exercise_analysis)
    ai_comment = await _synthesis_agent(
        req.health_goal, nutrition_analysis, exercise_analysis, goal_analysis
    )

    return WeeklyCoachingResponse(
        ai_comment=ai_comment,
        nutrition_summary=nutrition_analysis,
        exercise_summary=exercise_analysis,
        goal_summary=goal_analysis,
        avg_calorie_rate=avg_calorie_rate,
        achievement_days=achievement_days,
        weight_trend=weight_trend,
    )
