# -*- coding: utf-8 -*-
"""自维护（AB 自己集成 MCP server）的 L1 回归 —— 离线、临时目录、无 LLM、无网络。

被测：`agent_tools/mcp_manage.py`（六动作）+ station 发现层 `agent/mcp_station.py`，
外加 `tests/mcp_self_integration_probe.py`（端到端"装完就能用"）。

关于"装完就能用"：v2 起 MCP 工具**不进常驻工具表** —— 靠 `mcp_search` / `mcp_call` 每次
实时读 station 文件夹。v1 那条"装完就把工具写进常驻工具表、并灌进常驻编排器"的路已在
2026-09-15 连同它的用例一起删掉，等价能力（不重启即用）由下面 v2 station 段覆盖。

关于审批：集成这一步**按主人 2026-09-14 的决定不设专门闸门**（原 `approvals/mcp_install.py`
已退役到 `agent_workspace/agent_self_maintenance/retired_mcp_install.py.txt`）。
`test_install_gate_is_intentionally_retired` 是**决策锁**：谁把闸门加回来，它会立刻红。

为什么这些用例必须存在（都是真踩过的）：
  · 机器**绝不许**改写主人手写的 station（`STATION.md` 的正文/注释一个字节都不许动）。
  · 失败必须**真的回滚**（曾经回滚成"半成品"，而改动前那文件夹压根不存在）。
  · `mcp_manage` 必须串行跑（读-改-写序列，同批并发会丢更新）。
  · 路径隔离：环境变量指哪，写入就得跟到哪（写错过一次 → 测试垃圾落进真实仓库）。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_mcp_manage.py -q
    venv/Scripts/python tests/test_mcp_manage.py          # 独立直跑，自带汇总
"""
from __future__ import annotations

import contextlib
import json
import os
import pytest
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
STUB = Path(__file__).resolve().parent / "mcp_stub_server.py"
PROBE = Path(__file__).resolve().parent / "mcp_self_integration_probe.py"

for p in (str(AGENT_DIR), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import mcp_client as mc                                   # noqa: E402
import agent_tools as A                                   # noqa: E402
import mcp_station as ms                                  # noqa: E402
import importlib                                           # noqa: E402
MM = importlib.import_module("agent_tools.mcp_manage")      # 见 agent_tools/__init__.py 的坑注释

_ENV_KEYS = ("AETHER_MCP_STATIONS", "AETHER_MCP_IMAGE_TMP", "AETHER_MCP_STATE")


def _load_by_path(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp_spawn = _load_by_path("mcp_spawn_spec", AGENT_DIR / "approvals" / "mcp_spawn.py")


# ============================================================
# 夹具
# ============================================================

class FakeLogger:
    def __init__(self, records=None, span=""):
        self.records = records if records is not None else []
        self.span = span

    def with_span(self, span):
        return FakeLogger(self.records, span)

    def _rec(self, level, msg, **extra):
        self.records.append({"span": self.span, "level": level, "msg": msg, "extra": extra})

    def info(self, msg, **extra):
        self._rec("info", msg, **extra)

    def warning(self, msg, **extra):
        self._rec("warning", msg, **extra)

    def error(self, msg, **extra):
        self._rec("error", msg, **extra)

    def debug(self, msg, **extra):
        self._rec("debug", msg, **extra)


def tools_payload() -> list:
    return [{"name": "echo", "description": "回显",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}},
                             "required": ["text"]}}]


# 夹具：统一用下面的 station 段（`StationEnv` / `station_env()`）。
# v1 的"手写表 + auto 表"那套路径已在 2026-09-15 连同它的用例一起删掉；路径隔离的纪律不变：
# **先把 `AETHER_MCP_STATIONS` 指向临时目录，再动任何文件** —— 反过来写会让测试垃圾落进
# 真实仓库的 `agent_MCP/`（这条纪律是踩坑换来的）。


# ============================================================
# 2. 校验与版本固定
# ============================================================

def test_bad_server_name_is_rejected():
    with station_env():
        for bad in ("", "has space", "很长的中文名", "a" * 33, "with/slash"):
            out = MM.mcp_manage(action="add", name=bad, command=sys.executable)
            assert out.startswith("❌") and "不合法" in out, "非法 server 名必须拒绝：%r" % bad


def test_pinning_skipped_for_plain_command():
    with station_env():
        args, note = MM._pin_args(sys.executable, [str(STUB)])
        assert args == [str(STUB)], "非包管理器命令不该被塞版本号（会把命令弄坏）"
        assert "跳过版本固定" in note, "跳过时要如实说明（不许假装已固定）"


def test_pinning_refuses_when_version_unknown(monkeypatch=None):
    with station_env():
        saved = MM._probe_version
        MM._probe_version = lambda command, pkg: None
        try:
            try:
                MM._pin_args("uvx", ["mcp-server-time"])
                assert False, "探不到版本必须抛 PinError（绝不默默用 latest）"
            except MM.PinError:
                pass
            out = MM.mcp_manage(action="add", name="nover", command="uvx",
                                args=["mcp-server-time"], why="探不到版本")
            assert out.startswith("❌") and "不锁版本" in out, \
                "报错文案要说明为什么不用 latest（供应链风险）"
            assert "web_search" in out, "要告诉 AB 下一步怎么办（去查确切版本号）"
        finally:
            MM._probe_version = saved


def test_pinning_writes_version_for_package_runners():
    with station_env():
        saved = MM._probe_version
        MM._probe_version = lambda command, pkg: "1.2.3"
        try:
            args_u, note_u = MM._pin_args("uvx", ["mcp-server-time"])
            assert args_u == ["mcp-server-time==1.2.3"], "uvx 用 == 固定版本"
            args_n, note_n = MM._pin_args("npx", ["-y", "@modelcontextprotocol/server-time"])
            assert args_n == ["-y", "@modelcontextprotocol/server-time@1.2.3"], \
                "npx 用 @ 固定版本（-y 之类的开关要原样保留）"
            assert "已固定" in note_u and "已固定" in note_n
        finally:
            MM._probe_version = saved


def test_scoped_npm_package_pin_detection():
    assert MM._is_pinned("npx", "@scope/name") is False, \
        "@scope/name 里的 @ 是作用域不是版本（误判会导致跳过固定 = 没锁版本）"
    assert MM._is_pinned("npx", "@scope/name@1.0.0") is True
    assert MM._is_pinned("uvx", "pkg==1.0.0") is True
    assert MM._is_pinned("uvx", "pkg") is False


# ============================================================
# 3. 六动作
# ============================================================

def test_search_lists_catalog_and_teaches_how_to_find_more():
    with station_env():
        out = MM.mcp_manage(action="search", query="time")
        assert "mcp-server-time" in out, "离线目录里要有常见候选（给 AB 一个起点）"
        assert "web_search" in out and "add" in out, \
            "目录里没有的必须教它怎么找（绝不猜包名）"
        only = MM.mcp_manage(action="search", query="git")
        assert "mcp-server-git" in only and "mcp-server-time" not in only, "query 要能过滤"


def test_list_reports_origin_tools_and_state():
    """`list`（v2）：报 station 目录与注册表、每个 station 的来源/开关/工具数/进程状态。"""
    with station_env() as e:
        e.add("stauto", origin="auto")
        e.add("sthand", origin="hand")
        out = MM.mcp_manage(action="list")
        assert str(e.dir) in out and ms.REGISTRY_NAME in out, \
            "要报出 station 目录与注册表名（主人得知道真相源在哪）"
        assert "stauto" in out and "sthand" in out, "每个 station 都要列出来"
        assert "auto" in out and "hand" in out, "要说明来源（谁可以被机器改）"
        assert "工具 1" in out, "要报工具数"
        assert "未启动" in out, "要报进程状态"
        assert "origin: auto" in out, "要说清开关/移除只对机器集成的生效"


def test_add_failure_rolls_back_truly():
    """集成失败要回到改动前：这次建的 station 文件夹删掉，注册表不留行。"""
    with station_env() as e:
        assert not (e.dir / "badone").exists(), "前置：这次集成前没有这个 station"
        out = MM.mcp_manage(action="add", name="badone", command=sys.executable,
                            args=[str(e.dir / "nope.py")], why="故意失败")
        assert out.startswith("❌"), "跑不起来必须如实失败"
        assert not (e.dir / "badone").exists(), \
            "回滚要回到改动前（文件夹本来不存在 → 就该删掉），不是留一个半成品"
        rows = e.registry.read_text(encoding="utf-8") if e.registry.exists() else ""
        assert "| badone |" not in rows, "失败的 station 不许进注册表"


def test_add_failure_counts_and_stops_then_force():
    with station_env() as e:
        for i in (1, 2):
            MM.mcp_manage(action="add", name="flaky", command=sys.executable,
                          args=[str(e.dir / "nope.py")])
        assert MM._attempts("flaky") == MM.MAX_ATTEMPTS
        out = MM.mcp_manage(action="add", name="flaky", command=sys.executable,
                            args=[str(e.dir / "nope.py")])
        assert "停手" in out and "force" in out, "达上限要停手并说明怎么继续（有界自愈）"
        assert "已连续失败" in out, "要说清失败了几次"
        out2 = MM.mcp_manage(action="add", name="flaky", command=sys.executable,
                             args=[str(STUB), "--behavior", "normal"], force=True)
        assert out2.startswith("✅"), "force=true 应当继续执行（决定权交回给人）"
        assert MM._attempts("flaky") == 0, "成功后失败计数要清零"


def test_test_action_refreshes_tools_and_can_call_tool():
    """`test`（v2）：真起进程、刷新 `tools.yaml`、可顺带真调；未知 station 如实报错。"""
    with station_env() as e:
        e.add("stprobe", origin="auto", with_tools=False)    # 还没有工具清单
        out = MM.mcp_manage(action="test", name="stprobe")
        assert out.startswith("✅") and "连通性正常" in out, out
        assert "echo" in out, "要列出该 station 的工具名"
        assert "工具清单已刷新" in out, "要说明清单落在哪"
        out2 = MM.mcp_manage(action="test", name="stprobe", tool="echo",
                             args={"text": "试调"})
        assert "echo: 试调" in out2, "test 可选真调一次工具（集成前先见真章）"
        tools = yaml.safe_load((e.dir / "stprobe" / "tools.yaml").read_text(encoding="utf-8"))
        assert "echo" in [t["name"] for t in tools["tools"]], "工具清单要真的落盘"
        out3 = MM.mcp_manage(action="test", name="nosuch")
        assert out3.startswith("❌"), "注册表里没有的 station 要如实报错"


def test_unknown_action_is_rejected():
    with station_env():
        out = MM.mcp_manage(action="destroy")
        assert out.startswith("❌") and "action" in out, "未知动作要报错并列出允许值"


# ============================================================
# 5. 集成这一步的闸门（主人 2026-09-14 的决定：不设专门闸门）
# ============================================================

def test_install_gate_is_intentionally_retired():
    """决策锁：集成第三方 server **不设专门闸门**（这是主人的决定，不是遗漏）。

    原来的 `agent/approvals/mcp_install.py` 已退役到工作区。谁把闸门加回来
    （在 approvals/ 里新建一类、或让引擎为此弹卡），这条会立刻红 ——
    那时候请先问主人要不要这么做。
    """
    assert not (AGENT_DIR / "approvals" / "mcp_install.py").exists(), \
        "approvals/ 里不该再有集成闸门（退役是主人的决定）"
    retired = (PROJECT_ROOT / "agent_workspace" / "agent_self_maintenance"
               / "retired_mcp_install.py.txt")
    assert retired.exists(), "退役原文要留着（可恢复胜过彻底消失）"
    import approval
    assert "mcp.install" not in approval.specs_state()["loaded"], \
        "引擎不该再加载 mcp.install 这一类规范"


def test_manage_schema_warns_model_it_downloads_and_runs():
    """add 会在本机下载并真跑第三方包 —— 模型必须被明确告知（动手前先跟主人说清）。"""
    desc = MM.mcp_manage_schema["function"]["description"]
    assert "下载" in desc and "真跑" in desc, "工具描述必须写明 add 会下载并执行第三方包"
    assert "主人" in desc, "要提醒模型：动手前先跟主人说清包名/来源"
    assert "启动确认卡" in desc, "要说明第一次真调用时仍会有一张卡（别让模型承诺错）"


def test_manager_tool_is_forced_serial_in_orchestrator():
    """契约锁：mcp_manage 必须在编排器的强制串行名单里。"""
    import task_orchestrator as TO
    assert "mcp_manage" in TO._NEVER_PARALLEL_TOOLS, \
        "mcp_manage 是'读注册表→改→写回'序列，同批并发会丢更新（后写覆盖先写）"


# ============================================================
# 6. 端到端（子进程跑探针）
# ============================================================

def test_self_integration_probe_is_green():
    """v2 端到端：装一个 station → 不重启就能用 → 关掉即拒 → 移除进回收站。"""
    env_vars = dict(os.environ)
    env_vars["PYTHONIOENCODING"] = "utf-8"
    env_vars.pop("AETHER_MCP_STATIONS", None)
    proc = subprocess.run([sys.executable, "-X", "utf8", str(PROBE)], capture_output=True,
                          text=True, cwd=str(PROJECT_ROOT), env=env_vars, timeout=300,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "自集成端到端探针必须全过：\n%s\n%s" % (
        proc.stdout[-1500:], proc.stderr[-800:])
    data = json.loads(proc.stdout.strip())
    assert data["ok"] is True, "探针自报失败：%s" % [s for s in data["steps"] if not s["ok"]]
    steps = {s["step"]: s for s in data["steps"]}
    assert steps["add 建出 station 文件夹（STATION.md + tools.yaml）"]["ok"]
    assert steps["mcp_call 真调成功（echo 回显）"]["ok"], "装完不必重启就能调（核心承诺）"
    assert steps["试跑不会把 station 标成已批准（第一次真调用要问人）"]["ok"], \
        "连通性试跑不许冒充人工批准"
    assert steps["set_enabled 关掉后，同一个会话里调用立刻被拒"]["ok"], "开关必须是实时的"
    assert steps["remove 把文件夹挪进回收站（可恢复），站点立刻消失"]["ok"], \
        "移除要可恢复（trash 优于 rm）"
    assert steps["真实仓库的注册表没被这次探针碰过"]["ok"], "探针不许污染工作区"


# ============================================================
# v2：station 模式的自维护（P4）—— 集成 = 建文件夹，且只动自己建的那份
# ============================================================

class StationEnv:
    """一个临时 station 目录（`AETHER_MCP_STATIONS` 指向它 = station 模式）。

    纪律与 `Env` 相同：**先**把环境变量指向临时目录再看别的 —— 否则写路径会落到真实仓库。
    """

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ab_station_"))
        self._old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["AETHER_MCP_STATIONS"] = str(self.dir)
        os.environ.pop("AETHER_MCP_STATE", None)

    # --- 造 station（模拟主人手写，或机器集成后的样子）---
    def add(self, name, origin="auto", enabled=True, behavior="normal", with_tools=True,
            with_command=True, extra_body=""):
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        fm = {"name": name, "description": "测试 station", "enabled": enabled, "origin": origin}
        if with_command:
            fm["command"] = sys.executable
            fm["args"] = [str(STUB).replace("\\", "/"), "--behavior", behavior]
            fm["timeout"] = 10
        text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n"
        text += "# %s\n\n正文不该被机器改写。%s\n" % (name, extra_body)
        (folder / "STATION.md").write_text(text, encoding="utf-8")
        if with_tools:
            (folder / "tools.yaml").write_text(
                yaml.safe_dump({"station": name, "tools": tools_payload()},
                               allow_unicode=True, sort_keys=False), encoding="utf-8")
        return folder

    def doc(self, name) -> Path:
        return self.dir / name / "STATION.md"

    @property
    def registry(self) -> Path:
        return self.dir / ms.REGISTRY_NAME

    def close(self):
        try:
            mc.shutdown_all()
        except Exception:
            pass
        with mc._CONNS_LOCK:
            mc._CONNS.clear()
        mc.reset_approvals()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


@contextlib.contextmanager
def station_env():
    e = StationEnv()
    try:
        yield e
    finally:
        e.close()


def _manage(**kw):
    return A.mcp_manage(**kw)


def test_station_add_creates_folder_tools_and_registry_row():
    """集成 = 建 `<station>/`（STATION.md + tools.yaml），注册表立刻多一行，且**不写**旧 auto 表。"""
    with station_env() as e:
        out = _manage(action="add", name="stnew", command=sys.executable,
                      args=[str(STUB).replace("\\", "/"), "--behavior", "normal"],
                      why="测试集成", source="unit-test")
        assert out.startswith("✅"), out
        assert e.doc("stnew").exists(), "要建 STATION.md"
        doc = e.doc("stnew").read_text(encoding="utf-8")
        assert "origin: auto" in doc and "command:" in doc
        tools = yaml.safe_load((e.dir / "stnew" / "tools.yaml").read_text(encoding="utf-8"))
        names = [t["name"] for t in tools["tools"]]
        assert "echo" in names and len(names) >= 2, "假 server 的工具清单要落盘：%s" % names
        assert "| stnew |" in e.registry.read_text(encoding="utf-8"), "注册表要有一行"
        assert sorted(f.name for f in (e.dir / "stnew").iterdir()) == ["STATION.md", "tools.yaml"], \
            "station 文件夹里只该有这两样（不许再冒出旧 auto 表那类旁支文件）"
        assert "不用重启" in out, "要告诉模型/主人：不需要重启"


def test_station_add_refuses_to_touch_hand_written_station():
    """撞到主人手写的 station：不覆盖、不改写（原文件字节不变）。"""
    with station_env() as e:
        folder = e.add("handmade", origin="hand")
        before = (folder / "STATION.md").read_bytes()
        out = _manage(action="add", name="handmade", command=sys.executable, args=["-c", "pass"])
        assert out.startswith("❌") and "手写" in out, out
        assert (folder / "STATION.md").read_bytes() == before, "手写文件一个字节都不许变"


def test_station_add_rolls_back_a_failed_integration():
    """集成失败（server 起不来）→ 这次建的文件夹要删掉，注册表不留行，并记账。"""
    with station_env() as e:
        out = _manage(action="add", name="stbad", command=sys.executable,
                      args=["-c", "import sys; sys.exit(3)"])
        assert out.startswith("❌"), out
        assert "已回滚" in out, out
        assert not (e.dir / "stbad").exists(), "失败后不该留下半成品文件夹"
        rows = e.registry.read_text(encoding="utf-8") if e.registry.exists() else ""
        assert "| stbad |" not in rows


def test_station_add_is_bounded_by_failure_counter():
    """有界自愈：连续失败到上限就停手，必须 force 才继续（不许无限重试）。"""
    with station_env() as e:
        for _ in range(MM.MAX_ATTEMPTS):
            _manage(action="add", name="stflaky", command=sys.executable,
                    args=["-c", "import sys; sys.exit(9)"])
        out = _manage(action="add", name="stflaky", command=sys.executable,
                      args=["-c", "import sys; sys.exit(9)"])
        assert out.startswith("❌") and "停手" in out, out
        assert "向主人汇报" in out, "上限到了要让它去汇报，而不是自己继续试"


def test_station_set_enabled_edits_only_that_line():
    """开关：只改 `enabled:` 那一行（正文/其余行原样保留），并同步注册表。"""
    with station_env() as e:
        e.add("sttoggle", origin="auto")
        out = _manage(action="set_enabled", name="sttoggle", enabled=False)
        assert out.startswith("✅"), out
        text = e.doc("sttoggle").read_text(encoding="utf-8")
        assert "enabled: false" in text
        assert "正文不该被机器改写。" in text, "正文必须原样保留"
        reg = e.registry.read_text(encoding="utf-8") if e.registry.exists() else ""
        assert "| sttoggle |" in reg, "关掉的 station 仍留在注册表里（\"装着但关着\"要看得见）"
        assert "| off |" in reg, "关掉后状态列必须是 off"


def test_station_set_enabled_refuses_hand_written():
    """手写 station 的开关不由机器改（给出确切的一行改法与后续动作）。"""
    with station_env() as e:
        folder = e.add("handswitch", origin="hand")
        before = (folder / "STATION.md").read_bytes()
        out = _manage(action="set_enabled", name="handswitch", enabled=False)
        assert out.startswith("❌") and "手写" in out, out
        assert "mcp_station.py" in out, "要给出具体动作"
        assert (folder / "STATION.md").read_bytes() == before


def test_station_remove_moves_folder_to_trash():
    """移除 = 整个文件夹挪进 `.trash/`（可恢复），注册表同步删行。"""
    with station_env() as e:
        e.add("stgone", origin="auto")
        _manage(action="add", name="stkeep", command=sys.executable,
                args=[str(STUB).replace("\\", "/"), "--behavior", "normal"])
        out = _manage(action="remove", name="stgone")
        assert out.startswith("✅"), out
        assert not (e.dir / "stgone").exists()
        trashed = list((e.dir / ".trash").glob("*-stgone/STATION.md"))
        assert trashed, "要能在回收站里找回（可恢复胜过彻底消失）"
        assert "| stgone |" not in e.registry.read_text(encoding="utf-8")
        assert "| stkeep |" in e.registry.read_text(encoding="utf-8"), "别人不受影响"


def test_station_remove_refuses_hand_written():
    with station_env() as e:
        e.add("hands", origin="hand")
        out = _manage(action="remove", name="hands")
        assert out.startswith("❌") and "手写" in out, out
        assert (e.dir / "hands" / "STATION.md").exists(), "手写的不能删"


def test_station_list_shows_origin_switch_and_tools():
    with station_env() as e:
        e.add("stauto", origin="auto")
        e.add("sthand", origin="hand", enabled=False)
        out = _manage(action="list")
        assert "stauto" in out and "sthand" in out
        assert "auto" in out and "hand" in out and "关" in out
        assert "工具 1" in out, "要报工具数"


def test_station_test_refreshes_tools_yaml_and_can_call():
    """连通性测试：真起进程、刷新 tools.yaml，能顺带真调一个工具。"""
    with station_env() as e:
        e.add("sttest", origin="auto", with_tools=False)     # 没有工具清单
        out = _manage(action="test", name="sttest", tool="echo", args={"text": "嗨"})
        assert out.startswith("✅"), out
        assert "echo: 嗨" in out, "真调的结果要在输出里：%s" % out
        tools = yaml.safe_load((e.dir / "sttest" / "tools.yaml").read_text(encoding="utf-8"))
        assert "echo" in [t["name"] for t in tools["tools"]], "工具清单要刷新"


def test_station_test_unknown_station_lists_what_exists():
    with station_env() as e:
        e.add("stonly", origin="auto")
        out = _manage(action="test", name="nope")
        assert out.startswith("❌") and "stonly" in out, out


# ============================================================
# 直跑汇总（不依赖 pytest；`python tests/test_mcp_manage.py`）
# ============================================================

def _run_all() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for name, fn in tests:
        marks = getattr(fn, "pytestmark", []) or []
        if any(getattr(m, "name", "") == "skip" for m in marks):
            print("SKIP  %s（%s）" % (name, (getattr(marks[0], "kwargs", {}) or {}).get("reason", "")))
            continue
        try:
            fn()
            passed += 1
            print("PASS  %s" % name)
        except Exception as e:
            failed.append((name, e))
            print("FAIL  %s → %s: %s" % (name, type(e).__name__, e))
    print("-" * 60)
    print("通过 %d / %d" % (passed, len(tests)))
    for name, e in failed:
        print("  ❌ %s → %s" % (name, e))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
