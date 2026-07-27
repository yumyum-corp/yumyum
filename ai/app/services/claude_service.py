import json
import logging
import time
import httpx
from app.config import settings

logger = logging.getLogger("ai.claude")

# 모델별 $/1M 토큰 단가 (input, output). 확인된 모델만 등록 — 없는 모델은 cost_usd=None으로 로깅.
_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    # "claude-opus-4-5-20251101": 레거시 모델, 공식 단가 확인 후 채울 것
}


def _log_claude_call(
    *,
    kind: str,
    model: str,
    latency_ms: float,
    usage: dict | None,
    error: str | None = None,
) -> None:
    """Claude 호출 1건의 latency/토큰/비용을 구조화 로그로 남긴다."""
    input_tokens = (usage or {}).get("input_tokens", 0)
    output_tokens = (usage or {}).get("output_tokens", 0)

    pricing = _PRICING_USD_PER_1M.get(model)
    cost_usd = None
    if pricing and usage is not None:
        cost_usd = round(
            input_tokens / 1_000_000 * pricing[0] + output_tokens / 1_000_000 * pricing[1],
            6,
        )

    logger.info(json.dumps({
        "event": "claude_call",
        "kind": kind,  # "text" | "vision"
        "model": model,
        "latency_ms": round(latency_ms, 1),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "error": error,
    }, ensure_ascii=False))


def strip_json_code_block(raw: str) -> str:
    """Claude 응답에서 ```json ... ``` 코드블록 마커를 제거하고 JSON 본문만 반환한다."""
    cleaned = raw.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    return cleaned


async def call_claude(prompt: str, model: str | None = None, max_tokens: int = 1000) -> str:
    """
    GMS API를 통해 Claude 호출.
    dev 환경에서는 mock 응답 반환 (크레딧 절약).
    """
    if settings.env == "dev":
        return _mock_response(prompt)

    return await _call_gms(prompt, model=model or settings.default_model, max_tokens=max_tokens)


async def _call_gms(prompt: str, model: str, max_tokens: int) -> str:
    """
    GMS(Gen AI Management System) API 호출.
    curl 형식:
      POST https://gms.ssafy.io/gmsapi/api.anthropic.com/v1/messages
      x-api-key: $GMS_KEY
      anthropic-version: 2023-06-01
    """
    url = f"{settings.gms_base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.gms_api_key,
        "anthropic-version": settings.anthropic_version,
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except Exception as e:
        _log_claude_call(
            kind="text", model=model,
            latency_ms=(time.monotonic() - start) * 1000,
            usage=None, error=f"{type(e).__name__}: {e}",
        )
        raise

    data = response.json()
    _log_claude_call(
        kind="text", model=model,
        latency_ms=(time.monotonic() - start) * 1000,
        usage=data.get("usage"),
    )
    return data["content"][0]["text"]


def _mock_response(prompt: str) -> str:
    """개발용 mock 응답 (크레딧 절약). JSON 요청 여부에 따라 형식 분리."""
    if "체크인" in prompt:
        return "2주 동안 꾸준히 노력하셨어요! 달성률이 낮더라도 포기하지 마세요. 목표를 조금 조정하면 더 오래 지속 가능한 건강 습관을 만들 수 있습니다."

    elif "운동 코칭" in prompt:
        return (
            "스쿼트 동작 시 무릎이 발끝을 넘지 않도록 주의하고, 코어를 단단히 잡아주세요. "
            "현재 성공률을 보면 하체 운동 집중이 필요합니다. "
            "다음 주는 무게를 유지하며 자세 완성도를 높이는 데 집중해보세요!"
        )

    elif "칼로리 조정" in prompt:
        return "체중 변화 추세를 분석해 목표 칼로리를 조정했습니다. 새로운 목표에 맞춰 식단을 구성해보세요!"

    elif "리포트" in prompt or "주차" in prompt:
        return "이번 주도 꾸준히 노력하셨네요! 칼로리 달성률이 안정적으로 유지되고 있어요. 다음 주에는 단백질 섭취를 조금 더 신경 써보세요."

    elif "영양 균형" in prompt or "diet" in prompt.lower():
        return "오늘 식단을 분석한 결과, 전반적으로 균형 잡힌 하루를 보내셨네요! 단백질 섭취를 조금 더 늘리면 목표 달성에 가까워질 거예요."

    elif "조정" in prompt or "adjust" in prompt.lower():
        return "분석 결과를 반영했습니다. 꾸준한 노력이 빛을 발하고 있어요! 다음 주도 현재 강도를 유지하며 도전해보세요."

    elif "루틴" in prompt or "routine" in prompt.lower():
        return json.dumps({
            "routine_name": "4일 상체/하체 분할 루틴",
            "days": [
                {
                    "day_label": "상체",
                    "exercises": [
                        {"name": "벤치프레스", "sets": 4, "reps": 8, "weight_kg": 60.0},
                        {"name": "덤벨 숄더프레스", "sets": 3, "reps": 10, "weight_kg": 18.0},
                        {"name": "랫풀다운", "sets": 3, "reps": 10, "weight_kg": 45.0}
                    ]
                },
                {
                    "day_label": "하체",
                    "exercises": [
                        {"name": "바벨 스쿼트", "sets": 4, "reps": 8, "weight_kg": 80.0},
                        {"name": "레그프레스", "sets": 3, "reps": 12, "weight_kg": 120.0},
                        {"name": "루마니안 데드리프트", "sets": 3, "reps": 10, "weight_kg": 60.0}
                    ]
                },
                {
                    "day_label": "상체",
                    "exercises": [
                        {"name": "인클라인 벤치프레스", "sets": 3, "reps": 10, "weight_kg": 50.0},
                        {"name": "바벨 로우", "sets": 4, "reps": 8, "weight_kg": 55.0},
                        {"name": "바벨 컬", "sets": 3, "reps": 12, "weight_kg": 25.0}
                    ]
                },
                {
                    "day_label": "하체",
                    "exercises": [
                        {"name": "핵 스쿼트", "sets": 4, "reps": 10, "weight_kg": 70.0},
                        {"name": "레그 컬", "sets": 3, "reps": 12, "weight_kg": 40.0},
                        {"name": "카프레이즈", "sets": 4, "reps": 15, "weight_kg": 0.0}
                    ]
                }
            ],
            "ai_comment": "근육량 증가를 위해 복합운동 위주로 구성했습니다. 점진적으로 무게를 늘려보세요!"
        }, ensure_ascii=False)

    elif "JSON" in prompt or "json" in prompt:
        return (
            '[{"name":"닭가슴살 샐러드","kcal":380,"protein_g":42,"carb_g":18,"fat_g":12,'
            '"reason":"단백질 보충에 최적"},'
            '{"name":"두부된장찌개+현미밥","kcal":420,"protein_g":22,"carb_g":58,"fat_g":9,'
            '"reason":"균형잡힌 한식"},'
            '{"name":"연어구이+고구마","kcal":390,"protein_g":35,"carb_g":32,"fat_g":14,'
            '"reason":"오메가3 + 복합 탄수화물"}]'
        )
    else:
        return "오늘 하루도 균형 잡힌 식단으로 건강 목표에 가까워지고 있어요! 단백질 섭취에 특히 신경 써보세요."


async def call_claude_vision(
    image_base64: str,
    media_type: str,
    prompt: str,
    model: str | None = None,
    max_tokens: int = 800,
) -> str:
    """
    Claude Vision API 호출. 이미지 + 텍스트 프롬프트를 받아 분석 결과를 문자열로 반환.
    dev 환경에서는 mock JSON 응답 반환.
    """
    if settings.env == "dev":
        return _mock_vision_response()

    return await _call_gms_vision(
        image_base64=image_base64,
        media_type=media_type,
        prompt=prompt,
        model=model or "claude-opus-4-5-20251101",
        max_tokens=max_tokens,
    )


async def _call_gms_vision(
    image_base64: str, media_type: str, prompt: str, model: str, max_tokens: int
) -> str:
    url = f"{settings.gms_base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": settings.gms_api_key,
        "anthropic-version": settings.anthropic_version,
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_base64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except Exception as e:
        _log_claude_call(
            kind="vision", model=model,
            latency_ms=(time.monotonic() - start) * 1000,
            usage=None, error=f"{type(e).__name__}: {e}",
        )
        raise

    data = response.json()
    _log_claude_call(
        kind="vision", model=model,
        latency_ms=(time.monotonic() - start) * 1000,
        usage=data.get("usage"),
    )
    return data["content"][0]["text"]


def _mock_vision_response() -> str:
    return json.dumps({
        "detected_items": [
            {
                "name": "닭가슴살",
                "estimated_grams": 150.0,
                "kcal": 165.0,
                "protein_g": 31.0,
                "carb_g": 0.0,
                "fat_g": 3.6,
            },
            {
                "name": "현미밥",
                "estimated_grams": 200.0,
                "kcal": 278.0,
                "protein_g": 5.6,
                "carb_g": 58.0,
                "fat_g": 1.6,
            },
        ],
        "ai_comment": "[MOCK] 고단백 균형 식단이네요! 단백질 섭취가 훌륭합니다.",
    }, ensure_ascii=False)
