# P5 交付：编排器橙点要如实反映「跨回合作业」

## 这次修了什么（一句话）

橙灯以前**只活在本回合**：你一汇报完，前端、bridge 两头都把它当"本回合的工具"收掉了，
而作业其实还在跑。现在它**活过回合、活过刷新**，并且 hover 能说清"是哪个作业、跑了多久"。

## 怎么激活（⚠️ **必须做这一步**：你现在跑的网关还是旧代码）

我动代码的时候（23:28~23:40），你的网关和 AB **已经在跑**（23:13 起）。这次改的
`agent_webui/backend/*.py` 只在**进程启动时**加载，所以**当前这个网关里没有这次修复**。

**证据**（我刚实测）：`GET http://127.0.0.1:8900/api/orch` → **404**。
换上新代码后它必须返回 200 + `{"ok":true,"pipes":[...],"authoritative":true}`。

所以必须：

1. 界面点「⏻ 优雅关机」（让 AB 落盘退出；此刻 `turn_phase=IDLE`、没有回合在跑，安全）
2. **关掉网关窗口**（Ctrl+C）→ 重新双击 `agent_webui\webui.bat`
3. **浏览器硬刷新**（Ctrl+F5）—— 前端产物已换成新包 `index-Ce7Y34RO.js`
4. 界面点「🟢 开机」
5. 自检一次：浏览器打开 `http://127.0.0.1:8900/api/orch`
   - 期望：`{"ok":true,"pipes":[],"authoritative":true}`（此刻没作业，pipes 空是正常的）
   - **还是 404 → 网关没换成功**，别往下测了，先解决这一步

> 前端那半边不用重启（网关只 serve `frontend/dist`，构建我已经做完了），只要硬刷新。

## 期望现象（请按这个复现一次，重点看第 3、4 步）

1. 开一个新会话，对 AB 说：**「挂一个 30 秒的任务在后台，然后立马向我汇报」**
2. 右侧「任务编排器」面板里，**应当有一格亮橙色**（并行池 #N），
   hover 上去应当看到形如：
   ```
   并行池 #3 · 线程 orch-parallel_3 · 执行中：execute_shell（L1） · 跨回合作业 j1 ·
   execute_shell(sleep 30 && echo ...) · 已跑 7s
   ```
   注意「已跑 7s」的数字**每秒都在走**。
3. **AB 汇报完（回合结束）之后，橙灯必须继续亮着** ← 这就是这次修的 bug
4. **在作业还没跑完时按 F5 刷新页面** —— 橙灯**必须还在**
   （刷新后显示的是"真状态"，不是"推导"）
5. 约 30 秒后作业跑完 → 橙灯**自己灭掉**，那一格的 hover 变成「空闲 · 上次 execute_shell」

补充：把鼠标停在别的格子上，应当看到「串行池/并行池 #N · 线程 orch-… · 空闲/执行中」；
若出现「跨回合作业（未映射到具体槽位）」的那一格，说明作业在排队或有一帧丢了 ——
它仍然会亮，这是刻意的（宁可不精确，也不能让作业从界面上消失）。

## 失败了怎么办

**先跑一次自动判据**（不需要 AB 在跑，离线、几秒钟）：

```
venv\Scripts\python self_maintenance\packs\orch-orange-probe\probe_orch_orange.py
node self_maintenance\packs\orch-orange-probe\probe_store_runtime.mjs
```

两条都应当打印 `通过 (8/8)`。**若全过而界面还是不对**，那问题在"事件到不了浏览器"
（网络/SSE 层），不在这次改的逻辑里 —— 请按下面第 3 步抓 `/api/orch` 给我。

1. **先手动备份**：复制这几个文件到你自己的目录 ——
   `agent_webui\backend\{bridge,agent_proc,agent_client,api}.py`、
   `agent_webui\frontend\src\{App.tsx,api.ts,types.ts}`、
   `agent_webui\frontend\src\store\appStore.tsx`、
   `agent_webui\frontend\src\components\OrchStrip.tsx`
2. **回滚**（需要你点头）：
   ```
   venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260929-232831-P5-编排器橙点如实反映跨回合作业-占用条目活过回合_权威视图_前端对表 --yes
   ```
   回滚后必须**重建前端**：`cd agent_webui\frontend && npm run build`
3. **诊断**（按顺序，把结果发我）：
   - 直接把占用视图打出来（AB 开着、作业在跑时）：
     `curl http://127.0.0.1:8900/api/orch`
     期望：`{"ok":true,"pipes":[{"tc_id":"...","tool":"execute_shell","status":"running",
     "background":true,"thread":"orch-parallel_3","job_id":"j1","job_label":"...","elapsed":7.3,...}],
     "authoritative":true}` —— 这里**只要它有 `background:true` 的条目**，后端就是对的，
     问题在前端；反之后端就没给出来。
   - 前端 F12 → Network → 找 `/api/orch`：状态码是不是 200、`authoritative` 是不是 `true`
   - 还不行就看网关日志里 `agent_webui\logs\bridge_*.log` 有没有这一行：
     `[bridge] 本回合起点保留 N 个跨回合作业占用的槽位`

## 已知未做（不是这次范围）

**超时自动收编的作业**（没写 `background=true`、跑到超时才变成作业的那种）：
它起跑时是"普通管道"，所以橙点是靠定时对表（最多 5 秒）补亮的，不是当场就亮。
要当场亮，得让编排器在收编的那一刻发一条管道事件 —— 那要动 `agent/` 侧，这次没碰。

详见：`self_maintenance/NOTES.md` 第一条（2026-09-29 · P5）
