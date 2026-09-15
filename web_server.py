"""HTTP server for the browser version of Knowledge Defender.

The browser owns camera access and gesture recognition. This server owns the
game session and all Qwen calls, so the API key never reaches the client.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import random
import secrets
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from ai_matcher import AIError, MatchResult, QwenAIClient
from ai_tracker import build_enemy_request, build_match_request
from game_engine import SUBJECT_LABELS, SUBJECT_ORDER, ContentBank, GameEngine


MAX_BODY_BYTES = 512 * 1024
SESSION_TTL_SECONDS = 3 * 60 * 60
AI_WINDOW_SECONDS = 10 * 60
AI_REQUESTS_PER_WINDOW = 120


@dataclass
class WebSession:
    session_id: str
    engine: GameEngine
    created_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cache_lock: threading.Lock = field(default_factory=threading.Lock)
    prepared_matches: dict[tuple[str, str], MatchResult] = field(default_factory=dict)
    preparing_formulas: set[str] = field(default_factory=set)
    avoid_formula_ids: tuple[str, ...] = ()

    def touch(self) -> None:
        self.last_seen_at = time.monotonic()


class SessionStore:
    def __init__(self, bank: ContentBank, target_enemies: int) -> None:
        self.bank = bank
        self.target_enemies = target_enemies
        self._sessions: dict[str, WebSession] = {}
        self._recent_formula_ids: deque[str] = deque(maxlen=8)
        self._lock = threading.Lock()

    def create(self) -> WebSession:
        self.cleanup()
        session_id = uuid.uuid4().hex
        engine = GameEngine(self.bank, target_enemies=self.target_enemies)
        engine.start(time.monotonic())
        with self._lock:
            avoid_formula_ids = tuple(self._recent_formula_ids)
        session = WebSession(
            session_id=session_id,
            engine=engine,
            avoid_formula_ids=avoid_formula_ids,
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> WebSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    def cleanup(self) -> None:
        cutoff = time.monotonic() - SESSION_TTL_SECONDS
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if session.last_seen_at < cutoff
            ]
            for session_id in expired:
                self._sessions.pop(session_id, None)

    def remember_formula_ids(self, formula_ids: list[str]) -> None:
        with self._lock:
            self._recent_formula_ids.extend(formula_ids)


class RateLimiter:
    def __init__(self, limit: int = AI_REQUESTS_PER_WINDOW) -> None:
        self.limit = limit
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_key: str) -> bool:
        now = time.monotonic()
        cutoff = now - AI_WINDOW_SECONDS
        with self._lock:
            events = [item for item in self._events.get(client_key, []) if item >= cutoff]
            if len(events) >= self.limit:
                self._events[client_key] = events
                return False
            events.append(now)
            self._events[client_key] = events
        return True


class KnowledgeDefenderWebServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        root: Path,
        access_token: str,
        target_enemies: int,
    ) -> None:
        self.root = root
        self.web_root = root / "web"
        self.bank = ContentBank(root)
        self.ai_client = QwenAIClient()
        self.sessions = SessionStore(self.bank, target_enemies)
        self.rate_limiter = RateLimiter()
        self.access_token = access_token
        super().__init__(server_address, RequestHandler)


class RequestHandler(BaseHTTPRequestHandler):
    server: KnowledgeDefenderWebServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json_response(
                {
                    "ok": True,
                    "ai_configured": self.server.ai_client.configured,
                    "model": self.server.ai_client.model,
                }
            )
            return
        if parsed.path.startswith("/api/"):
            self._json_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if not self._authorized():
            self._json_error(HTTPStatus.UNAUTHORIZED, "访问令牌无效或已过期")
            return
        if parsed.path not in ("/api/session",) and not self.server.rate_limiter.allow(self.client_address[0]):
            self._json_error(HTTPStatus.TOO_MANY_REQUESTS, "本机请求过于频繁，请稍后再试")
            return
        try:
            payload = self._read_json_body()
            if parsed.path == "/api/session":
                self._create_session()
            elif parsed.path == "/api/decide":
                self._decide_enemy(payload)
            elif parsed.path == "/api/match":
                self._match_answer(payload)
            elif parsed.path == "/api/report":
                self._generate_report(payload)
            else:
                self._json_error(HTTPStatus.NOT_FOUND, "接口不存在")
        except AIError as exc:
            self._json_error(HTTPStatus.BAD_GATEWAY, str(exc))
        except (ValueError, KeyError, TypeError) as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            traceback.print_exc()
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Demo-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _create_session(self) -> None:
        session = self.server.sessions.create()
        self._json_response(self._session_payload(session))

    def _decide_enemy(self, payload: dict[str, Any]) -> None:
        session = self._get_session(payload)
        with session.lock:
            engine = session.engine
            if engine.game_over:
                raise ValueError("本局已经结束，请生成学习报告")
            if engine.monster is not None:
                self._json_response(self._enemy_payload(session))
                return
            request_payload = build_enemy_request(engine)
            avoid = set(session.avoid_formula_ids)
            if avoid:
                filtered = [
                    item for item in request_payload["available_formulas"]
                    if item["id"] not in avoid
                ]
                if len(filtered) >= 4:
                    request_payload["available_formulas"] = filtered
            request_payload["avoid_formula_ids"] = list(session.avoid_formula_ids)
            random.SystemRandom().shuffle(request_payload["available_formulas"])
            decision = self.server.ai_client.decide_enemy(request_payload)
            engine.install_monster(decision, time.monotonic())
            self.server.sessions.remember_formula_ids(decision.formula_ids)
            self._start_match_precompute(session)
            self._json_response(self._enemy_payload(session))

    def _match_answer(self, payload: dict[str, Any]) -> None:
        session = self._get_session(payload)
        formula_id = str(payload["formulaId"])
        concept_id = str(payload["conceptId"])
        with session.lock:
            engine = session.engine
            if engine.monster is None or engine.monster.current_formula_id != formula_id:
                raise ValueError("当前公式已经变化，请重新选择")
            if concept_id not in self.server.bank.knowledge:
                raise ValueError("知识点 ID 不存在")
            with session.cache_lock:
                result = session.prepared_matches.get((formula_id, concept_id))
            if result is None:
                result = self.server.ai_client.match_answer(build_match_request(engine, formula_id, concept_id))
            resolved = engine.resolve_answer(concept_id, result, time.monotonic())
            if resolved["monster_defeated"] and not engine.game_over:
                engine.advance_enemy()
            elif not resolved["monster_defeated"]:
                self._start_match_precompute(session)
            self._json_response(
                {
                    "match": {
                        "score": result.score,
                        "accepted": result.accepted,
                        "explanation": result.explanation,
                        "feedback": result.feedback,
                        "bestConceptId": result.best_concept_id,
                    },
                    "resolved": {
                        "formulaId": resolved["formula_id"],
                        "formula": resolved["formula"],
                        "monsterDefeated": resolved["monster_defeated"],
                    },
                    "state": self._state_payload(session),
                }
            )

    def _start_match_precompute(self, session: WebSession) -> None:
        formula_id = session.engine.monster.current_formula_id if session.engine.monster else None
        if not formula_id:
            return
        candidates = session.engine.candidate_concepts(formula_id)
        with session.cache_lock:
            if formula_id in session.preparing_formulas:
                return
            if all((formula_id, item["id"]) in session.prepared_matches for item in candidates):
                return
            session.preparing_formulas.add(formula_id)
        formula_payload = self.server.bank.formula_public_payload(formula_id)

        def prepare() -> None:
            try:
                prepared = self.server.ai_client.prepare_match_matrix(
                    {
                        "formula": formula_payload,
                        "candidate_concepts": candidates,
                    }
                )
                with session.cache_lock:
                    for concept_id, result in prepared.results.items():
                        session.prepared_matches[(formula_id, concept_id)] = result
            except Exception as exc:
                print(f"[precompute] {formula_id}: {type(exc).__name__}: {exc}")
            finally:
                with session.cache_lock:
                    session.preparing_formulas.discard(formula_id)

        threading.Thread(target=prepare, name=f"prepare-{formula_id}", daemon=True).start()

    def _generate_report(self, payload: dict[str, Any]) -> None:
        session = self._get_session(payload)
        with session.lock:
            if not session.engine.game_over:
                raise ValueError("本局尚未结束")
            report = self.server.ai_client.generate_report(session.engine.report_payload())
            self._json_response(
                {
                    "report": {
                        "summary": report.summary,
                        "strengths": report.strengths,
                        "weaknesses": report.weaknesses,
                        "reviewChapters": report.review_chapters,
                        "nextSteps": report.next_steps,
                    },
                    "state": self._state_payload(session),
                }
            )

    def _session_payload(self, session: WebSession) -> dict[str, Any]:
        engine = session.engine
        return {
            "sessionId": session.session_id,
            "title": "学科守卫者",
            "model": self.server.ai_client.model,
            "targetEnemies": engine.win_enemies,
            "maxLives": GameEngine.MAX_LIVES,
            "subjects": [
                {"id": subject, "label": SUBJECT_LABELS[subject]}
                for subject in SUBJECT_ORDER
            ],
            "knowledgeBySubject": self.server.bank.knowledge_payload_by_subject(),
            "state": self._state_payload(session),
        }

    def _enemy_payload(self, session: WebSession) -> dict[str, Any]:
        engine = session.engine
        if engine.monster is None:
            raise ValueError("当前没有怪物")
        formulas = []
        for formula_id in engine.monster.formula_ids:
            formula = self.server.bank.formula_public_payload(formula_id)
            formula["candidates"] = engine.candidate_concepts(formula_id)
            formulas.append(formula)
        decision = engine.monster.decision
        return {
            "decision": {
                "level": decision.level,
                "formulaCount": decision.formula_count,
                "hintEnabled": decision.hint_enabled,
                "hintText": decision.hint_text,
                "enemyName": decision.enemy_name,
                "reason": decision.reason,
            },
            "monster": {
                "formulas": formulas,
                "currentFormulaId": engine.monster.current_formula_id,
            },
            "state": self._state_payload(session),
        }

    def _state_payload(self, session: WebSession) -> dict[str, Any]:
        engine = session.engine
        return {
            "gameOver": engine.game_over,
            "won": engine.won,
            "enemiesDefeated": engine.enemies_defeated,
            "targetEnemies": engine.win_enemies,
            "lives": engine.lives,
            "maxLives": GameEngine.MAX_LIVES,
            "level": engine.current_level,
            "enemyIndex": engine.enemy_index,
            "metrics": engine.metrics.as_dict(),
            "conceptStats": {
                concept_id: {
                    **stat.as_dict(),
                    "title": self.server.bank.knowledge[concept_id].title,
                }
                for concept_id, stat in engine.concept_stats.items()
            },
        }

    def _get_session(self, payload: dict[str, Any]) -> WebSession:
        session_id = str(payload["sessionId"])
        session = self.server.sessions.get(session_id)
        if session is None:
            raise ValueError("游戏会话不存在或已过期，请刷新页面")
        return session

    def _authorized(self) -> bool:
        expected = self.server.access_token
        if not expected:
            return True
        provided = self.headers.get("X-Demo-Token", "")
        return secrets.compare_digest(provided, expected)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > MAX_BODY_BYTES:
            raise ValueError("请求数据过大")
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求 JSON 顶层必须是对象")
        return payload

    def _serve_static(self, raw_path: str) -> None:
        relative = "index.html" if raw_path in ("", "/") else raw_path.lstrip("/")
        candidate = (self.server.web_root / relative).resolve()
        web_root = self.server.web_root.resolve()
        if candidate != web_root and web_root not in candidate.parents:
            self._json_error(HTTPStatus.FORBIDDEN, "禁止访问")
            return
        if not candidate.is_file():
            candidate = web_root / "index.html"
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._json_response({"error": message}, status)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} {format_string % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Knowledge Defender Web Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--access-token", default=os.getenv("WEB_ACCESS_TOKEN", ""))
    parser.add_argument(
        "--target-enemies",
        type=int,
        default=int(os.getenv("WEB_TARGET_ENEMIES", "30")),
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    load_dotenv(os.path.expanduser("~/.codex/.env"), override=False)
    args = parse_args()
    root = Path(__file__).resolve().parent
    access_token = args.access_token
    if args.host not in ("127.0.0.1", "localhost") and not access_token:
        access_token = secrets.token_urlsafe(18)
    server = KnowledgeDefenderWebServer(
        (args.host, args.port),
        root=root,
        access_token=access_token,
        target_enemies=max(1, args.target_enemies),
    )
    host, port = server.server_address
    query = f"?token={access_token}" if access_token else ""
    print(f"Knowledge Defender web server: http://{host}:{port}/{query}")
    print(f"Qwen configured: {server.ai_client.configured}")
    if not server.ai_client.configured:
        print("警告：缺少 DASHSCOPE_API_KEY，AI 接口会返回配置错误。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
