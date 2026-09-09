from unittest.mock import AsyncMock

import pytest

from app.services import coaching_rag_policy
from app.services.coaching_rag_policy import (
    build_nutrition_query,
    fetch_nutrition_context,
    needs_nutrition_rag,
)


def test_영양소_달성률이_모두_80이상이면_rag가_필요하지_않다():
    assert needs_nutrition_rag(
        nutrition_selected=True,
        nutrition_days=7,
        nutrient_rates=(80.0, 95.0, 100.0),
        priority_nutrient="단백질",
    ) is False


def test_목표값_0으로_제외된_영양소는_부족_판정에서_제외한다():
    assert needs_nutrition_rag(
        nutrition_selected=True,
        nutrition_days=2,
        nutrient_rates=(None, 90.0, 100.0),
        priority_nutrient="탄수화물",
    ) is False


def test_가장_낮은_영양소가_80미만이면_rag가_필요하다():
    assert needs_nutrition_rag(
        nutrition_selected=True,
        nutrition_days=3,
        nutrient_rates=(65.0, 90.0, 85.0),
        priority_nutrient="단백질",
    ) is True


@pytest.mark.asyncio
async def test_rag_불필요시_검색을_호출하지_않는다(monkeypatch):
    to_thread = AsyncMock()
    monkeypatch.setattr(coaching_rag_policy.asyncio, "to_thread", to_thread)

    result = await fetch_nutrition_context(
        use_rag=False,
        health_goal="MUSCLE",
        priority_nutrient="단백질",
    )

    assert result.called is False
    assert result.documents == ()
    to_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_rag_필요시_최대_3개를_비동기로_검색한다(monkeypatch):
    documents = [
        {"name": "닭가슴살", "info": "단백질 공급원", "document": "닭가슴살 정보"},
    ]
    to_thread = AsyncMock(return_value=documents)
    monkeypatch.setattr(coaching_rag_policy.asyncio, "to_thread", to_thread)

    result = await fetch_nutrition_context(
        use_rag=True,
        health_goal="MUSCLE",
        priority_nutrient="단백질",
    )

    query = "근육 증가 목표, 단백질 보충에 적합한 한식 식품"
    assert build_nutrition_query("MUSCLE", "단백질") == query
    assert result.called is True
    assert len(result.documents) == 1
    to_thread.assert_awaited_once_with(
        coaching_rag_policy.rag_service.search,
        query,
        n_results=3,
    )


@pytest.mark.asyncio
async def test_rag_검색_실패는_빈_근거로_격리한다(monkeypatch):
    to_thread = AsyncMock(side_effect=RuntimeError("chroma unavailable"))
    monkeypatch.setattr(coaching_rag_policy.asyncio, "to_thread", to_thread)

    result = await fetch_nutrition_context(
        use_rag=True,
        health_goal="DIET",
        priority_nutrient="지방",
    )

    assert result.called is True
    assert result.documents == ()
