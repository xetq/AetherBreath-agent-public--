# P7 交付：点阵不再「多加橙点」+ 两件"丢数据"的真相

## 怎么激活（两半，各走各的）

| 改了什么 | 在哪 | 怎么生效 |
|---|---|---|
| 点阵不再新增格子（`OrchStrip.tsx`） | 前端 | **浏览器硬刷新**（Ctrl+F5）——我已 `npm run build`，产物 `index-Ni-rW-BM.js` |
| `task_list` 空册文案 / 看板末尾文案 | agent 侧 | **重启 AB**：「⏻ 优雅关机」→「🟢 开机」 |

**网关不用动**（本次没碰 `agent_webui/backend/*`）。
实测：当前网关起于 23:42:31、AB 起于 **0:08:00** —— 我的 agent 侧改动落盘于 0:1x，
所以 **AB 必须重启**；前端只要刷新。

## 期望现象

### 1. 点阵（格子数恒定）

- 底grid 永远是「并 16 · 串 1」共 17 个点，**不会因为作业多少而长出新点**
- 挂 10 个后台作业 → 就是其中 **10 个暗点变橙**，不是 17 个之外再冒出 10 个
- 如果有作业一时认不出槽位（池满排队 / 那一帧丢了），它会去占一个**空闲的暗点**，
  hover 会写「未映射到具体槽位（占一格空闲格显示）」
- 如果连空闲格都没有了，头部会显示「跑 N · **+K 未映射**」——**绝不画到格子外面**

### 2. 两件"丢数据"的真相（都不需要你做什么，但要知道）

- **"上一批 10 个作业整个消失"** → 那是 **AB 重启**（你按我的清单做的「关机→开机」）。
  作业登记册是**内存态**，重启即清空、编号从 j1 重来。现在 `task_list` 在空册时会明说这一点，
  AB 不会再把"空册"读成"结果被谁清了"。
- **"10 个只回来 9 个，缺 j1"** → **j1 是 AB 自己取回的**。取证在会话
  `session_20260929_235337`：AB 在那边为了验证取回通道，主动调了
  `task_output(job_id="j1")` 并拿到了 `BG15_L`。看板的判据里有"已取回的不再推"，
  所以轮到 220419 会话推看板时 j1 按设计不出现。现在看板末尾会写明
  "已被 task_output 取回的不会再列在这里（也可能是在别的会话里取的）"。

## 失败了怎么办

**先跑自动判据**（离线，几秒钟）：

```
node self_maintenance/packs/orch-orange-probe/probe_orch_slots.mjs
venv\Scripts\python -m pytest tests/test_orch_slots_contract.py tests/test_orch_board_delivery.py -q
```

期望：探针 `8/8 通过`、pytest 全绿。**若都绿而界面还是多出点**，
请把那一屏的 `/api/orch` 输出发我（`curl http://127.0.0.1:8900/api/orch`）——
有了它我能算出前端该画几格。

1. **先手动备份**：`agent_webui\frontend\src\components\OrchStrip.tsx`、
   `agent_tools\task_jobs.py`、`agent\task_orchestrator.py`
2. **回滚**（需要你点头）：
   ```
   venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260930-001619-P7-点阵格子数恒定_绝不新增橙点_未映射占空闲格_登记册重置文案 --yes
   ```
   回滚后**重建前端**（`cd agent_webui\frontend && npm run build`）并**重启 AB**。

## 遗留 / 可选

- **重启会丢"已完成作业的结果"** 这件事本身我没改（内存态是当初的设计）。
  如果你希望已完成的结果**落盘**、重启后还能 `task_output` 取回 —— 那是一次设计变更，
  说一句我做（代价：多一份要维护的磁盘状态，且重启时的在跑作业只能标成"被打断"）。
- 本轮**范围外改动 1 个**（如实报）：新建的探针
  `self_maintenance/packs/orch-orange-probe/probe_orch_slots.mjs` 忘了写进快照 `--paths`，
  所以它没有前像。它是纯测试资产，不在生产路径上。
