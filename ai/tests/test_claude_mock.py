import os
os.environ.setdefault("ENV", "dev")

from app.services.claude_service import _mock_response, strip_json_code_block


def test_루틴_프롬프트는_JSON_루틴을_반환한다():
    import json
    result = _mock_response("주 4회 루틴을 JSON으로 생성해주세요")
    data = json.loads(result)
    assert "routine_name" in data
    assert "days" in data
    assert len(data["days"]) > 0
    assert "ai_comment" in data


def test_식단_프롬프트는_기존_포맷을_유지한다():
    import json
    result = _mock_response("JSON 식단 추천해줘")
    items = json.loads(result)
    assert isinstance(items, list)
    assert "name" in items[0]


# ── strip_json_code_block ────────────────────────────────────────────

def test_strip_json_code_block_순수_JSON은_그대로_반환():
    assert strip_json_code_block('[{"name": "a"}]') == '[{"name": "a"}]'


def test_strip_json_code_block_json_라벨_코드블록_제거():
    raw = '```json\n[{"name": "a"}]\n```'
    result = strip_json_code_block(raw)
    assert result.strip() == '[{"name": "a"}]'


def test_strip_json_code_block_라벨_없는_코드블록_제거():
    raw = '```\n[{"name": "a"}]\n```'
    result = strip_json_code_block(raw)
    assert result.strip() == '[{"name": "a"}]'


def test_strip_json_code_block_대문자_JSON_라벨도_제거():
    raw = '```JSON\n[{"name": "a"}]\n```'
    result = strip_json_code_block(raw)
    assert result.strip() == '[{"name": "a"}]'
