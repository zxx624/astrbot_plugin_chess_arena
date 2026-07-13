from __future__ import annotations

import asyncio
import glob
import json
import os
import socket
import inspect
import random
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
import hashlib
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

@dataclass
class ArenaState:
    connected: bool = False
    last_event_at: float | None = None
    last_error: str = ""
    reconnect_count: int = 0
    accepted_challenges: int = 0
    submitted_moves: int = 0


@dataclass
class CardRoomDecisionSession:
    """CardRoom 斗地主 LLM 上下文决策会话（进程内，不持久化）。"""
    room_id: str = ""
    seat: str = ""
    last_state_hash: str = ""
    turn_count: int = 0
    history_summary: list[str] = __import__("dataclasses").field(default_factory=list)
    persona: str = ""
    last_errors: list[str] = __import__("dataclasses").field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def key(self) -> str:
        return f"{self.room_id}:{self.seat}"


class ChessArenaPlugin(Star):
    """AstrBot 棋擂台客户端：自动注册、SSE 接入、自动接挑战、合法走法和台词。"""

    # ── CardRoom LLM decision ──────────────────────────────────────────────
    _CARDROOM_DECISION_SESSION_PROMPT: str = (
        "你是一个斗地主 Bot，参与了三人 CardRoom 牌局。\n"
        "- 你只能看到自己的手牌和其他人的手牌数量，不能猜测对手具体手牌。\n"
        "- 你只能从本回合候选列表中原样选择 action_id，不能自造或改写 cards。\n"
        "- 只输出严格 JSON：{\"candidates\":[{\"action_id\":\"...\",\"reason\":\"...\",\"speech\":\"...\"}]}。\n"
        "- candidates 按优先级排列，最多 3 个且 action_id 不重复；每项必须给出 reason 和 speech。\n"
        "- 优先保留炸弹和火箭，除非必走。\n"
        "- 如果能 pass 且不值得出牌，就 pass。"
    )
    # ────────────────────────────────────────────────────────────────────────

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.arena_base = str(self.config.get("arena_base") or "https://gulu624.icu").rstrip("/")
        self.card_arena_base = str(
            self.config.get("cardroom_base_url")
            or self.config.get("card_arena_base")
            or "https://gulu624.icu"
        ).rstrip("/")
        self.cardroom_enabled = self._config_bool(self.config.get("cardroom_enabled"), default=True)
        self.cardroom_poll_interval = max(1.0, float(self.config.get("cardroom_poll_interval") or 5))
        self.cardroom_prompt_decision_enabled = self._config_bool(self.config.get("cardroom_prompt_decision_enabled"), default=True)
        self.cardroom_prompt_max_retries = max(0, min(5, int(self.config.get("cardroom_prompt_max_retries") or 5)))
        self.cardroom_seats = self._parse_cardroom_seats(self.config.get("cardroom_seats"))
        self.cardroom_pool_bindings = self._parse_cardroom_pool_bindings(self.config.get("cardroom_pool_bindings"))

        # CardRoom LLM decision (public default ON; invalid/timeout responses still fall back safely)
        self.cardroom_llm_decision_enabled = self._config_bool(self.config.get("cardroom_llm_decision_enabled"), default=True)
        self.cardroom_context_enabled = self._config_bool(self.config.get("cardroom_context_enabled"), default=True)
        self.cardroom_context_max_history = max(1, int(self.config.get("cardroom_context_max_history") or 6))
        self.cardroom_persona_prompt = str(self.config.get("cardroom_persona_prompt") or "你是斗地主 Bot。出牌自然、有一点胜负欲。").strip()

        # Go9 engine adapter (default OFF -> current random/pass fallback)
        self.go_engine_enabled = self._config_bool(self.config.get("go_engine_enabled"), default=False)
        self.go_engine_endpoint = str(self.config.get("go_engine_endpoint") or "http://127.0.0.1:8787/api/go9/analyze").strip()
        self.go_engine_timeout_sec = max(1.0, float(self.config.get("go_engine_timeout_sec") or 5))
        self.go_engine_fallback_random = self._config_bool(self.config.get("go_engine_fallback_random"), default=True)

        self.arena_fallback_bases = self._parse_fallback_bases(self.config.get("arena_fallback_bases"))
        self.token = str(self.config.get("token") or "").strip()
        self.auto_register = bool(self.config.get("auto_register", True))
        self.server_profile: dict[str, Any] = {}
        self._generated_bot_name = not str(self.config.get("bot_name") or "").strip()
        self.bot_name = self._default_bot_name()
        self.avatar_url = ""
        self.description = "AstrBot Chess Arena bot"
        self.chess_style = "random"
        self.persona_prompt = "像群里真人下棋，自然、松弛、有一点胜负欲，不要像客服。"
        self.commentary_enabled = bool(self.config.get("commentary_enabled", True))
        self.commentary_timeout_sec = int(self.config.get("commentary_timeout_sec") or 8)
        self.llm_provider_mode = str(self.config.get("llm_provider_mode") or "default").strip().lower()
        if self.llm_provider_mode not in {"default", "custom"}:
            self.llm_provider_mode = "default"
        self.llm_provider_id = str(self.config.get("llm_provider_id") or "").strip()
        self.llm_tools_enabled = self._config_bool(self.config.get("llm_tools_enabled"), default=True)
        self.llm_tools_allow_actions = self._config_bool(self.config.get("llm_tools_allow_actions"), default=False)
        self.enabled_games = self._parse_enabled_games(self.config.get("enabled_games"))
        self.default_game = self._normalize_game(self.config.get("default_game"))
        if self.default_game not in self.enabled_games:
            self.enabled_games.insert(0, self.default_game)
        self.auto_accept_challenges = bool(self.config.get("auto_accept_challenges", True))
        self.challenge_decision_mode = self._normalize_challenge_decision_mode(self.config.get("challenge_decision_mode"))
        self.server_challenge_policy = self._server_challenge_policy_for_mode(self.challenge_decision_mode)
        self.owner_notify_enabled = self._config_bool(self.config.get("owner_notify_enabled"), default=True)
        self.owner_notify_targets = str(self.config.get("owner_notify_targets") or "").strip()
        self.owner_decision_timeout_sec = max(1, int(self.config.get("owner_decision_timeout_sec") or 180))
        self.match_report_enabled = self._config_bool(self.config.get("match_report_enabled"), default=True)
        self.pending_owner_challenges: dict[str, dict[str, Any]] = {}
        self.active_matches: dict[str, dict[str, Any]] = {}
        self.recent_finished_matches: list[dict[str, Any]] = []
        self.notified_challenges: set[str] = set()
        self.finished_games: set[str] = set()
        self._notify_tasks: set[asyncio.Task] = set()
        self.engine_mode = self._normalize_engine_mode(self.config.get("engine_mode") or "auto")
        self.engine_depth = int(self.config.get("engine_depth") or 3)
        self.engine_timeout_sec = max(1, int(self.config.get("engine_timeout_sec") or 8))
        self.custom_engine_command = str(self.config.get("custom_engine_command") or "").strip()
        self.custom_engine_http_url = str(self.config.get("custom_engine_http_url") or "").strip()
        self.custom_engine_http_headers = self.config.get("custom_engine_http_headers") or ""
        self.local_engine_node_path = str(self.config.get("local_engine_node_path") or "node").strip() or "node"
        self.move_timeout_sec = int(self.config.get("move_timeout_sec") or 10)
        self.announce_to_current_chat = bool(self.config.get("announce_to_current_chat", False))
        self.verbose_logging = self._config_bool(self.config.get("verbose_logging"), default=False)

        self.state = ArenaState()
        self._cardroom_sessions: dict[str, CardRoomDecisionSession] = {}
        self._session: aiohttp.ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._cardroom_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

        self._startup_task = asyncio.create_task(self._startup(), name="chess_arena_startup")

    async def _startup(self) -> None:
        """启动流程：必要时自动注册，验证 token，按配置同步/拉取 profile，然后连接 SSE。"""
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

            self._routine_log("[ChessArena] 已读取网站端 profile；Bot 资料统一由网站管理，插件端不覆盖。")
            self._sse_task = asyncio.create_task(self._sse_loop(), name="chess_arena_sse_loop")
            if self.cardroom_enabled:
                self._cardroom_task = asyncio.create_task(self._cardroom_poll_loop(), name="chess_arena_cardroom_poll_loop")
            self._routine_log(
                "[ChessArena] SSE 客户端已启动: %s bot=%s token=%s",
                self.arena_base,
                self.effective_bot_name,
                self._token_hint(self.token),
            )
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

    @staticmethod
    def _parse_cardroom_seats(value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                raw = json.loads(value)
            except json.JSONDecodeError:
                raw = []
        else:
            raw = value
        if isinstance(raw, dict):
            raw = [raw]
        seats: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                room_id = str(item.get("room_id") or "").strip()
                seat = str(item.get("seat") if item.get("seat") is not None else "").strip()
                token = str(item.get("token") or "").strip()
                if room_id and seat:
                    seats.append({"room_id": room_id, "seat": seat, "token": token})
        return seats

    @staticmethod
    def _parse_cardroom_pool_bindings(value: Any) -> list[dict[str, Any]]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            try:
                raw = json.loads(value)
            except json.JSONDecodeError:
                raw = []
        else:
            raw = value
        if isinstance(raw, dict):
            raw = [raw]
        bindings: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return bindings
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                slot = max(1, min(5, int(item.get("slot") or 0)))
            except (TypeError, ValueError):
                continue
            seat_token = str(item.get("seat_token") or item.get("token") or "").strip()
            controller_id = str(item.get("controller_id") or "").strip()
            if not seat_token or not controller_id:
                continue
            bindings.append({
                "slot": slot,
                "controller_id": controller_id[:160],
                "seat": str(item.get("seat") if item.get("seat") is not None else "0"),
                "seat_token": seat_token,
                "room_id": str(item.get("room_id") or "").strip(),
                "status": str(item.get("status") or "waiting").strip() or "waiting",
            })
        return bindings

    _DUEL_GAMES = {"xiangqi", "go"}
    _CAPABILITY_GAMES = {"xiangqi", "go", "doudizhu"}
    _GAME_ALIASES = {
        "xiangqi": "xiangqi",
        "象棋": "xiangqi",
        "中国象棋": "xiangqi",
        "chess": "xiangqi",
        "go": "go",
        "doudizhu": "doudizhu",
        "\u6597\u5730\u4e3b": "doudizhu",
        "\u6597\u5730\u4e3b\u724c": "doudizhu",
        "围棋": "go",
        "围棋9路": "go",
        "9路围棋": "go",
    }

    @classmethod
    def _game_alias_to_id(cls, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return cls._GAME_ALIASES.get(text, text if text in cls._CAPABILITY_GAMES else "")

    def _parse_enabled_games(self, value: Any) -> list[str]:
        if value is None or (isinstance(value, str) and not value.strip()):
            raw_items: list[Any] = ["xiangqi", "go", "doudizhu"]
        elif isinstance(value, str):
            text = value.strip()
            raw_items = []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        raw_items = parsed
                except json.JSONDecodeError:
                    raw_items = []
            if not raw_items:
                raw_items = text.replace("\n", ",").replace("，", ",").replace(";", ",").replace("；", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = []

        games: list[str] = []
        for item in raw_items:
            game = self._game_alias_to_id(item)
            if game and game not in games:
                games.append(game)
        if "xiangqi" not in games:
            games.append("xiangqi")
        return games

    def _normalize_game(self, value: Any = None) -> str:
        raw = value
        if raw is None or str(raw).strip() == "":
            raw = getattr(self, "default_game", "xiangqi")
        game = self._game_alias_to_id(raw)
        enabled_games = list(getattr(self, "enabled_games", []) or [])
        if game in self._DUEL_GAMES and game in enabled_games:
            return game

        fallback = self._game_alias_to_id(getattr(self, "default_game", "xiangqi"))
        if fallback in self._DUEL_GAMES and fallback in enabled_games:
            return fallback
        return "xiangqi"

    @staticmethod
    def _config_bool(value: Any, *, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on", "启用", "是"}:
            return True
        if text in {"0", "false", "no", "n", "off", "禁用", "否"}:
            return False
        return default

    def _routine_log(self, message: str, *args: Any) -> None:
        """日常运行日志默认走 DEBUG；开启 verbose_logging 后提升到 INFO。"""
        if self.verbose_logging:
            logger.info(message, *args)
        else:
            logger.debug(message, *args)

    def _llm_tools_disabled_message(self) -> str:
        return "棋擂台 LLM 工具未启用：请在插件配置 llm_tools_enabled 开启。"

    @filter.llm_tool(name="chess_arena_status")
    async def chess_arena_status(self, event: AstrMessageEvent) -> str:
        """查看棋擂台连接状态、Bot、平台、引擎链和待确认挑战数量。

        Args:
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._llm_tool_status()

    @filter.llm_tool(name="chess_arena_find_bots")
    async def chess_arena_find_bots(self, event: AstrMessageEvent, query: str = "", game: str = "") -> str:
        """查询或列出可挑战的棋擂台 Bot，最多返回 8 个。

        Args:
            query(string): Bot 名称或 bot_id 关键词；空则列出可见 Bot。
            game(string): 游戏类型；默认 xiangqi，可选 xiangqi/go/围棋。只接受当前平台已接入的双人棋类。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._llm_tool_find_bots(query=query, game=game)

    @filter.llm_tool(name="chess_arena_challenge")
    async def chess_arena_challenge(self, event: AstrMessageEvent, opponent: str, side: str = "random", game: str = "") -> str:
        """按名字或 bot_id 向棋擂台 Bot 发起挑战。

        Args:
            opponent(string): 对手 Bot 名称或 bot_id。
            side(string): 我方执红/黑/随机；允许 red、black、random、红、黑。
            game(string): 游戏类型；默认 xiangqi，可选 xiangqi/go/围棋。斗地主不是双人棋类，不能用这个工具发起。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._llm_tool_challenge(opponent=opponent, side=side, game=game)

    @filter.llm_tool(name="chess_arena_pending_challenges")
    async def chess_arena_pending_challenges(self, event: AstrMessageEvent) -> str:
        """列出等待主人审批的棋擂台挑战。

        Args:
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._llm_tool_pending_challenges()

    @filter.llm_tool(name="chess_arena_owner_decision")
    async def chess_arena_owner_decision(
        self,
        event: AstrMessageEvent,
        decision: str,
        challenge_id: str = "",
        reason: str = "",
    ) -> str:
        """同意或拒绝一条等待主人审批的棋擂台挑战；不传 challenge_id 时处理最新一条。

        Args:
            decision(string): 只能是 accept 或 reject；必须显式传入，避免误同意挑战。
            challenge_id(string): 挑战 ID；为空时默认最新一条待确认。
            reason(string): 拒绝原因，可为空；不要包含隐私或凭据。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._llm_tool_owner_decision(challenge_id=challenge_id, decision=decision, reason=reason)

    @filter.llm_tool(name="card_arena_create_room")
    async def card_arena_create_room(self, event: AstrMessageEvent, seed: int = 0, landlord_index: int = 0) -> str:
        """在 9191 沙箱创建一个 CardRoom 斗地主房间；这是三人扑克房间，不是 8787 正式服棋类 Match。

        Args:
            seed(number): 可选随机种子；0 表示后端随机。
            landlord_index(number): 地主座位，0/1/2，默认 0。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._card_tool_create_room(seed=seed, landlord_index=landlord_index)

    @filter.llm_tool(name="card_arena_get_room")
    async def card_arena_get_room(self, event: AstrMessageEvent, room_id: str, seat: str = "0") -> str:
        """按指定 seat 查看 9191 CardRoom 斗地主房间的 LLM 视角；只返回自己的手牌和其他人的手牌数量。

        Args:
            room_id(string): CardRoom 房间 ID。
            seat(string): 座位 0/1/2 或 seat0/seat1/seat2；只能看到该 seat 的视角。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._card_tool_get_room(room_id=room_id, seat=seat)

    @filter.llm_tool(name="card_arena_get_legal_actions")
    async def card_arena_get_legal_actions(self, event: AstrMessageEvent, room_id: str, seat: str = "0") -> str:
        """查询 9191 CardRoom 斗地主当前 seat 的合法动作摘要，供 LLM 判断出牌或 pass。

        Args:
            room_id(string): CardRoom 房间 ID。
            seat(string): 座位 0/1/2 或 seat0/seat1/seat2。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        return await self._card_tool_get_legal_actions(room_id=room_id, seat=seat)

    @filter.llm_tool(name="card_arena_play")
    async def card_arena_play(self, event: AstrMessageEvent, room_id: str, seat: str, cards: str, reason: str = "") -> str:
        """向 9191 CardRoom 斗地主裁判提交出牌；后端会审查规则，非法时返回 code/message/legal_hint/attempt，LLM 最多重试 5 次。

        Args:
            room_id(string): CardRoom 房间 ID。
            seat(string): 座位 0/1/2 或 seat0/seat1/seat2。
            cards(string): 要出的牌，逗号或空格分隔，例如 "9S,9H"。必须来自该 seat 的 my_hand。
            reason(string): 简短出牌理由，会记录到 action_history。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        if not self.llm_tools_allow_actions:
            return "LLM 工具操作权限未开启：只能查询，不能提交斗地主出牌。"
        return await self._card_tool_play(room_id=room_id, seat=seat, cards=cards, reason=reason)

    @filter.llm_tool(name="card_arena_pass")
    async def card_arena_pass(self, event: AstrMessageEvent, room_id: str, seat: str, reason: str = "") -> str:
        """向 9191 CardRoom 斗地主裁判提交 pass；新一轮不能 pass，非法时返回结构化错误。

        Args:
            room_id(string): CardRoom 房间 ID。
            seat(string): 座位 0/1/2 或 seat0/seat1/seat2。
            reason(string): 简短 pass 理由，会记录到 action_history。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        if not self.llm_tools_allow_actions:
            return "LLM 工具操作权限未开启：只能查询，不能提交斗地主 pass。"
        return await self._card_tool_pass(room_id=room_id, seat=seat, reason=reason)

    @filter.llm_tool(name="card_arena_prompt_decision")
    async def card_arena_prompt_decision(
        self,
        event: AstrMessageEvent,
        room_id: str,
        seat: str = "0",
        action: str = "",
        cards: str = "",
        speech: str = "",
        reason: str = "",
    ) -> str:
        """提交一次斗地主 Prompt 决策；网站端会先按 private view/legal-actions 审核，非法最多重试/回退。

        Args:
            room_id(string): CardRoom 房间 ID。
            seat(string): 座位 0/1/2 或 seat0/seat1/seat2。
            action(string): play 或 pass。为空时插件从 legal-actions 选最小合法动作。
            cards(string): action=play 时的牌，逗号或空格分隔。
            speech(string): Bot 台词，写入 action_history，最长 300 字。
            reason(string): 简短决策理由。
        """
        if not self.llm_tools_enabled:
            return self._llm_tools_disabled_message()
        if not self.llm_tools_allow_actions:
            return "LLM 工具操作权限未开启：只能查询，不能提交斗地主 Prompt 决策。"
        return await self._card_tool_prompt_decision(room_id=room_id, seat=seat, action=action, cards=cards, speech=speech, reason=reason)

    def _register_llm_tools_safe(self) -> None:
        """兼容旧调用点：LLM 工具由 @filter.llm_tool 标准装饰器注册。"""
        return None

    def _llm_tool_result(self, value: Any, tool_exec_result_cls: Any = None) -> Any:
        text = self._safe_llm_tool_text(value)
        if tool_exec_result_cls is not None:
            try:
                return tool_exec_result_cls(text)
            except Exception:  # noqa: BLE001
                pass
        return text

    def _safe_llm_tool_text(self, value: Any, limit: int = 700) -> str:
        text = str(value or "").strip()
        if self.token:
            text = text.replace(self.token, "[token-redacted]")
            text = text.replace(self._token_hint(self.token), "[token-redacted]")
        text = " ".join(text.split()) if "\n" not in text[:200] else text.strip()
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text or "完成。"

    async def _llm_tool_status(self) -> str:
        try:
            status = "在线" if self.state.connected else "离线"
            pending_count = len(self._pending_challenge_lines())
            engine_chain = " -> ".join(self._engine_chain())
            active_lines = self._active_match_lines()
            recent_line = self._latest_finished_match_line()
            return (
                f"连接：{status}\n"
                f"Bot：{self.effective_bot_name}\n"
                f"平台：{self.arena_base}\n"
                f"引擎链：{engine_chain}\n"
                f"待确认：{pending_count}\n"
                f"进行中：{len(active_lines)}"
                + (("\n" + "\n".join(active_lines[:3])) if active_lines else "")
                + (("\n最近结束：" + recent_line) if recent_line else "")
            )
        except Exception as exc:  # noqa: BLE001
            return f"查询状态失败：{exc}"

    async def _llm_tool_find_bots(self, query: str = "", game: str = "") -> str:
        try:
            normalized_game = self._normalize_game(game)
            bots = [bot for bot in await self._fetch_bots(str(query or "").strip(), game=normalized_game) if not self._is_self_bot(bot)]
            bots = sorted(bots, key=self._bot_priority, reverse=True)[:8]
            if not bots:
                return f"未找到可列出的 Bot（game={normalized_game}）。"
            lines = []
            for bot in bots:
                name = self._bot_name(bot) or "未命名"
                bot_id = self._bot_id(bot) or "未知ID"
                online = "在线" if self._bot_online(bot) else "离线"
                available = "可用" if self._bot_available(bot) else "不可用"
                lines.append(f"- {name} ({bot_id})：{online}/{available}")
            return f"可挑战 Bot（game={normalized_game}）：\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"查询 Bot 失败：{exc}"

    async def _llm_tool_challenge(self, opponent: str = "", side: str = "random", game: str = "") -> str:
        try:
            if not self.llm_tools_allow_actions:
                return "LLM 工具操作权限未开启：只能查询，不能发起挑战。"
            opponent = str(opponent or "").strip()
            if not opponent:
                return "请提供 opponent（名字或 bot_id）。"
            if not self.token:
                return "Token 未配置，无法发起挑战。"
            normalized_game = self._normalize_game(game)
            parsed_side = self._parse_side(side)
            bot, error = await self._find_bot(opponent, game=normalized_game)
            if error or not bot:
                return error or "没找到对手。"
            return await self._challenge_bot(bot, parsed_side, game=normalized_game)
        except Exception as exc:  # noqa: BLE001
            return f"挑战失败：{exc}"

    async def _llm_tool_pending_challenges(self) -> str:
        try:
            lines = self._pending_challenge_lines()
            if not lines:
                return "当前没有待确认挑战。"
            return "待确认挑战：\n" + "\n".join(lines[:8])
        except Exception as exc:  # noqa: BLE001
            return f"查询待确认失败：{exc}"

    async def _llm_tool_owner_decision(self, challenge_id: str = "", decision: str = "", reason: str = "") -> str:
        try:
            if not self.llm_tools_allow_actions:
                return "LLM 工具操作权限未开启：只能查询，不能同意/拒绝挑战。"
            decision = str(decision or "").strip().lower()
            if decision not in {"accept", "reject"}:
                return "decision 必须显式填写 accept 或 reject。"
            cid = str(challenge_id or self._latest_pending_challenge_id()).strip()
            if not cid:
                return "当前没有待确认挑战。"
            clean_reason = str(reason or "").strip()[:120]
            data = await self._submit_owner_decision(cid, decision, reason=clean_reason if decision == "reject" else "")
            self.pending_owner_challenges.pop(cid, None)
            if decision == "accept":
                self.state.accepted_challenges += 1
            return self._format_owner_decision_result(cid, decision, data)
        except Exception as exc:  # noqa: BLE001
            return f"处理挑战决定失败：{exc}"

    def _normalize_challenge_decision_mode(self, value: Any) -> str:
        """兼容旧 auto_accept_challenges：未显式配置新模式时按旧布尔值映射。"""
        raw = str(value or "").strip().lower()
        if raw in {"auto_accept", "owner_approve", "ignore"}:
            return raw
        return "auto_accept" if self.auto_accept_challenges else "ignore"

    @staticmethod
    def _server_challenge_policy_for_mode(mode: str) -> str:
        if mode == "owner_approve":
            return "manual_approve"
        if mode == "ignore":
            return "reject_all"
        return "auto_accept"

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

    async def _api_json(
        self,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> tuple[str, int, dict[str, Any] | list[Any], str]:
        base, status, text = await self._request_text_with_fallback(
            method,
            path,
            json_payload=json_payload,
            headers=headers,
            timeout=timeout,
        )
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        return base, status, data, text

    async def _card_api_json(
        self,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any], str]:
        """Call CardRoom APIs only; never uses spectator for bot decisions."""
        session = await self._get_session()
        url = f"{self.card_arena_base}{path}"
        async with session.request(method, url, json=json_payload, timeout=timeout or aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {}
            return resp.status, data, text

    @staticmethod
    def _card_parse_seat(value: Any) -> str:
        text = str(value if value is not None else "0").strip().lower()
        if text.startswith("seat"):
            text = text[4:]
        if text not in {"0", "1", "2"}:
            text = "0"
        return text

    @staticmethod
    def _card_parse_cards(value: Any) -> list[str]:
        if isinstance(value, list):
            raw = value
        else:
            text = str(value or "").replace("，", ",").replace("、", ",").replace(";", ",")
            raw = []
            for chunk in text.split(","):
                raw.extend(str(chunk).split())
        return [str(card).strip().upper() for card in raw if str(card).strip()]

    @staticmethod
    def _card_json_text(data: Any, limit: int = 1800) -> str:
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text

    @staticmethod
    def _card_error_text(prefix: str, status: int, data: Any, raw_text: str) -> str:
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            return (
                f"{prefix}失败：HTTP {status}\n"
                f"code={detail.get('code') or ''}\n"
                f"message={detail.get('message') or ''}\n"
                f"legal_hint={detail.get('legal_hint') or ''}\n"
                f"attempt={json.dumps(detail.get('attempt') or {}, ensure_ascii=False)}\n"
                "请重新读取 card_arena_get_room 和 card_arena_get_legal_actions 后再选择；非法最多重试 5 次。"
            )
        message = detail if detail is not None else raw_text[:300]
        return f"{prefix}失败：HTTP {status} {message}"

    def _cardroom_controller_identity(self) -> tuple[str, str]:
        controller_id = self._my_bot_id() or self.effective_bot_name or self._token_hint(self.token) or "astrbot"
        display_name = self.effective_bot_name or controller_id
        return str(controller_id)[:160], str(display_name)[:80]

    def _upsert_cardroom_pool_binding(self, binding: dict[str, Any]) -> None:
        slot = int(binding.get("slot") or 0)
        self.cardroom_pool_bindings = [item for item in self.cardroom_pool_bindings if int(item.get("slot") or 0) != slot]
        self.cardroom_pool_bindings.append(dict(binding))
        self.cardroom_pool_bindings.sort(key=lambda item: int(item.get("slot") or 0))
        self.config["cardroom_pool_bindings"] = json.dumps(self.cardroom_pool_bindings, ensure_ascii=False, separators=(",", ":"))

    def _drop_cardroom_pool_binding(self, slot: Any) -> bool:
        slot_num = max(1, min(5, int(str(slot or "1").strip())))
        before = len(self.cardroom_pool_bindings)
        self.cardroom_pool_bindings = [item for item in self.cardroom_pool_bindings if int(item.get("slot") or 0) != slot_num]
        changed = len(self.cardroom_pool_bindings) != before
        if changed:
            self.config["cardroom_pool_bindings"] = json.dumps(self.cardroom_pool_bindings, ensure_ascii=False, separators=(",", ":"))
        return changed

    async def _card_tool_pool_status(self) -> str:
        try:
            status, data, text = await self._card_api_json("GET", "/api/card-rooms/pool")
            if status >= 400:
                return self._card_error_text("获取斗地主房间池", status, data, text)
            slots = data.get("slots") if isinstance(data, dict) else data
            if not isinstance(slots, list):
                return "斗地主房间池：\n" + self._card_json_text(data)
            lines = ["斗地主房间池（1-5）："]
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                seats = slot.get("seats") if isinstance(slot.get("seats"), list) else []
                names: list[str] = []
                for seat in seats:
                    if isinstance(seat, dict):
                        names.append(str(seat.get("display_name") or seat.get("controller_id") or f"seat{seat.get('seat', '')}"))
                status_text = str(slot.get("status") or "waiting")
                room_id = str(slot.get("room_id") or "")
                suffix = f" room={room_id}" if room_id else ""
                lines.append(f"{slot.get('slot')}: {status_text} {len(seats)}/3 {'、'.join(names) or '空'}{suffix}")
            lines.append("命令：斗地主加入 <1-5> / 斗地主退出 <1-5> / 斗地主开始 <1-5> / 斗地主状态")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"获取斗地主房间池失败：{exc}"

    async def _card_tool_pool_join(self, slot: Any) -> str:
        try:
            slot_num = max(1, min(5, int(str(slot or "1").strip())))
            controller_id, display_name = self._cardroom_controller_identity()
            payload = {"token": self.token, "display_name": display_name}
            status, data, text = await self._card_api_json("POST", f"/api/card-rooms/pool/{slot_num}/join-token", json_payload=payload)
            if status >= 400:
                return self._card_error_text(f"加入斗地主房间 {slot_num}", status, data, text)
            seat_data = data.get("seat") if isinstance(data, dict) and isinstance(data.get("seat"), dict) else {}
            binding = {
                "slot": slot_num,
                "controller_id": controller_id,
                "seat": str(seat_data.get("seat") if seat_data.get("seat") is not None else "0"),
                "seat_token": str(data.get("seat_token") or ""),
                "room_id": str(data.get("room_id") or ""),
                "status": str((data.get("slot") or {}).get("status") or "waiting"),
            }
            if not binding["seat_token"]:
                return "加入斗地主房间失败：网站没有返回 seat token。"
            self._upsert_cardroom_pool_binding(binding)
            await self._save_runtime_config()
            slot_data = data.get("slot") if isinstance(data, dict) and isinstance(data.get("slot"), dict) else data
            msg = f"已加入斗地主房间 {slot_num}：{display_name}。"
            if isinstance(slot_data, dict):
                msg += f"\n状态：{slot_data.get('status')}，人数：{len(slot_data.get('seats') or [])}/3"
                if slot_data.get("room_id"):
                    msg += f"\n牌局：{slot_data.get('room_id')}"
            return msg + "\n" + await self._card_tool_pool_status()
        except Exception as exc:  # noqa: BLE001
            return f"加入斗地主房间失败：{exc}"

    async def _card_tool_pool_leave(self, slot: Any) -> str:
        try:
            slot_num = max(1, min(5, int(str(slot or "1").strip())))
            payload = {"token": self.token}
            status, data, text = await self._card_api_json("POST", f"/api/card-rooms/pool/{slot_num}/leave-token", json_payload=payload)
            if status >= 400:
                return self._card_error_text(f"退出斗地主房间 {slot_num}", status, data, text)
            if self._drop_cardroom_pool_binding(slot_num):
                await self._save_runtime_config()
            return f"已退出斗地主房间 {slot_num}。\n" + await self._card_tool_pool_status()
        except Exception as exc:  # noqa: BLE001
            return f"退出斗地主房间失败：{exc}"

    async def _card_tool_pool_start(self, slot: Any) -> str:
        slot_num = max(1, min(5, int(str(slot or "1").strip())))
        return f"斗地主房间 {slot_num} 满 3 人后会自动开局，无需管理员手动开始。"

    async def _card_tool_create_room(self, seed: int = 0, landlord_index: int = 0) -> str:
        try:
            if not self.llm_tools_allow_actions:
                return "LLM 工具操作权限未开启：只能查询，不能创建斗地主房间。"
            payload: dict[str, Any] = {"game": "doudizhu", "landlord_index": max(0, min(2, int(landlord_index or 0)))}
            if int(seed or 0) != 0:
                payload["seed"] = int(seed)
            status, data, text = await self._card_api_json("POST", "/api/card-rooms", json_payload=payload)
            if status >= 400:
                return self._card_error_text("创建 CardRoom", status, data, text)
            room_id = data.get("room_id") if isinstance(data, dict) else ""
            return (
                f"已在 9191 沙箱创建斗地主房间：{room_id}\n"
                f"平台：{self.card_arena_base}\n"
                "下一步用 card_arena_get_room(room_id, seat) 看自己的牌，再用 card_arena_get_legal_actions 判断。"
            )
        except Exception as exc:  # noqa: BLE001
            return f"创建 CardRoom 失败：{exc}"

    @staticmethod
    def _append_cardroom_token(path: str, token: str | None) -> str:
        token = str(token or "").strip()
        if not token:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}token={quote(token, safe='')}"

    async def _card_tool_get_room(self, room_id: str, seat: Any = "0", token: str | None = None) -> str:
        try:
            rid = quote(str(room_id or "").strip(), safe="")
            if not rid:
                return "请提供 room_id。"
            seat_id = self._card_parse_seat(seat)
            path = self._append_cardroom_token(f"/api/card-rooms/{rid}/view?seat={seat_id}", token)
            status, data, text = await self._card_api_json("GET", path)
            if status >= 400:
                return self._card_error_text("获取 CardRoom 视角", status, data, text)
            return "CardRoom seat 视角（不会泄露其他玩家手牌）：\n" + self._card_json_text(data)
        except Exception as exc:  # noqa: BLE001
            return f"获取 CardRoom 视角失败：{exc}"

    async def _card_tool_get_legal_actions(self, room_id: str, seat: Any = "0", token: str | None = None) -> str:
        try:
            rid = quote(str(room_id or "").strip(), safe="")
            if not rid:
                return "请提供 room_id。"
            seat_id = self._card_parse_seat(seat)
            path = self._append_cardroom_token(f"/api/card-rooms/{rid}/legal-actions?seat={seat_id}", token)
            status, data, text = await self._card_api_json("GET", path)
            if status >= 400:
                return self._card_error_text("获取合法动作", status, data, text)
            return "CardRoom 合法动作摘要：\n" + self._card_json_text(data)
        except Exception as exc:  # noqa: BLE001
            return f"获取合法动作失败：{exc}"

    async def _card_tool_play(self, room_id: str, seat: Any, cards: Any, reason: str = "", token: str | None = None) -> str:
        try:
            rid = quote(str(room_id or "").strip(), safe="")
            if not rid:
                return "请提供 room_id。"
            card_list = self._card_parse_cards(cards)
            if not card_list:
                return "cards 不能为空；如果不出请用 card_arena_pass。"
            payload = {
                "seat": self._card_parse_seat(seat),
                "action": "play",
                "cards": card_list,
                "source": "astrbot_llm",
                "reason": str(reason or "")[:500],
            }
            if token:
                payload["token"] = str(token)
            status, data, text = await self._card_api_json("POST", f"/api/card-rooms/{rid}/actions", json_payload=payload)
            if status >= 400:
                return self._card_error_text("出牌", status, data, text)
            return "出牌成功，网站裁判已审查并写入 SQLite：\n" + self._card_json_text({"move": data.get("move"), "state": data.get("state")}, limit=1600)
        except Exception as exc:  # noqa: BLE001
            return f"出牌失败：{exc}"

    async def _card_tool_pass(self, room_id: str, seat: Any, reason: str = "", token: str | None = None) -> str:
        try:
            rid = quote(str(room_id or "").strip(), safe="")
            if not rid:
                return "请提供 room_id。"
            payload = {
                "seat": self._card_parse_seat(seat),
                "action": "pass",
                "cards": [],
                "source": "astrbot_llm",
                "reason": str(reason or "")[:500],
            }
            if token:
                payload["token"] = str(token)
            status, data, text = await self._card_api_json("POST", f"/api/card-rooms/{rid}/actions", json_payload=payload)
            if status >= 400:
                return self._card_error_text("Pass", status, data, text)
            return "pass 成功，网站裁判已审查并写入 SQLite：\n" + self._card_json_text({"move": data.get("move"), "state": data.get("state")}, limit=1600)
        except Exception as exc:  # noqa: BLE001
            return f"pass 失败：{exc}"

    def _active_cardroom_bindings(self) -> list[dict[str, Any]]:
        bindings = [dict(item) for item in self.cardroom_seats]
        for item in self.cardroom_pool_bindings:
            room_id = str(item.get("room_id") or "").strip()
            if not room_id:
                continue
            bindings.append({
                "slot": item.get("slot"),
                "room_id": room_id,
                "seat": item.get("seat"),
                "token": item.get("seat_token"),
            })
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for binding in bindings:
            key = (str(binding.get("room_id") or ""), str(binding.get("seat") or ""))
            if key[0] and key not in seen:
                seen.add(key)
                deduped.append(binding)
        return deduped

    async def _cardroom_reconcile_pool_bindings(self) -> None:
        if not self.cardroom_pool_bindings:
            return
        status, data, text = await self._card_api_json("GET", "/api/card-rooms/pool")
        if status >= 400:
            logger.warning("[ChessArena] CardRoom pool reconcile failed HTTP %s %s", status, text[:120])
            return
        slots = data.get("slots") if isinstance(data, dict) else None
        if not isinstance(slots, list):
            return
        slot_map = {int(item.get("slot") or 0): item for item in slots if isinstance(item, dict)}
        changed = False
        next_bindings: list[dict[str, Any]] = []
        for raw in self.cardroom_pool_bindings:
            binding = dict(raw)
            slot = slot_map.get(int(binding.get("slot") or 0))
            if not slot:
                next_bindings.append(binding)
                continue
            slot_status = str(slot.get("status") or "waiting")
            seats = slot.get("seats") if isinstance(slot.get("seats"), list) else []
            own = next((seat for seat in seats if isinstance(seat, dict) and str(seat.get("controller_id") or "") == str(binding.get("controller_id") or "")), None)
            if slot_status == "finished" or (slot_status == "waiting" and own is None):
                changed = True
                continue
            updated = dict(binding)
            updated["status"] = slot_status
            updated["room_id"] = str(slot.get("room_id") or "")
            if own is not None:
                updated["seat"] = str(own.get("seat") if own.get("seat") is not None else updated.get("seat") or "0")
            if updated != binding:
                changed = True
            next_bindings.append(updated)
        if changed:
            self.cardroom_pool_bindings = next_bindings
            self.config["cardroom_pool_bindings"] = json.dumps(next_bindings, ensure_ascii=False, separators=(",", ":"))
            await self._save_runtime_config()

    @staticmethod
    def _cardroom_default_speech(selected: dict[str, Any]) -> str:
        existing = str(selected.get("speech") or "").strip()
        if existing:
            return existing[:300]
        if str(selected.get("action") or "").lower() == "pass":
            return "这手先过。"
        cards = [str(card) for card in selected.get("cards") or []]
        if cards:
            return "我出这手。"
        return "轮到我了。"

    async def _cardroom_poll_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._cardroom_reconcile_pool_bindings()
                for binding in self._active_cardroom_bindings():
                    await self._cardroom_maybe_act(binding)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ChessArena] CardRoom poll failed: %s", exc)
            await asyncio.sleep(self.cardroom_poll_interval)

    async def _cardroom_maybe_act(self, binding: dict[str, Any]) -> None:
        room_id = str(binding.get("room_id") or "").strip()
        seat = self._card_parse_seat(binding.get("seat"))
        token = str(binding.get("token") or binding.get("seat_token") or "").strip()
        if not room_id:
            return
        rid = quote(room_id, safe="")
        view_path = self._append_cardroom_token(f"/api/card-rooms/{rid}/view?seat={seat}", token)
        legal_path = self._append_cardroom_token(f"/api/card-rooms/{rid}/legal-actions?seat={seat}", token)
        v_status, view, v_text = await self._card_api_json("GET", view_path)
        if v_status >= 400:
            logger.warning("[ChessArena] CardRoom view failed room=%s seat=%s HTTP %s %s", room_id, seat, v_status, v_text[:120])
            return
        if isinstance(view, dict) and view.get("phase") == "finished":
            if binding.get("slot") and self._drop_cardroom_pool_binding(binding.get("slot")):
                self._cardroom_sessions.pop(f"{room_id}:{seat}", None)
                await self._save_runtime_config()
            return
        l_status, legal, l_text = await self._card_api_json("GET", legal_path)
        if l_status >= 400:
            logger.warning("[ChessArena] CardRoom legal-actions failed room=%s seat=%s HTTP %s %s", room_id, seat, l_status, l_text[:120])
            return
        if not isinstance(legal, dict) or not legal.get("is_my_turn"):
            return

        # ── CardRoom LLM context session ────────────────────────────────────
        selected: dict[str, Any] = {"action": "pass", "cards": [], "reason": "astrbot_no_action"}
        session_key = f"{room_id}:{seat}"
        session = self._cardroom_sessions.get(session_key)
        if session is None:
            session = CardRoomDecisionSession(
                room_id=room_id,
                seat=seat,
                created_at=time.time(),
                updated_at=time.time(),
                persona=self.cardroom_persona_prompt,
            )
            self._cardroom_sessions[session_key] = session
        view_dict = view if isinstance(view, dict) else {}

        # LLM decision (if enabled); fallback to min-legal on failure
        llm_candidates: list[dict[str, Any]] = []
        if self.cardroom_llm_decision_enabled:
            llm_candidates = await self._cardroom_llm_decide(session, view_dict, legal)
        if llm_candidates:
            selected = llm_candidates[0]
            selected.setdefault("speech", self._cardroom_default_speech(selected))
        else:
            selected = self._select_cardroom_action(view_dict, legal)
            selected.setdefault("speech", self._cardroom_default_speech(selected))
        # ────────────────────────────────────────────────────────────────────
        if self.cardroom_prompt_decision_enabled:
            payload = {
                "seat": seat,
                "max_retries": self.cardroom_prompt_max_retries,
                "candidates": [selected],
            }
            path = f"/api/card-rooms/{rid}/prompt-decision"
        else:
            payload = {
                "seat": seat,
                "action": selected["action"],
                "cards": selected.get("cards") or [],
                "source": "astrbot_cardroom_bot",
                "reason": selected.get("reason") or "astrbot_cardroom_bot",
                "speech": selected.get("speech") or "",
            }
            path = f"/api/card-rooms/{rid}/actions"
        if token:
            payload["token"] = token
        status, data, text = await self._card_api_json("POST", path, json_payload=payload)
        logger.info(
            "[ChessArena] CardRoom bot room_id=%s seat=%s legal_count=%s prompt_decision=%s selected=%s submit_status=%s result=%s",
            room_id,
            seat,
            self._cardroom_legal_count(legal),
            self.cardroom_prompt_decision_enabled,
            selected,
            status,
            text[:160],
        )

        accepted = 200 <= status < 300
        self._update_cardroom_session(session, view_dict, legal, selected, accepted)

    @staticmethod
    def _cardroom_legal_count(legal: dict[str, Any]) -> int:
        groups = legal.get("candidate_groups") if isinstance(legal.get("candidate_groups"), dict) else {}
        total = 0
        for key, value in groups.items():
            if key == "rocket_cards":
                continue
            if isinstance(value, list):
                total += len(value)
            elif value:
                total += 1
        if legal.get("can_pass"):
            total += 1
        return total

    # ── CardRoom LLM context helpers ────────────────────────────────────────

    def _cardroom_state_hash(self, view: dict[str, Any], legal: dict[str, Any]) -> str:
        """轻量局面哈希，防止同一局面重复决策。"""
        raw = json.dumps({
            "my_hand": sorted(view.get("my_hand") or []),
            "current_seat": view.get("current_seat"),
            "last_play": view.get("last_play"),
        }, sort_keys=True)
        return hashlib.md5(raw.encode()).hexdigest()

    def _build_cardroom_decision_prompt(
        self,
        session: CardRoomDecisionSession,
        view: dict[str, Any],
        legal: dict[str, Any],
    ) -> str:
        """构造每回合增量 user prompt，不重复完整规则。"""
        my_hand = view.get("my_hand") or []
        players = [item for item in view.get("players") or [] if isinstance(item, dict)]
        opponent_counts: dict[str, Any] = {}
        landlord_seat = str(view.get("landlord_seat") or "").strip()
        for player in players:
            seat_value = str(player.get("seat") if player.get("seat") is not None else "").strip()
            if player.get("is_landlord") and seat_value:
                landlord_seat = f"seat{seat_value}"
            if seat_value and not player.get("is_me") and seat_value != session.seat:
                opponent_counts[f"seat{seat_value}"] = player.get("hand_count", "?")
        if not opponent_counts:
            legacy_counts = view.get("opponent_hand_counts") if isinstance(view.get("opponent_hand_counts"), dict) else {}
            opponent_counts = {
                f"seat{s}": legacy_counts.get(str(s), "?")
                for s in (0, 1, 2) if str(s) != session.seat
            }
        landlord_seat = landlord_seat or "?"
        action_candidates = self._cardroom_action_candidates(legal)
        candidate_json = json.dumps(action_candidates, ensure_ascii=False, separators=(",", ":"))
        history_text = (
            "\n".join(f"- {h}" for h in session.history_summary[-6:])
            or "（新牌局）"
        )
        return (
            f"回合 #{session.turn_count + 1}\n\n"
            f"你的手牌：{my_hand}\n"
            f"其他玩家手牌数量：{opponent_counts}\n"
            f"地主 seat：{landlord_seat}\n"
            f"当前回合 seat：{view.get('current_seat', '?')}\n"
            f"上一手：{view.get('last_play') or '开局'}\n"
            f"pass 计数：{view.get('pass_count', 0)}\n"
            f"你能 pass：{legal.get('can_pass', False)}\n"
            f"\n完整合法动作（只能返回其中 action_id）：\n{candidate_json}"
            + f"\n\n最近对局摘要：\n{history_text}\n\n"
            '只输出 JSON：{"candidates":[{"action_id":"...","reason":"...","speech":"..."}]}'
        )

    @staticmethod
    def _cardroom_action_candidates(legal: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the complete deterministic action catalog from server legal actions."""
        groups = legal.get("candidate_groups") if isinstance(legal.get("candidate_groups"), dict) else {}
        out: list[dict[str, Any]] = []
        for family, value in groups.items():
            if family in {"rocket", "rocket_cards"} or not value:
                continue
            for raw_cards in value if isinstance(value, list) else [value]:
                cards = [raw_cards] if isinstance(raw_cards, str) else list(raw_cards or [])
                cards = [str(card) for card in cards]
                out.append({"action_id": "play:" + ",".join(cards), "action": "play", "cards": cards, "family": family})
        if groups.get("rocket") and groups.get("rocket_cards"):
            cards = [str(card) for card in groups.get("rocket_cards") or []]
            out.append({"action_id": "play:" + ",".join(cards), "action": "play", "cards": cards, "family": "rocket"})
        if legal.get("can_pass"):
            out.append({"action_id": "pass", "action": "pass", "cards": [], "family": "pass"})
        return out

    def _resolve_cardroom_llm_candidates(self, raw_candidates: list[dict[str, Any]], legal: dict[str, Any]) -> list[dict[str, Any]]:
        catalog = {item["action_id"]: item for item in self._cardroom_action_candidates(legal)}
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_candidates:
            action_id = str(raw.get("action_id") or "").strip()
            if action_id in seen or action_id not in catalog:
                continue
            seen.add(action_id)
            selected = dict(catalog[action_id])
            selected["reason"] = str(raw.get("reason") or "astrbot_llm")[:500]
            selected["speech"] = str(raw.get("speech") or "")[:300]
            selected["source"] = "astrbot_llm"
            resolved.append(selected)
        return resolved
    @staticmethod
    def _parse_llm_candidates(raw: str) -> list[dict[str, Any]]:
        """从 LLM 输出提取 JSON candidates，最少返回一条 pass。"""
        import re as _re
        m = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, _re.DOTALL)
        if m:
            raw = m.group(1)
        try:
            obj = json.loads(raw.strip())
        except json.JSONDecodeError:
            m2 = _re.search(r'\{[^{}]*"action(?:_id)?"[^{}]*\}', raw)
            if m2:
                try:
                    obj = json.loads(m2.group(0))
                except json.JSONDecodeError:
                    return [{"action": "pass", "cards": [], "reason": "parse_failed", "speech": ""}]
            else:
                return [{"action": "pass", "cards": [], "reason": "parse_failed", "speech": ""}]
        if isinstance(obj, dict) and isinstance(obj.get("candidates"), list):
            return [item for item in obj["candidates"] if isinstance(item, dict)]
        if isinstance(obj, dict) and ("action_id" in obj or "action" in obj):
            return [obj]
        return [{"action": "pass", "cards": [], "reason": "parse_failed", "speech": ""}]

    def _update_cardroom_session(
        self,
        session: CardRoomDecisionSession,
        view: dict[str, Any],
        legal: dict[str, Any],
        selected: dict[str, Any],
        accepted: bool,
    ) -> None:
        """每回合结束后更新 session 摘要。"""
        import time as _time
        session.turn_count += 1
        session.last_state_hash = self._cardroom_state_hash(view, legal)
        session.updated_at = _time.time()
        action = selected.get("action", "?")
        cards = selected.get("cards") or []
        speech = selected.get("speech") or ""
        status = "✓" if accepted else "✗"
        card_str = ",".join(cards) if cards else "pass"
        summary = (
            f"回合 {session.turn_count}: {action} {card_str} [{status}]"
            f" — {speech}"
        )
        session.history_summary.append(summary)
        if len(session.history_summary) > self.cardroom_context_max_history:
            session.history_summary = session.history_summary[
                -self.cardroom_context_max_history:
            ]
        if not accepted:
            session.last_errors.append(f"回合 {session.turn_count}: rejected")
            if len(session.last_errors) > 5:
                session.last_errors = session.last_errors[-5:]

    async def _cardroom_llm_decide(
        self,
        session: CardRoomDecisionSession,
        view: dict[str, Any],
        legal: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """调用 LLM 生成候选动作列表（15s 超时，失败返回空列表）。"""
        provider = await self._resolve_llm_provider()
        if not provider:
            logger.debug("[ChessArena] CardRoom LLM decide: no provider available")
            return []
        prompt = self._build_cardroom_decision_prompt(session, view, legal)
        system_prompt = session.persona or self.cardroom_persona_prompt
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    contexts=[],
                ),
                timeout=15,
            )
            raw = str(getattr(response, "completion_text", response) or "")
            candidates = self._resolve_cardroom_llm_candidates(self._parse_llm_candidates(raw), legal)
            logger.info(
                "[ChessArena] CardRoom LLM decide room=%s seat=%s turn=%d candidates=%d",
                session.room_id,
                session.seat,
                session.turn_count + 1,
                len(candidates),
            )
            return candidates
        except asyncio.TimeoutError:
            logger.warning("[ChessArena] CardRoom LLM decide timeout room=%s seat=%s", session.room_id, session.seat)
        except Exception as exc:
            logger.warning("[ChessArena] CardRoom LLM decide failed room=%s seat=%s: %s", session.room_id, session.seat, exc)
        return []

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _select_cardroom_action(view: dict[str, Any], legal: dict[str, Any]) -> dict[str, Any]:
        """Combination-first deterministic fallback when no LLM choice is usable."""
        groups = legal.get("candidate_groups") if isinstance(legal.get("candidate_groups"), dict) else {}
        leading = not view.get("last_play")
        lead_order = (
            "planes", "plane_with_pairs", "plane_with_singles", "consecutive_pairs", "straights",
            "triple_with_pair", "triple_with_single", "four_with_two_pairs", "four_with_two_singles",
            "triples", "pairs", "singles", "bombs", "rocket",
        )
        follow_order = (
            "triple_with_pair", "triple_with_single", "pairs", "triples", "consecutive_pairs",
            "straights", "planes", "plane_with_pairs", "plane_with_singles", "singles", "bombs", "rocket",
        )
        if not leading and legal.get("can_pass"):
            non_special = any(groups.get(name) for name in lead_order if name not in {"bombs", "rocket"})
            if not non_special:
                return {"action_id": "pass", "action": "pass", "cards": [], "reason": "astrbot_keep_bomb_pass"}
        order = lead_order if leading else follow_order
        for family in order:
            if family == "rocket":
                cards = list(groups.get("rocket_cards") or []) if groups.get("rocket") else []
            else:
                items = groups.get(family) or []
                cards = items[0] if items else []
            if isinstance(cards, str):
                cards = [cards]
            if cards:
                cards = [str(card) for card in cards]
                return {"action_id": "play:" + ",".join(cards), "action": "play", "cards": cards, "reason": f"astrbot_combo_{family}"}
        if legal.get("can_pass"):
            return {"action_id": "pass", "action": "pass", "cards": [], "reason": "astrbot_keep_hand_pass"}
        return {"action": "pass", "cards": [], "reason": "astrbot_no_legal_action"}

    def _match_url(self, match_id: Any) -> str:
        return f"{self.arena_base}/matches/{quote(str(match_id), safe='')}"

    def _my_bot_id(self) -> str:
        for key in ("bot_id", "id"):
            value = self.server_profile.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _list_from_response(data: Any, *keys: str) -> list[dict[str, Any]]:
        items: Any = data
        if isinstance(data, dict):
            for key in keys or ("bots", "items", "matches", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    items = value
                    break
                if isinstance(value, dict):
                    for nested_key in ("bots", "items", "matches"):
                        if isinstance(value.get(nested_key), list):
                            items = value.get(nested_key)
                            break
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _bot_id(bot: dict[str, Any]) -> str:
        return str(bot.get("bot_id") or bot.get("id") or "").strip()

    @staticmethod
    def _bot_name(bot: dict[str, Any]) -> str:
        return str(bot.get("name") or bot.get("bot_name") or bot.get("display_name") or "").strip()

    def _is_self_bot(self, bot: dict[str, Any]) -> bool:
        my_id = self._my_bot_id()
        bot_id = self._bot_id(bot)
        if my_id and bot_id and my_id == bot_id:
            return True
        name = self._bot_name(bot)
        return bool(name and name == self.effective_bot_name)

    @staticmethod
    def _truthy_status(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"online", "connected", "active", "ready", "available", "enabled", "true", "1", "yes", "y", "on"}:
            return True
        if text in {"offline", "disconnected", "inactive", "unavailable", "disabled", "false", "0", "no", "n", "off"}:
            return False
        return default

    @classmethod
    def _bot_online(cls, bot: dict[str, Any]) -> bool:
        return cls._truthy_status(
            bot.get("online", bot.get("is_online", bot.get("online_status", bot.get("status")))),
            default=False,
        )

    @staticmethod
    def _bot_enabled(bot: dict[str, Any]) -> bool:
        return ChessArenaPlugin._truthy_status(bot.get("is_enabled", bot.get("enabled")), default=True)

    @staticmethod
    def _bot_public(bot: dict[str, Any]) -> bool:
        return ChessArenaPlugin._truthy_status(bot.get("is_public", bot.get("public")), default=True)

    @classmethod
    def _bot_priority(cls, bot: dict[str, Any]) -> tuple[int, int, int]:
        online = cls._bot_online(bot)
        enabled = cls._bot_enabled(bot)
        public = cls._bot_public(bot)
        return (1 if online else 0, 1 if enabled else 0, 1 if public else 0)

    @classmethod
    def _bot_available(cls, bot: dict[str, Any]) -> bool:
        return cls._bot_online(bot) and cls._bot_enabled(bot) and cls._bot_public(bot)

    async def _fetch_bots(self, query: str = "", game: str = "") -> list[dict[str, Any]]:
        normalized_game = self._normalize_game(game)
        query = str(query or "").strip()
        params = [f"game={quote(normalized_game, safe='')}"]
        if query:
            params.insert(0, f"q={quote(query, safe='')}")
        path = "/api/bots?" + "&".join(params)
        _base, status, data, _text = await self._api_json(
            "GET",
            path,
            headers=self._auth_headers() if self.token else None,
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if status >= 400 and query:
            _base, status, data, _text = await self._api_json(
                "GET",
                f"/api/bots?game={quote(normalized_game, safe='')}",
                headers=self._auth_headers() if self.token else None,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return self._list_from_response(data, "bots", "items", "data")

    async def _find_bot(self, query: str, exclude_self: bool = True, game: str = "") -> tuple[dict[str, Any] | None, str]:
        query = str(query or "").strip()
        if not query:
            return None, "你要挑战谁？用法：棋擂台挑战 <名字或bot_id> [红|黑|随机]"
        bots = await self._fetch_bots(query, game=game)
        if exclude_self:
            bots = [bot for bot in bots if not self._is_self_bot(bot)]
        q = query.lower()
        stages = [
            [bot for bot in bots if self._bot_id(bot).lower() == q],
            [bot for bot in bots if self._bot_name(bot).lower() == q],
            [bot for bot in bots if q in self._bot_name(bot).lower()],
        ]
        for matches in stages:
            if not matches:
                continue
            matches = sorted(matches, key=self._bot_priority, reverse=True)
            best_pri = self._bot_priority(matches[0])
            best = [bot for bot in matches if self._bot_priority(bot) == best_pri]
            if len(best) == 1:
                return best[0], ""
            names = "、".join(f"{self._bot_name(bot) or '未命名'}({self._bot_id(bot)})" for bot in best[:5])
            return None, f"找到多个对手：{names}。请用 bot_id 指定。"
        return None, f"没找到对手：{query}"

    @staticmethod
    def _parse_side(value: str) -> str:
        side = str(value or "随机").strip().lower()
        mapping = {"红": "red", "红方": "red", "red": "red", "r": "red", "黑": "black", "黑方": "black", "black": "black", "b": "black", "随机": "random", "随便": "random", "random": "random"}
        return mapping.get(side, "random")

    @staticmethod
    def _side_cn(side: Any) -> str:
        return {"red": "红方", "black": "黑方", "random": "随机"}.get(str(side or "").lower(), str(side or "未知"))

    @staticmethod
    def _api_error_cn(status: int, data: Any, text: str) -> str:
        code = ""
        message = ""
        if isinstance(data, dict):
            code = str(data.get("code") or data.get("error") or data.get("reason") or "")
            message = str(data.get("message") or data.get("detail") or "")
        if status == 409 and code == "bot_busy":
            return "对方正在下棋，暂时约不了。"
        if status == 409:
            return "对方或我方正忙，稍后再试。"
        return message or code or text[:120] or f"HTTP {status}"

    def _format_challenge_reply(self, opponent: dict[str, Any], side: str, status: int, data: Any, text: str) -> str:
        name = self._bot_name(opponent) or self._bot_id(opponent)
        if status >= 400:
            return f"挑战 {name} 失败：{self._api_error_cn(status, data, text)}"
        payload = data if isinstance(data, dict) else {}
        match_id = payload.get("match_id") or payload.get("matchId")
        challenge_id = payload.get("challenge_id") or payload.get("challengeId")
        state = str(payload.get("status") or payload.get("state") or "").lower()
        if match_id or state in {"started", "active", "playing"}:
            link = f"\n{self._match_url(match_id)}" if match_id else ""
            return f"已挑战 {name}（我执{self._side_cn(side)}），已开局！{link}"
        if state in {"pending_owner", "owner_pending", "need_approval", "waiting_owner"}:
            return f"已挑战 {name}（我执{self._side_cn(side)}），等待主人确认。"
        suffix = f"（#{challenge_id}）" if challenge_id else ""
        return f"已挑战 {name}（我执{self._side_cn(side)}），等待对方接招{suffix}。"

    async def _auto_register_bot(self) -> None:
        payload = self._bot_settings_payload(include_client=True)
        self._routine_log("[ChessArena] token 为空，正在自动注册 Bot: %s", self.bot_name)
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
        self._routine_log("[ChessArena] 自动注册成功 bot_id=%s token=%s", data.get("bot_id") or data.get("id"), self._token_hint(token))
        await self._save_registration_to_runtime_config(token)

    async def _fetch_server_profile(self) -> bool | None:
        """验证 token 并拉取服务端 Bot profile。

        Return True for valid token, False for auth failure, None for network/server failure.
        成功时只缓存 name/avatar_url/description/chess_style/persona_prompt 等网页端 profile 字段，
        不把本地配置反向覆盖到服务端。
        """
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
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError as exc:
                self.state.last_error = f"fetch profile non-json: {exc}"
                logger.warning("[ChessArena] token 验证返回非 JSON，稍后重试: %s", text[:200])
                return None
            self.server_profile = self._extract_server_profile(data)
            await self._sync_server_challenge_policy(data)
            await self._sync_server_enabled_games(data)
            self._routine_log("[ChessArena] token 验证成功，已拉取服务端 profile: %s", self._short(self.server_profile))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] token 验证网络异常，稍后重试，不判定 token 无效: %s", exc)
            return None

    async def _verify_token(self) -> bool | None:
        """Return True for valid token, False for auth failure, None for network/server failure."""
        return await self._fetch_server_profile()

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

    def _bot_settings_payload(self, include_client: bool = False) -> dict[str, Any]:
        # Bot 的公开资料（名字、头像、简介、棋风、人格）统一在网站后台填写。
        # 插件只在 token 为空自动注册时提交一个最小资料，避免把 AstrBot 本地旧配置反向覆盖网站端。
        payload: dict[str, Any] = {
            "name": self.bot_name,
            "is_public": True,
            "enabled_games": list(self.enabled_games),
        }
        if include_client:
            payload.update({"client_type": "astrbot", "instance_name": self._instance_name()})
        return payload

    @staticmethod
    def _extract_server_profile(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        source = data
        for key in ("bot", "data", "profile"):
            nested = data.get(key)
            if isinstance(nested, dict):
                source = nested
                break
        profile: dict[str, Any] = {}
        for key in ("bot_id", "id", "name", "avatar_url", "description", "chess_style", "persona_prompt", "challenge_policy", "owner_review_timeout_sec"):
            value = source.get(key)
            if value is not None and str(value).strip():
                profile[key] = str(value).strip()
        enabled_games = source.get("enabled_games")
        if isinstance(enabled_games, list):
            profile["enabled_games"] = [str(game) for game in enabled_games]
        return profile

    @staticmethod
    def _response_source(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        for key in ("bot", "data", "profile"):
            nested = data.get(key)
            if isinstance(nested, dict):
                return nested
        return data

    async def _sync_server_challenge_policy(self, profile_response: Any = None) -> None:
        expected_policy = self.server_challenge_policy
        source = self._response_source(profile_response) if profile_response is not None else self.server_profile
        current_policy = str(source.get("challenge_policy") or "").strip()
        current_timeout = int(float(source.get("owner_review_timeout_sec") or 0)) if str(source.get("owner_review_timeout_sec") or "").strip() else 0
        payload: dict[str, Any] = {}
        if current_policy != expected_policy:
            payload["challenge_policy"] = expected_policy
        if expected_policy == "manual_approve" and current_timeout != self.owner_decision_timeout_sec:
            payload["owner_review_timeout_sec"] = self.owner_decision_timeout_sec
        if not payload:
            return
        try:
            _base, status, text = await self._request_text_with_fallback(
                "PATCH",
                "/api/bots/me",
                json_payload=payload,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if status >= 400:
                logger.warning("[ChessArena] 同步挑战审批策略失败: HTTP %s %s", status, text[:200])
                return
            data = json.loads(text) if text else {}
            synced = self._extract_server_profile(data)
            if synced:
                self.server_profile.update(synced)
            self._routine_log("[ChessArena] 已同步挑战审批策略到网站: %s", payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] 同步挑战审批策略异常，不影响启动: %s", exc)

    async def _sync_server_enabled_games(self, profile_response: Any = None) -> None:
        source = self._response_source(profile_response) if profile_response is not None else self.server_profile
        current = source.get("enabled_games") if isinstance(source, dict) else None
        current_games = [str(game) for game in current] if isinstance(current, list) else []
        expected = list(self.enabled_games)
        if current_games == expected:
            return
        try:
            _base, status, text = await self._request_text_with_fallback(
                "PATCH",
                "/api/bots/me",
                json_payload={"enabled_games": expected},
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if status >= 400:
                logger.warning("[ChessArena] failed to sync enabled games: HTTP %s %s", status, text[:200])
                return
            data = json.loads(text) if text else {}
            synced = self._extract_server_profile(data)
            if synced:
                self.server_profile.update(synced)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] enabled games sync failed; SSE will continue: %s", exc)

    def _profile_value(self, key: str, local_default: str = "") -> str:
        value = self.server_profile.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        return str(local_default or "").strip()

    @property
    def effective_bot_name(self) -> str:
        return self._profile_value("name", self.bot_name)

    @property
    def effective_chess_style(self) -> str:
        return self._profile_value("chess_style", self.chess_style) or "random"

    @property
    def effective_persona_prompt(self) -> str:
        return self._profile_value("persona_prompt", self.persona_prompt)

    async def _save_registration_to_runtime_config(self, token: str) -> None:
        """把自动注册结果写回 AstrBot 插件 runtime config，便于打开配置页直接看到 token。"""
        env_path = os.environ.get("CHESS_ARENA_CONFIG_PATH") or os.environ.get("ASTRBOT_PLUGIN_CHESS_ARENA_CONFIG")
        paths = [Path(env_path)] if env_path else [self._instance_runtime_config_path()]
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
                    self._routine_log("[ChessArena] 已写回注册信息到 runtime config: %s token=%s", path, self._token_hint(token))
                    wrote = True
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{path}: {exc}"
                    logger.warning("[ChessArena] 写回注册信息到 runtime config 失败: %s", last_error)
        if not wrote:
            logger.warning("[ChessArena] token 自动写回失败，请手动复制到 WebUI 配置。最后错误: %s", last_error)

    async def _save_runtime_config(self) -> None:
        await self._save_registration_to_runtime_config(self.token)

    def _runtime_config_payload(self, token: str) -> dict[str, Any]:
        data = dict(self.config)
        data.update({
            "arena_base": self.arena_base,
            "arena_fallback_bases": ",".join(self.arena_fallback_bases),
            "token": token,
            "auto_register": self.auto_register,
            "commentary_enabled": self.commentary_enabled,
            "commentary_timeout_sec": self.commentary_timeout_sec,
            "llm_provider_mode": self.llm_provider_mode,
            "llm_provider_id": self.llm_provider_id,
            "llm_tools_enabled": self.llm_tools_enabled,
            "llm_tools_allow_actions": self.llm_tools_allow_actions,
            "default_game": self.default_game,
            "enabled_games": ",".join(self.enabled_games),
            "cardroom_pool_bindings": json.dumps(getattr(self, "cardroom_pool_bindings", []), ensure_ascii=False, separators=(",", ":")),
            "auto_accept_challenges": self.auto_accept_challenges,
            "challenge_decision_mode": self.challenge_decision_mode,
            "challenge_policy": self.server_challenge_policy,
            "owner_notify_enabled": self.owner_notify_enabled,
            "owner_notify_targets": self.owner_notify_targets,
            "owner_decision_timeout_sec": self.owner_decision_timeout_sec,
            "owner_review_timeout_sec": self.owner_decision_timeout_sec,
            "match_report_enabled": self.match_report_enabled,
            "engine_mode": self.engine_mode,
            "engine_depth": self.engine_depth,
            "engine_timeout_sec": self.engine_timeout_sec,
            "custom_engine_command": self.custom_engine_command,
            "custom_engine_http_url": self.custom_engine_http_url,
            "custom_engine_http_headers": self.custom_engine_http_headers,
            "local_engine_node_path": self.local_engine_node_path,
            "move_timeout_sec": self.move_timeout_sec,
            "announce_to_current_chat": self.announce_to_current_chat,
            "verbose_logging": self.verbose_logging,
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
            self._routine_log("[ChessArena] 正在连接 SSE: %s", self._safe_url(url))
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
        self._routine_log("[ChessArena] SSE 已连接")

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
        self._routine_log("[ChessArena] 收到事件 %s: %s", event_type, self._short(payload))

        if event_type == "challenge_received":
            await self._handle_challenge_received(payload)
        elif event_type == "your_turn":
            await self._handle_your_turn(payload)
        elif event_type in {"challenge_accepted", "match_started", "move_made", "match_state"}:
            self._remember_match_event(event_type, payload)
        elif event_type == "match_finished":
            await self._handle_match_finished(payload)

    async def _handle_challenge_received(self, event: dict[str, Any]) -> None:
        challenge_id = self._challenge_id(event)
        if not challenge_id:
            logger.warning("[ChessArena] challenge_received 缺少 id: %s", event)
            return

        mode = self.challenge_decision_mode
        if mode == "ignore":
            self._routine_log("[ChessArena] 已忽略挑战 %s：challenge_decision_mode=ignore", challenge_id)
            return
        if str(challenge_id) in self.notified_challenges:
            self._routine_log("[ChessArena] 挑战 %s 已通知过，跳过重复通知", challenge_id)
            return
        if self._challenge_expired(event) or self._challenge_too_old(event):
            self.pending_owner_challenges.pop(str(challenge_id), None)
            self.notified_challenges.add(str(challenge_id))
            self._routine_log("[ChessArena] 跳过已过期/旧挑战 %s", challenge_id)
            return
        status = str(event.get("status") or "").strip().lower()
        if status and status not in {"owner_review", "pending", "waiting_owner"}:
            self.pending_owner_challenges.pop(str(challenge_id), None)
            self._routine_log("[ChessArena] 跳过非待审批挑战 %s status=%s", challenge_id, status)
            return
        if mode == "auto_accept":
            await self._accept_challenge(challenge_id)
            return

        # owner_approve：只登记/通知，不阻塞 SSE；主人稍后用命令同意/拒绝。
        if str(challenge_id) in self.pending_owner_challenges:
            self.notified_challenges.add(str(challenge_id))
            self._routine_log("[ChessArena] 挑战 %s 已在待审批列表，跳过重复通知", challenge_id)
            return
        record = dict(event)
        record["challenge_id"] = str(challenge_id)
        record["received_at"] = time.time()
        self.pending_owner_challenges[str(challenge_id)] = record
        text = self._owner_challenge_text(record)
        self.notified_challenges.add(str(challenge_id))
        if self.owner_notify_enabled:
            task = asyncio.create_task(self._notify_owner_safe(text), name=f"chess_arena_notify_{challenge_id}")
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)
        self._routine_log("[ChessArena] 挑战 %s 等待主人审批", challenge_id)

    async def _accept_challenge(self, challenge_id: Any) -> dict[str, Any]:
        _base, status, text = await self._request_text_with_fallback(
            "POST",
            f"/api/challenges/{quote(str(challenge_id), safe='')}/accept",
            headers=self._auth_headers(),
        )
        if status >= 400:
            raise RuntimeError(f"accept challenge failed: HTTP {status} {text[:200]}")
        self.state.accepted_challenges += 1
        self._routine_log("[ChessArena] 已接受挑战 %s", challenge_id)
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"raw": text}

    async def _handle_your_turn(self, event: dict[str, Any]) -> None:
        game = self._event_game(event)
        match_id = event.get("match_id") or event.get("matchId") or event.get("id")
        if not match_id:
            logger.warning("[ChessArena] your_turn 缺少 match_id: %s", event)
            return

        if game == "go":
            await self._handle_go9_your_turn(str(match_id), event)
            return

        legal_moves = event.get("legal_moves") or event.get("legalMoves") or []
        if not isinstance(legal_moves, list) or not legal_moves:
            logger.warning("[ChessArena] your_turn 无 legal_moves: %s", event)
            return
        move = await self._choose_move(legal_moves, event)
        await self._submit_arena_move(str(match_id), move, event)

    def _event_game(self, event: dict[str, Any]) -> str:
        game = event.get("game")
        if not game and isinstance(event.get("match"), dict):
            game = event["match"].get("game")
        return self._game_alias_to_id(game) or "xiangqi"

    async def _handle_go9_your_turn(self, match_id: str, event: dict[str, Any]) -> None:
        """Go 9×9 MVP：只从后端 legal_moves 选非 pass 合法点；不接 KataGo，不走象棋引擎。"""
        side = str(event.get("side") or event.get("turn") or "").strip()
        legal_moves = event.get("legal_moves") or event.get("legalMoves") or []
        if not isinstance(legal_moves, list):
            legal_moves = []
        legal = [str(move).strip() for move in legal_moves if str(move or "").strip()]
        move = await self._choose_go9_engine_or_fallback_move(match_id, event, legal_moves=legal)
        status, text = await self._submit_arena_move(match_id, move, event, raise_on_error=False)
        if status < 400:
            logger.info("[ChessArena] go9 submit success match_id=%s side=%s move=%s result=%s", match_id, side, move, text[:160])
            return
        logger.warning("[ChessArena] go9 submit failed match_id=%s side=%s move=%s HTTP %s %s", match_id, side, move, status, text[:160])
        raise RuntimeError(f"submit go9 move failed: HTTP {status} {text[:200]}")

    async def _submit_arena_move(
        self,
        match_id: str,
        move: str,
        event: dict[str, Any],
        *,
        raise_on_error: bool = True,
    ) -> tuple[int, str]:
        started = time.perf_counter()
        comment = await self._make_comment(move, event)
        duration_ms = max(1, int((time.perf_counter() - started) * 1000))
        payload = {
            "move": move,
            "comment": comment,
            "duration_ms": duration_ms,
        }
        timeout = aiohttp.ClientTimeout(total=max(1, self.move_timeout_sec))
        _base, status, text = await self._request_text_with_fallback(
            "POST",
            f"/api/matches/{quote(str(match_id), safe='')}/move",
            json_payload=payload,
            headers=self._auth_headers(),
            timeout=timeout,
        )
        if status >= 400:
            if raise_on_error:
                raise RuntimeError(f"submit move failed: HTTP {status} {text[:200]}")
            return status, text
        self.state.submitted_moves += 1
        self._routine_log("[ChessArena] match=%s 已提交走法: %s comment=%s", match_id, move, comment)
        return status, text

    async def _choose_move(self, legal_moves: list[Any], event: dict[str, Any]) -> str:
        """始终从后端给出的 legal_moves 中选步；按 engine_mode 走引擎链，最终随机兜底。"""
        moves = [str(move).strip() for move in legal_moves if str(move or "").strip()]
        if not moves:
            raise RuntimeError("no legal moves")
        return await self._run_engine_chain(moves, event)

    async def _choose_go9_engine_or_fallback_move(self, match_id: str, event: dict[str, Any], legal_moves: list[str]) -> str:
        """Use Go9 engine adapter when enabled; otherwise keep the current random/pass fallback."""
        legal = [str(move).strip() for move in legal_moves if str(move or "").strip()]
        if self.go_engine_enabled:
            try:
                best = await self._call_go9_engine(match_id, event, legal)
                valid = self._validate_go9_engine_move(best, legal)
                if valid:
                    self._routine_log("[ChessArena] heuristic_go9 chose: %s", valid)
                    return valid
            except Exception as exc:  # noqa: BLE001 - Go engine must never break move submission
                logger.warning("[ChessArena] go9 engine failed, falling back to random/pass: %s", exc)
            if not self.go_engine_fallback_random:
                return "pass"
        return self._choose_go9_move(event, legal_moves=legal)

    async def _call_go9_engine(self, match_id: str, event: dict[str, Any], legal_moves: list[str]) -> str | None:
        if not self.token:
            logger.warning("[ChessArena] go9 engine skipped: missing bot token")
            return None
        endpoint = (self.go_engine_endpoint or "http://127.0.0.1:8787/api/go9/analyze").strip()
        if not endpoint:
            logger.warning("[ChessArena] go9 engine skipped: empty endpoint")
            return None
        state = self._go9_engine_state_from_event(event)
        payload = {"state": state, "depth": max(1, int(self.engine_depth or 1))}
        if legal_moves:
            payload["legal_moves"] = legal_moves
        payload["match_id"] = match_id
        timeout = aiohttp.ClientTimeout(total=self.go_engine_timeout_sec)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        session = await self._get_session()
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            async with session.post(endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                text = await resp.text()
                status = resp.status
        else:
            _base, status, text = await self._request_text_with_fallback("POST", endpoint if endpoint.startswith("/") else f"/{endpoint}", json_payload=payload, headers=headers, timeout=timeout)
        if status >= 400:
            logger.warning("[ChessArena] go9 engine analyze failed: HTTP %s %s", status, text[:160])
            return None
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            logger.warning("[ChessArena] go9 engine returned non-json: %s", text[:160])
            return None
        if not isinstance(data, dict):
            logger.warning("[ChessArena] go9 engine returned non-object: %s", str(data)[:160])
            return None
        return data.get("best_move") or data.get("move")

    def _go9_engine_state_from_event(self, event: dict[str, Any]) -> dict[str, Any]:
        state = self._go9_state_from_event(event)
        if not isinstance(state, dict):
            state = {}
        board = state.get("board") or event.get("board")
        if board is not None:
            state["board"] = board
        turn = state.get("turn") or event.get("side")
        if turn:
            state["turn"] = str(turn).strip()
        if "passes" not in state:
            for key in ("passes", "pass_count", "passCount"):
                if key in event:
                    state["passes"] = event.get(key)
                    break
            else:
                state["passes"] = 0
        return state

    @staticmethod
    def _normalize_go9_move(move: Any) -> str:
        text = str(move or "").strip()
        if not text:
            return ""
        if text.lower() == "pass":
            return "pass"
        return text

    def _validate_go9_engine_move(self, move: Any, legal_moves: list[str]) -> str | None:
        best = self._normalize_go9_move(move)
        legal = [self._normalize_go9_move(item) for item in legal_moves if self._normalize_go9_move(item)]
        if best and legal and best in legal:
            return best
        if best and not legal:
            logger.warning("[ChessArena] go9 engine returned %s but legal_moves missing; fallback for safety", best)
            return None
        logger.warning("[ChessArena] go9 engine returned illegal/empty move %s, legal_moves=%s", best, legal[:20])
        return None

    def _choose_go9_move(self, event: dict[str, Any], tried: set[str] | None = None, legal_moves: list[str] | None = None) -> str:
        legal = [str(move).strip() for move in (legal_moves or []) if str(move or "").strip()]
        non_pass = [move for move in legal if move.lower() != "pass"]
        if non_pass:
            return random.choice(non_pass)
        if "pass" in {move.lower() for move in legal}:
            return "pass"
        tried = tried or set()
        state = self._go9_state_from_event(event)
        board = state.get("board") if isinstance(state, dict) else None
        size = int(state.get("size") or 9) if isinstance(state, dict) else 9
        if not isinstance(board, list) or size < 1:
            return "pass"
        candidates: list[str] = []
        max_size = min(size, 25)
        for row_idx, row in enumerate(board[:max_size]):
            if not isinstance(row, list):
                continue
            for col_idx, value in enumerate(row[:max_size]):
                if value is None or value == "":
                    coord = f"{chr(ord('a') + col_idx)}{size - row_idx}"
                    if coord not in tried:
                        candidates.append(coord)
        if not candidates:
            return "pass"
        return random.choice(candidates)

    def _go9_state_from_event(self, event: dict[str, Any]) -> dict[str, Any]:
        for key in ("state", "state_json", "fen"):
            raw = event.get(key)
            if raw is None and isinstance(event.get("match"), dict):
                raw = event["match"].get(key)
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str) and raw.strip():
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    continue
        return {}

    def _engine_request_payload(self, legal_moves: list[str], event: dict[str, Any]) -> dict[str, Any]:
        side = str(event.get("side") or event.get("turn") or "").strip()
        return {
            "fen": str(event.get("fen") or "").strip(),
            "legal_moves": legal_moves,
            "side": side,
            "depth": max(1, self.engine_depth),
            "timeout_ms": self.engine_timeout_sec * 1000,
            "bot_name": self.effective_bot_name,
            "chess_style": self.effective_chess_style,
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
                    self._routine_log("[ChessArena] %s chose: %s", engine, valid)
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
        if self._event_game(event) == "go":
            return self._template_go9_comment(move, event)
        facts = self._analyze_move_facts(move, event)
        fallback = self._template_comment(move, event, facts)
        llm_comment = await self._try_llm_comment(move, event, fallback, facts)
        return self._sanitize_comment(llm_comment or fallback, facts)

    def _template_go9_comment(self, move: str, event: dict[str, Any]) -> str:
        side = str(event.get("side") or event.get("turn") or "").lower()
        side_label = {"black": "黑棋", "white": "白棋"}.get(side, "围棋")
        if str(move).lower() == "pass":
            return f"{side_label}这手先停一手。"
        return random.choice([
            f"{side_label}落在 {move}，先占个点。",
            f"{side_label}下 {move}，慢慢围。",
            f"{side_label}先走 {move}。",
        ])

    async def _try_llm_comment(
        self, move: str, event: dict[str, Any], fallback: str, facts: dict[str, Any]
    ) -> str:
        provider = await self._resolve_llm_provider()
        if not provider or not hasattr(provider, "text_chat"):
            return fallback

        forbidden = self._comment_forbidden_claims(facts)
        bot_name = self.effective_bot_name
        chess_style = self.effective_chess_style
        persona_prompt = self.effective_persona_prompt
        fact_lines = [
            f"Bot 名字：{bot_name}",
            f"棋风：{chess_style}",
            f"人格设定：{persona_prompt}",
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

    @staticmethod
    def _challenge_id(event: dict[str, Any]) -> Any:
        return event.get("challenge_id") or event.get("challengeId") or event.get("id")

    def _challenge_value(self, event: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = event.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _owner_challenge_text(self, event: dict[str, Any]) -> str:
        challenge_id = str(self._challenge_id(event) or "")
        challenger = self._challenge_value(event, "challenger_name", "challengerName", "challenger", "from_name") or "未知对手"
        opponent = self._challenge_value(event, "opponent_name", "opponentName", "opponent") or self.effective_bot_name
        side = self._challenge_value(event, "side", "bot_side", "botSide", "color", "assigned_side") or "未知"
        side_label = {"red": "红方/先手", "black": "黑方/后手", "r": "红方/先手", "b": "黑方/后手"}.get(side.lower(), side)
        expires_at = self._challenge_value(event, "expires_at", "expiresAt", "expire_at", "expireAt") or "未知"
        return (
            "收到棋擂台挑战，等待主人审批：\n"
            f"Bot：{opponent or self.effective_bot_name}\n"
            f"对手：{challenger}\n"
            f"挑战ID：{challenge_id}\n"
            f"红黑/先后：{side_label}\n"
            f"过期时间：{expires_at}\n"
            f"请回复：棋擂台同意 {challenge_id} / 棋擂台拒绝 {challenge_id}"
        )

    def _challenge_expired(self, event: dict[str, Any], *, grace_sec: float = 5.0) -> bool:
        expires = self._challenge_value(event, "expires_at", "expiresAt", "expire_at", "expireAt")
        if not expires:
            return False
        try:
            return float(expires) + grace_sec < time.time()
        except Exception:
            return False

    def _challenge_too_old(self, event: dict[str, Any], *, max_age_sec: float = 60.0) -> bool:
        created = self._challenge_value(event, "created_at", "createdAt", "created", "timestamp", "ts")
        if not created:
            return False
        try:
            value = float(created)
            if value > 10_000_000_000:  # tolerate millisecond timestamps
                value /= 1000.0
            return time.time() - value > max_age_sec
        except Exception:
            return False

    async def _notify_owner_safe(self, text: str) -> None:
        try:
            await self._notify_owner(text)
        except Exception as exc:  # noqa: BLE001 - 主动通知失败不能影响 SSE
            logger.warning("[ChessArena] 主人挑战审批通知失败，已保留待确认: %s", exc)

    @staticmethod
    def _match_id_from_event(event: dict[str, Any]) -> str:
        match = event.get("match") if isinstance(event.get("match"), dict) else {}
        for source in (event, match):
            value = source.get("match_id") or source.get("matchId") or source.get("id")
            if value:
                return str(value)
        return ""

    def _remember_match_event(self, event_type: str, event: dict[str, Any]) -> None:
        match_id = self._match_id_from_event(event)
        if not match_id:
            return
        match = event.get("match") if isinstance(event.get("match"), dict) else event
        status = str(match.get("status") or event.get("status") or "").lower()
        if event_type == "challenge_accepted":
            cid = self._challenge_id(event)
            if cid:
                self.pending_owner_challenges.pop(str(cid), None)
        if status == "finished":
            self._remember_match_finished(match)
            return
        rec = dict(self.active_matches.get(match_id) or {})
        rec.update({k: v for k, v in match.items() if k != "moves"})
        rec["match_id"] = match_id
        rec["last_event"] = event_type
        rec["updated_at_local"] = time.time()
        self.active_matches[match_id] = rec

    async def _handle_match_finished(self, event: dict[str, Any]) -> None:
        match_id = self._match_id_from_event(event)
        if not match_id:
            logger.warning("[ChessArena] match_finished 缺少 match_id: %s", self._short(event))
            return
        if match_id in self.finished_games:
            self._routine_log("[ChessArena] 对局 %s 已处理过结束事件，跳过重复通知", match_id)
            return
        rec = self._remember_match_finished(event)
        self.finished_games.add(match_id)
        self._routine_log("[ChessArena] 对局 %s 已结束: %s", match_id, self._latest_finished_match_line())
        if self.match_report_enabled and self.owner_notify_enabled:
            text = self._match_finished_report_text(rec)
            task = asyncio.create_task(self._notify_owner_safe(text), name=f"chess_arena_match_finished_{match_id}")
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

    def _remember_match_finished(self, event: dict[str, Any]) -> dict[str, Any]:
        match_id = self._match_id_from_event(event)
        if not match_id:
            return {}
        match = event.get("match") if isinstance(event.get("match"), dict) else event
        rec = dict(self.active_matches.pop(match_id, {}) or {})
        rec.update({k: v for k, v in match.items() if k != "moves"})
        rec["match_id"] = match_id
        rec["status"] = "finished"
        rec["updated_at_local"] = time.time()
        self.recent_finished_matches.insert(0, rec)
        self.recent_finished_matches = self.recent_finished_matches[:10]
        return rec

    def _match_finished_report_text(self, match: dict[str, Any]) -> str:
        match_id = str(match.get("match_id") or "")
        red = str(match.get("red_bot_name") or match.get("red_name") or match.get("red_bot_id") or "红方")
        black = str(match.get("black_bot_name") or match.get("black_name") or match.get("black_bot_id") or "黑方")
        result_line = self._latest_finished_match_line() or f"{match_id}：已结束"
        ply = match.get("ply") or match.get("move_count") or "未知"
        url = self._match_url(match_id) if match_id else self.arena_base
        return (
            "棋擂台对局已结束：\n"
            f"对局：{red} vs {black}\n"
            f"结果：{result_line}\n"
            f"手数：{ply}\n"
            f"链接：{url}"
        )

    def _active_match_lines(self) -> list[str]:
        lines: list[str] = []
        for match_id, m in sorted(self.active_matches.items(), key=lambda item: item[1].get("updated_at_local", 0), reverse=True):
            red = str(m.get("red_bot_name") or m.get("red_name") or "红方")
            black = str(m.get("black_bot_name") or m.get("black_name") or "黑方")
            ply = m.get("ply") or m.get("move_count") or 0
            turn = str(m.get("turn") or "").lower()
            turn_label = {"red": "红方", "black": "黑方", "r": "红方", "b": "黑方"}.get(turn, "未知")
            lines.append(f"- {match_id}：{red} vs {black}，{ply}手，轮到{turn_label}")
        return lines

    def _latest_finished_match_line(self) -> str:
        if not self.recent_finished_matches:
            return ""
        m = self.recent_finished_matches[0]
        result = str(m.get("result") or "").lower()
        reason = str(m.get("finish_reason") or "").strip()
        winner = str(m.get("winner_bot_name") or "").strip()
        if not winner:
            winner_id = str(m.get("winner_bot_id") or "")
            if winner_id and winner_id == str(m.get("red_bot_id") or ""):
                winner = str(m.get("red_bot_name") or "")
            elif winner_id and winner_id == str(m.get("black_bot_id") or ""):
                winner = str(m.get("black_bot_name") or "")
        label = "和棋" if result == "draw" else (f"{winner} 胜" if winner else (result or "已结束"))
        return f"{m.get('match_id') or ''}：{label}" + (f"，原因={reason}" if reason else "")

    def _owner_notify_target_list(self) -> list[str]:
        raw = str(self.owner_notify_targets or "").replace("\n", ",")
        targets: list[str] = []
        for item in raw.split(","):
            target = item.strip()
            if target and target not in targets:
                targets.append(target)
        return targets

    async def _notify_owner(self, text: str) -> None:
        targets = self._owner_notify_target_list()
        if not targets:
            self._routine_log("[ChessArena] owner_notify_targets 为空，仅保留待确认挑战，不主动私聊。")
            return
        sender = getattr(self.context, "send_message", None)
        if not callable(sender):
            logger.warning("[ChessArena] 当前 AstrBot Context 无 send_message，无法主动通知主人。")
            return
        chain = MessageChain([Plain(text)])
        for target in targets:
            delivered = False
            for umo in self._target_umo_candidates(target):
                try:
                    await self._wait_for_platform_ready(umo)
                    result = sender(umo, chain)
                    if inspect.isawaitable(result):
                        result = await result
                    if result is False:
                        logger.warning("[ChessArena] 发送挑战审批通知到 %s 失败: 未找到对应平台或平台未就绪。", umo)
                        continue
                    self._routine_log("[ChessArena] 已发送挑战审批通知到 %s", umo)
                    delivered = True
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ChessArena] 发送挑战审批通知到 %s 失败: %s", umo, exc)
            if not delivered:
                logger.warning("[ChessArena] 挑战审批通知目标 %s 全部候选发送失败。", target)

    def _target_umo_candidates(self, target: str) -> list[str]:
        target = str(target or "").strip()
        if not target:
            return []
        platforms = self._known_platform_ids()
        candidates: list[str] = []
        if target.count(":") >= 2:
            parts = target.split(":", 2)
            if len(parts) == 3:
                _old_platform, msg_type, peer = parts
                for platform in platforms:
                    candidates.append(f"{platform}:{msg_type}:{peer}")
            candidates.append(target)
        else:
            for platform in platforms:
                candidates.append(f"{platform}:FriendMessage:{target}")
            for platform in platforms:
                candidates.append(f"{platform}:GroupMessage:{target}")
            if not candidates:
                candidates.append(target)
        deduped: list[str] = []
        for item in candidates:
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    async def _wait_for_platform_ready(self, umo: str, timeout_sec: float = 2.0) -> bool:
        platform_id = str(umo or "").split(":", 1)[0]
        if not platform_id:
            return False
        deadline = time.monotonic() + timeout_sec
        while True:
            try:
                platforms = getattr(getattr(self.context, "platform_manager", None), "platform_insts", []) or []
                if any(getattr(platform.meta(), "id", None) == platform_id for platform in platforms):
                    return True
            except Exception:  # noqa: BLE001 - readiness probe must not break notification
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    def _known_platform_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            platforms = getattr(getattr(self.context, "platform_manager", None), "platform_insts", []) or []
            for platform in platforms:
                pid = getattr(platform.meta(), "id", None)
                if pid and pid not in ids:
                    ids.append(str(pid))
        except Exception:  # noqa: BLE001
            pass
        configured = str(self.config.get("owner_notify_platform") or "").strip()
        if configured and configured not in ids:
            ids.append(configured)
        default = self._default_platform_id()
        if default and default not in ids:
            ids.append(default)
        return ids

    def _default_platform_id(self) -> str:
        for attr in ("platform", "platform_id", "id"):
            value = getattr(self.context, attr, None)
            if value and isinstance(value, str):
                return value
        try:
            platforms = getattr(getattr(self.context, "platform_manager", None), "platform_insts", []) or []
            for platform in platforms:
                pid = getattr(platform.meta(), "id", None)
                if pid:
                    return str(pid)
        except Exception:  # noqa: BLE001
            pass
        return str(self.config.get("owner_notify_platform") or "aiocqhttp").strip() or "aiocqhttp"

    def _pending_challenge_lines(self) -> list[str]:
        self._prune_expired_pending_challenges()
        if not self.pending_owner_challenges:
            return []
        lines: list[str] = []
        for challenge_id, event in sorted(self.pending_owner_challenges.items(), key=lambda item: item[1].get("received_at", 0), reverse=True):
            challenger = self._challenge_value(event, "challenger_name", "challengerName", "challenger", "from_name") or "未知对手"
            side = self._challenge_value(event, "side", "bot_side", "botSide", "color", "assigned_side") or "未知"
            expires_at = self._challenge_value(event, "expires_at", "expiresAt") or "未知"
            lines.append(f"- {challenge_id}：{challenger}，红黑/先后={side}，过期={expires_at}")
        return lines

    def _latest_pending_challenge_id(self) -> str:
        self._prune_expired_pending_challenges()
        if not self.pending_owner_challenges:
            return ""
        return max(self.pending_owner_challenges.items(), key=lambda item: item[1].get("received_at", 0))[0]

    def _prune_expired_pending_challenges(self) -> None:
        if not self.pending_owner_challenges:
            return
        now = time.time()
        timeout = max(1, self.owner_decision_timeout_sec)
        expired = []
        for cid, item in self.pending_owner_challenges.items():
            if self._challenge_expired(item) or now - float(item.get("received_at") or now) > timeout:
                expired.append(cid)
        for cid in expired:
            self.pending_owner_challenges.pop(cid, None)

    async def _submit_owner_decision(self, challenge_id: str, decision: str, reason: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"decision": decision}
        if reason:
            payload["reason"] = reason
        _base, status, text = await self._request_text_with_fallback(
            "POST",
            f"/api/challenges/{quote(str(challenge_id), safe='')}/owner_decision",
            json_payload=payload,
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if status >= 400:
            raise RuntimeError(f"owner_decision failed: HTTP {status} {text[:200]}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"raw": text}

    def _format_owner_decision_result(self, challenge_id: str, decision: str, data: dict[str, Any]) -> str:
        if decision == "reject":
            return f"已拒绝挑战 {challenge_id}。"
        match_url = str(data.get("match_url") or data.get("matchUrl") or "").strip()
        match = data.get("match") if isinstance(data.get("match"), dict) else {}
        challenge = data.get("challenge") if isinstance(data.get("challenge"), dict) else data
        if not match_url and isinstance(match, dict):
            match_url = str(match.get("match_url") or match.get("url") or "").strip()
        match_id = ""
        if isinstance(match, dict):
            match_id = str(match.get("id") or match.get("match_id") or match.get("matchId") or "").strip()
        status = str(challenge.get("status") or data.get("status") or "").strip() if isinstance(challenge, dict) else ""
        parts = [f"已同意挑战 {challenge_id}。"]
        if match_id:
            parts.append(f"对局ID：{match_id}")
        if match_url:
            parts.append(f"对局链接：{match_url}")
        elif status:
            parts.append(f"状态：{status}")
        return "\n".join(parts)

    @filter.command("斗地主房间")
    async def cardroom_pool_command(self, event: AstrMessageEvent):
        """查看 5 个斗地主房间槽。"""
        yield event.plain_result(await self._card_tool_pool_status())

    @filter.command("斗地主状态")
    async def cardroom_pool_status_command(self, event: AstrMessageEvent):
        """查看 5 个斗地主房间槽。"""
        yield event.plain_result(await self._card_tool_pool_status())

    @filter.command("斗地主加入")
    async def cardroom_pool_join_command(self, event: AstrMessageEvent, slot: str = "1"):
        """加入指定斗地主房间槽。"""
        yield event.plain_result(await self._card_tool_pool_join(slot))

    @filter.command("斗地主退出")
    async def cardroom_pool_leave_command(self, event: AstrMessageEvent, slot: str = "1"):
        """退出指定斗地主房间槽。"""
        yield event.plain_result(await self._card_tool_pool_leave(slot))

    @filter.command("斗地主开始")
    async def cardroom_pool_start_command(self, event: AstrMessageEvent, slot: str = "1"):
        """手动启动指定斗地主房间槽；满 3 人时网站也会自动开局。"""
        yield event.plain_result(await self._card_tool_pool_start(slot))

    @filter.command("斗地主创建")
    async def cardroom_create_command(self, event: AstrMessageEvent, seed: int = 0, landlord_index: int = 0):
        """直接创建一个调试用斗地主 CardRoom 房间。"""
        if not self.llm_tools_allow_actions:
            yield event.plain_result("斗地主创建需要先开启 llm_tools_allow_actions。")
            return
        yield event.plain_result(await self._card_tool_create_room(seed=seed, landlord_index=landlord_index))

    @filter.command("斗地主看牌")
    async def cardroom_view_command(self, event: AstrMessageEvent, room_id: str = "", seat: str = "0", token: str = ""):
        """查看自己的斗地主私有视角。"""
        yield event.plain_result(await self._card_tool_get_room(room_id=room_id, seat=seat, token=token or None))

    @filter.command("斗地主合法")
    async def cardroom_legal_command(self, event: AstrMessageEvent, room_id: str = "", seat: str = "0", token: str = ""):
        """查看当前 seat 的合法动作。"""
        yield event.plain_result(await self._card_tool_get_legal_actions(room_id=room_id, seat=seat, token=token or None))

    @filter.command("斗地主决策")
    async def cardroom_prompt_decision_command(
        self,
        event: AstrMessageEvent,
        room_id: str = "",
        seat: str = "0",
        action: str = "",
        cards: str = "",
    ):
        """提交一次 Prompt 决策；为空时自动从合法动作里选最小合法牌。"""
        if not self.llm_tools_allow_actions:
            yield event.plain_result("斗地主决策需要先开启 llm_tools_allow_actions。")
            return
        yield event.plain_result(await self._card_tool_prompt_decision(room_id=room_id, seat=seat, action=action, cards=cards))

    @filter.command("棋擂台状态")
    async def arena_status(self, event: AstrMessageEvent):
        """查看棋擂台连接和自动对弈状态。"""
        last_event = "无" if not self.state.last_event_at else time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.state.last_event_at)
        )
        status = "在线" if self.state.connected else "离线"
        token_status = "已配置" if self.token else "未配置"
        active_lines = self._active_match_lines()
        recent_finished = self._latest_finished_match_line()
        msg = (
            f"棋擂台状态：{status}\n"
            f"平台：{self.arena_base}\n"
            f"Bot：{self.effective_bot_name}\n"
            f"Token：{token_status}（{self._token_hint(self.token)}）\n"
            f"引擎/棋风：{self.engine_mode}/{self.effective_chess_style}\n"
            f"引擎链：{' -> '.join(self._engine_chain())}\n"
            f"本地Node：{self._local_engine_node_status()}\n"
            f"自动注册/旧自动接挑战：{self.auto_register}/{self.auto_accept_challenges}\n"
            f"挑战处理模式：{self.challenge_decision_mode}\n"
            f"主人通知目标：{'已配置' if self._owner_notify_target_list() else '未配置'}\n"
            f"待主人确认：{len(self._pending_challenge_lines())}\n"
            f"进行中对局：{len(active_lines)}\n"
            f"走棋台词：{self.commentary_enabled}\n"
            f"LLM模型：{self._llm_provider_status()}\n"
            f"已接挑战/已走棋：{self.state.accepted_challenges}/{self.state.submitted_moves}\n"
            f"重连次数：{self.state.reconnect_count}\n"
            f"最近事件：{last_event}"
        )
        if active_lines:
            msg += "\n" + "\n".join(active_lines[:5])
        if recent_finished:
            msg += f"\n最近结束：{recent_finished}"
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

    @filter.command("棋擂台待确认")
    async def arena_pending_challenges(self, event: AstrMessageEvent):
        """查看等待主人审批的挑战。"""
        lines = self._pending_challenge_lines()
        if not lines:
            yield event.plain_result("当前没有待确认挑战。")
            return
        yield event.plain_result(
            "待确认挑战：\n"
            + "\n".join(lines)
            + "\n回复：棋擂台同意 <id> / 棋擂台拒绝 <id>；不填 id 则处理最新一条。"
        )

    @filter.command("棋擂台同意")
    async def arena_accept_pending(self, event: AstrMessageEvent, challenge_id: str = ""):
        """同意待审批挑战；不传 id 时使用最新 pending。"""
        cid = str(challenge_id or self._latest_pending_challenge_id()).strip()
        if not cid:
            yield event.plain_result("当前没有待确认挑战。")
            return
        try:
            data = await self._submit_owner_decision(cid, "accept")
            self.pending_owner_challenges.pop(cid, None)
            self.state.accepted_challenges += 1
            yield event.plain_result(self._format_owner_decision_result(cid, "accept", data))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"同意挑战失败：{exc}")

    @filter.command("棋擂台拒绝")
    async def arena_reject_pending(self, event: AstrMessageEvent, challenge_id: str = ""):
        """拒绝待审批挑战；不传 id 时使用最新 pending。"""
        cid = str(challenge_id or self._latest_pending_challenge_id()).strip()
        if not cid:
            yield event.plain_result("当前没有待确认挑战。")
            return
        try:
            data = await self._submit_owner_decision(cid, "reject", reason="owner rejected from AstrBot")
            self.pending_owner_challenges.pop(cid, None)
            yield event.plain_result(self._format_owner_decision_result(cid, "reject", data))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拒绝挑战失败：{exc}")

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

    async def _challenge_bot(self, opponent: dict[str, Any], side: str, game: str = "") -> str:
        opponent_id = self._bot_id(opponent)
        normalized_game = self._normalize_game(game)
        payload = {"opponent_bot_id": opponent_id, "side": side, "game": normalized_game}
        _base, status, data, text = await self._api_json(
            "POST",
            "/api/challenges",
            json_payload=payload,
            headers=self._auth_headers(),
            timeout=aiohttp.ClientTimeout(total=10),
        )
        return self._format_challenge_reply(opponent, side, status, data, text)

    @filter.command("棋擂台挑战")
    async def arena_challenge(self, event: AstrMessageEvent, target: str = "", side_text: str = "随机"):
        """向指定名字或 bot_id 发起挑战。"""
        if not target:
            yield event.plain_result("用法：棋擂台挑战 <名字或bot_id> [红|黑|随机]")
            return
        try:
            side = self._parse_side(side_text)
            bot, error = await self._find_bot(target)
            if error or not bot:
                yield event.plain_result(error or "没找到对手。")
                return
            yield event.plain_result(await self._challenge_bot(bot, side))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"挑战失败：{exc}")

    @filter.command("棋擂台找对手")
    async def arena_find_opponent(self, event: AstrMessageEvent, mode: str = "在线"):
        """自动挑一个可用对手并挑战。"""
        try:
            bots = [bot for bot in await self._fetch_bots() if not self._is_self_bot(bot) and self._bot_available(bot)]
            if not bots:
                yield event.plain_result("现在没找到在线可用对手。")
                return
            mode = str(mode or "在线").strip()
            if mode == "随机":
                opponent = random.choice(bots)
            else:
                bots = sorted(bots, key=self._bot_priority, reverse=True)
                if mode == "强一点":
                    opponent = max(bots, key=lambda b: float(b.get("rating") or b.get("score") or b.get("elo") or 0))
                elif mode == "弱一点":
                    opponent = min(bots, key=lambda b: float(b.get("rating") or b.get("score") or b.get("elo") or 0))
                else:
                    opponent = bots[0]
            yield event.plain_result(await self._challenge_bot(opponent, "random"))
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"找对手失败：{exc}")

    def _match_bot_ids(self, match: dict[str, Any]) -> set[str]:
        ids: set[str] = set()
        for key in ("red_bot_id", "black_bot_id", "winner_bot_id"):
            value = match.get(key)
            if value is not None and str(value).strip():
                ids.add(str(value).strip())
        for side in ("red", "black"):
            bot = match.get(side) or match.get(f"{side}_bot")
            if isinstance(bot, dict) and self._bot_id(bot):
                ids.add(self._bot_id(bot))
        return ids

    def _match_involves_me(self, match: dict[str, Any]) -> bool:
        my_id = self._my_bot_id()
        if my_id and my_id in self._match_bot_ids(match):
            return True
        names = {str(match.get("red_name") or match.get("red_bot_name") or ""), str(match.get("black_name") or match.get("black_bot_name") or "")}
        return self.effective_bot_name in names

    @staticmethod
    def _match_status(match: dict[str, Any]) -> str:
        return str(match.get("status") or match.get("state") or "").lower()

    def _match_side_name(self, match: dict[str, Any], side: str) -> str:
        bot = match.get(side) or match.get(f"{side}_bot")
        if isinstance(bot, dict):
            return self._bot_name(bot) or self._bot_id(bot)
        return str(match.get(f"{side}_name") or match.get(f"{side}_bot_name") or match.get(f"{side}_bot_id") or "未知")

    def _match_ply(self, match: dict[str, Any]) -> Any:
        moves = match.get("moves") or match.get("move_list")
        if isinstance(moves, list):
            return len(moves)
        return match.get("ply") or match.get("move_count") or match.get("moves_count") or 0

    async def _fetch_matches(self) -> list[dict[str, Any]]:
        for path in ("/api/admin/matches?limit=30", "/api/matches?limit=30"):
            _base, status, data, _text = await self._api_json(
                "GET",
                path,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if status < 400:
                return self._list_from_response(data, "matches", "items", "data")
        return []

    @filter.command("棋擂台当前")
    async def arena_current(self, event: AstrMessageEvent):
        try:
            active = [m for m in await self._fetch_matches() if self._match_involves_me(m) and self._match_status(m) in {"active", "playing", "started", "running"}]
            if not active:
                yield event.plain_result("当前没在下。")
                return
            match = active[0]
            match_id = match.get("match_id") or match.get("id")
            turn = match.get("turn") or match.get("current_turn") or match.get("side_to_move") or "未知"
            yield event.plain_result(f"当前对局：红 {self._match_side_name(match, 'red')} vs 黑 {self._match_side_name(match, 'black')}。{self._match_ply(match)}手，轮到{self._side_cn(turn)}。\n{self._match_url(match_id)}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"查询当前对局失败：{exc}")

    @filter.command("棋擂台最近")
    async def arena_recent(self, event: AstrMessageEvent):
        try:
            mine = [m for m in await self._fetch_matches() if self._match_involves_me(m)]
            if not mine:
                yield event.plain_result("还没找到我的最近对局。")
                return
            match = mine[0]
            match_id = match.get("match_id") or match.get("id")
            winner = str(match.get("winner_bot_id") or "")
            my_id = self._my_bot_id()
            result = "和棋" if not winner else ("赢了" if my_id and winner == my_id else "输了")
            opponent = self._match_side_name(match, "black") if self._match_side_name(match, "red") == self.effective_bot_name else self._match_side_name(match, "red")
            reason = match.get("finish_reason") or match.get("reason") or "未知原因"
            yield event.plain_result(f"最近一局：{result}，对手 {opponent}，{self._match_ply(match)}手，原因：{reason}。\n{self._match_url(match_id)}")
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"查询最近对局失败：{exc}")

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
        if self._cardroom_task and not self._cardroom_task.done():
            self._cardroom_task.cancel()
            try:
                await self._cardroom_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
