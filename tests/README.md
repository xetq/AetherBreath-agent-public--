# tests —— AetherBreath 回归测试集

> 判据：**一条命令、无需 LLM、无需网关、不写真实文件**（全走临时目录）。
> 全量耗时约 3 秒（单测层）；纳入工作区探针后约 20 秒。

## 跑法（项目根）

```bash
venv/Scripts/python -m pytest tests -q                      # 全量
venv/Scripts/python -m pytest tests/test_approval_engine.py -q   # 单文件
AETHER_PROBE_LIVE=1 venv/Scripts/python -m pytest tests -q  # 连真机探针（需网关 8900）
```

## 覆盖地图（12 个文件）

| 文件 | 被测对象 | 断言 | 重点 |
|---|---|---|---|
| `test_approval_engine.py` | 审批引擎 agent/approval.py + approvals/ 规范 | 84 | 判定分层、批次原子、五结算、作用域签发/撤销、部分批准文案、heredoc与ctypes绕过、strict档、误报存档 |
| `test_approval_binding.py` | 审批接线与卡片装配（动词-目标绑定、后果分区） | 11 | 历史误报全部钉成用例：禁止「共现即判定」；卡片字段整包透传 |
| `test_security_hardening.py` | 安全加固（SSRF/URL泄密/外发/凭据/项目外） | 10 | 2026-09-11 审计四类缺口固化；只调判定函数，零命令执行 |
| `test_context_manager.py` | 上下文管理器 agent/context_manager.py | 45 | 切轮切组、保护段、清理矩阵、幂等、触发与冷却、指纹命中、原子写、失败不抛、回捞 |
| `test_restore_context.py` | restore_context 工具 | 9 | 选择器校验、无数据源、未命中、异常兜底、schema 合法 |
| `test_orchestrator_pipeline.py` | 任务编排器 task_orchestrator.py | 19 | 模板/管道独立性、真并行、结果按 id 归位、never_parallel 串行层、超时、shutdown |
| `test_skill_system.py` | 技能系统 agent/skill_system.py | 13 | 扫描发现、frontmatter 容错、注册表渲染、幂等、增删改 diff、注入块 |
| `test_logger_drain.py` | AsyncLogWriter 关闭排空 | 3 | 2026-09-03 修复固化：哨兵排队尾，快批后关闭不丢日志 |
| `test_webui_session_delete.py` | WebUI 会话删除血缘 | 6 | 原文+视图+事件流三件套同移 .trash；移动失败如实报 warning |
| `test_workspace_probes.py` | 工作区探针纳管（子进程） | 7 | 4 真跑 + 2 登记(需网关,缺则 skip) + 1 防误纳管断言 |
| `test_mid_turn.py` | 中期交互 `agent_webui/backend/mid_turn.py` + 两侧接线 | 25 | 信箱读写/并发不丢件、上限拒绝不腾位、渲染格式、注入形态（独立 user 消息，绝不污染 tool 输出）、过期作废、观察者通道（异常不拖垮投递）、`run_id` 一路带着；契约锁：前端 `lib/midTurn.ts` 的前缀必须与后端逐字一致、`agent.py` **不含** mid_turn（只提供通用钩子）、`bridge` 必须注册钩子、`api.ts` EVENTS 白名单不能漏 `mid_turn` |
| `test_ask_multi.py` | 提问多卡：`backend/ask_batch.py` + 两侧接线 | 16 | 共享窗口语义（并行共 deadline、后来者不缩短、作答续窗、硬上限封顶、最后一张出局即关窗、下一批重开、`now` 时间旅行不真等）、批次上限按卡数放宽且 N=1 与历史一致；契约锁：前端 store 必须是 `clarifies` 数组（不许退回单槽）、编排器串行名单必须写工具真名 `ask_user`、bridge 必须接共享窗口 |
| `test_mcp_client.py` | MCP 客户端（`agent/mcp_client.py` + `agent/approvals/mcp_spawn.py`） | 30 | 手写 stdio JSON-RPC：换行分帧、**对号入座**（乱序/别人的 id/畸形行）、超时**真 kill**、EOF 判死、stderr 背压、翻页、`isError → ❌`、内容降维（text/image 落盘/资源）；`logger` 形参契约、`never_parallel` 模板；审批三态（首次问 / 已批准静默且**不遮蔽**其它规范 / 拿不到状态 fail-closed）；v2：`mcp.spawn` 认 `mcp_call` 且卡上写真实命令行与内部工具、`arguments` 里的路径对路径类规范可见；端到端：真 `TaskOrchestrator` + 真 `AVAILABLE_TOOLS` + 假 server（子进程跑 `tests/mcp_e2e_probe.py`，v2 走 mcp_search/mcp_call） |

| `test_mcp_manage.py` | 自维护（`agent_tools/mcp_manage.py` + `agent/mcp_station.py`） | 26 | 版本固定（拒绝 latest、`@scope/name` 不误判）；add/list/test/set_enabled/remove 全流程与**真回滚**（失败不留半成品、注册表不留行）+ 有界自愈（连续失败到上限就停手，必须 force）。**station 段**：集成 = 建 `<station>/`（STATION.md + tools.yaml）；撞到手写 station 一律不动（原文件**字节不变**）；开关**只改 `enabled:` 那一行**（正文保留）且注册表同步；移除挪 `.trash/`（可恢复）；`test` 真起进程刷新 tools.yaml 并能真调；未知 station 列出有哪些；`list` 报 station 目录/注册表/来源/开关/工具数/进程状态。另含 1 条跑 `mcp_self_integration_probe.py` 的 v2 端到端（12 步）。契约锁：`mcp_manage` 必须在编排器串行名单里（读-改-写序列并发会丢更新）、**集成不设专门闸门**（决策锁 + 退役原文必须留着）。路径隔离：环境变量指哪，写入就跟到哪 |
| `test_mcp_station.py` | station 发现层（`agent/mcp_station.py`） | 24 | **文件夹存在即注册**（`agent_MCP/<station>/STATION.md`）；坏文件夹**报出来不静默**（缺 frontmatter / 缺 name / 重名 / 有 tools.yaml 没 STATION.md / 文件夹名与 frontmatter 名不一致）；缺 command 或缺工具清单 = **不可用**（fail-closed，不进注册表）；注册表**每 station 恰好一行**；**KPI：注入体积只与 station 数线性、与工具数无关**（112 个工具的 station 与 2 个的一样大，且 schema 一个字都不许进来）；同步幂等（内容不变不动文件）；station → `ServerSpec`（origin/timeout/env/tools_path 透传）；`load_station` 每次读盘（同会话新建立刻可见 = 热的底层保证）；总闸读 config；写 `tools.yaml`；CLI `--check` 退出码；路径优先级（参数 > 环境变量 > config `paths.mcp_stations` > 项目内默认） |

| `test_mcp_stations_api.py` | WebUI 面板数据源（`agent_webui/backend/mcp_view.py` + `mcp_client` 的运行快照） | 9 | **常驻**：没起过进程的站也在快照里（`alive=false`/`calls=0`），且扫描**不起进程**；快照**不含任何工具 schema**（只给数量）；**运行态跨进程**：面板在**网关进程**里，看不到 agent 的连接表，所以运行态由 agent 侧写 `<stations>/.state/runtime.json`、网关侧读（`snapshot_at`/`runner_pid` 标明是谁什么时候写的；没快照就如实说"未知"，**不许**把 0 当事实，更**不许**拿网关的 env 去猜 agent 缺不缺 token —— 实测误报过）；**Q26 数据路径**：真调一次后能说出"调了哪个 station 的哪个工具"（成功 `last_call_ok=true`，失败/抛异常也如实记 false）；**没碰过 MCP 的进程不许写快照**（空快照会盖掉真数据；且快照目录按"调用发生那一刻"记，避免 `atexit` 收尾时 env 已被还原而写进真实仓库 —— 实测泄漏过）；总闸 `mcp.enabled=false` 如实上报；目录不存在 = 空态不抛（面板不装死） |
| `test_mcp_gateway.py` | MCP 两个元工具（`agent_tools/mcp_gateway.py`） | 20 | `mcp_search`：列 station 时**不带** schema、点名时给**完整 inputSchema**（含必填/缺省）、超大 station **分页**（`MAX_TOOLS_PER_PAGE`，且告诉模型怎么翻页）、站名写错列现有的+近似提示、关着的 station 给开它的动作、坏 station 如实说、空目录教下一步；`mcp_call`：缺必填**本地就拒**（断言进程**没被拉起**）、工具名近似提示、参数吃对象/JSON 字符串/其他形状一律 ❌、客户端抛异常也不外抛（`内部异常`）、真起一次 `mcp_stub_server` 跑通对象与 JSON 两种参数并收干净；契约：两个 schema 的必填项与"第一次可能弹批准卡"的提醒 |

合计可见断言 **247** 条（pytest 收集为单元级，实际数条合一）；
2026-09-14 起另加 MCP 四组：`test_mcp_client.py`（**30 个用例**，含 1 个跑 `mcp_e2e_probe.py` 的端到端用例）、
`test_mcp_manage.py`（**26 个用例**，含 1 个跑 `mcp_self_integration_probe.py` 的端到端用例）、
`test_mcp_station.py`（**24 个用例**，v2 发现层）与 `test_mcp_gateway.py`（**20 个用例**，v2 两个元工具）。
2026-09-15 v2 切换后：`test_mcp_client.py` 的注册契约改为"**没有** MCP 工具进常驻工具表"，新增
v2 审批三例（`mcp_call` 弹卡 / 批准后静默 / **`arguments` 里的路径对路径类规范可见**）；
`test_mcp_manage.py` 里的自集成探针已重写为 v2（集成 → 不重启就能用 → 关掉即拒 → 移除进回收站，12 步全过）。
2026-09-15 v1 抹除收尾（测试侧）：清掉测试与探针里全部 v1 引用（旧注册表环境变量、旧 auto 表/
手写表合并、旧 auto 表文件名、"每个 MCP 工具都是常驻一等工具"的旧注册形态）；删掉 **13 条**只测已删机制的用例
（`test_mcp_manage.py` 12 条 + `test_mcp_station.py` 1 条）；`list` / `test` / add 失败回滚三条
**改写为 station 语义**（不删，因为这三个动作在 v2 仍然存在）；`test_mcp_manage.py` 的共享夹具
由"手写表 + auto 表目录"改为 **station 目录**（路径隔离纪律不变：先把 `AETHER_MCP_STATIONS`
指向临时目录再动文件）。
全量：`venv/Scripts/python -m pytest tests -q` → **252 passed / 2 skipped**（2026-09-16，注册表「三档状态」改造后：关着的 station 留行标 `off`、开着但没配好标 `⚠ 不可用`）。
> 注意跑法要带 `tests` 范围：仓库根直接 `pytest` 会去收集 `agent_workspace/**` 下的独立探针
> （它们模块级 `sys.exit()`，是**刻意**不纳入 pytest 的，见下文"刻意不纳入"）。

## 四层结构（为什么要分层）

| 层 | 位置 | 条件 | 何时跑 |
|---|---|---|---|
| L1 单测 | `tests/test_*.py`（纯函数/临时目录） | 无 | 每次改 `agent/`、`agent_tools/`、审批规范后必跑 |
| L2 纳管探针 | `test_workspace_probes.py` 调起的子进程脚本 | 无（脚本自带桩） | 同上，一条命令已包含 |
| L3 真机探针 | `agent_webui/scripts/regression_p7.py`、`verify_gate_restore.py` | 需网关 8900 | 前端/网关/bridge 链路改动后手动跑 |
| L4 行为回归 | `agent_workspace/回归测试集/回归测试集.md`（46 条真实用户消息，L1核心/L2行为/L3弱） | 需真 LLM | 改 prompt、模型、工具集后**人工**过一遍 |
| L5 上下文专项 | `agent_workspace/上下文管理器/{e2e_probe,replay_probe,bench_view}.py` | 需真实会话存档 | 改压缩规则后跑；**不得纳入 pytest**（理由见下） |

## 刻意不纳入 pytest 的东西（有断言守护这个决定）

`test_documented_omissions_are_justified` 会检查这些文件仍在原位，且断言它们没被塞进纳管清单：

1. **上下文管理器的三个探针** —— 判据要**真实会话存档**（不是夹具），没法无人值守地跑；见该目录 `DESIGN.md §17-6`。
   （历史上还叠着一条硬伤：`agent_tools/multi_search.py` 在 win32 import 期**无条件** `sys.stdout.detach()`，会摧毁 pytest 的 capture，于是"任何 import agent_tools 的用例"都得开子进程绕。**2026-09-14 已修**：现在只对**真控制台**重包编码，宿主替换过 stdout（pytest 捕获、桥接捕获）就一个字都不动 —— 进程内 `import agent_tools` 已经安全。这三个探针仍不纳入 pytest，理由就是上面那条"需要真实存档"。）
2. **security-audit-aetherbreath/probe1~8、verify_t***  —— 结论已固化进 `test_security_hardening.py` 与 `test_approval_engine.py`（例如 `bash -c`、`cmd /c`、heredoc、`ctypes DeleteFileW` 那些条目就来自它们）。原件是取证脚本：dump 结构、无断言，留着作历史证据。
3. **webui延迟检测/probe*.py** —— 性能采样，没有恒定的通过判据，混进回归会让"红"失去意义。
4. **系统盘审计功能规划/test_approval.py** —— 已演化为 `tests/test_approval_engine.py`（工作区那份是历史版本，功能被完整超越）。

## 维护约定

- 新增判定规则时，**先把踩过的坑写成断言**再改代码；只改代码不补断言 = 该缺陷留着。
- 断言文本用中文写清"为什么"，不要只写 `assert x`：将来红了要能一眼看出是哪条契约破了。
- 禁止绝对路径与盘符字面量：盘符/根目录一律运行期取（GitHub 友好，也是审批系统不吃误报的前提）。
- 禁止在用例里执行真命令：审批类测试只调 `inspect_one()` 等纯判定函数（执行会真改盘）。
- 每个测试文件都可**独立直跑**（`python tests/xxx.py` 自带汇总），不依赖 pytest 也能看结果。
- `tests/` 只放"能判定通过/失败"的东西；观察性脚本留在工作区，靠 L2 的存在性断言管住。

## 已知空白（下次补）

- 审批**通道层**（WebUI adapter 的 request_many、多卡并发、恢复）目前只有 L2/L3 覆盖，没有 L1 单测 —— 一旦网关没起就无人值守不到。
- `execute_python` / `execute_shell` 的**行为层**（Grant 消费 / audit hook）尚未实现，因此无测试。
- L4 行为回归依赖人工，无自动化；`回归测试集.md` 的 46 条尚未脚本化对拍。
