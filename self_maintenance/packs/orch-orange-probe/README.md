# orch-orange-probe —— 编排器橙点（跨回合作业占用的槽位）探针

**为什么有它**：2026-09-29 真机 —— 「挂一个 30 秒的任务在后台，然后立马向我汇报」，
AB 确实挂上了也汇报了，**但回复一落地那盏橙灯就灭了**，而作业还在跑。
主人原话："我要的是编排器能正确反映任务状况，不然都是黑箱操作。"

## 三个文件

| 文件 | 判什么 | 怎么跑 |
|---|---|---|
| `probe_orch_orange.py` | **后端底座** 8 条：回合起点保留跨回合作业的占用条目 / 事件与占用条目带作业身份 / 终态帧保留 background / `/orch` 视图（活作业必占一格、结算即出局、无 agent 不炸）/ 网关 `orch()` 的 authoritative 纪律 | `venv/Scripts/python.exe probe_orch_orange.py` |
| `probe_store_runtime.mjs` | **前端 reducer 真跑一遍**（esbuild 打包 + node 执行）8 条：`done` 之后橙点那格仍 running、普通管道照旧收 idle、`merge_orch` 非权威时一字不改 / 权威时覆盖与补格 / 作业结算后落回 idle、pipeline 事件带作业身份 | `node probe_store_runtime.mjs` |
| `apply_p5.py` | 那次修复的逐条字符串补丁（已应用在生产；留档 + 可对副本重放） | `--root <树根>` 只检查锚点，`--apply` 写盘 |

> `probe_store_runtime.mjs` 需要 `store/appStore.tsx` 导出 `reducer`（生产里那行 export
> 就是为它加的，不进任何调用路径）。它支持 `--store <别的 appStore.tsx>` 跑对照。

## 期望输出（修好之后）

```
probe_orch_orange.py     all 8 PASS -> 探针结论：通过 (8/8)
probe_store_runtime.mjs  8/8 通过
```

修之前分别是 **0/8** 和 **4/8**（前端那次里失败的头一条正是真机现象：
`回合 done 之后跨回合作业那格仍然 running <- status=idle（橙点会灭）`）。
判据先立起来，再动代码。

## 「修前 / 修后」对照怎么跑

```bash
mkdir -p /tmp/trial/agent_webui/backend
cp agent_webui/backend/{bridge,agent_proc,agent_client,api}.py /tmp/trial/agent_webui/backend/
python apply_p5.py --root /tmp/trial --apply
PYTHONPATH=<根>/agent;<根>/agent_webui/backend \
  python probe_orch_orange.py /tmp/trial/agent_webui/backend
```

⚠️ `bridge.py` 用 `__file__` 推项目根，所以副本模式必须把真实的 `agent/` 与
`agent_webui/backend` 放进 `PYTHONPATH`（NOTES 里记过同一个坑）。
本次实跑：副本 8/8，且副本与生产**逐字节一致**（SHA256 比对过）。

## 什么时候必须重跑

- 改过 `bridge._pipe_emit` / `_PIPES` / `_reset_pipes_for_turn` / `_orch_view` / `_job_info`
- 改过 `bridge._wrap_tool` 里那三处 `_pipe_emit`（pending/running/终态）
- 改过网关 `AgentManager.orch` / `api.py` 的 `/orch` 路由 / `agent_client.orch`
- 改过前端 `store/appStore.tsx`（idleAll / pipeline 字段 / merge_orch）、
  `components/OrchStrip.tsx`、`App.tsx` 的 `syncOrch`
  （对应回归用例：`tests/test_orch_orange_job.py`，20 条）
