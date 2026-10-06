# -*- coding: utf-8 -*-
"""MCP 元工具 L1 回归 —— `agent_tools/mcp_gateway.py`（v2 的两个入口）。

判据（照 tests/README.md）：一条命令、无需 LLM、无需网关、不写真实文件（全走临时目录）。
"真进程"只有一个：`tests/mcp_stub_server.py`（纯 stdlib 假 server），且只在少数用例里起。

钉住的核心承诺（v2，见 docs/MCP设计.md §五）：
  · **工具 schema 按需取**：注册表里没有 schema；`mcp_search(station=...)` 才把该 station 的
    工具与**完整 inputSchema** 给出来（工具多时**分页**，不许一次吐爆）；
  · **调用只走 `mcp_call`**：参数吃对象或 JSON 字符串；缺必填在**本地**就拒（不白起进程）；
  · **不猜**：station/tool 名字写错就如实说，并把现有的/最接近的报出来；
  · 任何异常都不抛出窗口（一律 ❌ 文本），否则编排器会把它当工具崩溃。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
STUB = Path(__file__).resolve().parent / "mcp_stub_server.py"
for _p in (str(AGENT_DIR), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mcp_client as mc          # noqa: E402
import mcp_station as ms         # noqa: E402
import agent_tools.mcp_gateway as G   # noqa: E402


# ============================================================
# 夹具与工具
# ============================================================

class Stations:
    """临时 station 目录（每例一个，互不干扰）。"""

    def __init__(self, root: Path):
        self.dir = root

    def add(self, name, tools=1, enabled=True, command=True, description="示例 station",
            args=None, timeout=10, env=None):
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        lines = ["---", "name: %s" % name, "description: %s" % description]
        if command:
            lines += ["command: %s" % json.dumps(sys.executable),
                      "args: %s" % json.dumps([str(STUB).replace("\\", "/"), "--behavior", "normal"]
                                              if args is None else args)]
        lines += ["enabled: %s" % ("true" if enabled else "false"),
                  "timeout: %s" % timeout, "origin: hand"]
        if env:
            lines.append("env:")
            for k, v in env.items():
                lines.append("  %s: %s" % (k, json.dumps(v)))
        lines += ["---", "", "# %s" % name, ""]
        (folder / "STATION.md").write_text("\n".join(lines), encoding="utf-8")
        payload = []
        for i in range(tools):
            payload.append({
                "name": "tool%03d" % i if tools > 1 else "echo",
                "description": "示例工具 %d" % i,
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string", "description": "要回显的文本"},
                                               "n": {"type": "integer", "default": 1}},
                                "required": ["text"]},
            })
        ms.write_tools_file(name, payload, {"protocol_version": "test"}, stations=self.dir)
        return folder


@contextlib.contextmanager
def env(stations_dir: Path):
    saved = {k: os.environ.get(k) for k in ("AETHER_MCP_STATIONS",)}
    os.environ["AETHER_MCP_STATIONS"] = str(stations_dir)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            mc.shutdown_all()
        except Exception:
            pass


@contextlib.contextmanager
def tmp_stations():
    root = Path(tempfile.mkdtemp(prefix="mcp_gw_"))
    yield Stations(root)


# ============================================================
# 1. mcp_search —— 取工具与 schema
# ============================================================

def test_search_lists_stations_without_any_schema():
    with tmp_stations() as st:
        st.add("alpha", tools=3, description="阿尔法")
        with env(st.dir):
            out = G.mcp_search()
        assert "alpha" in out and "阿尔法" in out
        assert "inputSchema" not in out, "列 station 时不许带 schema（那是按需才给的）"
        assert "mcp_search(station" in out, "要教模型下一步怎么取 schema"


def test_search_returns_full_schema_for_a_station():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            out = G.mcp_search(station="alpha")
        assert "### echo" in out
        assert "`text`（string，必填）" in out
        assert "`n`（integer，可选，默认 1）" in out, "缺省值也要给出来（省得模型瞎填）"


def test_search_paginates_big_stations():
    with tmp_stations() as st:
        st.add("big", tools=G.MAX_TOOLS_PER_PAGE + 7)
        with env(st.dir):
            page1 = G.mcp_search(station="big")
            page2 = G.mcp_search(station="big", page=2)
        assert page1.count("### ") == G.MAX_TOOLS_PER_PAGE, "第一页正好一页"
        assert "第 1/2 页" in page1 and "page=2" in page1, "要告诉模型怎么翻页"
        assert page2.count("### ") == 7 and "第 2/2 页" in page2, "第二页是余下的"


def test_search_unknown_station_lists_existing_and_suggests():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        st.add("alphabet", tools=1)
        with env(st.dir):
            out = G.mcp_search(station="alph")
        assert out.startswith("❌") and "alpha" in out
        assert "是不是想找" in out, "近似的要提示，别让模型瞎试"


def test_search_disabled_station_says_how_to_enable():
    with tmp_stations() as st:
        st.add("off", tools=1, enabled=False)
        with env(st.dir):
            out = G.mcp_search(station="off")
        assert "关着" in out and "enabled" in out and "true" in out, "要说清是开关问题"
        assert "mcp_station.py" in out, "要给出开它的具体动作，而不是只说不行"


def test_search_unusable_station_explains_problems():
    with tmp_stations() as st:
        st.add("broken", tools=1, command=False)
        with env(st.dir):
            out = G.mcp_search(station="broken")
        assert "不可用" in out and "command" in out


def test_search_with_no_stations_teaches_how_to_add_one():
    with tmp_stations() as st:
        with env(st.dir):
            out = G.mcp_search()
        assert "还没有任何 station" in out
        assert "mcp_manage" in out or "STATION.md" in out, "要有下一步（自己集成或手工建）"


def test_search_by_query_returns_candidates_not_schemas():
    with tmp_stations() as st:
        st.add("alpha", tools=2, description="时间与日期")
        st.add("beta", tools=2, description="文件与目录")
        with env(st.dir):
            hit = G.mcp_search(query="时间")
            miss = G.mcp_search(query="量子计算")
        assert "alpha" in hit and "beta" not in hit
        assert "inputSchema" not in hit, "候选阶段不该吐 schema"
        assert miss.startswith("❌") and "alpha" in miss, "没匹配要如实说，并把现有的列出来"


def test_search_query_hit_single_station_hints_next_step():
    with tmp_stations() as st:
        st.add("alpha", tools=2, description="时间与日期")
        with env(st.dir):
            out = G.mcp_search(query="日期")
        assert "匹配到 1 个 station" in out and "mcp_search(station=\"alpha\")" in out


# ============================================================
# 2. mcp_call —— 参数与拒绝路径（不起进程）
# ============================================================

def test_call_requires_station_and_tool():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            out = G.mcp_call(station="alpha")
        assert out.startswith("❌") and "station" in out and "tool" in out


def test_call_missing_required_arg_is_rejected_locally():
    """缺必填要在**本地**拒：省一趟进程启动，而且提示里必须带 schema（别让模型猜）。"""
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            out = G.mcp_call(station="alpha", tool="echo")
            alive = mc.peek_server_info("alpha").get("alive")
        assert out.startswith("❌") and "缺必填参数" in out and "`text`" in out
        assert "### echo" in out, "拒绝时要把 schema 一并给出来"
        assert alive is not True, "本地拒的情况下**不许**起 server 进程"


def test_call_unknown_tool_suggests_close_names():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            out = G.mcp_call(station="alpha", tool="ech")
        assert out.startswith("❌") and "是不是想调" in out and "echo" in out


def test_call_bad_arguments_shapes_are_rejected():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            bad_json = G.mcp_call(station="alpha", tool="echo", arguments="{不是 JSON")
            not_obj = G.mcp_call(station="alpha", tool="echo", arguments="[1,2]")
            wrong_type = G.mcp_call(station="alpha", tool="echo", arguments=5)
        assert bad_json.startswith("❌") and "JSON" in bad_json
        assert not_obj.startswith("❌") and "对象" in not_obj
        assert wrong_type.startswith("❌")


def test_call_unknown_station_says_what_exists():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            out = G.mcp_call(station="nope", tool="echo")
        assert out.startswith("❌") and "alpha" in out


def test_call_never_raises_even_if_client_explodes():
    """兜底：客户端炸了也不许把异常抛给编排器（那会算成工具崩溃）。"""
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        saved = mc.call_tool
        mc.call_tool = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸"))
        try:
            with env(st.dir):
                out = G.mcp_call(station="alpha", tool="echo", arguments={"text": "x"})
        finally:
            mc.call_tool = saved
        assert out.startswith("❌") and "内部异常" in out


# ============================================================
# 3. 真调用（起一次假 server）
# ============================================================

def test_call_runs_a_real_stub_server_with_dict_and_json_args():
    with tmp_stations() as st:
        st.add("alpha", tools=1)
        with env(st.dir):
            r1 = G.mcp_call(station="alpha", tool="echo", arguments={"text": "对象"})
            r2 = G.mcp_call(station="alpha", tool="echo", arguments='{"text": "字符串"}')
            alive = mc.peek_server_info("alpha").get("alive")
            closed = mc.shutdown_all()
        assert r1.strip() == "echo: 对象", repr(r1)
        assert r2.strip() == "echo: 字符串", "JSON 字符串也要能调（模型常写成串）"
        assert alive is True, "真调用必须把 server 进程拉起来"
        assert closed >= 1, "用完能收干净"


# ============================================================
# 4. 工具声明
# ============================================================

def test_schemas_declare_the_two_meta_tools():
    for schema, name in ((G.mcp_search_schema, "mcp_search"), (G.mcp_call_schema, "mcp_call")):
        assert schema["type"] == "function" and schema["function"]["name"] == name
        desc = schema["function"]["description"]
        assert "MCP" in desc
    assert set(G.mcp_call_schema["function"]["parameters"]["required"]) == {"station", "tool"}
    assert G.mcp_search_schema["function"]["parameters"]["required"] == [], \
        "mcp_search 不带参数就该能列 station（不能强制先给参数）"


def test_call_schema_warns_about_the_approval_card():
    desc = G.mcp_call_schema["function"]["description"]
    assert "批准" in desc, "要提醒模型：第一次调某个 station 可能弹卡（别把承诺说错）"


# ============================================================
# 独立直跑汇总（不依赖 pytest）
# ============================================================

def _run_all() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print("PASS  %s" % name)
        except Exception as e:
            failed.append((name, e))
            print("FAIL  %s → %s: %s" % (name, type(e).__name__, e))
    print("-" * 60)
    print("%d passed, %d failed（共 %d）" % (len(passed), len(failed), len(tests)))
    return 1 if failed else 0


def test_call_refuses_when_required_env_is_missing():
    """缺 token 这类"配置不全"，要在调用**之前**说清缺哪个变量（不起进程、不白等超时）。

    实测过：真的去起进程调工具，没 token 时会白等一整个超时（60s）才报错。
    """
    with tmp_stations() as st:
        st.add("needs_token", tools=1, env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${NO_SUCH_VAR_FOR_TEST}"})
        os.environ.pop("NO_SUCH_VAR_FOR_TEST", None)
        with env(st.dir):
            out = G.mcp_call(station="needs_token", tool="echo", arguments={"text": "x"})
        assert out.startswith("❌") and "缺环境变量" in out, out
        assert "NO_SUCH_VAR_FOR_TEST" in out, "要说清缺的是哪一个"
        assert "MCP-GitHub接入" in out, "要给出配置文档的去处（教会怎么装）"
        assert mc.peek_server_info("needs_token").get("alive") is not True, "不许白起进程"


def test_call_proceeds_when_env_is_present():
    """变量在，就不该拦（别把正常的站也拦死）。"""
    os.environ["PRESENT_VAR_FOR_TEST"] = "yes"
    try:
        with tmp_stations() as st:
            st.add("has_token", tools=1, env={"SOME_OK_VAR": "${PRESENT_VAR_FOR_TEST}"})
            with env(st.dir):
                out = G.mcp_call(station="has_token", tool="echo", arguments={"text": "好"})
            assert "echo: 好" in out, out
    finally:
        os.environ.pop("PRESENT_VAR_FOR_TEST", None)


if __name__ == "__main__":
    sys.exit(_run_all())
