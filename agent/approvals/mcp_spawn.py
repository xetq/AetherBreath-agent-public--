# -*- coding: utf-8 -*-
"""规范：启动 MCP server（本地子进程）需要人工确认一次。

v2（`docs/MCP设计.md` §七）里 MCP 只从**两个元工具**进来：`mcp_search`（只读 station
文件夹，不起进程）与 `mcp_call`（真的 `Popen` 一个本地可执行文件并调用工具）。缺口：
`mcp_call` 会启动 npx/uvx/python 这类本地可执行文件，等价于 `execute_shell`，
而它的参数里通常**没有路径**，路径类规范够不着它。

取向：

  · **只问不拒** —— MCP 是正当需求，只要人点头；
  · **只问一次** —— 该 station 的进程一旦成功启动过（= 人已经批过那条命令行），
    本进程内之后不再打扰。每次调用都弹卡会把主人训练成闭眼点「允许」（见 approvals/README.md）；
  · 卡片显示**将要执行的真实命令行**（${VAR} 展开后）+ 要调哪个 station 的哪个工具 + 参数摘要；
  · `mcp_search` **不弹卡**（它只读文件夹，不执行任何第三方代码）；
  · 入口只有一个：**`tool == "mcp_call"` 且参数里带 `station`**（v2 起 MCP 工具名不进
    常驻工具表，所以不存在别的形态）。

⚠️ 一个刻意的实现选择：**"已批准"时返回 `None`，而不是 `{"quiet": True}`。**
   引擎在第一个命中的规范处 `break`，而 `quiet` 也是"命中" —— 那会让本规范
   **遮蔽**后面那些规范（`cdrive.files` / `engine.selfmodify`）的 ask：
   一旦某个 station 被批过一次，用它去写系统盘就再也没人问了。
   返回 `None` 只表示"本规范这次无话可说"，不干扰其它规范判定。

关于参数里的路径：`mcp_call` 的参数是 `{station, tool, arguments:{...}}`，
`approval.extract_paths` 会扫到 `arguments` 里的字符串（含嵌套 dict 的 str 形式），
所以"用 MCP 去写系统盘"这类事**仍会被 `cdrive.files` 拦住** —— 有测试钉住。
（2026-10：`outzone.write` / `secrets_read` 已随四档权限模式下线，原先写在这里的
两个 KIND 名随之删掉；盘外防护现在由「工作区模式」的硬边界承担。）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

KIND = "mcp.spawn"
TITLE = "启动 MCP server（本地子进程）"
RISK = 2

_GATEWAY_TOOL = "mcp_call"          # MCP 调用的唯一入口
_MAX_ARG_CHARS = 160


def _client():
    """取客户端模块（拿它的审批状态与命令行预告）。

    真实运行时 agent/ 在 sys.path 上（agent.py 就是扁平导入），直接命中；
    独立场景（例如单测）拿不到 —— 那时**按未批准处理**（老老实实弹卡）。
    """
    try:
        import mcp_client                      # noqa: PLC0415（运行时扁平导入）
        return mcp_client
    except Exception:
        return None


def applies(ctx: Dict[str, Any]) -> bool:
    """便宜初筛：MCP 只有 `mcp_call` 这一个入口与本类相关。"""
    return str(ctx.get("tool") or "") == _GATEWAY_TOOL


def _summarize_args(kwargs: Dict[str, Any]) -> str:
    parts = []
    for k, v in list((kwargs or {}).items())[:6]:
        s = str(v)
        if len(s) > 40:
            s = s[:40] + "…"
        parts.append("%s=%s" % (k, s))
    out = "，".join(parts)
    return out[:_MAX_ARG_CHARS] + ("…" if len(out) > _MAX_ARG_CHARS else "")


def _station_of(ctx: Dict[str, Any]) -> str:
    """本次调用要启动哪个 station 的进程？—— 入口只有一个：`kwargs["station"]`。"""
    return str((ctx.get("kwargs") or {}).get("station") or "").strip()


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not applies(ctx):
        return None

    client = _client()
    kwargs = dict(ctx.get("kwargs") or {})
    server = _station_of(ctx)
    inner = str(kwargs.get("tool") or "").strip()

    # 该 station 已经批准并成功启动过 → 这次无话可说（见文件头：刻意返回 None）
    if server and client is not None:
        try:
            if client.is_approved(server):
                return None
        except Exception:
            pass                               # 拿不到状态 → 按未批准处理（fail-closed）

    cmd = ""
    if server and client is not None:
        try:
            cmd = client.command_line_preview(server)
        except Exception:
            cmd = ""

    notes = []
    if server:
        if cmd:
            notes.append("将要执行的命令行：" + cmd)
        else:
            notes.append("⚠️ 没能取到 station「%s」的启动命令 —— 批准前先确认它的 "
                         "`STATION.md`（command/args）写对了" % server)
    else:
        notes.append("⚠️ 这次调用的参数里没有 `station`，无法确认要启动哪个 server —— "
                     "按未批准处理（fail-closed）")
    notes.append("批准后本进程内**不再重复询问**该 station（要收回：重启 agent，"
                 "或在代码里调 mcp_client.revoke_approval）")
    notes.append("⚠️ 闸门看得见的是本次调用的参数（含 `arguments` 里的路径，会被路径类规范扫到），"
                 "但看不见 server 拿到参数后在自己进程里做了什么 —— 别把不受信任的 server 配进来")

    return {
        "quiet": False,
        "action": "启动 MCP server",
        "targets": [],                          # 没有文件目标可报，不许编一个
        "intent": "启动 MCP server「%s」并调用工具 %s%s" % (
            server or "(未知)", inner or "(未写)",
            ("（参数：%s）" % _summarize_args(kwargs.get("arguments") or {}))),
        "reason": "MCP server 是本地子进程（等同执行可执行文件），首次启动需主人确认",
        "notes": notes,
        "critical": False,
    }
