"""Qwen-backed AI services used by the core game loop.

The module intentionally keeps the network layer separate from Pygame. Jobs run
in a worker thread so the camera and game UI remain responsive while the model
is reasoning.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from dotenv import load_dotenv


class AIError(RuntimeError):
    """Raised when the model cannot produce a valid structured response."""


@dataclass(frozen=True)
class MatchResult:
    score: int
    accepted: bool
    explanation: str
    best_concept_id: str
    feedback: str


@dataclass(frozen=True)
class EnemyDecision:
    level: int
    formula_count: int
    hint_enabled: bool
    enemy_name: str
    reason: str
    formula_ids: list[str]
    hint_text: str = ""


@dataclass(frozen=True)
class ReportResult:
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    review_chapters: list[str]
    next_steps: list[str]


@dataclass
class AIJob:
    name: str
    operation: Callable[[Any], Any]
    argument: Any
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    result: Any = None
    error: str | None = None


class AIWorker:
    """Executes AI operations without blocking the rendering thread."""

    def __init__(self, client: "QwenAIClient") -> None:
        self.client = client
        self._jobs: queue.Queue[AIJob | None] = queue.Queue()
        self._results: queue.Queue[AIJob] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="qwen-ai-worker", daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            self._jobs.put(None)
            self._thread.join(timeout=2)
            self._started = False

    def submit(self, name: str, operation: Callable[[Any], Any], argument: Any) -> AIJob:
        job = AIJob(name=name, operation=operation, argument=argument)
        self._jobs.put(job)
        return job

    def poll(self) -> list[AIJob]:
        completed: list[AIJob] = []
        while True:
            try:
                completed.append(self._results.get_nowait())
            except queue.Empty:
                return completed

    @property
    def pending_count(self) -> int:
        return self._jobs.qsize() + self._results.qsize()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                job.result = job.operation(job.argument)
            except Exception as exc:  # Keep the game alive and surface the failure.
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.completed_at = time.monotonic()
                self._results.put(job)


class QwenAIClient:
    """Small DashScope-compatible client with JSON response validation."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        load_dotenv()
        load_dotenv(os.path.expanduser("~/.codex/.env"), override=False)

        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = (base_url or os.getenv("QWEN_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.model = model or os.getenv("QWEN_MODEL") or "qwen3.8-max"
        self.timeout = timeout or float(os.getenv("QWEN_TIMEOUT_SECONDS", "45"))
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def match_answer(self, payload: dict[str, Any]) -> MatchResult:
        formula = payload["formula"]
        selected = payload["selected_concept"]
        candidates = payload["candidate_concepts"]
        prompt = {
            "task": "判断玩家选择的知识点是否能用于解决题目公式",
            "formula_id": formula["id"],
            "formula": formula["formula"],
            "subject": formula["subject"],
            "selected_concept": selected,
            "candidate_concepts": candidates,
            "rules": [
                "只根据数学或物理含义判断，不要被选项顺序影响",
                "score 为 0 到 100 的整数",
                "只有真正能直接解决问题或属于关键必要理论时才 accepted=true",
                "explanation 用一句中文解释公式的真正知识点",
                "feedback 用一句中文告诉玩家下一步怎么做",
            ],
        }
        response = self._chat_json(
            [
                {
                    "role": "system",
                    "content": "你是学科守卫者的核心判题引擎。严格输出 JSON，不要输出 Markdown。",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ]
        )
        try:
            score = max(0, min(100, int(response["score"])))
            accepted = score >= 80 and bool(response["accepted"])
            explanation = str(response["explanation"]).strip()
            best_concept_id = str(
                response.get(
                    "best_concept_id",
                    selected["id"] if accepted else candidates[0]["id"],
                )
            ).strip()
            feedback = str(response["feedback"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise AIError(f"判题结果字段不完整: {response}") from exc

        valid_ids = {item["id"] for item in candidates}
        if best_concept_id not in valid_ids:
            raise AIError(f"AI 返回了不存在的知识点: {best_concept_id}")
        if not explanation or not feedback:
            raise AIError("AI 返回了空的判题解释")
        return MatchResult(score, accepted, explanation, best_concept_id, feedback)

    def decide_enemy(self, payload: dict[str, Any]) -> EnemyDecision:
        prompt = {
            "task": "根据玩家实时表现设计下一只怪物",
            "metrics": payload["metrics"],
            "available_formulas": payload["available_formulas"],
            "recent_results": payload["recent_results"],
            "current_level": payload["current_level"],
            "enemy_index": payload["enemy_index"],
            "rules": [
                "level 只能是 1、2、3",
                "formula_count 只能是 1、2、3，并且不能超过 level",
                "连续答对时提高挑战，连续答错时降低挑战",
                "formula_ids 必须来自 available_formulas，数量等于 formula_count",
                "enemy_name 和 reason 使用简短中文",
                "hint_enabled 为布尔值，为 true 时 hint_text 给出一条不直接泄露答案的中文提示",
            ],
        }
        response = self._chat_json(
            [
                {
                    "role": "system",
                    "content": "你是动态难度决策器。严格输出 JSON，不要输出 Markdown。",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ]
        )
        available = {item["id"] for item in payload["available_formulas"]}
        try:
            level = max(1, min(3, int(response["level"])))
            count = max(1, min(3, min(level, int(response["formula_count"]))))
            formula_ids = [str(item) for item in response["formula_ids"]]
            unique_ids = list(dict.fromkeys(formula_ids))
            if len(unique_ids) != count or any(item not in available for item in unique_ids):
                raise AIError(f"AI 返回了无效公式组合: {formula_ids}")
            hint_enabled = bool(response["hint_enabled"])
            enemy_name = str(response["enemy_name"]).strip() or "未知考点兽"
            reason = str(response["reason"]).strip()
            hint_text = str(response.get("hint_text", "")).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise AIError(f"难度决策字段不完整: {response}") from exc
        return EnemyDecision(level, count, hint_enabled, enemy_name, reason, unique_ids, hint_text)

    def generate_report(self, payload: dict[str, Any]) -> ReportResult:
        prompt = {
            "task": "根据整局游戏记录生成个性化学习报告",
            "metrics": payload["metrics"],
            "concept_stats": payload["concept_stats"],
            "attempts": payload["attempts"][-30:],
            "rules": [
                "所有结论必须来自记录，不要虚构练习经历",
                "strengths 和 weaknesses 使用中文短语列表",
                "review_chapters 使用题库中的章节名",
                "next_steps 给出 3 到 5 条可执行建议",
            ],
        }
        response = self._chat_json(
            [
                {
                    "role": "system",
                    "content": "你是学习分析教练。严格输出 JSON，不要输出 Markdown。",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            max_tokens=1200,
        )
        try:
            summary = str(response["summary"]).strip()
            strengths = [str(item).strip() for item in response["strengths"] if str(item).strip()]
            weaknesses = [str(item).strip() for item in response["weaknesses"] if str(item).strip()]
            chapters = [str(item).strip() for item in response["review_chapters"] if str(item).strip()]
            next_steps = [str(item).strip() for item in response["next_steps"] if str(item).strip()]
        except (KeyError, TypeError) as exc:
            raise AIError(f"学习报告字段不完整: {response}") from exc
        if not summary or not next_steps:
            raise AIError("AI 返回了不完整的学习报告")
        return ReportResult(summary, strengths, weaknesses, chapters, next_steps)

    def _chat_json(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 700,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise AIError(
                "缺少 DASHSCOPE_API_KEY。请复制 .env.example 为 .env 并填写 Key，"
                "或在用户级 .codex/.env 中配置。"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.25,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = self.session.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            body = response.text[:500]
            raise AIError(f"Qwen API 请求失败 ({response.status_code}): {body}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIError(f"Qwen API 返回格式异常: {response.text[:500]}") from exc
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return self._parse_json_object(str(content))

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                raise AIError(f"AI 没有返回 JSON: {content[:300]}") from exc
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as nested_exc:
                raise AIError(f"AI JSON 解析失败: {content[:300]}") from nested_exc
        if not isinstance(parsed, dict):
            raise AIError("AI 返回的 JSON 顶层必须是对象")
        return parsed
