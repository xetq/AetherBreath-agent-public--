# -*- coding: utf-8 -*-
"""P13：**停止本回合 / 关机 AB** 必须把编排器里的作业**全部强杀**（不只是清登记册）。

主人 2026-09-30 的原话（PLAN 决策 7 的落点）：

> 终止回合或关机 AB 全部强杀任务编排器内所有任务

源码取证（本次维护，先看清现状再动手）：

```
停止本回合 -> bridge._stop_run -> _inject_keyboard_interrupt(线程) -> agent.py 的
              except KeyboardInterrupt -> _ORCHESTRATOR.stop_all_jobs("主人点停止")   ✅ 路通了
关机 AB     -> 网关 stop() -> bridge._graceful_exit() -> _agent.shutdown_orchestrator()
              -> TaskOrchestrator.shutdown() -> stop_all_jobs("编排器关闭")            ✅ 路也通了
```

**但那两条路都只是"把取消信号发出去"**：

* `stop_all_jobs` 是**发完即走** —— 它给每个作业 `cancel_event.set()`，然后立刻 finish + 清册。
  工具的 poll 循环要**下一拍**（约 50ms）才看见令牌、才去杀进程树。
* `_graceful_exit` 紧接着就 `os._exit(0)` —— 进程一没，**没来得及杀的子进程就成了孤儿**
  （Job Object 刻意没设 KILL_ON_JOB_CLOSE，为的是不污染"用户故意放到后台的活儿"）。

所以判据是：**发信号之后必须等它真的落地（有界），并且台账要如实**。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent"), str(ROOT / "agent_tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_orchestrator as T          # noqa: E402

BACKEND = ROOT / "agent_webui" / "backend"
AGENT_PY = ROOT / "agent" / "agent.py"
ORCH_PY = ROOT / "agent" / "task_orchestrator.py"
BRIDGE_PY = BACKEND / "bridge.py"


def _orch(**kw):
    o = T.TaskOrchestrator(max_workers=2, default_timeout=60, **kw)
    o._register_signal_handlers = lambda: None
    return o


def _cancelable(seen=None, block=3.0):
    """一个"支持取消令牌"的工具（与 execute_shell 同款契约）。"""
    def fn(command="", cancel_event=None, **kw):
        if seen is not None and cancel_event is not None:
            seen["ev"] = cancel_event
        deadline = time.time() + block
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return "⏹ 已被取消"
            time.sleep(0.02)
        return "ok"
    return fn


# ============================================================
# 1. 软中断：取消令牌要真的发到每个存活作业手里，并且**等它落地**
# ============================================================

def test_stop_all_jobs_signals_every_live_job():
    seen = {}
    o = _orch(tools_map={"probe": _cancelable(seen, block=5.0)}, tool_timeouts={},
              side_effect_tools=set())
    try:
        o.execute([T.ToolCall(id="c1", name="probe",
                              arguments={"command": "x", "background": True})])
        time.sleep(0.2)
        assert o.jobs.get("j1").alive, "作业没跑起来，后面的判据都无从谈起"
        led = o.stop_all_jobs("主人点停止")
        assert led["total"] == 1 and led["settled"] == 1
        assert seen.get("ev") is not None and seen["ev"].is_set(), \
            "取消令牌没发到工具手里 —— 工具根本不知道自己该停"
        assert o.jobs.list_jobs() == [], "清册"
    finally:
        o.shutdown()


def test_stop_all_jobs_waits_for_the_kill_to_land():
    """**核心判据**：`stop_all_jobs` 返回时，作业必须已经**真的停了**（不是"信号发出去了"）。"""
    o = _orch(tools_map={"probe": _cancelable(block=5.0)}, tool_timeouts={},
              side_effect_tools=set())
    try:
        o.execute([T.ToolCall(id="c1", name="probe",
                              arguments={"command": "x", "background": True})])
        time.sleep(0.2)
        job = o.jobs.get("j1")
        t0 = time.time()
        res = o.stop_all_jobs("主人点停止")
        dt = time.time() - t0
        assert dt < 3.0, "停止被拖了 %.2fs（有界等待不该超过 wait）" % dt
        assert job.status == "cancelled"
        d = res if isinstance(res, dict) else {}
        assert d.get("settled") == 1, \
            "台账要如实报「几个真的停下来了」，实际=%r" % (d or res)
    finally:
        o.shutdown()


def test_stop_all_jobs_is_bounded_when_tool_ignores_cancel():
    """**有界性**：一个不理会取消令牌的顽固工具，绝不许把"停止"按钮卡住。"""
    def stubborn(command="", cancel_event=None, **kw):
        time.sleep(5.0)
        return "我不退"
    o = _orch(tools_map={"stubborn": stubborn}, tool_timeouts={}, side_effect_tools=set())
    try:
        o.execute([T.ToolCall(id="c1", name="stubborn",
                              arguments={"command": "x", "background": True})])
        time.sleep(0.2)
        t0 = time.time()
        res = o.stop_all_jobs("主人点停止", wait=1.0)
        dt = time.time() - t0
        assert dt < 2.0, "顽固作业把停止卡了 %.2fs —— 停止必须有界" % dt
        d = res if isinstance(res, dict) else {}
        assert d.get("settled") == 0 and d.get("stuck") == 1, \
            "台账必须如实报「1 个没停下来」，实际=%r" % (d or res)
        assert o.jobs.list_jobs() == [], "就算没停下来，也要出册（不许留着僵尸条目）"
    finally:
        o.shutdown()


# ============================================================
# 2. 关机：shutdown 也要等它落地（否则 os._exit 一来就是孤儿进程）
# ============================================================

def test_shutdown_kills_jobs_before_returning():
    o = _orch(tools_map={"probe": _cancelable(block=5.0)}, tool_timeouts={},
              side_effect_tools=set())
    o.execute([T.ToolCall(id="c1", name="probe",
                          arguments={"command": "x", "background": True})])
    time.sleep(0.2)
    job = o.jobs.get("j1")
    t0 = time.time()
    o.shutdown()
    dt = time.time() - t0
    assert dt < 5.0, "关编排器拖了 %.2fs" % dt
    assert job.status == "cancelled", "关机必须把作业结算掉（不能只是清册）"


def test_graceful_exit_shuts_orchestrator_before_exit():
    """bridge 的优雅退出：先关编排器（它会等强杀落地），再 os._exit。

    ⚠️ 这条判据是**顺序**的：`shutdown_orchestrator()` 必须出现在 `os._exit` **之前**，
    否则进程一没，没杀干净的子树就成孤儿（Job Object 没设 KILL_ON_JOB_CLOSE）。
    """
    src = BRIDGE_PY.read_text(encoding="utf-8")
    seg = src[src.index("def _graceful_exit"):]
    seg = seg[:seg.index("def main")]
    i_shut = seg.index("shutdown_orchestrator")
    i_exit = seg.index("os._exit")
    assert i_shut < i_exit, "先 os._exit 再关编排器 = 强杀根本没机会落地"


# ============================================================
# 3. 真进程取证：停回合之后，那条 shell 的进程树必须真没了
# ============================================================

def test_real_shell_job_is_really_killed_by_stop(tmp_path):
    """用**真的 `execute_shell`** 挂一个后台作业，停止后它不许跑完（标记文件不许出现）。"""
    from agent_tools.execute_shell import execute_shell as _shell_fn   # 注意：包命名空间里
    mark = tmp_path / "NEVER.txt"                                      # execute_shell 是**函数**
    cmd = "sleep 6 && echo DONE > NEVER.txt"
    o = _orch(tools_map={"execute_shell": _shell_fn},
              tool_timeouts={}, side_effect_tools=set())
    try:
        o.execute([T.ToolCall(id="c1", name="execute_shell",
                              arguments={"command": cmd, "timeout": 60, "background": True})])
        time.sleep(1.0)                       # 让它真的把 bash 拉起来
        assert o.jobs.get("j1").alive, "作业没跑起来"
        t0 = time.time()
        res = o.stop_all_jobs("主人点停止")
        dt = time.time() - t0
        assert dt < 4.0, "停止真进程花了 %.2fs（太慢 = 用户在等）" % dt
        time.sleep(6.0)                       # 越过命令的自然时长，看它到底有没有被杀
        assert not mark.exists(), "命令跑完了 —— 强杀没落到实处（进程树还活着）"
        assert (res if isinstance(res, dict) else {}).get("settled") == 1
    finally:
        o.shutdown()


# ============================================================
# 4. 装配契约：两条路都必须真的调用它（防以后被重构掉）
# ============================================================

def test_agent_interrupt_calls_stop_all_jobs():
    src = AGENT_PY.read_text(encoding="utf-8")
    seg = src[src.index("except KeyboardInterrupt"):]
    seg = seg[:seg.index("def ", 10)]
    assert "stop_all_jobs" in seg, "「停止本回合」不再一起停后台作业了"


def test_stop_all_jobs_reports_counts_honestly():
    """返回值/日志必须能区分「停下来了」与「只是丢了结果」—— 口径不许含糊。"""
    src = ORCH_PY.read_text(encoding="utf-8")
    seg = src[src.index("def stop_all_jobs"):]
    seg = seg[:seg.index("\n    def ", 10)]
    assert "settled" in seg and "stuck" in seg, "台账没有区分「真的停了」与「没停下来」"
    assert "wait" in seg, "stop_all_jobs 没有有界等待"


# ============================================================
# 5. 硬杀兜底：AB 被 taskkill /F（没走优雅退出）时，作业的进程树不许变孤儿
# ============================================================
#
# 为什么需要它：优雅关机那条路我们已经"发信号 + 等落地"了；但 AB 也可能**根本没机会收尾**
# （任务管理器结束进程 / 网关强制 kill / 崩溃）。那时唯一还能兜底的是内核：
# 给**后台作业**的 Job Object 设 KILL_ON_JOB_CLOSE —— AB 一死，句柄随之关闭，
# 内核把整棵树收掉。（刻意只对后台作业设：前台调用里用户故意放到后台的活儿不许被顺手杀。）

_HELPER = '''
import sys, time
ROOT = r"__ROOT__"
for p in (ROOT, ROOT + r"\\agent", ROOT + r"\\agent_tools"):
    sys.path.insert(0, p)
import task_orchestrator as T
from agent_tools.execute_shell import execute_shell

work = sys.argv[1]
o = T.TaskOrchestrator(max_workers=2, default_timeout=120,
                       tools_map={"execute_shell": execute_shell},
                       tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
o._register_signal_handlers = lambda: None
T.set_current_orchestrator(o)
o.set_session("s_hardkill")
import os as _os
mark = _os.path.join(work, "MARK.txt").replace(_os.sep, "/")
o.execute([T.ToolCall(id="c1", name="execute_shell",
                      arguments={"command": "sleep 6 && echo DONE > %s" % mark,
                                 "timeout": 120, "background": True})])
time.sleep(0.6)                       # 让 bash 真的起来
print("READY", flush=True)
while True:
    time.sleep(0.5)                   # 等被硬杀
'''


def test_hard_killed_ab_leaves_no_orphan_job_process(tmp_path):
    """**硬杀兜底**：AB 被 `taskkill /F` 之后，后台作业的进程树不许继续活着。"""
    import subprocess
    helper = tmp_path / "ab_stand_in.py"
    helper.write_text(_HELPER.replace("__ROOT__", str(ROOT)), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(helper), str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            cwd=str(ROOT))
    try:
        ready = False
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "READY" in line:
                ready = True
                break
        assert ready, "替身 AB 没能把后台作业挂起来"
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
        proc.wait(timeout=15)
        time.sleep(7.5)               # 越过 `sleep 6` 的自然时长
        assert not (tmp_path / "MARK.txt").exists(), (
            "AB 被硬杀之后，那条 shell 还活着并把标记写出来了 —— 孤儿进程")
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)


# ============================================================
# 6. 关机之后点阵不许还亮着（浏览器那半）
# ============================================================

def test_frontend_darkens_orchestrator_when_agent_goes_off():
    """AB 一关机，编排器整块就没了 —— 界面必须把残留的橙点放回灰色。

    （比"等 /api/orch 请求失败"精确：那条路连一次网络抖动都会误伤。）
    """
    store = (ROOT / "agent_webui" / "frontend" / "src" / "store" / "appStore.tsx").read_text(
        encoding="utf-8")
    seg = store[store.index("case 'agent_phase'"):]
    seg = seg[:seg.index("case '", 10)]
    assert "idleAll" in seg, "关机后没有把残留的编排器占用放回灰色"
    assert "'OFF'" in seg and "'STOPPING'" in seg, "判据要用进程级事实（phase），不是猜"