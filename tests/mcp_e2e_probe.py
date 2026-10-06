# -*- coding: utf-8 -*-
"""MCP 端到端探针 v2（真编排器 + 真 station 层 + 假 server，**无 LLM、无网络**）。

它回答一个问题：**v2 的两个元工具是不是真的在系统里跑起来了**？为此用的是
**真的** `agent_tools.AVAILABLE_TOOLS`、**真的** `TaskOrchestrator`、**真的** station 扫描
与 `mcp_client`，只把 MCP server 换成 `tests/mcp_stub_server.py`（本机 python 拉的假 server）。

验证六件事：
  1. **工具表里没有 MCP 工具**（MCP 工具一律不进常驻工具表）——这是 v2 的核心 KPI；
  2. 两个元工具（`mcp_search` / `mcp_call`）确实在工具表里，`MCP_REGISTRATION` 报告无错误；
  3. `mcp_search` 从 station 文件夹里**现场**取到工具与 schema（读盘，不读注册表）；
  4. 执行链：`orchestrator.execute()` → 模板 → 管道 → `mcp_call` → 客户端 → 结果回到调用方；
  5. 失败语义：`isError: true` 走 ❌ 前缀 → 编排器算作失败（界面不虚报成功率）；
     以及"缺必填参数"在**本地**就被拒（不白起一趟进程）；
  6. **logger 自动注入** + 假 server 进程真的被拉起、能收干净。

跑法：venv/Scripts/python tests/mcp_e2e_probe.py        # 打印 JSON 摘要，退出码 0 = 全过
被 tests/test_mcp_client.py 的 `test_end_to_end_through_task_orchestrator` 纳管。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
STUB = Path(__file__).resolve().parent / "mcp_stub_server.py"
for p in (AGENT_DIR, PROJECT_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


class FakeLogger:
    """假的会话日志：记录 span/事件，用来证明"编排器真的把 logger 注进去了"。"""

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


_TOOLS = [
    {"name": "echo", "description": "回显",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
]


def build_fixture():
    """临时 station 目录：一个正常 server + 一个总是报错的 server（都用假 server 当命令）。"""
    import mcp_station as ms

    tmp = Path(tempfile.mkdtemp(prefix="mcp_e2e_"))

    def add(name, behavior):
        folder = tmp / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "STATION.md").write_text(
            "---\nname: %s\ndescription: 端到端探针用的假 station（%s）\n"
            "command: %s\nargs: [%r, \"--behavior\", %r]\nenabled: true\ntimeout: 10\n"
            "origin: hand\n---\n\n# %s\n\n探针用。\n"
            % (name, behavior, json.dumps(sys.executable),
               str(STUB).replace("\\", "/"), behavior, name),
            encoding="utf-8")
        ms.write_tools_file(name, _TOOLS, {"protocol_version": "test"}, stations=tmp)

    add("e2e_ok", "normal")
    add("e2e_err", "error")
    return tmp


def main() -> int:
    tmp = build_fixture()
    os.environ["AETHER_MCP_STATIONS"] = str(tmp)

    result = {"ok": False, "steps": [], "problems": []}

    def note(step, ok, detail=""):
        result["steps"].append({"step": step, "ok": bool(ok), "detail": detail})
        if not ok:
            result["problems"].append("%s：%s" % (step, detail))

    # ---- 1. 工具表里**不该**有 MCP 工具（v2 的核心） ----
    import agent_tools as A
    mcp_names = sorted(n for n in A.AVAILABLE_TOOLS if n.startswith("mcp"))
    note("v2：MCP 工具不再进常驻工具表",
         mcp_names == ["mcp_call", "mcp_manage", "mcp_search"],
         "（注册进来的：%s）" % ",".join(mcp_names))
    note("两个元工具在工具表里",
         {"mcp_search", "mcp_call"} <= set(A.AVAILABLE_TOOLS),
         ", ".join(sorted(n for n in A.AVAILABLE_TOOLS if n.startswith("mcp"))))
    schema_names = sorted(s["function"]["name"] for s in A.TOOLS_SCHEMA
                          if s["function"]["name"].startswith("mcp"))
    note("两个元工具的 schema 也在（且只有这 3 个 mcp 工具）",
         {"mcp_search", "mcp_call"} <= set(schema_names)
         and set(schema_names) <= {"mcp_search", "mcp_call", "mcp_manage"},
         ",".join(schema_names))
    note("MCP 自检报告无错误", not A.MCP_REGISTRATION.get("errors"),
         "; ".join(A.MCP_REGISTRATION.get("errors") or [])[:200])
    note("自检报告是 station 模式", A.MCP_REGISTRATION.get("mode") == "station",
         json.dumps({k: v for k, v in A.MCP_REGISTRATION.items() if k != "problems"},
                    ensure_ascii=False))

    # ---- 2. 真编排器执行（两个元工具走同一条管道） ----
    from task_orchestrator import TaskOrchestrator, ToolCall
    log = FakeLogger()
    orch = TaskOrchestrator(tools_map=A.AVAILABLE_TOOLS, log_instance=log, log_enabled=True)
    try:
        batch = orch.execute([
            ToolCall(id="c1", name="mcp_search", arguments={"station": "e2e_ok"}),
            ToolCall(id="c2", name="mcp_call",
                     arguments={"station": "e2e_ok", "tool": "echo", "arguments": {"text": "编排器"}}),
            ToolCall(id="c3", name="mcp_call",
                     arguments={"station": "e2e_err", "tool": "echo", "arguments": {"text": "x"}}),
            ToolCall(id="c4", name="mcp_call",
                     arguments={"station": "e2e_ok", "tool": "echo"}),
        ])
        by_id = {r.tool_call_id: r for r in batch.results}
        note("mcp_search 现场取到工具与 schema",
             "echo" in str(by_id["c1"].result) and "text" in str(by_id["c1"].result),
             repr(by_id["c1"].result)[:120])
        note("mcp_call 正常调用（真起了假 server）",
             str(by_id["c2"].result).strip() == "echo: 编排器", repr(by_id["c2"].result)[:120])
        note("isError 走 ❌ 前缀（biz_fail）",
             bool(by_id["c3"].biz_fail) and str(by_id["c3"].result).startswith("❌"),
             repr(by_id["c3"].result)[:120])
        note("缺必填参数在本地就被拒（不白起进程）",
             str(by_id["c4"].result).startswith("❌") and "必填" in str(by_id["c4"].result),
             repr(by_id["c4"].result)[:120])
        note("计数不虚报（成功 2 / 失败 2）",
             batch.success_count == 2 and batch.failure_count == 2,
             "success=%d failure=%d" % (batch.success_count, batch.failure_count))
    finally:
        orch.shutdown()

    # ---- 3. logger 真的被注入了吗 ----
    mcp_events = [r for r in log.records if r["span"].startswith("mcp:")]
    note("编排器按签名注入 logger（span=mcp:*）", bool(mcp_events),
         "%d 条 mcp 事件，例：%s" % (len(mcp_events), mcp_events[0]["msg"] if mcp_events else ""))
    note("编排器自身日志也在同一 logger 上",
         any(r["msg"].startswith("[Orchestrator]") for r in log.records),
         "%d 条编排器日志" % sum(1 for r in log.records if r["msg"].startswith("[Orchestrator]")))

    # ---- 4. 进程确实起了、并且能收干净 ----
    import mcp_client as mc
    alive = mc.peek_server_info("e2e_ok").get("alive") is True
    note("假 server 进程确实被拉起", alive,
         json.dumps(mc.peek_server_info("e2e_ok"), ensure_ascii=False))
    closed = mc.shutdown_all()
    note("shutdown 能收干净", closed >= 1, "关闭 %d 个" % closed)

    result["ok"] = not result["problems"]
    result["fixture"] = str(tmp)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
