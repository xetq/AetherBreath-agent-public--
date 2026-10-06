# -*- coding: utf-8 -*-
"""mcp_gateway —— MCP 的两个元工具：`mcp_search`（取工具清单与 schema）与 `mcp_call`（调用）。

设计：`docs/MCP设计.md` v2 §五。**为什么只有两个元工具**（而不是把每个 MCP 工具注册成一等工具）：
  · 一个 station 的工具可能上百个（官方 GitHub MCP = 112 个），全量进工具表 = 每轮白吃几万 token；
  · 用不到的工具不该常驻上下文 —— 需要时现取（渐进式披露），不用就不花这份钱。
注入侧的配套：系统提示词里只放**注册表**（每 station 一行），工具 schema 一概不进。

调用链：`mcp_search` 读 station 文件夹（**实时、不缓存**，所以新建的 station 同会话就能用）
      → 模型拿到完整 `inputSchema` → `mcp_call` 走 `mcp_client`
      （懒启动 server 进程、同 station 串行、超时真 kill、结果降维 —— 那些是已验证资产，不重写）。

纪律（都是从"猜"这件事的代价来的）：
  · 不许靠记忆猜参数名：`mcp_search` 返回**完整 inputSchema**，`mcp_call` 缺必填参数时直接拒。
  · 不许拿近似名硬试：station/tool 不存在就如实说，并把现有的列出来（附最接近的候选）。
  · 任何异常都不抛出窗口（返回 ❌ 文本），否则编排器会把它当工具崩溃处理。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import mcp_client as mc
import mcp_station as ms

# 一次最多返回多少个工具的完整 schema（口径 3：超过就分页，防止一个 112 工具的 station 一次吐爆）
MAX_TOOLS_PER_PAGE = 60

# 传给 mcp_search 的 query 至少要多长（太短的词匹配噪音大）
_MIN_QUERY = 1


def _log(logger: Any, msg: str, **extra: Any) -> None:
    if logger is None:
        return
    try:
        lg = logger.with_span("mcp:gateway") if hasattr(logger, "with_span") else logger
        lg.info(msg, **extra)
    except Exception:
        pass


# ============================================================
# 1. 渲染（给人也看得懂的 schema）
# ============================================================

def _fmt_type(schema: Any) -> str:
    """把一个参数 schema 压成一行类型描述。"""
    if not isinstance(schema, dict):
        return "any"
    t = schema.get("type") or ("any" if "anyOf" not in schema else "any")
    if isinstance(t, list):
        t = "/".join(str(x) for x in t)
    if t == "array":
        items = schema.get("items")
        it = _fmt_type(items) if isinstance(items, dict) else "any"
        out = "%s<%s>" % (t, it)
    else:
        out = str(t)
    if schema.get("enum"):
        out += " 取值：" + "/".join(str(x) for x in schema["enum"][:12])
    return out


def _fmt_params(schema: Optional[Dict[str, Any]]) -> str:
    """把 inputSchema 渲染成参数清单（必填在前）。"""
    if not isinstance(schema, dict):
        return "  （没有声明参数）"
    props = schema.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return "  （没有参数）"
    required = set(schema.get("required") or [])
    lines: List[str] = []
    for key in sorted(props, key=lambda k: (k not in required, k)):
        sub = props.get(key) or {}
        desc = str(sub.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 180:
            desc = desc[:180] + "…"
        default = sub.get("default")
        bits = [_fmt_type(sub)]
        bits.append("必填" if key in required else "可选")
        if default is not None:
            bits.append("默认 %r" % (default,))
        lines.append("  - `%s`（%s）%s" % (key, "，".join(bits), ("：" + desc) if desc else ""))
    return "\n".join(lines)


def _render_tool(tool: Dict[str, Any]) -> str:
    name = str(tool.get("name") or "")
    desc = str(tool.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:400] + "…"
    return ("### %s\n%s\n参数：\n%s" % (name, desc or "（没有描述）",
                                    _fmt_params(tool.get("inputSchema"))))


def _station_line(st: ms.StationInfo) -> str:
    return "- `%s`：%s（工具 %d 个%s）" % (
        st.name, st.description or "（未写用途）", st.tools_count,
        "" if st.enabled else "，**已关闭**")


def _list_stations(found: List[ms.StationInfo], issues: List[str] = None) -> str:
    vis = [st for st in found if st.enabled and st.usable]
    if not vis:
        return "（没有可用的 station）"
    return "\n".join(_station_line(st) for st in vis)


def _not_found(name: str, found: List[ms.StationInfo]) -> str:
    names = sorted(st.name for st in found)
    hint = ""
    close = [n for n in names if name.lower() in n.lower() or n.lower() in name.lower()]
    if close:
        hint = "\n是不是想找：%s？" % "、".join("`%s`" % c for c in close[:5])
    return ("❌ 没有叫 `%s` 的 station。现有的：%s%s\n"
            "（station 名要写准；不确定就先 `mcp_search()` 看清单）"
            % (name, "、".join("`%s`" % n for n in names) or "（一个都没有）", hint))


# ============================================================
# 2. mcp_search —— 取某个 station 的工具与完整 schema
# ============================================================

def mcp_search(station: str = "", query: str = "", page: int = 1,
               logger: Any = None) -> str:
    """查 MCP 工具：给 station 名 → 返回它的工具与完整参数 schema；给 query → 返回候选 station。"""
    try:
        root = ms.stations_dir()
        found, issues = ms.scan_stations(root)
    except Exception as e:                                   # 绝不把异常抛给编排器
        return "❌ 读 station 目录失败：%s: %s" % (type(e).__name__, e)

    if not found:
        return ("MCP 还没有任何 station（一个 station = 一个 MCP server）。\n"
                "让我用 `mcp_manage` 去搜一个并集成，或手工建 "
                "`agent_MCP/<name>/STATION.md`（见 agent_MCP/README.md）。")

    name = str(station or "").strip()
    if name:
        st = next((s for s in found if s.name == name), None)
        if st is None:
            st = next((s for s in found if s.name.lower() == name.lower()), None)
        if st is None:
            return _not_found(name, found)
        if not st.enabled:
            return ("⚠️ station `%s` 是关着的（`STATION.md` 里 `enabled: false`）。\n"
                    "要开：把 enabled 改成 true，跑 `python agent/mcp_station.py` 同步注册表，"
                    "然后重启我（注入是会话快照）。" % st.name)
        if not st.usable:
            return ("⚠️ station `%s` 现在不可用，先修它：\n  - %s"
                    % (st.name, "\n  - ".join(st.problems) or "原因不明"))

        tools = st.tools or []
        total = len(tools)
        pages = max(1, (total + MAX_TOOLS_PER_PAGE - 1) // MAX_TOOLS_PER_PAGE)
        try:
            p = max(1, min(int(page or 1), pages))
        except Exception:
            p = 1
        chunk = tools[(p - 1) * MAX_TOOLS_PER_PAGE: p * MAX_TOOLS_PER_PAGE]
        head = ["station `%s`：%s" % (st.name, st.description or "（未写用途）"),
                "工具 %d 个（第 %d/%d 页）。调用用 `mcp_call(station=\"%s\", tool=..., arguments={...})`："
                % (total, p, pages, st.name), ""]
        body = [_render_tool(t) for t in chunk]
        tail = []
        if pages > 1:
            tail.append("（还有 %d 个工具：再调 `mcp_search(station=\"%s\", page=%d)`）"
                        % (total - p * MAX_TOOLS_PER_PAGE if p < pages else 0,
                           st.name, p + 1 if p < pages else p))
        _log(logger, "mcp_search 命中 station", station=st.name, tools=total, page=p)
        return "\n".join(head + body + tail)

    q = str(query or "").strip()
    if len(q) >= _MIN_QUERY:
        low = q.lower()
        hits = []
        for st in found:
            if not st.enabled or not st.usable:
                continue
            blob = " ".join([st.name, st.description] + [str(t.get("name")) for t in st.tools]
                            + [str(t.get("description") or "") for t in st.tools]).lower()
            if low in blob:
                hits.append(st)
        if not hits:
            return ("❌ 没搜到匹配 `%s` 的 station。\n现有的：\n%s\n"
                    "（也可以让我用 `mcp_manage` 去找一个合适的 server 集成进来）"
                    % (q, _list_stations(found)))
        if len(hits) == 1:
            st = hits[0]
            return ("匹配到 1 个 station：`%s`（%s，工具 %d 个）。\n"
                    "要工具清单就 `mcp_search(station=\"%s\")`。"
                    % (st.name, st.description or "未写用途", st.tools_count, st.name))
        return ("匹配到 %d 个 station（挑一个再查它的工具）：\n%s"
                % (len(hits), "\n".join(_station_line(st) for st in hits)))

    _log(logger, "mcp_search 列出 station", count=len(found))
    out = ["已注册的 MCP 服务站（一个 station = 一个 server）：", _list_stations(found),
           "", "要某个 station 的工具与参数 schema：`mcp_search(station=\"<名字>\")`"]
    if issues:
        out.append("（提示：%s）" % "；".join(issues[:3]))
    return "\n".join(out)


# ============================================================
# 3. mcp_call —— 调用
# ============================================================

def _parse_arguments(arguments: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """把 arguments 规整成 dict（模型有时会把 JSON 写成字符串）。"""
    if arguments in (None, "", {}):
        return {}, None
    if isinstance(arguments, dict):
        return dict(arguments), None
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}, None
        try:
            parsed = json.loads(text)
        except Exception as e:
            return None, ("❌ arguments 是个字符串但不像 JSON（%s）。要么直接给对象，"
                          "要么给合法的 JSON 文本。拿到的是：%s"
                          % (type(e).__name__, text[:120]))
        if not isinstance(parsed, dict):
            return None, "❌ arguments 必须是对象（拿到 JSON %s）" % type(parsed).__name__
        return parsed, None
    return None, "❌ arguments 只能是对象或 JSON 字符串（拿到 %s）" % type(arguments).__name__


def _missing_env(st: Any) -> List[str]:
    """这个 station 的配置里有哪些 `${VAR}` 当前取不到值（**不起进程**就能判断）。

    为什么要有：没有 token 时，真去起进程 + 调工具会**白等一整个超时**（实测 60s）才报错。
    这种"缺东西"应当在调用前就说清、并告诉去哪配（fail-closed + 顺带教会主人怎么装）。
    """
    missing: List[str] = []
    try:
        mc.expand_env(str(getattr(st, "command", "") or ""), missing)
        for a in (getattr(st, "args", None) or []):
            mc.expand_env(str(a), missing)
        for v in (getattr(st, "env", None) or {}).values():
            mc.expand_env(str(v), missing)
    except Exception:                                        # noqa: BLE001
        return []
    return sorted(set(missing))


def mcp_call(station: str = "", tool: str = "", arguments: Any = None,
             logger: Any = None) -> str:
    """调用某个 station 的某个工具（参数先 mcp_search 看清楚，别猜）。"""
    name = str(station or "").strip()
    tname = str(tool or "").strip()
    if not name or not tname:
        return ("❌ mcp_call 需要 `station` 与 `tool` 两个参数。\n"
                "先用 `mcp_search()` 看有哪些 station，再 `mcp_search(station=\"...\")` 看工具。")
    try:
        st = ms.load_station(name)                     # 实时读盘（新建的 station 同会话可用）
    except Exception as e:
        return "❌ 读 station `%s` 失败：%s: %s" % (name, type(e).__name__, e)
    if st is None:
        found, _ = ms.scan_stations()
        return _not_found(name, found)
    if not st.enabled:
        return ("⚠️ station `%s` 是关着的（`STATION.md` 里 `enabled: false`），先开再调。" % name)
    if not st.usable:
        return ("⚠️ station `%s` 现在不可用，先修它：\n  - %s"
                % (name, "\n  - ".join(st.problems) or "原因不明"))
    miss = _missing_env(st)
    if miss:
        return ("❌ station `%s` 缺环境变量：%s\n"
                "  它们写在 `%s` 的 `env:` 里，值从 `.env` 或系统环境变量展开。\n"
                "  配好再调（GitHub 的 token 怎么申请：见 `docs/MCP-GitHub接入.md`）。"
                % (name, "、".join(miss), ms.station_doc(name)))

    tool_names = [str(t.get("name")) for t in (st.tools or [])]
    if tname not in tool_names:
        low = tname.lower()
        close = [n for n in tool_names if low in n.lower() or n.lower() in low]
        tip = ("是不是想调：%s？" % "、".join("`%s`" % c for c in close[:5])) if close else \
              ("它有 %d 个工具，用 `mcp_search(station=\"%s\")` 看清单。" % (len(tool_names), name))
        return "❌ station `%s` 里没有工具 `%s`。%s" % (name, tname, tip)

    args, err = _parse_arguments(arguments)
    if err:
        return err
    spec = next((t for t in st.tools if str(t.get("name")) == tname), {})
    schema = spec.get("inputSchema") if isinstance(spec, dict) else {}
    required = [k for k in ((schema or {}).get("required") or [])]
    missing = [k for k in required if k not in args]
    if missing:
        return ("❌ 缺必填参数：%s\n\n%s\n\n（参数名照抄上面这份 schema，别猜）"
                % ("、".join("`%s`" % m for m in missing), _render_tool(spec)))

    _log(logger, "mcp_call 开始", station=name, tool=tname,
         args=",".join(sorted(args)) or "（无参数）")
    try:
        out = mc.call_tool(name, tname, args, logger=logger)
    except Exception as e:                                   # 兜底：绝不抛给编排器
        _log(logger, "mcp_call 异常", station=name, tool=tname, err=repr(e))
        mc.record_call(name, tname, False)   # 抛出来的也算失败一次（面板要如实）
        return "❌ 调用 station `%s` 的工具 `%s` 时内部异常：%s: %s" % (name, tname, type(e).__name__, e)
    ok = not str(out or "").startswith("❌")
    mc.record_call(name, tname, ok)     # 只给界面看：调用了哪个 station 的哪个工具（Q26）
    _log(logger, "mcp_call 结束", station=name, tool=tname, ok=ok)
    return out


# ============================================================
# 4. 工具声明（给模型看的 schema）
# ============================================================

mcp_search_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mcp_search",
        "description": (
            "查 MCP 服务站（station）的工具与参数。MCP 工具**不进常驻工具表**，"
            "所以要调用前先在这里查 schema：station=\"名字\" → 返回该 station 的工具与完整参数 schema；"
            "query=\"关键词\" → 返回匹配的候选 station；两个都不给 → 列出所有 station。"
            "**别凭记忆猜工具名与参数名**。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "station": {"type": "string",
                            "description": "station 名（注册表里 `station` 那一列，要写准）"},
                "query": {"type": "string",
                          "description": "按关键词找 station（名字/用途/工具名/工具描述里包含即可）"},
                "page": {"type": "integer",
                         "description": "工具太多时翻页（一次最多 %d 个，默认第 1 页）"
                                        % MAX_TOOLS_PER_PAGE},
            },
            "required": [],
        },
    },
}

mcp_call_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mcp_call",
        "description": (
            "调用某个 MCP 服务站（station）里的一个工具。参数名照 `mcp_search(station=...)` 给的 schema，"
            "不要猜。第一次调用某个 station 时可能要等主人批准（会弹一张卡，上面是真实命令行）,全部批准后审批细节不传回；"
            "server 进程由我托管（懒启动、同一 station 串行、超时真 kill）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "station": {"type": "string", "description": "station 名"},
                "tool": {"type": "string", "description": "工具名（该 station 的，先 mcp_search 查）"},
                "arguments": {"type": "object",
                              "description": "工具参数对象（键名照 schema；也可以给 JSON 字符串）"},
            },
            "required": ["station", "tool"],
        },
    },
}
