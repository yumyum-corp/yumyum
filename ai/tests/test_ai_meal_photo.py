import os
os.environ.setdefault("ENV", "dev")

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

VALID_REQUEST = {
    "image_base64": "dGVzdA==",
    "media_type": "image/jpeg",
    "meal_type": "LUNCH",
}


def test_사진_분석_200_반환():
    response = client.post("/ai/meal/analyze-photo", json=VALID_REQUEST)
    assert response.status_code == 200


def test_사진_분석_응답_구조_검증():
    response = client.post("/ai/meal/analyze-photo", json=VALID_REQUEST)
    data = response.json()
    assert "detected_items" in data
    assert "total_kcal" in data
    assert "ai_comment" in data
    assert isinstance(data["detected_items"], list)


def test_detected_item_필드_검증():
    response = client.post("/ai/meal/analyze-photo", json=VALID_REQUEST)
    items = response.json()["detected_items"]
    assert len(items) > 0
    item = items[0]
    for field in ["name", "estimated_grams", "kcal", "protein_g", "carb_g", "fat_g"]:
        assert field in item


def test_total_kcal_아이템_합산과_일치():
    response = client.post("/ai/meal/analyze-photo", json=VALID_REQUEST)
    data = response.json()
    items_sum = sum(i["kcal"] for i in data["detected_items"])
    assert abs(data["total_kcal"] - items_sum) < 1.0


def test_빈_base64는_400_반환():
    response = client.post("/ai/meal/analyze-photo", json={
        "image_base64": "",
        "media_type": "image/jpeg",
        "meal_type": "BREAKFAST",
    })
    assert response.status_code == 400


def test_지원하지_않는_media_type은_400_반환():
    response = client.post("/ai/meal/analyze-photo", json={
        "image_base64": "dGVzdA==",
        "media_type": "image/gif",
        "meal_type": "BREAKFAST",
    })
    assert response.status_code == 400


def test_너무_큰_base64는_400_반환():
    oversized = "a" * 8_000_001
    response = client.post("/ai/meal/analyze-photo", json={
        "image_base64": oversized,
        "media_type": "image/jpeg",
        "meal_type": "BREAKFAST",
    })
    assert response.status_code == 400


def test_vision_호출_실패시_기술적_오류_메시지_반환(monkeypatch):
    """call_claude_vision이 예외를 던지면 '음식 미감지'가 아닌 오류 메시지를 반환해야 한다."""
    from app.routers import ai_meal

    async def failing_call_claude_vision(**kwargs):
        raise RuntimeError("GMS 타임아웃")

    monkeypatch.setattr(ai_meal, "call_claude_vision", failing_call_claude_vision)

    response = client.post("/ai/meal/analyze-photo", json=VALID_REQUEST)
    data = response.json()
    assert data["detected_items"] == []
    assert "오류" in data["ai_comment"]
    assert "인식하지 못했" not in data["ai_comment"]
