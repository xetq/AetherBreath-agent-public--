# P8 交付：后台作业改成「自动交付全文」

## 一句话

以前是"**看板 + 你自己取回**"，现在是"**作业跑完 → 全文自动送进会话 → 释放内存 → 列表标已交付**"。

## 怎么激活（**只重启 AB，网关不用动**）

改的 4 个文件（`agent/agent.py`、`agent/task_orchestrator.py`、`agent_tools/task_jobs.py`、
`agent_webui/backend/bridge.py`）**都在 AB 进程里跑**（`bridge.py` 是 AB 子进程的入口脚本，
由网关拉起的是新进程）。所以：

1. 界面点「⏻ 优雅关机」
2. 界面点「🟢 开机」

前端**没有改动**，不用刷新也不用重建。

## 期望现象（三条，对应你提的三点）

### ① `task_list` 只是列表

```
后台作业（共 3 个）：
  [session_xxx] j1 · execute_shell(sleep 5 && echo BG5_L) · 已完成 5.3s · 已交付
  [session_xxx] j2 · execute_shell(sleep 30 && echo LONG) · 运行中 12.8s
  [session_xxx] j3 · execute_shell(sleep 5 && echo BG5_K) · 已完成 5.3s · 待交付
→ 看某个作业的运行情况：task_output(job_id=...)；终止：task_kill(job_id=...)。
  · 已结算的作业会自动交付全文给它的归属会话，这里不返回内容。
```

**不会再出现「未取回」**；两个标签只有 `· 已交付` / `· 待交付`。

### ② 跑完的全文会自动进会话（并在界面上看得见）

- 在挂作业的那个会话里**下一次说话时**，作业的**完整输出**会作为一条消息进入上下文，
  开头是 `【后台作业交付 · 跨回合任务（不是你本回合发起的动作）】`
- 界面上它显示为**一条带前缀的消息**（和「用户交代」同一类），点开能看到全文
- 交付后 `task_list` 里那条变成 `· 已交付`，**内存已释放**（`task_output` 不再返回内容）

### ③ `task_output` 只报运行情况

- **运行中** → 状态 + 已跑时长 + 最近 40 行日志（这是它的主用途：长作业看进度）
- **已结算** → 只有一句：

```
作业 j1：execute_shell(sleep 5 && echo BG5_L)（已完成，已跑 5.3s）
结果已**自动交付**到本会话历史（这里不再返回内容）。
输出大小：约 15390 字符。
```

## 一个必须知道的取舍

**交付的是全文，所以它会留在会话历史里**（这是"释放内存"能成立的前提：
内容已经落到历史，登记册里那份就可以扔了）。代价：一个 15KB 的结果 ≈ 5k token，
长会话会更快触发上下文压缩。如果哪天你希望"超过 N KB 就只交付头尾"，说一句——
现在按你的原话是**不截断**。

## 失败了怎么办

**先跑自动判据**（离线、几秒）：

```
venv\Scripts\python -m pytest tests/test_job_board_delivery.py tests/test_task_jobs.py -q
```

期望 `45 passed` 左右。若全绿而真机行为不对，把那一轮的 bridge 日志发我
（我要看 `[后台作业交付] N 个作业的全文已写入会话: j1, j2` 这行有没有出现）。

1. **先手动备份**：`agent\agent.py`、`agent\task_orchestrator.py`、`agent_tools\task_jobs.py`、
   `agent_webui\backend\bridge.py`
2. **回滚**（需要你点头）：
   ```
   venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260930-005805-P8-作业自动交付全文_释放内存_list只列表_task_output只报运行 --yes
   ```
   回滚后**重启 AB** 即恢复旧语义（看板 + 取回）。
