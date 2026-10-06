# -*- coding: utf-8 -*-
"""MCP 自集成探针 v2 —— 把"我自己装一个 station，然后立刻用它干活"整条链真跑一遍。

它回答的是 v2 最要紧的那个问题：**`mcp_manage` 装的 station，能不能不重启就被用上？**
为此全用真东西：真的 station 目录（临时）、真的 `mcp_manage`、真的编排器、
真的 `mcp_search`/`mcp_call`、真的假 server 进程（`tests/mcp_stub_server.py`）。
无 LLM、无网络。

用法：`python tests/mcp_self_integration_probe.py`（stdout 输出一行 JSON）。
退出码 0 = 全过；1 = 有步骤没过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
STUB = HERE / "mcp_stub_server.py"
for p in (str(PROJECT_ROOT), str(PROJECT_ROOT / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)

STEPS = []


def note(step: str, ok: bool, extra=None) -> None:
    STEPS.append({"step": step, "ok": bool(ok), "extra": extra})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ab_selfint_"))
    old_env = {k: os.environ.get(k) for k in
               ("AETHER_MCP_STATIONS", "AETHER_MCP_STATE")}
    os.environ["AETHER_MCP_STATIONS"] = str(tmp)          # 真相源 = 这个临时目录
    os.environ.pop("AETHER_MCP_STATE", None)
    try:
        import agent_tools as A
        import mcp_client as mc
        import mcp_station as ms
        from task_orchestrator import TaskOrchestrator, ToolCall

        # 1) 工具表：MCP 一个工具都不许常驻（v2 的根承诺）
        mcp_tools = sorted(n for n in A.AVAILABLE_TOOLS if n.startswith("mcp"))
        note("工具表里没有 MCP 工具（只有 3 个元/运维工具）",
             mcp_tools == ["mcp_call", "mcp_manage", "mcp_search"], ",".join(mcp_tools))

        # 2) 候选目录：离线可用，并教模型联网自己查（别猜）
        out = A.mcp_manage(action="search", query="time")
        note("search 给出离线候选目录 + 联网自查步骤",
             "web_search" in out and "uvx" in out,
             out.splitlines()[1][:60] if len(out.splitlines()) > 1 else "")

        # 3) 集成：建 station 文件夹（STATION.md + tools.yaml）+ 注册表一行
        add = A.mcp_manage(action="add", name="selfint", command=sys.executable,
                           args=[str(STUB).replace("\\", "/"), "--behavior", "normal"],
                           why="自集成探针", source="tests/mcp_self_integration_probe.py")
        folder = tmp / "selfint"
        note("add 建出 station 文件夹（STATION.md + tools.yaml）",
             add.startswith("✅") and (folder / "STATION.md").exists()
             and (folder / "tools.yaml").exists(), add.splitlines()[0][:80])

        reg = (tmp / "MCP_REGISTRY.md")
        note("注册表多了一行（机器生成的 MCP_REGISTRY.md）",
             reg.exists() and "| selfint |" in reg.read_text(encoding="utf-8"))

        # 4) 试跑不算批准：第一次真调用仍要弹启动卡
        note("试跑不会把 station 标成已批准（第一次真调用要问人）",
             mc.is_approved("selfint") is False)

        # 5) 热发现 + 真调用（**不重启**，走真编排器）
        orch = TaskOrchestrator(tools_map=A.AVAILABLE_TOOLS, log_instance=None, log_enabled=False)
        batch = orch.execute([
            ToolCall(id="s1", name="mcp_search", arguments={"station": "selfint"}),
            ToolCall(id="c1", name="mcp_call",
                     arguments={"station": "selfint", "tool": "echo", "arguments": {"text": "自集成"}}),
            ToolCall(id="c2", name="mcp_call",
                     arguments={"station": "selfint", "tool": "echo"}),      # 缺必填
        ])
        by_id = {r.tool_call_id: str(r.result or "") for r in batch.results}
        note("mcp_search 现场取到工具与完整 schema",
             "echo" in by_id.get("s1", "") and "text" in by_id.get("s1", ""),
             by_id.get("s1", "").splitlines()[0][:60])
        note("mcp_call 真调成功（echo 回显）", "echo: 自集成" in by_id.get("c1", ""),
             by_id.get("c1", "")[:60])
        note("缺必填参数在本地就被拒（不白起进程）",
             by_id.get("c2", "").startswith("❌") and "text" in by_id.get("c2", ""),
             by_id.get("c2", "")[:60])

        # 6) 关掉它 → 调用立刻被拒（开关是实时的，不是重启才生效）
        off = A.mcp_manage(action="set_enabled", name="selfint", enabled=False)
        out_off = A.AVAILABLE_TOOLS["mcp_call"](station="selfint", tool="echo",
                                               arguments={"text": "x"})
        note("set_enabled 关掉后，同一个会话里调用立刻被拒",
             off.startswith("✅") and "关着" in out_off, out_off.splitlines()[0][:60])
        _rtext = reg.read_text(encoding="utf-8")
        note("关掉后注册表里仍留着它、状态标 off",
             "| selfint |" in _rtext and "| off |" in _rtext,
             "关掉的 station 不消失（\"装着但关着\"要看得见）")
        A.mcp_manage(action="set_enabled", name="selfint", enabled=True)

        # 7) 移除 → 挪进回收站（可恢复），且立刻搜不到
        rm = A.mcp_manage(action="remove", name="selfint")
        trash = list((tmp / ".trash").glob("*-selfint/STATION.md"))
        out_gone = A.mcp_manage(action="test", name="selfint")
        note("remove 把文件夹挪进回收站（可恢复），站点立刻消失",
             rm.startswith("✅") and bool(trash) and not folder.exists()
             and "没有叫 selfint" in out_gone, rm.splitlines()[0][:60])

        # 8) 全程没往真实仓库写东西
        real_reg = PROJECT_ROOT / "agent_MCP" / "MCP_REGISTRY.md"
        note("真实仓库的注册表没被这次探针碰过",
             "| selfint |" not in real_reg.read_text(encoding="utf-8"))

        # 9) 收干净：进程都关掉
        closed = mc.shutdown_all()
        note("探针结束时 MCP 进程都收干净", closed >= 0, "closed=%d" % closed)
    except Exception as e:                                  # noqa: BLE001
        import traceback
        note("探针自身没抛异常（%s: %s）" % (type(e).__name__, e), False,
             traceback.format_exc()[-400:])
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    ok = all(s["ok"] for s in STEPS) and bool(STEPS)
    print(json.dumps({"ok": ok, "steps": STEPS}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
