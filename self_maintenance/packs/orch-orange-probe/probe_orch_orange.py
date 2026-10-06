# -*- coding: utf-8 -*-
"""探针（编排器橙点 / 后端侧）：占用条目活过回合、事件带作业身份、/orch 权威视图。

用法（先在工作台副本上试，再动生产）：
    # 未打补丁的生产
    venv/Scripts/python.exe probe_orch_orange.py
    # 打过补丁的副本（其余模块仍从生产 backend 解析）
    PYTHONPATH=<根>/agent;<根>/agent_webui/backend \
      venv/Scripts/python.exe probe_orch_orange.py <副本的 agent_webui/backend 目录>

判据（全绿 = 橙点的三条后端底座都成立）：
  A 回合起点的清场保留跨回合作业的占用条目
  B pipeline 事件与占用条目都带 作业号/可读名
  C 终态帧也带 background + 作业身份（前端才收得掉那格橙色）
  D /orch 视图：活着的作业必占一格；已结算的不算占用；没有 agent 也不炸
  E 网关 orch()：bridge 不在时如实标 non-authoritative（不许"看不见=不存在"）
"""
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "agent_webui").is_dir())
BACKEND = ROOT / "agent_webui" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
for _extra in reversed([a for a in sys.argv[1:] if not a.startswith("-")]):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

_REAL_STDOUT = sys.stdout
import bridge                       # noqa: E402
sys.stdout = _REAL_STDOUT
import agent_proc                   # noqa: E402
import sse                          # noqa: E402
import state                        # noqa: E402
import task_orchestrator as T       # noqa: E402

RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((True, name, ""))
    except Exception as e:
        RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))


def _drain(q):
    out = []
    while True:
        try:
            out.append(q.get(timeout=0.4 if not out else 0.05))
        except Exception:
            return out


def _clean():
    with bridge._PIPE_LOCK:
        bridge._PIPES.clear()
        bridge._PIPE_ORDER.clear()


class _FakeOrch:
    def __init__(self, reg):
        self.jobs = reg


def _fake_agent(reg):
    return type("FakeAgent", (), {"_ORCHESTRATOR": _FakeOrch(reg)})


# ---- A -------------------------------------------------------------------
def a_keep_on_turn_reset():
    _clean()
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-bg"] = {"tool": "execute_shell", "status": "running",
                                  "layer": 1, "background": True}
        bridge._PIPES["tc-fg"] = {"tool": "read_file", "status": "running",
                                  "layer": 1, "background": False}
        bridge._PIPE_ORDER.extend(["tc-bg", "tc-fg"])
    kept = bridge._reset_pipes_for_turn()
    assert kept == 1, "保留数=%s（应为 1）" % kept
    assert "tc-bg" in bridge._PIPES, "跨回合作业的占用条目被回合起点清掉了"
    assert "tc-fg" not in bridge._PIPES, "本回合的条目该清没清"
    _clean()


# ---- B -------------------------------------------------------------------
def b_event_carries_identity():
    _clean()
    q = bridge.BUS.subscribe()
    try:
        bridge._pipe_emit("running", "tc1", "execute_shell", layer=2, background=True,
                          job_id="j3", job_label="execute_shell(sleep 30)")
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"]
    finally:
        bridge.BUS.unsubscribe(q)
    assert evts, "没发 pipeline 事件"
    assert evts[-1].get("background") is True, "事件没标 background"
    assert evts[-1].get("job_id") == "j3", "事件没带作业号"
    assert evts[-1].get("job_label"), "事件没带可读名"
    with bridge._PIPE_LOCK:
        assert bridge._PIPES["tc1"].get("job_id") == "j3", "占用条目没留作业身份"
    _clean()


# ---- C -------------------------------------------------------------------
def c_terminal_keeps_flag():
    _clean()
    q = bridge.BUS.subscribe()
    try:
        bridge._pipe_emit("running", "tc9", "execute_shell", background=True,
                          job_id="j9", job_label="L9")
        bridge._pipe_emit("done", "tc9", "execute_shell", elapsed=30.2,
                          background=True, job_id="j9", job_label="L9")
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"]
    finally:
        bridge.BUS.unsubscribe(q)
    done = [e for e in evts if e.get("status") == "done"]
    assert done, "没收到终态帧"
    assert done[-1].get("background") is True, "终态帧丢了 background"
    assert done[-1].get("job_id") == "j9", "终态帧丢了作业号"
    with bridge._PIPE_LOCK:
        assert "tc9" not in bridge._PIPES, "结算掉的作业还占着槽位"
    _clean()


# ---- D -------------------------------------------------------------------
def d_view_lists_live_job_without_pipe():
    _clean()
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="execute_shell(sleep 30)",
                       sid="s1", tool_call_id="tc-live")
    old = bridge._agent
    bridge._agent = _fake_agent(reg)
    try:
        got = {p["tc_id"]: p for p in bridge._orch_view()["pipes"]}
    finally:
        bridge._agent = old
    assert "tc-live" in got, "活着的作业没出现在视图里"
    assert got["tc-live"]["background"] is True
    assert got["tc-live"]["job_id"] == job.job_id
    assert got["tc-live"].get("thread") is None, "没槽位却编了个线程名"


def d_view_merges_and_drops():
    _clean()
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="L", sid="s1", tool_call_id="tc1")
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc1"] = {"tool": "execute_shell", "status": "running", "layer": 1,
                                "background": True, "thread": "orch-parallel_3"}
    old = bridge._agent
    bridge._agent = _fake_agent(reg)
    try:
        got = {p["tc_id"]: p for p in bridge._orch_view()["pipes"]}
        assert got["tc1"]["thread"] == "orch-parallel_3", "槽位身份丢了"
        assert got["tc1"]["job_id"] == job.job_id, "身份没并进去"
        assert "elapsed" in got["tc1"], "没给已跑时长"
        job.finish(result="OK")
        assert not bridge._orch_view()["pipes"], "结算后仍算占用 -> 橙点永远灭不掉"
        bridge._agent = None
        v = bridge._orch_view()
        # 没有 agent（拿不到登记册）时**故意 fail-open**：占用条目本身是"它曾经在跑"的证据，
        # 宁可多亮一格，也不能把可能还在跑的作业判成不存在（那正是黑箱）。
        assert v.get("ok") is True and isinstance(v.get("pipes"), list), \
            "没有 agent 时视图炸了：%r" % (v,)
    finally:
        bridge._agent = old
    _clean()


def d_route_exists():
    src = Path(bridge.__file__).read_text(encoding="utf-8")   # 读**当前加载的那份**（副本/生产）
    assert 'if path == "/orch"' in src, "bridge 没有 /orch 路由"


def d_pipe_turn_reset_hooked():
    src = Path(bridge.__file__).read_text(encoding="utf-8")
    assert "_reset_pipes_for_turn()" in src, "回合起点没接上保留逻辑"


# ---- E -------------------------------------------------------------------
def e_gateway_orch_discipline():
    mgr = agent_proc.AgentManager(sse.SSEHub(), state.StateMachine())
    mgr._client = None
    r = mgr.orch()
    assert r["ok"] is True and r["pipes"] == []
    assert r["authoritative"] is False, "bridge 不在时竟标了权威 —— 会把橙点灭掉"

    class _C:
        def orch(self):
            return {"ok": True, "pipes": [{"tc_id": "tc1", "background": True}]}

    mgr._client = _C()
    mgr.sm.set_phase("ON")
    r2 = mgr.orch()
    assert r2["authoritative"] is True and r2["pipes"][0]["tc_id"] == "tc1", "透传坏了"


def main():
    check("A 回合起点保留跨回合作业", a_keep_on_turn_reset)
    check("B 事件与条目带作业身份", b_event_carries_identity)
    check("C 终态帧保留 background/身份", c_terminal_keeps_flag)
    check("D /orch 视图补齐无槽位的活作业", d_view_lists_live_job_without_pipe)
    check("D /orch 视图合并身份 + 结算即出局 + 无 agent 不炸", d_view_merges_and_drops)
    check("D bridge 有 /orch 路由", d_route_exists)
    check("D _execute_turn 接上保留逻辑", d_pipe_turn_reset_hooked)
    check("E 网关 orch() 的 authoritative 纪律", e_gateway_orch_discipline)

    print("bridge: %s" % bridge.__file__)
    ok = 0
    for good, name, why in RESULTS:
        print("  %s %s%s" % ("[PASS]" if good else "[FAIL]", name,
                             "" if good else "  <- " + why))
        ok += 1 if good else 0
    print("\n探针结论：%s (%d/%d)" % ("通过" if ok == len(RESULTS) else "未通过",
                                      ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
