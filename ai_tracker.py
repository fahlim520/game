"""AI decision helpers and local metric serialization.

The learning metrics themselves are deterministic so they can be audited.
Difficulty decisions and reports are delegated to Qwen in `ai_matcher.py`.
"""

from __future__ import annotations

from typing import Any


def build_enemy_request(engine: Any) -> dict[str, Any]:
    """Build a compact, privacy-conscious prompt payload for the next enemy."""
    catalog = engine.formula_catalog_for_ai(engine.current_level)
    by_subject: dict[str, list[dict[str, object]]] = {}
    for item in catalog:
        by_subject.setdefault(str(item["subject"]), []).append(item)
    available: list[dict[str, object]] = []
    for items in by_subject.values():
        engine.rng.shuffle(items)
        available.extend(items[:3])
    engine.rng.shuffle(available)
    return {
        "metrics": engine.ai_metrics(),
        "available_formulas": available,
        "recent_results": engine.recent_results(),
        "current_level": engine.current_level,
        "enemy_index": engine.enemy_index,
        "avoid_formula_ids": [],
    }


def build_match_request(engine: Any, formula_id: str, selected_concept_id: str) -> dict[str, Any]:
    """Send only the current question, selected answer, and candidate labels."""
    return {
        "formula": engine.bank.formula_payload(formula_id),
        "selected_concept": {
            "id": selected_concept_id,
            "title": engine.bank.knowledge[selected_concept_id].title,
            "description": engine.bank.knowledge[selected_concept_id].description,
            "chapter": engine.bank.knowledge[selected_concept_id].chapter,
        },
        "candidate_concepts": engine.candidate_concepts(formula_id),
    }
