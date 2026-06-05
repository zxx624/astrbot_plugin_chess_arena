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
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

try:  # AstrBot 新版优先从 astrbot.api 暴露 FunctionTool。
    from astrbot.api import FunctionTool as AstrFunctionTool
except Exception:  # noqa: BLE001 - 不同 AstrBot 版本可能没有该导出
    AstrFunctionTool = None

try:  # 兼容部分 AstrBot 版本的底层工具类型。
    from astrbot.api import ToolExecResult as AstrToolExecResult
except Exception:  # noqa: BLE001
    AstrToolExecResult = None

try:
    from pydantic import Field
    from pydantic.dataclasses import dataclass as pydantic_dataclass
except Exception:  # noqa: BLE001 - 注册 LLM 工具失败不能影响插件启动
    Field = None
    pydantic_dataclass = None


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
        self.auto_accept_challenges = bool(self.config.get("auto_accept_challenges", True))
        self.challenge_decision_mode = self._normalize_challenge_decision_mode(self.config.get("challenge_decision_mode"))
        self.server_challenge_policy = self._server_challenge_policy_for_mode(self.challenge_decision_mode)
        self.owner_notify_enabled = self._config_bool(self.config.get("owner_notify_enabled"), default=True)
        self.owner_notify_targets = str(self.config.get("owner_notify_targets") or "").strip()
        self.owner_decision_timeout_sec = max(1, int(self.config.get("owner_decision_timeout_sec") or 180))
        self.match_report_enabled = self._config_bool(self.config.get("match_report_enabled"), default=True)
        self.pending_owner_challenges: dict[str, dict[str, Any]] = {}
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
        self._session: aiohttp.ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._llm_tools_registered = False

        if self.llm_tools_enabled:
            self._register_llm_tools_safe()

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

    def _register_llm_tools_safe(self) -> None:
        """向 AstrBot 默认聊天模型注册棋擂台工具；任何失败都只记录 warning。"""
        try:
            registrar = getattr(self.context, "add_llm_tools", None)
            if not callable(registrar):
                logger.warning("[ChessArena] 当前 AstrBot Context 不支持 add_llm_tools，跳过棋擂台 LLM 工具注册。")
                return

            function_tool_cls, tool_exec_result_cls = self._llm_tool_classes()
            if function_tool_cls is None:
                logger.warning("[ChessArena] 当前 AstrBot 版本未提供 FunctionTool，跳过棋擂台 LLM 工具注册。")
                return

            tools = [
                self._build_llm_function_tool(
                    function_tool_cls,
                    tool_exec_result_cls,
                    "chess_arena_status",
                    "查看棋擂台连接状态、Bot、平台、引擎链和待确认挑战数量。",
                    self._llm_tool_status,
                    self._tool_parameters({}),
                ),
                self._build_llm_function_tool(
                    function_tool_cls,
                    tool_exec_result_cls,
                    "chess_arena_find_bots",
                    "查询或列出可挑战的棋擂台 Bot，最多返回 8 个。",
                    self._llm_tool_find_bots,
                    self._tool_parameters(
                        {
                            "query": {"type": "string", "description": "Bot 名称或 bot_id 关键词；空则列出可见 Bot。"},
                        }
                    ),
                ),
                self._build_llm_function_tool(
                    function_tool_cls,
                    tool_exec_result_cls,
                    "chess_arena_challenge",
                    "按名字或 bot_id 向棋擂台 Bot 发起挑战。",
                    self._llm_tool_challenge,
                    self._tool_parameters(
                        {
                            "opponent": {"type": "string", "description": "对手 Bot 名称或 bot_id。"},
                            "side": {"type": "string", "description": "我方执红/黑/随机；允许 red、black、random、红、黑。"},
                        },
                        required=["opponent"],
                    ),
                ),
                self._build_llm_function_tool(
                    function_tool_cls,
                    tool_exec_result_cls,
                    "chess_arena_pending_challenges",
                    "列出等待主人审批的棋擂台挑战。",
                    self._llm_tool_pending_challenges,
                    self._tool_parameters({}),
                ),
                self._build_llm_function_tool(
                    function_tool_cls,
                    tool_exec_result_cls,
                    "chess_arena_owner_decision",
                    "同意或拒绝一条等待主人审批的棋擂台挑战；不传 challenge_id 时处理最新一条。",
                    self._llm_tool_owner_decision,
                    self._tool_parameters(
                        {
                            "challenge_id": {"type": "string", "description": "挑战 ID；为空时默认最新一条待确认。"},
                            "decision": {"type": "string", "description": "只能是 accept 或 reject。"},
                            "reason": {"type": "string", "description": "拒绝原因，可为空；不要包含隐私或凭据。"},
                        },
                    ),
                ),
            ]
            self._call_add_llm_tools(registrar, tools)
            self._llm_tools_registered = True
            self._routine_log("[ChessArena] 已注册棋擂台 LLM 工具: %s", ", ".join(getattr(tool, "name", "") for tool in tools))
        except Exception as exc:  # noqa: BLE001 - LLM 工具不可影响插件启动/SSE/自动走棋
            logger.warning("[ChessArena] 注册棋擂台 LLM 工具失败，已跳过: %s", exc)

    @staticmethod
    def _tool_parameters(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": required or []}

    @staticmethod
    def _llm_tool_classes() -> tuple[Any, Any]:
        function_tool_cls = AstrFunctionTool
        tool_exec_result_cls = AstrToolExecResult
        if function_tool_cls is None:
            try:
                from astrbot.core.agent.tool import FunctionTool as CoreFunctionTool  # type: ignore
                from astrbot.core.agent.tool import ToolExecResult as CoreToolExecResult  # type: ignore

                function_tool_cls = CoreFunctionTool
                tool_exec_result_cls = tool_exec_result_cls or CoreToolExecResult
            except Exception:  # noqa: BLE001
                return None, tool_exec_result_cls
        elif tool_exec_result_cls is None:
            try:
                from astrbot.core.agent.tool import ToolExecResult as CoreToolExecResult  # type: ignore

                tool_exec_result_cls = CoreToolExecResult
            except Exception:  # noqa: BLE001
                pass
        return function_tool_cls, tool_exec_result_cls

    def _build_llm_function_tool(
        self,
        function_tool_cls: Any,
        tool_exec_result_cls: Any,
        name: str,
        description: str,
        handler: Any,
        parameters: dict[str, Any],
    ) -> Any:
        async def callback(*args: Any, **kwargs: Any) -> Any:
            kwargs.pop("ctx", None)
            kwargs.pop("context", None)
            return self._llm_tool_result(await handler(**kwargs), tool_exec_result_cls)

        constructors = [
            lambda: function_tool_cls(name=name, description=description, parameters=parameters, func=callback),
            lambda: function_tool_cls(name=name, description=description, parameters=parameters, handler=callback),
            lambda: function_tool_cls(name=name, desc=description, parameters=parameters, func=callback),
            lambda: function_tool_cls(callback, name=name, description=description, parameters=parameters),
            lambda: function_tool_cls(name, description, parameters, callback),
        ]
        for factory_name in ("from_callable", "from_function", "from_func"):
            factory = getattr(function_tool_cls, factory_name, None)
            if callable(factory):
                constructors.append(lambda factory=factory: factory(callback, name=name, description=description, parameters=parameters))
        for constructor in constructors:
            try:
                return constructor()
            except Exception:  # noqa: BLE001 - 尝试其它 AstrBot 版本的构造方式
                continue

        if pydantic_dataclass is not None and Field is not None:
            plugin = self

            @pydantic_dataclass
            class ChessArenaLLMTool(function_tool_cls):  # type: ignore[misc, valid-type]
                name: str = ""
                description: str = ""
                parameters: dict[str, Any] = Field(default_factory=dict)
                handler: Any = Field(default=None, repr=False)
                plugin: Any = Field(default=plugin, repr=False)
                result_cls: Any = Field(default=tool_exec_result_cls, repr=False)

                async def run(self, ctx: Any = None, **kwargs: Any) -> Any:  # noqa: ANN001
                    result = self.handler(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                    return self.plugin._llm_tool_result(result, self.result_cls)

            return ChessArenaLLMTool(name=name, description=description, parameters=parameters, handler=handler)

        raise RuntimeError(f"cannot construct FunctionTool {name}")

    def _call_add_llm_tools(self, registrar: Any, tools: list[Any]) -> None:
        try:
            signature = inspect.signature(registrar)
            params = list(signature.parameters.values())
            use_varargs = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params)
        except Exception:  # noqa: BLE001
            use_varargs = False
        calls = [lambda: registrar(*tools), lambda: registrar(tools)] if use_varargs else [lambda: registrar(tools), lambda: registrar(*tools)]
        last_error = ""
        for call in calls:
            try:
                result = call()
                if inspect.isawaitable(result):
                    try:
                        loop = asyncio.get_running_loop()
                        task = loop.create_task(result, name="chess_arena_add_llm_tools")
                        task.add_done_callback(self._log_add_llm_tools_task_result)
                    except RuntimeError:
                        logger.warning("[ChessArena] add_llm_tools 返回 awaitable，但当前无运行事件循环；已跳过等待。")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
        raise RuntimeError(last_error or "add_llm_tools call failed")

    @staticmethod
    def _log_add_llm_tools_task_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ChessArena] 异步注册棋擂台 LLM 工具失败，已跳过: %s", exc)

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
            return (
                f"连接：{status}\n"
                f"Bot：{self.effective_bot_name}\n"
                f"平台：{self.arena_base}\n"
                f"引擎链：{engine_chain}\n"
                f"待确认：{pending_count}"
            )
        except Exception as exc:  # noqa: BLE001
            return f"查询状态失败：{exc}"

    async def _llm_tool_find_bots(self, query: str = "") -> str:
        try:
            bots = [bot for bot in await self._fetch_bots(str(query or "").strip()) if not self._is_self_bot(bot)]
            bots = sorted(bots, key=self._bot_priority, reverse=True)[:8]
            if not bots:
                return "未找到可列出的 Bot。"
            lines = []
            for bot in bots:
                name = self._bot_name(bot) or "未命名"
                bot_id = self._bot_id(bot) or "未知ID"
                online = "在线" if bool(bot.get("online", bot.get("is_online", False))) else "离线"
                available = "可用" if self._bot_available(bot) else "不可用"
                lines.append(f"- {name} ({bot_id})：{online}/{available}")
            return "可挑战 Bot：\n" + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"查询 Bot 失败：{exc}"

    async def _llm_tool_challenge(self, opponent: str = "", side: str = "random") -> str:
        try:
            if not self.llm_tools_allow_actions:
                return "LLM 工具操作权限未开启：只能查询，不能发起挑战。"
            opponent = str(opponent or "").strip()
            if not opponent:
                return "请提供 opponent（名字或 bot_id）。"
            if not self.token:
                return "Token 未配置，无法发起挑战。"
            parsed_side = self._parse_side(side)
            bot, error = await self._find_bot(opponent)
            if error or not bot:
                return error or "没找到对手。"
            return await self._challenge_bot(bot, parsed_side)
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

    async def _llm_tool_owner_decision(self, challenge_id: str = "", decision: str = "accept", reason: str = "") -> str:
        try:
            if not self.llm_tools_allow_actions:
                return "LLM 工具操作权限未开启：只能查询，不能同意/拒绝挑战。"
            decision = str(decision or "accept").strip().lower()
            if decision not in {"accept", "reject"}:
                return "decision 只能是 accept 或 reject。"
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
    def _bot_priority(bot: dict[str, Any]) -> tuple[int, int, int]:
        online = bool(bot.get("online", bot.get("is_online", False)))
        enabled = bool(bot.get("is_enabled", bot.get("enabled", True)))
        public = bool(bot.get("is_public", bot.get("public", True)))
        return (1 if online else 0, 1 if enabled else 0, 1 if public else 0)

    @staticmethod
    def _bot_available(bot: dict[str, Any]) -> bool:
        online = bot.get("online", bot.get("is_online", False))
        enabled = bot.get("is_enabled", bot.get("enabled", True))
        public = bot.get("is_public", bot.get("public", True))
        return bool(online) and bool(enabled) and bool(public)

    async def _fetch_bots(self, query: str = "") -> list[dict[str, Any]]:
        path = "/api/bots"
        if query:
            path += f"?q={quote(query, safe='')}"
        _base, status, data, _text = await self._api_json(
            "GET",
            path,
            headers=self._auth_headers() if self.token else None,
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if status >= 400 and query:
            _base, status, data, _text = await self._api_json(
                "GET",
                "/api/bots",
                headers=self._auth_headers() if self.token else None,
                timeout=aiohttp.ClientTimeout(total=10),
            )
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return self._list_from_response(data, "bots", "items", "data")

    async def _find_bot(self, query: str, exclude_self: bool = True) -> tuple[dict[str, Any] | None, str]:
        query = str(query or "").strip()
        if not query:
            return None, "你要挑战谁？用法：棋擂台挑战 <名字或bot_id> [红|黑|随机]"
        bots = await self._fetch_bots(query)
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
                    self._routine_log("[ChessArena] 已写回注册信息到 runtime config: %s token=%s", path, self._token_hint(token))
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
            "commentary_enabled": self.commentary_enabled,
            "commentary_timeout_sec": self.commentary_timeout_sec,
            "llm_provider_mode": self.llm_provider_mode,
            "llm_provider_id": self.llm_provider_id,
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

    async def _handle_challenge_received(self, event: dict[str, Any]) -> None:
        challenge_id = self._challenge_id(event)
        if not challenge_id:
            logger.warning("[ChessArena] challenge_received 缺少 id: %s", event)
            return

        mode = self.challenge_decision_mode
        if mode == "ignore":
            self._routine_log("[ChessArena] 已忽略挑战 %s：challenge_decision_mode=ignore", challenge_id)
            return
        if mode == "auto_accept":
            await self._accept_challenge(challenge_id)
            return

        # owner_approve：只登记/通知，不阻塞 SSE；主人稍后用命令同意/拒绝。
        record = dict(event)
        record["challenge_id"] = str(challenge_id)
        record["received_at"] = time.time()
        self.pending_owner_challenges[str(challenge_id)] = record
        text = self._owner_challenge_text(record)
        if self.owner_notify_enabled:
            try:
                await self._notify_owner(text)
            except Exception as exc:  # noqa: BLE001 - 主动通知失败不能影响 SSE
                logger.warning("[ChessArena] 主人挑战审批通知失败，已保留待确认: %s", exc)
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
            raise RuntimeError(f"submit move failed: HTTP {status} {text[:200]}")
        self.state.submitted_moves += 1
        self._routine_log("[ChessArena] match=%s 已提交走法: %s comment=%s", match_id, move, comment)

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
            for umo in self._target_umo_candidates(target):
                try:
                    result = sender(umo, chain)
                    if inspect.isawaitable(result):
                        await result
                    self._routine_log("[ChessArena] 已发送挑战审批通知到 %s", umo)
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ChessArena] 发送挑战审批通知到 %s 失败: %s", umo, exc)

    def _target_umo_candidates(self, target: str) -> list[str]:
        target = str(target or "").strip()
        if not target:
            return []
        if target.count(":") >= 2:
            return [target]
        platform = self._default_platform_id()
        candidates: list[str] = []
        if platform:
            candidates.append(f"{platform}:FriendMessage:{target}")
            candidates.append(f"{platform}:GroupMessage:{target}")
        return candidates or [target]

    def _default_platform_id(self) -> str:
        for attr in ("platform", "platform_id", "id"):
            value = getattr(self.context, attr, None)
            if value and isinstance(value, str):
                return value
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
        expired = [cid for cid, item in self.pending_owner_challenges.items() if now - float(item.get("received_at") or now) > timeout]
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
            f"Bot：{self.effective_bot_name}\n"
            f"Token：{token_status}（{self._token_hint(self.token)}）\n"
            f"引擎/棋风：{self.engine_mode}/{self.effective_chess_style}\n"
            f"引擎链：{' -> '.join(self._engine_chain())}\n"
            f"本地Node：{self._local_engine_node_status()}\n"
            f"自动注册/旧自动接挑战：{self.auto_register}/{self.auto_accept_challenges}\n"
            f"挑战处理模式：{self.challenge_decision_mode}\n"
            f"主人通知目标：{'已配置' if self._owner_notify_target_list() else '未配置'}\n"
            f"待主人确认：{len(self._pending_challenge_lines())}\n"
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

    async def _challenge_bot(self, opponent: dict[str, Any], side: str) -> str:
        opponent_id = self._bot_id(opponent)
        payload = {"opponent_bot_id": opponent_id, "side": side}
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
        if self._session and not self._session.closed:
            await self._session.close()
