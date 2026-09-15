from __future__ import annotations

from pathlib import Path

from ai_matcher import QwenAIClient
from game_engine import ContentBank, GameEngine


class FakeResponse:
    def __init__(self, content: str, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = content
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers, json, timeout):
        self.requests.append((url, headers, json, timeout))
        return self.responses.pop(0)


def test_parse_json_object_accepts_markdown_fence() -> None:
    parsed = QwenAIClient._parse_json_object('```json\n{"score": 95}\n```')
    assert parsed == {"score": 95}


def test_match_answer_uses_validated_model_output() -> None:
    session = FakeSession(
        [
            FakeResponse(
                '{"score": 96, "accepted": true, '
                '"explanation": "使用不定积分求原函数。", '
                '"best_concept_id": "indefinite_integral", '
                '"feedback": "继续选择原函数相关知识点。"}'
            )
        ]
    )
    client = QwenAIClient(api_key="test-key", session=session)
    result = client.match_answer(
        {
            "formula": {
                "id": "calc_indefinite_integral_x2",
                "formula": "∫ x² dx",
                "subject": "calculus",
            },
            "selected_concept": {
                "id": "indefinite_integral",
                "title": "不定积分",
            },
            "candidate_concepts": [
                {"id": "indefinite_integral", "title": "不定积分"},
                {"id": "limit", "title": "极限"},
            ],
        }
    )
    assert result.accepted is True
    assert result.score == 96
    assert result.best_concept_id == "indefinite_integral"
    assert session.requests[0][0].endswith("/chat/completions")
    assert session.requests[0][2]["response_format"] == {"type": "json_object"}


def test_enemy_decision_is_clamped_and_validated() -> None:
    bank = ContentBank(Path(__file__).resolve().parents[1])
    engine = GameEngine(bank)
    available = engine.formula_catalog_for_ai(2)
    first_id = available[0]["id"]
    session = FakeSession(
        [
                FakeResponse(
                '{"level": 9, "formula_count": 1, "hint_enabled": false, '
                f'"enemy_name": "测试怪", "reason": "测试", "formula_ids": ["{first_id}"]'
                "}"
            )
        ]
    )
    client = QwenAIClient(api_key="test-key", session=session)
    decision = client.decide_enemy(
        {
            "metrics": {"accuracy": 80},
            "available_formulas": available,
            "recent_results": [],
            "current_level": 1,
            "enemy_index": 2,
        }
    )
    assert decision.level == 3
    assert decision.formula_count == 1
    assert decision.formula_ids == [first_id]
