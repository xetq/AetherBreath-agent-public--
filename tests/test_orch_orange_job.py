# -*- coding: utf-8 -*-
"""编排器橙点契约测试：跨回合后台作业占用的槽位必须**亮得起来、亮得住、说得清**。

真机现象（主人 2026-09-29）：
    「挂一个 30 秒的任务在后台，然后立马向我汇报」→ AB 确实挂上了、也汇报了，
    但**回复后那盏橙灯立马就灭了** —— 30 秒的作业还在跑，界面却当它不存在。

三个断点（每一个都让橙点失真）：

  1. **前端**：`appStore` 的 `done` 分支对**所有**管道调 `idleAll` —— 把跨回合作业的
     "运行中"抹成"空闲"，橙点当场变灰（它活过这个回合，本就不该跟着回合收尾）。
  2. **bridge**：`_execute_turn` 一开始就 `_PIPES.clear()` —— 下一个回合一起来，
     服务端也把那个仍在跑的作业条目抹了（`_PIPES` 是前端的真状态来源）。
  3. **刷新/新连接**：`state.pipes` 只活在浏览器内存里，F5 之后全空 → 回落"推导"模式，
     跑着的作业一格都不亮（黑箱）。

本文件把这三层 + 作业身份（"是哪个作业在跑"）都钉住。
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
import state                                      # noqa: E402
import sse                                        # noqa: E402
import task_orchestrator as T                     # noqa: E402


def _drain(q, timeout=0.6):
    out = []
    while True:
        try:
            out.append(q.get(timeout=timeout if not out else 0.05))
        except queue.Empty:
            return out


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(text: str, start: str, end: str) -> str:
    """取 [start, end) 之间的源码（前后端跨层契约的判据都靠它）。"""
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


@pytest.fixture(autouse=True)
def _clean_pipes():
    with bridge._PIPE_LOCK:
        bridge._PIPES.clear()
        bridge._PIPE_ORDER.clear()
    yield
    with bridge._PIPE_LOCK:
        bridge._PIPES.clear()
        bridge._PIPE_ORDER.clear()


class _FakeOrch:
    """只提供 `_orch_view` 会读的那一个接口（作业登记册）。"""

    def __init__(self, reg):
        self.jobs = reg


def _fake_agent(reg):
    return type("FakeAgent", (), {"_ORCHESTRATOR": _FakeOrch(reg)})


# ============================================================
# 1. bridge：跨回合作业的占用条目必须活过回合边界
# ============================================================

def test_background_pipe_survives_turn_start_clear():
    """回合起点的清场不许连跨回合作业的槽位一起清 —— 清了橙点就中途熄灭。"""
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-bg"] = {"tool": "execute_shell", "status": "running",
                                  "layer": 1, "background": True}
        bridge._PIPES["tc-fg"] = {"tool": "read_file", "status": "running",
                                  "layer": 1, "background": False}
        bridge._PIPE_ORDER.extend(["tc-bg", "tc-fg"])
    kept = bridge._reset_pipes_for_turn()
    with bridge._PIPE_LOCK:
        assert "tc-bg" in bridge._PIPES, "跨回合作业占用的槽位被回合起点清掉了"
        assert bridge._PIPES["tc-bg"]["status"] == "running"
        assert "tc-fg" not in bridge._PIPES, "本回合的工具条目该清还得清"
    assert kept == 1, "返回值要如实报「保留了几个跨回合作业槽位」，界面/日志要用"


def test_pipe_emit_carries_job_identity():
    """橙点必须能说出"是哪个作业在跑"——只有颜色没有身份，还是黑箱。"""
    q = bridge.BUS.subscribe()
    try:
        bridge._pipe_emit("running", "tc1", "execute_shell", layer=2, background=True,
                          job_id="j3", job_label="execute_shell(sleep 30)")
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"]
    finally:
        bridge.BUS.unsubscribe(q)
    assert evts, "没发 pipeline 事件"
    e = evts[-1]
    assert e.get("background") is True
    assert e.get("job_id") == "j3", "事件里没有作业号"
    assert e.get("job_label", "").startswith("execute_shell"), "事件里没有可读名"
    with bridge._PIPE_LOCK:
        assert bridge._PIPES["tc1"].get("job_id") == "j3", \
            "作业身份必须留在占用视图里（刷新对表时要用它）"


def test_background_flag_survives_to_terminal_frame():
    """终态帧也要带 background/作业身份 —— 否则前端那条正在跑的橙色没有来源可清。"""
    q = bridge.BUS.subscribe()
    try:
        bridge._pipe_emit("running", "tc9", "execute_shell", background=True,
                          job_id="j9", job_label="execute_shell(sleep 30)")
        bridge._pipe_emit("done", "tc9", "execute_shell", elapsed=30.2,
                          background=True, job_id="j9", job_label="execute_shell(sleep 30)")
        evts = [e for e in _drain(q) if e.get("type") == "pipeline" and e.get("tc_id") == "tc9"]
    finally:
        bridge.BUS.unsubscribe(q)
    done = [e for e in evts if e.get("status") == "done"]
    assert done, "没收到终态帧"
    assert done[-1].get("background") is True, "终态帧丢了 background，前端认不出这是作业收尾"
    assert done[-1].get("job_id") == "j9"
    with bridge._PIPE_LOCK:
        assert "tc9" not in bridge._PIPES, "结算掉的作业不该继续占着槽位"


def test_execute_background_emits_orange_pipeline_end_to_end():
    """端到端（真编排器 + 真包装器）：`background=true` 的调用必须发得出带作业身份的 running 帧。"""
    o = T.TaskOrchestrator(max_workers=2, default_timeout=5,
                           tools_map={"probe": lambda **kw: "ok"},
                           tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    o._register_signal_handlers = lambda: None
    q = bridge.BUS.subscribe()
    try:
        bridge.install_orchestrator_watch(o)
        _drain(q)
        o.execute([T.ToolCall(id="tcBG", name="probe", arguments={"background": True})])
        time.sleep(0.4)                     # 后台作业不 join，等 worker 把帧发完
        evts = [e for e in _drain(q)
                if e.get("type") == "pipeline" and e.get("tc_id") == "tcBG"]
    finally:
        bridge.BUS.unsubscribe(q)
        o.shutdown()
    run = [e for e in evts if e.get("status") == "running"]
    assert run, "没发 running 帧: %s" % [e.get("status") for e in evts]
    assert run[-1].get("background") is True, "真机跑出来的 running 帧没标成跨回合作业"
    assert run[-1].get("job_id"), "真机跑出来的 running 帧没有作业号"


# ============================================================
# 2. bridge：编排器占用视图（刷新/对表用的权威快照）
# ============================================================

def test_orch_view_lists_live_job_without_pipe(monkeypatch):
    """活着的作业哪怕没有管道条目，也必须在视图里占一格 —— 少一格就是黑箱。"""
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="execute_shell(sleep 30)",
                       sid="s1", tool_call_id="tc-live")
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    view = bridge._orch_view()
    got = {p["tc_id"]: p for p in view["pipes"]}
    assert "tc-live" in got, "活着的作业没出现在编排器视图里"
    assert got["tc-live"]["background"] is True
    assert got["tc-live"]["job_id"] == job.job_id
    assert got["tc-live"]["status"] == "running"
    assert got["tc-live"].get("thread") is None, "没槽位就如实留空，别编一个线程名"


def test_orch_view_merges_job_identity_into_live_pipe(monkeypatch):
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="execute_shell(sleep 30)",
                       sid="s1", tool_call_id="tc1")
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc1"] = {"tool": "execute_shell", "status": "running", "layer": 1,
                                "background": True, "thread": "orch-parallel_3"}
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    got = {p["tc_id"]: p for p in bridge._orch_view()["pipes"]}
    assert got["tc1"]["thread"] == "orch-parallel_3", "槽位身份丢了 —— 橙点不知道点亮哪一格"
    assert got["tc1"]["job_id"] == job.job_id
    assert "elapsed" in got["tc1"], "视图必须给出已跑时长（刷新后界面才能显示「已跑 Ns」）"


def test_orch_view_drops_finished_job(monkeypatch):
    """作业结算了，视图就不该再把它算作"占用中"（否则橙点永远灭不掉）。"""
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="L", sid="s1", tool_call_id="tc-x")
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-x"] = {"tool": "execute_shell", "status": "running", "layer": 1,
                                 "background": True, "thread": "orch-parallel_1"}
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    assert bridge._orch_view()["pipes"], "结算前应当在视图里"
    job.finish(result="OK")
    assert not bridge._orch_view()["pipes"], "结算后仍占着槽位 —— 橙点会一直亮着骗人"


def test_orch_view_keeps_turn_scoped_pipes(monkeypatch):
    """普通（非后台）管道照旧上报：刷新时正在跑的本回合工具也要亮绿。"""
    monkeypatch.setattr(bridge, "_agent", _fake_agent(T.JobRegistry()))
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-fg"] = {"tool": "read_file", "status": "running", "layer": 2,
                                  "background": False, "thread": "orch-serial_0"}
    got = {p["tc_id"]: p for p in bridge._orch_view()["pipes"]}
    assert "tc-fg" in got and got["tc-fg"]["background"] is False


def test_orch_view_is_safe_without_agent(monkeypatch):
    """刚启动/编排器还没建：视图必须是空而不是炸 —— 它是刷新路径上的常驻调用。"""
    monkeypatch.setattr(bridge, "_agent", None)
    view = bridge._orch_view()
    assert view.get("ok") is True and view.get("pipes") == []


def test_bridge_exposes_orch_route():
    src = _read(BACKEND / "bridge.py")
    assert 'if path == "/orch"' in src, "bridge 没有 /orch 路由 —— 刷新后拿不到占用视图"


# ============================================================
# 2.5 杀掉了就得灭灯（2026-09-30 真机：task_kill 后进程停了，橙灯还亮着）
# ============================================================
#
# `task_kill` 会把作业从登记册 **drop** 掉，而那个作业的工具线程是在自己的取消令牌上
# 收尾的（它还会活一会儿）。于是 `_PIPES` 里那条 `background/running` 条目**没人终结**：
#   · `/orch` 视图查不到作业 -> 旧实现"原样上报" -> 前端对表时又把橙灯维持住；
#   · 结果就是"进程确实停了，灯却一直亮"。
# 判据两条：**视图以登记册为准**（查不到就不是占用）、**扫尾要发终态帧**（立刻灭灯）。

def test_killed_job_disappears_from_orch_view(monkeypatch):
    reg = T.JobRegistry()
    job = reg.register(tool="execute_shell", label="execute_shell(sleep 300)",
                       sid="s1", tool_call_id="tc-kill")
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-kill"] = {"tool": "execute_shell", "status": "running",
                                    "layer": 1, "background": True,
                                    "thread": "orch-parallel_4"}
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    assert any(p["tc_id"] == "tc-kill" for p in bridge._orch_view()["pipes"]), "杀之前该在"

    reg.drop(job.job_id)          # ← 这就是 task_kill 干的最后一件事
    assert not any(p["tc_id"] == "tc-kill" for p in bridge._orch_view()["pipes"]), (
        "作业已经从登记册里没了，视图却还报它占用着 —— 界面上那盏橙灯就永远不灭")


def test_orch_view_still_fail_open_without_registry(monkeypatch):
    """登记册**读不到**时保持 fail-open：宁可多亮一格，也不把可能还在跑的作业判成不存在。"""
    monkeypatch.setattr(bridge, "_agent", None)
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-x"] = {"tool": "execute_shell", "status": "running",
                                 "layer": 1, "background": True, "thread": "orch-parallel_5"}
    assert any(p["tc_id"] == "tc-x" for p in bridge._orch_view()["pipes"])


def test_sweep_finishes_dead_background_pipe(monkeypatch):
    """扫尾：作业没了的那条跨回合占用要**发终态帧并出局**，前端才能立刻灭灯。"""
    reg = T.JobRegistry()                     # 空登记册 = 作业已被 kill/drop
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-dead"] = {"tool": "execute_shell", "status": "running",
                                    "layer": 1, "background": True,
                                    "thread": "orch-parallel_6"}
    q = bridge.BUS.subscribe()
    try:
        n = bridge._sweep_dead_background_pipes()
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"
                and e.get("tc_id") == "tc-dead"]
    finally:
        bridge.BUS.unsubscribe(q)
    assert n == 1, "没扫到那条死占用"
    assert evts and evts[-1].get("status") == "cancelled", "没发终态帧：界面无从灭灯"
    assert evts[-1].get("background") is True, "终态帧丢了 background，前端认不出这是作业收尾"
    with bridge._PIPE_LOCK:
        assert "tc-dead" not in bridge._PIPES, "死占用还留在视图里"


def test_sweep_never_touches_live_job(monkeypatch):
    """反向判据：作业还活着就一个都不许动（扫尾不能误杀正在跑的作业）。"""
    reg = T.JobRegistry()
    reg.register(tool="execute_shell", label="L", sid="s1", tool_call_id="tc-live")
    monkeypatch.setattr(bridge, "_agent", _fake_agent(reg))
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-live"] = {"tool": "execute_shell", "status": "running",
                                    "layer": 1, "background": True,
                                    "thread": "orch-parallel_7"}
    assert bridge._sweep_dead_background_pipes() == 0
    with bridge._PIPE_LOCK:
        assert "tc-live" in bridge._PIPES


def test_sweep_keeps_turn_scoped_pipes(monkeypatch):
    """本回合的普通管道（background=False）不归它管 —— 它们由回合边界清理。"""
    monkeypatch.setattr(bridge, "_agent", _fake_agent(T.JobRegistry()))
    with bridge._PIPE_LOCK:
        bridge._PIPES["tc-fg"] = {"tool": "read_file", "status": "running",
                                  "layer": 1, "background": False,
                                  "thread": "orch-serial_0"}
    assert bridge._sweep_dead_background_pipes() == 0
    with bridge._PIPE_LOCK:
        assert "tc-fg" in bridge._PIPES


def test_sweep_is_wired_after_every_tool_run():
    """扫尾必须**接在工具执行之后**：task_kill 一返回就要能灭灯，而不是等 5 秒对表。"""
    src = _read(BACKEND / "bridge.py")
    seg = src[src.index("def run(self):"):src.index("pipe.run = run")]
    assert "_sweep_dead_background_pipes" in seg, "pipe.run 收尾没接扫尾 —— 灯要等对表才灭"


def test_gateway_registers_orch_route():
    """网关路由：GET/POST 都收（方法写错就整条通道失效，/approval/pending 上踩过）。"""
    src = _read(BACKEND / "api.py")
    seg = _slice(src, '@router.api_route("/orch"', "def api_orch")
    assert "GET" in seg and "POST" in seg, "只收一种方法 —— 前端换个写法就整条通道失效"
    body = _slice(src, "def api_orch", '@router')
    assert "authoritative" in body, "网关没把 authoritative 透传给前端"
    assert "_mgr().orch()" in body, "网关没有真的去问 bridge"


# ============================================================
# 3. 网关：把 bridge 的占用视图转给前端（authoritative 纪律与卡片恢复同源）
# ============================================================

def test_gateway_orch_is_not_authoritative_when_bridge_down():
    mgr = agent_proc.AgentManager(sse.SSEHub(), state.StateMachine())
    mgr._client = None
    r = mgr.orch()
    assert r["ok"] is True and r["pipes"] == []
    assert r["authoritative"] is False, "没问到 ≠ 没有作业 —— 前端不许据此熄灭橙点"


def test_gateway_orch_passes_through_when_bridge_ok():
    mgr = agent_proc.AgentManager(sse.SSEHub(), state.StateMachine())

    class _C:
        def orch(self):
            return {"ok": True, "pipes": [{"tc_id": "tc1", "background": True}]}

    mgr._client = _C()
    mgr.sm.set_phase("ON")
    r = mgr.orch()
    assert r["authoritative"] is True and r["pipes"][0]["tc_id"] == "tc1"


# ============================================================
# 4. 前端：三处跨层契约（字段穿透的老坑）
# ============================================================

def test_store_keeps_live_background_pipe_on_turn_end():
    store = _read(SRC / "store" / "appStore.tsx")
    idle = _slice(store, "function idleAll", "function onEvent")
    assert "background" in idle, (
        "idleAll 不认识跨回合作业 —— done 一到，跑着的作业槽位就被抹成空闲（橙点熄灭的真凶）")
    assert "running" in idle and "pending" in idle, "只在运行/排队中的作业才该被豁免"


def test_store_picks_up_job_identity_fields():
    store = _read(SRC / "store" / "appStore.tsx")
    pipes = _slice(store, "case 'pipeline'", "case 'usage'")
    for f in ("background", "job_id", "job_label", "elapsed"):
        assert f in pipes, (
            "store 处理 pipeline 事件时是**显式挑字段**的：漏了 %s 就在这一层被丢掉" % f)


def test_store_has_authoritative_merge_action():
    store = _read(SRC / "store" / "appStore.tsx")
    assert "merge_orch" in store, "store 没有接收编排器视图的动作，刷新后无法恢复橙点"
    seg = _slice(store, "case 'merge_orch'", "case 'event':")
    assert "authoritative" in seg, "对表结果不权威时不许改本地状态（会把真在跑的作业抹掉）"


def test_types_declare_pipe_job_fields():
    types_ = _read(SRC / "types.ts")
    pv = _slice(types_, "export interface PipeView", "export interface PoolHello")
    for f in ("job_id", "job_label"):
        assert f in pv, "PipeView 缺 %s —— tsc 编译不过或字段被静默丢弃" % f
    assert "OrchView" in types_ or "OrchPipe" in types_, "缺编排器视图的类型声明"


def test_api_ts_exposes_orch_endpoint():
    api = _read(SRC / "api.ts")
    assert "'/orch'" in api, "api.ts 没有 /orch 调用 —— 刷新后拿不到占用视图"


def test_app_hydrates_orch_on_load_and_reconnect():
    app = _read(SRC / "App.tsx")
    assert "api.orch()" in app, "App 没有在装载/重连时对表编排器占用"
    assert app.count("syncOrch") >= 3, "syncOrch 必须被定义、在重连分支调用、并被定时对表复用"
    assert "setInterval" in app, "没有对表定时器：丢一帧橙点就永远卡住/永远亮着"


def test_orchstrip_shows_job_identity_and_live_elapsed():
    strip = _read(SRC / "components" / "OrchStrip.tsx")
    assert "job_id" in strip or "jobId" in strip, "橙点的 hover 里没有作业号（说不清是哪个作业）"
    assert "job_label" in strip or "jobLabel" in strip, "橙点的 hover 里没有可读名"
    assert "已跑" in strip, "橙点没有「已跑多久」 —— 长作业看起来仍像黑箱"
    assert "setInterval" in strip or "nowS" in strip, "没有本地秒针：已跑时长不会动"
