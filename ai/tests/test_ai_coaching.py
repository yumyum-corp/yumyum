import os
os.environ.setdefault("ENV", "dev")

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

VALID_REQUEST = {
    "week_number": 3,
    "health_goal": "MUSCLE",
    "daily_nutrition": [
        {
            "date": f"2026-06-1{i}",
            "kcal": 1900.0,
            "protein_g": 100.0,
            "carb_g": 230.0,
            "fat_g": 60.0,
            "calories_burned": 300.0,
        }
        for i in range(7)
    ],
    "target_kcal": 2000.0,
    "target_protein_g": 120.0,
    "target_carb_g": 250.0,
    "target_fat_g": 65.0,
    "routine_sessions": [
        {
            "exercise_name": "벤치프레스",
            "successful_sets": 4,
            "total_sets": 4,
            "weight_kg": 65.0,
            "session_date": "2026-06-16",
        }
    ],
    "weight_records": [
        {"date": "2026-06-10", "weight_kg": 70.0},
        {"date": "2026-06-17", "weight_kg": 70.3},
    ],
}


def test_주간_코칭_200_반환():
    response = client.post("/ai/coaching/weekly", json=VALID_REQUEST)
    assert response.status_code == 200


def test_응답_필드_7개_모두_존재():
    response = client.post("/ai/coaching/weekly", json=VALID_REQUEST)
    data = response.json()
    for field in ["ai_comment", "nutrition_summary", "exercise_summary",
                  "goal_summary", "avg_calorie_rate", "achievement_days", "weight_trend"]:
        assert field in data, f"Missing field: {field}"


def test_응답에는_기존_7개_필드만_존재한다():
    response = client.post("/ai/coaching/weekly", json=VALID_REQUEST)

    assert set(response.json()) == {
        "ai_comment",
        "nutrition_summary",
        "exercise_summary",
        "goal_summary",
        "avg_calorie_rate",
        "achievement_days",
        "weight_trend",
    }
    assert "routing" not in response.json()


def test_weight_records_없으면_weight_trend_null():
    req = {**VALID_REQUEST, "weight_records": []}
    response = client.post("/ai/coaching/weekly", json=req)
    assert response.status_code == 200
    assert response.json()["weight_trend"] is None


def test_routine_sessions_없어도_200():
    req = {**VALID_REQUEST, "routine_sessions": []}
    response = client.post("/ai/coaching/weekly", json=req)
    assert response.status_code == 200


def test_dev_mock_응답_확인():
    response = client.post("/ai/coaching/weekly", json=VALID_REQUEST)
    data = response.json()
    assert "[MOCK]" in data["ai_comment"]
    assert "[MOCK]" in data["nutrition_summary"]


def test_빈_기록_요청은_llm_없이_데이터_부족_안내를_반환한다():
    req = {
        **VALID_REQUEST,
        "daily_nutrition": [],
        "routine_sessions": [],
        "weight_records": [],
    }

    response = client.post("/ai/coaching/weekly", json=req)

    assert response.status_code == 200
    assert response.json()["ai_comment"] == (
        "이번 주에는 분석할 식단·운동·체중 기록이 부족합니다. "
        "기록을 추가하면 맞춤 코칭을 제공할 수 있습니다."
    )


def test_weight_record가_1개면_weight_trend_null():
    req = {
        **VALID_REQUEST,
        "daily_nutrition": [],
        "routine_sessions": [],
        "weight_records": [{"date": "2026-06-17", "weight_kg": 70.3}],
    }

    response = client.post("/ai/coaching/weekly", json=req)

    assert response.status_code == 200
    assert response.json()["weight_trend"] is None


def test_지원하지_않는_health_goal은_422():
    response = client.post(
        "/ai/coaching/weekly",
        json={**VALID_REQUEST, "health_goal": "GAIN"},
    )

    assert response.status_code == 422
