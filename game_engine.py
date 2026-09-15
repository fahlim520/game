"""Pure game-state logic for Knowledge Defender.

This module does not import Pygame. Keeping game rules independent makes the
prototype testable without a display, camera, or live model call.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ai_matcher import EnemyDecision, MatchResult, ReportResult


SUBJECT_ORDER = ("calculus", "linear_probability", "physics")
SUBJECT_LABELS = {
    "calculus": "高数",
    "linear_probability": "线代/概率",
    "physics": "大物",
}


@dataclass(frozen=True)
class Formula:
    id: str
    subject: str
    formula: str
    accepted_concepts: tuple[str, ...]
    skill_label: str
    canonical_explanation: str
    difficulty: int
    latex: str = ""


@dataclass(frozen=True)
class KnowledgePoint:
    id: str
    subject: str
    title: str
    description: str
    chapter: str


class ContentBank:
    def __init__(self, root: Path) -> None:
        formula_data = json.loads((root / "data" / "formulas.json").read_text(encoding="utf-8"))
        knowledge_data = json.loads((root / "data" / "knowledge.json").read_text(encoding="utf-8"))
        self.formulas = {
            item["id"]: Formula(
                id=item["id"],
                subject=item["subject"],
                formula=item["formula"],
                accepted_concepts=tuple(item["accepted_concepts"]),
                skill_label=item["skill_label"],
                canonical_explanation=item["canonical_explanation"],
                difficulty=int(item["difficulty"]),
                latex=item.get("latex", item["formula"]),
            )
            for item in formula_data
        }
        self.knowledge = {
            item["id"]: KnowledgePoint(
                id=item["id"],
                subject=item["subject"],
                title=item["title"],
                description=item["description"],
                chapter=item["chapter"],
            )
            for item in knowledge_data
        }
        formula_ids = set(self.formulas)
        knowledge_ids = set(self.knowledge)
        for formula in self.formulas.values():
            missing = set(formula.accepted_concepts) - knowledge_ids
            if missing:
                raise ValueError(f"{formula.id} 引用了不存在的知识点: {sorted(missing)}")
        self.validate_all_formula_ids(formula_ids)

    def validate_all_formula_ids(self, formula_ids: Iterable[str]) -> None:
        missing = set(formula_ids) - set(self.formulas)
        if missing:
            raise ValueError(f"不存在的公式 ID: {sorted(missing)}")

    def knowledge_for_subject(self, subject: str) -> list[KnowledgePoint]:
        return sorted(
            (item for item in self.knowledge.values() if item.subject == subject),
            key=lambda item: item.title,
        )

    def all_knowledge(self) -> list[KnowledgePoint]:
        return list(self.knowledge.values())

    def knowledge_payload_by_subject(self) -> dict[str, list[dict[str, str]]]:
        return {
            subject: [
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "chapter": item.chapter,
                }
                for item in self.knowledge_for_subject(subject)
            ]
            for subject in SUBJECT_ORDER
        }

    def formula_public_payload(self, formula_id: str) -> dict[str, Any]:
        formula = self.formulas[formula_id]
        return {
            "id": formula.id,
            "subject": formula.subject,
            "formula": formula.formula,
            "skill_label": formula.skill_label,
            "difficulty": formula.difficulty,
            "latex": formula.latex,
        }

    def formulas_for_level(self, level: int) -> list[Formula]:
        allowed = {level, max(1, level - 1)}
        return [item for item in self.formulas.values() if item.difficulty in allowed]

    def formula_payload(self, formula_id: str) -> dict[str, Any]:
        return self._formula_to_dict(self.formulas[formula_id])

    def formula_ids_by_subject(self) -> dict[str, list[str]]:
        result = {subject: [] for subject in SUBJECT_ORDER}
        for formula in self.formulas.values():
            result.setdefault(formula.subject, []).append(formula.id)
        return result

    @staticmethod
    def _formula_to_dict(formula: Formula) -> dict[str, Any]:
        return {
            "id": formula.id,
            "subject": formula.subject,
            "formula": formula.formula,
            "difficulty": formula.difficulty,
        }


@dataclass
class Monster:
    decision: EnemyDecision
    formula_ids: list[str]
    cleared_formula_ids: list[str] = field(default_factory=list)

    @property
    def is_defeated(self) -> bool:
        return not self.formula_ids

    @property
    def current_formula_id(self) -> str | None:
        return self.formula_ids[0] if self.formula_ids else None


@dataclass
class Attempt:
    enemy_index: int
    formula_id: str
    selected_concept_id: str
    score: int
    accepted: bool
    reaction_seconds: float
    elapsed_seconds: float


@dataclass
class Metrics:
    attempts: int = 0
    correct: int = 0
    wrong: int = 0
    response_total: float = 0.0
    streak: int = 0
    best_streak: int = 0

    @property
    def accuracy(self) -> float:
        return (self.correct / self.attempts * 100.0) if self.attempts else 0.0

    @property
    def average_response(self) -> float:
        return (self.response_total / self.attempts) if self.attempts else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "correct": self.correct,
            "wrong": self.wrong,
            "accuracy": round(self.accuracy, 1),
            "average_response_seconds": round(self.average_response, 2),
            "streak": self.streak,
            "best_streak": self.best_streak,
        }


@dataclass
class ConceptStat:
    attempts: int = 0
    correct: int = 0
    response_total: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "correct": self.correct,
            "accuracy": round(self.correct / self.attempts * 100, 1) if self.attempts else 0.0,
            "average_response_seconds": round(self.response_total / self.attempts, 2) if self.attempts else 0.0,
        }


class GameEngine:
    WIN_ENEMIES = 30
    START_LIVES = 6
    MAX_LIVES = 6

    def __init__(
        self,
        bank: ContentBank,
        rng: random.Random | None = None,
        target_enemies: int = 30,
    ) -> None:
        self.bank = bank
        self.rng = rng or random.Random()
        self.win_enemies = max(1, int(target_enemies))
        self.metrics = Metrics()
        self.concept_stats: dict[str, ConceptStat] = {}
        self.attempts: list[Attempt] = []
        self.enemies_defeated = 0
        self.lives = self.START_LIVES
        self.current_level = 2
        self.used_formula_ids: set[str] = set()
        self.enemy_index = 1
        self.game_over = False
        self.won = False
        self.game_started_at = 0.0
        self.formula_started_at = 0.0
        self.monster: Monster | None = None

    def start(self, now: float) -> None:
        self.game_started_at = now
        self.formula_started_at = now

    def install_monster(self, decision: EnemyDecision, now: float) -> None:
        self.bank.validate_all_formula_ids(decision.formula_ids)
        self.current_level = decision.level
        self.used_formula_ids.update(decision.formula_ids)
        self.monster = Monster(decision=decision, formula_ids=list(decision.formula_ids))
        self.formula_started_at = now

    def resolve_answer(
        self,
        selected_concept_id: str,
        result: MatchResult,
        now: float,
    ) -> dict[str, Any]:
        if self.monster is None or self.monster.current_formula_id is None:
            raise RuntimeError("当前没有可回答的公式")

        formula_id = self.monster.current_formula_id
        formula = self.bank.formulas[formula_id]
        reaction = max(0.0, now - self.formula_started_at)
        attempt = Attempt(
            enemy_index=self.enemy_index,
            formula_id=formula_id,
            selected_concept_id=selected_concept_id,
            score=result.score,
            accepted=result.accepted,
            reaction_seconds=reaction,
            elapsed_seconds=max(0.0, now - self.game_started_at),
        )
        self.attempts.append(attempt)
        self.metrics.attempts += 1
        self.metrics.response_total += reaction
        stat = self.concept_stats.setdefault(selected_concept_id, ConceptStat())
        stat.attempts += 1
        stat.response_total += reaction

        if result.accepted:
            self.metrics.correct += 1
            self.metrics.streak += 1
            self.metrics.best_streak = max(self.metrics.best_streak, self.metrics.streak)
            stat.correct += 1
            self.monster.formula_ids.pop(0)
            if self.monster.is_defeated:
                self.enemies_defeated += 1
                if self.enemies_defeated >= self.win_enemies:
                    self.game_over = True
                    self.won = True
            else:
                self.formula_started_at = now
        else:
            self.metrics.wrong += 1
            self.metrics.streak = 0
            self.lives -= 1
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
                self.won = False

        return {
            "formula_id": formula.id,
            "formula": formula.formula,
            "formula_explanation": formula.canonical_explanation,
            "monster_defeated": self.monster.is_defeated if self.monster else False,
        }

    def advance_enemy(self) -> None:
        if self.game_over:
            return
        self.enemy_index += 1
        self.monster = None

    def candidate_concepts(self, formula_id: str, limit: int = 10) -> list[dict[str, str]]:
        formula = self.bank.formulas[formula_id]
        accepted = set(formula.accepted_concepts)
        subject_items = self.bank.knowledge_for_subject(formula.subject)
        same_chapter = [
            item for item in subject_items
            if item.id not in accepted and any(
                item.chapter == self.bank.knowledge[accepted_id].chapter
                for accepted_id in accepted
                if accepted_id in self.bank.knowledge
            )
        ]
        other_items = [item for item in subject_items if item.id not in accepted and item not in same_chapter]
        self.rng.shuffle(same_chapter)
        self.rng.shuffle(other_items)

        selected: dict[str, KnowledgePoint] = {}
        for item_id in formula.accepted_concepts:
            selected[item_id] = self.bank.knowledge[item_id]
        for item in same_chapter + other_items:
            if len(selected) >= min(limit, len(subject_items)):
                break
            selected[item.id] = item
        return [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "chapter": item.chapter,
            }
            for item in selected.values()
        ]

    def recent_results(self, count: int = 6) -> list[dict[str, Any]]:
        return [
            {
                "formula_id": item.formula_id,
                "selected_concept_id": item.selected_concept_id,
                "score": item.score,
                "accepted": item.accepted,
            }
            for item in self.attempts[-count:]
        ]

    def ai_metrics(self) -> dict[str, Any]:
        data = self.metrics.as_dict()
        data.update(
            {
                "enemies_defeated": self.enemies_defeated,
                "enemies_remaining": max(0, self.win_enemies - self.enemies_defeated),
                "lives": self.lives,
                "current_level": self.current_level,
            }
        )
        return data

    def formula_catalog_for_ai(self, level: int) -> list[dict[str, Any]]:
        formulas = self.bank.formulas_for_level(level)
        preferred = [item for item in formulas if item.difficulty == level]
        if len(preferred) >= 6:
            formulas = preferred
        unused = [item for item in formulas if item.id not in self.used_formula_ids]
        if len(unused) >= 4:
            formulas = unused
        return [
            {
                "id": item.id,
                "subject": item.subject,
                "formula": item.formula,
                "skill_label": item.skill_label,
                "difficulty": item.difficulty,
            }
            for item in formulas
        ]

    def report_payload(self) -> dict[str, Any]:
        return {
            "metrics": self.ai_metrics(),
            "concept_stats": {
                concept_id: {
                    **stat.as_dict(),
                    "title": self.bank.knowledge[concept_id].title,
                    "chapter": self.bank.knowledge[concept_id].chapter,
                }
                for concept_id, stat in self.concept_stats.items()
            },
            "attempts": [
                {
                    "enemy_index": item.enemy_index,
                    "formula_id": item.formula_id,
                    "formula": self.bank.formulas[item.formula_id].formula,
                    "selected_concept_id": item.selected_concept_id,
                    "selected_concept": self.bank.knowledge[item.selected_concept_id].title,
                    "score": item.score,
                    "accepted": item.accepted,
                    "reaction_seconds": round(item.reaction_seconds, 2),
                }
                for item in self.attempts
            ],
        }

    def fallback_report(self, result: ReportResult | None = None) -> ReportResult:
        if result is not None:
            return result
        weak = sorted(
            (
                (stat.as_dict()["accuracy"], concept_id)
                for concept_id, stat in self.concept_stats.items()
                if stat.attempts
            ),
            key=lambda item: item[0],
        )
        weakest = weak[:3]
        return ReportResult(
            summary=f"完成 {self.enemies_defeated} 只怪物，正确率 {self.metrics.accuracy:.1f}%。",
            strengths=[self.bank.knowledge[item_id].title for _, item_id in weak[-2:]],
            weaknesses=[self.bank.knowledge[item_id].title for _, item_id in weakest],
            review_chapters=list(dict.fromkeys(self.bank.knowledge[item_id].chapter for _, item_id in weakest)),
            next_steps=["复盘错误公式", "按薄弱章节完成 10 分钟专项练习", "再次挑战并提高平均反应速度"],
        )
