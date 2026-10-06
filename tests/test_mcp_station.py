# -*- coding: utf-8 -*-
"""MCP 服务站（station）层 L1 回归 —— `agent/mcp_station.py`（v2 的发现层）。

判据（照 tests/README.md）：一条命令、无需 LLM、无需网关、不写真实文件（全走临时目录）。

钉住的核心承诺：
  · **文件夹存在即注册**（与技能系统同构的渐进式披露）；不合规的文件夹**报出来**，不静默丢；
  · **注入体积只与 station 数线性、与工具数无关**（本次重构的 KPI —— 112 个工具的大 station
    在注册表里也只有一行）；
  · `load_station()` **每次读盘** → 同一会话内新建的 station 立刻可见（"热"的底层保证）；
  · 路径优先级：参数 > `AETHER_MCP_STATIONS` > config `paths.mcp_stations` > 项目内默认目录。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_mcp_station.py -q
    venv/Scripts/python tests/test_mcp_station.py          # 独立直跑，自带汇总
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import mcp_client as mc                        # noqa: E402
import mcp_station as ms                       # noqa: E402

_ENV_KEYS = ("AETHER_MCP_STATIONS",)


# ============================================================
# 夹具
# ============================================================

def _station_md(name="demo", **meta) -> str:
    fields = {"name": name, "description": "示例 station", "command": "uvx",
              "args": ["pkg==1.0.0"], "enabled": True, "timeout": 20, "origin": "hand"}
    fields.update(meta)
    lines = ["---"]
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, list):
            lines.append("%s: %s" % (k, "[" + ", ".join('"%s"' % x for x in v) + "]"))
        elif isinstance(v, bool):
            lines.append("%s: %s" % (k, "true" if v else "false"))
        else:
            lines.append("%s: %s" % (k, v))
    lines += ["---", "", "# %s" % name, "", "正文说明（模型按需才会读）。"]
    return "\n".join(lines) + "\n"


def _tools_yaml(n: int, prefix="t") -> str:
    lines = ["# 机器生成（测试夹具）", "tools:"]
    for i in range(n):
        lines += ["- name: %s%d" % (prefix, i),
                  "  description: 工具 %d" % i,
                  "  inputSchema:",
                  "    type: object",
                  "    properties:",
                  "      arg:",
                  "        type: string",
                  "        description: 参数说明 %d" % i]
    return "\n".join(lines) + "\n"


class Env:
    """临时 station 目录；退出时清干净并把环境变量还原。"""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ab_station_"))
        self._old = {k: os.environ.get(k) for k in _ENV_KEYS}
        os.environ["AETHER_MCP_STATIONS"] = str(self.dir)

    def add(self, name="demo", tools=2, raw_md=None, raw_tools=None, **meta):
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        if raw_md is not None:
            if raw_md:
                (folder / "STATION.md").write_text(raw_md, encoding="utf-8")
        else:
            (folder / "STATION.md").write_text(_station_md(name, **meta), encoding="utf-8")
        if tools:
            (folder / "tools.yaml").write_text(raw_tools or _tools_yaml(tools),
                                               encoding="utf-8")
        return folder

    def close(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)


import contextlib  # noqa: E402


def _data_rows(text: str) -> int:
    """数注册表里的数据行（排除表头与 `|---|` 分隔行）。"""
    n = 0
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("| ") and not s.startswith("| station ") and not s.startswith("| （暂无）"):
            n += 1
    return n


@contextlib.contextmanager
def env():
    e = Env()
    try:
        yield e
    finally:
        e.close()


# ============================================================
# 1. 扫描与解析
# ============================================================

def test_folder_presence_means_registered():
    with env() as e:
        e.add("alpha", tools=3)
        e.add("beta", tools=1)
        found, issues = ms.scan_stations(e.dir)
        assert [s.name for s in found] == ["alpha", "beta"], "文件夹存在即注册（按名字排序）"
        assert issues == [], "正常 station 不该有 issue：%s" % issues
        assert found[0].tools_count == 3 and found[0].command == "uvx", "frontmatter 要解析出来"


def test_non_station_dirs_are_skipped():
    with env() as e:
        e.add("real", tools=1)
        (e.dir / ".state").mkdir()
        (e.dir / "__pycache__").mkdir()
        (e.dir / "tools").mkdir()
        (e.dir / "notes.txt").write_text("x", encoding="utf-8")
        found, _ = ms.scan_stations(e.dir)
        assert [s.name for s in found] == ["real"], "运行时痕迹/遗留目录/普通文件都不是 station"


def test_tools_without_station_md_is_reported():
    with env() as e:
        e.add("orphan", tools=2, raw_md="")
        found, issues = ms.scan_stations(e.dir)
        assert found == [], "没有 STATION.md 就不算 station"
        assert any("orphan" in i and "STATION.md" in i for i in issues), \
            "半成品文件夹要报出来（不许静默忽略）：%s" % issues


def test_bad_frontmatter_is_reported():
    with env() as e:
        e.add("nofm", tools=1, raw_md="# 没有 frontmatter 的文档\n")
        found, issues = ms.scan_stations(e.dir)
        assert found == [] and any("frontmatter" in i for i in issues), \
            "缺 frontmatter 要报：%s" % issues
        e.add("broken", tools=1, raw_md="---\nname: [broken\n---\n正文\n")
        found2, issues2 = ms.scan_stations(e.dir)
        assert "broken" not in [s.name for s in found2], "YAML 坏了就跳过"


def test_missing_name_falls_back_to_folder_name():
    with env() as e:
        (e.dir / "byfolder").mkdir(parents=True)
        (e.dir / "byfolder" / "STATION.md").write_text(
            "---\ncommand: uvx\n---\n正文\n", encoding="utf-8")
        (e.dir / "byfolder" / "tools.yaml").write_text(_tools_yaml(1), encoding="utf-8")
        found, issues = ms.scan_stations(e.dir)
        assert [s.name for s in found] == ["byfolder"], "缺 name 时用文件夹名"
        assert any("缺 name" in i for i in issues), "但要把这件事记下来"


def test_duplicate_names_keep_first_and_report():
    with env() as e:
        e.add("a", tools=1)
        (e.dir / "b").mkdir(parents=True)
        (e.dir / "b" / "STATION.md").write_text(_station_md("a"), encoding="utf-8")
        (e.dir / "b" / "tools.yaml").write_text(_tools_yaml(1), encoding="utf-8")
        found, issues = ms.scan_stations(e.dir)
        assert [s.name for s in found] == ["a"], "重名只留第一个"
        assert any("重复" in i for i in issues), "重名要记账"


def test_disabled_station_is_not_callable():
    with env() as e:
        e.add("on_me", tools=1)
        e.add("off_me", tools=1, enabled=False)
        vis = [s.name for s in ms.visible_stations(e.dir)]
        assert vis == ["on_me"], "enabled: false 不进注册表"
        rows = ms.render_rows(ms.scan_stations(e.dir)[0])
        assert "off_me" in rows, "但目录视角（render_rows 全量）仍能看到它（便于排障）"


def test_station_without_command_or_tools_is_unusable():
    with env() as e:
        e.add("nocmd", tools=1, command=None)
        e.add("notools", tools=0)
        found, issues = ms.scan_stations(e.dir)
        assert ms.visible_stations(e.dir) == [], "缺 command / 缺工具清单 = 不可用（fail-closed）"
        by = {s.name: s for s in found}
        assert not by["nocmd"].usable and not by["notools"].usable
        assert any("command" in i for i in issues) and any("tools.yaml" in i for i in issues), \
            "两种缺失都要说清：%s" % issues


# ============================================================
# 2. 注册表：渲染 / 同步 / 注入（核心 KPI）
# ============================================================

def test_each_station_is_exactly_one_row():
    with env() as e:
        e.add("a", tools=2)
        e.add("b", tools=1)
        rows = ms.render_rows(ms.visible_stations(e.dir))
        assert len(rows.splitlines()) == 2, "每 station 恰好一行"


def test_registry_size_is_independent_of_tool_count():
    """KPI：注入体积只与 station 数线性、与工具数无关。

    允许几个字节的差异 —— 行里的"工具数"是数字（112 vs 2 差 2 字节）。
    真正要钉死的是：**schema 一个字都不许进来**（那是几千字节的量级）。
    """
    with env() as small:
        small.add("s", tools=2)
        blk_small = ms.build_injection_block(small.dir, small.dir / "MCP_REGISTRY.md")
    with env() as big:
        big.add("s", tools=112)
        blk_big = ms.build_injection_block(big.dir, big.dir / "MCP_REGISTRY.md")
    assert abs(len(blk_small) - len(blk_big)) <= 8, \
        "112 个工具与 2 个工具的 station，注入块大小必须基本相同（实测差 %d 字节）" % (
            abs(len(blk_big) - len(blk_small)))
    for blk in (blk_small, blk_big):
        assert "inputSchema" not in blk and "参数说明" not in blk, \
            "工具 schema 一个字都不许进注册表/注入块"
        assert _data_rows(blk) == 1, "每 station 恰好一行（实测 %d 行）" % _data_rows(blk)


def test_injection_scales_linearly_with_station_count():
    """注入体积随 station 数线性增长（而不是随工具数）。"""
    with env() as e:
        e.add("one", tools=2)
        blk1 = ms.build_injection_block(e.dir, e.dir / "MCP_REGISTRY.md")
    with env() as e2:
        e2.add("one", tools=2)
        e2.add("two", tools=2)
        e2.add("three", tools=2)
        blk3 = ms.build_injection_block(e2.dir, e2.dir / "MCP_REGISTRY.md")
    assert _data_rows(blk3) == 3, "三个 station = 三行（实测 %d）" % _data_rows(blk3)
    assert len(blk3) > len(blk1), "多了 station 注入块当然变大（这就是线性）"
    assert len(blk3) - len(blk1) < 300, "每个 station 的成本就是一行（实测 %d 字节）" % (
        len(blk3) - len(blk1))


def test_registry_render_has_header_and_count():
    with env() as e:
        e.add("a", tools=1)
        text = ms.render_registry(ms.visible_stations(e.dir))
        assert "MCP_REGISTRY.md" in text and "mcp_search" in text, "注册表要写明怎么用（引导在注入块里）"
        assert "共 1 个 station" in text, "要有总数（给人看）"


def test_sync_created_then_unchanged_then_updated():
    with env() as e:
        e.add("a", tools=1)
        reg = e.dir / "MCP_REGISTRY.md"
        r1 = ms.sync_registry(e.dir, reg)
        assert r1.created and r1.changed and reg.exists(), "第一次同步 = 新建"
        r2 = ms.sync_registry(e.dir, reg)
        assert not r2.changed, "内容没变就不动文件（省得脏 diff）"
        e.add("b", tools=1)
        r3 = ms.sync_registry(e.dir, reg)
        assert r3.changed and r3.rows_added == ["b"], "新增 station 要报出来：%s" % r3.rows_added
        assert "| b |" in reg.read_text(encoding="utf-8")
        # 只改文件夹名：station 名来自 frontmatter，所以改名**不该**悄悄生效 ——
        # 要报出"两者不一致"，让主人知道得两处一起改。
        (e.dir / "b").rename(e.dir / "b_renamed")
        found, issues = ms.scan_stations(e.dir)
        assert any("不一致" in i for i in issues), "文件夹名与 frontmatter 名不一致要报出来：%s" % issues
        assert [s.name for s in found] == ["a", "b"], "以 frontmatter 为准（改名静默无效）"
        r4 = ms.sync_registry(e.dir, reg)
        assert not r4.changed, "名字没变 → 注册表也不该变"


def test_sync_reports_issues_for_broken_station():
    with env() as e:
        e.add("bad", tools=0)          # 缺 tools.yaml
        r = ms.sync_registry(e.dir, e.dir / "MCP_REGISTRY.md")
        assert r.issues, "坏 station 的问题要随同步结果报出来"
        assert r.stations == 1, "坏 station 也留在注册表里占一行（靠状态列报告问题）"
        reg = (e.dir / "MCP_REGISTRY.md").read_text(encoding="utf-8")
        assert "⚠ 不可用" in reg, "坏 station 的状态必须是 ⚠ 不可用（标 on 是骗人）"


def test_injection_block_teaches_two_step_flow():
    with env() as e:
        e.add("a", tools=1)
        blk = ms.build_injection_block(e.dir, e.dir / "MCP_REGISTRY.md")
        assert "mcp_search" in blk and "mcp_call" in blk, "注入块要教模型两步走"
        assert "不要凭记忆猜参数名" in blk, "要点明'先 search 再 call'"
        assert "| a |" in blk, "注册表正文要在里面"


def test_injection_block_empty_when_no_station_dir():
    with env() as e:
        empty = e.dir / "nope"
        assert ms.build_injection_block(empty, empty / "MCP_REGISTRY.md") == "", \
            "没有 station 目录时注入空串（不许炸，也不许注入半截）"


# ============================================================
# 3. 运行期 spec（给 mcp_client 用）
# ============================================================

def test_load_registry_builds_specs_from_stations():
    with env() as e:
        e.add("alpha", tools=3, origin="auto", timeout=45, never_parallel=True,
              env={"TOKEN": "${T}"})
        reg = ms.load_registry(e.dir)
        assert reg.path == e.dir, "station 模式下 Registry.path 就是 station 目录"
        specs = {s.name: s for s in reg.servers}
        assert "alpha" in specs, "station 要变成运行期 spec"
        sp = specs["alpha"]
        assert sp.origin == "auto", "origin 来自 STATION.md 的 frontmatter"
        assert sp.timeout == 45 and sp.never_parallel is True, "frontmatter 的字段要生效"
        assert sp.env == {"TOKEN": "${T}"}, "env 占位要原样带下去（展开在运行期做）"
        assert len(sp.tools) == 3 and sp.tools_path == e.dir / "alpha" / "tools.yaml", \
            "工具 schema 从 station 自己的 tools.yaml 读"
        assert sp.tools[0].name and isinstance(sp.tools[0].input_schema, dict), \
            "工具要有名字与原始 inputSchema"


def test_load_station_reads_fresh_each_time():
    """热发现的底层保证：`load_station` 每次读盘，不缓存。"""
    with env() as e:
        assert ms.load_station("fresh", e.dir) is None, "还没建就该是 None"
        e.add("fresh", tools=1)
        st = ms.load_station("fresh", e.dir)
        assert st is not None and st.tools_count == 1, \
            "同一会话里刚建的 station 必须立刻可见（不重启、不缓存）"
        (e.dir / "fresh" / "tools.yaml").write_text(_tools_yaml(4), encoding="utf-8")
        assert ms.load_station("fresh", e.dir).tools_count == 4, "改动也要立刻可见"


def test_mcp_client_uses_station_mode_by_default():
    with env() as e:
        e.add("via_client", tools=1)
        reg = mc.load_registry()          # 不传路径、没设旧 env → station 模式
        assert reg.path == e.dir, "mcp_client 应当走 station 加载器"
        assert any(s.name == "via_client" for s in reg.servers)


# ============================================================
# 4. 路径 / 开关 / 写文件 / CLI
# ============================================================

def test_stations_dir_precedence():
    """优先级：**参数 > `AETHER_MCP_STATIONS` > config `paths.mcp_stations` > 项目内默认**。

    曾经还认一个"v1 注册表路径"环境变量作为回退 —— 那条兼容已在 2026-09-15 随 v1
    一起删掉，所以优先级里不再提它。
    """
    with env() as e:
        assert ms.stations_dir() == e.dir, "环境变量优先"
        assert ms.stations_dir(Path("X:/explicit")) == Path("X:/explicit"), "参数最高优先"
        saved = ms._config_paths
        try:
            os.environ.pop("AETHER_MCP_STATIONS", None)
            ms._config_paths = lambda: {"paths": {"mcp_stations": "somewhere/else"}}
            assert ms.stations_dir() == ms.PROJECT_ROOT / "somewhere/else", \
                "没有环境变量时看 config.yaml 的 paths.mcp_stations"
            ms._config_paths = lambda: {}
            assert ms.stations_dir() == ms.PROJECT_ROOT / ms.DEFAULT_STATIONS_DIR, \
                "config 也没写时落到项目内默认目录（%s）" % ms.DEFAULT_STATIONS_DIR
            assert str(ms.stations_dir()).replace("\\", "/").endswith("agent_MCP")
        finally:
            ms._config_paths = saved


def test_global_switch_reads_config():
    saved = ms._config_paths
    try:
        ms._config_paths = lambda: {"mcp": {"enabled": False}}
        assert ms.mcp_enabled() is False, "总闸 false 要能读出来"
        ms._config_paths = lambda: {"mcp": {"enabled": "false"}}
        assert ms.mcp_enabled() is False, "字符串 false 也算关"
        ms._config_paths = lambda: {}
        assert ms.mcp_enabled() is True, "缺省 = 开"
    finally:
        ms._config_paths = saved


def test_write_tools_file_lands_in_station_folder():
    with env() as e:
        path = ms.write_tools_file("newbie", [{"name": "x", "description": "d",
                                               "inputSchema": {"type": "object"}}],
                                   {"protocol_version": "2025-06-18"}, e.dir)
        assert path == e.dir / "newbie" / "tools.yaml", "必须写进 station 自己的文件夹"
        text = path.read_text(encoding="utf-8")
        assert "机器所有" in text, "表头要写明机器所有（免得有人手改）"
        tools = ms.read_tools(path)
        assert len(tools) == 1 and tools[0]["name"] == "x" and \
            tools[0]["inputSchema"] == {"type": "object"}, "写进去的要能原样读回来"


def test_cli_check_exit_codes():
    with env() as e:
        e.add("a", tools=1)
        reg = e.dir / "MCP_REGISTRY.md"
        assert ms.main(["--check"]) == 1, "注册表还没生成 → 不一致（退出码 1）"
        ms.sync_registry(e.dir, reg)
        assert ms.main(["--check"]) == 0, "同步过后一致（退出码 0）"


def test_tools_file_normalization_handles_junk():
    with env() as e:
        folder = e.add("junk", tools=0)
        (folder / "tools.yaml").write_text("tools:\n- name: ok\n- 'just-a-string'\n- {}\n",
                                           encoding="utf-8")
        tools = ms.read_tools(folder / "tools.yaml")
        assert [t["name"] for t in tools] == ["ok", "just-a-string"], \
            "坏条目要能容错（字典缺字段/字符串形式都认），空条目不认"
        assert ms._tool_entry("plain").get("inputSchema") == {"type": "object", "properties": {}}, \
            "纯字符串工具名也要生成合法 schema 兜底"


# ============================================================
# 独立直跑汇总
# ============================================================

def _run_all() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for name, fn in tests:
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
