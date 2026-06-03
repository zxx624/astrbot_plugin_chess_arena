# AstrBot 棋擂台插件：自定义象棋引擎接口文档

版本：v1.0
适用对象：给 `astrbot_plugin_chess_arena` / 棋擂台 Bot 编写自定义中国象棋引擎的人
目标：让第三方引擎可以被插件调用，返回合法走法，由插件负责提交到棋擂台平台。

---

## 1. 总体说明

棋擂台插件的走棋逻辑可以分成两层：

1. **插件层**
   - 连接棋擂台平台；
   - 接收当前局面；
   - 获取当前合法走法列表；
   - 调用引擎；
   - 校验引擎返回的走法是否合法；
   - 把走法提交给棋擂台。

2. **引擎层**
   - 接收当前 FEN、合法走法、执棋方、搜索深度、超时时间等信息；
   - 计算最佳走法；
   - 返回 `best_move`；
   - 可选返回评分、引擎名、思考信息等。

重点：

> 自定义引擎只负责“选一步棋”，不要直接提交走法，不要直接操作棋擂台 API。

插件会统一负责：

- 权限；
- SSE 连接；
- 走法提交；
- 非法走法兜底；
- 随机走法 fallback；
- Bot 台词/LLM 解说。

---

## 2. 支持的自定义引擎形式

推荐支持两种接口形式。

### 2.1 命令行引擎：`custom_command`

插件通过子进程启动一个命令，把 JSON 请求写入 stdin，引擎从 stdout 输出 JSON 结果。

适合：

- 本地可执行文件；
- Python / Node.js / C++ / Rust 写的引擎；
- 不想常驻 HTTP 服务的引擎；
- Windows / Linux 都容易部署。

调用方式示意：

```bash
printf '%s' '{"fen":"...","legal_moves":["b2e2"],"side":"red"}' | python my_engine.py
```

引擎输出：

```json
{"best_move":"b2e2","score":12,"engine":"my_engine"}
```

### 2.2 HTTP 引擎：`custom_http`

插件向一个 HTTP 地址发送 POST JSON，引擎返回 JSON。

适合：

- 独立服务；
- 需要常驻内存的大引擎；
- 需要 GPU / 多进程 / 分布式；
- 多个 Bot 共用一个引擎。

请求示意：

```bash
curl -X POST http://127.0.0.1:8799/analyze \
  -H 'Content-Type: application/json' \
  -d '{"fen":"...","legal_moves":["b2e2"],"side":"red"}'
```

返回：

```json
{"best_move":"b2e2","score":12,"engine":"my_http_engine"}
```

---

## 3. 推荐插件引擎模式

插件配置里建议保留这些模式：

| 模式 | 含义 |
|---|---|
| `auto` | 自动尝试：自定义命令 → 自定义 HTTP → 本地 xqwlight → 服务器 xqwlight → 随机 |
| `custom_command` | 只调用命令行自定义引擎，失败则 fallback |
| `custom_http` | 只调用 HTTP 自定义引擎，失败则 fallback |
| `local_xqwlight` | 插件本地 Node.js xqwlight |
| `server_xqwlight` | 调用棋擂台服务器 `/api/analyze` |
| `xqwlight` | 旧配置兼容，等同于 `server_xqwlight` |
| `random` | 随机合法走法 |

推荐默认：

```json
{
  "engine_mode": "auto"
}
```

这样普通用户不用管，引擎坏了也不会导致 Bot 不走棋。

---

## 4. 输入 JSON 格式

无论命令行还是 HTTP，自定义引擎都应该接收同一种 JSON。

### 4.1 最小请求

```json
{
  "fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1",
  "legal_moves": ["b2e2", "h2e2"],
  "side": "red",
  "depth": 3,
  "timeout_ms": 8000
}
```

### 4.2 完整请求字段

```json
{
  "protocol": "xiangqi-engine-v1",
  "request_id": "match_abc_123_ply_18",
  "match_id": "match_abc_123",
  "bot_id": "bot_xxx",
  "fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1",
  "side": "red",
  "ply": 18,
  "legal_moves": ["b2e2", "h2e2", "a0a1"],
  "last_move": "h7e7",
  "history": ["b2e2", "h7e7"],
  "depth": 3,
  "timeout_ms": 8000,
  "red_time_left_ms": 550000,
  "black_time_left_ms": 542000,
  "total_time_ms": 600000,
  "engine_options": {
    "style": "balanced",
    "skill_level": 5,
    "use_book": true
  }
}
```

### 4.3 字段解释

| 字段 | 类型 | 必填 | 说明 |
|---|---:|---:|---|
| `protocol` | string | 否 | 协议名，推荐固定为 `xiangqi-engine-v1` |
| `request_id` | string | 否 | 本次请求 ID，方便日志排查 |
| `match_id` | string | 否 | 棋擂台对局 ID |
| `bot_id` | string | 否 | 当前 Bot ID |
| `fen` | string | 是 | 当前局面 FEN |
| `side` | string | 是 | 当前该谁走，`red` 或 `black` |
| `ply` | integer | 否 | 当前半回合数，从 0 或 1 开始都可以，仅供参考 |
| `legal_moves` | string[] | 强烈建议 | 当前所有合法走法，UCCI 格式 |
| `last_move` | string/null | 否 | 上一步走法，UCCI 格式 |
| `history` | string[] | 否 | 历史走法列表，UCCI 格式 |
| `depth` | integer | 否 | 建议搜索深度 |
| `timeout_ms` | integer | 否 | 插件允许引擎思考的最大毫秒数 |
| `red_time_left_ms` | integer | 否 | 红方剩余时间 |
| `black_time_left_ms` | integer | 否 | 黑方剩余时间 |
| `total_time_ms` | integer | 否 | 总用时配置 |
| `engine_options` | object | 否 | 用户自定义引擎参数 |

---

## 5. FEN 格式说明

### 5.1 标准初始局面

```text
rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1
```

这里采用中国象棋常见 FEN：

- `/` 分隔 10 行；
- 每行从黑方底线到红方底线；
- 小写是黑方；
- 大写是红方；
- `r` 表示红方走；
- `b` 表示黑方走。

### 5.2 棋子字母

| 字母 | 红方 | 黑方 | 含义 |
|---|---|---|---|
| `K/k` | 帅 | 将 | 将帅 |
| `A/a` | 仕 | 士 | 士/仕 |
| `B/b` 或 `E/e` | 相 | 象 | 象/相，兼容两种写法 |
| `N/n` 或 `H/h` | 马 | 马 | 马，兼容两种写法 |
| `R/r` | 车 | 車 | 车 |
| `C/c` | 炮 | 砲 | 炮 |
| `P/p` | 兵 | 卒 | 兵卒 |

注意：

- 有些引擎用 `N/B` 表示马/象；
- 有些中国象棋程序用 `H/E` 表示马/象；
- 自定义引擎最好两种都兼容。

### 5.3 行列方向

FEN 第 1 行是黑方底线，也就是棋盘顶部。

FEN 10 行对应：

```text
第 0 行：黑方底线
第 1 行
第 2 行
第 3 行
第 4 行
第 5 行
第 6 行
第 7 行
第 8 行
第 9 行：红方底线
```

---

## 6. 走法格式：UCCI 坐标

插件和引擎统一使用 UCCI 走法格式。

### 6.1 格式

```text
<from_file><from_rank><to_file><to_rank>
```

示例：

```text
b2e2
h0e2
```

### 6.2 file 文件列

从红方视角看，左到右：

```text
a b c d e f g h i
```

共 9 列。

### 6.3 rank 行号

rank 从红方底线开始：

```text
0 = 红方底线
1
2
3
4
5
6
7
8
9 = 黑方底线
```

这点非常重要：

> UCCI 的 rank 0 是红方底线，不是 FEN 第一行。

### 6.4 初始局面例子

初始局面中：

- 红方左炮在 `b2`；
- 红方右炮在 `h2`；
- 红方帅在 `e0`；
- 黑方将在 `e9`。

所以：

```text
b2e2 = 红方左炮平到中路
h0e2 = 红方马从 h0 到 e2
```

---

## 7. 输出 JSON 格式

### 7.1 最小成功返回

```json
{
  "best_move": "b2e2"
}
```

插件也可以兼容：

```json
{
  "move": "b2e2"
}
```

但推荐统一使用 `best_move`。

### 7.2 完整成功返回

```json
{
  "protocol": "xiangqi-engine-v1",
  "request_id": "match_abc_123_ply_18",
  "engine": "my-xiangqi-engine",
  "engine_version": "1.0.0",
  "best_move": "b2e2",
  "score": 35,
  "score_type": "cp",
  "depth": 5,
  "nodes": 123456,
  "elapsed_ms": 742,
  "pv": ["b2e2", "h7e7", "h0e2"],
  "reason": "central cannon pressure",
  "debug": {
    "legal_count": 44,
    "used_book": false
  }
}
```

### 7.3 返回字段解释

| 字段 | 类型 | 必填 | 说明 |
|---|---:|---:|---|
| `best_move` | string | 是 | 推荐走法，UCCI 格式 |
| `move` | string | 否 | 兼容字段；没有 `best_move` 时可用 |
| `engine` | string | 否 | 引擎名称 |
| `engine_version` | string | 否 | 引擎版本 |
| `score` | number | 否 | 局面评分 |
| `score_type` | string | 否 | `cp`、`mate`、`winrate` 等 |
| `depth` | integer | 否 | 实际搜索深度 |
| `nodes` | integer | 否 | 搜索节点数 |
| `elapsed_ms` | integer | 否 | 实际耗时 |
| `pv` | string[] | 否 | 主变走法 |
| `reason` | string | 否 | 简短解释，给日志/台词参考 |
| `debug` | object | 否 | 调试信息 |

---

## 8. 错误返回格式

引擎不能走时，应该返回错误 JSON，而不是随便返回非法走法。

### 8.1 推荐错误返回

```json
{
  "error": {
    "code": "no_move",
    "message": "no legal move found"
  },
  "engine": "my_engine",
  "elapsed_ms": 120
}
```

### 8.2 常见错误码

| code | 含义 |
|---|---|
| `invalid_fen` | FEN 无法解析 |
| `no_legal_moves` | 没有合法走法 |
| `timeout` | 引擎内部超时 |
| `unsupported_position` | 引擎不支持该局面 |
| `internal_error` | 内部异常 |
| `invalid_request` | 请求字段缺失或类型错误 |
| `illegal_best_move` | 算出来的走法不在合法走法列表里 |

插件收到错误后应：

1. 记录日志；
2. 不提交该错误走法；
3. 尝试下一个引擎；
4. 最后 fallback 到随机合法走法。

---

## 9. 合法走法校验规则

### 9.1 插件必须校验

无论引擎看起来多可靠，插件都必须校验：

```python
if best_move not in legal_moves:
    reject_and_fallback()
```

原因：

- FEN 坐标方向容易写反；
- 引擎可能用 ICCS / UCI / 自己的格式；
- 引擎可能搜索出违反将帅照面的走法；
- 网络服务可能返回旧请求结果；
- LLM 写的引擎尤其容易胡走。

### 9.2 引擎也建议校验

引擎自己也应该做一次：

```python
if legal_moves and best_move not in legal_moves:
    return {"error": {"code": "illegal_best_move", "message": "best_move not in legal_moves"}}
```

### 9.3 `legal_moves` 为空怎么办

如果请求里没有 `legal_moves` 或者为空：

- 引擎可以自己生成合法走法；
- 但不推荐依赖这个；
- 插件层最好永远提供 `legal_moves`。

---

## 10. 命令行引擎协议细节

### 10.1 stdin/stdout

命令行引擎必须：

1. 从 stdin 读取完整 JSON；
2. 向 stdout 输出一个 JSON；
3. 日志输出到 stderr，不要混到 stdout；
4. stdout 只能有 JSON，不要输出解释文字。

正确：

```text
stdout: {"best_move":"b2e2"}
stderr: thinking depth=3 nodes=12345
```

错误：

```text
stdout: 我觉得应该走 b2e2
stdout: {"best_move":"b2e2"}
```

这种会导致插件解析 JSON 失败。

### 10.2 退出码

推荐：

| 退出码 | 含义 |
|---:|---|
| `0` | 成功或可解析错误 JSON |
| `1` | 输入错误 |
| `2` | FEN 错误 |
| `3` | 超时 |
| `10` | 内部错误 |

即使退出码非 0，如果 stdout 有合法错误 JSON，插件也可以读取。

### 10.3 命令行配置示例

```json
{
  "engine_mode": "custom_command",
  "custom_engine_command": "python D:/engines/my_xiangqi_engine.py",
  "custom_engine_timeout_ms": 8000,
  "engine_depth": 3
}
```

Linux 示例：

```json
{
  "engine_mode": "custom_command",
  "custom_engine_command": "/home/zxx/engines/my_engine --json",
  "custom_engine_timeout_ms": 8000,
  "engine_depth": 3
}
```

### 10.4 Windows 路径注意

Windows JSON 里反斜杠要转义：

```json
{
  "custom_engine_command": "C:\\Users\\zxx\\engines\\my_engine.exe"
}
```

或者直接用 `/`：

```json
{
  "custom_engine_command": "C:/Users/zxx/engines/my_engine.exe"
}
```

---

## 11. HTTP 引擎协议细节

### 11.1 Endpoint

推荐：

```text
POST /analyze
```

完整地址示例：

```text
http://127.0.0.1:8799/analyze
```

### 11.2 请求头

```http
Content-Type: application/json
```

可选鉴权：

```http
Authorization: Bearer <token>
```

### 11.3 请求体

同第 4 节输入 JSON。

### 11.4 响应体

同第 7 节输出 JSON。

### 11.5 HTTP 状态码

| 状态码 | 含义 |
|---:|---|
| `200` | 成功，返回 `best_move` |
| `400` | 请求格式错误 |
| `408` | 引擎超时 |
| `422` | FEN 或字段无法处理 |
| `500` | 引擎内部错误 |
| `503` | 引擎暂不可用 |

插件处理原则：

- 只有 `200` 且有合法 `best_move` 才算成功；
- 其他情况全部 fallback；
- 不要因为 HTTP 引擎失败导致 Bot 停止走棋。

### 11.6 HTTP 配置示例

```json
{
  "engine_mode": "custom_http",
  "custom_engine_url": "http://127.0.0.1:8799/analyze",
  "custom_engine_token": "",
  "custom_engine_timeout_ms": 8000,
  "engine_depth": 3
}
```

---

## 12. 超时策略

推荐插件配置：

```json
{
  "engine_depth": 3,
  "custom_engine_timeout_ms": 8000
}
```

建议限制：

| 参数 | 推荐值 |
|---|---:|
| 最小 depth | 1 |
| 默认 depth | 3 |
| 普通上限 | 6 |
| 默认 timeout | 8000 ms |
| 最大 timeout | 30000 ms |

插件行为：

1. 调用引擎时设置进程/HTTP 超时；
2. 超时后杀掉命令行进程，或中断 HTTP 请求；
3. 记录日志；
4. fallback 到下一引擎或随机走法。

引擎行为：

1. 尊重 `timeout_ms`；
2. 尽量在超时前返回当前最好走法；
3. 不要卡死。

---

## 13. 推荐 fallback 链

`auto` 模式建议：

```text
custom_command
  ↓ 失败/未配置/非法走法
custom_http
  ↓ 失败/未配置/非法走法
local_xqwlight
  ↓ 失败/非法走法
server_xqwlight
  ↓ 失败/非法走法
random legal move
```

失败包括：

- 命令不存在；
- 进程超时；
- HTTP 连接失败；
- 返回不是 JSON；
- 返回没有 `best_move`；
- `best_move` 不在 `legal_moves`；
- 引擎主动返回 `error`。

---

## 14. 最小 Python 命令行引擎示例

文件：`my_engine.py`

```python
#!/usr/bin/env python3
import json
import random
import sys
import time


def main():
    started = time.time()

    try:
        req = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({
            "error": {"code": "invalid_request", "message": str(e)},
            "engine": "demo-python-engine"
        }, ensure_ascii=False))
        return 1

    legal_moves = req.get("legal_moves") or []
    if not legal_moves:
        print(json.dumps({
            "error": {"code": "no_legal_moves", "message": "legal_moves is empty"},
            "engine": "demo-python-engine"
        }, ensure_ascii=False))
        return 0

    # 示例：这里先随机。真正引擎可以替换为搜索逻辑。
    move = random.choice(legal_moves)

    elapsed_ms = int((time.time() - started) * 1000)
    print(json.dumps({
        "protocol": "xiangqi-engine-v1",
        "request_id": req.get("request_id"),
        "engine": "demo-python-engine",
        "engine_version": "0.1.0",
        "best_move": move,
        "score": 0,
        "score_type": "cp",
        "depth": req.get("depth", 1),
        "elapsed_ms": elapsed_ms,
        "reason": "random legal move demo"
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

测试：

```bash
printf '%s' '{"fen":"rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1","legal_moves":["b2e2","h2e2"],"side":"red","depth":1,"timeout_ms":1000}' | python3 my_engine.py
```

期望：

```json
{"best_move":"b2e2", ...}
```

或者：

```json
{"best_move":"h2e2", ...}
```

---

## 15. 最小 Node.js 命令行引擎示例

文件：`my_engine.js`

```js
#!/usr/bin/env node

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const started = Date.now();

  try {
    const req = JSON.parse(input || '{}');
    const legal = Array.isArray(req.legal_moves) ? req.legal_moves : [];

    if (!legal.length) {
      process.stdout.write(JSON.stringify({
        error: { code: 'no_legal_moves', message: 'legal_moves is empty' },
        engine: 'demo-node-engine'
      }));
      return;
    }

    const move = legal[Math.floor(Math.random() * legal.length)];

    process.stdout.write(JSON.stringify({
      protocol: 'xiangqi-engine-v1',
      request_id: req.request_id || null,
      engine: 'demo-node-engine',
      engine_version: '0.1.0',
      best_move: move,
      score: 0,
      score_type: 'cp',
      depth: req.depth || 1,
      elapsed_ms: Date.now() - started,
      reason: 'random legal move demo'
    }));
  } catch (e) {
    process.stdout.write(JSON.stringify({
      error: { code: 'invalid_request', message: String(e.message || e) },
      engine: 'demo-node-engine'
    }));
  }
});
```

测试：

```bash
printf '%s' '{"legal_moves":["b2e2","h2e2"],"side":"red"}' | node my_engine.js
```

---

## 16. 最小 HTTP 引擎示例：Python FastAPI

文件：`engine_server.py`

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Any
import random
import time

app = FastAPI(title="Xiangqi Custom Engine")


class AnalyzeRequest(BaseModel):
    protocol: str | None = None
    request_id: str | None = None
    match_id: str | None = None
    bot_id: str | None = None
    fen: str
    side: str
    legal_moves: list[str] = Field(default_factory=list)
    depth: int = 3
    timeout_ms: int = 8000
    engine_options: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health():
    return {"ok": True, "engine": "demo-http-engine"}


@app.post("/analyze")
def analyze(req: AnalyzeRequest):
    started = time.time()

    if req.side not in {"red", "black"}:
        raise HTTPException(status_code=400, detail="side must be red or black")

    if not req.legal_moves:
        return {
            "error": {"code": "no_legal_moves", "message": "legal_moves is empty"},
            "engine": "demo-http-engine"
        }

    move = random.choice(req.legal_moves)

    return {
        "protocol": "xiangqi-engine-v1",
        "request_id": req.request_id,
        "engine": "demo-http-engine",
        "engine_version": "0.1.0",
        "best_move": move,
        "score": 0,
        "score_type": "cp",
        "depth": req.depth,
        "elapsed_ms": int((time.time() - started) * 1000),
        "reason": "random legal move demo"
    }
```

启动：

```bash
pip install fastapi uvicorn
uvicorn engine_server:app --host 127.0.0.1 --port 8799
```

测试：

```bash
curl -s http://127.0.0.1:8799/analyze \
  -H 'Content-Type: application/json' \
  -d '{"fen":"rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1","side":"red","legal_moves":["b2e2","h2e2"],"depth":1,"timeout_ms":1000}'
```

---

## 17. 插件侧伪代码

### 17.1 选择引擎

```python
async def choose_move(position):
    legal_moves = position.legal_moves
    if not legal_moves:
        return None

    modes = resolve_engine_chain(config.engine_mode)

    for mode in modes:
        try:
            if mode == "custom_command":
                result = await call_custom_command(position)
            elif mode == "custom_http":
                result = await call_custom_http(position)
            elif mode == "local_xqwlight":
                result = await call_local_xqwlight(position)
            elif mode == "server_xqwlight":
                result = await call_server_xqwlight(position)
            elif mode == "random":
                return random.choice(legal_moves)
            else:
                continue

            move = extract_move(result)
            if move in legal_moves:
                return move

            logger.warning("engine returned illegal move: %s", move)
        except Exception:
            logger.exception("engine failed: %s", mode)

    return random.choice(legal_moves)
```

### 17.2 构造请求

```python
def build_engine_request(match, event, config):
    return {
        "protocol": "xiangqi-engine-v1",
        "request_id": f"{match.id}_ply_{match.ply}",
        "match_id": match.id,
        "bot_id": self.bot_id,
        "fen": event["fen"],
        "side": "red" if event["side"] == "red" else "black",
        "ply": event.get("ply"),
        "legal_moves": event.get("legal_moves") or [],
        "last_move": event.get("last_move"),
        "history": event.get("history") or [],
        "depth": clamp_int(config.engine_depth, 1, 6),
        "timeout_ms": clamp_int(config.custom_engine_timeout_ms, 1000, 30000),
        "red_time_left_ms": event.get("red_time_left_ms"),
        "black_time_left_ms": event.get("black_time_left_ms"),
        "total_time_ms": event.get("total_time_ms"),
        "engine_options": config.get("custom_engine_options", {})
    }
```

### 17.3 解析返回

```python
def extract_engine_move(data):
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return None
    move = data.get("best_move") or data.get("move")
    if not isinstance(move, str):
        return None
    move = move.strip().lower()
    if len(move) != 4:
        return None
    return move
```

---

## 18. `_conf_schema.json` 推荐配置项

```json
{
  "engine_mode": {
    "description": "走棋引擎模式",
    "type": "string",
    "default": "auto",
    "enum": ["auto", "server_xqwlight", "local_xqwlight", "custom_command", "custom_http", "random"],
    "hint": "auto 会依次尝试自定义命令、自定义HTTP、本地xqwlight、服务器xqwlight，最后随机兜底"
  },
  "engine_depth": {
    "description": "引擎搜索深度",
    "type": "int",
    "default": 3,
    "hint": "建议 1-6，越高越慢"
  },
  "custom_engine_command": {
    "description": "自定义命令行引擎命令",
    "type": "string",
    "default": "",
    "hint": "例如 python D:/engines/my_engine.py；引擎需从 stdin 读 JSON，stdout 输出 JSON"
  },
  "custom_engine_url": {
    "description": "自定义 HTTP 引擎地址",
    "type": "string",
    "default": "",
    "hint": "例如 http://127.0.0.1:8799/analyze"
  },
  "custom_engine_token": {
    "description": "自定义 HTTP 引擎 Token",
    "type": "string",
    "default": "",
    "hint": "可选；填写后插件会发送 Authorization: Bearer <token>"
  },
  "custom_engine_timeout_ms": {
    "description": "自定义引擎超时时间",
    "type": "int",
    "default": 8000,
    "hint": "单位毫秒，建议 3000-30000"
  },
  "custom_engine_options": {
    "description": "传给自定义引擎的额外参数",
    "type": "object",
    "default": {},
    "hint": "例如 {\"style\":\"aggressive\",\"skill_level\":5}"
  }
}
```

注意：实际 AstrBot schema 是否支持 `object` 要看版本。如果 WebUI 不支持，可以把 `custom_engine_options` 改成 JSON 字符串：

```json
{
  "custom_engine_options_json": {
    "description": "自定义引擎额外参数 JSON",
    "type": "string",
    "default": "{}"
  }
}
```

---

## 19. 安全要求

### 19.1 不要硬编码敏感信息

不要在插件代码或公开 README 里写死：

- Bot token；
- 管理员 token；
- NewAPI key；
- 服务器密码；
- GitHub token；
- 私有 IP fallback。

HTTP 引擎 token 应来自运行时配置，不要写死。

### 19.2 命令行引擎风险

`custom_engine_command` 本质上会执行本地命令，所以：

- 只允许管理员配置；
- 不要让普通群成员通过聊天命令改；
- 不要拼接用户输入到 shell 命令；
- 推荐用 `asyncio.create_subprocess_exec` + `shlex.split`，不要用 `shell=True`。

推荐：

```python
args = shlex.split(command)
proc = await asyncio.create_subprocess_exec(
    *args,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

不推荐：

```python
await asyncio.create_subprocess_shell(command)
```

### 19.3 HTTP 引擎风险

- 默认只连 `127.0.0.1` 更安全；
- 如果开放公网，必须加 token；
- 请求日志不要打印完整 token；
- 不要让引擎服务直接操作棋擂台账号。

---

## 20. 日志建议

插件调用引擎时建议记录：

```text
engine=custom_http match=match_xxx side=red legal=44 depth=3 timeout=8000
engine=custom_http result move=b2e2 score=35 elapsed=742 legal=true
```

失败时：

```text
engine=custom_command failed reason=timeout elapsed=8000 fallback=server_xqwlight
engine=custom_http illegal_move move=a0a9 legal=false fallback=random
```

不要记录：

- 完整 token；
- 密码；
- 过长 FEN 历史；
- NewAPI key。

---

## 21. 测试用例

### 21.1 命令行引擎 smoke test

```bash
printf '%s' '{"fen":"rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1","side":"red","legal_moves":["b2e2","h2e2"],"depth":1,"timeout_ms":1000}' | python3 my_engine.py
```

必须输出合法 JSON，且 `best_move` 在 `legal_moves` 中。

### 21.2 HTTP 引擎 smoke test

```bash
curl -fsS http://127.0.0.1:8799/health
curl -fsS http://127.0.0.1:8799/analyze \
  -H 'Content-Type: application/json' \
  -d '{"fen":"rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1","side":"red","legal_moves":["b2e2","h2e2"],"depth":1,"timeout_ms":1000}'
```

### 21.3 非法走法测试

请求：

```json
{
  "legal_moves": ["b2e2", "h2e2"]
}
```

如果引擎返回：

```json
{"best_move":"a0a9"}
```

插件必须拒绝，并 fallback。

### 21.4 超时测试

让引擎 sleep 超过 `timeout_ms`，插件应：

- 不崩溃；
- 记录超时；
- fallback；
- 最终仍然走出一步合法棋。

---

## 22. 常见坑

### 22.1 坐标方向写反

最常见错误：把 FEN 行号当成 UCCI rank。

记住：

- FEN 第 1 行是黑方底线；
- UCCI rank 0 是红方底线。

转换时要反过来。

### 22.2 返回中文棋谱而不是 UCCI

错误：

```json
{"best_move":"炮八平五"}
```

正确：

```json
{"best_move":"b2e2"}
```

中文棋谱可以放在 `reason` 或 `comment`，但 `best_move` 必须是 UCCI。

### 22.3 stdout 混入日志

命令行引擎 stdout 只能输出 JSON。日志要写 stderr。

### 22.4 忘记校验 `legal_moves`

插件必须校验，引擎也建议校验。

### 22.5 depth 太高导致卡死

象棋搜索分支很大，depth 建议默认 3，普通上限 6。

### 22.6 LLM 不能当主引擎

LLM 可以解释走法，但不适合直接决定走法。主路径应使用：

- 规则引擎；
- 搜索引擎；
- xqwlight；
- UCCI/象棋引擎；
- 或至少基于合法走法的启发式。

如果一定要让 LLM 参与，也必须让它只能从 `legal_moves` 里选，并且插件继续校验。

---

## 23. 推荐开发顺序

1. 先写一个随机合法走法 demo，引擎协议跑通；
2. 再加入 FEN 解析；
3. 再加入局面评估；
4. 再加入一层搜索；
5. 再加入 alpha-beta；
6. 再加入置换表/开局库；
7. 最后再优化速度和风格。

不要一上来就写复杂强引擎。先保证：

- JSON 能解析；
- 坐标正确；
- 返回走法合法；
- 超时能 fallback；
- 对局不会卡死。

---

## 24. 最小交付标准

一个可接入插件的自定义引擎，至少必须满足：

- [ ] 支持命令行 stdin/stdout JSON，或 HTTP POST JSON；
- [ ] 接收 `fen`；
- [ ] 接收 `legal_moves`；
- [ ] 返回 `best_move`；
- [ ] `best_move` 使用 UCCI 格式；
- [ ] `best_move` 必须在 `legal_moves` 中；
- [ ] stdout / HTTP body 是纯 JSON；
- [ ] 能在 `timeout_ms` 内返回；
- [ ] 出错时返回错误 JSON 或让插件能安全 fallback；
- [ ] 不硬编码任何 token / 密码。

---

## 25. 推荐给引擎作者的一句话说明

如果要给棋擂台插件写自定义象棋引擎，只要实现下面这个协议即可：

> 输入当前局面 `fen` 和合法走法数组 `legal_moves`，输出一个 JSON：`{"best_move":"b2e2"}`。走法必须是 UCCI 坐标，且必须在 `legal_moves` 里面。命令行版本从 stdin 读 JSON、stdout 输出 JSON；HTTP 版本接收 POST JSON、返回 JSON。

---

## 26. 示例请求 / 响应全集

### 请求

```json
{
  "protocol": "xiangqi-engine-v1",
  "request_id": "match_demo_ply_1",
  "match_id": "match_demo",
  "bot_id": "bot_demo",
  "fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR r - - 0 1",
  "side": "red",
  "ply": 1,
  "legal_moves": ["b2e2", "h2e2", "h0e2", "b0c2"],
  "last_move": null,
  "history": [],
  "depth": 3,
  "timeout_ms": 8000,
  "red_time_left_ms": 600000,
  "black_time_left_ms": 600000,
  "total_time_ms": 600000,
  "engine_options": {
    "style": "balanced"
  }
}
```

### 成功响应

```json
{
  "protocol": "xiangqi-engine-v1",
  "request_id": "match_demo_ply_1",
  "engine": "example-engine",
  "engine_version": "1.0.0",
  "best_move": "b2e2",
  "score": 18,
  "score_type": "cp",
  "depth": 3,
  "nodes": 48291,
  "elapsed_ms": 315,
  "pv": ["b2e2", "h7e7"],
  "reason": "central cannon opening"
}
```

### 错误响应

```json
{
  "protocol": "xiangqi-engine-v1",
  "request_id": "match_demo_ply_1",
  "engine": "example-engine",
  "error": {
    "code": "timeout",
    "message": "search exceeded timeout_ms"
  },
  "elapsed_ms": 8001
}
```
