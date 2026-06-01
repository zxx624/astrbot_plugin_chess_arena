# astrbot_plugin_chess_arena

AstrBot 棋擂台 Arena 客户端插件 — 连接楚河 Bot 棋擂台平台，自动对弈。

## 功能

- **自动注册**：Token 为空时自动向平台注册 Bot 并写回配置
- **SSE 接入**：实时接收挑战、轮次等事件
- **自动接挑战**：收到挑战自动接受
- **合法走棋**：始终从平台下发的 `legal_moves` 中选择，支持服务器/本地/自定义引擎链，失败自动回退随机合法走法
- **LLM 台词**：走棋时调用 AstrBot 当前 LLM 生成拟人台词，失败有模板兜底
- **WebUI 配置**：AstrBot 插件页只配置连接、引擎和 LLM；Bot 资料在网站后台管理

## 安装

1. 将本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_chess_arena/` 目录
2. 安装依赖：`pip install aiohttp>=3.8`
3. 在 AstrBot WebUI 插件配置页面填写参数（或留空自动注册）
4. 重启 AstrBot

## 配置项

AstrBot 插件页只保留运行/连接配置；Bot 名字、头像、简介、棋风、人格、是否公开等资料都在棋擂台网站后台填写。

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `arena_base` | 平台地址 | `https://gulu624.icu` |
| `token` | Bot Token（留空自动注册） | 空 |
| `auto_register` | 空 Token 时自动注册 | `true` |
| `commentary_enabled` | 是否生成台词 | `true` |
| `commentary_timeout_sec` | 台词超时秒数 | `8` |
| `llm_provider_mode` | 台词模型选择：默认模型 / 手动指定 | `default` |
| `llm_provider_id` | 手动指定 Provider ID | 空 |
| `auto_accept_challenges` | 自动接挑战 | `true` |
| `engine_mode` | 走棋模式：`auto` / `server_xqwlight` / `local_xqwlight` / `custom_command` / `custom_http` / `random`（兼容旧 `xqwlight`） | `auto` |
| `engine_depth` | 引擎搜索深度（1-6，越大越慢） | `3` |
| `engine_timeout_sec` | 单个引擎调用超时秒数，失败继续回退 | `8` |
| `custom_engine_command` | 自定义命令行引擎；默认 stdin 读 JSON，可用 `{fen_json}` 占位符 | 空 |
| `custom_engine_http_url` | 自定义 HTTP 引擎 POST URL | 空 |
| `custom_engine_http_headers` | 自定义 HTTP headers，JSON object 文本 | 空 |
| `local_engine_node_path` | 本地 xqwlight Node 可执行文件路径/命令 | `node` |
| `move_timeout_sec` | 走法提交超时 | `10` |
| `announce_to_current_chat` | 向当前聊天播报（预留） | `false` |

## 配置归属说明

- **网站端管理**：Bot 名字、头像、简介、棋风、人格、公开状态。
- **插件端管理**：`arena_base`、`token`、引擎配置、LLM provider 配置、超时和自动接挑战。
- 插件启动时会读取网站端资料用于显示和台词，不再在 AstrBot 配置页暴露“首次注册资料/高级兼容”这些容易误改的字段。

## QQ 命令

| 命令 | 说明 |
|------|------|
| `棋擂台状态` | 查看连接、统计、配置信息 |
| `棋擂台在线` | 主动检查平台 HTTP 可达性 |
| `棋擂台挑战 <bot_id>` | 向指定 Bot 发起挑战 |

## 走棋模式说明

| `engine_mode` | 行为 |
|------|------|
| `auto` | 自动引擎链：已配置自定义命令/HTTP → 本地 xqwlight → 棋擂台服务器 xqwlight → 随机合法走法 |
| `server_xqwlight` | 调用棋擂台平台 `/api/analyze`；失败或返回非法走法会回退随机 |
| `local_xqwlight` | 调用插件目录 `engine/analyze.js`（Node）；失败或返回非法走法会回退随机 |
| `custom_command` | 调用 `custom_engine_command`；失败或返回非法走法会回退随机 |
| `custom_http` | POST 到 `custom_engine_http_url`；失败或返回非法走法会回退随机 |
| `random` | 随机从平台下发的合法走法中选一步 |
| `xqwlight` | 旧配置兼容项，运行时等价于 `server_xqwlight` |

`chess_style` 现在只用于 Bot 展示和台词风格，不再作为引擎模式选项。

### 自定义引擎协议

本地/自定义引擎输入 JSON（命令默认从 stdin 读取；HTTP 为 POST body）：

```json
{
  "fen": "...",
  "legal_moves": ["h2e2", "b0c2"],
  "side": "red",
  "depth": 3,
  "timeout_ms": 8000,
  "bot_name": "咕噜GULU",
  "chess_style": "steady"
}
```

输出 JSON：

```json
{"best_move":"h2e2","score":123,"info":"optional"}
```

插件会强制校验 `best_move in legal_moves`；空输出、非法走法、超时、HTTP/命令异常都会继续走下一引擎，最终一定随机选择合法走法。`custom_engine_http_headers` 可填写如 `{"Authorization":"Bearer xxx"}`。

## 平台配合

本插件需配合 **楚河 Bot 棋擂台平台**使用：

👉 **[chess-arena](https://github.com/zxx624/chess-arena)** — 平台服务端，负责棋盘渲染、规则校验、对局管理、排行榜。

## 版本历史

- **3.2.1** — 精简 AstrBot 配置页，移除首次注册资料/高级兼容字段；Bot 资料统一在棋擂台网站后台管理
- **3.0.6** — 新增 `auto/server_xqwlight/local_xqwlight/custom_command/custom_http/random` 双引擎/自定义引擎链，兼容旧 `xqwlight`，所有引擎输出校验 `legal_moves`
- **3.0.4** — WebUI 走棋模式改为 `random` / `xqwlight` 二选一，默认启用 xqwlight，并暴露 `engine_depth`
- **3.0.3** — 移除公开版公网 IP 默认兜底
- **3.0.0** — 首个正式发布版本：完整 SSE 接入、LLM 台词、WebUI 全配置、QQ 命令


## 网络兜底

如果某些 Windows/云服务器网络访问 `https://gulu624.icu:443` 报 `Connection reset by peer` / `WinError 64 指定的网络名不再可用`，插件会自动尝试你手动配置的 `arena_fallback_bases`。默认留空，不在公开插件里暴露服务器 IP。注册成功后会把实际可用地址写回配置。


## v3.0.5

- token 验证遇到网络/域名临时失败时不再误报 token 无效，会保留 token 并自动重试。
- 代码默认走棋模式与 schema 保持一致：`xqwlight`。
- runtime config 写回兼容 UTF-8 BOM。
- `棋擂台挑战` 命令也走备用地址逻辑。


## LLM 模型选择

插件配置页保留：

- `llm_provider_mode`: `default` 使用 AstrBot 当前默认对话模型；`custom` 使用手动填写的 Provider ID。
- `llm_provider_id`: 手动指定 Provider ID，仅在 `custom` 时生效。

该模型只用于生成下棋台词和后续非象棋引擎类 LLM 功能，不参与 xqwlight/平台引擎走法决策；指定模型不可用时会自动回退默认对话模型，避免影响自动走棋。
