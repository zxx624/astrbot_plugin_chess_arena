from __future__ import annotations

import asyncio
import glob
import json
import os
import socket
import random
import shlex
import shutil
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
        self.arena_base = str(self.config.get("arena_base") or "https://gulu624.icu").rstrip("/")
        self.arena_fallback_bases = self._parse_fallback_bases(self.config.get("arena_fallback_bases"))
        self.token = str(self.config.get("token") or "").strip()
        self.auto_register = bool(self.config.get("auto_register", True))
        configured_bot_name = str(self.config.get("bot_name") or "").strip()
        self._generated_bot_name = not configured_bot_name
        self.bot_name = configured_bot_name or self._default_bot_name()
        self.avatar_url = str(self.config.get("avatar_url") or "").strip()
        self.description = str(self.config.get("description") or "AstrBot Chess Arena bot").strip()
        self.chess_style = str(self.config.get("chess_style") or "random").strip() or "random"
        self.persona_prompt = str(
            self.config.get("persona_prompt")
            or "像群里真人下棋，自然、松弛、有一点胜负欲，不要像客服。"
        ).strip()
        self.commentary_enabled = bool(self.config.get("commentary_enabled", True))
        self.commentary_timeout_sec = int(self.config.get("commentary_timeout_sec") or 8)
        self.llm_provider_mode = str(self.config.get("llm_provider_mode") or "default").strip().lower()
        if self.llm_provider_mode not in {"default", "custom"}:
            self.llm_provider_mode = "default"
        self.llm_provider_id = str(self.config.get("llm_provider_id") or "").strip()
        self.auto_accept_challenges = bool(self.config.get("auto_accept_challenges", True))
        self.engine_mode = self._normalize_engine_mode(self.config.get("engine_mode") or "auto")
        self.engine_depth = int(self.config.get("engine_depth") or 3)
        self.engine_timeout_sec = max(1, int(self.config.get("engine_timeout_sec") or 8))
        self.custom_engine_command = str(self.config.get("custom_engine_command") or "").strip()
        self.custom_engine_http_url = str(self.config.get("custom_engine_http_url") or "").strip()
        self.custom_engine_http_headers = self.config.get("custom_engine_http_headers") or ""
        self.local_engine_node_path = str(self.config.get("local_engine_node_path") or "node").strip() or "node"
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

            verify_ok = await self._verify_token_with_retry()
            if verify_ok is False:
                logger.warning("[ChessArena] token 无效，请在 WebUI 检查配置。token=%s", self._token_hint(self.token))
                return
            if verify_ok is None:
                logger.warning("[ChessArena] 暂时无法连接棋擂台，保留 token 并稍后重试启动。token=%s", self._token_hint(self.token))
                await self._schedule_startup_retry()
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

    def _candidate_arena_bases(self) -> list[str]:
        bases = [self.arena_base, *self.arena_fallback_bases]
        deduped: list[str] = []
        seen: set[str] = set()
        for base in bases:
            base = str(base or "").strip().rstrip("/")
            if base and base not in seen:
                seen.add(base)
                deduped.append(base)
        return deduped

    @staticmethod
    def _parse_fallback_bases(value: Any) -> list[str]:
        if value is None or value == "":
            raw_items = []
        elif isinstance(value, str):
            raw_items = value.replace("\n", ",").split(",")
        elif isinstance(value, (list, tuple)):
            raw_items = list(value)
        else:
            raw_items = []
        bases: list[str] = []
        for item in raw_items:
            base = str(item or "").strip().rstrip("/")
            if base and base not in bases:
                bases.append(base)
        return bases

    async def _request_text_with_fallback(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> tuple[str, int, str]:
        session = await self._get_session()
        last_error = ""
        for base in self._candidate_arena_bases():
            url = f"{base}{path}"
            try:
                async with session.request(method, url, json=json_payload, headers=headers, timeout=timeout) as resp:
                    text = await resp.text()
                    if resp.status >= 500 and base != self._candidate_arena_bases()[-1]:
                        last_error = f"{base}: HTTP {resp.status} {text[:200]}"
                        logger.warning("[ChessArena] %s %s 失败，尝试备用地址: %s", method, base, last_error)
                        continue
                    if base != self.arena_base:
                        old = self.arena_base
                        self.arena_base = base
                        self.config["arena_base"] = base
                        logger.warning("[ChessArena] 主地址不可用，已切换棋擂台地址: %s -> %s", old, base)
                    return base, resp.status, text
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = f"{base}: {exc}"
                logger.warning("[ChessArena] 连接棋擂台失败，将尝试备用地址: %s", last_error)
        raise RuntimeError(f"all arena bases failed for {method} {path}: {last_error}")

    async def _auto_register_bot(self) -> None:
        payload = self._bot_settings_payload(include_client=True)
        logger.info("[ChessArena] token 为空，正在自动注册 Bot: %s", self.bot_name)
        base, status, text = await self._request_text_with_fallback(
            "POST",
            "/api/bots/register",
            json_payload=payload,
            timeout=aiohttp.ClientTimeout(total=15),
        )
        if status >= 400:
            raise RuntimeError(f"register bot failed: HTTP {status} {text[:200]}")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"register bot returned non-json: {text[:200]}") from exc

        token = str(data.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"register bot response missing token: {self._short(data)}")
        self.token = token
        self.config["token"] = token
        self.config["arena_base"] = self.arena_base
        if self._generated_bot_name:
            self.config["bot_name"] = self.bot_name
        logger.info("[ChessArena] 自动注册成功 bot_id=%s token=%s", data.get("bot_id") or data.get("id"), self._token_hint(token))
        await self._save_registration_to_runtime_config(token)

    async def _verify_token(self) -> bool | None:
        """Return True for valid token, False for auth failure, None for network/server failure."""
        try:
            _base, status, text = await self._request_text_with_fallback(
                "GET",
                "/api/bots/me",
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if status in {401, 403, 404}:
                logger.warning("[ChessArena] token 验证失败: HTTP %s %s", status, text[:200])
                return False
            if status >= 400:
                logger.warning("[ChessArena] token 验证遇到服务端/临时错误: HTTP %s %s", status, text[:200])
                return None
            logger.info("[ChessArena] token 验证成功: %s", self._short(json.loads(text) if text else {}))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] token 验证网络异常，稍后重试，不判定 token 无效: %s", exc)
            return None

    async def _verify_token_with_retry(self) -> bool | None:
        last: bool | None = None
        for attempt in range(3):
            last = await self._verify_token()
            if last is not None:
                return last
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
        return last

    async def _schedule_startup_retry(self) -> None:
        async def _retry() -> None:
            try:
                await asyncio.sleep(30)
                if not self._stopping.is_set():
                    await self._startup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ChessArena] 启动重试失败: %s", exc)

        self._startup_task = asyncio.create_task(_retry(), name="chess_arena_startup_retry")

    async def _patch_bot_settings(self) -> None:
        payload = self._bot_settings_payload(include_client=False)
        _base, status, text = await self._request_text_with_fallback(
            "PATCH",
            "/api/bots/me",
            json_payload=payload,
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if status >= 400:
            logger.warning("[ChessArena] 上报 Bot 设置失败: HTTP %s %s", status, text[:200])
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

    async def _save_registration_to_runtime_config(self, token: str) -> None:
        """把自动注册结果写回 AstrBot 插件 runtime config，便于打开配置页直接看到 token。"""
        paths = self._candidate_runtime_config_paths()
        if not paths:
            logger.warning("[ChessArena] 未找到 runtime config 路径，无法自动写回 token=%s", self._token_hint(token))
            return

        last_error = ""
        wrote = False
        for path in paths:
            # 只写当前实例推导出的 config；避免 astrbot1 新装时误写 astrbot2。
            if path.exists() or path == self._instance_runtime_config_path():
                try:
                    await asyncio.to_thread(self._write_registration_config_file, path, self._runtime_config_payload(token))
                    logger.info("[ChessArena] 已写回注册信息到 runtime config: %s token=%s", path, self._token_hint(token))
                    wrote = True
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{path}: {exc}"
                    logger.warning("[ChessArena] 写回注册信息到 runtime config 失败: %s", last_error)
        if not wrote:
            logger.warning("[ChessArena] token 自动写回失败，请手动复制到 WebUI 配置。最后错误: %s", last_error)

    def _runtime_config_payload(self, token: str) -> dict[str, Any]:
        data = dict(self.config)
        data.update({
            "arena_base": self.arena_base,
            "arena_fallback_bases": ",".join(self.arena_fallback_bases),
            "token": token,
            "auto_register": self.auto_register,
            "bot_name": self.bot_name,
            "avatar_url": self.avatar_url,
            "description": self.description,
            "chess_style": self.chess_style,
            "persona_prompt": self.persona_prompt,
            "commentary_enabled": self.commentary_enabled,
            "commentary_timeout_sec": self.commentary_timeout_sec,
            "llm_provider_mode": self.llm_provider_mode,
            "llm_provider_id": self.llm_provider_id,
            "auto_accept_challenges": self.auto_accept_challenges,
            "engine_mode": self.engine_mode,
            "engine_depth": self.engine_depth,
            "engine_timeout_sec": self.engine_timeout_sec,
            "custom_engine_command": self.custom_engine_command,
            "custom_engine_http_url": self.custom_engine_http_url,
            "custom_engine_http_headers": self.custom_engine_http_headers,
            "local_engine_node_path": self.local_engine_node_path,
            "move_timeout_sec": self.move_timeout_sec,
            "announce_to_current_chat": self.announce_to_current_chat,
        })
        return data

    def _instance_runtime_config_path(self) -> Path:
        here = Path(__file__).resolve()
        for parent in here.parents:
            if parent.name == "plugins" and parent.parent.name == "data":
                return parent.parent / "config" / "astrbot_plugin_chess_arena_config.json"
        return here.parent / "astrbot_plugin_chess_arena_config.json"

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
    def _write_registration_config_file(path: Path, values: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            raw = path.read_text(encoding="utf-8-sig").strip()
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("config root is not object")
        data.update(values)
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
        last_error = ""
        for base in self._candidate_arena_bases():
            url = f"{base}/sse/bot?token={quote(self.token)}"
            logger.info("[ChessArena] 正在连接 SSE: %s", self._safe_url(url))
            try:
                async with session.get(url, headers={"Accept": "text/event-stream"}) as resp:
                    if base != self.arena_base:
                        old = self.arena_base
                        self.arena_base = base
                        self.config["arena_base"] = base
                        logger.warning("[ChessArena] SSE 已切换棋擂台地址: %s -> %s", old, base)
                    return await self._read_sse_response(resp)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = f"{base}: {exc}"
                logger.warning("[ChessArena] SSE 地址不可用，尝试下一个: %s", last_error)
        raise RuntimeError(f"all arena SSE bases failed: {last_error}")

    async def _read_sse_response(self, resp: aiohttp.ClientResponse) -> None:
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
        move = await self._choose_move(legal_moves, event)
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

    async def _choose_move(self, legal_moves: list[Any], event: dict[str, Any]) -> str:
        """始终从后端给出的 legal_moves 中选步；按 engine_mode 走引擎链，最终随机兜底。"""
        moves = [str(move).strip() for move in legal_moves if str(move or "").strip()]
        if not moves:
            raise RuntimeError("no legal moves")
        return await self._run_engine_chain(moves, event)

    def _engine_request_payload(self, legal_moves: list[str], event: dict[str, Any]) -> dict[str, Any]:
        side = str(event.get("side") or event.get("turn") or "").strip()
        return {
            "fen": str(event.get("fen") or "").strip(),
            "legal_moves": legal_moves,
            "side": side,
            "depth": max(1, self.engine_depth),
            "timeout_ms": self.engine_timeout_sec * 1000,
            "bot_name": self.bot_name,
            "chess_style": self.chess_style,
        }

    @staticmethod
    def _normalize_engine_mode(mode: Any) -> str:
        value = str(mode or "auto").strip().lower()
        if value == "xqwlight":  # 兼容旧配置：旧 xqwlight 等价于服务器 xqwlight
            return "server_xqwlight"
        allowed = {"auto", "server_xqwlight", "local_xqwlight", "custom_command", "custom_http", "random"}
        return value if value in allowed else "auto"

    def _engine_chain(self) -> list[str]:
        mode = self._normalize_engine_mode(self.engine_mode)
        if mode == "random":
            return ["random"]
        if mode != "auto":
            return [mode, "random"]

        chain: list[str] = []
        if self.custom_engine_command:
            chain.append("custom_command")
        if self.custom_engine_http_url:
            chain.append("custom_http")
        chain.extend(["local_xqwlight", "server_xqwlight", "random"])
        deduped: list[str] = []
        for engine in chain:
            if engine not in deduped:
                deduped.append(engine)
        return deduped

    async def _run_engine_chain(self, legal_moves: list[str], event: dict[str, Any]) -> str:
        payload = self._engine_request_payload(legal_moves, event)
        for engine in self._engine_chain():
            try:
                if engine == "random":
                    return random.choice(legal_moves)
                if engine == "server_xqwlight":
                    best = await self._choose_server_xqwlight_move(payload, legal_moves)
                elif engine == "local_xqwlight":
                    best = await self._choose_local_xqwlight_move(payload, legal_moves)
                elif engine == "custom_command":
                    best = await self._choose_custom_command_move(payload, legal_moves)
                elif engine == "custom_http":
                    best = await self._choose_custom_http_move(payload, legal_moves)
                else:
                    logger.warning("[ChessArena] 未知 engine_mode=%s，跳过", engine)
                    continue
                valid = self._validate_engine_move(best, legal_moves, engine)
                if valid:
                    logger.info("[ChessArena] %s chose: %s", engine, valid)
                    return valid
            except Exception as exc:  # noqa: BLE001 - 引擎失败不能影响走棋
                logger.warning("[ChessArena] %s 引擎失败，尝试下一引擎: %s", engine, exc)
        return random.choice(legal_moves)

    def _validate_engine_move(self, move: Any, legal_moves: list[str], engine: str = "engine") -> str | None:
        best = str(move or "").strip()
        if best and best in legal_moves:
            return best
        logger.warning("[ChessArena] %s 返回非法/空走法 %s，legal_moves=%s", engine, best, legal_moves[:20])
        return None

    async def _choose_xqwlight_move(self, legal_moves: list[str], event: dict[str, Any]) -> str:
        """兼容旧内部调用：走服务器 xqwlight，失败随机。"""
        payload = self._engine_request_payload(legal_moves, event)
        try:
            best = await self._choose_server_xqwlight_move(payload, legal_moves)
            return self._validate_engine_move(best, legal_moves, "server_xqwlight") or random.choice(legal_moves)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] xqwlight engine error, falling back to random: %s", exc)
            return random.choice(legal_moves)

    async def _choose_server_xqwlight_move(self, payload: dict[str, Any], legal_moves: list[str]) -> str | None:
        """调用棋擂台平台 /api/analyze；只返回已校验前的 best_move，异常交给引擎链处理。"""
        if not payload.get("fen"):
            logger.warning("[ChessArena] server_xqwlight skipped: no FEN in event")
            return None
        req = {"fen": payload["fen"], "depth": payload["depth"], "legal_moves": legal_moves}
        base, status, text = await self._request_text_with_fallback(
            "POST",
            "/api/analyze",
            json_payload=req,
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=self.engine_timeout_sec),
        )
        if status >= 400:
            logger.warning("[ChessArena] server_xqwlight analyze failed: %s HTTP %s %s", base, status, text[:100])
            return None
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            logger.warning("[ChessArena] server_xqwlight returned non-json: %s", text[:100])
            return None
        return data.get("best_move") or data.get("move")

    async def _choose_local_xqwlight_move(self, payload: dict[str, Any], legal_moves: list[str]) -> str | None:
        script = Path(__file__).resolve().parent / "engine" / "analyze.js"
        if not script.exists():
            logger.debug("[ChessArena] local_xqwlight skipped: %s not found", script)
            return None
        node = self.local_engine_node_path or "node"
        if os.path.sep not in node and shutil.which(node) is None:
            logger.debug("[ChessArena] local_xqwlight skipped: node executable not found: %s", node)
            return None
        return await self._run_command_engine([node, str(script)], payload, "local_xqwlight")

    async def _choose_custom_command_move(self, payload: dict[str, Any], legal_moves: list[str]) -> str | None:
        command = self.custom_engine_command
        if not command:
            return None
        payload_text = json.dumps(payload, ensure_ascii=False)
        if "{fen_json}" in command:
            parts = shlex.split(command.replace("{fen_json}", shlex.quote(payload_text)))
        else:
            parts = shlex.split(command)
        if not parts:
            return None
        return await self._run_command_engine(parts, payload, "custom_command", stdin_json="{fen_json}" not in command)

    async def _run_command_engine(
        self,
        args: list[str],
        payload: dict[str, Any],
        engine: str,
        *,
        stdin_json: bool = True,
    ) -> str | None:
        input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if stdin_json else None
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_json else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input_bytes), timeout=self.engine_timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("[ChessArena] %s command timeout: %s", engine, args[:2])
            return None
        if proc.returncode != 0:
            logger.warning("[ChessArena] %s command exited %s: %s", engine, proc.returncode, stderr.decode("utf-8", "ignore")[:200])
            return None
        text = stdout.decode("utf-8", "ignore").strip()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            logger.warning("[ChessArena] %s command returned non-json: %s", engine, text[:200])
            return None
        return data.get("best_move") or data.get("move")

    async def _choose_custom_http_move(self, payload: dict[str, Any], legal_moves: list[str]) -> str | None:
        url = self.custom_engine_http_url
        if not url:
            return None
        headers = self._custom_engine_headers()
        session = await self._get_session()
        async with session.post(
            url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=self.engine_timeout_sec),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                logger.warning("[ChessArena] custom_http failed: HTTP %s %s", resp.status, text[:100])
                return None
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                logger.warning("[ChessArena] custom_http returned non-json: %s", text[:200])
                return None
        return data.get("best_move") or data.get("move")

    def _custom_engine_headers(self) -> dict[str, str]:
        raw = self.custom_engine_http_headers
        if not raw:
            return {}
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        try:
            data = json.loads(str(raw))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except json.JSONDecodeError:
            logger.warning("[ChessArena] custom_engine_http_headers 不是合法 JSON object，已忽略")
        return {}

    async def _make_comment(self, move: str, event: dict[str, Any]) -> str:
        if not self.commentary_enabled:
            return ""
        facts = self._analyze_move_facts(move, event)
        fallback = self._template_comment(move, event, facts)
        llm_comment = await self._try_llm_comment(move, event, fallback, facts)
        return self._sanitize_comment(llm_comment or fallback, facts)

    async def _try_llm_comment(
        self, move: str, event: dict[str, Any], fallback: str, facts: dict[str, Any]
    ) -> str:
        provider = await self._resolve_llm_provider()
        if not provider or not hasattr(provider, "text_chat"):
            return fallback

        forbidden = self._comment_forbidden_claims(facts)
        fact_lines = [
            f"Bot 名字：{self.bot_name}",
            f"棋风：{self.chess_style}",
            f"人格设定：{self.persona_prompt}",
            f"本步 UCCI：{move}",
            f"中文走法：{facts.get('notation') or move}",
            f"移动棋子：{facts.get('piece_name') or '未知'}",
            f"动作类型：{facts.get('action_label') or '普通调动'}",
            f"吃子：{'是，吃掉' + facts.get('captured_name', '') if facts.get('is_capture') else '否'}",
            f"将军：{'是' if facts.get('is_check') else '否'}",
            f"执棋方：{facts.get('side_label') or event.get('side') or event.get('turn') or '未知'}",
            f"当前手数 ply：{event.get('ply', '')}",
        ]
        if forbidden:
            fact_lines.append("禁止声称：" + "、".join(forbidden))
        prompt = (
            "你正在给中国象棋 Bot 的刚刚这一步生成一句台词。必须严格贴合下面事实，不能脑补。\n"
            + "\n".join(fact_lines)
            + "\n要求：只输出一句中文短台词，30字以内；像群里真人下棋；不要解释棋理；"
              "不要提 prompt/system/AI；不要说事实里没有的吃子、将军、杀棋、绝杀、优势。"
        )
        system_prompt = "你只根据给定事实写象棋走棋台词；事实没有写明的战术效果一律不能声称。"
        try:
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, system_prompt=system_prompt, contexts=[]),
                timeout=max(1, self.commentary_timeout_sec),
            )
            return str(getattr(response, "completion_text", response) or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ChessArena] LLM 台词生成失败，使用事实模板台词: %s", exc)
            return fallback


    async def _resolve_llm_provider(self) -> Any:
        """选择用于台词/非象棋引擎能力的 LLM provider。

        default：跟随 AstrBot 当前默认对话模型。
        custom：使用配置里的 provider_id；找不到时回退默认对话模型，避免影响走棋。
        """
        if self.llm_provider_mode == "custom" and self.llm_provider_id:
            try:
                get_by_id = getattr(self.context, "get_provider_by_id", None)
                if callable(get_by_id):
                    provider = get_by_id(self.llm_provider_id)
                    if provider and hasattr(provider, "text_chat"):
                        return provider
                    logger.warning(
                        "[ChessArena] 配置的 LLM provider_id 不可用，将回退默认对话模型: %s",
                        self.llm_provider_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ChessArena] 获取手动指定 LLM provider 失败，将回退默认对话模型: %s", exc)

        try:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter()
                if provider and hasattr(provider, "text_chat"):
                    return provider
            get_all = getattr(self.context, "get_all_providers", None)
            if callable(get_all):
                providers = list(get_all() or [])
                for provider in providers:
                    if provider and hasattr(provider, "text_chat"):
                        return provider
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ChessArena] 获取默认 LLM provider 失败，使用模板台词: %s", exc)
        return None

    def _comment_forbidden_claims(self, facts: dict[str, Any]) -> list[str]:
        forbidden: list[str] = []
        if not facts.get("is_capture"):
            forbidden.extend(["吃子", "白赚", "有便宜", "收子", "兑掉"])
        if not facts.get("is_check"):
            forbidden.extend(["将军", "将一手", "杀棋", "绝杀", "要杀"])
        if facts.get("action_label") in {"后退防守", "横向调整", "普通调动"}:
            forbidden.extend(["压上去", "冲上去", "强攻"])
        return forbidden

    def _analyze_move_facts(self, move: str, event: dict[str, Any]) -> dict[str, Any]:
        board = self._parse_fen_board(str(event.get("fen") or ""))
        src_file, src_rank, dst_file, dst_rank = self._parse_ucci(move)
        piece = ""
        captured = ""
        if board and src_file is not None and src_rank is not None:
            src_row = 9 - src_rank
            src_col = src_file
            if 0 <= src_row < 10 and 0 <= src_col < 9:
                piece = board[src_row][src_col] or ""
        if board and dst_file is not None and dst_rank is not None:
            dst_row = 9 - dst_rank
            dst_col = dst_file
            if 0 <= dst_row < 10 and 0 <= dst_col < 9:
                captured = board[dst_row][dst_col] or ""

        side = str(event.get("side") or event.get("turn") or "").lower()
        if not side and piece:
            side = "red" if piece.isupper() else "black"
        notation = self._ucci_to_chinese(move, piece)
        action_label = self._move_action_label(piece, captured, src_rank, dst_rank)
        return {
            "move": move,
            "piece": piece,
            "piece_name": self._piece_name(piece),
            "captured": captured,
            "captured_name": self._piece_name(captured),
            "is_capture": bool(captured),
            "is_check": bool(event.get("check") or event.get("is_check") or event.get("gives_check")),
            "side": side,
            "side_label": {"red": "红方", "black": "黑方", "r": "红方", "b": "黑方"}.get(side, side),
            "notation": notation,
            "action_label": action_label,
        }

    @staticmethod
    def _parse_ucci(move: str) -> tuple[int | None, int | None, int | None, int | None]:
        move = str(move or "").strip().lower()
        if len(move) < 4:
            return None, None, None, None
        try:
            return ord(move[0]) - ord("a"), int(move[1]), ord(move[2]) - ord("a"), int(move[3])
        except Exception:
            return None, None, None, None

    @staticmethod
    def _parse_fen_board(fen: str) -> list[list[str]]:
        placement = str(fen or "").split()[0] if fen else ""
        rows: list[list[str]] = []
        for raw_row in placement.split("/")[:10]:
            row: list[str] = []
            for ch in raw_row:
                if ch.isdigit():
                    row.extend([""] * int(ch))
                else:
                    row.append(ch)
            rows.append((row + [""] * 9)[:9])
        while len(rows) < 10:
            rows.append([""] * 9)
        return rows

    @staticmethod
    def _piece_name(piece: str) -> str:
        names = {
            "K": "帅", "A": "仕", "B": "相", "E": "相", "N": "马", "H": "马", "R": "车", "C": "炮", "P": "兵",
            "k": "将", "a": "士", "b": "象", "e": "象", "n": "马", "h": "马", "r": "车", "c": "炮", "p": "卒",
        }
        return names.get(piece or "", "")

    def _ucci_to_chinese(self, move: str, piece: str = "") -> str:
        sf, sr, df, dr = self._parse_ucci(move)
        name = self._piece_name(piece)
        if sf is None or sr is None or df is None or dr is None or not name:
            return move
        is_red = piece.isupper()
        red_cols = "九八七六五四三二一"
        black_cols = "123456789"
        red_nums = "一二三四五六七八九"
        black_nums = "123456789"
        if is_red:
            start_col = red_cols[sf]
            if df == sf:
                verb = "进" if dr > sr else "退"
                target = red_nums[abs(dr - sr) - 1] if abs(dr - sr) else red_nums[dr]
            else:
                verb = "平"
                target = red_cols[df]
        else:
            start_col = black_cols[sf]
            if df == sf:
                verb = "进" if dr < sr else "退"
                target = black_nums[abs(dr - sr) - 1] if abs(dr - sr) else black_nums[dr]
            else:
                verb = "平"
                target = black_cols[df]
        return f"{name}{start_col}{verb}{target}"

    @staticmethod
    def _move_action_label(piece: str, captured: str, src_rank: int | None, dst_rank: int | None) -> str:
        if captured:
            return "吃子"
        if src_rank is None or dst_rank is None or not piece:
            return "普通调动"
        if dst_rank == src_rank:
            return "横向调整"
        is_red = piece.isupper()
        forward = dst_rank > src_rank if is_red else dst_rank < src_rank
        if forward:
            return "向前压进"
        return "后退防守"

    def _template_comment(self, move: str, event: dict[str, Any], facts: dict[str, Any] | None = None) -> str:
        facts = facts or self._analyze_move_facts(move, event)
        notation = facts.get("notation") or move
        if facts.get("is_check") and facts.get("is_capture"):
            pool = ["吃一口还带将，挺顺。", "这手有点劲，看你怎么应。"]
        elif facts.get("is_check"):
            pool = ["将一手，看看你怎么解。", "先给你一点压力。"]
        elif facts.get("is_capture"):
            pool = ["这子我先收下。", "有子能吃，不能客气。"]
        elif facts.get("action_label") == "向前压进":
            pool = ["先往前压一步。", "这步把位置顶上去。"]
        elif facts.get("action_label") == "后退防守":
            pool = ["先回防一下。", "这口我先补住。"]
        elif facts.get("action_label") == "横向调整":
            pool = ["先换个位置看你反应。", "这步先调整一下。"]
        else:
            pool = ["先走这步，看看你怎么接。", "这手先稳住局面。"]
        return f"{random.choice(pool)}（{notation}）"

    @staticmethod
    def _sanitize_comment(text: str, facts: dict[str, Any] | None = None) -> str:
        text = " ".join(str(text or "").replace("\n", " ").split()).strip(' \"“”')
        banned = ("作为AI", "作为 AI", "我是AI", "我是 AI")
        for item in banned:
            text = text.replace(item, "")
        if facts:
            blocked: list[str] = []
            if not facts.get("is_capture"):
                blocked.extend(["吃", "收下", "白赚", "有便宜"])
            if not facts.get("is_check"):
                blocked.extend(["将军", "绝杀", "杀棋"])
            if any(word in text for word in blocked):
                return ChessArenaPlugin._template_comment_static(facts)
        return text[:80] if text else "先走这步。"

    @staticmethod
    def _template_comment_static(facts: dict[str, Any]) -> str:
        notation = facts.get("notation") or facts.get("move") or "这步"
        if facts.get("is_check") and facts.get("is_capture"):
            return f"这手有点劲，看你怎么应。（{notation}）"
        if facts.get("is_check"):
            return f"将一手，看看你怎么解。（{notation}）"
        if facts.get("is_capture"):
            return f"这子我先收下。（{notation}）"
        action = facts.get("action_label")
        if action == "向前压进":
            return f"先往前压一步。（{notation}）"
        if action == "后退防守":
            return f"先回防一下。（{notation}）"
        if action == "横向调整":
            return f"这步先调整一下。（{notation}）"
        return f"先走这步，看看你怎么接。（{notation}）"

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
            f"引擎链：{' -> '.join(self._engine_chain())}\n"
            f"本地Node：{self._local_engine_node_status()}\n"
            f"自动注册/自动接挑战：{self.auto_register}/{self.auto_accept_challenges}\n"
            f"走棋台词：{self.commentary_enabled}\n"
            f"LLM模型：{self._llm_provider_status()}\n"
            f"已接挑战/已走棋：{self.state.accepted_challenges}/{self.state.submitted_moves}\n"
            f"重连次数：{self.state.reconnect_count}\n"
            f"最近事件：{last_event}"
        )
        if self.state.last_error:
            msg += f"\n最近错误：{self.state.last_error}"
        yield event.plain_result(msg)

    def _llm_provider_status(self) -> str:
        if self.llm_provider_mode == "custom":
            return f"手动指定({self.llm_provider_id or '未填写，回退默认'})"
        return "默认对话模型"

    def _local_engine_node_status(self) -> str:
        script = Path(__file__).resolve().parent / "engine" / "analyze.js"
        node = self.local_engine_node_path or "node"
        node_ok = bool(os.path.sep in node or shutil.which(node))
        script_ok = script.exists()
        return f"node={'可用' if node_ok else '不可用'} analyze.js={'存在' if script_ok else '缺失'}"

    @filter.command("棋擂台在线")
    async def arena_online(self, event: AstrMessageEvent):
        """主动检查棋擂台 HTTP 可达性。"""
        try:
            base, status, text = await self._request_text_with_fallback(
                "GET",
                "/api/bots/me",
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            )
            if status >= 400:
                yield event.plain_result(f"棋擂台在线检查失败：{base} HTTP {status} {text[:200]}")
            else:
                yield event.plain_result(f"棋擂台在线检查成功：{base} HTTP {status} {text[:200]}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"棋擂台在线检查异常：{exc}")

    @filter.command("棋擂台挑战")
    async def arena_challenge(self, event: AstrMessageEvent, bot_id: str):
        """向指定 bot_id 发起挑战。"""
        if not bot_id:
            yield event.plain_result("用法：棋擂台挑战 <bot_id>")
            return
        try:
            payload = {"opponent_bot_id": bot_id, "side": "random"}
            base, status, text = await self._request_text_with_fallback(
                "POST",
                "/api/challenges",
                json_payload=payload,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if status >= 400:
                yield event.plain_result(f"发起挑战失败：{base} HTTP {status} {text[:200]}")
            else:
                yield event.plain_result(f"已发起挑战 {bot_id}：{text[:300]}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"发起挑战异常：{exc}")

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Bot-Token": self.token}

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
        return f"AstrBot-{socket.gethostname()[:8] or 'bot'}-{random.randint(1000, 9999)}"

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
