# P11 交付：强杀改成 Job Object（你选的 A），并同步了 AB 看到的提示词

## 一句话

Windows 上"杀进程树"从 **`taskkill /T`（按父子链，会漏）** 换成 **Job Object（按进程归属，一网打尽）**；
并且**改了行为就改了提示词**（你专门交代的那件事）。正常路径**零行为变化**。

## 怎么激活（只重启 AB，网关不用动）

改的是 `agent_tools/` 下 4 个文件 → AB 进程启动时加载。「⏻ 优雅关机」→「🟢 开机」即可。
前端无改动，网关不用动。

## 期望现象

对 AB 说：**「用 background 跑 `sleep 15 && echo SHOULD_NOT_APPEAR`，timeout=5」**

| 观察点 | 期望 |
|---|---|
| 结算时间 | 约 **5 秒**（不是 15 秒） |
| 文案 | `⏰ 执行超时（超过 5 秒），已强杀整个进程树` |
| **孤儿提示** | **不再出现** `⚠️ 可能有子进程逃过强杀…`（说明真杀干净了；只有在拿不到 Job Object 的机器上才会出现） |
| 输出 | 不含 `SHOULD_NOT_APPEAR` |

**"不污染"的自查**（成功路径照旧）：让 AB 跑
`( sleep 5; touch BG_OK.txt ) & echo started`（不设超时）→ 工具**立刻返回 started**，
5 秒后 `BG_OK.txt` **必须出现**（后台子进程没被顺手杀掉）。

## 改了什么（4 改 1 新）

| 文件 | 改动 |
|---|---|
| `agent_tools/win_job.py`（**新**） | 纯 stdlib ctypes 封装 Job Object：`create_for` / `terminate` / `close` / `available` / `why_unavailable` |
| `execute_shell.py` / `execute_python.py` | Popen 后挂进 job（失败静默降级）；`_kill_process_tree` **先试 `TerminateJobObject`**，成功即返回，否则照旧 `taskkill`/进程组；`finally` 关句柄 |
| `task_jobs.py` | `task_kill` 的口径与返回文案：旧"只能尽力中断" → 新"**线程杀不掉，进程树能杀干净**" |

**提示词同步清单**（这次专门做了，并有 3 条测试守着不许漂移）：
`execute_shell` 工具描述 + `timeout` 参数说明、`execute_python` 同上、`task_kill` 描述与返回文案、
以及被杀时那条孤儿提示（现在明说"只在没拿到 Job Object 时才会出现"）。

## 为什么不设 `KILL_ON_JOB_CLOSE`

设了的话，"关句柄"就等于"杀掉里面还活着的一切" —— 于是成功路径上你**故意放到后台的活儿**
（`nohup server &`）会在工具返回时被静默杀死。准则：**只在你要杀的时候杀**。
（实测已守住：`( sleep 2; touch X ) & echo` 成功返回后，那个后台子进程照旧活着。）

## 失败了怎么办

```
venv\Scripts\python -m pytest tests/test_subprocess_timeout_return.py -q     # 期望 17 passed
```

若这条绿而真机仍看到"孤儿提示"，说明**本机没拿到 Job Object**（降级了）——
跑一句把原因打出来发我：

```
venv\Scripts\python -c "import sys; sys.path.insert(0,'agent_tools'); import win_job as w; print(w.available(), w.why_unavailable())"
```

1. **先手动备份**：`agent_tools\execute_shell.py`、`execute_python.py`、`task_jobs.py`
2. **回滚**（需要你点头）—— ⚠️ **这次不能用 `snapshot.py rollback`**：
   我的 `snapshot begin` 是在改动**之后**才跑的（流程错误），那次快照的前像 = 改后状态，
   **回滚不了 P11 的代码**。已补一个**双向、往返逐字节验证过**的补丁脚本顶上：

   ```
   venv\Scripts\python self_maintenance\packs\win-job-kill\revert_or_apply_p11.py --root . --to pre --apply
   ```

   （它会同时删掉新增的 `agent_tools\win_job.py`；想装回来就把 `--to pre` 换成 `--to post`。）
   回滚后**重启 AB**。

## 已知边界（如实）

- **纯 Python 线程仍然杀不掉**（这是 Python 语言层的限制，Job Object 管的是进程）——
  所以 `task_kill` 的口径仍是"结果不再回来"永远成立、"对端已停"只对起子进程的工具成立。
- **任务编排器的超时（`_timeout_message`）走的是另一条路**：它是"把超时调用收编成后台作业"
  （P2 的决策），**不杀**。与工具**自身**的超时（到点强杀）是两回事，这次没动它。
