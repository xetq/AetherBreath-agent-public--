"""
WebUI 运行时 monkey-patch 的契约测试（审计 L3-W4，P0）

背景：agent_webui 的全部能力都建立在**运行期 monkey-patch** 上 —— 这是"零侵入不改
agent/ 源码"这个刻意约束的必然产物（换来 WebUI 可整体删除、agent 可独立跑 CLI）。
代价是：**agent/ 层任何一次重构（改 _run_on_pool 签名、改 create_pipeline、
client 变量改名）都会静默废掉 WebUI 的时间线 / 等待窗口 / 用量统计，且不会有任何
测试变红**。审计 grep 实测：tests/ 与 scripts/ 里没有任何文件引用这些 patch 名。

本文件就是把那道闸补上：每个 patch 装完必须**真的能发事件**，改动一侧就红。

存活清单（`_install_ask_window` 那个补丁已在 2026-09-17 删除 —— ask_user 的等待
时限改走 per-tool 超时声明的正规通道，不再需要 monkey-patch）：
  1. install_orchestrator_watch —— 包 create_pipeline / ToolPipeline.run / _run_on_pool
  2. _wrap_tool                —— 发 tool_begin / tool_end
  3. _install_usage_watch      —— 包 client.chat.completions.create 采集 usage
  4. _inject_keyboard_interrupt —— ctypes 异步异常，停止按钮的唯一通道
"""
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "agent_webui" / "backend"
for _p in (str(ROOT), str(ROOT / "agent"), str(BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REAL_STDOUT = sys.stdout
import bridge                                    # noqa: E402
sys.stdout = _REAL_STDOUT                        # bridge 在 import 期把 stdout 让给了 stderr

from task_orchestrator import TaskOrchestrator, ToolCall, ToolTemplate   # noqa: E402


def _drain(q, timeout=1.0):
    """把事件队列里现有的都取出来。"""
    out = []
    while True:
        try:
            out.append(q.get(timeout=timeout if not out else 0.05))
        except queue.Empty:
            return out


def _orch(tools=None):
    o = TaskOrchestrator(max_workers=2, default_timeout=5,
                         tools_map=tools or {"probe": lambda **kw: "ok"},
                         tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    o._register_signal_handlers = lambda: None
    return o


@pytest.fixture
def run_ctx():
    """活动回合上下文：bridge 的 patch 靠它给事件归属（`current_run()`）。

    先按线程匹配，否则取活动回合指针 —— 所以把 thread_id 设成本测试线程即可。
    """
    ctx = bridge.RunContext("r-probe", "s-probe", "probe")
    ctx.thread_id = threading.get_ident()
    with bridge._RUNS_LOCK:
        bridge._RUNS[ctx.run_id] = ctx
    yield ctx
    with bridge._RUNS_LOCK:
        bridge._RUNS.pop(ctx.run_id, None)


# ============ 1. 编排器监视（时间线的命脉） ============

def test_orchestrator_watch_installs_all_hooks():
    o = _orch()
    try:
        st = bridge.install_orchestrator_watch(o)
        assert st.get("tpl") is True, "ToolTemplate.create_pipeline 没挂上 —— 时间线槽位不会出现"
        assert st.get("pipe") is True, "ToolPipeline.run 没挂上 —— 工具执行事件全丢"
        assert st.get("batch") is True, "_run_on_pool 没挂上 —— 分层信息全丢"
    finally:
        o.shutdown()


def test_orchestrator_watch_actually_emits_pipeline_events():
    """装完必须**真的发出** pipeline 事件（只检查返回值是不够的）。"""
    o = _orch()
    q = bridge.BUS.subscribe()
    try:
        bridge.install_orchestrator_watch(o)
        _drain(q)                                  # 清掉装之前可能的残留
        o.execute([ToolCall(id="tc1", name="probe", arguments={})])
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"]
        assert evts, "执行了一次工具却没收到任何 pipeline 事件 —— patch 已失效"
        states = {e.get("status") for e in evts}
        assert "running" in states, "少了 running 事件: %s" % states
        assert states & {"done", "failed"}, "少了终态事件: %s" % states
        ids = [e.get("tc_id") for e in evts if e.get("tc_id")]
        assert "tc1" in ids, "事件没带上真实的 tool_call.id（字段名是 tc_id）: %s" % ids
    finally:
        bridge.BUS.unsubscribe(q)
        o.shutdown()


def test_orchestrator_watch_is_idempotent():
    """重复安装不许把事件发两遍（bridge 可能多次调用）。"""
    o = _orch()
    q = bridge.BUS.subscribe()
    try:
        bridge.install_orchestrator_watch(o)
        bridge.install_orchestrator_watch(o)
        _drain(q)
        o.execute([ToolCall(id="tc1", name="probe", arguments={})])
        evts = [e for e in _drain(q) if e.get("type") == "pipeline"
                and e.get("status") == "running"]
        assert len(evts) == 1, "同一次调用发了 %d 条 running 事件（重复包装）" % len(evts)
    finally:
        bridge.BUS.unsubscribe(q)
        o.shutdown()


# ============ 2. 工具事件包装 ============

def test_wrap_tool_emits_begin_and_end(run_ctx):
    q = bridge.BUS.subscribe()
    try:
        wrapped = bridge._wrap_tool("probe", lambda **kw: "ok")
        assert wrapped(**{}) == "ok", "包装后返回值必须原样透传"
        evts = _drain(q)
        kinds = [e.get("type") for e in evts]
        assert "tool_begin" in kinds, "没发 tool_begin: %s" % kinds
        assert "tool_end" in kinds, "没发 tool_end: %s" % kinds
    finally:
        bridge.BUS.unsubscribe(q)


def test_wrap_tool_marks_failure_and_forwards_exception(run_ctx):
    """失败要标出来，异常要原样重抛（编排器靠它判 success）。"""
    q = bridge.BUS.subscribe()
    try:
        def boom(**kw):
            raise ValueError("boom")
        wrapped = bridge._wrap_tool("probe", boom)
        with pytest.raises(ValueError):
            wrapped(**{})
        end = [e for e in _drain(q) if e.get("type") == "tool_end"]
        assert end, "异常路径也必须发 tool_end（否则界面上永远挂着 running）"
        assert end[-1].get("ok") is False
    finally:
        bridge.BUS.unsubscribe(q)


def test_wrap_tool_end_carries_elapsed(run_ctx):
    """tool_end 必须带 elapsed —— 前端 TimelinePanel 一直在渲染 elapsed.toFixed(2)s，
    而修复前后端算了却没发（跨层字段漏穿透），界面因此永远不显示工具耗时。"""
    q = bridge.BUS.subscribe()
    try:
        wrapped = bridge._wrap_tool("probe", lambda **kw: "ok")
        wrapped(**{})
        end = [e for e in _drain(q) if e.get("type") == "tool_end"]
        assert end, "没发 tool_end"
        assert "elapsed" in end[-1], "tool_end 少了 elapsed 字段（前端时间线会空着）"
        assert isinstance(end[-1]["elapsed"], (int, float))
    finally:
        bridge.BUS.unsubscribe(q)


# ============ 3. 计量包装 ============

class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _FakeResp:
    def __init__(self):
        self.usage = _FakeUsage()


class _FakeCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return _FakeResp()


class _FakeAgent:
    """最小替身：_install_usage_watch 只碰 client.chat.completions.create。"""

    def __init__(self):
        self.client = type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions()})()})()


def test_usage_watch_wraps_create():
    agent = _FakeAgent()
    assert bridge._install_usage_watch(agent) == "on"
    q = bridge.BUS.subscribe()
    try:
        resp = agent.client.chat.completions.create(model="m", messages=[], stream=False)
        assert isinstance(resp, _FakeResp), "包装后必须原样返回响应对象"
        evts = [e for e in _drain(q) if e.get("type") == "usage"]
        assert evts, "调用后没发 usage 事件 —— 计量面板会一直不动"
    finally:
        bridge.BUS.unsubscribe(q)


def test_usage_watch_is_idempotent():
    """重复安装不重复包装（第二次返回 failed 表示"已经装过了"，不是错误）。"""
    agent = _FakeAgent()
    assert bridge._install_usage_watch(agent) == "on"
    assert bridge._install_usage_watch(agent) == "failed"
    wrapped = agent.client.chat.completions.create
    assert getattr(wrapped, "_ab_webui_usage", False), "包装标记丢了"
    # 再调一次也只能计一次数
    q = bridge.BUS.subscribe()
    try:
        agent.client.chat.completions.create(model="m", messages=[], stream=False)
        evts = [e for e in _drain(q) if e.get("type") == "usage"]
        assert len(evts) == 1, "同一次调用发了 %d 条 usage 事件（重复包装）" % len(evts)
    finally:
        bridge.BUS.unsubscribe(q)


def test_usage_watch_never_breaks_the_call():
    """计量绝不许拖垮回合：采集路径出错也要原样把响应交回去。"""
    agent = _FakeAgent()
    bridge._install_usage_watch(agent)
    resp = agent.client.chat.completions.create(model="m", messages=[], stream=False)
    assert resp is not None


# ============ 4. 停止通道 ============

def test_keyboard_interrupt_injection_reaches_a_live_thread():
    """停止按钮的唯一通道：往回合线程注入 KeyboardInterrupt（agent 靠它保存退出）。

    这里用**纯 Python 忙等**做目标 —— 异常在字节码边界立刻生效。
    C 层阻塞时的延迟行为单独一条用例钉住（见下）。
    """
    got = []

    def _worker():
        try:
            while True:
                pass
        except KeyboardInterrupt:
            got.append("interrupted")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(0.2)
    assert bridge._inject_keyboard_interrupt(t.ident) is True
    t.join(timeout=3)
    assert got == ["interrupted"], "线程没收到 KeyboardInterrupt —— 停止按钮失效"


def test_keyboard_interrupt_is_deferred_while_blocked_in_c():
    """已知边界（审计 L3-W3）：线程阻塞在 C 层（sleep / recv / subprocess.wait）期间，
    注入的异常要等它回到下一个字节码边界才生效。

    这条不是在为缺陷背书，而是把行为钉住：正因为如此，界面上只能说
    「已请求停止（已在跑的工具会自行跑完）」，不能说「已停止」。
    若哪天这条变了（比如换成子进程化 + 真 kill），界面文案要跟着改。
    """
    got = []

    def _worker():
        try:
            time.sleep(1.0)                 # C 层阻塞
        except KeyboardInterrupt:
            got.append("interrupted")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(0.15)
    bridge._inject_keyboard_interrupt(t.ident)
    time.sleep(0.3)
    assert got == [], "C 层阻塞期间不该立刻生效（若这条变了，界面文案要跟着改）"
    t.join(timeout=3)
    assert got == ["interrupted"], "阻塞返回后必须在下一个字节码边界生效"


def test_keyboard_interrupt_injection_on_dead_thread_is_safe():
    """对不存在的线程 id 必须安全返回 False，不许抛。"""
    assert bridge._inject_keyboard_interrupt(999999) is False


# ============ 5. 守护：这些名字必须还在（有人重命名就红） ============

def test_patch_entrypoints_still_exist():
    for name in ("install_orchestrator_watch", "_wrap_tool", "_install_usage_watch",
                 "_inject_keyboard_interrupt", "PATCH_STATE", "ORCH_STATE",
                 "RunContext", "current_run", "_RUNS", "_TLS"):
        assert hasattr(bridge, name), "bridge 的 patch 入口 %s 不见了" % name


def test_agent_layer_targets_still_exist():
    """被 patch 的 agent/ 侧目标必须还在（改名 = patch 静默失效）。"""
    import task_orchestrator as tot
    assert hasattr(tot.ToolTemplate, "create_pipeline")
    assert hasattr(tot.ToolPipeline, "run")
    assert hasattr(tot.TaskOrchestrator, "_run_on_pool")
    assert isinstance(ToolTemplate(name="x", func=lambda: 1), ToolTemplate)
