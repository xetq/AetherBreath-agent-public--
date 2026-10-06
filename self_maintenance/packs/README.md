# packs/ —— 自维护包

**是什么**：辅助自维护的**可复用**资料 —— 检查清单、模板、一次性脚本、踩坑案例。
**独立于技能系统**（不注册进 `agent_skills/`，不进 SKILL_REGISTRY，不占 AB 每轮上下文）。

**什么时候看**：维护前**先翻一遍** —— 有现成的就别重写（MANUAL.md §七）。
**什么时候往里放**：维护收尾时，把这次产出的"以后还用得上"的东西挪进来，并在下表登记一行。

## 登记表

| 名字 | 是什么 | 什么时候用 | 来源 |
|---|---|---|---|
| `verify-probe/` | 探针：验证 `verify.check_compile` 真能"失败"（坏文件必判错 + 好文件必通过，双向对照） | 改过 `tools/verify.py` 的语法检查层之后必跑 | 2026-09-17 回滚演练产出 |
| `inject-probe/` | 探针：真调 `load_system_prompt()`，验证系统提示的注入清单与顺序（SOUL→AGENTS→USER→MEMORY） | 改过注入清单或 `config.yaml` 的 `*_file` 路径键之后 | 2026-09-18 注入 USER.md 维护产出 |
| `pytest-diag/` | 诊断插件：在 pytest 进程内部打印真实状态（死因 / 退出码 / stderr 尾巴） | 测试报错被 `--tb` 截断、需要看进程内状态时 | 2026-09-17 排查 mcp 调用失败时写的 |
| `midturn-hook-probe/` | 探针：验证「中期交互已迁 WebUI 侧」—— agent 只提供通用钩子、mid_turn 来自 backend、注册后投递真能注入、无注册者时零行为差异（离线，不碰运行中的 AB） | 改过 `agent.register_after_tools_hook` / `agent_webui/backend/mid_turn.py` / bridge 的 7.6 注入段之后 | 2026-09-22 mid_turn 搬家维护产出 |
| `compact-gate-probe/` | 探针：验证上下文压缩的触发判据是**纯 token 闸门**（轮数防线已删 / 闸门 0.55 / 冷却只节流） | 改过 `should_compact()`、`trigger_ratio`、`cooldown_rounds` 或 config 的 `context_manager` 段之后**必跑** | 2026-09-22 压缩改纯 token 闸门维护产出 |
| `late-frame-probe/` | 探针：**后台作业的迟到帧不许改写回合/忙碌状态** —— 网关侧（迟到 turn_phase 会不会把 `busy` 置回 True）+ bridge 侧（收口后还会不会发 `turn_phase`）；另附那次修复的逐条补丁脚本 | 改过 `agent_proc._forward` / `_wrap_tool` / `RunContext` / 前端 `tool_begin`·`tool_end` 的 busy 处理之后**必跑** | 2026-09-29 「回合结束后界面自己变忙」实证与修复产出 |
| `orch-orange-probe/` | 探针：**编排器橙点如实反映跨回合作业** —— 占用条目活过回合起点、事件带作业身份（作业号+可读名）、`/orch` 权威占用视图（活作业必占一格 / 结算即出局）、网关 authoritative 纪律；另附那次修复的逐条补丁脚本 | 改过 `_pipe_emit` / `_PIPES` / `_reset_pipes_for_turn` / `_orch_view` / `_wrap_tool` 的 emit 处 / `AgentManager.orch` / 前端 `idleAll`·`merge_orch`·`OrchStrip`·`App.syncOrch` 之后**必跑** | 2026-09-29 「橙灯回复后就灭」实证与修复产出 |
| `win-job-kill/` | 双向补丁：**Windows Job Object 强杀整棵树 + 提示词同步**（`--to pre` 回退 / `--to post` 应用，往返逐字节一致已验证）。**P11 的快照建晚了、回滚不了它，所以回退用这个脚本** | 要回退 P11、或以后再改 `_kill_process_tree` / 工具的超时文案时 | 2026-09-30 P11 产出 |
| `p12-delivery-ui/` | 双向补丁：**交付挂用户卡（橙）+ 交付当场可见（done 带回执 -> 重放历史）**（同样往返逐字节一致已验证）。**P12 的快照漏声明了 4 个"中途才决定要改"的文件，回退那 4 个用它** | 要完整回退 P12、或以后再动"交付的呈现/可见性"这条链时 | 2026-09-30 P12 产出 |

---

## 纪律

- **只放能复用的**。一次性的失败产物不属于这里（该删就删）
- 每个包**自带一句用途说明**（放进来的东西，未来的你要能看懂）
- 放脚本的话，**头部写清怎么跑、有什么副作用**
- 这里的文件**不进快照**（`packs/` 是资产不是生产代码，但它会被 git 跟踪，改动可查历史）
