# -*- coding: utf-8 -*-
"""后台作业「迟到帧」契约测试（2026-09-29 真机事故 · P4）

现象（主人原话）：
    新会话里让 AB「挂一个 30 秒的任务在后台，然后立马向我汇报」→ 汇报完回合正常收口，
    但约 30 秒后界面**自己**变回「回合进行中」的交互形态（草稿变成「中期交互」），
    点「停止本回合」却被告知**没有活动的回合**。

根因链（每一环都有代码出处，已用 workbench 探针离线复现）：
  1. 后台作业跑在编排器的 worker 线程里，会在**回合收口之后**才收尾；
  2. `bridge._wrap_tool` 收尾时仍 `_set_turn(ctx, "THINKING")` —— 这一帧带着
     **已经结束的** run_id 出去；
  3. 网关 `agent_proc._forward` 对 turn_phase **无条件** `sm.set_turn` →
     忙碌态被重新点亮；而 `/agent/status` 读的正是这个状态机，
     所以前端看门狗（每 10 秒 syncStatus）刷到的也一直是"忙" —— **永远救不回来**，
     与"停止说没有活动回合"完全吻合。

三层各自钉一条契约：网关的**归属判据**、bridge 的**断源**、前端的**权威字段**。
"""
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "agent_webui" / "backend"
SRC = ROOT / "agent_webui" / "frontend" / "src"
for _p in (str(ROOT), str(ROOT / "agent"), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REAL_STDOUT = sys.stdout
import bridge                                     # noqa: E402
sys.stdout = _REAL_STDOUT                         # bridge 在 import 期把 stdout 让给了 stderr

import agent_proc                                 # noqa: E402
import events                                     # noqa: E402
import sse                                        # noqa: E402
import state                                      # noqa: E402


# ============================================================
# 夹具：一个"已开机"的网关
# ============================================================

class _FakeClient:
    """假 bridge 客户端：只提供 _forward/stop/status 这三条被测路径要用的接口。"""

    def __init__(self, health=None, stop_result=None):
        self._health = health or {"ok": True, "phase": "IDLE"}
        self._stop = stop_result or {"ok": True, "stopped": False, "reason": "no-active-run"}
        self.health_calls = 0

    def health(self, timeout: float = 8):        # noqa: ARG002 - 只验证可短超时
        self.health_calls += 1
        return self._health

    def stop(self, run_id=None):                 # noqa: ARG002
        return self._stop


@pytest.fixture
def gw():
    hub = sse.SSEHub()
    sm = state.StateMachine()
    mgr = agent_proc.AgentManager(hub, sm)
    sm.set_phase(events.PHASE_ON)
    sm.set_session("s-late")
    yield hub, sm, mgr


def _frames(hub, since_seq=0):
    return [e for e in hub.history_snapshot(200) if e.get("hub_seq", 0) > since_seq]


def _fwd(mgr, etype, run_id="r1", **kw):
    payload = {"type": etype, "run_id": run_id, "session_id": "s-late"}
    payload.update(kw)
    mgr._forward(payload)


# ============================================================
# 1. 网关：迟到帧不许改写回合状态机
# ============================================================

def test_late_turn_phase_after_done_does_not_rearm_busy(gw):
    """核心回归：回合收口后，属于该回合的迟到 turn_phase 不得把 busy 置回 True。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    assert sm.snapshot()["busy"] is True, "回合里工具开跑，网关本该是忙的"
    _fwd(mgr, "done", content="已挂上 j1")
    assert sm.snapshot()["busy"] is False, "done 之后应当回到空闲"

    # 30 秒后：后台作业在 worker 线程里收尾，bridge 发来的迟到帧
    _fwd(mgr, "turn_phase", phase="THINKING")
    snap = sm.snapshot()
    assert snap["busy"] is False, (
        "迟到帧把网关又推回忙了 —— 前端会自己变回「回合进行中」，"
        "而 /chat/stop 只能说 no-active-run（2026-09-29 真机事故）")
    assert snap["turn_phase"] == "IDLE"


def test_late_frame_is_published_with_authoritative_busy(gw):
    """迟到的帧仍要转发（时间线要收尾），但**必须**带上服务端权威的 busy=False。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    _fwd(mgr, "done")
    mark = hub.seq
    _fwd(mgr, "tool_end", tool="execute_shell", call_id="c1", ok=True, elapsed=30.2)
    late = _frames(hub, mark)
    assert late, "tool_end 必须照常转发（否则时间线上的工具永远停在 running）"
    for e in late:
        assert e.get("busy") is False, "迟到帧带着 busy=True 出去了：前端会照它置忙 %s" % e


def test_unknown_run_still_drives_state_machine(gw):
    """放行面：网关不认识这个 run_id（网关重启 / 外部起回合）时仍按老规矩驱动状态机。

    这条是防"修过头"——把归属判据做成"run_id 没见过就一律不信"，
    网关重启后的整个忙碌链路会静默失效（比卡忙更糟）。
    """
    _hub, sm, mgr = gw
    assert sm.snapshot()["run_id"] is None
    _fwd(mgr, "tool_begin", run_id="r-unknown", tool="read_file", call_id="c9")
    assert sm.snapshot()["busy"] is True, "网关不知道 run_id 时不该拒绝置忙"


def test_frame_from_another_live_run_does_not_touch_current_turn(gw):
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    _fwd(mgr, "turn_phase", run_id="r2", phase="IDLE")     # 别的回合的帧
    assert sm.snapshot()["busy"] is True, "另一个回合的帧不该改本回合的状态"


def test_done_of_old_run_does_not_clear_new_run(gw):
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    _fwd(mgr, "done")
    sm.set_run("r2")                                        # 新回合开始
    _fwd(mgr, "tool_begin", run_id="r2", tool="read_file", call_id="c2")
    assert sm.snapshot()["busy"] is True
    _fwd(mgr, "done", run_id="r1")                          # r1 的迟到 done
    assert sm.snapshot()["busy"] is True, "旧回合的 done 把新回合的清空了"


def test_stop_with_no_active_run_clears_stuck_busy(gw):
    """对症止血：bridge 说"没有活动回合"时，网关必须就地校正 —— 
    否则前端那句「界面状态已校正」是假话（它刷到的状态仍然是忙）。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    assert sm.snapshot()["busy"] is True
    mark = hub.seq
    mgr._client = _FakeClient(stop_result={"ok": True, "stopped": False,
                                           "reason": "no-active-run"})
    res = mgr.stop_run(None)
    assert res.get("reason") == "no-active-run"
    assert sm.snapshot()["busy"] is False, "no-active-run 之后网关还是忙的 —— 前端校正不动"
    assert any(e.get("type") == "turn_phase" and e.get("busy") is False
               for e in _frames(hub, mark)), "校正之后必须广播一帧，前端才会跟着回到空闲"


def test_status_reconciles_stuck_busy_with_bridge(gw):
    """自愈兜底：忙了 >=5 秒而 bridge 说它 IDLE -> status 就地校正（前端看门狗刷的就是它）。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    assert sm.snapshot()["busy"] is True
    mgr._client = _FakeClient(health={"ok": True, "phase": "IDLE"})
    mgr._turn_armed_at -= 10                                # 假装已经忙了 10 秒
    st = mgr.status()
    assert st["busy"] is False, "bridge 已无活动回合，status 必须把忙碌态校正掉"
    assert mgr._client.health_calls == 1


def test_status_does_not_reconcile_while_bridge_is_running(gw):
    """反向用例：bridge 真在跑就不许校正（否则会把正在跑的回合判成空闲）。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    mgr._client = _FakeClient(health={"ok": True, "phase": "RUNNING", "run_id": "r1"})
    mgr._turn_armed_at -= 10
    assert mgr.status()["busy"] is True


def test_status_skips_reconcile_when_just_armed(gw):
    """反向用例：刚置忙的几毫秒不核对 —— done 正在路上，别抢跑成假空闲。"""
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    mgr._client = _FakeClient(health={"ok": True, "phase": "IDLE"})
    assert mgr.status()["busy"] is True
    assert mgr._client.health_calls == 0, "刚置忙就去问 bridge = 白问一轮"


def test_busy_frames_do_not_defer_self_heal(gw):
    """置忙时刻只在"不忙 -> 忙"的**跃迁**上打点。

    若每帧都刷新它，一个卡住的忙碌态只要有帧在流（后台作业的 progress 也算），
    status 的自愈核对就会被无限期推迟 —— 那正是要修的场景。
    """
    hub, sm, mgr = gw
    sm.set_run("r1")
    _fwd(mgr, "tool_begin", tool="execute_shell", call_id="c1")
    armed = mgr._turn_armed_at
    assert armed > 0
    time.sleep(0.01)
    _fwd(mgr, "progress", text="还在跑", round=0)
    _fwd(mgr, "turn_phase", phase="THINKING")
    assert mgr._turn_armed_at == armed, "忙碌中的帧刷新了置忙时刻 —— 自愈会被无限推迟"
    assert sm.snapshot()["busy"] is True, "忙态本身不该被这些帧改掉"


# ============================================================
# 2. bridge：回合收口之后，它的工具不许再发任何 turn_phase（断源）
# ============================================================

def _drain(q, timeout=0.4):
    out = []
    while True:
        try:
            out.append(q.get(timeout=timeout if not out else 0.05))
        except queue.Empty:
            return out


def _run_wrapped(finished: bool):
    ctx = bridge.RunContext("r-late", "s-late", "probe")
    ctx.thread_id = threading.get_ident()
    ctx.finished = finished
    with bridge._RUNS_LOCK:
        bridge._RUNS[ctx.run_id] = ctx
    q = bridge.BUS.subscribe()
    try:
        bridge._wrap_tool("probe", lambda **kw: "ok")()
        return _drain(q)
    finally:
        bridge.BUS.unsubscribe(q)
        with bridge._RUNS_LOCK:
            bridge._RUNS.pop(ctx.run_id, None)
        bridge._set_current(None)


def test_bridge_emits_turn_phase_while_run_is_alive():
    """正常路径的保护：回合还在跑时照旧发 TOOL_RUNNING / THINKING。"""
    phases = [e.get("phase") for e in _run_wrapped(False) if e.get("type") == "turn_phase"]
    assert phases == ["TOOL_RUNNING", "THINKING"], phases


def test_bridge_stops_emitting_turn_phase_after_run_finished():
    """断源：已经收口的回合，其工具收尾不许再发 turn_phase（那就是把网关推回忙的帧）。"""
    evts = _run_wrapped(True)
    phases = [e.get("phase") for e in evts if e.get("type") == "turn_phase"]
    assert phases == [], "回合已收口却仍发了 turn_phase: %s" % phases
    kinds = [e.get("type") for e in evts]
    assert "tool_end" in kinds, "tool_end 不能一起丢 —— 时间线靠它收尾: %s" % kinds


def test_execute_turn_marks_context_finished():
    """收口标记必须真的被写：写在 _execute_turn 的 finally 里（源码级契约）。"""
    src = (BACKEND / "bridge.py").read_text(encoding="utf-8")
    assert "ctx.finished = True" in src, "RunContext.finished 没人置位 = 断源失效"
    assert "not getattr(ctx, \"finished\", False)" in src, "_wrap_tool 没查 finished"


# ============================================================
# 3. 前端：busy 认服务端权威，迟到帧骗不过本地推断
# ============================================================

def test_frontend_tool_begin_reads_server_busy():
    store = (SRC / "store" / "appStore.tsx").read_text(encoding="utf-8")
    assert "evt.busy ?? true" in store, (
        "tool_begin 必须读服务端 busy —— 靠\"收到 tool_begin 就置忙\"的本地推断，"
        "后台作业的迟到帧就能把界面骗回忙态")
    assert "evt.busy ?? base.agent.busy" in store or "stillBusy" in store, \
        "tool_end 必须只在仍忙时改 turn_phase"


def test_gateway_publishes_authoritative_busy_on_every_frame():
    """跨层契约：网关每一帧都带 busy/can_send（前端就是靠它不靠本地推断）。"""
    src = (BACKEND / "agent_proc.py").read_text(encoding="utf-8")
    assert 'payload["busy"] = snap["busy"]' in src, "网关没在统一收尾里写权威 busy"
    assert 'payload["can_send"] = snap["can_send"]' in src, "网关没在统一收尾里写 can_send"


def test_ws_event_type_has_busy_field():
    types_ = (ROOT / "agent_webui" / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")
    assert "busy?: boolean" in types_, "WsEvent 必须声明 busy（否则 tsc 前端读它编译不过）"
