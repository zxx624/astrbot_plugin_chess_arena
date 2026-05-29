from __future__ import annotations

import asyncio
import glob
import json
import os
import socket
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


@dataclass
class ArenaState:
    connected: bool = False
    last_event_at: float | None = None
    last_error: str = ""
    reconnect_count: int = 0
    accepted_challenges: int = 0
    submitted_moves: int = 0


class ChessArenaPlugin(Star):
    """AstrBot 棋擂台客户端：自动注册、SSE 接入、自动接挑战、合法走法和台词。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.arena_base = str(self.config.get("arena_base") or "http://127.0.0.1:8787").rstrip("/")
        self.token = str(self.config.get("token") or "").strip()
        self.auto_register = bool(self.config.get("auto_register", True))
        self.bot_name = str(self.config.get("bot_name") or "").strip() or self._default_bot_name()
        self.avatar_url = str(self.config.get("avatar_url") or "").strip()
        self.description = str(self.config.get("description") or "AstrBot Chess Arena bot").strip()
        self.chess_style = str(self.config.get("chess_style") or "random").strip() or "random"
        self.persona_prompt = str(
            self.config.get("persona_prompt")
            or "像群里真人下棋，自然、松弛、有一点胜负欲，不要像客服。"
        ).strip()
        self.commentary_enabled = bool(self.config.get("commentary_enabled", True))
        self.commentary_timeout_sec = int(self.config.get("commentary_timeout_sec") or 8)
        self.auto_accept_challenges = bool(self.config.get("auto_accept_challenges", True))
        self.engine_mode = str(self.config.get("engine_mode") or "random")
        self.move_timeout_sec = int(self.config.get("move_timeout_sec") or 10)
        self.announce_to_current_chat = bool(self.config.get("announce_to_current_chat", False))

        self.state = ArenaState()
        self._session: aiohttp.ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

        self._startup_task = asyncio.create_task(self._startup(), name="chess_arena_startup")

    async def _startup(self) -> None:
        """启动流程：必要时自动注册，验证 token，上报 Bot 设置，然后连接 SSE。"""
        try:
            if not self.token and self.auto_register:
                await self._auto_register_bot()
            elif not self.token:
                logger.warning("[ChessArena] 未配置 token 且 auto_register=false，SSE 客户端不会连接。")
                return

            if not await self._verify_token():
                logger.warning("[ChessArena] token 无效或验证失败，请在 WebUI 检查配置。token=%s", self._token_hint(self.token))
                return

            await self._patch_bot_settings()
            self._sse_task = asyncio.create_task(self._sse_loop(), name="chess_arena_sse_loop")
            logger.info("[ChessArena] SSE 客户端已启动: %s bot=%s token=%s", self.arena_base, self.bot_name, self._token_hint(self.token))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - plugin startup must not crash AstrBot
            self.state.last_error = str(exc)
            logger.exception("[ChessArena] 启动流程失败: %s", exc)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _auto_register_bot(self) -> None:
        session = await self._get_session()
        url = f"{self.arena_base}/api/bots/register"
        payload = self._bot_settings_payload(include_client=True)
        logger.info("[ChessArena] token 为空，正在自动注册 Bot: %s", self.bot_name)
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"register bot failed: HTTP {resp.status} {text[:200]}")
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"register bot returned non-json: {text[:200]}") from exc

        token = str(data.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"register bot response missing token: {self._short(data)}")
        self.token = token
        self.config["token"] = token
        logger.info("[ChessArena] 自动注册成功 bot_id=%s token=%s", data.get("bot_id") or data.get("id"), self._token_hint(token))
        await self._save_token_to_runtime_config(token)

    async def _verify_token(self) -> bool:
        session = await self._get_session()
        url = f"{self.arena_base}/api/bots/me"
        try:
            async with session.get(url, headers=self._auth_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    logger.warning("[ChessArena] token 验证失败: HTTP %s %s", resp.status, text[:200])
                    return False
                logger.info("[ChessArena] token 验证成功: %s", self._short(json.loads(text) if text else {}))
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] token 验证异常: %s", exc)
            return False

    async def _patch_bot_settings(self) -> None:
        session = await self._get_session()
        url = f"{self.arena_base}/api/bots/me"
        payload = self._bot_settings_payload(include_client=False)
        async with session.patch(url, json=payload, headers=self._auth_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.warning("[ChessArena] 上报 Bot 设置失败: HTTP %s %s", resp.status, text[:200])
                return
            logger.info("[ChessArena] 已上报 Bot 设置: name=%s style=%s", self.bot_name, self.chess_style)

    def _bot_settings_payload(self, include_client: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.bot_name,
            "avatar_url": self.avatar_url,
            "description": self.description,
            "chess_style": self.chess_style,
            "persona_prompt": self.persona_prompt,
            "engine_mode": self.engine_mode,
            "is_public": True,
        }
        if include_client:
            payload.update({"client_type": "astrbot", "instance_name": self._instance_name()})
        return payload

    async def _save_token_to_runtime_config(self, token: str) -> None:
        """尽量把自动注册 token 写回 AstrBot 插件 runtime config；失败只提示日志。"""
        paths = self._candidate_runtime_config_paths()
        if not paths:
            logger.warning("[ChessArena] 未找到 runtime config 路径，无法自动写回 token=%s", self._token_hint(token))
            return

        last_error = ""
        for path in paths:
            try:
                await asyncio.to_thread(self._write_token_config_file, path, token)
                logger.info("[ChessArena] 已写回 token 到 runtime config: %s token=%s", path, self._token_hint(token))
                return
            except Exception as exc:  # noqa: BLE001
                last_error = f"{path}: {exc}"
                logger.warning("[ChessArena] 写回 token 到 runtime config 失败: %s", last_error)
        logger.warning("[ChessArena] token 自动写回失败，请手动复制到 WebUI 配置。最后错误: %s", last_error)

    def _candidate_runtime_config_paths(self) -> list[Path]:
        env_path = os.environ.get("CHESS_ARENA_CONFIG_PATH") or os.environ.get("ASTRBOT_PLUGIN_CHESS_ARENA_CONFIG")
        candidates: list[Path] = [Path(env_path)] if env_path else []

        # live 部署常见路径：/opt/astrbotN/data/config/astrbot_plugin_chess_arena_config.json
        candidates.extend(Path(p) for p in glob.glob("/opt/astrbot*/data/config/astrbot_plugin_chess_arena_config.json"))
        candidates.extend(Path(p) for p in glob.glob("/opt/astrbot*/data/config/*chess_arena*config*.json"))

        # 根据插件文件位置反推 data/config。
        here = Path(__file__).resolve()
        for parent in here.parents:
            if parent.name == "plugins" and parent.parent.name == "data":
                candidates.append(parent.parent / "config" / "astrbot_plugin_chess_arena_config.json")

        # 开发目录兜底，便于本地验证；不会影响 live 手动配置。
        candidates.append(here.parent / "astrbot_plugin_chess_arena_config.json")

        deduped: list[Path] = []
        seen: set[str] = set()
        for item in candidates:
            key = str(item)
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        # 优先写已存在的 live config；不存在的兜底路径排后。
        return sorted(deduped, key=lambda p: (not p.exists(), str(p)))

    @staticmethod
    def _write_token_config_file(path: Path, token: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("config root is not object")
        data["token"] = token
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    async def _sse_loop(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._connect_and_read_sse()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - plugin must survive network errors
                self.state.connected = False
                self.state.last_error = str(exc)
                self.state.reconnect_count += 1
                logger.warning("[ChessArena] SSE 连接异常，将重连: %s", exc)

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _connect_and_read_sse(self) -> None:
        session = await self._get_session()
        url = f"{self.arena_base}/sse/bot?token={quote(self.token)}"
        logger.info("[ChessArena] 正在连接 SSE: %s", self._safe_url(url))
        async with session.get(url, headers={"Accept": "text/event-stream"}) as resp:
            resp.raise_for_status()
            self.state.connected = True
            self.state.last_error = ""
            logger.info("[ChessArena] SSE 已连接")

            event_name: str | None = None
            data_lines: list[str] = []
            async for raw in resp.content:
                if self._stopping.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    await self._dispatch_sse_event(event_name, "\n".join(data_lines))
                    event_name = None
                    data_lines = []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            self.state.connected = False

    async def _dispatch_sse_event(self, sse_event: str | None, data: str) -> None:
        if not data and not sse_event:
            return
        self.state.last_event_at = time.time()
        payload: dict[str, Any]
        try:
            payload = json.loads(data) if data else {}
        except json.JSONDecodeError:
            payload = {"raw": data}

        event_type = str(payload.get("type") or payload.get("event") or sse_event or "message")
        logger.info("[ChessArena] 收到事件 %s: %s", event_type, self._short(payload))

        if event_type == "challenge_received":
            await self._handle_challenge_received(payload)
        elif event_type == "your_turn":
            await self._handle_your_turn(payload)

    async def _handle_challenge_received(self, event: dict[str, Any]) -> None:
        if not self.auto_accept_challenges:
            logger.info("[ChessArena] 已忽略挑战：auto_accept_challenges=false")
            return
        challenge_id = event.get("id") or event.get("challenge_id") or event.get("challengeId")
        if not challenge_id:
            logger.warning("[ChessArena] challenge_received 缺少 id: %s", event)
            return
        session = await self._get_session()
        url = f"{self.arena_base}/api/challenges/{quote(str(challenge_id), safe='')}/accept"
        async with session.post(url, headers=self._auth_headers()) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"accept challenge failed: HTTP {resp.status} {text[:200]}")
            self.state.accepted_challenges += 1
            logger.info("[ChessArena] 已接受挑战 %s", challenge_id)

    async def _handle_your_turn(self, event: dict[str, Any]) -> None:
        legal_moves = event.get("legal_moves") or event.get("legalMoves") or []
        if not isinstance(legal_moves, list) or not legal_moves:
            logger.warning("[ChessArena] your_turn 无 legal_moves: %s", event)
            return
        move = self._choose_move(legal_moves, event)
        match_id = event.get("match_id") or event.get("matchId") or event.get("id")
        if not match_id:
            logger.warning("[ChessArena] your_turn 缺少 match_id: %s", event)
            return

        started = time.perf_counter()
        comment = await self._make_comment(move, event)
        duration_ms = max(1, int((time.perf_counter() - started) * 1000))
        session = await self._get_session()
        url = f"{self.arena_base}/api/matches/{quote(str(match_id), safe='')}/move"
        payload = {
            "move": move,
            "comment": comment,
            "duration_ms": duration_ms,
        }
        timeout = aiohttp.ClientTimeout(total=max(1, self.move_timeout_sec))
        async with session.post(url, json=payload, headers=self._auth_headers(), timeout=timeout) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"submit move failed: HTTP {resp.status} {text[:200]}")
            self.state.submitted_moves += 1
            logger.info("[ChessArena] match=%s 已提交走法: %s comment=%s", match_id, move, comment)

    def _choose_move(self, legal_moves: list[Any], event: dict[str, Any]) -> str:
        """始终从后端给出的 legal_moves 中选步；按 chess_style 做轻量偏好。"""
        moves = [str(move) for move in legal_moves if move]
        if not moves:
            raise RuntimeError("no legal moves")
        style = (self.chess_style or self.engine_mode or "random").lower()
        if style in {"steady", "defensive"}:
            return sorted(moves)[len(moves) // 2]
        if style in {"aggressive", "greedy", "showman"}:
            ranked = sorted(moves)
            pool = ranked[max(0, len(ranked) * 2 // 3):] or ranked
            return random.choice(pool)
        return random.choice(moves)

    async def _make_comment(self, move: str, event: dict[str, Any]) -> str:
        if not self.commentary_enabled:
            return ""
        fallback = self._template_comment(move, event)
        llm_comment = await self._try_llm_comment(move, event, fallback)
        return self._sanitize_comment(llm_comment or fallback)

    async def _try_llm_comment(self, move: str, event: dict[str, Any], fallback: str) -> str:
        provider = None
        try:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter()
            elif hasattr(self.context, "get_all_providers"):
                providers = list(self.context.get_all_providers() or [])
                provider = providers[0] if providers else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ChessArena] 获取 LLM provider 失败，使用模板台词: %s", exc)
            return fallback
        if not provider or not hasattr(provider, "text_chat"):
            return fallback

        prompt = (
            f"你是正在下中国象棋的 Bot，名字：{self.bot_name}，棋风：{self.chess_style}。\n"
            f"人格设定：{self.persona_prompt}\n"
            f"当前轮到你走，已选定合法走法 {move}，局面 ply={event.get('ply', '')} side={event.get('side', '')}。\n"
            "请只输出一句 30 字以内的自然短台词，不要解释，不要提 system/prompt，不要说作为 AI。"
        )
        system_prompt = "你只给象棋走棋台词，短、自然、有个性；不得决定或修改走法。"
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt),
                timeout=max(1, self.commentary_timeout_sec),
            )
            return str(getattr(response, "completion_text", response) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ChessArena] LLM 台词生成失败，使用模板台词: %s", exc)
            return fallback

    def _template_comment(self, move: str, event: dict[str, Any]) -> str:
        style = (self.chess_style or "random").lower()
        templates = {
            "aggressive": ["这步先压上去，别眨眼。", "我先抢个先手，看你怎么接。"],
            "greedy": ["有便宜不占，那可不行。", "这步看着就有油水。"],
            "steady": ["不急，先把阵型站稳。", "稳一点，这棋慢慢磨。"],
            "defensive": ["先补一手，别给你机会。", "把门看住，再找反击。"],
            "showman": ["来点花活，棋盘上见真章。", "这步有点意思，坐稳了。"],
            "random": ["先走这步，看看风向。", "这手落下，局面就热闹了。"],
        }
        pool = templates.get(style) or templates["random"]
        return f"{random.choice(pool)}（{move}）"

    @staticmethod
    def _sanitize_comment(text: str) -> str:
        text = " ".join(str(text or "").replace("\n", " ").split()).strip(' \"“”')
        banned = ("作为AI", "作为 AI", "我是AI", "我是 AI")
        for item in banned:
            text = text.replace(item, "")
        return text[:80] if text else "先走这步。"

    @filter.command("棋擂台状态")
    async def arena_status(self, event: AstrMessageEvent):
        """查看棋擂台连接和自动对弈状态。"""
        last_event = "无" if not self.state.last_event_at else time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.state.last_event_at)
        )
        status = "在线" if self.state.connected else "离线"
        token_status = "已配置" if self.token else "未配置"
        msg = (
            f"棋擂台状态：{status}\n"
            f"平台：{self.arena_base}\n"
            f"Bot：{self.bot_name}\n"
            f"Token：{token_status}（{self._token_hint(self.token)}）\n"
            f"引擎/棋风：{self.engine_mode}/{self.chess_style}\n"
            f"自动注册/自动接挑战：{self.auto_register}/{self.auto_accept_challenges}\n"
            f"走棋台词：{self.commentary_enabled}\n"
            f"已接挑战/已走棋：{self.state.accepted_challenges}/{self.state.submitted_moves}\n"
            f"重连次数：{self.state.reconnect_count}\n"
            f"最近事件：{last_event}"
        )
        if self.state.last_error:
            msg += f"\n最近错误：{self.state.last_error}"
        yield event.plain_result(msg)

    @filter.command("棋擂台在线")
    async def arena_online(self, event: AstrMessageEvent):
        """主动检查棋擂台 HTTP 可达性。"""
        try:
            session = await self._get_session()
            url = f"{self.arena_base}/api/bots/me"
            async with session.get(url, headers=self._auth_headers(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    yield event.plain_result(f"棋擂台在线检查失败：HTTP {resp.status} {text[:200]}")
                else:
                    yield event.plain_result(f"棋擂台在线检查成功：HTTP {resp.status} {text[:200]}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"棋擂台在线检查异常：{exc}")

    @filter.command("棋擂台挑战")
    async def arena_challenge(self, event: AstrMessageEvent, bot_id: str):
        """向指定 bot_id 发起挑战。"""
        if not bot_id:
            yield event.plain_result("用法：棋擂台挑战 <bot_id>")
            return
        try:
            session = await self._get_session()
            url = f"{self.arena_base}/api/challenges"
            payload = {"opponent_bot_id": bot_id, "side": "random"}
            async with session.post(url, json=payload, headers=self._auth_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    yield event.plain_result(f"发起挑战失败：HTTP {resp.status} {text[:200]}")
                else:
                    yield event.plain_result(f"已发起挑战 {bot_id}：{text[:300]}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"发起挑战异常：{exc}")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _safe_url(url: str) -> str:
        if "token=" not in url:
            return url
        return url.split("token=", 1)[0] + "token=***"

    @staticmethod
    def _short(payload: Any, limit: int = 500) -> str:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "..."

    @staticmethod
    def _token_hint(token: str) -> str:
        token = str(token or "")
        if not token:
            return "<empty>"
        if len(token) <= 10:
            return token[:2] + "***"
        return f"{token[:6]}...{token[-4:]}"

    @staticmethod
    def _default_bot_name() -> str:
        return f"AstrBot-{socket.gethostname()[:12] or 'bot'}"

    @staticmethod
    def _instance_name() -> str:
        return os.environ.get("ASTRBOT_INSTANCE_NAME") or socket.gethostname() or "astrbot"

    async def terminate(self):
        """插件卸载时停止启动/SSE 任务并关闭 HTTP 会话。"""
        self._stopping.set()
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
