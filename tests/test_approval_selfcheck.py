"""
审批自检的「豁免名单对账」测试（审计 L1-B7）

病灶：`approval.self_check(registered_tools)` 的参数**此前完全没用** —— 自检只跑一
批硬编码用例，于是"名单里写着一个不存在的工具名"没人会发现。本项目真发生过同型事故：
`_NEVER_PARALLEL_TOOLS` 里写着 "clarify" 而运行时真名是 "ask_user"，
于是"交互工具要串行"这个设计意图静默失效了很久。

本文件锁死：对账必须真的看注册表，名字对不上就报警。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import approval                                  # noqa: E402


def test_stale_names_are_reported():
    """注册表里没有的名字 → 必须报出来（不能静默），但**不改变 ok**。"""
    out = approval.self_check(["read_file", "execute_shell"])
    assert "豁免名单无失效名字" in out["checks"], "对账项没跑起来（参数又被忽略了？）"
    ok, detail = out["checks"]["豁免名单无失效名字"]
    assert ok is False
    assert "不存在" in str(detail)
    # 结构化字段：供脚本/界面直接读，不用去解析 detail 字符串
    assert out["stale_tool_names"] == sorted(approval.NON_FS_TOOLS - {"read_file", "execute_shell"})
    # 关键：漂移属于"维护问题"，不该把审批策略判成故障
    assert out["stale_tool_names"], "这组输入本来就该有失效名字"
    assert out["strategy_live"] is True, "策略本身仍是活的 —— 失效名字不会误拦也不会漏拦"


def test_consistent_names_pass():
    """把完整注册表传进来 → 一致，不报警。"""
    from agent_tools import AVAILABLE_TOOLS
    out = approval.self_check(sorted(AVAILABLE_TOOLS.keys()))
    ok, detail = out["checks"]["豁免名单无失效名字"]
    assert ok is True, "真实注册表应当与豁免名单一致，实得: %s" % detail
    assert out["stale_tool_names"] == []


def test_no_argument_still_works():
    """CLI 等不传工具表的场景：跳过对账，但自检本身照常完成。"""
    out = approval.self_check()
    assert "ok" in out and "checks" in out


def test_non_fs_list_matches_reality():
    """名单本身不许再带上不存在的工具名（这次修掉的 multi_search / web_reader）。"""
    from agent_tools import AVAILABLE_TOOLS
    stale = sorted(set(approval.NON_FS_TOOLS) - set(AVAILABLE_TOOLS))
    assert stale == [], "NON_FS_TOOLS 里仍有失效工具名: %s" % stale
