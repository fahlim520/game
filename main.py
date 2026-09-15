"""Knowledge Defender / 学科守卫者 game entry point.

Run with a camera and a real Qwen API key:

    python main.py

Useful development switches:

    python main.py --no-camera --input mouse
    python main.py --smoke-test
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame

from ai_matcher import AIError, AIWorker, EnemyDecision, QwenAIClient, ReportResult
from ai_tracker import build_enemy_request, build_match_request
from game_engine import (
    SUBJECT_LABELS,
    SUBJECT_ORDER,
    ContentBank,
    GameEngine,
    KnowledgePoint,
)
from hand_tracker import GESTURE_GRAB, GESTURE_MOVE, GESTURE_THROW, HandFrame, HandTracker


WIDTH = 1280
HEIGHT = 800
FPS = 60

COLOR_BG = (9, 17, 26)
COLOR_GRID = (20, 37, 52)
COLOR_PANEL = (15, 28, 40)
COLOR_PANEL_ALT = (19, 35, 49)
COLOR_BORDER = (55, 83, 103)
COLOR_TEXT = (230, 241, 245)
COLOR_MUTED = (141, 166, 181)
COLOR_CYAN = (48, 211, 190)
COLOR_BLUE = (78, 160, 255)
COLOR_ORANGE = (247, 163, 74)
COLOR_RED = (236, 91, 91)
COLOR_GREEN = (95, 214, 128)
COLOR_PURPLE = (168, 130, 255)

SUBJECT_COLORS = {
    "calculus": COLOR_CYAN,
    "linear_probability": COLOR_BLUE,
    "physics": COLOR_ORANGE,
}


class AppState(Enum):
    THINKING = auto()
    PLAYING = auto()
    EVALUATING = auto()
    REPORTING = auto()
    REPORT = auto()
    ERROR = auto()


@dataclass(frozen=True)
class Hotspot:
    action: str
    value: str
    rect: pygame.Rect


@dataclass
class ResultOverlay:
    accepted: bool
    score: int
    explanation: str
    feedback: str
    monster_defeated: bool
    shown_at: float


class FontBook:
    def __init__(self) -> None:
        self.path = self._find_font()
        self.cache: dict[tuple[int, bool], pygame.font.Font] = {}

    @staticmethod
    def _find_font() -> str | None:
        candidates = [
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyhbd.ttc"),
            Path("C:/Windows/Fonts/simhei.ttf"),
            Path("C:/Windows/Fonts/NotoSansCJK-Regular.ttc"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    def get(self, size: int, bold: bool = False) -> pygame.font.Font:
        key = (size, bold)
        if key not in self.cache:
            font = pygame.font.Font(self.path, size) if self.path else pygame.font.SysFont("arial", size)
            font.set_bold(bold)
            self.cache[key] = font
        return self.cache[key]


class KnowledgeDefenderApp:
    def __init__(self, args: argparse.Namespace, root: Path) -> None:
        pygame.init()
        pygame.display.set_caption("Knowledge Defender / 学科守卫者")
        flags = pygame.FULLSCREEN if args.fullscreen else 0
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT), flags)
        self.clock = pygame.time.Clock()
        self.root = root
        self.args = args
        self.fonts = FontBook()
        self.background = self._build_background()

        self.bank = ContentBank(root)
        self.engine = GameEngine(
            self.bank,
            random.Random(),
            target_enemies=args.target_enemies,
        )
        self.ai_client = QwenAIClient()
        self.ai_worker = AIWorker(self.ai_client)
        self.ai_worker.start()

        self.hand_tracker: HandTracker | None = None
        self.camera_ready = False
        self.hand_frame = HandFrame(False)
        self.last_hand_gesture = GESTURE_MOVE

        self.state = AppState.THINKING
        self.running = True
        self.cursor = pygame.Vector2(WIDTH * 0.5, HEIGHT * 0.55)
        self.active_book_subject: str | None = None
        self.held_concept_id: str | None = None
        self.held_concept_title = ""
        self.result_overlay: ResultOverlay | None = None
        self.report: ReportResult | None = None
        self.error_message = ""
        self.ai_status = "正在联系 Qwen，设计第一只怪物…"
        self.ai_trace: list[str] = ["AI 初始化：等待学情数据"]
        self.toast = "把手放在摄像头前，握拳抓书，张开手掌投掷"
        self.toast_until = time.monotonic() + 6.0
        self.last_ai_result = ""
        self.smoke_test = args.smoke_test
        self.frame_count = 0
        self.session_key = datetime.now().strftime("%Y%m%d_%H%M%S")

    def run(self) -> int:
        try:
            if self.smoke_test:
                self._render()
                return 0
            if not self.ai_client.configured:
                raise AIError(
                    "未配置 DASHSCOPE_API_KEY。请复制 .env.example 为 .env 并填写 Key，"
                    "或确认用户目录下的 .codex/.env 已配置。"
                )
            self._open_camera()
            self.engine.start(time.monotonic())
            self._request_next_enemy()

            while self.running:
                now = time.monotonic()
                self._handle_events(now)
                self._update(now)
                self._render()
                pygame.display.flip()
                self.clock.tick(FPS)
                self.frame_count += 1
            return 0
        except AIError as exc:
            self.error_message = str(exc)
            print(f"AI 配置或调用错误: {exc}", file=sys.stderr)
            return 2
        except Exception:
            raise
        finally:
            self._shutdown()

    def _open_camera(self) -> None:
        if self.args.no_camera:
            self.camera_ready = False
            self.toast = "键盘/鼠标测试模式：WASD 移动，F 抓取，空格投掷"
            self.toast_until = time.monotonic() + 8.0
            return
        self.hand_tracker = HandTracker(camera_index=self.args.camera)
        self.camera_ready = self.hand_tracker.open()
        if not self.camera_ready:
            self.hand_tracker.close()
            self.hand_tracker = None
            raise AIError(
                f"无法打开摄像头 {self.args.camera}。检查摄像头权限，"
                "或先用 `--no-camera --input mouse` 验证界面。"
            )

    def _shutdown(self) -> None:
        self.ai_worker.stop()
        if self.hand_tracker is not None:
            self.hand_tracker.close()
        pygame.quit()

    def _handle_events(self, now: float) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                self._handle_key(event, now)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    self.cursor.update(event.pos)
                    self._handle_click(event.pos, now)
                elif event.button == 3:
                    self._cancel_interaction()

    def _handle_key(self, event: pygame.event.Event, now: float) -> None:
        if event.key == pygame.K_ESCAPE:
            self.running = False
        elif event.key == pygame.K_F1:
            self.toast = "手势：移动手掌控制光标，握拳抓取，张开手掌投掷"
            self.toast_until = now + 5
        elif event.key == pygame.K_r and self.state == AppState.ERROR:
            self.error_message = ""
            self._retry_after_error()
        elif event.key == pygame.K_f and self.state == AppState.PLAYING:
            self._handle_grab(self.cursor, now)
        elif event.key == pygame.K_SPACE and self.state == AppState.PLAYING:
            self._handle_throw(self.cursor, now)

    def _update(self, now: float) -> None:
        if self.hand_tracker is not None:
            self.hand_frame = self.hand_tracker.read()
            if self.hand_frame.found:
                target = pygame.Vector2(self.hand_frame.x * WIDTH, self.hand_frame.y * HEIGHT)
                self.cursor = self.cursor.lerp(target, 0.45)
                if (
                    self.hand_frame.gesture == GESTURE_GRAB
                    and self.last_hand_gesture != GESTURE_GRAB
                    and self.state == AppState.PLAYING
                ):
                    self._handle_grab(self.cursor, now)
                if (
                    self.hand_frame.gesture == GESTURE_THROW
                    and self.last_hand_gesture != GESTURE_THROW
                    and self.state == AppState.PLAYING
                ):
                    self._handle_throw(self.cursor, now)
                self.last_hand_gesture = self.hand_frame.gesture
            else:
                self.last_hand_gesture = GESTURE_MOVE

        if self.args.no_camera and self.args.input == "keyboard":
            keys = pygame.key.get_pressed()
            speed = 12
            self.cursor.x += (keys[pygame.K_d] - keys[pygame.K_a]) * speed
            self.cursor.y += (keys[pygame.K_s] - keys[pygame.K_w]) * speed
            self.cursor.x = max(0, min(WIDTH - 1, self.cursor.x))
            self.cursor.y = max(0, min(HEIGHT - 1, self.cursor.y))

        self._poll_ai_jobs()
        self._advance_transition(now)

    def _poll_ai_jobs(self) -> None:
        for job in self.ai_worker.poll():
            if job.error:
                self._handle_ai_error(job.name, job.error)
                continue
            if job.name == "enemy":
                self._handle_enemy_decision(job.result)
            elif job.name == "match":
                self._handle_match_result(job.result)
            elif job.name == "report":
                self._handle_report(job.result)

    def _request_next_enemy(self) -> None:
        self.state = AppState.THINKING
        self.ai_status = f"AI 决策中：分析最近表现，设计第 {self.engine.enemy_index} 只怪物…"
        self._push_trace("AI 决策：读取正确率、速度和连击记录")
        payload = build_enemy_request(self.engine)
        self.ai_worker.submit("enemy", self.ai_client.decide_enemy, payload)

    def _handle_enemy_decision(self, decision: EnemyDecision) -> None:
        self.engine.install_monster(decision, time.monotonic())
        self.state = AppState.PLAYING
        self.active_book_subject = None
        self.held_concept_id = None
        self.held_concept_title = ""
        self.ai_status = (
            f"AI 决策：{decision.enemy_name}，"
            f"难度 L{decision.level}，{decision.formula_count} 个公式槽，"
            f"{'提供提示' if decision.hint_enabled else '无提示'}"
        )
        self.last_ai_result = decision.reason
        hint_suffix = f" / AI 提示：{decision.hint_text}" if decision.hint_enabled and decision.hint_text else ""
        self._push_trace(f"AI 生成怪物：{decision.enemy_name} / {decision.reason}{hint_suffix}")
        self.toast = "选择课本，抓取正确知识点，再向怪物投掷"
        self.toast_until = time.monotonic() + 4

    def _request_match(self, concept_id: str) -> None:
        if self.engine.monster is None or self.engine.monster.current_formula_id is None:
            return
        formula_id = self.engine.monster.current_formula_id
        title = self.bank.knowledge[concept_id].title
        self.state = AppState.THINKING
        self.ai_status = f"AI 分析中：“{title}”能否解决当前公式…"
        self._push_trace(f"提交语义判断：{title} → {self.bank.formulas[formula_id].formula}")
        payload = build_match_request(self.engine, formula_id, concept_id)
        self.ai_worker.submit("match", self.ai_client.match_answer, payload)

    def _handle_match_result(self, result: Any) -> None:
        if self.engine.monster is None or self.held_concept_id is None:
            self._handle_ai_error("match", "AI 返回时当前题目已变化")
            return
        selected_concept_id = self.held_concept_id
        now = time.monotonic()
        resolved = self.engine.resolve_answer(selected_concept_id, result, now)
        self.last_ai_result = result.feedback
        self.result_overlay = ResultOverlay(
            accepted=result.accepted,
            score=result.score,
            explanation=result.explanation,
            feedback=result.feedback,
            monster_defeated=bool(resolved["monster_defeated"]),
            shown_at=now,
        )
        verdict = "匹配成功" if result.accepted else "匹配失败"
        self.ai_status = f"AI 判定：{verdict}，关联度 {result.score}%"
        self._push_trace(f"AI 判题：{verdict} / {result.score}% / {result.explanation}")
        if not result.accepted:
            self._push_trace(f"AI 提示：{result.feedback}")
        self.held_concept_id = None
        self.held_concept_title = ""
        self.active_book_subject = None
        self.state = AppState.EVALUATING

    def _request_report(self) -> None:
        self.state = AppState.REPORTING
        self.ai_status = "AI 分析整局记录，生成个性化学习报告…"
        self._push_trace("AI 报告：汇总知识点掌握度、章节与下一步建议")
        self.ai_worker.submit("report", self.ai_client.generate_report, self.engine.report_payload())

    def _handle_report(self, report: ReportResult) -> None:
        self.report = report
        self.state = AppState.REPORT
        self.ai_status = "AI 学习报告已生成"
        self._push_trace(f"AI 报告完成：{report.summary}")
        self._write_report()

    def _handle_ai_error(self, job_name: str, message: str) -> None:
        self.error_message = message
        self.state = AppState.ERROR
        self.ai_status = f"AI 调用失败：{job_name}"
        self._push_trace(f"AI 失败：{message}")

    def _retry_after_error(self) -> None:
        if self.engine.game_over and not self.report:
            self._request_report()
        elif self.engine.monster is None:
            self._request_next_enemy()
        else:
            self.state = AppState.PLAYING
            self.ai_status = "AI 已恢复，请重新投掷"

    def _advance_transition(self, now: float) -> None:
        if self.state != AppState.EVALUATING or self.result_overlay is None:
            return
        if now - self.result_overlay.shown_at < 2.4:
            return
        if self.engine.game_over:
            self.result_overlay = None
            self._request_report()
            return
        if self.engine.monster is not None and self.engine.monster.is_defeated:
            self.engine.advance_enemy()
            self.result_overlay = None
            self._request_next_enemy()
            return
        self.result_overlay = None
        self.state = AppState.PLAYING

    def _handle_grab(self, point: pygame.Vector2 | tuple[int, int], now: float) -> None:
        if self.state != AppState.PLAYING:
            return
        if self._rect_contains(self._monster_rect(), point):
            self._handle_throw(point, now)
            return

        if self.held_concept_id is None:
            for hotspot in self._book_hotspots():
                if hotspot.rect.collidepoint(point):
                    self.active_book_subject = hotspot.value
                    self.toast = f"已展开：{SUBJECT_LABELS[hotspot.value]}"
                    self.toast_until = now + 2
                    return
        else:
            self.held_concept_id = None
            self.held_concept_title = ""
            self.toast = "已取消当前知识点"
            self.toast_until = now + 2
            return

        for hotspot in self._card_hotspots():
            if hotspot.rect.collidepoint(point):
                self.held_concept_id = hotspot.value
                self.held_concept_title = self.bank.knowledge[hotspot.value].title
                self.toast = f"已抓取“{self.held_concept_title}”，移到怪物方向后张开手掌"
                self.toast_until = now + 4
                return

    def _handle_throw(self, point: pygame.Vector2 | tuple[int, int], now: float) -> None:
        if self.state != AppState.PLAYING:
            return
        if self.held_concept_id is None:
            self.toast = "先抓取一个知识点"
            self.toast_until = now + 2
            return
        if not self._rect_contains(self._throw_zone(), point):
            self.toast = "把光标移向怪物，再张开手掌投掷"
            self.toast_until = now + 2
            return
        self._request_match(self.held_concept_id)

    def _handle_click(self, point: tuple[int, int], now: float) -> None:
        if self.state == AppState.ERROR:
            self._retry_after_error()
            return
        if self.state != AppState.PLAYING:
            return
        self._handle_grab(point, now)

    def _cancel_interaction(self) -> None:
        self.active_book_subject = None
        self.held_concept_id = None
        self.held_concept_title = ""
        self.toast = "已取消操作"
        self.toast_until = time.monotonic() + 2

    def _render(self) -> None:
        self.screen.blit(self.background, (0, 0))
        self._draw_top_bar()
        self._draw_monster()
        self._draw_content_area()
        self._draw_sidebar()
        self._draw_cursor()
        self._draw_toast()
        if self.state == AppState.THINKING:
            self._draw_ai_overlay("AI 正在工作", self.ai_status)
        elif self.state == AppState.EVALUATING and self.result_overlay is not None:
            self._draw_result_overlay()
        elif self.state == AppState.REPORTING:
            self._draw_ai_overlay("AI 正在生成学习报告", self.ai_status)
        elif self.state == AppState.REPORT and self.report is not None:
            self._draw_report()
        elif self.state == AppState.ERROR:
            self._draw_error()
        if self.args.no_camera:
            self._draw_keyboard_help()
        else:
            self._draw_camera_overlay()

    def _draw_top_bar(self) -> None:
        pygame.draw.rect(self.screen, COLOR_PANEL, (0, 0, WIDTH, 66))
        pygame.draw.line(self.screen, COLOR_BORDER, (0, 66), (WIDTH, 66), 1)
        self._text("学科守卫者", (22, 16), 27, COLOR_TEXT, bold=True)
        self._text("KNOWLEDGE DEFENDER", (22, 46), 11, COLOR_CYAN)

        defeated = min(self.engine.enemies_defeated, self.engine.win_enemies)
        self._text(f"已击破 {defeated}/{self.engine.win_enemies}", (255, 20), 22, COLOR_TEXT, bold=True)
        self._text(f"怪物 #{self.engine.enemy_index}", (255, 47), 12, COLOR_MUTED)

        self._text(f"防线耐久 {self.engine.lives}/{GameEngine.MAX_LIVES}", (455, 20), 22, COLOR_TEXT, bold=True)
        hearts_x = 455
        for index in range(GameEngine.MAX_LIVES):
            color = COLOR_RED if index < self.engine.lives else (55, 69, 80)
            pygame.draw.circle(self.screen, color, (hearts_x + index * 24 + 10, 55), 7)

        current = self.engine.monster.current_formula_id if self.engine.monster else None
        subject = self.bank.formulas[current].subject if current else None
        subject_text = SUBJECT_LABELS.get(subject, "等待 AI") if subject else "等待 AI"
        self._text(f"当前学科：{subject_text}", (690, 20), 20, COLOR_TEXT, bold=True)
        self._text(f"难度 L{self.engine.current_level}", (690, 47), 12, COLOR_MUTED)

        pending = self.ai_worker.pending_count
        status_color = COLOR_ORANGE if pending else COLOR_GREEN
        pygame.draw.circle(self.screen, status_color, (930, 32), 6)
        self._text("AI 工作中" if pending else "AI 在线", (944, 19), 18, status_color, bold=True)
        self._text(self.ai_status[:25], (944, 43), 11, COLOR_MUTED)

    def _draw_monster(self) -> None:
        grid_y = 105
        grid_width = 930
        pygame.draw.line(self.screen, COLOR_GRID, (35, grid_y + 55), (grid_width, grid_y + 55), 2)
        for index in range(GameEngine.MAX_LIVES + 1):
            x = 85 + index * 135
            color = COLOR_BORDER if index else COLOR_RED
            pygame.draw.line(self.screen, color, (x, grid_y + 45), (x, grid_y + 65), 2)

        if self.engine.monster is None:
            self._text("AI 正在构筑下一位对手…", (350, 230), 28, COLOR_MUTED, bold=True)
            return

        progress = GameEngine.MAX_LIVES - self.engine.lives
        monster_x = 690 - progress * 93
        monster_y = 190 + progress * 28
        now = time.monotonic()
        pulse = int(5 * (1 + math.sin(now * 4)))
        shadow = pygame.Rect(monster_x - 100, monster_y + 95, 200, 32)
        pygame.draw.ellipse(self.screen, (5, 10, 15), shadow)

        body_color = (
            (132, 75, 183) if self.state != AppState.EVALUATING
            else (COLOR_GREEN if self.result_overlay and self.result_overlay.accepted else COLOR_RED)
        )
        body = pygame.Rect(monster_x - 88 - pulse, monster_y - 72 - pulse, 176 + pulse * 2, 142 + pulse * 2)
        pygame.draw.ellipse(self.screen, body_color, body)
        pygame.draw.ellipse(self.screen, (218, 193, 255), (monster_x - 64, monster_y - 34, 38, 28))
        pygame.draw.ellipse(self.screen, (218, 193, 255), (monster_x + 26, monster_y - 34, 38, 28))
        pygame.draw.circle(self.screen, (21, 22, 35), (monster_x - 45, monster_y - 20), 9)
        pygame.draw.circle(self.screen, (21, 22, 35), (monster_x + 45, monster_y - 20), 9)
        pygame.draw.arc(
            self.screen,
            (43, 24, 50),
            (monster_x - 45, monster_y + 8, 90, 48),
            0.15,
            3.0,
            5,
        )

        formula = self.bank.formulas[self.engine.monster.current_formula_id] if self.engine.monster.current_formula_id else None
        if formula is None:
            return
        panel = pygame.Rect(monster_x - 230, 115, 460, 74)
        self._panel(panel, fill=(25, 28, 45), border=SUBJECT_COLORS.get(formula.subject, COLOR_PURPLE), radius=8, border_width=2)
        self._text(formula.formula, (panel.x + 20, panel.y + 13), 31, COLOR_TEXT, bold=True)
        self._text(formula.skill_label, (panel.x + 20, panel.y + 50), 13, COLOR_MUTED)

        slot_y = 285
        for index, formula_id in enumerate(self.engine.monster.formula_ids):
            item = self.bank.formulas[formula_id]
            x = monster_x - (len(self.engine.monster.formula_ids) - 1) * 70 / 2 + index * 70
            pygame.draw.rect(self.screen, SUBJECT_COLORS.get(item.subject, COLOR_PURPLE), (x - 27, slot_y, 54, 18), border_radius=5)
            self._text(f"{index + 1}", (x - 4, slot_y - 2), 13, COLOR_BG, bold=True)

    def _draw_content_area(self) -> None:
        if self.engine.monster is None or self.state in (AppState.REPORT, AppState.REPORTING):
            return
        self._draw_books()
        if self.active_book_subject:
            self._draw_cards(self.active_book_subject)
        elif self.held_concept_id:
            panel = pygame.Rect(48, 430, 900, 100)
            self._panel(panel, fill=(18, 34, 47), border=COLOR_CYAN, radius=8)
            self._text("已抓取知识点", (70, 450), 14, COLOR_CYAN, bold=True)
            self._text(self.held_concept_title, (70, 476), 28, COLOR_TEXT, bold=True)
            self._text("把光标移到怪物区域并张开手掌，或用空格投掷", (70, 508), 15, COLOR_MUTED)

    def _draw_books(self) -> None:
        for index, subject in enumerate(SUBJECT_ORDER):
            rect = self._book_rect(index)
            color = SUBJECT_COLORS[subject]
            selected = self.active_book_subject == subject
            self._panel(
                rect,
                fill=(24, 42, 55) if selected else (17, 31, 43),
                border=color,
                radius=7,
                border_width=3 if selected else 1,
            )
            spine = pygame.Rect(rect.x, rect.y, 18, rect.height)
            pygame.draw.rect(self.screen, color, spine, border_top_left_radius=7, border_bottom_left_radius=7)
            pygame.draw.line(self.screen, (240, 245, 245), (rect.x + 42, rect.y + 18), (rect.right - 22, rect.y + 18), 2)
            pygame.draw.line(self.screen, (168, 192, 200), (rect.x + 42, rect.y + 31), (rect.right - 55, rect.y + 31), 1)
            self._text(SUBJECT_LABELS[subject], (rect.x + 38, rect.y + 49), 23, COLOR_TEXT, bold=True)
            self._text("握拳展开", (rect.x + 38, rect.y + 80), 12, COLOR_MUTED)

    def _draw_cards(self, subject: str) -> None:
        cards = self._cards_for_subject(subject)
        panel = pygame.Rect(48, 405, 900, 150)
        self._panel(panel, fill=(13, 26, 37), border=SUBJECT_COLORS[subject], radius=8)
        self._text(f"{SUBJECT_LABELS[subject]}知识点", (66, 418), 15, SUBJECT_COLORS[subject], bold=True)
        rects = self._card_rects(cards)
        for point, rect in zip(cards, rects):
            hovered = rect.collidepoint(self.cursor)
            fill = (31, 55, 69) if hovered else COLOR_PANEL_ALT
            self._panel(rect, fill=fill, border=SUBJECT_COLORS[subject] if hovered else COLOR_BORDER, radius=6)
            self._text(point.title, (rect.x + 12, rect.y + 9), 16, COLOR_TEXT, bold=True)
            self._wrapped_text(point.description, (rect.x + 12, rect.y + 34), rect.width - 24, 12, COLOR_MUTED, 2)

    def _draw_sidebar(self) -> None:
        rect = pygame.Rect(990, 78, 270, 704)
        self._panel(rect, fill=(12, 24, 35), border=COLOR_BORDER, radius=8)
        self._text("AI 学情面板", (1010, 94), 20, COLOR_TEXT, bold=True)
        self._text("实时追踪", (1010, 120), 11, COLOR_CYAN)

        metrics = self.engine.metrics
        cards = [
            ("正确率", f"{metrics.accuracy:.1f}%", COLOR_GREEN),
            ("平均反应", f"{metrics.average_response:.2f}s", COLOR_BLUE),
            ("错误次数", str(metrics.wrong), COLOR_RED),
            ("当前连击", str(metrics.streak), COLOR_ORANGE),
        ]
        for index, (label, value, color) in enumerate(cards):
            x = 1008 + (index % 2) * 126
            y = 144 + (index // 2) * 72
            box = pygame.Rect(x, y, 114, 58)
            self._panel(box, fill=COLOR_PANEL_ALT, border=(41, 60, 74), radius=6)
            self._text(label, (x + 10, y + 8), 11, COLOR_MUTED)
            self._text(value, (x + 10, y + 27), 21, color, bold=True)

        weak = self._weak_concepts(3)
        self._text("薄弱知识点", (1010, 300), 14, COLOR_TEXT, bold=True)
        if weak:
            for index, item in enumerate(weak):
                self._text(f"{index + 1}. {item}", (1012, 326 + index * 24), 13, COLOR_MUTED)
        else:
            self._text("AI 等待答题数据…", (1012, 326), 13, COLOR_MUTED)

        self._text("AI 当前决策", (1010, 415), 14, COLOR_TEXT, bold=True)
        self._wrapped_text(self.ai_status, (1010, 441), 228, 13, COLOR_CYAN, 4)
        if self.engine.monster and self.engine.monster.decision.hint_enabled:
            hint = self.engine.monster.decision.hint_text or "先判断公式需要完成哪类运算，再选择知识点。"
            self._text("本怪提示", (1010, 520), 12, COLOR_ORANGE, bold=True)
            self._wrapped_text(hint, (1010, 540), 228, 11, COLOR_MUTED, 2)
        if self.last_ai_result:
            result_y = 590 if self.engine.monster and self.engine.monster.decision.hint_enabled else 535
            self._text("最近结论", (1010, result_y), 14, COLOR_TEXT, bold=True)
            self._wrapped_text(self.last_ai_result, (1010, result_y + 26), 228, 12, COLOR_MUTED, 3)

        trace_y = 665 if self.engine.monster and self.engine.monster.decision.hint_enabled else 640
        self._text("AI 推理轨迹", (1010, trace_y), 14, COLOR_TEXT, bold=True)
        for index, line in enumerate(self.ai_trace[-2:]):
            self._wrapped_text(f"• {line}", (1010, trace_y + 26 + index * 36), 228, 10, (115, 143, 158), 2)

    def _draw_cursor(self) -> None:
        x, y = int(self.cursor.x), int(self.cursor.y)
        if self.held_concept_id:
            color = COLOR_ORANGE
        elif self.hand_frame.gesture == GESTURE_GRAB:
            color = COLOR_CYAN
        else:
            color = COLOR_TEXT
        pygame.draw.circle(self.screen, (7, 15, 23), (x, y), 15)
        pygame.draw.circle(self.screen, color, (x, y), 12, 3)
        pygame.draw.circle(self.screen, color, (x, y), 3)
        if self.held_concept_id:
            pygame.draw.line(self.screen, color, (x + 14, y - 14), (x + 30, y - 30), 3)

    def _draw_camera_overlay(self) -> None:
        rect = pygame.Rect(18, 82, 250, 190)
        self._panel(rect, fill=(7, 16, 24), border=COLOR_BORDER, radius=7)
        if self.hand_frame.frame_bgr is not None:
            try:
                rgb = HandTracker.draw_camera_overlay(
                    self.hand_frame.frame_bgr,
                    self.hand_frame.landmarks,
                    rect.width - 6,
                    rect.height - 34,
                )
                surface = pygame.image.frombuffer(rgb.tobytes(), (rgb.shape[1], rgb.shape[0]), "RGB")
                self.screen.blit(surface, (rect.x + 3, rect.y + 3))
            except (ValueError, pygame.error):
                self._text("摄像头帧读取中…", (rect.x + 58, rect.y + 76), 14, COLOR_MUTED)
        else:
            self._text("等待摄像头帧…", (rect.x + 58, rect.y + 76), 14, COLOR_MUTED)
        status = "检测到手" if self.hand_frame.found else "未检测到手"
        self._text(status, (rect.x + 10, rect.bottom - 25), 13, COLOR_GREEN if self.hand_frame.found else COLOR_ORANGE, bold=True)
        gesture_label = {
            GESTURE_MOVE: "移动",
            GESTURE_GRAB: "握拳",
            GESTURE_THROW: "张开",
        }.get(self.hand_frame.gesture, "移动")
        self._text(f"动作：{gesture_label}", (rect.right - 92, rect.bottom - 25), 13, COLOR_TEXT)

    def _draw_keyboard_help(self) -> None:
        rect = pygame.Rect(18, 82, 250, 190)
        self._panel(rect, fill=(7, 16, 24), border=COLOR_BORDER, radius=7)
        self._text("测试模式", (rect.x + 16, rect.y + 16), 18, COLOR_ORANGE, bold=True)
        lines = ["鼠标点击：抓取/选择", "WASD：移动光标", "F：抓取或取消", "空格：向怪物投掷", "鼠标右键：取消"]
        for index, line in enumerate(lines):
            self._text(line, (rect.x + 16, rect.y + 52 + index * 25), 14, COLOR_MUTED)

    def _draw_toast(self) -> None:
        now = time.monotonic()
        if now > self.toast_until:
            return
        font = self.fonts.get(16, bold=True)
        width = min(870, font.size(self.toast)[0] + 42)
        rect = pygame.Rect(48, 745, width, 38)
        self._panel(rect, fill=(21, 42, 54), border=COLOR_CYAN, radius=7)
        self._text(self.toast, (rect.x + 18, rect.y + 9), 16, COLOR_TEXT, bold=True)

    def _draw_ai_overlay(self, title: str, text: str) -> None:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((3, 8, 13, 155))
        self.screen.blit(overlay, (0, 0))
        rect = pygame.Rect(300, 275, 600, 230)
        self._panel(rect, fill=(13, 27, 39), border=COLOR_CYAN, radius=10, border_width=2)
        self._text(title, (rect.x + 35, rect.y + 27), 29, COLOR_TEXT, bold=True)
        self._text("QWEN REASONING PIPELINE", (rect.x + 35, rect.y + 65), 12, COLOR_CYAN)
        self._wrapped_text(text, (rect.x + 35, rect.y + 104), 525, 18, COLOR_MUTED, 3)
        progress = (self.frame_count % 60) / 60
        pygame.draw.rect(self.screen, (35, 57, 70), (rect.x + 35, rect.bottom - 38, 530, 6), border_radius=3)
        pygame.draw.rect(
            self.screen,
            COLOR_CYAN,
            (rect.x + 35, rect.bottom - 38, int(530 * progress), 6),
            border_radius=3,
        )

    def _draw_result_overlay(self) -> None:
        assert self.result_overlay is not None
        result = self.result_overlay
        color = COLOR_GREEN if result.accepted else COLOR_RED
        rect = pygame.Rect(135, 330, 850, 245)
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((3, 8, 13, 90))
        self.screen.blit(overlay, (0, 0))
        self._panel(rect, fill=(13, 27, 39), border=color, radius=10, border_width=3)
        title = "AI 判定：匹配成功" if result.accepted else "AI 判定：关联不足"
        self._text(title, (rect.x + 32, rect.y + 24), 29, color, bold=True)
        self._text(f"语义关联度 {result.score}%", (rect.right - 230, rect.y + 29), 20, COLOR_TEXT, bold=True)
        self._text("AI 解释", (rect.x + 34, rect.y + 82), 14, COLOR_CYAN, bold=True)
        self._wrapped_text(result.explanation, (rect.x + 34, rect.y + 110), 760, 18, COLOR_TEXT, 2)
        self._text("AI 反馈", (rect.x + 34, rect.y + 165), 14, COLOR_CYAN, bold=True)
        self._wrapped_text(result.feedback, (rect.x + 34, rect.y + 191), 760, 16, COLOR_MUTED, 2)

    def _draw_report(self) -> None:
        assert self.report is not None
        panel = pygame.Rect(42, 85, 1200, 680)
        self._panel(panel, fill=(12, 24, 35), border=COLOR_CYAN, radius=10, border_width=2)
        title = f"胜利：防线守住了 {self.engine.win_enemies} 只怪物" if self.engine.won else "挑战结束：怪物突破了防线"
        self._text(title, (75, 112), 32, COLOR_GREEN if self.engine.won else COLOR_RED, bold=True)
        self._text(
            f"击破 {self.engine.enemies_defeated}/{self.engine.win_enemies}  |  正确率 {self.engine.metrics.accuracy:.1f}%  "
            f"|  平均反应 {self.engine.metrics.average_response:.2f}s  |  错误 {self.engine.metrics.wrong}",
            (75, 158),
            17,
            COLOR_MUTED,
        )
        self._text("AI 总结", (75, 210), 17, COLOR_CYAN, bold=True)
        self._wrapped_text(self.report.summary, (75, 238), 1125, 18, COLOR_TEXT, 3)

        columns = [
            ("AI 识别优势", self.report.strengths or ["暂无明显优势"], COLOR_GREEN),
            ("AI 识别薄弱项", self.report.weaknesses or ["暂无数据"], COLOR_ORANGE),
            ("建议复习章节", self.report.review_chapters or ["根据课堂进度复习"], COLOR_BLUE),
        ]
        for index, (heading, items, color) in enumerate(columns):
            x = 75 + index * 376
            box = pygame.Rect(x, 330, 350, 180)
            self._panel(box, fill=COLOR_PANEL_ALT, border=color, radius=7)
            self._text(heading, (x + 18, 348), 17, color, bold=True)
            for item_index, item in enumerate(items[:5]):
                self._wrapped_text(f"• {item}", (x + 18, 382 + item_index * 25), 310, 13, COLOR_MUTED, 1)

        self._text("AI 下一步学习建议", (75, 540), 17, COLOR_CYAN, bold=True)
        for index, item in enumerate(self.report.next_steps[:5]):
            self._wrapped_text(f"{index + 1}. {item}", (78, 570 + index * 30), 1120, 15, COLOR_TEXT, 1)
        self._text("按 Esc 退出，报告同时保存在 reports 目录", (75, 730), 13, COLOR_MUTED)

    def _draw_error(self) -> None:
        overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        overlay.fill((3, 8, 13, 210))
        self.screen.blit(overlay, (0, 0))
        rect = pygame.Rect(220, 245, 840, 300)
        self._panel(rect, fill=(31, 19, 25), border=COLOR_RED, radius=10, border_width=3)
        self._text("AI 调用失败，游戏已暂停", (rect.x + 34, rect.y + 28), 28, COLOR_RED, bold=True)
        self._wrapped_text(self.error_message, (rect.x + 34, rect.y + 82), 760, 16, COLOR_TEXT, 5)
        self._text("不会使用随机数或固定模板伪装 AI 结果。", (rect.x + 34, rect.y + 198), 15, COLOR_ORANGE, bold=True)
        self._text("修复网络或 API 配置后按 R 重试，按 Esc 退出。", (rect.x + 34, rect.y + 232), 15, COLOR_MUTED)

    def _panel(
        self,
        rect: pygame.Rect,
        fill: tuple[int, int, int],
        border: tuple[int, int, int],
        radius: int = 8,
        border_width: int = 1,
    ) -> None:
        pygame.draw.rect(self.screen, fill, rect, border_radius=radius)
        pygame.draw.rect(self.screen, border, rect, width=border_width, border_radius=radius)

    def _text(
        self,
        text: str,
        position: tuple[int, int],
        size: int,
        color: tuple[int, int, int],
        bold: bool = False,
    ) -> None:
        surface = self.fonts.get(size, bold).render(text, True, color)
        self.screen.blit(surface, position)

    def _wrapped_text(
        self,
        text: str,
        position: tuple[int, int],
        max_width: int,
        size: int,
        color: tuple[int, int, int],
        max_lines: int = 3,
        bold: bool = False,
    ) -> None:
        font = self.fonts.get(size, bold)
        lines: list[str] = []
        current = ""
        for char in text:
            if char == "\n":
                lines.append(current)
                current = ""
                continue
            trial = current + char
            if font.size(trial)[0] <= max_width:
                current = trial
            else:
                lines.append(current)
                current = char
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        for index, line in enumerate(lines[:max_lines]):
            rendered = font.render(line, True, color)
            self.screen.blit(rendered, (position[0], position[1] + index * (size + 5)))

    def _build_background(self) -> pygame.Surface:
        surface = pygame.Surface((WIDTH, HEIGHT))
        surface.fill(COLOR_BG)
        for x in range(0, WIDTH, 40):
            pygame.draw.line(surface, COLOR_GRID, (x, 66), (x, HEIGHT), 1)
        for y in range(66, HEIGHT, 40):
            pygame.draw.line(surface, COLOR_GRID, (0, y), (WIDTH, y), 1)
        return surface

    def _book_rect(self, index: int) -> pygame.Rect:
        return pygame.Rect(58 + index * 305, 632, 270, 102)

    def _book_hotspots(self) -> list[Hotspot]:
        return [
            Hotspot("open_book", subject, self._book_rect(index))
            for index, subject in enumerate(SUBJECT_ORDER)
        ]

    def _cards_for_subject(self, subject: str) -> list[KnowledgePoint]:
        if self.engine.monster is None or self.engine.monster.current_formula_id is None:
            return []
        formula = self.bank.formulas[self.engine.monster.current_formula_id]
        if subject == formula.subject:
            candidate_ids = [item["id"] for item in self.engine.candidate_concepts(formula.id)]
            return [self.bank.knowledge[item_id] for item_id in candidate_ids]
        return self.bank.knowledge_for_subject(subject)[:8]

    def _card_rects(self, cards: list[KnowledgePoint]) -> list[pygame.Rect]:
        rects: list[pygame.Rect] = []
        columns = 4
        card_width = 210
        card_height = 44
        gap_x = 10
        gap_y = 6
        for index, _ in enumerate(cards):
            row = index // columns
            column = index % columns
            rects.append(
                pygame.Rect(
                    62 + column * (card_width + gap_x),
                    450 + row * (card_height + gap_y),
                    card_width,
                    card_height,
                )
            )
        return rects

    def _card_hotspots(self) -> list[Hotspot]:
        if not self.active_book_subject:
            return []
        cards = self._cards_for_subject(self.active_book_subject)
        return [
            Hotspot("select_card", item.id, rect)
            for item, rect in zip(cards, self._card_rects(cards))
        ]

    @staticmethod
    def _rect_contains(rect: pygame.Rect, point: pygame.Vector2 | tuple[int, int]) -> bool:
        return rect.collidepoint(float(point[0]), float(point[1]))

    @staticmethod
    def _monster_rect() -> pygame.Rect:
        return pygame.Rect(330, 105, 620, 310)

    @staticmethod
    def _throw_zone() -> pygame.Rect:
        return pygame.Rect(270, 80, 700, 385)

    def _push_trace(self, message: str) -> None:
        self.ai_trace.append(message)
        self.ai_trace = self.ai_trace[-30:]

    def _weak_concepts(self, count: int) -> list[str]:
        records: list[tuple[float, str]] = []
        for concept_id, stat in self.engine.concept_stats.items():
            if stat.attempts:
                records.append((stat.correct / stat.attempts, self.bank.knowledge[concept_id].title))
        records.sort(key=lambda item: item[0])
        return [title for _, title in records[:count]]

    def _write_report(self) -> None:
        if self.report is None:
            return
        report_dir = self.root / "reports"
        report_dir.mkdir(exist_ok=True)
        path = report_dir / f"session_{self.session_key}.json"
        data = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "won": self.engine.won,
            "metrics": self.engine.ai_metrics(),
            "ai_report": {
                "summary": self.report.summary,
                "strengths": self.report.strengths,
                "weaknesses": self.report.weaknesses,
                "review_chapters": self.report.review_chapters,
                "next_steps": self.report.next_steps,
            },
            "attempts": self.engine.report_payload()["attempts"],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._push_trace(f"报告已保存：{path.name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Knowledge Defender / 学科守卫者")
    parser.add_argument("--camera", type=int, default=0, help="摄像头编号，默认 0")
    parser.add_argument("--no-camera", action="store_true", help="禁用摄像头，使用鼠标或键盘测试")
    parser.add_argument("--input", choices=("mouse", "keyboard"), default="mouse", help="无摄像头时的光标输入方式")
    parser.add_argument("--fullscreen", action="store_true", help="全屏运行")
    parser.add_argument("--smoke-test", action="store_true", help="初始化界面后立即退出，用于 CI 检查")
    parser.add_argument(
        "--target-enemies",
        type=int,
        default=GameEngine.WIN_ENEMIES,
        help="获胜需要击破的怪物数，正式规则为 30；短视频演示可使用 3",
    )
    return parser.parse_args()


def main() -> int:
    root = Path(__file__).resolve().parent
    args = parse_args()
    app = KnowledgeDefenderApp(args, root)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
