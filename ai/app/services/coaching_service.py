import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

from app.config import settings
from app.schemas.coaching import WeeklyCoachingRequest, WeeklyCoachingResponse
from app.services.claude_service import call_claude
from app.services.coaching_rag_policy import fetch_nutrition_context, format_nutrition_context
from app.services.coaching_router import (
    AgentName,
    CoachingFacts,
    RoutingDecision,
    build_facts,
    decide_route,
)


logger = logging.getLogger("ai.coaching")

SKIPPED_NUTRITION = "분석 미실행: 해당 주차 영양 기록이 없습니다."
SKIPPED_EXERCISE = "분석 미실행: 해당 주차 운동 기록이 없습니다."
SKIPPED_GOAL = "분석 미실행: 체중 추세 또는 교차 분석 데이터가 부족합니다."
NO_DATA_COMMENT = (
    "이번 주에는 분석할 식단·운동·체중 기록이 부족합니다. "
    "기록을 추가하면 맞춤 코칭을 제공할 수 있습니다."
)


@dataclass
class _RunMetrics:
    coaching_run_id: str
    llm_call_count: int = 0
    rag_called: bool = False
    rag_hit_count: int = 0


def _new_metrics() -> _RunMetrics:
    return _RunMetrics(coaching_run_id=str(uuid.uuid4()))


def _calc_stats(req: WeeklyCoachingRequest) -> tuple[float, int]:
    """기존 호출자 호환용: 칼로리 달성률 평균과 목표 달성일 수를 반환한다."""
    facts = build_facts(req)
    return facts.avg_calorie_rate, facts.achievement_days


async def _call_agent_llm(
    prompt: str,
    *,
    max_tokens: int,
    operation: str,
    metrics: _RunMetrics,
) -> str:
    metrics.llm_call_count += 1
    return await call_claude(
        prompt,
        max_tokens=max_tokens,
        log_context={
            "coaching_run_id": metrics.coaching_run_id,
            "operation": operation,
        },
    )


async def _nutrition_agent(
    req: WeeklyCoachingRequest,
    avg_calorie_rate: float,
    *,
    facts: CoachingFacts | None = None,
    decision: RoutingDecision | None = None,
    metrics: _RunMetrics | None = None,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 칼로리 달성률 85%, 단백질 섭취 다소 부족합니다."

    facts = facts or build_facts(req)
    decision = decision or decide_route(facts)
    metrics = metrics or _new_metrics()
    rag_result = await fetch_nutrition_context(
        use_rag=decision.use_nutrition_rag,
        health_goal=req.health_goal,
        priority_nutrient=facts.priority_nutrient,
    )
    metrics.rag_called = rag_result.called
    metrics.rag_hit_count = rag_result.hit_count

    nutrient_lines = [
        f"단백질 달성률 평균: {facts.avg_protein_rate:.0f}%"
        if facts.avg_protein_rate is not None
        else "단백질 달성률 평균: 목표 없음",
        f"탄수화물 달성률 평균: {facts.avg_carb_rate:.0f}%"
        if facts.avg_carb_rate is not None
        else "탄수화물 달성률 평균: 목표 없음",
        f"지방 달성률 평균: {facts.avg_fat_rate:.0f}%"
        if facts.avg_fat_rate is not None
        else "지방 달성률 평균: 목표 없음",
    ]
    prompt_parts = [
        f"[영양 분석] 건강 목표: {req.health_goal}",
        f"실제 영양 기록 일수: {facts.nutrition_days}일",
        f"칼로리 달성률 평균: {avg_calorie_rate:.0f}%",
        *nutrient_lines,
        f"7일 칼로리 기록: {[record.kcal for record in req.daily_nutrition]}",
    ]
    if rag_result.documents:
        prompt_parts.extend(
            [
                "[참고 식품 정보]",
                format_nutrition_context(rag_result.documents),
                "참고 정보 범위 안에서 부족 영양소를 보완할 한식 식품 패턴을 제안하세요.",
            ]
        )
    elif rag_result.called:
        prompt_parts.append(
            "검색 근거가 없으므로 구체적인 식품별 영양 수치를 생성하지 마세요."
        )
    prompt_parts.append("이 데이터를 바탕으로 이번 주 영양 섭취 패턴을 2문장으로 분석하세요.")

    try:
        return await _call_agent_llm(
            "\n".join(prompt_parts),
            max_tokens=200,
            operation="nutrition",
            metrics=metrics,
        )
    except Exception:
        return "영양 분석 불가"


async def _exercise_agent(
    req: WeeklyCoachingRequest,
    *,
    metrics: _RunMetrics | None = None,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 세션 성공률 양호, 단백질 보충 권장합니다."

    valid_sessions = [session for session in req.routine_sessions if session.total_sets > 0]
    if not valid_sessions:
        return "이번 주 운동 기록 없음"

    metrics = metrics or _new_metrics()
    session_summary = "\n".join(
        f"- {session.exercise_name}: "
        f"{session.successful_sets}/{session.total_sets}세트 성공 ({session.weight_kg}kg)"
        for session in valid_sessions
    )
    prompt = (
        f"[운동 분석] 건강 목표: {req.health_goal}\n"
        f"유효 운동 기록 수: {len(valid_sessions)}개\n"
        f"세션 기록:\n{session_summary}\n"
        "위 데이터를 바탕으로 이번 주 운동 성과를 2문장으로 분석하세요."
    )
    try:
        return await _call_agent_llm(
            prompt,
            max_tokens=200,
            operation="exercise",
            metrics=metrics,
        )
    except Exception:
        return "운동 분석 불가"


def _analysis_lines(analyses: Mapping[AgentName, str]) -> list[str]:
    labels = {
        "nutrition": "영양 분석",
        "exercise": "운동 분석",
        "goal": "목표 달성 분석",
    }
    return [f"{labels[name]}: {analysis}" for name, analysis in analyses.items()]


async def _goal_agent(
    req: WeeklyCoachingRequest,
    weight_trend: float | None,
    prior_analyses: Mapping[AgentName, str],
    *,
    metrics: _RunMetrics | None = None,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 체중 추세 안정적, 현재 궤도 유지하세요."

    metrics = metrics or _new_metrics()
    trend_text = f"{weight_trend:+.2f}kg/주" if weight_trend is not None else "기록 없음"
    prompt = "\n".join(
        [
            f"[목표 달성 분석] 건강 목표: {req.health_goal}",
            f"체중 추세: {trend_text}",
            *_analysis_lines(prior_analyses),
            f"위 데이터를 바탕으로 {req.health_goal} 목표 달성 궤도에 있는지 2문장으로 평가하세요.",
        ]
    )
    try:
        return await _call_agent_llm(
            prompt,
            max_tokens=200,
            operation="goal",
            metrics=metrics,
        )
    except Exception:
        return "목표 분석 불가"


async def _synthesis_agent(
    health_goal: str,
    analyses: Mapping[AgentName, str],
    *,
    metrics: _RunMetrics | None = None,
) -> str:
    if settings.env == "dev":
        return "[MOCK] 이번 주 전반적으로 잘 하셨습니다. 단백질 섭취를 조금 늘리면 더욱 효과적입니다."

    metrics = metrics or _new_metrics()
    prompt = "\n".join(
        [
            f"[통합 코칭] 건강 목표: {health_goal}",
            *_analysis_lines(analyses),
            "실행된 분석을 종합해 회원에게 격려와 다음 주 실천 방향을 4~5문장 한국어로 코칭해주세요.",
        ]
    )
    try:
        return await _call_agent_llm(
            prompt,
            max_tokens=400,
            operation="synthesis",
            metrics=metrics,
        )
    except Exception:
        return "일부 분석에 문제가 있었습니다. 꾸준히 노력하고 계신 점은 훌륭합니다."


def _log_route(
    req: WeeklyCoachingRequest,
    decision: RoutingDecision,
    metrics: _RunMetrics,
    *,
    started_at: float,
) -> None:
    executed_agent_count = len(decision.selected_agents) + bool(decision.selected_agents)
    logger.info(
        json.dumps(
            {
                "event": "coaching_route",
                "coaching_run_id": metrics.coaching_run_id,
                "week_number": req.week_number,
                "selected_agents": list(decision.selected_agents),
                "skipped_agents": list(decision.skipped_agents),
                "executed_agent_count": executed_agent_count,
                "llm_call_count": metrics.llm_call_count,
                "rag_called": metrics.rag_called,
                "rag_hit_count": metrics.rag_hit_count,
                "total_latency_ms": round((time.monotonic() - started_at) * 1000, 1),
                "reason_codes": list(decision.reason_codes),
            },
            ensure_ascii=False,
        )
    )


async def run_coaching_chain(req: WeeklyCoachingRequest) -> WeeklyCoachingResponse:
    started_at = time.monotonic()
    metrics = _new_metrics()
    facts = build_facts(req)
    decision = decide_route(facts)

    nutrition_analysis = SKIPPED_NUTRITION
    exercise_analysis = SKIPPED_EXERCISE
    goal_analysis = SKIPPED_GOAL

    try:
        selected = set(decision.selected_agents)
        if {"nutrition", "exercise"}.issubset(selected):
            nutrition_analysis, exercise_analysis = await asyncio.gather(
                _nutrition_agent(
                    req,
                    facts.avg_calorie_rate,
                    facts=facts,
                    decision=decision,
                    metrics=metrics,
                ),
                _exercise_agent(req, metrics=metrics),
            )
        elif "nutrition" in selected:
            nutrition_analysis = await _nutrition_agent(
                req,
                facts.avg_calorie_rate,
                facts=facts,
                decision=decision,
                metrics=metrics,
            )
        elif "exercise" in selected:
            exercise_analysis = await _exercise_agent(req, metrics=metrics)

        executed_analyses: dict[AgentName, str] = {}
        if "nutrition" in selected:
            executed_analyses["nutrition"] = nutrition_analysis
        if "exercise" in selected:
            executed_analyses["exercise"] = exercise_analysis

        if "goal" in selected:
            goal_analysis = await _goal_agent(
                req,
                facts.weight_trend,
                executed_analyses,
                metrics=metrics,
            )
            executed_analyses["goal"] = goal_analysis

        ai_comment = (
            await _synthesis_agent(
                req.health_goal,
                executed_analyses,
                metrics=metrics,
            )
            if decision.selected_agents
            else NO_DATA_COMMENT
        )

        return WeeklyCoachingResponse(
            ai_comment=ai_comment,
            nutrition_summary=nutrition_analysis,
            exercise_summary=exercise_analysis,
            goal_summary=goal_analysis,
            avg_calorie_rate=facts.avg_calorie_rate,
            achievement_days=facts.achievement_days,
            weight_trend=facts.weight_trend,
        )
    finally:
        _log_route(req, decision, metrics, started_at=started_at)
