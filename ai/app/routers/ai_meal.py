import json

from fastapi import APIRouter, HTTPException
from app.schemas.meal import (
    LastRecommendRequest, LastRecommendResponse, MealRecommendation,
    DietAnalyzeRequest, DietAnalyzeResponse,
    PhotoAnalyzeRequest, DetectedItem, PhotoAnalyzeResponse,
)
from app.services.claude_service import call_claude, call_claude_vision
from app.services.diet_service import calculate_diet_analysis

router = APIRouter(prefix="/ai/meal", tags=["AI Meal"])

_ALLOWED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_BASE64_CHARS = 8_000_000  # 약 6MB 원본 이미지 상당 (비용·지연 방지)


@router.post("/last-recommend", response_model=LastRecommendResponse)
async def last_recommend(req: LastRecommendRequest):
    """
    F802 - 마지막 끼니 추천
    트리거 조건:
      - 잔여 칼로리가 1끼 분량(목표 ÷ 끼니 수) ± 20% 범위일 때
      - 오후 5시 이후 (Spring에서 시간 체크 후 호출)
      - 오늘 최소 1끼 이상 기록됨
    """
    if req.meal_count < 1:
        raise HTTPException(status_code=400, detail="오늘 기록된 끼니가 없어 추천을 생성할 수 없습니다.")

    # ① 잔여 영양소 계산
    remain_kcal    = req.target_kcal      - req.total_kcal
    remain_protein = req.target_protein_g - req.total_protein_g
    remain_carb    = req.target_carb_g    - req.total_carb_g
    remain_fat     = req.target_fat_g     - req.total_fat_g

    # ② 부족 영양소 우선순위 (달성률이 가장 낮은 영양소)
    rates = {
        "protein": req.total_protein_g / req.target_protein_g if req.target_protein_g > 0 else 1,
        "carb":    req.total_carb_g    / req.target_carb_g    if req.target_carb_g    > 0 else 1,
        "fat":     req.total_fat_g     / req.target_fat_g     if req.target_fat_g     > 0 else 1,
    }
    priority = min(rates, key=rates.get)

    # ③ Claude 프롬프트 생성
    prompt = f"""
당신은 영양사 AI입니다. 아래 잔여 영양소를 채울 수 있는 저녁 식단 3가지를 추천해주세요.

[잔여 영양소]
- 칼로리: {remain_kcal:.0f} kcal
- 단백질: {remain_protein:.1f} g
- 탄수화물: {remain_carb:.1f} g
- 지방: {remain_fat:.1f} g
- 가장 부족한 영양소: {priority}

[조건]
- 과도하게 많은 양이 아닌 현실적인 한 끼 분량으로 제안
- 잔여 영양소를 대부분 채울 수 있는 식단 위주로 추천
- 각 식단은 음식명, 칼로리(kcal), 단백질(g), 탄수화물(g), 지방(g), 추천 이유를 포함

JSON 배열 형식으로만 응답하세요:
[
  {{"name": "음식명", "kcal": 숫자, "protein_g": 숫자, "carb_g": 숫자, "fat_g": 숫자, "reason": "이유"}},
  ...
]
"""

    _FALLBACK = [
        MealRecommendation(name="닭가슴살 샐러드",    kcal=380, protein_g=42, carb_g=18, fat_g=12, reason="단백질 보충에 최적"),
        MealRecommendation(name="두부된장찌개+현미밥", kcal=420, protein_g=22, carb_g=58, fat_g=9,  reason="균형잡힌 한식"),
        MealRecommendation(name="연어구이+고구마",     kcal=390, protein_g=35, carb_g=32, fat_g=14, reason="오메가3 + 복합 탄수화물"),
    ]

    try:
        raw = await call_claude(prompt)

        # ```json 블록 또는 순수 배열 모두 처리
        cleaned = raw.strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            cleaned = parts[1] if len(parts) > 1 else cleaned
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]

        meals = json.loads(cleaned)
        recommendations = [MealRecommendation(**m) for m in meals]
    except Exception as e:
        # GMS API 장애·파싱 실패 시 fallback (서버 500 방지)
        print(f"[ai_meal] fallback 사용 — 원인: {type(e).__name__}: {e}")
        recommendations = _FALLBACK

    return LastRecommendResponse(
        recommendations=recommendations,
        priority_nutrient=priority,
        ai_comment=f"오늘 {priority} 섭취가 가장 부족합니다. 아래 식단으로 하루를 마무리해보세요!",
    )


@router.post("/diet-analyze", response_model=DietAnalyzeResponse)
async def diet_analyze(req: DietAnalyzeRequest):
    """
    F701 - 식단 영양 균형 분석
    Spring이 오늘의 식단 합산값과 목표값을 전송하면
    달성률·균형 점수·부족/과다 영양소·AI 코멘트를 반환한다.
    """
    rates, balance_score, weak, excess = calculate_diet_analysis(
        total_kcal=req.total_kcal,
        total_protein_g=req.total_protein_g,
        total_carb_g=req.total_carb_g,
        total_fat_g=req.total_fat_g,
        target_kcal=req.target_kcal,
        target_protein_g=req.target_protein_g,
        target_carb_g=req.target_carb_g,
        target_fat_g=req.target_fat_g,
    )

    weak_label   = "·".join(weak)   if weak   else "없음"
    excess_label = "·".join(excess) if excess else "없음"
    prompt = (
        f"사용자 목표: {req.health_goal}, 날짜: {req.meal_date}\n"
        f"영양 균형 분석 — 균형 점수: {balance_score:.0f}/100\n"
        f"달성률: 칼로리 {rates['calorie_rate']:.0f}%, 단백질 {rates['protein_rate']:.0f}%, "
        f"탄수화물 {rates['carb_rate']:.0f}%, 지방 {rates['fat_rate']:.0f}%\n"
        f"부족: {weak_label} / 과다: {excess_label}\n"
        "위 결과를 바탕으로 사용자에게 2~3문장으로 격려와 개선 방향을 한국어로 제안하세요."
    )

    ai_comment = await call_claude(prompt, max_tokens=300)

    return DietAnalyzeResponse(
        calorie_rate=rates["calorie_rate"],
        protein_rate=rates["protein_rate"],
        carb_rate=rates["carb_rate"],
        fat_rate=rates["fat_rate"],
        balance_score=balance_score,
        weak_nutrients=weak,
        excess_nutrients=excess,
        ai_comment=ai_comment,
    )


@router.post("/analyze-photo", response_model=PhotoAnalyzeResponse)
async def analyze_photo(req: PhotoAnalyzeRequest):
    """
    Vision AI 사진 식단 분석.
    이미지 base64를 받아 Claude Vision으로 음식 감지 + 영양소 추정.
    Spring이 multipart → base64 변환 후 호출.
    """
    if not req.image_base64:
        raise HTTPException(status_code=400, detail="이미지 데이터가 없습니다.")
    if req.media_type not in _ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 이미지 형식입니다: {req.media_type}")
    if len(req.image_base64) > _MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(status_code=400, detail="이미지 크기가 너무 큽니다.")

    prompt = (
        f"이 사진에 있는 음식을 모두 감지하고 영양소를 추정해주세요. 식사 유형: {req.meal_type}\n\n"
        "아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):\n"
        '{"detected_items": [{"name": "음식명(한국어)", "estimated_grams": 숫자, '
        '"kcal": 숫자, "protein_g": 숫자, "carb_g": 숫자, "fat_g": 숫자}], '
        '"ai_comment": "한 문장 한국어 코멘트"}\n\n'
        "음식이 감지되지 않으면 detected_items를 빈 배열로 반환하세요."
    )

    try:
        raw = await call_claude_vision(
            image_base64=req.image_base64,
            media_type=req.media_type,
            prompt=prompt,
            max_tokens=800,
        )
        cleaned = raw.strip()
        if "```" in cleaned:
            parts = cleaned.split("```")
            cleaned = parts[1] if len(parts) > 1 else cleaned
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
        data = json.loads(cleaned)
        items = [DetectedItem(**item) for item in data.get("detected_items", [])]
        total_kcal = sum(i.kcal for i in items)
        ai_comment = data.get("ai_comment") or (
            "사진에서 음식을 감지하지 못했어요. 다른 각도로 다시 촬영해보세요." if not items else ""
        )
    except Exception as e:
        # 파싱/호출 실패(기술적 오류) — "음식 미감지"와는 다른 메시지로 구분
        print(f"[analyze_photo] vision 분석 실패 — {type(e).__name__}: {e}")
        items = []
        total_kcal = 0.0
        ai_comment = "사진 분석 중 오류가 발생했어요. 잠시 후 다시 시도해주세요."

    return PhotoAnalyzeResponse(
        detected_items=items,
        total_kcal=total_kcal,
        ai_comment=ai_comment,
    )
