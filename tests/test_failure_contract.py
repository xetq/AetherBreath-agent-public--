"""
失败语义契约测试（守护审计 L2-T1/T4/T8 + L3-W5/W8 的修复）

修复前的病灶：**同一个"失败"有 5 套方言，只有 1 套能穿过层间接缝**——
    ❌ 前缀        → 唯一被认的
    中文「错误：」  → 认不出（web_reader / calculator 全套失败都用它）
    dict success:False → 认不出（time_weather / skillhub_install）
    dict 无失败位   → 认不出（web_extract 全失败也是 {"results": [...]}）
    ⚠️ 前缀        → 认不出（mcp_gateway 的"station 不可用"）
后果：一条命令明明失败，实时界面标红、刷新后标绿（判据复制三份且不等价）；
      历史 3892 次执行里至少 18 次「标 ✅ 但返回值在说失败」。

本文件锁死修复后的契约：
  1. **一份判据**（`task_orchestrator.is_business_failure`）认识全部合法方言
  2. 三处消费点（编排器 / bridge / 会话还原）**引用同一实现**，不许再各留一份
  3. 真实工具的业务失败，必须能被这份判据认出来（防未来又漂出第 7 套方言）

未覆盖面（诚实声明，别把"没测"当成"没问题"）：
  · search / rag_query / time_weather / skillhub_install / create_tool / mcp_* 的失败路径
    需要网络、向量库冷启动或真实副作用，未纳入本契约用例 —— 它们靠
    "dict 显式失败位"这条结构方言覆盖（取值逻辑已被 test_judge_covers_all_dialects 锁住）。
  · 只读/写盘之外的**语义级**失败（例如工具"成功返回"但结果无意义）无法机械判定，
    仍靠模型自行判断。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
BACKEND_DIR = ROOT / "agent_webui" / "backend"
for _p in (str(ROOT), str(AGENT_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from task_orchestrator import is_business_failure      # noqa: E402
from agent_tools import AVAILABLE_TOOLS                # noqa: E402


# ============ 1. 判据本体：方言覆盖 ============

@pytest.mark.parametrize("value,expected,why", [
    ("❌ 执行失败", True, "文本方言（工具层 27 处的老约定）"),
    ("\n❌ 前置换行的失败", True, "lstrip 后仍能认"),
    ("   ❌ 前置空格的失败", True, "★ sessions 那份复制品当年就栽在这里（少了 lstrip）"),
    ("⚠️ station 不可用先修它", False, "⚠️ 是限制/降级说明，不是失败"),
    ("⏰ 执行超时，已强杀进程树", False, "中断说明，不是失败"),
    ("错误：无法连接到该网址", True, "历史方言：只读兼容（旧落盘会话原文要能正确标红），"
                                     "新工具禁止再用 —— 由 test_no_chinese_error_prefix_left_in_tools 守着"),
    ("  错误：除以零", True, "历史方言 + 前置空白"),
    ("没有错误：随便一句包含该词但不是开头", False, "只认开头，不做全文搜索"),
    ("✅ 成功", False, "成功"),
    ("目录为空", False, "正常信息别误判成失败"),
    ("", False, "空串"),
    ({"success": False, "error": "x"}, True, "结构方言：time_weather / skillhub_install"),
    ({"ok": False, "results": []}, True, "结构方言：web_extract 新加的失败位"),
    ({"success": True, "result": 1}, False, "结构方言：成功"),
    ({"results": []}, False, "中性结构不判死（宁可不报，也不误报）"),
    (None, False, "非字符串非字典"),
    ([], False, "列表"),
    (123, False, "数字"),
])
def test_judge_covers_all_dialects(value, expected, why):
    assert is_business_failure(value) is expected, why


# ============ 2. 单一实现：三处不许再各留一份 ============

def test_sessions_reuses_the_single_judge():
    """会话还原层必须用同一个函数对象（曾经它自带一份、还少了 lstrip）。"""
    import sessions
    assert sessions.is_business_failure is is_business_failure


def test_bridge_wraps_the_single_judge_without_copying():
    """bridge 保留"永不抛出"的包装（戒律②），但逻辑本体不再复制（戒律①靠模块顶部 import 满足）。"""
    src = (BACKEND_DIR / "bridge.py").read_text(encoding="utf-8")
    assert "def biz_failed(" in src, "bridge 应保留永不抛出的包装层"
    assert "is_business_failure" in src, "bridge 必须引用 agent 层的唯一实现"
    assert "BIZ_FAIL_PREFIX" not in src, "本地复制的方言常量应已删除"


def _code_lines(path: Path):
    """只取代码行（注释里允许引用历史形态，不算残留）。"""
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]


def test_sessions_has_no_inline_judgement():
    src = "\n".join(_code_lines(BACKEND_DIR / "sessions.py"))
    assert 'startswith("❌")' not in src, "内联判据应已删除"


def test_agent_layer_has_no_second_copy():
    """agent 层内部也只许有一处定义（agent.py 自己不许再判一次）。"""
    src = "\n".join(_code_lines(AGENT_DIR / "agent.py"))
    assert 'startswith("❌")' not in src, "agent.py 应统一走 is_business_failure"


# ============ 3. 工具契约：真实失败调用必须能被认出来 ============

CONTRACT_CASES = [
    ("read_file", {"file_path": "不存在的文件_契约测试.txt"}, "文件不存在"),
    ("fetch_url", {"url": "not-a-url"}, "★ 曾经用中文『错误：』前缀，判据全不认"),
    ("execute_shell", {"command": ""}, "空命令参数守卫"),
    ("web_extract", {"urls": ["not-a-url"]}, "★ 曾经全失败也无失败位"),
    ("calculator", {"expression": "1/0"}, "★ 第 6 套方言：中文『错误：除以零！』"),
    ("calculator", {"expression": ""}, "★ 同上（表达式为空）"),
    ("calculator", {"expression": "1+"}, "★ 同上（语法错误）"),
]


@pytest.mark.parametrize("tool,kwargs,why", CONTRACT_CASES)
def test_tool_business_failure_is_recognized(tool, kwargs, why):
    """真实调用工具的安全失败路径：返回值必须被判据认出。

    这些用例只走**本地可触发、无网络、无副作用**的失败分支。
    """
    fn = AVAILABLE_TOOLS[tool]
    result = fn(**kwargs)
    assert is_business_failure(result) is True, (
        "%s 的失败返回没被统一判据认出来（%s）：%r" % (tool, why, str(result)[:180]))


def test_no_chinese_error_prefix_left_in_tools():
    """工具层不许再出现中文『错误：』前缀的失败方言（那是第 3 套方言的残留形态）。"""
    offenders = []
    for f in (ROOT / "agent_tools").glob("*.py"):
        if f.name == "__init__.py":
            continue
        text = f.read_text(encoding="utf-8")
        if '"错误：' in text or 'f"错误：' in text:
            offenders.append(f.name)
    assert not offenders, "仍有中文『错误：』方言：%s" % offenders


def test_web_extract_has_explicit_failure_bit():
    """web_extract 必须带 ok 失败位（全失败时为 False），否则整批失败会被当成功。"""
    r = AVAILABLE_TOOLS["web_extract"](urls=["not-a-url"])
    assert isinstance(r, dict) and "ok" in r, "缺 ok 失败位"
    assert r["ok"] is False
