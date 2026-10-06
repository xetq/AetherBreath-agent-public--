"""
幽灵执行（ghost execution）止损测试

审计 L1-B1 实测确证的机制：工具被判超时后，`future.cancel()` 对已启动的任务无效，
**它的线程仍在后台跑到结束**——也就是说副作用（写盘/提交/发送/删除）可能已经发生，
而模型收到的是「失败」。模型最自然的反应就是重试 → 同一个副作用做两遍。
（对照实验 verify_p1_timeout.py 已把这一条钉成事实：30.0s 判失败、35s 后副作用文件出现。）

修复分三层，本文件锁死后两层：
  1. 文案层：超时结果表达「**结果未知**，不是失败」，有副作用的工具额外警告"先核对再动"
  2. 止损层：曾被判超时的**有副作用**调用，拒绝原样重发（`_is_refused_retry`）
  3. 指纹层：调用指纹先规范化再比对 —— 否则「键序/空格变一下」就绕过了止损
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent as ab                                  # noqa: E402  （import 约 3.5s，一次性）
from task_orchestrator import ToolCall, TaskOrchestrator    # noqa: E402
from agent_tools import AVAILABLE_TOOLS, NON_IDEMPOTENT_TOOLS  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_guard():
    """每个用例前后都清空止损台账（它是模块级全局状态）。"""
    ab._TIMED_OUT_CALLS.clear()
    yield
    ab._TIMED_OUT_CALLS.clear()


def _orch(side_effects=NON_IDEMPOTENT_TOOLS):
    o = TaskOrchestrator(max_workers=2, default_timeout=30, tools_map=AVAILABLE_TOOLS,
                         tool_timeouts={}, side_effect_tools=side_effects, log_enabled=False)
    o._register_signal_handlers = lambda: None
    return o


# ============ 1. 超时文案：结果未知，不是失败 ============

def test_timeout_message_says_result_unknown():
    o = _orch()
    try:
        msg = o._timeout_message(ToolCall(id="1", name="read_file", arguments={}), 30)
        assert "结果未知" in msg, "超时语义必须是「结果未知」而不是「失败」"
        assert "不是失败" in msg
        assert "重试是安全" in msg, "只读工具应当说明重试安全"
    finally:
        o.shutdown()


def test_timeout_message_warns_for_side_effect_tools():
    o = _orch()
    try:
        msg = o._timeout_message(ToolCall(id="1", name="execute_shell", arguments={}), 120)
        assert "结果未知" in msg
        assert "有副作用" in msg
        assert "不要原样重试" in msg, "有副作用的工具必须显式禁止原样重试"
        assert "核对目标状态" in msg
    finally:
        o.shutdown()


def test_timeout_message_reports_the_actual_limit():
    o = _orch()
    try:
        msg = o._timeout_message(ToolCall(id="1", name="execute_shell", arguments={}), 130)
        assert "130 秒" in msg, "报的必须是这次实际用的时限"
    finally:
        o.shutdown()


# ============ 2. 副作用声明位（幂等性） ============

@pytest.mark.parametrize("tool", ["execute_shell", "execute_python", "create_tool",
                                  "skillhub_install", "mcp_manage", "mcp_call",
                                  "execute_browser"])
def test_known_side_effect_tools_are_declared(tool):
    assert tool in NON_IDEMPOTENT_TOOLS


@pytest.mark.parametrize("tool", ["read_file", "calculator", "search", "rag_query",
                                  "fetch_url", "web_extract", "time_weather",
                                  "restore_context"])
def test_readonly_tools_are_not_declared(tool):
    """只读工具不进清单：它们重试安全，拦它们反而妨碍正常干活。"""
    assert tool not in NON_IDEMPOTENT_TOOLS


def test_side_effect_table_is_live_reference():
    """与超时表同理：编排器持引用，WebUI 注入 ask_user 后立刻生效。"""
    table = set()
    o = _orch(side_effects=table)
    try:
        tc = ToolCall(id="1", name="ask_user", arguments={})
        assert o._has_side_effects(tc) is False
        table.add("ask_user")
        assert o._has_side_effects(tc) is True
    finally:
        o.shutdown()


# ============ 3. 指纹规范化（不然止损形同虚设） ============

@pytest.mark.parametrize("a,b", [
    ('{"file_path":"a.txt"}', '{"file_path": "a.txt"}'),
    ('{"a":1,"b":2}', '{"b":2,"a":1}'),
    ('{"command":"ls -la"}', '{"command":"ls -la" }'),
    ('{"x": 1,\n "y": 2}', '{"y":2,"x":1}'),
])
def test_fingerprint_is_normalized(a, b):
    assert ab._call_fingerprint("t", a) == ab._call_fingerprint("t", b), \
        "同语义不同字面必须同指纹，否则模型换个写法就绕过止损"


def test_fingerprint_distinguishes_real_changes():
    assert ab._call_fingerprint("t", '{"a":1}') != ab._call_fingerprint("t", '{"a":2}')
    assert ab._call_fingerprint("t1", '{"a":1}') != ab._call_fingerprint("t2", '{"a":1}')


def test_fingerprint_survives_broken_json():
    """非法 JSON 不抛异常（退回原样），也不能让调用直接崩。"""
    assert ab._call_fingerprint("t", "not json") != ab._call_fingerprint("t", "also not json")


# ============ 4. 止损：拒绝原样重试 ============

def test_same_call_is_refused_after_timeout():
    args = '{"command": "rm -rf build/ && python build.py"}'
    fp = ab._call_fingerprint("execute_shell", args)
    assert ab._is_refused_retry("sid1", "execute_shell", fp) is False, "没超时过就不该拦"
    ab._remember_timeout("sid1", fp)
    assert ab._is_refused_retry("sid1", "execute_shell", fp) is True, "超时过且原样重发 → 拦"


def test_rewritten_call_passes():
    """模型改了参数 = 已经重新审视过 → 放行（给出路，否则模型会卡死）。"""
    fp1 = ab._call_fingerprint("execute_shell", '{"command":"do_it"}')
    ab._remember_timeout("sid1", fp1)
    fp2 = ab._call_fingerprint("execute_shell", '{"command":"do_it", "timeout":60}')
    assert ab._is_refused_retry("sid1", "execute_shell", fp2) is False


def test_readonly_tool_is_never_refused():
    """只读工具超时后重试完全安全 —— 不该被拦。"""
    args = '{"file_path": "x.txt"}'
    fp = ab._call_fingerprint("read_file", args)
    ab._remember_timeout("sid1", fp)
    assert ab._is_refused_retry("sid1", "read_file", fp) is False


def test_guard_is_per_session():
    """止损台账按会话隔离：A 会话的超时不许妨碍 B 会话的正常调用。"""
    fp = ab._call_fingerprint("execute_shell", '{"command":"x"}')
    ab._remember_timeout("sid-A", fp)
    assert ab._is_refused_retry("sid-B", "execute_shell", fp) is False


def test_guard_is_bounded():
    """台账有上限，长期运行不会无限增长。"""
    for i in range(ab._TIMED_OUT_LIMIT + 50):
        ab._remember_timeout("sid", "fp-%d" % i)
    assert len(ab._TIMED_OUT_CALLS["sid"]) == ab._TIMED_OUT_LIMIT
