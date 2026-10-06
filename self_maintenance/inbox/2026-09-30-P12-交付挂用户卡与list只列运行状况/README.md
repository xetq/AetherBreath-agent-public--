# P12 交付：交付挂在用户卡里（橙，区分于交代的紫）+ `task_list` 只当"仪表盘"

## 怎么激活

| 改了什么 | 需要做什么 |
|---|---|
| 前端（`lib/jobDelivery.ts`、`turns.ts`、`MessageList.tsx`、`styles.css`、store、ChatView） | **已重建 `dist`**（`tsc --noEmit && vite build` 通过）→ 浏览器**硬刷新**（Ctrl+F5） |
| agent 侧（`task_orchestrator.py`、`agent_tools/task_jobs.py`、`agent.py`、`bridge.py`） | **重启 AB**（「⏻ 优雅关机」→「🟢 开机」）；网关不用动 |

## 期望现象

### 1. 交付出现在**用户输入卡里**，橙色，与「用户交代」一眼分得开

- 位置：该回合的用户卡内、你的输入之后（与「用户交代」同一位置）
- 外观：左侧橙色竖条 + `☲ AB · 后台作业交付` + 角标 `✅ 全文已自动交付` + 作业标题行 +
  正文（输出/日志全文；超过 1500 字符折叠成「📦 交付全文 · N 字符」可展开）
- 对比：`👤 你 · 用户交代` 仍是**紫色** —— 两者不许混
- **当场可见**：回合结束时 bridge 会在 `done` 里带上"刚交付了谁"，界面据此重放一次历史 ——
  **不用再等切会话/刷新**（这是这次专门补的一环）

### 2. `task_list` 只报"编排器当前状况"

- 有在跑/待交付的：列出它们（`[会话] jN · 名字 · 运行中 12.8s` / `已完成 5.3s · 待交付`）
- 全空时：`ℹ️ 编排器当前空闲：没有在跑或待交付的后台作业。` + 说明"交付完就出册，
  要回看请在会话历史里找【后台作业交付】"
- **交付过的作业不会再出现**（它已出册）—— 所以这张表**不会越用越长**
- `task_output(job_id=j已交付)` → `❌ 没有这个后台作业：jN …它可能已经结算并交付进会话历史了…`
- 让 AB 自己验一句：`task_list()` 之后应看到"编排器当前 N 个"或"编排器当前空闲"

## 失败了怎么办

```
venv\Scripts\python -m pytest tests/test_job_delivery_ui.py -q     # 期望 14 passed
venv\Scripts\python -m pytest tests -q                             # 期望 599 passed / 1 failed(既有红)
```

**现象 → 最可能的原因**：

| 现象 | 原因 |
|---|---|
| 交付还是"主人发言"的样子 | 浏览器没硬刷新（旧 `dist` 在缓存里） |
| 交付出现了但要切会话才看见 | AB 没重启（`done` 里没有 `delivered_jobs`） |
| 表里还留着交付完的作业 | AB 没重启（旧 `task_orchestrator.py`） |
| 橙紫两种块长得一样 | `styles.css` 没进新构建（看 `dist/assets/*.css` 时间戳） |

## 回滚（需要你点头）—— 分两步，因为快照**漏声明了 4 个文件**

我在快照建好之后又临时决定加「交付当场可见」那一环，动了 4 个**没写进 `--paths`** 的文件
（`agent/agent.py`、`agent_webui/backend/bridge.py`、`frontend/src/store/appStore.tsx`、
`frontend/src/components/ChatView.tsx`）—— 它们没有前像。所以：

1. **声明过的文件**（前端主要改动 + 编排器/工具/测试）走快照：

```
venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260930-105802-P12-作业交付改用用户卡内联样式_list只列运行状况_交付后出册 --yes
```

2. **漏声明的 4 个文件**走双向补丁（「回退 → 再应用 → 与生产逐字节一致」已验证）：

```
venv\Scripts\python self_maintenance\packs\p12-delivery-ui\revert_or_apply_p12.py --root . --to pre --apply
```

回滚后：**前端重建**（`cd agent_webui\frontend && npm run build`）+ 浏览器硬刷新 + **重启 AB**。

## 一句话总结

**交付不是"一条用户消息"，是用户卡里的一段**（位置同「用户交代」，样式必须分得开）；
**`task_list` 不是表，是仪表盘** —— 内容落到会话里了，登记册就只管"现在还活着什么"。
