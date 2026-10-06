# -*- coding: utf-8 -*-
"""MCP 服务站快照 —— 给「运行时」面板的数据组装（**不依赖 fastapi**，可单测）。

为什么单独一个模块：网关跑在 `venv-gateway`（有 fastapi），测试跑在主 venv（没有）。
把组装逻辑放这里、`api.py` 只做一层薄包装 → 测试能直接 import 它
（`tests/test_mcp_stations_api.py`），不用为了测一段字典拼装去装 fastapi。

口径（主人 2026-09-13 对面板的三条硬要求）：
  · **常驻** —— 没起过进程的站也要出现（`alive=false` / `calls=0`），不能"先发一条消息才有数"；
  · 只放**会变**的量（开关、工具数、在线、调用次数、最近一次调用），不堆累计空指标；
  · 只读盘 + 读运行时表，**不起任何进程**；出错如实返回 `ok=false`（面板照实显示，不装死）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# agent/ 在仓库根下（agent_webui/backend/mcp_view.py → parents[2]）。网关进程通常已经把它
# 放进 sys.path 了，这里兜底；测试里也一样能用。
_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"


def _mcp_modules() -> Tuple[Any, Any]:
    if str(_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_DIR))
    import mcp_client as mc                            # noqa: PLC0415
    import mcp_station as ms                           # noqa: PLC0415
    return ms, mc


def _read_runtime_snapshot(ms: Any) -> Tuple[Dict[str, Any], Optional[str], Optional[int]]:
    """读 **agent 侧**落盘的运行快照（`<stations>/.state/runtime.json`）。

    **为什么不能在本进程直接读连接表**：面板跑在**网关进程**里，而 MCP 连接活在
    **bridge/agent 进程**里 —— 网关这份 `mcp_client` 连接表永远是空的。实测过两个后果：
    面板一直显示"未起 / 0 次"；以及**误报**"缺 `GITHUB_PERSONAL_ACCESS_TOKEN`"（网关没加载
    agent 的 `.env`，但 agent 侧明明有）。所以运行状态一律以 agent 的快照为准。

    读不到就返回空 → 面板如实说"运行时未知"，**不猜、不误报**。
    """
    try:
        path = ms.state_dir() / "runtime.json"
        if not path.exists():
            return {}, None, None
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("servers") or {}), data.get("at"), data.get("pid")
    except Exception:                                   # noqa: BLE001
        return {}, None, None


def stations_snapshot() -> Dict[str, Any]:
    """station 目录 + 每个站的开关/工具数/进程状态/最近一次调用，**一次给全**。

    返回结构（前端 `McpStationsResp`）：
      ok / enabled / stations_dir / registry / count / online / on_switch / total_tools
      / issues / stations[{name,description,origin,enabled,usable,tools,problems,alive,
      calls,missing_env,last_tool,last_call_at,last_call_ok,server_version}]
    """
    try:
        ms, mc = _mcp_modules()
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "MCP 模块不可用：%s: %s" % (type(e).__name__, e),
                "stations": [], "enabled": False}
    try:
        snap, snap_at, snap_pid = _read_runtime_snapshot(ms)
        found, issues = ms.scan_stations()
        rows: List[Dict[str, Any]] = []
        for st in sorted(found, key=lambda s: s.name):
            rt = snap.get(st.name) or {}            # 运行态一律来自 agent 的落盘快照
            rows.append({
                "name": st.name,
                "description": st.description,
                "origin": st.origin,                   # hand = 主人手写；auto = AB 自己集成
                "enabled": bool(st.enabled),
                "usable": bool(st.usable),
                "tools": len(st.tools),
                "problems": list(st.problems),
                "alive": bool(rt.get("alive")),
                "calls": int(rt.get("calls") or 0),
                "missing_env": list(rt.get("missing_env") or []),
                "last_tool": rt.get("last_tool"),
                "last_call_at": rt.get("last_call_at"),
                "last_call_ok": rt.get("last_call_ok"),
                "server_version": str(rt.get("server_version") or ""),
            })
        return {
            "ok": True,
            "enabled": ms.mcp_enabled(),               # 总闸（config.yaml 的 mcp.enabled）
            "stations_dir": str(ms.stations_dir()),
            "registry": ms.registry_path().name,
            "count": len(rows),
            "online": sum(1 for r in rows if r["alive"]),
            "on_switch": sum(1 for r in rows if r["enabled"]),
            "total_tools": sum(r["tools"] for r in rows),
            "issues": list(issues),
            "snapshot_at": snap_at,                   # agent 侧运行快照的时间（null = 还没跑过）
            "runner_pid": snap_pid,                   # 写快照的 agent 进程
            "stations": rows,
        }
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "扫描 station 失败：%s: %s" % (type(e).__name__, e),
                "stations": []}
