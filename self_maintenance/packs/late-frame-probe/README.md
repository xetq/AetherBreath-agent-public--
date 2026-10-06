# late-frame-probe —— 后台作业「迟到帧」污染回合状态机的探针

**为什么有它**：2026-09-29 真机事故（挂 30 秒后台作业 + 立即汇报 → 界面自己变回
"回合进行中"，点停止却说 no-active-run）。根因是**后台作业在回合收口之后才收尾**，
而 bridge 与网关都会因为这一帧 `turn_phase` 把"忙碌态"重新点亮。这个包就是那次
"先写探针、再改代码"留下的判据。

**性质**：离线探针 —— 不起进程、不连网、不碰 LLM、不碰运行中的 AB，直接 import
真实模块跑行为断言。

## 三个文件

| 文件 | 判什么 | 怎么跑 |
|---|---|---|
| `probe_late_bg_event.py` | **网关侧**：回合 done 之后，属于该回合的迟到 `turn_phase` 会不会把 `StateMachine.busy` 置回 True，以及这一帧有没有带着 `busy=True` 发给前端 | `python probe_late_bg_event.py` |
| `probe_bridge_late_turn.py` | **bridge 侧**：`RunContext.finished=True` 之后，被包装的工具还会不会发 `turn_phase`（断源），同时 `tool_end` 必须照发 | `python probe_bridge_late_turn.py` |
| `apply_fix.py` | 那次修复的**逐条字符串补丁**（已应用在生产；留档 + 可对副本重放） | `python apply_fix.py --root <树根>`（只检查锚点）/ `--apply` |

## 期望输出（修好之后）

```
probe_late_bg_event.py     -> 探针结论：通过（exit 0）
                              事件流里唯一 busy=True 的帧 = 合法的 tool_begin
probe_bridge_late_turn.py  -> 探针结论：通过（exit 0）
                              finished=True 时事件只剩 tool_begin / tool_end
```

修之前它们是 FAIL（exit 1）—— 这正是"判据先立起来"的用法：**先看它红，再改代码。**

## 「修前 / 修后」对照怎么跑

两支探针都接受一个可选的"模块目录"参数，排在 `sys.path[0]`，于是可以用**同一份探针**
跑两份代码：

```bash
# 1) 把要试的生产文件复制到临时目录（保持同样的相对层级）
mkdir -p /tmp/trial/agent_webui/backend
cp agent_webui/backend/agent_proc.py /tmp/trial/agent_webui/backend/

# 2) 在副本上打补丁
python apply_fix.py --root /tmp/trial --apply

# 3) 用副本跑探针
python probe_late_bg_event.py /tmp/trial/agent_webui/backend
```

⚠️ 跑 `probe_bridge_late_turn.py <副本目录>` 时，`bridge.py` 会用 `__file__` 推项目根，
所以要把**真实**的 `agent/` 与 `agent_webui/backend` 放进 `PYTHONPATH`：

```bash
PYTHONPATH=<项目根>/agent;<项目根>/agent_webui/backend \
  python probe_bridge_late_turn.py /tmp/trial/agent_webui/backend
```

（上一条是 2026-09-29 实跑时踩到的：副本 import 生产代码时，它会用 `__file__` 推项目根
—— NOTES 里记过同一个坑。）

## 什么时候必须重跑

- 改过 `agent_webui/backend/agent_proc.py` 的 `_forward` / `stop_run` / `status`
- 改过 `bridge.py` 的 `_wrap_tool` / `_execute_turn` / `RunContext`
- 改过前端 `store/appStore.tsx` 里 `tool_begin` / `tool_end` 的 busy 处理
  （对应回归用例：`tests/test_late_background_events.py`，15 条）
