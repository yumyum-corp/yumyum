import json

from fastapi import APIRouter, Query
from app.schemas.food import FoodItem, FoodSearchResponse
from app.services.mfds_service import search_food_mfds
from app.services.claude_service import call_claude, strip_json_code_block

router = APIRouter(prefix="/food", tags=["Food"])


@router.get("/search", response_model=FoodSearchResponse)
async def search_food(
    query: str = Query(..., description="검색할 식품명 (한글)", min_length=1),
    page:  int = Query(1,  ge=1,          description="페이지 번호"),
    size:  int = Query(10, ge=1, le=50,   description="페이지당 결과 수"),
):
    """
    식품 검색 — 식품안전처(MFDS) DB 우선 조회, 결과 없으면 Claude AI 추정값 반환.

    source 필드:
      - MFDS   : 식품안전처 공공데이터 (신뢰도 높음)
      - AI     : Claude 추정값 (DB에 없는 식품 대응)
      - MANUAL : 사용자 직접 입력 (Spring Boot 담당)
    """
    # ① 식품안전처 DB 검색
    items, total = await search_food_mfds(query, page, size)

    # ② MFDS 결과 없을 때 Claude AI 추정값으로 fallback
    if not items:
        items = await _ai_search(query)
        total = len(items)

    return FoodSearchResponse(items=items, total=total, page=page, size=size, query=query)


async def _ai_search(query: str) -> list[FoodItem]:
    """MFDS에 없는 식품을 Claude가 영양 정보 추정. 실패 시 빈 리스트 반환."""
    prompt = f"""
식품 "{query}"의 영양 정보를 추정해서 JSON 배열로 알려주세요.
조리법이 다른 2가지 버전(예: 구이/삶은, 일반/저지방)으로 제공해주세요.

JSON 배열 형식으로만 응답:
[{{"name": "식품명(조리법)", "serving_size_g": 숫자, "kcal": 숫자, "protein_g": 숫자, "carb_g": 숫자, "fat_g": 숫자, "sugar_g": 숫자, "fiber_g": 숫자}}]
"""
    try:
        raw = await call_claude(prompt)
        cleaned = strip_json_code_block(raw)
        meals = json.loads(cleaned)
        return [
            FoodItem(
                food_cd       = f"AI_{i+1:03d}",
                source        = "AI",
                **m,
            )
            for i, m in enumerate(meals)
        ]
    except Exception as e:
        print(f"[food] AI fallback 실패 — {type(e).__name__}: {e}")
        return []
