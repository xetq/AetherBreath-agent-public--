# P10 交付：超时被杀之后，工具必须**立刻**返回

## 一句话

你看到的"**timeout 跑满 130 秒才判**"是真的，但**不是** AB 推断的"跑完再判"——
树**确实在 120 秒被杀**（有证据），**晚的是工具的返回**：收尾时卡在一个逃过 `taskkill` 的
**孙进程**占着的管道上。现在返回准点了，并且**如实提示**那个逃掉的进程。

## 证据（同一台机器实测，先证伪再修）

```
sleep 3                          (timeout=1) -> 1.45s 返回   ✅ 准点
sleep 5 && echo DONE > MARK.txt  (timeout=1) -> 5.15s 返回   ❌ 拖满自然时长
   但 MARK.txt 不存在  ->  命令没跑完  ->  树确实在超时点被杀了（不是"跑完再判"）
杀死后 2 秒的进程表：bash.exe 已消失，sleep.exe(PID 29168) 还活着
```

机制：`taskkill /F /T` 杀掉中间那层 shell 后，真正干活的**孙进程被重新挂靠、逃出 /T 的遍历**；
它继续占着 stdout/stderr 管道 → 读线程卡在 `read()` → 收尾处 `pipe.close()` 在等读线程的锁
→ **工具要等那条命令自然结束才返回**。

## 修好之后（同机对照）

| 场景（timeout） | 修前 | 修后 |
|---|---|---|
| `sleep 3`（1s） | 1.45s | 1.65s |
| `sleep 3 && echo DONE > MARK`（1s） | **5.15s** | **1.65s** + 孤儿提示 |
| `sleep 12 && echo DONE > MARK`（2s） | **30.13s** | **2.68s** + 孤儿提示 |

所以你那发 `sleep 130 && echo BG130_OK`（timeout=120）现在会在 **~120.x 秒**返回
（不再是 130.2s），而且返回值里会多出这一段：

```
⚠️ 可能有子进程逃过强杀（taskkill 的进程树遍历漏掉了被重新挂靠的孙进程）：
它可能仍在运行，并且还占着输出管道。若这条命令会写盘/提交/删除，请先核对目标状态再决定是否重做。
```

## 怎么激活（**只重启 AB，网关不用动**）

改的是 `agent_tools/execute_shell.py` 与 `execute_python.py` → AB 进程启动时加载。
界面「⏻ 优雅关机」→「🟢 开机」即可。前端无改动。

## 期望现象（复现一次）

对 AB 说：**「用 background 跑 `sleep 15 && echo SHOULD_NOT_APPEAR`，timeout=5」**
- 约 **5 秒**后作业结算（不是我修之前的"等 15 秒"）
- 结果文案：`⏰ 执行超时（超过 5 秒），已强杀整个进程树` + 上面那段孤儿提示
- **`SHOULD_NOT_APPEAR` 不会出现**（命令没跑完）
- 自动交付进会话时"已跑 ~5.x 秒"—— 这才是超时应有的样子

## 失败了怎么办

```
venv\Scripts\python -m pytest tests/test_subprocess_timeout_return.py -q     # 期望 10 passed
```

若这条绿而真机还是等满自然时长，请把那一轮的 **bridge 日志**（`agent_webui\logs\bridge_*.log`）
发我 —— 我要看 `[中期进度]` 前后两条的时间戳，确认"工具返回"到底在哪一刻。

1. **先手动备份**：`agent_tools\execute_shell.py`、`agent_tools\execute_python.py`
2. **回滚**（需要你点头）：
   ```
   venv\Scripts\python self_maintenance\tools\snapshot.py rollback 20260930-013953-P10-超时被杀后工具必须立刻返回_管道收尾不许阻塞_孤儿孙进程如实提示 --yes
   ```
   回滚后**重启 AB**。

## ⚠️ 一个仍未修的东西（请你拍板）

**`taskkill /T` 漏掉被重新挂靠的孙进程**（Windows 特有；POSIX 用进程组没这问题）。
后果：那条命令的**孙进程可能仍在跑**（还可能写盘/提交），只是工具立刻返回并如实提示了。
这直接影响"`task_kill` 到底停没停干净"。

两个修法：

- **A（推荐，彻底）**：给子进程建 Windows **Job Object**
  （`CreateJobObject` + `SetInformationJobObject(KillOnJobClose)` + `AssignProcessToJobObject`，
  杀的时候 `TerminateJobObject`）—— 整棵树含**未来派生**的进程一起死，Windows 上唯一可靠的做法。
- **B（轻）**：自己枚举后代（Toolhelp32）**从叶子往根杀**，避开"杀中间层导致孙进程逃逸"的竞态；
  实现简单些，但仍与"杀的过程中又派生"赛跑。

我没擅自动 —— 两者都改 `_kill_process_tree`（取消/超时链路共用的咽喉），A 还要写 ctypes 结构体。
你说 A 还是 B（或先不做）。
