# P13 交付：停回合 / 关机 AB → 编排器里的作业**全部强杀**（而且等它落地）

## 怎么激活（**重启 AB + 浏览器硬刷新**）

改的是 `agent/task_orchestrator.py`、`agent/agent.py`、`agent_tools/*`（AB 进程加载）+
前端 store（已重建 dist）：

1. 界面「⏻ 优雅关机」→「🟢 开机」
2. 浏览器 **Ctrl+F5**

## 期望现象

**A. 停止本回合**

1. 让 AB 用 background 挂一个长作业：`用 background 跑 sleep 300`
2. 点「停止本回合」
3. **期望**：约 **1 秒内**那盏橙灯灭；`task_list` 里那个作业**不再出现**；
   日志里出现台账：`🛑 一起停：N 个后台作业被取消并清空（K 个确认已收手…）`
4. 真机取证（不用猜）：挂 `sleep 30 && echo X > 标记文件`，停止后**等过 30 秒**，
   标记文件**不许出现** = 进程树真被杀

**B. 优雅关机**

同上挂一个作业再「优雅关机」：**退出前**作业的进程树必须已被收掉
（`_graceful_exit` → `shutdown_orchestrator()` → 有界等待 → 才 `os._exit(0)`）。

**C. 硬杀兜底（任务管理器结束进程 / 网关强制 kill / 崩溃）**

后台作业的 Job Object 现在带 `KILL_ON_JOB_CLOSE`：**AB 一死，内核替它把这棵树收掉。**
（只对**后台作业**设 —— 前台调用里你故意放到后台的活儿，`nohup server &` 那种，照旧活着。）

**D. 关机之后点阵不许亮**

AB 关机（phase = OFF/STOPPING）时，界面把残留的橙点放回灰色 ——
不会再出现"关机了、点阵还亮着，像有活儿在跑"。

## 失败了怎么办

```
venv\Scripts\python -m pytest tests/test_job_kill_all.py -q     # 期望 10 passed
venv\Scripts\python -m pytest tests -q                          # 期望 608 passed / 1 failed(既有红)
```

| 现象 | 最可能的原因 |
|---|---|
| 停止后橙灯不灭 | AB 没重启（新 `stop_all_jobs` 没加载） |
| 灯灭了但那个活儿还在写盘 | 那个工具**不支持取消令牌**（纯线程动作）—— 台账会报 `stuck`，这是语言层限制，不是回归 |
| 关机后点阵还亮 | 前端没硬刷新（旧 `dist`） |
| `nohup` 起的服务一关 AB 就没了 | 不该发生（`KILL_ON_JOB_CLOSE` 只对后台作业设）；若真发生把那次命令发我 |

## 回滚（需要你点头）

```
venv\Scripts\python self_maintenance\tools\snapshot.py rollback <快照名见 NOTES/报告> --yes
```

回滚后**重启 AB**；若回滚含前端文件，再 `cd agent_webui\frontend && npm run build` + 硬刷新。

## 回滚补一句（我的流程失误）

快照漏声明了 `tests/test_task_jobs.py`（我只把 `stop_all_jobs(...) == 1` 改成了台账断言）——
回滚后请把那 2 处 `[\"total\"] == 1` 改回 `== 1`，否则那两条测试会因为旧实现返回 int 而失败。

## 已知边界（如实）

- **纯 Python 线程动作杀不掉**（语言层限制）——台账记为 `stuck`，文案不撒谎。
- **超时被"收编"成后台的作业**拿不到硬杀兜底（参数在收编前就绑定好了）——
  AB 被硬杀时这种作业可能残留；要兜住它得在收编时重建调用（下一轮可选）。
- POSIX 用进程组强杀（本来就没有 Windows 那种 re-parent 问题）。
