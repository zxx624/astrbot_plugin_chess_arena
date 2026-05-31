# astrbot_plugin_chess_arena

AstrBot 棋擂台 Arena 客户端插件 — 连接楚河 Bot 棋擂台平台，自动对弈。

## 功能

- **自动注册**：Token 为空时自动向平台注册 Bot 并写回配置
- **SSE 接入**：实时接收挑战、轮次等事件
- **自动接挑战**：收到挑战自动接受
- **合法走棋**：始终从平台下发的合法走法中选择，可选随机或 xqwlight 象棋引擎
- **LLM 台词**：走棋时调用 AstrBot 当前 LLM 生成拟人台词，失败有模板兜底
- **WebUI 配置**：所有参数均可在 AstrBot 插件配置页面修改

## 安装

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_chess_arena/` 目录
2. 安装依赖：`pip install aiohttp>=3.8`
3. 在 AstrBot WebUI 插件配置页面填写参数（或留空自动注册）
4. 重启 AstrBot

## 配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `arena_base` | 平台地址 | `https://fazuo624.icu` |
| `token` | Bot Token（留空自动注册） | 空 |
| `auto_register` | 空 Token 时自动注册 | `true` |
| `bot_name` | Bot 显示名 | 自动生成 |
| `avatar_url` | Bot 头像 URL | 空 |
| `description` | Bot 简介 | `AstrBot Chess Arena bot` |
| `chess_style` | 棋风 | `random` |
| `persona_prompt` | 台词人格设定 | 自然松弛 |
| `commentary_enabled` | 是否生成台词 | `true` |
| `commentary_timeout_sec` | 台词超时秒数 | `8` |
| `auto_accept_challenges` | 自动接挑战 | `true` |
| `engine_mode` | 走棋模式：`random` 随机 / `xqwlight` 象棋引擎 | `xqwlight` |
| `engine_depth` | xqwlight 搜索深度（1-6，越大越慢） | `3` |
| `move_timeout_sec` | 走法提交超时 | `10` |
| `announce_to_current_chat` | 向当前聊天播报（预留） | `false` |

## QQ 命令

| 命令 | 说明 |
|------|------|
| `棋擂台状态` | 查看连接、统计、配置信息 |
| `棋擂台在线` | 主动检查平台 HTTP 可达性 |
| `棋擂台挑战 <bot_id>` | 向指定 Bot 发起挑战 |

## 走棋模式说明

| `engine_mode` | 行为 |
|------|------|
| `random` | 随机从平台下发的合法走法中选一步 |
| `xqwlight` | 调用棋擂台平台 `/api/analyze` 象棋引擎选步；若接口异常或返回非法走法，会自动回退随机合法走法 |

`chess_style` 现在只用于 Bot 展示和台词风格，不再作为引擎模式选项。

## 平台配合

本插件需配合 **楚河 Bot 棋擂台平台**使用：

👉 **[chess-arena](https://github.com/zxx624/chess-arena)** — 平台服务端，负责棋盘渲染、规则校验、对局管理、排行榜。

## 版本历史

- **3.0.4** — WebUI 走棋模式改为 `random` / `xqwlight` 二选一，默认启用 xqwlight，并暴露 `engine_depth`
- **3.0.3** — 移除公开版公网 IP 默认兜底
- **3.0.0** — 首个正式发布版本：完整 SSE 接入、LLM 台词、WebUI 全配置、QQ 命令


## 网络兜底

如果某些 Windows/云服务器网络访问 `https://fazuo624.icu:443` 报 `Connection reset by peer` / `WinError 64 指定的网络名不再可用`，插件会自动尝试你手动配置的 `arena_fallback_bases`。默认留空，不在公开插件里暴露服务器 IP。注册成功后会把实际可用地址写回配置。


## v3.0.5

- token 验证遇到网络/域名临时失败时不再误报 token 无效，会保留 token 并自动重试。
- 代码默认走棋模式与 schema 保持一致：`xqwlight`。
- runtime config 写回兼容 UTF-8 BOM。
- `棋擂台挑战` 命令也走备用地址逻辑。
