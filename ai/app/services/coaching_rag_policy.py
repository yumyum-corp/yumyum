import asyncio
import logging
from dataclasses import dataclass

from app.services import rag_service


logger = logging.getLogger("ai.coaching")

_NUTRIENT_RAG_THRESHOLD = 80.0
_HEALTH_GOAL_LABELS = {
    "DIET": "다이어트",
    "MUSCLE": "근육 증가",
    "HEALTH": "건강 유지",
    "DISEASE": "질환 관리",
}


@dataclass(frozen=True)
class NutritionRagResult:
    called: bool
    documents: tuple[dict, ...]

    @property
    def hit_count(self) -> int:
        return len(self.documents)


def needs_nutrition_rag(
    *,
    nutrition_selected: bool,
    nutrition_days: int,
    nutrient_rates: tuple[float | None, ...],
    priority_nutrient: str | None,
) -> bool:
    """구체적인 식품 근거가 필요한 영양소 부족 상태인지 판정한다."""
    return (
        nutrition_selected
        and nutrition_days >= 1
        and priority_nutrient is not None
        and any(
            rate is not None and rate < _NUTRIENT_RAG_THRESHOLD
            for rate in nutrient_rates
        )
    )


def build_nutrition_query(health_goal: str, priority_nutrient: str) -> str:
    goal_label = _HEALTH_GOAL_LABELS.get(health_goal, health_goal)
    return f"{goal_label} 목표, {priority_nutrient} 보충에 적합한 한식 식품"


async def fetch_nutrition_context(
    *,
    use_rag: bool,
    health_goal: str,
    priority_nutrient: str | None,
) -> NutritionRagResult:
    """동기 ChromaDB 검색을 이벤트 루프 밖에서 실행하고 실패를 격리한다."""
    if not use_rag or priority_nutrient is None:
        return NutritionRagResult(called=False, documents=())

    query = build_nutrition_query(health_goal, priority_nutrient)
    try:
        documents = await asyncio.to_thread(
            rag_service.search,
            query,
            n_results=3,
        )
    except Exception as exc:
        logger.warning(
            "Nutrition coaching RAG search failed: %s",
            type(exc).__name__,
        )
        documents = []
    return NutritionRagResult(called=True, documents=tuple(documents))


def format_nutrition_context(documents: tuple[dict, ...]) -> str:
    return "\n".join(
        f"- {document.get('name', '식품')}: "
        f"{document.get('document') or document.get('info', '')}"
        for document in documents
    )
