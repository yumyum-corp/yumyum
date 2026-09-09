#!/usr/bin/env python3
"""F704 고정 DAG와 규칙 기반 동적 라우팅의 구조 비용을 비교한다.

Mock 모드는 호출 수와 오케스트레이션 지연만 검증한다. 실제 모델 토큰과 비용은
real 모드를 명시적으로 승인해 실행했을 때 Claude 구조화 로그에서 집계한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, patch


os.environ["ENV"] = "prod"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

from app.config import settings  # noqa: E402
from app.schemas.coaching import (  # noqa: E402
    DailyNutritionRecord,
    RoutineSessionRecord,
    WeeklyCoachingRequest,
    WeightRecord,
)
from app.services import coaching_service  # noqa: E402
from app.services.claude_service import call_claude  # noqa: E402
from app.services.coaching_rag_policy import NutritionRagResult  # noqa: E402
from app.services.coaching_router import build_facts, decide_route  # noqa: E402


SCENARIOS = (
    "no_records",
    "nutrition_only",
    "exercise_only",
    "weight_only",
    "nutrition_weight",
    "exercise_weight",
    "nutrition_exercise",
    "all_data",
)


@dataclass
class Sample:
    latency_ms: float
    agent_calls: int
    llm_calls: int
    rag_calls: int
    fallback: bool
    status_ok: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ClaudeLogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except (TypeError, json.JSONDecodeError):
            return
        if payload.get("event") == "claude_call":
            self.events.append(payload)

    def reset_events(self) -> None:
        self.events.clear()


def make_request(name: str) -> WeeklyCoachingRequest:
    nutrition = [
        DailyNutritionRecord(
            date="2026-09-01",
            kcal=1200,
            protein_g=60,
            carb_g=180,
            fat_g=40,
            calories_burned=0,
        )
    ]
    exercise = [
        RoutineSessionRecord(
            exercise_name="스쿼트",
            successful_sets=3,
            total_sets=4,
            weight_kg=60,
            session_date="2026-09-01",
        )
    ]
    weights = [
        WeightRecord(date="2026-08-25", weight_kg=70.0),
        WeightRecord(date="2026-09-01", weight_kg=70.2),
    ]
    has_nutrition = name in {"nutrition_only", "nutrition_weight", "nutrition_exercise", "all_data"}
    has_exercise = name in {"exercise_only", "exercise_weight", "nutrition_exercise", "all_data"}
    has_weight = name in {"weight_only", "nutrition_weight", "exercise_weight", "all_data"}
    return WeeklyCoachingRequest(
        week_number=3,
        health_goal="MUSCLE",
        daily_nutrition=nutrition if has_nutrition else [],
        target_kcal=2000,
        target_protein_g=120,
        target_carb_g=250,
        target_fat_g=65,
        routine_sessions=exercise if has_exercise else [],
        weight_records=weights if has_weight else [],
    )


def _token_metrics(events: list[dict]) -> tuple[int | None, int | None, float | None]:
    if not events:
        return None, None, None
    input_tokens = sum(int(event.get("input_tokens") or 0) for event in events)
    output_tokens = sum(int(event.get("output_tokens") or 0) for event in events)
    costs = [event["cost_usd"] for event in events if event.get("cost_usd") is not None]
    return input_tokens, output_tokens, sum(costs) if costs else None


async def run_fixed(
    req: WeeklyCoachingRequest,
    invoke: Callable[[str, int], Awaitable[str]],
    collector: ClaudeLogCollector,
) -> Sample:
    """변경 전: Nutrition/Exercise 함수, Goal, Synthesis를 항상 실행한다."""
    collector.reset_events()
    llm_calls = 0
    fallback = False
    facts = build_facts(req)

    async def counted(prompt: str, max_tokens: int) -> str:
        nonlocal llm_calls
        llm_calls += 1
        return await invoke(prompt, max_tokens)

    async def safe_counted(prompt: str, max_tokens: int, fallback_text: str) -> str:
        nonlocal fallback
        try:
            return await counted(prompt, max_tokens)
        except Exception:
            fallback = True
            return fallback_text

    async def nutrition() -> str:
        if not req.daily_nutrition:
            return "영양 기록 없음"
        protein_rates = [
            round(record.protein_g / req.target_protein_g * 100, 1)
            if req.target_protein_g > 0
            else 100.0
            for record in req.daily_nutrition
        ]
        avg_protein = round(sum(protein_rates) / len(protein_rates), 1)
        prompt = (
            f"[영양 분석] 건강 목표: {req.health_goal}\n"
            f"칼로리 달성률 평균: {facts.avg_calorie_rate:.0f}%\n"
            f"단백질 달성률 평균: {avg_protein:.0f}%\n"
            f"7일 칼로리 기록: {[record.kcal for record in req.daily_nutrition]}\n"
            "이 데이터를 바탕으로 이번 주 영양 섭취 패턴을 2문장으로 분석하세요."
        )
        return await safe_counted(prompt, 200, "영양 분석 불가")

    async def exercise() -> str:
        if not req.routine_sessions:
            return "이번 주 운동 기록 없음"
        session_summary = "\n".join(
            f"- {session.exercise_name}: "
            f"{session.successful_sets}/{session.total_sets}세트 성공 "
            f"({session.weight_kg}kg)"
            for session in req.routine_sessions
        )
        prompt = (
            f"[운동 분석] 건강 목표: {req.health_goal}\n"
            f"세션 기록:\n{session_summary}\n"
            "위 데이터를 바탕으로 이번 주 운동 성과를 2문장으로 분석하세요."
        )
        return await safe_counted(prompt, 200, "운동 분석 불가")

    started_at = time.monotonic()
    status_ok = True
    try:
        nutrition_result, exercise_result = await asyncio.gather(nutrition(), exercise())
        trend_text = (
            f"{facts.weight_trend:+.2f}kg/주"
            if facts.weight_trend is not None
            else "기록 없음"
        )
        goal_result = await safe_counted(
            (
                f"[목표 달성 분석] 건강 목표: {req.health_goal}\n"
                f"체중 추세: {trend_text}\n"
                f"영양 분석: {nutrition_result}\n"
                f"운동 분석: {exercise_result}\n"
                f"위 데이터를 바탕으로 {req.health_goal} 목표 달성 궤도에 "
                "있는지 2문장으로 평가하세요."
            ),
            200,
            "목표 분석 불가",
        )
        await safe_counted(
            (
                f"[통합 코칭] 건강 목표: {req.health_goal}\n"
                f"영양 분석: {nutrition_result}\n"
                f"운동 분석: {exercise_result}\n"
                f"목표 달성 분석: {goal_result}\n"
                "세 분석을 종합해 회원에게 격려와 다음 주 실천 방향을 "
                "4~5문장 한국어로 코칭해주세요."
            ),
            400,
            "일부 분석에 문제가 있었습니다.",
        )
    except Exception:
        fallback = True
        status_ok = False
    input_tokens, output_tokens, cost_usd = _token_metrics(collector.events)
    return Sample(
        latency_ms=(time.monotonic() - started_at) * 1000,
        agent_calls=4,
        llm_calls=llm_calls,
        rag_calls=0,
        fallback=fallback,
        status_ok=status_ok,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


async def run_dynamic_mock(
    req: WeeklyCoachingRequest,
    delay_seconds: float,
) -> Sample:
    async def delayed_response(*args, **kwargs) -> str:
        await asyncio.sleep(delay_seconds)
        return "[EVAL MOCK] 분석"

    claude_mock = AsyncMock(side_effect=delayed_response)
    rag_mock = AsyncMock(
        return_value=NutritionRagResult(
            called=True,
            documents=({"name": "두부", "document": "단백질 식품", "info": ""},),
        )
    )
    decision = decide_route(build_facts(req))
    started_at = time.monotonic()
    fallback = False
    status_ok = True
    try:
        with (
            patch.object(settings, "env", "prod"),
            patch.object(coaching_service, "call_claude", claude_mock),
            patch.object(coaching_service, "fetch_nutrition_context", rag_mock),
        ):
            result = await coaching_service.run_coaching_chain(req)
        fallback = any(
            "분석 불가" in summary
            for summary in (
                result.nutrition_summary,
                result.exercise_summary,
                result.goal_summary,
            )
        )
    except Exception:
        fallback = True
        status_ok = False
    return Sample(
        latency_ms=(time.monotonic() - started_at) * 1000,
        agent_calls=len(decision.selected_agents) + bool(decision.selected_agents),
        llm_calls=claude_mock.await_count,
        rag_calls=rag_mock.await_count,
        fallback=fallback,
        status_ok=status_ok,
    )


async def run_dynamic_real(
    req: WeeklyCoachingRequest,
    collector: ClaudeLogCollector,
) -> Sample:
    collector.reset_events()
    decision = decide_route(build_facts(req))
    started_at = time.monotonic()
    status_ok = True
    fallback = False
    try:
        result = await coaching_service.run_coaching_chain(req)
        fallback = any(
            "분석 불가" in summary
            for summary in (
                result.nutrition_summary,
                result.exercise_summary,
                result.goal_summary,
            )
        )
    except Exception:
        status_ok = False
        fallback = True
    events = list(collector.events)
    input_tokens, output_tokens, cost_usd = _token_metrics(events)
    return Sample(
        latency_ms=(time.monotonic() - started_at) * 1000,
        agent_calls=len(decision.selected_agents) + bool(decision.selected_agents),
        llm_calls=len(events),
        rag_calls=int(decision.use_nutrition_rag),
        fallback=fallback,
        status_ok=status_ok,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percent)))
    return ordered[index]


def average_optional(samples: list[Sample], field: str) -> float | None:
    values = [getattr(sample, field) for sample in samples]
    available = [float(value) for value in values if value is not None]
    return round(statistics.mean(available), 6) if available else None


def summarize(samples: list[Sample]) -> dict:
    latencies = [sample.latency_ms for sample in samples]
    return {
        "avg_agent_calls": round(statistics.mean(sample.agent_calls for sample in samples), 3),
        "avg_llm_calls": round(statistics.mean(sample.llm_calls for sample in samples), 3),
        "avg_rag_calls": round(statistics.mean(sample.rag_calls for sample in samples), 3),
        "latency_ms_mean": round(statistics.mean(latencies), 3),
        "latency_ms_p50": round(percentile(latencies, 0.50), 3),
        "latency_ms_p90": round(percentile(latencies, 0.90), 3),
        "avg_input_tokens": average_optional(samples, "input_tokens"),
        "avg_output_tokens": average_optional(samples, "output_tokens"),
        "cost_usd_mean": average_optional(samples, "cost_usd"),
        "agent_fallback_rate": round(
            sum(sample.fallback for sample in samples) / len(samples), 4
        ),
        "response_200_rate": round(
            sum(sample.status_ok for sample in samples) / len(samples), 4
        ),
    }


def write_result(path_value: str, payload: dict) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def evaluate(args: argparse.Namespace) -> dict:
    collector = ClaudeLogCollector()
    claude_logger = logging.getLogger("ai.claude")
    claude_logger.addHandler(collector)
    fixed_samples: list[Sample] = []
    dynamic_samples: list[Sample] = []
    delay_seconds = args.mock_delay_ms / 1000

    async def mock_invoke(prompt: str, max_tokens: int) -> str:
        await asyncio.sleep(delay_seconds)
        return "[EVAL MOCK] 분석"

    async def real_invoke(prompt: str, max_tokens: int) -> str:
        return await call_claude(prompt, max_tokens=max_tokens)

    invoke = mock_invoke if args.mode == "mock" else real_invoke
    try:
        for scenario in SCENARIOS:
            req = make_request(scenario)
            if not args.skip_warmup:
                fixed_warmup = await run_fixed(req, invoke, collector)
                if args.mode == "mock":
                    await run_dynamic_mock(req, delay_seconds)
                else:
                    dynamic_warmup = await run_dynamic_real(req, collector)
                    if fixed_warmup.fallback or dynamic_warmup.fallback:
                        raise RuntimeError(
                            f"Real API warm-up failed for {scenario}; "
                            "stopping before repeated calls"
                        )

            for run_number in range(1, args.runs + 1):
                fixed_samples.append(await run_fixed(req, invoke, collector))
                if args.mode == "mock":
                    dynamic_samples.append(await run_dynamic_mock(req, delay_seconds))
                else:
                    dynamic_samples.append(await run_dynamic_real(req, collector))
                print(
                    f"[progress] scenario={scenario} run={run_number}/{args.runs}",
                    file=sys.stderr,
                    flush=True,
                )

            if args.out:
                write_result(
                    args.out,
                    {
                        "status": "running",
                        "completed_scenarios": list(
                            SCENARIOS[: SCENARIOS.index(scenario) + 1]
                        ),
                        "runs_per_scenario": args.runs,
                        "fixed_partial": summarize(fixed_samples),
                        "dynamic_partial": summarize(dynamic_samples),
                    },
                )
    finally:
        claude_logger.removeHandler(collector)

    return {
        "dataset": "8-scenarios-equal-weight",
        "scenarios": list(SCENARIOS),
        "mode": args.mode,
        "runs_per_scenario": args.runs,
        "warmup_runs_per_scenario": 0 if args.skip_warmup else 1,
        "mock_delay_ms_per_llm_call": args.mock_delay_ms if args.mode == "mock" else None,
        "measurement_note": (
            "Mock latency compares orchestration structure only; token and cost metrics are unmeasured."
            if args.mode == "mock"
            else "Real API result from Claude structured call logs."
        ),
        "fixed": summarize(fixed_samples),
        "dynamic": summarize(dynamic_samples),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("mock", "real"), default="mock")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--mock-delay-ms", type=float, default=10.0)
    parser.add_argument("--yes-real-api", action="store_true")
    parser.add_argument("--skip-warmup", action="store_true")
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.mode == "real" and not args.yes_real_api:
        parser.error("real mode incurs API cost; pass --yes-real-api to continue")
    return args


def main() -> None:
    args = parse_args()
    claude_logger = logging.getLogger("ai.claude")
    previous_level = claude_logger.level
    claude_logger.setLevel(logging.INFO)
    try:
        result = asyncio.run(evaluate(args))
    finally:
        claude_logger.setLevel(previous_level)
    result["status"] = "complete"
    if args.out:
        write_result(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
