"""
per-tool 超时（timeout 单源）回归测试

背景（生产就绪度审计 L1-B2 / L2-T2,T3 / L3-W1）：
    修复前 `ToolCall` 没有 timeout 字段、`ToolTemplate.timeout` 全项目零读取、
    `agent.py` 也从不传 timeout → **所有工具恒为 30 秒**。后果：
      · execute_shell/python 的 schema 承诺「可 clamp 到 120」是假承诺
      · execute_browser（schema 声明默认 180）被 30 秒机制性杀死
      · rag 冷启动（17~32s）随机撞墙，表现为"有时超时有时刚好通过"
      · WebUI 只能靠 monkey-patch 抬高 ask 窗口（已随本次修复删除）

本文件锁死修复后的契约：
    1. 时限取值优先级：工具入参 timeout > 模板/声明表 > 全局默认
    2. 只有"声明过的"时限加 ORCH_GRACE 缓冲（让工具先到自己的点并返回它自己的
       结果，编排器只兜底）——未声明的快工具行为一字不变（仍是 default_timeout）
    3. 声明表是**运行时可变引用**：编排器构造之后追加的键必须立刻生效
       （WebUI bridge 就是这样注入 ask_user 的）
    4. 真的按各自的时限结算，且超时文案报的是实际用的秒数
"""
import sys
import time
from pathlib import Path

import pytest

# 确保 agent 目录可导入（与其它测试同款做法）
AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))

import task_orchestrator as orch_mod  # noqa: E402
from task_orchestrator import ToolCall, TaskOrchestrator  # noqa: E402


def _sleep_tool(seconds: float, result: str = "done"):
    """造一个'跑 seconds 秒'的工具函数（不碰真实 IO）。"""
    def _f(**kwargs):
        time.sleep(seconds)
        return result
    return _f


def _make_orch(tools, timeouts=None, default=30, workers=4):
    """造一个测试用编排器：屏蔽信号注册（测试进程不该被改信号），用完必须 shutdown。"""
    o = TaskOrchestrator(max_workers=workers, default_timeout=default,
                         tools_map=tools, tool_timeouts=timeouts, log_enabled=False)
    o._register_signal_handlers = lambda: None
    return o


@pytest.fixture
def no_grace(monkeypatch):
    """把缓冲压成 0，让行为断言不必真等 10 秒。数值断言另开测试。"""
    monkeypatch.setattr(orch_mod, "ORCH_GRACE", 0)


# ---------------- 1. 优先级 ----------------

def test_caller_argument_wins_over_declaration(no_grace):
    """模型在 schema 里显式传的 timeout 优先于工具声明。"""
    o = _make_orch({"t": _sleep_tool(0)}, {"t": 50})
    try:
        assert o._tool_timeout(ToolCall(id="1", name="t", arguments={"timeout": 5})) == 5
    finally:
        o.shutdown()


def test_declaration_used_when_no_argument(no_grace):
    """模型没传 → 用工具声明值（不是全局默认）。"""
    o = _make_orch({"t": _sleep_tool(0)}, {"t": 90})
    try:
        assert o._tool_timeout(ToolCall(id="1", name="t", arguments={})) == 90
    finally:
        o.shutdown()


def test_template_timeout_beats_declaration_table(no_grace):
    """用 ToolTemplate 注册时，模板自带 timeout 优先于声明表。"""
    from task_orchestrator import ToolTemplate
    o = _make_orch({"t": ToolTemplate(name="t", func=_sleep_tool(0), timeout=77)},
                   {"t": 50})
    try:
        assert o._tool_timeout(ToolCall(id="1", name="t", arguments={})) == 77
    finally:
        o.shutdown()


def test_undeclared_tool_falls_back_to_default_without_grace(no_grace):
    """没声明的快工具：行为与修复前一致（default_timeout，且不加缓冲）。"""
    o = _make_orch({"fast": _sleep_tool(0)}, {"other": 90}, default=30)
    try:
        assert o._tool_timeout(ToolCall(id="1", name="fast", arguments={})) == 30
    finally:
        o.shutdown()


def test_bad_timeout_argument_ignored(no_grace):
    """模型传了非数字 / 负数 timeout → 忽略，落回声明值，不炸。"""
    o = _make_orch({"t": _sleep_tool(0)}, {"t": 45})
    try:
        for bad in ({"timeout": "abc"}, {"timeout": None}, {"timeout": 0}, {"timeout": -3}):
            assert o._tool_timeout(ToolCall(id="1", name="t", arguments=bad)) == 45
    finally:
        o.shutdown()


def test_declared_timeout_gets_grace_buffer(monkeypatch):
    """声明过的时限额外加 ORCH_GRACE（让工具先到点，编排器只兜底）。"""
    monkeypatch.setattr(orch_mod, "ORCH_GRACE", 10)
    o = _make_orch({"t": _sleep_tool(0)}, {"t": 120})
    try:
        assert o._tool_timeout(ToolCall(id="1", name="t", arguments={})) == 130
        # 未声明的不加
        assert o._tool_timeout(ToolCall(id="2", name="fast", arguments={})) == 30
    finally:
        o.shutdown()


# ---------------- 2. 运行时可变引用（WebUI 注入路径） ----------------

def test_timeout_table_is_live_reference(no_grace):
    """编排器构造**之后**往表里追加的键必须立刻生效。

    这是 WebUI 的关键路径：bridge 在启动期注入 ask_user 时才把它写进
    agent_tools.TOOL_TIMEOUTS，而编排器是更早（主线程）就建好的单例。
    若编排器在构造时对表做快照，ask_user 就会退回 30 秒默认值，
    主人晚到的答复会被投给一个已被结算的等待者（静默吞掉）。
    """
    table = {}
    o = _make_orch({"late": _sleep_tool(0)}, table)
    try:
        assert o._tool_timeout(ToolCall(id="1", name="late", arguments={})) == 30
        table["late"] = 100                  # 构造之后才追加（模拟 bridge 注入 ask_user）
        assert o._tool_timeout(ToolCall(id="2", name="late", arguments={})) == 100
    finally:
        o.shutdown()


# ---------------- 3. 真实行为：逐任务结算 ----------------

def test_declared_long_tool_is_not_killed_by_default_30s(no_grace):
    """声明 3 秒的工具跑 1 秒：必须成功。

    修复前它会在 30 秒默认值下同样成功 —— 该用例真正锁的是
    "声明值确实被当成该工具的时限来判定"（配合下面两个用例看反例）。
    """
    o = _make_orch({"slow": _sleep_tool(1.0)}, {"slow": 3})
    try:
        res = o.execute([ToolCall(id="a", name="slow", arguments={})])
        assert res.success_count == 1, res.results[0].error
        assert res.results[0].success is True
    finally:
        o.shutdown()


def test_declared_tool_is_killed_at_its_own_limit(no_grace):
    """声明 1 秒的工具跑 3 秒：1 秒到点即判超时，且文案报 1 秒（不是 30）。"""
    o = _make_orch({"slow": _sleep_tool(3.0)}, {"slow": 1})
    try:
        t0 = time.time()
        res = o.execute([ToolCall(id="a", name="slow", arguments={})])
        elapsed = time.time() - t0
        # 2026-09-29 语义变更（主人决策 2）：到点后**转入后台作业**，不再判失败。
        # 本用例真正锁的"时限归属"没变：用的还是它自己的 1 秒，不是全局 30 秒。
        assert res.results[0].success is True
        assert "已自动转入后台" in str(res.results[0].result), res.results[0].result
        assert "（1 秒）" in str(res.results[0].result), res.results[0].result
        assert elapsed < 2.5, f"应在自己的时限到点，而不是等默认 30 秒（实测 {elapsed:.1f}s）"
    finally:
        o.shutdown()


def test_each_task_uses_its_own_limit(no_grace):
    """同一层里：短任务按自己的时限到点，长任务拿到自己的宽限 —— 互不牵连。"""
    o = _make_orch({"quick": _sleep_tool(3.0), "patient": _sleep_tool(1.0)},
                   {"quick": 1, "patient": 5})
    try:
        res = o.execute([
            ToolCall(id="q", name="quick", arguments={}),
            ToolCall(id="p", name="patient", arguments={}),
        ])
        by_id = {r.tool_call_id: r for r in res.results}
        assert by_id["q"].success is True, "短任务在自己的 1 秒时限上结算（转后台）"
        assert "已自动转入后台" in str(by_id["q"].result), by_id["q"].result
        assert "（1 秒）" in str(by_id["q"].result), by_id["q"].result
        assert by_id["p"].success is True, "长任务不该被旁边的短任务拖死"
    finally:
        o.shutdown()


def test_caller_timeout_argument_reaches_the_orchestrator(no_grace):
    """模型传 timeout 也能改变编排器判定（per-tool 超时对参数透传生效）。"""
    o = _make_orch({"t": _sleep_tool(3.0)}, None, default=30)
    try:
        res = o.execute([ToolCall(id="a", name="t", arguments={"timeout": 1})])
        assert res.results[0].success is True
        assert "已自动转入后台" in str(res.results[0].result), res.results[0].result
        assert "（1 秒）" in str(res.results[0].result), res.results[0].result
    finally:
        o.shutdown()
