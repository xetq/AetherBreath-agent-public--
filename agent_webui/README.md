# AetherBreath WebUI

给终端里的 AetherBreath 一个浏览器面孔：真实回合、真实工具、真实 SSE 事件流，
并且能随时 **优雅停止 / 强制关闭**，不改动 `agent/` 一行源码。

三进程拓扑：**浏览器 ⇄ 网关(FastAPI) ⇄ AB 子进程(bridge)**。
网关只管电源与会话转发，AB 本体跑在自己的 venv 里，两者通过本机 HTTP + 一次性 token 通信。

```
┌──────────┐  SSE /api/events   ┌─────────────────┐  HTTP+token  ┌────────────────────┐
│ 浏览器    │ ←────────────────→ │  网关 gateway    │ ←──────────→ │ AB 子进程 bridge    │
│ React SPA│  POST /api/chat    │ FastAPI :8900    │  /run /stop  │ 项目根 venv         │
└──────────┘                    │ 托管 dist/ 静态   │              │ agent.py 的调用者   │
      ▲                         │ 读写会话 JSON     │              │ 13 工具 + ask_user │
      │ 同一份文件               └─────────────────┘              └────────────────────┘
      └────────── agent_memory/working_memory/*.json （CLI 与 WebUI 共享磁盘）
```

- **网关**（`backend/main.py` + `api.py`）：只依赖 `fastapi/uvicorn`（已包含在项目根 `requirements.txt` 里），日常模式顺带托管前端产物。
- **bridge**（`backend/bridge.py`）：由网关按需拉起的 AB 子进程，跑项目根 `venv/`，把 `agent.call_agent_with_tools` 的每一步（思考/工具/文本）转成 SSE 事件。
- **前端**（`frontend/`）：Vite + React + TS，纯事件驱动 reducer，不轮询。

---

## 0. 30 秒上手

```bash
# 1) 后端依赖（装在项目根 venv；WebUI 网关的 fastapi/uvicorn 已包含在内）
pip install -r requirements.txt

# 2) 前端依赖 + 构建
cd agent_webui/frontend && npm install && npm run build && cd ../..

# 3) 起网关（单命令， anywhere 皆可，路径自推导）
python agent_webui/backend/main.py
#   Windows 也可直接跑：agent_webui\webui.bat

# 4) 浏览器打开 http://127.0.0.1:8900 → 点「🟢 开机」→ 聊天
```

> Windows 上若 `npm` 被 WSL 转发劫持（报 `wsl: <3>InternalError` 之类），改用 `npm.cmd install` / `npm.cmd run build`。
>
> 想让网关用**独立 venv**（不共用项目根环境）？先跑一次 `webui.bat --setup`；
> 本文件下文出现的 `venv-gateway/…` 路径均指这种独立模式。

---

## 1. 启动与停止

### 日常模式（单命令）
1. `npm run build`（改过前端才需要）
2. `python backend/main.py`（项目根 venv；或 `webui.bat`）
3. 浏览器 `http://127.0.0.1:8900`

网关自动检测 `frontend/dist/`：存在即托管（`dist_ready: true`），SPA 未知路径回落 `index.html`，刷新不 404。

### 开发模式（双命令，前端热更新）
```bash
# 终端 A：网关（只当 API 后端用）
python backend/main.py            # 项目根 venv
# 终端 B：Vite dev server，/api 全量代理到 8900（SSE 已禁用缓冲）
cd frontend && npm run dev        # → http://127.0.0.1:5173
```
同源代理，因此默认无需 CORS；跨源直连时用 `AETHER_CORS_ORIGINS` 开白名单。

### 停止
| 动作 | 怎么做 | 语义 |
|---|---|---|
| 优雅关机 | UI「⏻ 优雅关机」或 `POST /api/agent/stop` | 请求中断 → 等回合在**工具/请求边界**落盘（默认 15s）→ 退出进程。会话不丢。UI 点击前二次确认，提示语按当前回合阶段动态说明后果。 |
| 强制关闭 | UI「🛑 强制关闭」或 `POST /api/agent/kill` | `taskkill /T /F` 杀整棵进程树。仅在卡死时用。UI 点击前二次确认。 |
| 停网关 | 终端 `Ctrl+C` | 退出前回收自己拉起的 bridge，不留孤儿。 |

---

## 2. 界面能做什么

- **电源**：开机（可带 `mode`）/ 优雅关机 / 强杀，状态机可见（`OFF → STARTING → ON`）。
- **聊天**：多会话侧栏、真实历史（含工具调用配对）、Markdown 渲染、流式文本块。
- **工具时间线**：每次工具调用的 `tool / args / ok / elapsed / result` 摘要，按编排器线程分组，并行调用看得清清楚楚。
- **ask（agent 反问）**：AB 调 `ask_user` 时暂停回合，UI 弹卡片等你选/答，答复后回合原地续跑。
  **支持同批并行提问**：一条消息里问多个独立问题时，几张卡**同时挂出**、共享一条等待窗口
  （你答一个，其余的等待时间自动续上），逐个答完后再一起回给模型。详见 §11。
- **工作区 / 语境面板**：只读快照（会话文件、工作区目录、SOUL/AGENTS/MEMORY 加载情况）。
- **中断**：回合中随时「停」，在边界保存，历史可续聊。
- **中期交互（「用户交代」）**：回合跑动时你盯着工具时间线发现它跑偏了，可以直接在输入框打字 ——
  右侧按钮会从红色「⏹ 停止本回合」变成紫色「⤴ 发送（用户交代）」。发送**不打断**当前回合：
  这句话随 AB 的**下一批工具返回**一起进模型（独立 user 消息、带「用户交代」标识，绝不混进
  工具输出里）。**输入框为空时回车什么也不做** —— 防止误触停止；**AB 在跑别的会话时**同理，
  本会话没有可投递的目标。回合结束时仍未送达的会在界面上标「⚠️ 未送达」并弹提示（不留下诈尸的旧指令）。
  详见 §10。

---

## 3. API 一览

交互式文档（可直接试）：**http://127.0.0.1:8900/docs**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 网关健康 + 解释器/路径/超时/事件订阅数 |
| POST | `/api/agent/start` | 开机 `{mode?}`，异步返回，进度看 SSE |
| POST | `/api/agent/stop` | 优雅关机（中断→等保存→退出） |
| POST | `/api/agent/kill` | 强杀进程树 |
| GET | `/api/agent/status` | 进程级 + 回合级状态（`can_send`/`busy`） |
| GET | `/api/agent/tools` | 当前 AB 进程可见工具集 |
| GET | `/api/agent/log?lines=` | bridge stderr 尾部（诊断） |
| GET | `/api/sessions` | 会话列表（含消息数/工具数/状态） |
| POST | `/api/sessions` | 新建/续聊 `{session_id?, prefix?}` |
| GET | `/api/sessions/{sid}/history?limit=` | 历史（补齐 tool_calls 配对） |
| DELETE | `/api/sessions/{sid}` | 删除（移入 `working_memory/.trash/`，可恢复） |
| POST | `/api/chat` | 发起回合 `{session_id, message}` → 立即返回 `run_id` |
| POST | `/api/chat/stop` | 中断当前回合 `{run_id?}` |
| POST | `/api/chat/mid_turn` | 回合运行中追加「用户交代」`{session_id, text, run_id?}`（详见 §10） |
| POST | `/api/ask/answer` | 回答提问 `{ask_id, answer}` |
| GET | `/api/events?since=&session_id=` | **SSE** 事件流（网关 + bridge 同源） |
| GET | `/api/workspace/status` `· /api/workspace/dir?path=` | 工作区只读快照 / 展开子目录 |

## 4. 事件协议（SSE）

`data:{...}` 一行一事件，字段平铺。类型：

| type | 何时 | 关键字段 |
|---|---|---|
| `agent_phase` | 进程级状态变化 | `phase, reason, pid` |
| `turn_phase` | 回合级状态变化 | `phase`(THINKING/TOOL_RUNNING/RESPONDING/ASK_WAIT/IDLE/…), `reason` |
| `stage` | 回合里程碑 | `stage`(context_ready/…), `history_count, snapshot, model` |
| `progress` | 中期进度文字 | `text` |
| `tool_begin` / `tool_end` | 每次工具调用 | `tool, index, call_id, thread, args` / `ok, elapsed, result` |
| `text` | 助手文本块 | `text` |
| `ask_request` | AB 反问 | `ask_id, question, options, mode, timeout, batch_size, batch_live, remaining` |
| `ask_resolved` | 挂着的提问整批作废（回合被终止） | `reason, count` |
| `mid_turn` | 用户交代的三阶段 | `mid`(accepted/injected/dropped), `item_id`/`ids`, `count`, `texts` |
| `done` | 回合结束 | `content, iterations, tool_calls, elapsed, interrupted` |
| `error` | 回合异常 | `message, iterations` |
| `heartbeat` | 15s 保活 | — |

**断线不丢状态**：事件带单调 `seq`，重连传 `?since=<最后 seq>` 即从环形缓冲回放；
缓冲过期则前端改用 `/api/sessions/{sid}/history` 重建。

## 5. 环境变量（全部 `AETHER_*`）

| 变量 | 默认 | 作用 |
|---|---|---|
| `AETHER_WEBUI_HOST` / `AETHER_WEBUI_PORT` | `127.0.0.1` / `8900` | 网关监听 |
| `AETHER_GATEWAY_PYTHON` / `AETHER_AGENT_PYTHON` | `agent_webui/venv-gateway/…` / 项目根 `venv/…` | 两个解释器 |
| `AETHER_START_TIMEOUT` | 60 | 开机等待首行协议 + health 总超时 |
| `AETHER_GRACEFUL_TIMEOUT` | 15 | 优雅关机等待边界保存的超时，超时转强杀 |
| `AETHER_ASK_TIMEOUT` | **120** | ask 等你答复的窗口；含 `ask_user` 的那批工具超时被抬到 120+10s（余量确保是 waiter 先超时，不吞答复） |
| `AETHER_CORS_ORIGINS` | 空 | 开发模式跨源白名单（逗号分隔） |
| `AETHER_AB_MODE` | `default` | 传给 bridge 的模式标记（为「模式切换」预留） |
| `AETHER_BRIDGE_TOKEN` | 随机生成 | 网关→bridge 鉴权，仅存内存与环境，**不落盘不落日志** |
| `AETHER_KILL_ORPHANS` | 1（开启） | 启动时是否回收遗留 bridge；**设 0 关闭**（例如同时跑两个网关做对比测试，此时它绝不碰别的网关的活动 bridge） |

## 6. 与 CLI 共存（重要）

WebUI 与命令行版（`python agent/agent.py`）是**两个独立的 AB 进程**，共享同一批会话文件。
- ✅ 可以同时开着，各用**不同**会话。
- ⚠️ 别让两端**同时操作同一个 session_id**：两边都会 `save_session`，后写的覆盖先写的。
- 语境快照（SOUL / AGENTS / MEMORY / 技能注册表）在**会话创建时冻结**：改了这些文件要**新建会话**才生效；改了工具/`agent_tools/` 要**重新开机**（重启 AB 子进程）才生效——UI 上的关机→开机即可。

## 7. 卡死与恢复

- 回合真卡住（工具死循环、LLM 长挂）：点「🛑 强杀」→ 等 `phase=OFF` → 「🟢 开机」→ 打开原会话续聊。
- 优雅停止的落盘点是**工具/请求边界**，因此正在跑的工具会等它结束；超过 15s 自动升级强杀。
- 网关被 `kill` 掉来不及跑 `atexit`？下次启动会自动回收上一代 bridge（按命令行特征识别，**不碰**你自己的 python/浏览器进程）。
- Windows 专属坑：`venv/Scripts/python.exe` 是 uv 的 **trampoline**，`Popen.pid` 只是壳。因此停止/强杀一律走 `taskkill /T /F` 杀进程树，否则壳死了、真身变孤儿。

## 8. 扩展指南

**加一个右栏面板**：`frontend/src/panels/registry.ts` 注册一条（id/标题/组件），组件放 `components/`，数据走 `api.ts` 里已定义的方法或新增一个只读 GET。
**加一种事件**：bridge 里 `BUS.emit("your_type", {...})`（保留键 `type/seq/ts` 不可被 payload 覆盖），然后 `frontend/src/types.ts` 的 `WsEventType` + `appStore.tsx` 的 reducer 各加一个 case。
**加一个后端端点**：`backend/api.py` 里 `@router.get/post`，会话/进程相关能力优先复用 `AgentManager`（`agent_proc.py`）与 `sessions.py`。
**加 mode（模式切换）**：bridge 已接受 `mode` 参数并回显在 `hello`/`status`；真正的模式差异应落在 `run_turn` 的工具集/系统提示组装处，前端「开机」按钮传 `{mode}` 即可，无需改协议。

## 9. 自检与回归

```bash
python scripts/regression_p7.py     # 需网关已运行
```
真实跑完：多工具时间线 → ask 提问/答复续跑 → 优雅中断 → 强杀重开续聊（历史恢复），
结果落 `logs/regression_p7.json`，全 PASS 退出码 0。前端质量门：`npm run typecheck`（strict + noUnusedLocals，当前零错误）。

## 10. 中期交互（「用户交代」）—— 回合跑动中改方向

**要解决的问题**：AB 干长活时会持续产生中期输出（工具时间线、中期进度）。你看着它跑偏了，
但不想先停掉整个回合再重开 —— 那会把已跑完的工具结果和当前语境一起丢掉。

**机制**（仅 WebUI 有投递入口；CLI 端不接这个入口 → 信箱恒空，行为零差异）：

```
输入框打字 → 紫色「⤴ 发送（用户交代）」
        → POST /api/chat/mid_turn
        → bridge /mid_turn（三道校验）→ mid_turn 信箱（与 AB 同进程的模块级单例）
        → AB 在「下一批工具返回」那一刻取件，作为独立 user 消息追加进 conversation
```

| 要点 | 说明 |
|---|---|
| 注入形态 | **独立 user 消息**，正文带 `【用户交代 · 回合进行中追加】` 标识；**绝不拼进工具输出**（拼进去模型会把你的话当成工具的真实内容，读文件时尤其危险） |
| 注入时机 | 只在本批工具结果写进 conversation **之后**、下一次 LLM 请求之前（= "跟随下一个工具返回"） |
| 不打断 | 它不起新回合、不打断工具执行 —— 只是把话塞进模型的下一步动作里 |
| 三道校验 | ①必须有活动回合 ②给了 `run_id` 就必须命中该回合 ③会话必须与活动回合一致。任一不成立 → 409/404 明确拒绝，绝不"猜你想投给谁" |
| 多条 | 同一批工具返回前连发多条 → 按到达顺序合并成一条（`1.` `2.` …）注入 |
| 未送达 | 回合结束仍未送达的一律**作废**（`mid_turn:dropped`）：界面标「⚠️ 未送达」并弹提示。刻意不留到下一个回合 —— 旧指令诈尸会让模型执行一个早已不成立的要求 |
| 落盘 | 注入后随会话落盘：续聊、CLI 打开同一会话、事后翻历史都能看出它是「用户交代」 |
| 事件 | `mid_turn` 三阶段：`accepted`（已投递，前端据此落座消息）→ `injected`（已注入）/ `dropped`（作废） |

**界面语义（颜色即防误触）**：绿 = 正常发送（新回合）、红 = 停止本回合、紫 = 把这句话塞进
**正在跑的那个回合**。只有「输入框有内容」且「AB 正在跑本会话」时才是紫色；空回车什么也不做 ——
回车绝不会穿透到下面的「停止本回合」上。

**实现落点**：`backend/mid_turn.py`（信箱 + 渲染，**WebUI 侧**独立模块，2026-09-22 从 agent/ 迁来；
注入点走 agent 的通用钩子 `register_after_tools_hook` —— agent 不认识中期交互，CLI 下无注册者、零开销）
+ `backend/bridge.py`（`/mid_turn` 端点 + 事件出口）+ `backend/api.py`、`agent_proc.py`、`agent_client.py`（通道）
+ 前端 `lib/midTurn.ts` / `ChatView` / `appStore` / `MessageList` / `styles.css`。

**回归**：`tests/test_mid_turn.py`（25 条单测，含"前后端标识前缀必须逐字一致"的契约锁）；
真机端到端 `scripts/mid_turn_e2e.py`（真模型 + 真工具，覆盖"送达"与"作废"两个场景）。

---

## 11. 提问多卡（`ask_user` 并行提问）

**过去的样子**：不管 AB 在一个批次里问几个问题，屏幕上**只有一张卡弹得出来** —— 其余几张互相
覆盖，主人答完第一张就再也看不见它们，只能等它们各自超时；AB 那边则要等到下个回合才重新问，
效率全耗在来回上。

**根因（先取证再动手）**：后端本来就是好的 —— 三个 `ask_user` 并行进入 ask 通道、服务端台帐里
三张卡同时挂着、逐个答复也都能收（`scripts/repro_multi_ask.py` 的取证输出即证据）。问题全在前端：
`state.ask` 是**单槽**，三个 `ask_request` 事件互相覆盖。所以修的是「前端多卡 + 后端窗口
呼吸空间」，而不是"让后端支持并行"。

**现在的样子**：

| 行为 | 说明 |
|---|---|
| **逐张展示** | 一条消息里问 N 个独立问题，屏幕上**永远只有一张卡**；答完这张，下一张自动顶上。卡片头部只在还有排队时提示「还有 N 个问题」，没有多余说明 |
| 共享窗口 | 一批卡共用一条 deadline：**有人在答就往后续窗**（连续 120s 没人答才算没人应答），硬上限 `120×N + 60s` 防挂死。于是答下一张时窗口是**满的**，不是接着上一张的余额往下掉 |
| 整批回灌 | 逐个答完后，这一批的答案**一起**回到模型（编排器等这一层任务全部完成才返回）—— 逐张只是展示方式，不牺牲并行收益 |
| 终止即摘卡 | 点「⏹ 停止本回合」→ 挂着的（含还没轮到的）卡立即出局（`ask_resolved` 事件），不留僵尸 |
| 批次上限 | 编排器一批的等待上限按卡数放宽为 `120×N + 10s`（N=1 时与历史行为**完全一致**，不多等一秒） |

**顺带修掉的一个死条目**：编排器的强制串行名单 `_NEVER_PARALLEL_TOOLS` 里写的是 `"ask"`，
而工具真名叫 `"ask_user"` —— 这条从未匹配上，"交互工具要串行"的设计意图一直没生效。现已写回真名。
（N 个 `ask_user` 之间**仍然并行**：它们是同类屏障，彼此不依赖 —— 这正是本功能要的效果。）

**实现落点**：`backend/ask_batch.py`（新，共享窗口，纯逻辑可单测）
+ `bridge.py`（join/touch/leave、事件带批次信息、终止时撤回挂起卡）
+ 前端 `store/appStore.tsx`（`clarifies: AskRequest[]` + `askWindow` 共享倒计时）与
`components/AskCard.tsx`（多卡容器）
+ `agent/task_orchestrator.py`（串行名单真名）+ `backend/ask_user_tool.py`（明确告诉模型可以并行问）。

**回归**：`tests/test_ask_multi.py`（16 条：窗口语义 + 契约锁）；
`scripts/verify_gate_store.mjs` 新增多卡 reducer 断言；
真机 `scripts/verify_multi_ask.py` + 界面层 `scripts/verify_multi_ask_ui.mjs`。

---

## 12. 已知边界（v1 有意为之）

- `stream=False`：文本按块出现，不做逐 token 打字机效果。
- 事件缓冲是内存环形队列：网关重启后旧事件不可回放，靠会话历史重建界面。
- 慢连接的 SSE 订阅者会被丢事件（宁丢不阻塞 AB 回合）。
- 工具执行可能数分钟：界面显示"等待中"+心跳，不做假进度。
- **ask 窗口 120 秒**（原先到 30 秒就被掐）：编排器对「一批工具」共用一个 `timeout`
  （`agent.py` 建的是 `default_timeout=30`），而 `ask_user` 是在 worker 线程里等人回答。
  bridge 现在按批判定：本批含 `ask_user` 就把该批窗口抬到 `ASK_TIMEOUT + 10`（运行期包一层
  `_run_on_pool`，不碰 `agent/` 源码）。+10 是刻意余量 —— 让 waiter 自己先超时并返回
  「主人没答」这条**正常结果**，避免编排器先结算导致晚到的答复被静默吞掉。
  实测：故意不答，ask 后 **126 秒**正常收尾（见 ACCEPTANCE §10）。副作用：同批其它工具的
  超时被一起抬高（仅该批）。
- 工作区面板只读，不提供写操作。

> ℹ️ 更新（09-08 16:44）：ask 的两个问题都已修 —— ① `choice/multi` 缺自由文本输入框（ACCEPTANCE §8）；
> ② 30 秒编排器批次上限按批放宽到 120+10 秒（ACCEPTANCE §10）。前端倒计时改读后端上报的真实窗口。
