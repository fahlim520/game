"""Generate a UI preview image using the real Qwen enemy decision endpoint."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pygame

from ai_matcher import QwenAIClient
from ai_tracker import build_enemy_request
from main import AppState, KnowledgeDefenderApp


def main() -> int:
    root = ROOT
    args = argparse.Namespace(
        camera=0,
        no_camera=True,
        input="mouse",
        fullscreen=False,
        smoke_test=False,
        target_enemies=30,
    )
    app = KnowledgeDefenderApp(args, root)
    try:
        app.engine.start(time.monotonic())
        client = QwenAIClient()
        decision = client.decide_enemy(build_enemy_request(app.engine))
        app.engine.install_monster(decision, time.monotonic())
        app.state = AppState.PLAYING
        app.ai_status = (
            f"AI 决策：{decision.enemy_name}，"
            f"难度 L{decision.level}，{decision.formula_count} 个公式槽，"
            f"{'提供提示' if decision.hint_enabled else '无提示'}"
        )
        app.last_ai_result = decision.reason
        app.ai_trace = [
            "AI 决策：读取正确率、速度和连击记录",
            f"AI 生成怪物：{decision.enemy_name} / {decision.reason}",
        ]
        app._render()
        output = root / "docs" / "preview.png"
        output.parent.mkdir(exist_ok=True)
        pygame.image.save(app.screen, output)
        print(output)
        return 0
    finally:
        app._shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
