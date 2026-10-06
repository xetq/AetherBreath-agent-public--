#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mcp_sync —— 从真 MCP server 拉工具清单，写进 station 自己的文件夹。

**为什么要它**：注册表只注入"有哪些 station"，工具 schema 按需现取（设计见
`docs/MCP设计.md` v2）—— 所以每个 station 的工具清单要有一份**静态**真相（`tools.yaml`），
供 `mcp_search` 直接读、不必每次连 server。这个脚本就是从真 server 刷新那份清单的唯一入口。

**用法**（在项目根跑）：

    venv/Scripts/python agent_MCP/mcp_sync.py --list             # 有哪些 station、各几个工具
    venv/Scripts/python agent_MCP/mcp_sync.py --sync <station>   # 同步某个
    venv/Scripts/python agent_MCP/mcp_sync.py --sync all         # 同步全部 enabled 的
    venv/Scripts/python agent_MCP/mcp_sync.py --check            # 漂移检查（有漂移退出码 1）

注意：`--sync` 会**真的启动**该 server 进程一次（握手 → tools/list → 退出）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))       # 与运行时同一套扁平导入（模块身份唯一）

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import mcp_client as mc                      # noqa: E402
import mcp_station as ms                     # noqa: E402


def _diff(old: list, new: list) -> list:
    """比对工具清单，返回人类可读的差异行。"""
    om = {str(t.get("name")): t for t in (old or [])}
    nm = {str(t.get("name")): t for t in (new or [])}
    out = []
    for name in sorted(set(nm) - set(om)):
        out.append("+ 新增工具 %s" % name)
    for name in sorted(set(om) - set(nm)):
        out.append("- 消失工具 %s" % name)
    for name in sorted(set(om) & set(nm)):
        if om[name].get("inputSchema") != nm[name].get("inputSchema"):
            out.append("~ 参数 schema 变了 %s" % name)
        elif om[name].get("description") != nm[name].get("description"):
            out.append("~ 描述变了 %s" % name)
    return out


def _fetch(station: str) -> list:
    """连真 server 拉 tools/list（懒启动 → 握手 → 拉取 → 结束时统一退出）。"""
    st = ms.load_station(station)
    if st is None:
        raise mc.MCPError("station 目录里没有：%s" % station)
    if not st.command:
        raise mc.MCPError("station %s 的 STATION.md 没写 command" % station)
    return mc.list_tools(st.name)


def cmd_list(stations: Path) -> int:
    found, issues = ms.scan_stations(stations)
    print("station 目录：%s" % stations)
    print("注册表：%s" % ms.registry_path(stations))
    if not found:
        print("（还没有 station；建一个 `%s/<名字>/STATION.md` 文件夹即可）" % stations.name)
    for st in found:
        mark = "✅" if st.tools else "—"
        print("· %-14s %-4s 工具 %-4s %s  %s" % (
            st.name, "on" if st.enabled else "off",
            ("%d %s" % (st.tools_count, mark)) if st.tools else "0",
            st.description[:36] or "（未写用途）",
            "" if st.usable else "⚠️ 不可用"))
        for p in st.problems:
            print("    ⚠️  %s" % p)
    for i in issues:
        print("  ⚠️  %s" % i)
    return 0


def cmd_sync(stations: Path, target: str, check_only: bool = False) -> int:
    found, _ = ms.scan_stations(stations)
    known = {st.name: st for st in found}
    if target not in ("all", "*"):
        if target not in known:
            print("❌ station 目录里没有：%s" % target)
            return 2
        names = [target]
    else:
        names = [st.name for st in found]
    rc = 0
    for name in names:
        st = known[name]
        if target in ("all", "*") and not st.enabled:
            print("· 跳过 %s（enabled: false）" % name)
            continue
        print("→ 同步 %s：%s" % (name, " ".join(st.args and [st.command] or [st.command])
                                + (" " + " ".join(st.args) if st.args else "")))
        try:
            tools = _fetch(name)
        except mc.MCPError as e:
            print("  ❌ 失败：%s" % e)
            rc = 1
            continue
        meta = mc.peek_server_info(name)
        new = [ms._tool_entry(t) for t in tools]           # noqa: SLF001（同一模块族的内部规范化）
        old = [ms._tool_entry(t) for t in st.tools]        # noqa: SLF001
        diff = _diff(old, new)
        if check_only:
            if diff:
                print("  ⚠️ 有漂移（%d 处）：" % len(diff))
                for d in diff:
                    print("     %s" % d)
                rc = 1
            else:
                print("  ✅ 无漂移（%d 个工具）" % len(new))
            continue
        out = ms.write_tools_file(name, tools, meta, stations)
        print("  ✅ 写入 %s（%d 个工具）" % (out, len(new)))
        for d in diff:
            print("     %s" % d)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MCP 工具清单同步（station 模式，AetherBreath）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="列出 station 与工具数")
    g.add_argument("--sync", metavar="NAME", help="同步某个 station（或 all）")
    g.add_argument("--check", metavar="NAME", nargs="?", const="all",
                   help="漂移检查，不写文件（有漂移退出码 1）")
    args = ap.parse_args(argv)

    stations = ms.stations_dir()
    if not stations.is_dir():
        print("❌ station 目录不存在：%s" % stations)
        print("   先建它（见 agent_MCP/README.md）")
        return 2
    try:
        if args.list:
            return cmd_list(stations)
        if args.sync:
            return cmd_sync(stations, args.sync)
        return cmd_sync(stations, args.check, check_only=True)
    finally:
        n = mc.shutdown_all()
        if n:
            print("（已关闭 %d 个 MCP server 进程）" % n)


if __name__ == "__main__":
    sys.exit(main())
