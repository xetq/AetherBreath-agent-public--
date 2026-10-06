# -*- coding: utf-8 -*-
"""MCP 服务站快照（WebUI「运行时」面板的数据源）—— `agent_webui/backend/mcp_view.py`

判据（照 tests/README.md）：一条命令、无需 LLM、无需网关、不写真实文件。

钉住的东西（都是主人点名过的口径）：
  · **常驻**：没起过进程的 station 也必须出现在快照里（alive=false / calls=0）——
    面板不能"先发一条消息才有数"；
  · **只放会变的量**：开关 / 工具数 / 在线 / 调用次数 / 最近一次调用；
  · **快照里不含任何工具 schema**（那是 mcp_search 的事，注入/展示都不许夹带）；
  · Q26 的数据路径：真调一次之后，快照能说出**调了哪个 station 的哪个工具**；
  · 总闸 `mcp.enabled=false` 要如实上报；
  · 目录不存在也不许抛（面板照实显示空态）。

跑法（项目根）：venv/Scripts/python -m pytest tests/test_mcp_stations_api.py -q
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent_webui" / "backend"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

import mcp_view as V            # noqa: E402
import mcp_client as mc         # noqa: E402
import mcp_station as ms        # noqa: E402

STUB = ROOT / "tests" / "mcp_stub_server.py"
_ENV_KEYS = ("AETHER_MCP_STATIONS",)

# 假 server 上真实存在的两个工具（tools 传名字列表时按这个造 schema）
_SCHEMAS = {
    "echo": ("回显文本", {"text": "string"}, ["text"]),
    "fail": ("故意失败", {"reason": "string"}, []),
}


class Env:
    """临时 station 目录（环境变量指哪，扫描就跟到哪）。"""

    def __init__(self):
        self.dir = Path(__import__("tempfile").mkdtemp(prefix="ab_mcpview_"))
        self._old = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["AETHER_MCP_STATIONS"] = str(self.dir)

    def add(self, name, *, origin="hand", enabled=True, tools=2, command=True):
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        cmd = sys.executable if command else ""
        (folder / "STATION.md").write_text(
            "---\nname: %s\ndescription: %s 的说明\ncommand: %s\nargs: ['%s', '--behavior', 'normal']\n"
            "enabled: %s\ntimeout: 10\norigin: %s\n---\n\n# %s\n"
            % (name, name, cmd, str(STUB).replace("\\", "/"), "true" if enabled else "false", origin, name),
            encoding="utf-8")
        rows = []
        names = ["t%d" % i for i in range(tools)] if isinstance(tools, int) else list(tools)
        for n in names:
            desc, props, req = _SCHEMAS.get(n, ("工具 " + n, {"text": "string"}, ["text"]))
            block = ("  - name: %s\n    description: %s\n    inputSchema:\n      type: object\n"
                     "      properties:\n" % (n, desc))
            for p, typ in props.items():
                block += "        %s:\n          type: %s\n" % (p, typ)
            if req:
                block += "      required: [%s]\n" % ", ".join(req)
            rows.append(block)
        (folder / "tools.yaml").write_text("station: %s\ntools:\n%s\n" % (name, "\n".join(rows)),
                                           encoding="utf-8")
        return folder

    def close(self):
        try:
            mc.shutdown_all()
        except Exception:                                        # noqa: BLE001
            pass
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        __import__("shutil").rmtree(str(self.dir), ignore_errors=True)


@pytest.fixture()
def env():
    e = Env()
    try:
        yield e
    finally:
        e.close()


def test_snapshot_is_resident_and_never_starts_processes(env):
    """没起过进程的 station 也在（常驻），且扫描本身不起进程。"""
    env.add("alpha", origin="hand", tools=3)
    env.add("beta", origin="auto", enabled=False, tools=1)
    snap = V.stations_snapshot()
    assert snap["ok"] is True, snap
    assert snap["count"] == 2, "关着的站也得在快照里（面板要看得见）"
    assert snap["on_switch"] == 1 and snap["online"] == 0
    assert snap["total_tools"] == 4
    assert {r["name"] for r in snap["stations"]} == {"alpha", "beta"}
    for r in snap["stations"]:
        assert r["alive"] is False and r["calls"] == 0, "没调过就不能装成在跑"
        for k in ("origin", "enabled", "usable", "tools", "problems", "last_tool",
                  "last_call_at", "last_call_ok", "missing_env", "server_version"):
            assert k in r, "面板要的字段一个都不能少：%s" % k
    beta = [r for r in snap["stations"] if r["name"] == "beta"][0]
    assert beta["enabled"] is False and beta["origin"] == "auto"


def test_snapshot_carries_no_tool_schema(env):
    """快照里不许夹带工具 schema（那是 mcp_search 的活；注入/展示都别夹带）。"""
    env.add("big", tools=30)
    import json
    blob = json.dumps(V.stations_snapshot(), ensure_ascii=False)
    assert "inputSchema" not in blob and "properties" not in blob, blob[:400]
    assert '"tools": 30' in blob, "只需要**数量**"


def test_snapshot_reports_last_call_after_a_real_call(env):
    """Q26 的数据路径：真调一次之后，快照能说出调了哪个 station 的哪个工具。"""
    env.add("alpha", tools=["echo", "fail"])
    snap0 = V.stations_snapshot()
    assert snap0["stations"][0]["last_tool"] is None, "还没调过就得如实说没有"
    from agent_tools import mcp_gateway as G
    out = G.mcp_call(station="alpha", tool="echo", arguments={"text": "面板"})
    assert "echo: 面板" in out, out
    row = V.stations_snapshot()["stations"][0]
    assert row["last_tool"] == "echo", row
    assert row["last_call_ok"] is True and row["last_call_at"], row
    assert row["alive"] is True and row["calls"] >= 1, "调过之后是在线且计数的"
    assert mc.peek_server_info("alpha")["last_tool"] == "echo", "底层表里也要有"


def test_snapshot_reports_failed_call_as_failed(env):
    env.add("bad", tools=["fail"])
    from agent_tools import mcp_gateway as G
    out = G.mcp_call(station="bad", tool="fail", arguments={"reason": "故意"})
    assert out.startswith("❌"), out
    row = V.stations_snapshot()["stations"][0]
    assert row["last_call_ok"] is False, "失败也要如实记（不许只记成功）"


def test_snapshot_reports_global_switch(env, monkeypatch):
    env.add("alpha")
    assert V.stations_snapshot()["enabled"] is True
    monkeypatch.setattr(ms, "mcp_enabled", lambda: False)
    assert V.stations_snapshot()["enabled"] is False, "总闸状态要如实上报"


def test_snapshot_survives_missing_and_broken_station_dir(monkeypatch):
    monkeypatch.setenv("AETHER_MCP_STATIONS", str(Path(tempfile_path())))
    snap = V.stations_snapshot()
    assert snap["ok"] is True and snap["count"] == 0, "目录不存在 = 空态，不是崩溃"


def tempfile_path():
    import tempfile
    return Path(tempfile.mkdtemp(prefix="ab_mcpview_empty_")) / "nope"


def test_panel_reports_unknown_not_false_zeros_when_agent_never_ran(env):
    """没有 agent 快照时，面板必须说"运行态未知"，**不许**把 0/未起当事实、更不许误报缺 env。

    实测踩过的两个坑（都是"网关进程看不到 agent 状态"引起的）：
      · 面板一直显示"未起 / 0 次"，可 AB 那边明明在调工具（连接活在 bridge 进程里）；
      · 面板误报"缺 GITHUB_PERSONAL_ACCESS_TOKEN"，而 agent 侧 `.env` 里有它（网关没加载）。
    """
    env.add("alpha", tools=["echo"])
    snap = V.stations_snapshot()
    assert snap["ok"] is True
    assert snap.get("snapshot_at") is None, "没有快照就要如实说没有（前端据此显示'运行态未知'）"
    assert snap.get("runner_pid") is None
    row = [r for r in snap["stations"] if r["name"] == "alpha"][0]
    assert row["alive"] is False and row["calls"] == 0, "没快照 = 只有静态事实"
    assert row["missing_env"] == [], "网关侧不许猜 env（它看不到 agent 的 .env，猜就是误报）"


def test_snapshot_marks_when_the_agent_process_wrote_it(env):
    """真调一次 → 快照落盘 → 面板知道"这是 agent 写的、什么时候写的、哪个 pid"。"""
    env.add("alpha", tools=["echo"])
    from agent_tools import mcp_gateway as G
    out = G.mcp_call(station="alpha", tool="echo", arguments={"text": "快照"})
    assert "echo: 快照" in out, out
    snap = V.stations_snapshot()
    assert snap.get("snapshot_at"), "调用过就该有时间戳（面板据此从'未知'变成'在线'）"
    assert snap.get("runner_pid") == os.getpid(), "写快照的就是本进程"
    row = [r for r in snap["stations"] if r["name"] == "alpha"][0]
    assert row["alive"] is True and row["last_tool"] == "echo" and row["last_call_ok"] is True


def test_a_fresh_process_that_never_touched_mcp_writes_no_snapshot():
    """**全新进程**（没碰过 MCP）调 `shutdown_all()`：不许写快照、也不许凭空造 `.state/`。

    用子进程验，是因为 `_CONNS`/`_STATS` 是**进程级**的 —— 在同一进程里"装成没碰过"是假的
    （pytest 主进程前面早调过 MCP 了）。实测踩过：某个没连过 MCP 的短命进程写下 `servers: {}`，
    把 agent 写的"调过 3 次"抹成 0 次。
    """
    import subprocess
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="ab_snap_guard_"))
    code = ("import os, sys;"
            "sys.path.insert(0, r'%s');"
            "os.environ['AETHER_MCP_STATIONS'] = r'%s';"
            "import mcp_client as mc;"
            "mc.shutdown_all()" % (str(ROOT / "agent"), str(tmp)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-300:]
    assert not (tmp / ".state" / "runtime.json").exists(), "没碰过 MCP 就不该写快照"
    assert not (tmp / ".state").exists(), "也不该凭空造目录"


if __name__ == "__main__":                                       # 直跑（省得记 pytest 参数）
    sys.exit(pytest.main([__file__, "-q"]))
