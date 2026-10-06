"""后台作业（P1）：登记册 / 三工具 / background 保留参数 —— 回归测试。

判据：一条命令、无需 LLM、不写真实文件。
    venv/Scripts/python -m pytest tests/test_task_jobs.py -q

锁定的契约：
  · 挂起（background=true）立即返回占位，**不阻塞本回合**（对比：前台调用必须阻塞）
  · 作业结算后结果可跨回合取回；已结算的作业不被迟到结果覆盖
  · 每个作业只在**结算后**自动交付一次全文；谁都不能靠"看一眼"把它标成交付过
  · 交互/等待类工具不能转后台（名单与 _NEVER_PARALLEL_TOOLS 同源）
  · background 参数必须在调用工具前被剥离（工具签名里没有它）
  · 没有编排器时三个工具如实降级，不假装"没有作业"
"""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_orchestrator as T                                  # noqa: E402
from agent_tools import task_jobs as J                         # noqa: E402
from agent_tools import TOOLS_SCHEMA, TOOL_TIMEOUTS            # noqa: E402


@pytest.fixture(autouse=True)
def _clean_orch():
    """每个用例前后都摘掉当前编排器（它是模块级全局状态）。"""
    T.set_current_orchestrator(None)
    yield
    T.set_current_orchestrator(None)


def _tool(command="", timeout=None, logger=None):
    """假工具：签名里**没有 background**（用它证明该参数被剥离）。"""
    if logger:
        logger.info("跑 " + command)
    return "ok:" + command


def _slow(command="", logger=None):
    time.sleep(0.4)
    if logger:
        logger.info("慢活跑完 " + command)
    return "slow-ok:" + command


def _orch(tools=None):
    o = T.TaskOrchestrator(max_workers=3, default_timeout=30,
                           tools_map=tools or {"_tool": _tool, "_slow": _slow},
                           tool_timeouts={}, side_effect_tools={"_slow"},
                           log_enabled=False)
    o._register_signal_handlers = lambda: None
    return o


def _call(i, name, args):
    return T.ToolCall(id=i, name=name, arguments=dict(args))


# ---------- 1. 登记册本体 ----------

def test_register_and_get():
    reg = T.JobRegistry()
    j = reg.register(tool="execute_shell", label="execute_shell(python a.py)", sid="s1")
    assert j.job_id == "j1" and reg.get("j1") is j and j.status == "running"
    assert reg.get("nope") is None


def test_label_is_readable():
    assert T.JobRegistry.make_label("execute_shell", {"command": "python train.py --epoch 3"}) \
        == "execute_shell(python train.py --epoch 3)"
    _long = T.JobRegistry.make_label("execute_shell", {"command": "x" * 200})
    assert "..." in _long and len(_long) < 100
    assert "background" not in T.JobRegistry.make_label("t", {"background": True, "a": 1})


def test_finish_is_sticky():
    """已结算的作业不被迟到结果覆盖（kill 之后 worker 才返回的情形）。"""
    reg = T.JobRegistry()
    j = reg.register(tool="t", label="t()", sid="s")
    j.finish(error="被停止", status="cancelled")
    j.finish(result="迟到的成功")
    assert j.status == "cancelled" and j.result is None


def test_log_ring_is_bounded():
    reg = T.JobRegistry()
    j = reg.register(tool="t", label="t()", sid="s")
    for i in range(T.JOB_LOG_RING_LINES + 80):
        j.append_log("line-%d" % i)
    assert len(j.log_lines) == T.JOB_LOG_RING_LINES
    assert j.log_lines[-1] == "line-%d" % (T.JOB_LOG_RING_LINES + 79)


# ---------- 2. 交付队列（旧"看板"在 P8 被自动交付取代） ----------

def test_pending_delivery_empty_without_jobs():
    assert T.JobRegistry().pending_delivery() == []


def test_pending_delivery_carries_finished_output():
    """**语义变更（2026-09-30 P8）**：看板 -> 自动交付。

    旧契约是 `board_text()` 渲染一段截断的看板；现在是"每个作业各自全文交付"。
    判据见 tests/test_job_board_delivery.py（含"交付的是全文、不是 4KB 截断版"）。
    """
    reg = T.JobRegistry()
    j = reg.register(tool="t", label="t(x)", sid="s1")
    j.finish(result="产出内容")
    pend = reg.pending_delivery("s1")
    assert pend == [j]
    txt = j.delivery_text()
    assert "产出内容" in txt and j.job_id in txt and T.JOB_DELIVERY_PREFIX in txt


def test_delivered_job_leaves_pending_queue():
    reg = T.JobRegistry()
    j = reg.register(tool="t", label="t()", sid="s")
    j.finish(result="x")
    assert reg.pending_delivery("s") == [j]
    reg.mark_delivered("j1")
    assert reg.pending_delivery("s") == []
    assert j.result is None, "交付后要释放内存"
    assert reg.get("j1") is None, "交付后要出册（P12：表不许越用越长）"


# ---------- 3. 挂起：本回合不阻塞 ----------

def test_background_returns_placeholder_immediately():
    o = _orch()
    t0 = time.time()
    br = o.execute([_call("c1", "_slow", {"command": "long", "background": True})])
    dt = time.time() - t0
    res = br.results[0]
    assert dt < 0.3, "挂起不该阻塞（实测 %.2fs）" % dt
    assert res.success and "已转后台" in str(res.result) and "j1" in str(res.result)
    assert (res.metadata or {}).get("background_job") == "j1"
    assert o.jobs.get("j1").status == "running"
    o.jobs.wait("j1", 3)
    assert "slow-ok" in str(o.jobs.get("j1").result)


def test_foreground_still_blocks_and_no_job_is_registered():
    o = _orch()
    br = o.execute([_call("c1", "_slow", {"command": "sync"})])
    assert "slow-ok" in str(br.results[0].result)
    assert o.jobs.list_jobs() == []


def test_background_param_is_stripped_before_tool_call():
    """background 不许传进工具函数（它的签名里没有这个参数）。"""
    seen = {}

    def _spy(command="", **kw):
        seen.update(kw)
        return "spy:" + command

    o = _orch(tools={"_spy": _spy})
    o.execute([_call("c1", "_spy", {"command": "x", "background": True})])
    o.jobs.wait("j1", 3)
    assert "background" not in seen, seen
    assert "logger" in seen or True


def test_background_does_not_delay_foreground_in_same_batch():
    o = _orch()
    t0 = time.time()
    br = o.execute([_call("f1", "_tool", {"command": "fg"}),
                    _call("b1", "_slow", {"command": "bg", "background": True})])
    dt = time.time() - t0
    m = {r.tool_call_id: r for r in br.results}
    assert dt < 0.35, "同批前台不该被后台拖住（实测 %.2fs）" % dt
    assert "ok:fg" in str(m["f1"].result)
    assert "已转后台" in str(m["b1"].result)
    o.jobs.wait("j1", 3)


def test_interactive_tool_cannot_go_background():
    """交互类（名单同源于 _NEVER_PARALLEL_TOOLS）即使声明 background 也走前台。"""
    o = _orch(tools={"ask_user": _tool})
    assert "ask_user" in T.NO_BACKGROUND_TOOLS
    br = o.execute([_call("c1", "ask_user", {"command": "q", "background": True})])
    assert "已转后台" not in str(br.results[0].result)
    assert o.jobs.list_jobs() == []


# ---------- 4. 三工具 ----------

def test_tools_degrade_without_orchestrator():
    assert J.task_list().startswith(chr(8505))
    assert J.task_output(job_id="j1").startswith(chr(8505))
    assert J.task_kill(job_id="j1").startswith(chr(8505))


def test_task_output_unknown_job_is_failure():
    T.set_current_orchestrator(_orch())
    out = J.task_output(job_id="j999")
    assert out.startswith("❌") and "j999" in out


def test_task_output_never_touches_delivery_state():
    """**语义变更（2026-09-30 P8）**：`task_output` 只看运行情况，不读内容、不改交付状态。

    旧契约是"结算后才标 fetched，从而从看板消失"。现在交付是**自动的**：
    谁都不能靠"看一眼"把作业标成交付过（那会让它永远收不到自动交付）。
    """
    o = _orch()
    o.set_session("s1")            # 作业的归属会话来自编排器的 _session_id（agent 每轮都设）
    T.set_current_orchestrator(o)
    o.execute([_call("c1", "_slow", {"command": "x", "background": True})])
    out = J.task_output(job_id="j1", wait_seconds=0)
    assert "运行中" in out, "运行中就该给状态 + 日志"
    assert o.jobs.get("j1") is not None, "看一眼不等于交付过（作业还该在册）"
    # 待交付队列的硬不变量：**只装已结算的**（不看时序，跑快跑慢都成立）
    assert all(not p.alive for p in o.jobs.pending_delivery("s1")), \
        "待交付队列里混进了还在跑的作业"
    J.task_output(job_id="j1", wait_seconds=5)          # 等它结算
    assert o.jobs.get("j1") is not None, "看进度也不许把作业弄出册"
    assert o.jobs.get("j1") in o.jobs.pending_delivery("s1"), "结算后应当排进待交付"


def test_task_list_lists_jobs():
    o = _orch()
    T.set_current_orchestrator(o)
    o.execute([_call("c1", "_slow", {"command": "x", "background": True})])
    out = J.task_list()
    assert "j1" in out and "_slow(x)" in out
    o.jobs.wait("j1", 3)


def test_task_kill_does_not_claim_terminated():
    """口径红线：kill 保证的是"结果不再回来"，不是"对端已停"。"""
    o = _orch()
    T.set_current_orchestrator(o)
    o.execute([_call("c1", "_slow", {"command": "x", "background": True})])
    out = J.task_kill(job_id="j1")
    assert "仍在后台跑" in out and "已终止" not in out
    assert o.jobs.get("j1") is None


# ---------- 5. background 保留参数注入 ----------

def test_background_injected_into_schemas():
    names = [s.get("function", {}).get("name") for s in TOOLS_SCHEMA]
    assert "execute_shell" in names
    props = [s for s in TOOLS_SCHEMA if s["function"]["name"] == "execute_shell"][0]
    props = props["function"]["parameters"]["properties"]
    assert "background" in props and props["background"]["type"] == "boolean"


def test_background_not_injected_for_interactive_tools():
    for s in TOOLS_SCHEMA:
        fn = s.get("function") or {}
        if fn.get("name") in T.NO_BACKGROUND_TOOLS:
            props = (fn.get("parameters") or {}).get("properties") or {}
            assert "background" not in props, fn["name"]


def test_inject_is_idempotent():
    assert J.inject_background_params(TOOLS_SCHEMA) == 0


def test_task_output_timeout_declared():
    """它要能等满 wait_seconds，编排器时限必须留够（否则会被超时掐掉）。"""
    assert TOOL_TIMEOUTS.get("task_output", 0) > J._MAX_WAIT
    assert TOOL_TIMEOUTS.get("task_output", 0) <= 300


# ---------- 6. P2：超时收编 / 看门狗 / 一起停 / 尽力中断 ----------

@pytest.fixture
def orch_pool():
    """创建编排器并**保证收尾关闭** —— 池线程是非 daemon 的，不关会把 pytest 钉死。"""
    made = []

    def _make(**kw):
        o = T.TaskOrchestrator(max_workers=3, **kw)
        o._register_signal_handlers = lambda: None
        made.append(o)
        return o

    yield _make
    for o in made:
        try:
            o.shutdown()
        except Exception:
            pass


def _slow3(command="", logger=None):
    time.sleep(3)
    return "slow-ok:" + command


def _cancelable(command="", cancel_event=None):
    t0 = time.time()
    while not (cancel_event is not None and cancel_event.is_set()):
        if time.time() - t0 > 8:
            return "等不到取消信号（说明令牌没注入）"
        time.sleep(0.05)
    return "看到了取消信号"


def test_timeout_becomes_background_job(orch_pool):
    """超时兜底：到点不判失败，而是转成后台作业（结果有地方回来）。"""
    o = orch_pool(default_timeout=1, tools_map={"_slow3": _slow3}, tool_timeouts={},
                  side_effect_tools=set(), log_enabled=False)
    t0 = time.time()
    br = o.execute([_call("c1", "_slow3", {"command": "bg"})])
    dt = time.time() - t0
    r = br.results[0]
    assert r.success and "已自动转入后台" in str(r.result)
    assert dt < 2.0, "不该等工具跑完（%.2fs）" % dt
    assert o.jobs.get("j1") is not None
    o.jobs.wait("j1", 8)
    assert "slow-ok" in str(o.jobs.get("j1").result)


def test_interactive_tool_never_autobackgrounds(orch_pool):
    o = orch_pool(default_timeout=1, tools_map={"ask_user": _slow3}, tool_timeouts={},
                  side_effect_tools=set(), log_enabled=False)
    br = o.execute([_call("c1", "ask_user", {"command": "q"})])
    assert not br.results[0].success
    assert o.jobs.list_jobs() == []


def test_watchdog_expires_job(orch_pool, monkeypatch):
    """看门狗：超寿命作业被标 expired，且文案如实说明线程可能仍在跑。"""
    monkeypatch.setattr(T, "JOB_WATCHDOG_INTERVAL", 0.2)
    monkeypatch.setattr(T, "JOB_MAX_LIFETIME", 1)
    o = orch_pool(default_timeout=30, tools_map={"_slow3": _slow3}, tool_timeouts={},
                  side_effect_tools=set(), log_enabled=False)
    o.execute([_call("c1", "_slow3", {"command": "z", "background": True})])
    deadline = time.time() + 6
    while time.time() < deadline and o.jobs.get("j1").status == "running":
        time.sleep(0.1)
    job = o.jobs.get("j1")
    assert job.status == "expired", job.status
    assert "超寿命" in (job.error or "")
    assert "线程可能仍在跑" in (job.error or "")


def test_background_task_gets_cancel_token(orch_pool):
    """支持 cancel_event 的工具必须拿到令牌 —— 显式挂起走的是 _launch_background，
    不经过 _run_on_pool 的提交循环（这一处曾漏，导致工具永远等不到取消信号）。"""
    o = orch_pool(default_timeout=30, tools_map={"_cancelable": _cancelable},
                  tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    tc = _call("c1", "_cancelable", {"command": "x", "background": True})
    o.execute([tc])
    assert (tc.metadata or {}).get("cancel_event") is not None


def test_stop_all_jobs_cancels_and_sets_token(orch_pool):
    """一起停：标记取消 + 真的把取消信号打到工具 + 清空登记册。"""
    o = orch_pool(default_timeout=30, tools_map={"_cancelable": _cancelable},
                  tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    tc = _call("c1", "_cancelable", {"command": "x", "background": True})
    o.execute([tc])
    ev = (tc.metadata or {}).get("cancel_event")
    job = o.jobs.get("j1")
    assert callable(job.interrupter)
    assert not ev.is_set()
    assert o.stop_all_jobs("测试")["total"] == 1
    assert job.status == "cancelled"
    assert ev.is_set(), "一起停必须真的打到工具"
    assert o.jobs.list_jobs() == []
    assert o.jobs.pending_delivery() == []


def test_tool_without_token_reports_none(orch_pool):
    """不支持取消令牌的工具：interrupter 保持 None —— 不许装作能中断。"""
    o = orch_pool(default_timeout=30, tools_map={"_slow3": _slow3}, tool_timeouts={},
                  side_effect_tools=set(), log_enabled=False)
    o.execute([_call("c1", "_slow3", {"command": "x", "background": True})])
    job = o.jobs.get("j1")
    assert job.interrupter is None
    assert o.stop_all_jobs("测试")["total"] == 1
    assert job.status == "cancelled"

# ---------- 7. 装配：agent 必须把编排器注册给工具侧（真机事故的回归门） ----------

def test_agent_registers_orchestrator_for_tools():
    """`agent._get_orchestrator()` 之后，**工具侧必须能拿到它**。

    本用例的存在理由（2026-09-29 真机事故）：注册那一行漏了 ->
    task_list / task_output / task_kill 恒定返回"没有可用的任务编排器"，
    而看板照常推送（看板走 agent 内部的 _ORCHESTRATOR，不经过这个注册）。

    旧测试全都自己 `T.set_current_orchestrator(...)` 手动注册 —— **等于自己给自己喂答案**，
    于是"装配"这一环从没被测到。所以本用例刻意**不手动注册**，逼真装配线自己走一遍。
    """
    import agent as ab
    try:
        o = ab._get_orchestrator()
        assert o is not None
        assert T.get_current_orchestrator() is o, "agent 必须把常驻编排器注册给工具侧"
        out = J.task_list()
        # 判据要精确：ℹ️ 同时用于"没有编排器"和"没有作业"两种**都正确**的返回，
        # 所以不能只看前缀 —— 要看它说的是哪一句。
        assert "没有可用的任务编排器" not in out, \
            "有了编排器就不该再说拿不到编排器：%s" % out[:40]
        assert "编排器当前空闲" in out, out[:40]
    finally:
        ab.shutdown_orchestrator()
    assert T.get_current_orchestrator() is None, "关闭后必须注销（否则工具会拿到已死的编排器）"


def test_background_goes_to_parallel_pool_not_serial(orch_pool):
    """后台作业必须跑在**并行池**上。

    真机事故（2026-09-29）：单工具调用走串行池，而串行池只有 1 个 worker ——
    一个 30 秒的后台作业把整个串行通道占死，其它单工具调用全在排队。
    判据用**线程名**（直接证据），不看日志文案。
    """
    seen = {}

    def _who(command="", cancel_event=None):
        seen["thread"] = threading.current_thread().name
        time.sleep(0.2)
        return "done"

    o = orch_pool(tools_map={"_who": _who}, tool_timeouts={},
                  side_effect_tools=set(), log_enabled=False)
    o.execute([_call("c1", "_who", {"command": "x", "background": True})])
    o.jobs.wait("j1", 6)
    th = seen.get("thread", "")
    assert th.startswith("orch-parallel"), "后台作业跑到串行池上了：%s" % th
