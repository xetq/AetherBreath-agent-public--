# -*- coding: utf-8 -*-
"""mcp_client —— MCP（Model Context Protocol）stdio 客户端。

设计：docs/MCP设计.md（逐条决策 + 为什么）；注册表：`agent_MCP/<station>/`（一个 server
就是一个文件夹）；用法：`agent_MCP/README.md`。

本模块只负责**传输与进程**：注册表的发现与解析是 `agent/mcp_station.py` 的事，本模块的
`load_registry()` 只是它的一层薄包装（下游拿到的仍是同形的 `Registry` / `ServerSpec`）。

它做什么：
  · 读注册表（station 目录）——server 的启动命令、参数、env、超时、开关；
  · 懒启动 server 子进程（首次真调用才 Popen），进程内复用连接；
  · 手写同步 JSON-RPC over stdio：initialize / tools/list / tools/call（换行分隔分帧）；
  · 每 server 一把锁：stdio 只有一条 stdout 管道，并发调用必须串行化才不串包；
  · 真超时：读线程 + 队列，超时后 kill 该 server 进程并标记连接失效；
  · 结果降维：content 数组 → 字符串（text 直连 / image 落盘 / resource 给 uri+摘要）；
  · 失败语义：一切错误返回 "❌ …" 字符串（对齐 task_orchestrator.BIZ_FAIL_MARKS），
    让界面与日志不虚报成功率；
  · 日志：可选注入 SessionLogger（span = mcp:<server>），终端零输出。

为什么不用官方 mcp SDK：见 docs/MCP设计.md §三-1（编排器是同步线程池；同步实现才
能真 kill 卡死的进程；零新依赖让 tests/ 能离线确定性运行）。

本模块**没有任何 import 期副作用**（不起进程、不读盘、不打印）——这是设计决策 2 的
前提：工具必须在 import 期注册，而 import 期绝不能启动子进程。
"""
from __future__ import annotations

import atexit
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 1. 常量
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_TMP_REL = "agent_workspace/.tmp/mcp"

PROTOCOL_VERSION = "2025-06-18"      # 我方偏好的协议版本；对端回什么都接受（下面只记日志）
CLIENT_NAME = "aetherbreath-mcp-client"
CLIENT_VERSION = "0.1.0"

STDERR_KEEP = 40                     # 保留 server stderr 的尾部行数（排障用）
MAX_PAGES = 20                       # tools/list 分页上限（防对端死循环）
MAX_STRAY = 8                        # 记录多少个"对不上号"的响应 id（串包证据）
MAX_RESULT_CHARS = 30000             # 单次结果回灌给模型的上限，超出截断
MAX_LINE_CHARS = 4_000_000           # 单行上限，防对端灌爆内存

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ============================================================
# 2. 异常
# ============================================================

class MCPError(Exception):
    """MCP 客户端一切错误的基类。"""


class MCPTimeout(MCPError):
    """等待响应超时（进程已被 kill）。"""


class MCPProcessDead(MCPError):
    """server 进程死了（启动失败 / 中途退出 / 管道断裂）。"""


class MCPProtocolError(MCPError):
    """握手或响应格式不符合 JSON-RPC / MCP 约定。"""


# ============================================================
# 3. ${VAR} 展开（决策 7）
# ============================================================

def expand_env(text: str, missing: Optional[List[str]] = None) -> str:
    """把 ${NAME} 从环境变量展开。

    缺失的变量展开为空串，并把名字记进 missing（**不静默**：调用方会记日志、
    审批卡片也会写出来）。没有占位符时原样返回。
    """
    if not isinstance(text, str) or "${" not in text:
        return text

    def _sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        val = os.environ.get(name)
        if val is None or val == "":
            if missing is not None and name not in missing:
                missing.append(name)
            return ""
        return val

    return _RE_VAR.sub(_sub, text)


# ============================================================
# 4. 注册表（station 目录）
# ============================================================

@dataclass
class ToolSpec:
    """一个 MCP 工具的静态声明（来自 station 的 `tools.yaml`，由 `mcp_sync` 生成）。"""
    name: str
    description: str = ""
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ServerSpec:
    """一个 MCP server 的运行期声明（来自某个 `agent_MCP/<station>/STATION.md`）。"""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    never_parallel: bool = False
    enabled: bool = False
    description: str = ""
    tools: List[ToolSpec] = field(default_factory=list)
    tools_path: Optional[Path] = None
    problems: List[str] = field(default_factory=list)
    # 来源："hand" = 主人手写的 station；"auto" = AB 自己集成的 station。
    # 只有 auto 的条目允许被 mcp_manage 改写/移除（手写的文件永远不动）。
    origin: str = "hand"

    def resolved_env(self, missing: Optional[List[str]] = None) -> Dict[str, str]:
        return {str(k): expand_env(str(v), missing) for k, v in (self.env or {}).items()}

    def resolved_argv(self, missing: Optional[List[str]] = None) -> List[str]:
        cmd = expand_env(str(self.command), missing)
        args = [expand_env(str(a), missing) for a in (self.args or [])]
        return _resolve_argv(cmd, args)


@dataclass
class Registry:
    """整份注册表 + 读取过程中的问题（problems 不为空时**要报**，不静默）。"""
    path: Path
    servers: List[ServerSpec] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def by_name(self, name: str) -> Optional[ServerSpec]:
        for s in self.servers:
            if s.name == name:
                return s
        return None

    def enabled(self) -> List[ServerSpec]:
        return [s for s in self.servers if s.enabled]

    def origin_of(self, name: str) -> str:
        """这个 server 是手写的还是 AB 自己集成的（""=没有这个 server）。"""
        s = self.by_name(str(name))
        return s.origin if s is not None else ""


def load_registry(path: Optional[Path] = None, with_tools: bool = True) -> Registry:
    """读注册表 —— **station 目录**（一个 server = 一个文件夹）。

    真相源只有一个：`agent_MCP/<station>/`（见 `docs/MCP设计.md` v2 §三/§四）。
    发现与解析全部由 `agent/mcp_station.py` 负责，本模块只是转发（延迟导入，避免环形依赖）。

    `path` 是 **station 目录**（不传则按 mcp_station 的优先级解析：参数 > 环境变量
    `AETHER_MCP_STATIONS` > config `paths.mcp_stations` > 项目内默认）。
    """
    import mcp_station                              # noqa: PLC0415（延迟导入：避免环形依赖）
    return mcp_station.load_registry(stations=path, with_tools=with_tools)


# ============================================================
# 5. 命令行解析（Windows 上 .cmd/.bat 必须经 cmd /c）
# ============================================================

def _resolve_argv(command: str, args: List[str]) -> List[str]:
    """把 command 解析成可执行的 argv。

    Windows 上 `npx` 实际是 `npx.cmd`，CreateProcess 不能直接跑 .cmd/.bat ——
    必须经 `cmd /c`。用 shutil.which（遵守 PATHEXT）解析，解析不到就原样交给系统。
    """
    if not command:
        return []
    exe = shutil.which(command) or command
    if os.name == "nt" and str(exe).lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", exe] + list(args)
    return [exe] + list(args)


# ============================================================
# 6. 日志（可选注入；终端零输出）
# ============================================================

def _slog(logger: Any, server_name: str, level: str, msg: str, **extra: Any) -> None:
    """把一条事件写进会话日志（span = mcp:<server>）。logger 缺失/异常一律吞掉 —— 
    日志写不进去绝不能影响工具执行。

    形参叫 `server_name` 而不是 `server`：调用方常把 `server=` 之类的键塞进 extra，
    与形参重名会抛 `TypeError: _slog() got multiple values for argument 'server'`
    —— 那会在**日志路径上**炸掉正常调用。
    """
    if logger is None:
        return
    try:
        lg = logger.with_span("mcp:%s" % server_name) if hasattr(logger, "with_span") else logger
        fn = getattr(lg, level, None)
        if callable(fn):
            fn(msg, **extra)
    except Exception:
        pass


# ============================================================
# 7. 连接（进程 + 管道 + 对号入座）
# ============================================================

_EOF = "__eof__"


class ServerConnection:
    """一个 MCP server 的常驻连接。

    锁纪律：**所有** request 都在 self._lock 内完成"写请求 + 等到对应 id 的响应"，
    因此同一连接上永远只有一个在途请求 —— 这是 stdio 单管道下不串包的根本保证。
    （RLock：握手在锁内发起，_spawn_locked 会用到 _exchange_locked。）
    """

    def __init__(self, spec: ServerSpec, logger: Any = None):
        self.spec = spec
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._inbox: "queue.Queue[Tuple[Any, Any]]" = queue.Queue()
        self._stderr_tail: deque = deque(maxlen=STDERR_KEEP)
        self._notifications: deque = deque(maxlen=50)
        self._stray_ids: List[Any] = []
        self._unparsable: List[str] = []
        self._next_id = 1
        self._dead_reason = ""
        # stdout 读到 EOF = 对端已经把输出关了。**必须单独记一个标志**：
        # 进程刚退出那一瞬间 poll() 可能仍返回 None，只看 poll() 会把死进程判成
        # "运行中"，于是既不重启也不回收，堆出一批半死子进程。
        self._stdout_closed = False
        self.protocol_version = ""
        self.server_info: Dict[str, Any] = {}
        self.started_at = 0.0
        self.calls = 0
        self.missing_env: List[str] = []
        self._logger = logger

    # ---------- 状态 ----------

    def alive(self) -> bool:
        """进程活着**且**输出还开着才算活着（见 _stdout_closed 的注释）。"""
        return (self._proc is not None and self._proc.poll() is None
                and not self._stdout_closed)

    def dead_reason(self) -> str:
        if self._dead_reason:
            return self._dead_reason
        if self._proc is None:
            return "尚未启动"
        rc = self._proc.poll()
        if rc is None and self._stdout_closed:
            return "server 输出已关闭（读到 EOF）—— 进程很可能已退出"
        if rc is None:
            return "运行中"
        tail = self.stderr_tail(3)
        return "进程已退出（退出码 %s）%s" % (rc, ("；stderr: " + " | ".join(tail)) if tail else "")

    def stderr_tail(self, n: int = 5) -> List[str]:
        return list(self._stderr_tail)[-n:]

    def command_line(self) -> str:
        """真实命令行（展开 ${VAR} 后），给审批卡片与日志用。"""
        missing = list(self.missing_env)
        argv = self.spec.resolved_argv(missing)
        return " ".join(argv)

    # ---------- 启动 / 关闭 ----------

    def _spawn_locked(self) -> None:
        # 先把上一次残留的进程收干净（可能是 EOF 判死后留下的半死进程）
        old = self._proc
        if old is not None:
            try:
                if old.poll() is None:
                    old.terminate()
                    try:
                        old.wait(timeout=3)
                    except Exception:
                        old.kill()
            except Exception:
                pass
        self._proc = None
        self._stdout_closed = False
        missing: List[str] = []
        argv = self.spec.resolved_argv(missing)
        env = os.environ.copy()
        # Windows 中文环境下 Python 子进程默认按 locale(cp936) 读 stdin，而本客户端
        # 用 ensure_ascii=False 写 UTF-8 —— 中文 payload 会被解成孤立代理字符，
        # 子进程一旦把它写进日志就 UnicodeEncodeError 崩溃（表现为 MCP 调用报"读到 EOF"）。
        # PYTHONUTF8 / PYTHONIOENCODING 只影响 Python 子进程，对 Node 等 server 无害。
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            env.update(self.spec.resolved_env(missing))
        except Exception as e:
            raise MCPProcessDead("env 展开失败：%s: %s" % (type(e).__name__, e))
        self.missing_env = missing
        if not argv:
            raise MCPProcessDead("server %s 没有可执行的 command" % self.spec.name)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_NO_WINDOW,
            )
        except Exception as e:
            raise MCPProcessDead("启动失败（%s）：%s: %s" % (self.command_line(), type(e).__name__, e))
        self.started_at = time.monotonic()
        self._dead_reason = ""
        self._inbox = queue.Queue()
        self._stray_ids = []
        self._reader = threading.Thread(target=self._read_loop, name="mcp-read-%s" % self.spec.name, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._stderr_loop, name="mcp-err-%s" % self.spec.name, daemon=True)
        self._stderr_reader.start()
        _slog(self._logger, self.spec.name, "info", "MCP server 进程已启动",
              cmd=self.command_line(), pid=getattr(self._proc, "pid", None),
              missing_env=list(missing))

    def _read_loop(self) -> None:
        """读 stdout：有 id 的进 inbox，无 id 的是通知（记下来，不干扰对号入座）。"""
        proc = self._proc
        try:
            while True:
                line = proc.stdout.readline() if proc and proc.stdout else ""
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                if len(line) > MAX_LINE_CHARS:
                    self._unparsable.append("超长行已丢弃（%d 字符）" % len(line))
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    if len(self._unparsable) < MAX_STRAY:
                        self._unparsable.append(line[:200])
                    continue
                if isinstance(msg, dict) and msg.get("id") is not None:
                    self._inbox.put((msg.get("id"), msg))
                else:
                    self._notifications.append(msg)
        except Exception as e:
            self._unparsable.append("读线程异常：%s: %s" % (type(e).__name__, e))
        finally:
            self._stdout_closed = True
            if not self._dead_reason:
                rc = None
                try:
                    rc = proc.poll() if proc is not None else None
                except Exception:
                    rc = None
                self._dead_reason = ("server 输出已关闭（读到 EOF），进程已退出（退出码 %s）" % rc
                                     if rc is not None else
                                     "server 输出已关闭（读到 EOF）—— 进程很可能已退出")
            self._inbox.put((_EOF, None))

    def _stderr_loop(self) -> None:
        """持续排空 stderr。

        必须读：server（尤其 Node 类）往 stderr 写东西，没人读的话管道缓冲写满，
        进程会**卡死**在写 stderr 上 —— 表现为工具调用永远不返回。
        """
        proc = self._proc
        try:
            while True:
                line = proc.stderr.readline() if proc and proc.stderr else ""
                if not line:
                    break
                self._stderr_tail.append(line.rstrip("\r\n")[:500])
        except Exception:
            pass

    def _kill_locked(self, why: str) -> None:
        """kill 进程并标记失效（决策 9：超时必须真回收资源）。"""
        proc = self._proc
        self._dead_reason = why
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        proc.kill()
            except Exception:
                pass
        self._proc = None
        self._stdout_closed = True
        _slog(self._logger, self.spec.name, "warning", "MCP server 进程已终止",
              why=why, stderr_tail=self.stderr_tail(3))

    def close(self) -> None:
        with self._lock:
            self._kill_locked("主动关闭")

    # ---------- 请求 ----------

    def _write_locked(self, payload: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPProcessDead(self.dead_reason())
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as e:
            self._kill_locked("写请求失败：%s: %s" % (type(e).__name__, e))
            raise MCPProcessDead(self.dead_reason())

    def _notify_locked(self, method: str, params: Dict[str, Any]) -> None:
        try:
            self._write_locked({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except MCPError:
            pass

    def _exchange_locked(self, method: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """写请求 + 等到**对号**的响应。调用方必须已持有 self._lock。"""
        rid = self._next_id
        self._next_id += 1
        self._write_locked({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_locked("响应超时（%.1fs，方法 %s）" % (timeout, method))
                raise MCPTimeout("等待 %s 响应超时（%.1fs），已 kill 进程" % (method, timeout))
            try:
                got_id, msg = self._inbox.get(timeout=remaining)
            except queue.Empty:
                self._kill_locked("响应超时（%.1fs，方法 %s）" % (timeout, method))
                raise MCPTimeout("等待 %s 响应超时（%.1fs），已 kill 进程" % (method, timeout))
            if got_id == _EOF:
                raise MCPProcessDead(self.dead_reason())
            if got_id == rid:
                return msg
            # 对不上号：不是我的响应。记证据、继续等（乱序/串包都会在这里现形）
            if len(self._stray_ids) < MAX_STRAY:
                self._stray_ids.append(got_id)

    def _ensure_locked(self) -> None:
        if not self.alive():
            self._spawn_locked()
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        """MCP 握手：initialize → 收到响应 → 发 initialized 通知。"""
        resp = self._exchange_locked(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
            self.spec.timeout,
        )
        if "error" in resp:
            raise MCPProtocolError("initialize 被拒：%s" % json.dumps(resp["error"], ensure_ascii=False)[:300])
        result = resp.get("result") or {}
        self.protocol_version = str(result.get("protocolVersion") or "")
        self.server_info = result.get("serverInfo") or {}
        if self.protocol_version and self.protocol_version != PROTOCOL_VERSION:
            _slog(self._logger, self.spec.name, "warning", "协议版本与本地偏好不一致（不致命）",
                  client_version=PROTOCOL_VERSION, server_version=self.protocol_version)
        self._notify_locked("notifications/initialized", {})
        approve(self.spec.name)      # 走到这里 = 人已批准过（审批闸门在调用前）→ 本进程内免再问
        _slog(self._logger, self.spec.name, "info", "MCP 握手完成",
              protocol=self.protocol_version, server_info=self.server_info)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """发一个 JSON-RPC 请求并等响应（自动懒启动 + 串行化）。"""
        with self._lock:
            self._ensure_locked()
            return self._exchange_locked(method, params or {}, timeout or self.spec.timeout)

    # ---------- MCP 方法 ----------

    def list_tools(self) -> List[Dict[str, Any]]:
        """tools/list（自动翻页）。"""
        out: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            resp = self.request("tools/list", params)
            if "error" in resp:
                raise MCPProtocolError("tools/list 失败：%s" % json.dumps(resp["error"], ensure_ascii=False)[:300])
            result = resp.get("result") or {}
            tools = result.get("tools")
            if isinstance(tools, list):
                out += [t for t in tools if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

    def call_tool(self, tool: str, arguments: Dict[str, Any], logger: Any = None) -> str:
        """tools/call → 降维成字符串。任何失败都返回 ❌ 开头的文本（决策 8）。"""
        log = logger if logger is not None else self._logger
        t0 = time.monotonic()
        try:
            resp = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        except MCPError as e:
            _slog(log, self.spec.name, "error", "MCP 调用失败", tool=tool, err=str(e))
            return "❌ MCP 调用失败（server=%s, tool=%s）：%s" % (self.spec.name, tool, e)
        except Exception as e:                       # 兜底：绝不让异常穿透到编排器
            _slog(log, self.spec.name, "error", "MCP 调用异常", tool=tool, err=repr(e))
            return "❌ MCP 调用异常（server=%s, tool=%s）：%s: %s" % (
                self.spec.name, tool, type(e).__name__, e)
        elapsed = time.monotonic() - t0
        self.calls += 1
        if "error" in resp:
            err = json.dumps(resp["error"], ensure_ascii=False)[:500]
            _slog(log, self.spec.name, "warning", "MCP 返回 JSON-RPC 错误",
                  tool=tool, elapsed=round(elapsed, 3), err=err)
            return "❌ MCP 错误（server=%s, tool=%s）：%s" % (self.spec.name, tool, err)
        result = resp.get("result")
        if not isinstance(result, dict):
            return "❌ MCP 响应格式异常（server=%s, tool=%s）：result 不是对象" % (self.spec.name, tool)
        body = render_result(result, self.spec.name)
        is_err = bool(result.get("isError"))
        _slog(log, self.spec.name, "info" if not is_err else "warning",
              "MCP 工具调用完成", tool=tool, elapsed=round(elapsed, 3),
              is_error=is_err, chars=len(body))
        if is_err:
            return "❌ MCP 工具报错（server=%s, tool=%s）：%s" % (self.spec.name, tool, body)
        return body


# ============================================================
# 8. 结果降维（决策 8）
# ============================================================

def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n…[已截断 %d 字符]" % (len(text) - MAX_RESULT_CHARS)


_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


def image_tmp_dir() -> Path:
    """图片落盘目录：环境变量 AETHER_MCP_IMAGE_TMP > 项目内默认。

    可覆盖是为了让测试能把落盘重定向到临时目录（tests/ 的判据是"不写真实文件"），
    也让主人想把 MCP 图片挪到别处时不用改代码。
    """
    env = (os.environ.get("AETHER_MCP_IMAGE_TMP") or "").strip()
    if env:
        return Path(env)
    return PROJECT_ROOT / IMAGE_TMP_REL


def _save_image(server: str, idx: int, mime: str, b64: str) -> Optional[str]:
    """图片落盘，返回绝对路径（失败返回 None，不抛）。"""
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception:
        return None
    ext = _EXT_BY_MIME.get(str(mime or "").lower(), ".bin")
    out_dir = image_tmp_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("%s_%d_%s%s" % (server, idx, time.strftime("%Y%m%d_%H%M%S"), ext))
        path.write_bytes(data)
        return str(path)
    except Exception:
        return None


def render_result(result: Dict[str, Any], server: str = "") -> str:
    """把 MCP 的 result 降维成给模型看的字符串。

    · content 数组里的 text 直连；
    · image 落盘到 agent_workspace/.tmp/mcp/ 并给绝对路径；
    · resource 给 uri（带内嵌文本时给文本）；
    · 认不出的类型原样 JSON 附在末尾（不丢信息、不脑补）。
    """
    content = result.get("content")
    if content is None:
        rest = {k: v for k, v in result.items() if k != "isError"}
        return _truncate(json.dumps(rest, ensure_ascii=False)) if rest else "(空结果)"
    if not isinstance(content, list):
        return _truncate(json.dumps(content, ensure_ascii=False))

    parts: List[str] = []
    unknown: List[str] = []
    for i, item in enumerate(content):
        if not isinstance(item, dict):
            unknown.append(json.dumps(item, ensure_ascii=False)[:200])
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "image":
            path = _save_image(server, i, str(item.get("mimeType") or ""), str(item.get("data") or ""))
            if path:
                parts.append("[图片已保存：%s]" % path)
            else:
                parts.append("[图片内容无法解码，已丢弃]")
        elif kind == "resource":
            res = item.get("resource") or {}
            uri = str(res.get("uri") or "")
            if res.get("text"):
                parts.append("[资源 %s]\n%s" % (uri, res.get("text")))
            else:
                parts.append("[资源：%s]" % (uri or "(无 uri)"))
        else:
            unknown.append(json.dumps(item, ensure_ascii=False)[:200])

    out = "\n".join(p for p in parts if p)
    if unknown:
        out += ("\n" if out else "") + "[未识别的内容类型，原样附上]\n" + "\n".join(unknown)
    if not out:
        out = "(空结果)"
    return _truncate(out)


# ============================================================
# 9. 连接池 / 审批状态 / 对外 API
# ============================================================

_CONNS: Dict[str, ServerConnection] = {}
_CONNS_LOCK = threading.Lock()
_APPROVED: set = set()          # 本进程内"已批准并成功启动过"的 server（审批规范读它）


def is_approved(server: str) -> bool:
    """该 server 的进程在本进程内是否已经成功启动过（= 主人批准过）。

    审批规范 agent/approvals/mcp_spawn.py 用它决定"问还是免打扰"。
    故意做成**进程级**而非会话级：进程运行期间同一个人、同一台机器，
    重复问同一个命令行只会训练出闭眼点"允许"。
    """
    return str(server) in _APPROVED


def approve(server: str) -> None:
    _APPROVED.add(str(server))


def revoke_approval(server: Optional[str] = None) -> None:
    """撤销批准（server=None 时清空）。测试与"我不再信任它"时用。"""
    if server is None:
        _APPROVED.clear()
    else:
        _APPROVED.discard(str(server))


def reset_approvals() -> None:
    _APPROVED.clear()


def get_registry() -> Registry:
    return load_registry()


def find_server(server: str, reg: Optional[Registry] = None) -> ServerSpec:
    r = reg or get_registry()
    spec = r.by_name(str(server))
    if spec is None:
        raise MCPError("注册表里没有 server：%s（看 agent_MCP/<station>/STATION.md）" % server)
    return spec


def command_line_preview(server: str) -> str:
    """给审批卡片用：这条 server 将要执行的真实命令行（对端信息拿不到就返回空串）。"""
    try:
        spec = find_server(server)
        return " ".join(spec.resolved_argv([]))
    except Exception:
        return ""


def get_connection(server: str, logger: Any = None, reg: Optional[Registry] = None) -> ServerConnection:
    """取（必要时创建）某 server 的连接对象。**不启动进程**——进程在首次请求时懒启动。"""
    name = str(server)
    with _CONNS_LOCK:
        conn = _CONNS.get(name)
        if conn is None:
            spec = find_server(name, reg)
            conn = ServerConnection(spec, logger=logger)
            _CONNS[name] = conn
        elif logger is not None:
            conn._logger = logger
    return conn


def drop_connection(server: str) -> bool:
    """丢掉某 server 的连接（关进程 + 从池里移除）。

    **改了 YAML 之后必须丢**：连接对象持有创建时的 ServerSpec 快照，不丢就会
    拿着旧命令/旧参数继续跑（自集成的"改参数重试"会因此测出一个假的成功/失败）。
    """
    with _CONNS_LOCK:
        conn = _CONNS.pop(str(server), None)
    if conn is None:
        return False
    _STATS[str(server)] = _stat_row(conn, alive=False)   # 留个"上次跑到哪"给面板
    write_runtime_snapshot()
    try:
        conn.close()
    except Exception:
        pass
    return True


def call_tool(server: str, tool: str, arguments: Optional[Dict[str, Any]] = None,
              logger: Any = None, reg: Optional[Registry] = None) -> str:
    """对外主入口（`mcp_call` 元工具与 mcp_manage 都调它）。任何失败都返回 ❌ 文本。"""
    try:
        spec = find_server(server, reg)
    except MCPError as e:
        return "❌ %s" % e
    except Exception as e:
        return "❌ 读取 MCP 注册表失败：%s: %s" % (type(e).__name__, e)
    if not spec.enabled:
        # fail-closed：没开开关的 server 不许被调用（可能是模型从历史里学到的旧工具名）
        return "❌ MCP server 未启用：%s（它的 STATION.md 里 enabled: false）" % spec.name
    conn = get_connection(spec.name, logger=logger, reg=reg)
    return conn.call_tool(str(tool), arguments or {}, logger=logger)


def list_tools(server: str, logger: Any = None) -> List[Dict[str, Any]]:
    """连真 server 拉 tools/list（mcp_sync 用；测试用 stub）。"""
    spec = find_server(server)
    conn = get_connection(spec.name, logger=logger)
    return conn.list_tools()


def peek_server_info(server: str) -> Dict[str, Any]:
    """已在运行时返回它的握手信息（不启动进程）。

    额外带三个**纯展示**字段（WebUI 运行时面板 / 时间线用，不参与任何调用逻辑）：
    `last_tool` / `last_call_at` / `last_call_ok` —— 最近一次工具调用是谁、什么时候、成不成。
    """
    conn = _CONNS.get(str(server))
    if conn is None:
        return {}
    return {"protocol_version": conn.protocol_version, "server_info": conn.server_info,
            "alive": conn.alive(), "calls": conn.calls, "missing_env": list(conn.missing_env),
            "last_tool": getattr(conn, "last_tool", None),
            "last_call_at": getattr(conn, "last_call_at", None),
            "last_call_ok": getattr(conn, "last_call_ok", None)}


_STATS: Dict[str, Dict[str, Any]] = {}   # 进程退出后仍想让人看见"调过几次"（给面板用）


def _stat_row(conn: Any, alive: bool = True) -> Dict[str, Any]:
    """把一个连接对象拍成面板要的那一行（**只读**，任何字段缺失都不抛）。"""
    return {
        "alive": bool(alive),
        "calls": int(getattr(conn, "calls", 0) or 0),
        "last_tool": getattr(conn, "last_tool", None),
        "last_call_at": getattr(conn, "last_call_at", None),
        "last_call_ok": getattr(conn, "last_call_ok", None),
        "missing_env": list(getattr(conn, "missing_env", None) or []),
        "protocol_version": str(getattr(conn, "protocol_version", "") or ""),
        "server_version": str((getattr(conn, "server_info", None) or {}).get("version") or ""),
    }


def runtime_snapshot() -> Dict[str, Any]:
    """**本进程**的 MCP 运行状态（在线/调用次数/最近调用/缺哪些 env）。"""
    rows: Dict[str, Any] = {}
    for name in list(_STATS.keys()):
        rows[str(name)] = dict(_STATS[name])
    for name, conn in list(_CONNS.items()):
        rows[str(name)] = _stat_row(conn, alive=bool(conn.alive()))
    return rows


_ACTIVITY_DIRS: Dict[str, Path] = {}     # station 名 → 它**这次是在哪个目录里**被用的
_ACTIVITY_ORDER: List[str] = []          # 最近活跃顺序（末位最新）


def _remember_dir(name: str) -> None:
    """记下"这个 station 这次是在哪个目录里用的" —— **在调用发生的那一刻解析**。

    为什么要记而不是写的时候现算：本模块有 `atexit` 收尾（`shutdown_all`），那一刻调用方
    可能**已经还原了环境变量** —— 实测：自集成探针在 finally 里还原 env，退出时那次收尾写入
    就落进了**真实仓库**的 `.state/`（临时目录里的测试数据泄进真库）。
    """
    try:
        import mcp_station as ms                     # noqa: PLC0415
        d = ms.state_dir()
        _ACTIVITY_DIRS[str(name)] = d
        if str(name) in _ACTIVITY_ORDER:
            _ACTIVITY_ORDER.remove(str(name))
        _ACTIVITY_ORDER.append(str(name))
    except Exception:                                # noqa: BLE001
        pass


def _snapshot_target() -> Optional[Path]:
    """快照写哪儿：按**活跃记录**取（末位最新），取不到就不写（宁可少写，不写错地方）。"""
    if not _ACTIVITY_ORDER:
        return None
    d = _ACTIVITY_DIRS.get(_ACTIVITY_ORDER[-1])
    return (d / "runtime.json") if d else None


def write_runtime_snapshot() -> Optional[Path]:
    """把运行状态落盘，给 WebUI「运行时」面板看（best-effort，绝不因它影响调用）。

    **为什么要落盘**：面板跑在**网关进程**里，而 MCP 连接活在 **bridge/agent 进程**里 ——
    网关那份连接表永远是空的（实测：面板一直显示"未起 / 0 次"，还误报"缺
    `GITHUB_PERSONAL_ACCESS_TOKEN`"，因为 `.env` 只加载在 agent 侧）。所以由 agent 侧写、
    网关侧读。文件在 `agent_MCP/.state/runtime.json`（已 gitignore）。
    """
    if not _CONNS and not _STATS:
        # 本进程压根没碰过 MCP：**不许写**。否则一个空快照会盖掉 agent 进程写进去的真数据
        # （实测：某个没连过 MCP 的进程调 shutdown_all() 就把面板的"调过 3 次"抹成 0 次），
        # 也会在没跑过 MCP 的进程里凭空造出 `.state/` 目录。
        return None
    path = _snapshot_target()
    if path is None:
        return None                                  # 没记下活动目录 → 不猜
    try:
        payload = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "pid": os.getpid(), "servers": runtime_snapshot()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path
    except Exception:                                # noqa: BLE001
        return None


def record_call(server: str, tool: str, ok: bool) -> None:
    """记一笔"最近一次工具调用"（**只给界面看**，失败也不抛：面板不能影响调用）。

    v2 的 MCP 工具都从 `mcp_gateway.mcp_call` 进来，所以在那里记一次就够 ——
    主人要求"在 WebUI 里能看见调用了哪个 MCP 的哪个工具"（docs/MCP设计.md §十 Q26）。
    """
    try:
        conn = _CONNS.get(str(server))
        if conn is None:
            return
        conn.last_tool = str(tool or "")
        conn.last_call_at = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.last_call_ok = bool(ok)
        _STATS[str(server)] = _stat_row(conn)
        _remember_dir(server)          # 在"调用发生的那一刻"记下目录（atexit 时 env 可能已被还原）
        write_runtime_snapshot()
    except Exception:
        pass


def shutdown_all() -> int:
    """关闭所有 MCP server 进程，返回关闭的个数。"""
    with _CONNS_LOCK:
        conns = list(_CONNS.values())
    n = 0
    for c in conns:
        try:
            if c.alive():
                c.close()
                n += 1
        except Exception:
            pass
    for name, c in list(_CONNS.items()):
        _STATS[str(name)] = _stat_row(c, alive=False)
    write_runtime_snapshot()
    return n


atexit.register(shutdown_all)
