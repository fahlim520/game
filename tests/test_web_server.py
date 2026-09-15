from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from ai_matcher import EnemyDecision, MatchResult, ReportResult
from web_server import KnowledgeDefenderWebServer


class FakeAIClient:
    configured = True
    model = "fake-qwen"

    def decide_enemy(self, payload):
        return EnemyDecision(
            level=1,
            formula_count=1,
            hint_enabled=True,
            enemy_name="测试怪",
            reason="测试决策",
            formula_ids=["physics_newton_second"],
            hint_text="关注力、质量和加速度的关系。",
        )

    def match_answer(self, payload):
        return MatchResult(
            score=96,
            accepted=True,
            explanation="牛顿第二定律描述合外力、质量与加速度的关系。",
            best_concept_id="newton_second_law",
            feedback="继续使用受力分析确定合外力。",
        )

    def generate_report(self, payload):
        return ReportResult(
            summary="测试报告",
            strengths=["牛顿第二定律"],
            weaknesses=["样本不足"],
            review_chapters=["大物第1章 质点运动"],
            next_steps=["复练习受力分析"],
        )


def post_json(base_url: str, path: str, payload: dict, token: str = "test-token"):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Demo-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_web_session_and_match_flow() -> None:
    root = Path(__file__).resolve().parents[1]
    server = KnowledgeDefenderWebServer(
        ("127.0.0.1", 0),
        root=root,
        access_token="test-token",
        target_enemies=3,
    )
    server.ai_client = FakeAIClient()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        session = post_json(base_url, "/api/session", {})
        session_id = session["sessionId"]

        enemy = post_json(base_url, "/api/decide", {"sessionId": session_id})
        assert enemy["decision"]["enemyName"] == "测试怪"
        assert enemy["monster"]["currentFormulaId"] == "physics_newton_second"

        match = post_json(
            base_url,
            "/api/match",
            {
                "sessionId": session_id,
                "formulaId": "physics_newton_second",
                "conceptId": "newton_second_law",
            },
        )
        assert match["match"]["accepted"] is True
        assert match["resolved"]["monsterDefeated"] is True
        assert match["state"]["enemiesDefeated"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
