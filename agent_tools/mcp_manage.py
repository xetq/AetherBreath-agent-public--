# -*- coding: utf-8 -*-
"""mcp_manage —— 让 AB **自己集成** MCP server（自维护）。

设计：`agent_workspace/agent_self_maintenance/DESIGN.md`；station 与用法：`agent_MCP/README.md`。

六个动作（工具参数 `action`）：

| action | 干什么 |
|---|---|
| `search` | 给一份离线候选目录 + 告诉 AB 怎么继续找（它自己有 web_search） |
| `list` | station 目录里每个 station 的开关/来源/工具数/进程状态 |
| `add` | 集成一个新 station：校验 → **固定版本** → 建 `<station>/` 文件夹 → **连通性测试**（装完不用重启） |
| `test` | 对已有 station 跑连通性测试（并可选真调一次工具） |
| `set_enabled` | 开关某个 station（只允许改 AB 自己集成的） |
| `remove` | 移除某个 station（只允许改 AB 自己集成的；整个文件夹挪进 `.trash/`，可恢复） |

真相源只有一个：`agent_MCP/<station>/`（一个 server = 一个文件夹，**文件夹存在即注册**）。

安全与纪律（每条都有理由，别随手改）：

· **只动 `origin: auto` 的 station**（AB 自己集成的那份）。主人手写的 `STATION.md`
  **永不**被机器改写 —— PyYAML 写回会抹掉那里的注释与排版，主人的解释就没了。
· **集成这一步不设专门闸门**（主人 2026-09-14 的决定）：`add` 会联网下载并**真跑**该包做
  连通性测试 —— 等同执行第三方可执行文件。所以**动手前先跟主人说清**：包名、来源、会联网
  下载、失败会回滚。（这是 AB 自己的纪律；引擎不再为此弹卡。）
  另有一道门仍在：试跑后会**撤销"已批准"**，所以**第一次真正用它干活时**，主人会收到
  `mcp.spawn` 的启动确认卡 —— 卡上写的就是那条真实命令行。
· **版本必须固定**（`npx pkg@x.y.z` / `uvx pkg==x.y.z`）：不锁版本 = 上游什么时候塞进来
  什么都认。探不到版本就报错让人来（用 web_search 查），**绝不**默默用 latest。
· **有界自愈**：同一 station 连续失败 `MAX_ATTEMPTS` 次就停手，并把这回新建的文件夹**删掉**
  —— 不留坏配置，也不无限重试烧时间。要继续必须显式 `force=True`。
· 一切失败返回 **`❌` 开头**的文本（编排器靠它识别业务失败，界面才不会虚报成功）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AGENT_DIR = PROJECT_ROOT / "agent"
if str(_AGENT_DIR) not in sys.path:              # 与运行时同一套扁平导入（模块身份唯一）
    sys.path.insert(0, str(_AGENT_DIR))

import mcp_client as mc                          # noqa: E402

MAX_ATTEMPTS = 2                                 # 有界自愈：同一 station 连续失败几次就停
# 失败计数状态：**跟着 station 目录走**（与 `.trash/`、`tools.yaml` 同一套规则）。
# 以前写死成项目根下的固定路径 —— 那样把 station 目录指到别处时（测试、多套配置）
# 状态会写到不相干的地方，测试也会往真实仓库里落文件。
STATE_REL = ".state/self_integration.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PIN_RE = re.compile(r"(@\d|==|>=|<=|~=|!=)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 离线候选目录：给 AB 一个起点，不用每次都从零搜。
# ⚠️ 除标注"本机实测"的那条，其余都**没有**在本机跑过 —— 装之前的连通性测试才是真验证。
CATALOG: List[Dict[str, str]] = [
    {"name": "time", "package": "mcp-server-time", "kind": "uvx",
     "why": "查时区/时间换算（本机实测可用：工具 get_current_time / convert_time）"},
    {"name": "files", "package": "@modelcontextprotocol/server-filesystem", "kind": "npx",
     "why": "按目录授权读写文件（官方 Node 参考实现，未实测）"},
    {"name": "fetch", "package": "mcp-server-fetch", "kind": "uvx",
     "why": "抓网页并转成 markdown（官方 Python 参考实现，未实测）"},
    {"name": "git", "package": "mcp-server-git", "kind": "uvx",
     "why": "查看/操作本地 git 仓库（官方 Python 参考实现，未实测）"},
    {"name": "memory", "package": "@modelcontextprotocol/server-memory", "kind": "npx",
     "why": "知识图谱式记忆（官方 Node 参考实现，未实测）"},
    {"name": "everything", "package": "@modelcontextprotocol/server-everything", "kind": "npx",
     "why": "官方自测用 server：各种协议特性都有（未实测）"},
]


class PinError(Exception):
    """版本固定失败（探不到版本号）。"""


# 只有这些"包管理器"命令才谈得上固定版本；其它命令（如直接跑一个脚本/二进制）
# 固定版本这回事不适用 —— 硬塞 "pkg==1.2.3" 只会把命令弄坏。
_PACKAGE_RUNNERS = ("npx", "npm", "pnpm", "bunx", "yarn", "uvx", "uv", "pipx")


def _is_runner(command: str) -> bool:
    return _base_cmd(command).startswith(_PACKAGE_RUNNERS)


# ============================================================
# 1. 日志 / 状态文件
# ============================================================

def _log(logger: Any, msg: str, **extra: Any) -> None:
    if logger is None:
        return
    try:
        lg = logger.with_span("mcp:manage") if hasattr(logger, "with_span") else logger
        lg.info(msg, **extra)
    except Exception:
        pass


def _state_file() -> Path:
    """失败计数文件位置：环境变量 AETHER_MCP_STATE > station 目录的 `.state/`。

    与 `.trash/`、station 文件夹同一条纪律：**都跟着 station 目录走**，
    这样把目录指到别处（测试、多套配置）时不会把状态写进真实仓库。
    """
    env = (os.environ.get("AETHER_MCP_STATE") or "").strip()
    if env:
        return Path(env)
    import mcp_station as ms                     # noqa: PLC0415
    return ms.state_dir() / Path(STATE_REL).name


def _load_state() -> Dict[str, Any]:
    p = _state_file()
    if not p.exists():
        return {"attempts": {}, "last_error": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"attempts": {}, "last_error": {}}
    except Exception:
        return {"attempts": {}, "last_error": {}}


def _save_state(st: Dict[str, Any]) -> None:
    p = _state_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _attempts(name: str) -> int:
    return int(_load_state().get("attempts", {}).get(str(name), 0) or 0)


def _note_attempt(name: str, err: str) -> int:
    st = _load_state()
    n = int(st.setdefault("attempts", {}).get(name, 0) or 0) + 1
    st["attempts"][name] = n
    st.setdefault("last_error", {})[name] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                            "err": str(err)[:400]}
    _save_state(st)
    return n


def _clear_attempts(name: str) -> None:
    st = _load_state()
    st.setdefault("attempts", {}).pop(name, None)
    st.setdefault("last_error", {}).pop(name, None)
    _save_state(st)


# ============================================================
# 6. 版本固定（不锁版本 = 供应链风险）
# ============================================================

def _base_cmd(command: str) -> str:
    return Path(str(command)).name.lower()


def _is_npm_like(command: str) -> bool:
    base = _base_cmd(command)
    return base.startswith(("npx", "npm"))


def _package_arg(args: List[str]) -> Optional[str]:
    """从 argv 里挑出"包"参数（跳过 -y / --yes 之类的开关）。"""
    for a in (args or []):
        s = str(a)
        if s.startswith("-"):
            continue
        return s
    return None


def _is_pinned(command: str, pkg: str) -> bool:
    if _is_npm_like(command):
        body = pkg[1:] if pkg.startswith("@") else pkg      # @scope/name 的第一个 @ 不算版本
        return "@" in body
    return bool(_PIN_RE.search(pkg))


def _pin_expr(command: str, pkg: str, version: str) -> str:
    return "%s@%s" % (pkg, version) if _is_npm_like(command) else "%s==%s" % (pkg, version)


def _probe_version(command: str, pkg: str) -> Optional[str]:
    """查包的最新版本号（**只读查询**：npm view / pip index，不装东西）。"""
    try:
        if _is_npm_like(command):
            exe = shutil.which("npm") or shutil.which("npm.cmd")
            if not exe:
                return None
            argv = [exe, "view", pkg, "version"]
        else:
            argv = [sys.executable, "-m", "pip", "index", "versions", pkg]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=90,
                              cwd=str(PROJECT_ROOT), creationflags=_NO_WINDOW,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if _is_npm_like(command):
            m = re.search(r"\b(\d+\.\d+[0-9A-Za-z.\-+]*)", out)
            return m.group(1) if m else None
        m = re.search(r"%s\s*\(([^)]+)\)" % re.escape(pkg), out) or \
            re.search(r"Available versions:\s*([0-9][0-9A-Za-z.\-+]*)", out)
        if m:
            return m.group(1).split(",")[0].strip()
        return None
    except Exception:
        return None


def _pin_args(command: str, args: List[str]) -> Tuple[List[str], str]:
    """把版本固定进 args。已经固定就原样返回；探不到版本 → 抛 PinError。

    只对**包管理器命令**（uvx/npx/…）做这件事：别的命令（例如直接跑一个本地脚本）
    没有"版本"可言，硬改参数只会把它弄坏。那种情况如实说明，让人自己保证版本。
    """
    args = list(args or [])
    if not _is_runner(command):
        return args, "（%s 不是包管理器命令，跳过版本固定 —— 请自己确保它跑的是固定版本）" % _base_cmd(command)
    pkg = _package_arg(args)
    if not pkg:
        return args, "（没有包参数，跳过版本固定）"
    if _is_pinned(command, pkg):
        return args, "版本已固定：%s" % pkg
    ver = _probe_version(command, pkg)
    if not ver:
        raise PinError(pkg)
    new_pkg = _pin_expr(command, pkg, ver)
    out = []
    for a in args:
        out.append(new_pkg if str(a) == pkg else a)
    return out, "已固定到最新版：%s" % new_pkg


# ============================================================
# 3. 连通性测试 / 生效时机
# ============================================================

def _stderr_tail(name: str, n: int = 6) -> List[str]:
    conn = mc._CONNS.get(str(name))
    if conn is None:
        return []
    try:
        return conn.stderr_tail(n)
    except Exception:
        return []


def _tool_names_of(name: str) -> List[str]:
    """某个 station 声明的工具名（裸名，不带任何前缀 —— station 里的名字就是模型要传的名字）。

    直接从 station 目录读盘（跟 `mcp_search` / `mcp_call` 同一个真相源）。
    """
    import mcp_station as ms
    st = ms.load_station(str(name))
    if st is None:
        return []
    return [str(t.get("name") or "") for t in (st.tools or []) if str(t.get("name") or "")]


# ============================================================
# 3b. station 的写路径（一个 server = 一个文件夹）
# ============================================================
#   · 集成 = 建 `agent_MCP/<name>/`（STATION.md 手写风 + tools.yaml 机器生成），不是写 YAML 表；
#   · 装完**不用重启** —— 根因：mcp_search/mcp_call 每次都实时读文件夹，
#     不存在"把工具塞进常驻工具表"这回事；
#   · 只动 `origin: auto` 的 station（主人手写的文件一个字不改）；
#   · 移除挪 `.trash/`（可恢复胜过彻底消失）。

def _normalize_add(name: str, command: str, args: Any, env: Any, timeout: Any):
    """规整 add 的入参（错误信息给模型看，改动要保持一字不差）。"""
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        return None, "❌ server 名不合法：只允许字母/数字/下划线/短横，长度 1-32（拿到的：%r）" % name
    command = str(command or "").strip()
    if not command:
        return None, "❌ 必须给 command（例如 uvx 或 npx）"
    if isinstance(args, str):                     # 模型有时把列表写成 JSON 串
        try:
            args = json.loads(args)
        except Exception:
            return None, "❌ args 需要是字符串列表（例如 [\"mcp-server-time\"]）"
    args = [str(a) for a in (args or [])] if isinstance(args, (list, tuple)) else []
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except Exception:
            return None, "❌ env 需要是键值映射（例如 {\"TOKEN\":\"${SOME_TOKEN}\"}）"
    env = {str(k): str(v) for k, v in (env or {}).items()} if isinstance(env, dict) else {}
    try:
        timeout = int(timeout or 30)
    except (TypeError, ValueError):
        timeout = 30
    return (name, command, args, env, timeout), ""


def _as_bool(value: Any, default: bool = False) -> bool:
    """宽容的布尔解析（模型常把 true 写成字符串）。"""
    if isinstance(value, bool):
        return value
    s = str(value if value is not None else "").strip().lower()
    if s in ("1", "true", "yes", "on", "开", "是"):
        return True
    if s in ("0", "false", "no", "off", "关", "否"):
        return False
    return default


def _station_names() -> List[str]:
    """现有 station 名（给"写错了"的提示用）。"""
    import mcp_station as ms
    found, _ = ms.scan_stations()
    return sorted(s.name for s in found)


def _station_connectivity(name: str) -> Tuple[bool, List[Dict[str, Any]], str]:
    """station 版的握手 + tools/list：成功则写进 `<station>/tools.yaml`。"""
    import mcp_station as ms
    mc.drop_connection(name)                      # 一律用最新的 STATION.md 起新进程
    try:
        tools = mc.list_tools(name)
    except Exception as e:
        tail = _stderr_tail(name)
        err = "%s: %s" % (type(e).__name__, e)
        if tail:
            err += "\n  server stderr 尾部：\n    " + "\n    ".join(tail[-6:])
        mc.drop_connection(name)
        return False, [], err
    meta = mc.peek_server_info(name)
    try:
        ms.write_tools_file(name, tools, meta)
    except Exception as e:
        mc.drop_connection(name)
        return False, [], "工具清单写入失败：%s: %s" % (type(e).__name__, e)
    # 试跑成功 ≠ 人批准过（同 v1 的理由）：不撤销的话，`add` 里承诺的
    # "第一次真调用会弹启动卡"就永远不会发生。
    mc.revoke_approval(name)
    mc.drop_connection(name)
    return True, tools, ""


def _rollback_station_add(name: str) -> str:
    """把 add 失败留下的 station 文件夹删掉（回到"这文件夹本来不存在"）。"""
    import mcp_station as ms
    st = ms.load_station(name)
    if st is None:
        return "已回滚（没留下 station 文件夹）。"
    if st.origin != "auto":
        return "⚠️ 没能回滚：`%s` 不是 auto，我不动它（请手工检查 %s）。" % (name, st.path)
    try:
        shutil.rmtree(str(st.path))
    except Exception as e:
        return "⚠️ 回滚失败（请手工删 %s）：%s: %s" % (st.path, type(e).__name__, e)
    if ms.station_exists(name):
        return "⚠️ 回滚没干净：%s 还在。" % st.path
    return "已回滚：这次新建的 station 文件夹（%s）已删除。" % st.path.name


def _station_add(name: str, command: str, args: Any, env: Any, timeout: Any = None,
                 never_parallel: Any = False, source: str = "", why: str = "",
                 force: Any = False, logger: Any = None) -> str:
    """v2 集成：建一个 station 文件夹并真跑一次连通性，把工具清单写下来。"""
    import mcp_station as ms
    vals, err = _normalize_add(name, command, args, env, timeout)
    if err:
        return err
    name, command, args, env, timeout = vals

    if ms.station_exists(name):
        st = ms.load_station(name)
        if st is not None and st.origin != "auto":
            return ("❌ `%s` 已经是你手写的 station 了（%s）—— 我不改写手写的文件。\n"
                    "  要调整请直接改它，或换个名字让我集成一份新的。" % (name, ms.station_doc(name)))
        return ("❌ `%s` 我已经集成过了（%s）。要改参数：先 action='remove' 再 add；"
                "要开关：action='set_enabled'。" % (name, ms.station_doc(name)))

    n_prev = _attempts(name)
    if n_prev >= MAX_ATTEMPTS and not _as_bool(force):
        err_txt = (_load_state().get("last_error", {}).get(name) or {}).get("err", "")
        return ("❌ %s 已连续失败 %d 次，我停手了（最近一次：%s）。\n"
                "  这是有界自愈的上限：**先向主人汇报**，别再自己试。\n"
                "  确实要继续（例如主人给了正确答案）：把 force 设为 true 再来一次。"
                % (name, n_prev, err_txt or "（无记录）"))

    if shutil.which(command) is None:
        return ("❌ 找不到可执行文件 %s。Python 包用 uvx（要装 uv），"
                "Node 包用 npx（要装 Node.js）。" % command)

    try:
        args, pin_note = _pin_args(command, args)
    except PinError as e:
        return ("❌ 无法确定包 %s 的版本号，而我**不会**用不锁版本的 latest（供应链风险）。\n"
                "  请你用 web_search 查到确切版本号，再带版本重试，例如：\n"
                "    npx → args=[\"-y\", \"%s@1.2.3\"]    uvx → args=[\"%s==1.2.3\"]"
                % (e, e, e))

    meta = {"command": command, "args": args, "enabled": True, "timeout": timeout,
            "never_parallel": _as_bool(never_parallel), "origin": "auto"}
    if env:
        meta["env"] = env
    if str(why or "").strip():
        meta["description"] = str(why).strip()
    if str(source or "").strip():
        meta["source"] = str(source).strip()

    cmdline = " ".join(mc.ServerSpec(name=name, command=command, args=args).resolved_argv([]))
    _log(logger, "开始集成 MCP station", station=name, cmd=cmdline, source=source or "-")
    try:
        doc = ms.write_station(name, meta)
    except Exception as e:
        return "❌ 建 station 文件夹失败：%s: %s" % (type(e).__name__, e)

    ok, tools, err = _station_connectivity(name)
    if not ok:
        n = _note_attempt(name, err)
        rolled = _rollback_station_add(name)
        _log(logger, "集成失败已回滚", station=name, attempt=n, err=err[:400])
        if n < MAX_ATTEMPTS:
            nxt = "还有 %d 次机会：改包名 / 补 env / 加 args 之后重试。" % (MAX_ATTEMPTS - n)
        else:
            nxt = ("已达自愈上限（%d 次）：**停下来向主人汇报**，别再自己试"
                   "（确实要继续就把 force 设为 true）。" % MAX_ATTEMPTS)
        return ("❌ 集成 %s 失败（第 %d/%d 次）：\n  %s\n  %s\n  命令行：%s\n"
                "  常见原因：包名拼错 / 缺依赖 / 缺环境变量 —— 上面 server 的 stderr 尾部通常直指原因。\n  %s"
                % (name, n, MAX_ATTEMPTS, err, rolled, cmdline, nxt))

    _clear_attempts(name)
    syn = ms.sync_registry()
    tool_names = _tool_names_of(name)             # 从盘上读回来：tools.yaml 才是真相源
    _log(logger, "集成成功", station=name, tools=tool_names)
    lines = [
        "✅ 已集成 station `%s`（%s）" % (name, pin_note),
        "  命令行：%s" % cmdline,
        "  建的是文件夹：%s（`STATION.md` 写启动参数 + 机器生成的 `tools.yaml` 存工具清单）" % doc.parent.name,
        "  工具：%d 个 —— %s" % (len(tools), ", ".join(tool_names[:8]) + ("…" if len(tool_names) > 8 else "")),
        "  注册表：%s" % syn.summary(),
        "  **不用重启**：现在就能 `mcp_search(station=\"%s\")` 取 schema、再用 mcp_call 调用。" % name,
        "  提示：第一次真调用它时，主人会收到一张启动确认卡（试跑不算批准）。",
    ]
    if name in _station_names():
        return "\n".join(lines)
    return "\n".join(lines + ["  ⚠️ 注册表同步后仍没看到它，请用 action='list' 检查。"])


def _station_test(name: str, tool: str = "", args: Any = None, logger: Any = None) -> str:
    """station 版的连通性测试（顺带刷新 `<station>/tools.yaml`，可选真调一个工具）。"""
    import mcp_station as ms
    name = str(name or "").strip()
    st = ms.load_station(name)
    if st is None:
        return ("❌ 没有叫 %s 的 station。现有的：%s"
                % (name, ", ".join(_station_names()) or "（一个都没有）"))
    if not st.command:
        return "❌ station `%s` 缺 command，没法测（看它的 STATION.md）" % name
    cmdline = " ".join(mc.ServerSpec(name=st.name, command=st.command, args=list(st.args)).resolved_argv([]))
    ok, tools, err = _station_connectivity(name)
    if not ok:
        _log(logger, "连通性测试失败", station=name, err=err[:400])
        return "❌ %s 连通性测试失败：\n  %s\n  命令行：%s" % (name, err, cmdline)
    lines = ["✅ station `%s` 连通性正常（%d 个工具）：%s"
             % (name, len(tools), ", ".join(str(t.get("name", "")) for t in tools)),
             "  命令行：%s" % cmdline,
             "  工具清单已刷新：%s" % (ms.station_doc(name).parent / ms.TOOLS_FILE)]
    if tool:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        names = [str(t.get("name")) for t in tools]
        if tool not in names:
            lines.append("  ❌ 没有名为 %s 的工具（有的：%s）" % (tool, ", ".join(names)))
            return "\n".join(lines)
        out = mc.call_tool(name, tool, args or {}, logger=logger)
        lines.append("  真调 %s → %s" % (tool, out[:800]))
    return "\n".join(lines)


def _station_set_enabled(name: str, enabled: Any, logger: Any = None) -> str:
    """开关一个 station（**只动 origin: auto 的**；手写文件不改）。"""
    import mcp_station as ms
    name = str(name or "").strip()
    if not name:
        return "❌ 要改哪个 station？给 name（用 action='list' 看有哪些）"
    st = ms.load_station(name)
    if st is None:
        return ("❌ 没有叫 %s 的 station。现有的：%s"
                % (name, ", ".join(_station_names()) or "（一个都没有）"))
    if enabled is None:
        # 审计 B7：`_as_bool(None)` 返回 **False** —— 漏传 enabled 等于"静默关站"，
        # 而文案还会说"（现在就是关，没动）"，把非法入参盖过去。改开关是显式动作，
        # 缺参必须报错，不能用假值兜底。
        return ("❌ set_enabled 需要 `enabled` 参数（true=开 / false=关），"
                "现在拿到的是 None。\n"
                "  刻意不默认成任何一边：关掉一个 station 是显式动作。")
    want = _as_bool(enabled)
    if st.origin != "auto":
        return (
            "❌ station `%s` 是你手写的（`origin: hand`）—— 我不改你手写的文件。\n"
            "  自己改一行就行：%s 里的 `enabled: %s` → `%s`，"
            "然后跑 `python agent/mcp_station.py`（重启后注入生效）。"
            % (name, ms.station_doc(name), "true" if st.enabled else "false",
               "false" if want else "true"))
    if st.enabled == want:
        return "（`%s` 现在就是 %s，没动）" % (name, "开" if want else "关")
    backup = ms.backup_file(ms.station_doc(name), "set_enabled")
    ok, info = ms.set_station_enabled(name, want)
    if not ok:
        return "❌ 改 %s 失败：%s" % (name, info)
    syn = ms.sync_registry()
    _log(logger, "station 开关", station=name, enabled=want)
    return ("✅ station `%s` 已%s（改的是 %s 的 `enabled:` 那一行；备份：%s）\n"
            "  注册表：%s\n"
            "  ⚠️ 本会话注入的注册表是**快照**，可能还显示旧状态 —— 但 `mcp_search`/`mcp_call` "
            "每次都实时读文件夹，所以行为已经变了；要刷新那段快照就重启我。"
            % (name, "打开" if want else "关闭", ms.station_doc(name).name,
               backup.name if backup else "（无）", syn.summary()))


def _station_remove(name: str, logger: Any = None) -> str:
    """移除 station（`origin: auto` 才允许；整个文件夹挪 `.trash/`，可恢复）。"""
    import mcp_station as ms
    name = str(name or "").strip()
    if not name:
        return "❌ 要移除哪个 station？给 name（用 action='list' 看有哪些）"
    st = ms.load_station(name)
    if st is None:
        return ("❌ 没有叫 %s 的 station。现有的：%s"
                % (name, ", ".join(_station_names()) or "（一个都没有）"))
    if st.origin != "auto":
        return ("❌ station `%s` 是你手写的（`origin: hand`）—— 我不删主人手写的东西。\n"
                "  要删自己动手（整个文件夹）：%s" % (name, st.path))
    try:
        mc.drop_connection(name)                  # 有活着的进程先收掉
    except Exception:
        pass
    ok, info = ms.remove_station(name)
    if not ok:
        return "❌ 移除 %s 失败：%s" % (name, info)
    syn = ms.sync_registry()
    _log(logger, "station 已移除", station=name, trash=info)
    return ("✅ 已移除 station `%s`（挪到回收站，可恢复：%s）\n  注册表：%s"
            % (name, info, syn.summary()))


def _station_list() -> str:
    """v2 的清单：station 目录 + 每个 station 的开关/来源/工具数/进程状态。"""
    import mcp_station as ms
    root = ms.stations_dir()
    found, issues = ms.scan_stations(root)
    lines = ["station 目录：%s（注册表：%s）"
             % (root, ms.registry_path(root).name)]
    if not found:
        lines.append("（还没有任何 station；用 action='search' 找候选，或 action='add' 集成一个）")
    for st in sorted(found, key=lambda s: s.name):
        info = mc.peek_server_info(st.name)
        state = "未启动"
        if info.get("alive"):
            state = "运行中(调用 %s 次)" % info.get("calls")
        elif info:
            state = "已关闭"
        lines.append("· %-14s %-4s %-5s 工具 %-3d %s  %s" % (
            st.name, "开" if st.enabled else "关", st.origin, st.tools_count, state,
            ("（%s）" % st.description[:40]) if st.description else ""))
        for p in (st.problems or []):
            lines.append("    ⚠️  %s" % p)
    for i in issues:
        lines.append("  ⚠️  %s" % i)
    state_file = _load_state()
    if state_file.get("attempts"):
        lines.append("失败计数（连续 %d 次即停手）：%s"
                     % (MAX_ATTEMPTS, json.dumps(state_file["attempts"], ensure_ascii=False)))
    lines.append("开关/移除只对 `origin: auto`（我集成的）生效；`hand` 是你手写的，我不动。")
    return "\n".join(lines)


# ============================================================
# 4. 动作实现
# ============================================================

def _act_search(query: str = "") -> str:
    lines = ["离线候选目录（这些是**常见**的，不是权威清单；装之前的连通性测试才是真验证）："]
    for c in CATALOG:
        if query and query.lower() not in (c["package"] + c["why"]).lower():
            continue
        lines.append("· %-10s %-45s %s → %s" % (c["name"], c["package"],
                                                "uvx" if c["kind"] == "uvx" else "npx -y", c["why"]))
    lines += [
        "",
        "目录里没有想要的？用你自己的联网工具找，别猜：",
        "  1) web_search 搜「<需求> MCP server」或「mcp server <关键词>」，优先官方/知名维护方；",
        "  2) web_extract 读它的 README，拿到**安装命令**（uvx 包名 或 npx 包名）与需要的环境变量；",
        "  3) 把包名/参数/来源地址交给 mcp_manage(action='add') —— 我会把版本固定住再测试。",
    ]
    return "\n".join(lines)


def _act_list() -> str:
    return _station_list()


def _act_add(name: str, command: str, args: Any, env: Any, timeout: Any = None,
             never_parallel: Any = False, source: str = "", why: str = "",
             force: Any = False, logger: Any = None) -> str:
    return _station_add(name, command, args, env, timeout, never_parallel,
                        source, why, force, logger)


def _act_test(name: str, tool: str = "", args: Any = None, logger: Any = None) -> str:
    return _station_test(name, tool, args, logger)


def _act_set_enabled(name: str, enabled: Any, logger: Any = None) -> str:
    return _station_set_enabled(name, enabled, logger)


def _act_remove(name: str, logger: Any = None) -> str:
    return _station_remove(name, logger)


# ============================================================
# 5. 对外入口
# ============================================================

_ACTIONS = ("search", "list", "add", "test", "set_enabled", "remove")


def mcp_manage(action: str = "", name: str = "", command: str = "", args: Any = None,
               env: Any = None, timeout: Any = None, never_parallel: Any = False,
               source: str = "", why: str = "", tool: str = "", query: str = "",
               enabled: Any = None, force: Any = False, logger: Any = None) -> str:
    """自维护入口：搜索 / 查看 / 集成 / 测试 / 开关 / 移除 MCP server。"""
    act = str(action or "").strip().lower()
    if act not in _ACTIONS:
        return "❌ action 必须是 %s 之一（拿到的是 %r）" % ("/".join(_ACTIONS), action)
    try:
        if act == "search":
            return _act_search(str(query or ""))
        if act == "list":
            return _act_list()
        if act == "add":
            return _act_add(name, command, args, env, timeout, never_parallel,
                            source, why, force, logger=logger)
        if act == "test":
            return _act_test(name, str(tool or ""), args, logger=logger)
        if act == "set_enabled":
            return _act_set_enabled(name, enabled, logger=logger)
        return _act_remove(name, logger=logger)
    except Exception as e:                        # 兜底：绝不把异常抛给编排器
        _log(logger, "mcp_manage 异常", action=act, err=repr(e))
        return "❌ mcp_manage 内部异常（%s）：%s: %s" % (act, type(e).__name__, e)


mcp_manage_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mcp_manage",
        "description": (
            "自维护：自己集成/管理 MCP server（外部工具服务）。"
            "search=看候选并学会怎么找；list=看现有 station 与运行状态；"
            "add=集成新的（会自动固定版本、建 station 文件夹、连通性测试、装完立刻可用）；"
            "test=连通性测试（可选真调一次工具）；set_enabled=开关；remove=移除（可恢复）。"
            "只动你自己集成的 station（`origin: auto`），主人手写的一个字节都不改。"
            "⚠️ add 会在本机下载并**真跑**该包（等同执行可执行文件）：动手前先跟主人说清"
            "包名/来源/会联网下载；集成不设专门闸门，但第一次真调用该 station 时，"
            "主人会收到一张启动确认卡（上面是真实命令行）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": list(_ACTIONS),
                           "description": "要做的动作"},
                "name": {"type": "string",
                         "description": "station 名（字母/数字/下划线/短横，1-32 字符）。文件夹名与它一致"},
                "command": {"type": "string",
                            "description": "启动命令：Python 包用 uvx，Node 包用 npx"},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "命令参数，例如 [\"mcp-server-time\"]。不用自己写版本号，我会查并固定；查不到会让你去查"},
                "env": {"type": "object",
                        "description": "该 server 要的环境变量，值用 ${VAR} 占位（别写明文密钥）"},
                "timeout": {"type": "integer", "description": "单次调用超时秒数（默认 30）"},
                "never_parallel": {"type": "boolean",
                                   "description": "true = 该 server 的工具单独占一层、前后不并行（交互型 server 用）"},
                "source": {"type": "string",
                           "description": "来源（仓库/文档 URL 或主人给的线索）—— 会显示在审批卡片上"},
                "why": {"type": "string", "description": "为什么集成它（一句话，给人看的）"},
                "tool": {"type": "string", "description": "test 动作里可选：真调一次的工具名"},
                "query": {"type": "string", "description": "search 动作的过滤词（可空）"},
                "enabled": {"type": "boolean", "description": "set_enabled 动作：true 开 / false 关"},
                "force": {"type": "boolean",
                          "description": "失败已达上限后仍要重试时置 true（默认 false，先向主人汇报）"},
            },
            "required": ["action"],
        },
    },
}
