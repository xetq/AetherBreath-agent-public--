# -*- coding: utf-8 -*-
"""
restore_context 工具测试（agent_tools/restore_context.py）
==========================================================
覆盖：参数校验 / 无数据源 / 未找到 / 正常取回 / 异常兜底 / 参数透传 / schema 合法性

注意 import 方式：直接按文件导入，不走 agent_tools 包——包 __init__ 会拉起一堆兄弟工具，
还曾在 win32 下经由 multi_search 在 import 期 detach stdout、摧毁 pytest capture
（2026-09-14 已修根因；这里仍按文件导入，因为本用例要测的只是这个工具本身）。

跑法（项目根）：venv/Scripts/python -m pytest tests/test_restore_context.py -q
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent_tools"))

import restore_context as R  # noqa: E402


def setup_function(_):
    R.set_provider(None)                     # 每个用例从「无数据源」开始


# ---------------------------------------------------------------------------
# 参数校验与数据源缺失
# ---------------------------------------------------------------------------

def test_requires_at_least_one_selector():
    out = R.restore_context()
    assert "round_no" in out and "tool_call_id" in out and out.startswith("❌")


def test_without_provider_reports_clearly():
    out = R.restore_context(round_no=1)
    assert out.startswith("❌") and "无数据源" in out


def test_missing_target_returns_message():
    R.set_provider(lambda **kw: None)
    out = R.restore_context(tool_call_id="nope")
    assert out.startswith("❌") and "未找到" in out


# ---------------------------------------------------------------------------
# 正常路径与参数透传
# ---------------------------------------------------------------------------

def test_returns_payload_and_passes_arguments():
    seen = {}

    def provider(round_no=None, tool_call_id=None, max_chars=20000):
        seen.update(round_no=round_no, tool_call_id=tool_call_id, max_chars=max_chars)
        return "原始消息内容"

    R.set_provider(provider)
    out = R.restore_context(round_no=3, max_chars=1234)
    assert out == "原始消息内容"
    assert seen == {"round_no": 3, "tool_call_id": None, "max_chars": 1234}


def test_default_max_chars_is_20k():
    seen = {}
    R.set_provider(lambda **kw: seen.update(kw) or "x")
    R.restore_context(round_no=1)
    assert seen["max_chars"] == 20000


def test_bad_max_chars_falls_back():
    seen = {}
    R.set_provider(lambda **kw: seen.update(kw) or "x")
    R.restore_context(round_no=1, max_chars="不是数字")
    assert seen["max_chars"] == 20000


# ---------------------------------------------------------------------------
# 失败绝不崩回合
# ---------------------------------------------------------------------------

def test_provider_exception_is_caught():
    def boom(**kw):
        raise RuntimeError("boom")

    R.set_provider(boom)
    out = R.restore_context(round_no=1)
    assert out.startswith("❌") and "RuntimeError" in out and "boom" in out


def test_set_provider_none_disables_again():
    R.set_provider(lambda **kw: "x")
    assert R.restore_context(round_no=1) == "x"
    R.set_provider(None)
    assert "无数据源" in R.restore_context(round_no=1)


# ---------------------------------------------------------------------------
# schema 契约（工具注册要能直接用）
# ---------------------------------------------------------------------------

def test_schema_shape():
    s = R.restore_context_schema
    assert s["type"] == "function"
    fn = s["function"]
    assert fn["name"] == "restore_context"
    assert set(fn["parameters"]["properties"]) == {"round_no", "tool_call_id", "max_chars"}
    assert "折叠" in fn["description"]                    # 描述里必须说清何时该用
