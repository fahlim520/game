from __future__ import annotations

from pathlib import Path

from ai_matcher import EnemyDecision, MatchResult
from game_engine import ContentBank, GameEngine


def make_engine() -> GameEngine:
    root = Path(__file__).resolve().parents[1]
    return GameEngine(ContentBank(root))


def decision(formula_ids: list[str], level: int = 1) -> EnemyDecision:
    return EnemyDecision(
        level=level,
        formula_count=len(formula_ids),
        hint_enabled=False,
        enemy_name="测试怪",
        reason="测试决策",
        formula_ids=formula_ids,
    )


def match(accepted: bool, score: int = 95) -> MatchResult:
    return MatchResult(
        score=score,
        accepted=accepted,
        explanation="测试解释",
        best_concept_id="indefinite_integral",
        feedback="测试反馈",
    )


def test_correct_answer_defeats_monster_and_updates_metrics() -> None:
    engine = make_engine()
    engine.start(0)
    engine.install_monster(decision(["calc_indefinite_integral_x2"]), 0)

    resolved = engine.resolve_answer("indefinite_integral", match(True), 2.5)

    assert resolved["monster_defeated"] is True
    assert engine.enemies_defeated == 1
    assert engine.metrics.correct == 1
    assert engine.metrics.average_response == 2.5
    assert engine.lives == GameEngine.START_LIVES


def test_wrong_answer_moves_monster_forward_and_deducts_life() -> None:
    engine = make_engine()
    engine.start(0)
    engine.install_monster(decision(["calc_indefinite_integral_x2"]), 0)

    engine.resolve_answer("limit", match(False, score=20), 2.0)

    assert engine.enemies_defeated == 0
    assert engine.metrics.wrong == 1
    assert engine.lives == GameEngine.START_LIVES - 1


def test_six_wrong_answers_end_game() -> None:
    engine = make_engine()
    engine.start(0)
    engine.install_monster(decision(["calc_indefinite_integral_x2"]), 0)
    for index in range(GameEngine.START_LIVES):
        engine.resolve_answer("limit", match(False, score=10), index + 1)

    assert engine.game_over is True
    assert engine.won is False
    assert engine.lives == 0


def test_thirty_cleared_formulas_win_the_game() -> None:
    engine = make_engine()
    engine.start(0)
    formula_id = "calc_indefinite_integral_x2"
    for index in range(GameEngine.WIN_ENEMIES):
        engine.install_monster(decision([formula_id]), float(index))
        engine.resolve_answer("indefinite_integral", match(True), float(index) + 1)
        if not engine.game_over:
            engine.advance_enemy()

    assert engine.enemies_defeated == GameEngine.WIN_ENEMIES
    assert engine.game_over is True
    assert engine.won is True


def test_compound_monster_requires_all_formula_slots_to_be_cleared() -> None:
    engine = make_engine()
    engine.start(0)
    engine.install_monster(
        decision(
            [
                "calc_indefinite_integral_x2",
                "calc_derivative_chain",
            ],
            level=2,
        ),
        0,
    )

    first = engine.resolve_answer("indefinite_integral", match(True), 1)
    second = engine.resolve_answer("chain_rule", match(True), 2)

    assert first["monster_defeated"] is False
    assert second["monster_defeated"] is True
    assert engine.enemies_defeated == 1


def test_match_payload_does_not_leak_accepted_solution() -> None:
    engine = make_engine()
    formula_id = "calc_indefinite_integral_x2"
    payload = engine.bank.formula_payload(formula_id)

    assert payload["formula"] == "∫ x² dx"
    assert "accepted_concepts" not in payload
    assert "skill_label" not in payload
    assert "canonical_explanation" not in payload


def test_content_bank_includes_advanced_mechanics_and_thermodynamics() -> None:
    bank = ContentBank(Path(__file__).resolve().parents[1])
    physics = [item for item in bank.formulas.values() if item.subject == "physics"]
    labels = {item.skill_label for item in physics}

    assert "拉格朗日方程" in labels
    assert "纳维-斯托克斯方程" in labels
    assert "热力学第一定律" in labels
    assert "玻尔兹曼熵" in labels
    assert "正则配分函数" in labels
    engine = GameEngine(bank)
    catalog = engine.formula_catalog_for_ai(engine.current_level)
    assert engine.current_level == 2
    assert catalog
    assert all(item["difficulty"] == 2 for item in catalog)
