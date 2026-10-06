# -*- coding: utf-8 -*-
"""bridge.py —— AetherBreath 本体的子进程入口（可被强制 kill）。

启动方式（由 backend/agent_proc.py 拉起）：
    <项目根 venv>/python  agent_webui/backend/bridge.py
协议：
    stdout 首行 = JSON {"port": N, "pid": P}，之后 stdout 不再使用
    其余输出全部走 stderr（可自由 print，不污染协议）
    HTTP 仅监听 127.0.0.1:<随机端口>，所有请求需 X-Bridge-Token 校验

⚠️ 零侵入承诺：本文件不修改 agent/ 与 agent_tools/ 下任何文件。
所有能力扩展（ask_user 工具、工具事件上报、中期交互注入）都通过「运行时对象引用可变」
这一 Python 语义在 bridge 自己的进程内完成：
    agent_tools.AVAILABLE_TOOLS      -> dict，追加键即对 agent 模块生效
    agent_tools.TOOLS_SCHEMA         -> list，追加元素即对 agent 模块生效
    agent._ORCHESTRATOR              -> 单例实例，可读引用
    agent.register_after_tools_hook  -> 通用宿主钩子：注册「每批工具返回之后」的回调
    mid_turn.BOX                     -> 本目录（backend/）的模块级信箱（中期交互）
中期交互（2026-09-22 起实现整体在本目录 mid_turn.py）：注入点用的是 agent 提供的
**通用**钩子 —— agent 不认识「中期交互」这个概念；bridge 只做两件事：注册钩子 +
提供 WebUI 独有的投递入口 /mid_turn。
CLI 模式（python agent/agent.py）不会 import 本文件，行为完全不变。
"""
from __future__ import annotations

import ctypes
import functools
import json
import os
import queue
import re
import sys
import threading
import time
import uuid
_LAYER_SEQ = [0]
_LAYER_CUR = (None,)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

# ============================================================
# 0. 协议保护：先抢下真实 stdout，再把 stdout 让给 stderr
# ============================================================
_REAL_STDOUT = sys.stdout
try:
    _REAL_STDOUT.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.stdout = sys.stderr  # import 期 agent 内部 print 不再污染首行协议

# ============================================================
# 1. 路径装配
# ============================================================
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.dirname(BACKEND_DIR)
PROJECT_ROOT = os.path.dirname(WEBUI_DIR)
AGENT_DIR = os.path.join(PROJECT_ROOT, "agent")
for p in (PROJECT_ROOT, AGENT_DIR, BACKEND_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# 中期交互信箱（本目录 mid_turn.py，2026-09-22 从 agent/ 迁来）：与 AB 主循环
# 同进程共享的模块级单例，import 即共享，不需要任何跨进程通道。注入点走 agent 的
# 通用钩子（见 _install_runtime 7.6），agent 本体不认识本功能；
# CLI 不 import 本文件 → 没有投递入口。
import mid_turn  # noqa: E402  （BACKEND_DIR 已在上方入 path）

# ask 多卡共享窗口（纯逻辑，可单测）：一批并行 ask_user 卡共享一条 deadline，
# 有人在答就往后续，连续 ASK_TIMEOUT 秒没人答才算没人应答。
import ask_batch  # noqa: E402

# 业务失败判据：**唯一实现**在 agent 层（task_orchestrator.is_business_failure），
# 编排器 / 本文件 / 会话还原层三处同源。历史上这里自带第二份复制品、sessions 带第三份，
# 且实测不等价（sessions 那份少了 lstrip）→ 同一动作实时界面标红、刷新后标绿。
# 放在**模块顶部**（不是调用点 lazy import）：名字必须在模块作用域可见 —— 见下方戒律。
from task_orchestrator import is_business_failure  # noqa: E402
# 后台作业"自动交付"消息的前缀：常量只有一处定义（agent 侧），本文件 import 来用 ——
# _user_seq 必须把它排除在"真实用户输入"之外，否则每交付一次界面轮次就错一格。
from task_orchestrator import JOB_DELIVERY_PREFIX  # noqa: E402
# 权限模式切换通知的前缀与文案：常量只有一处定义（agent 侧 permission_modes）。
# `_user_seq` 必须把它也排除在"真实用户输入"之外，否则每切一次模式界面轮次错一格
# —— 与上面那条交付前缀同一个坑，同一套纪律。
try:
    from permission_modes import NOTICE_PREFIX as PM_NOTICE_PREFIX  # noqa: E402
except Exception:                                    # pragma: no cover
    PM_NOTICE_PREFIX = "\U0001f510 [权限模式] "

TOKEN = os.environ.get("AETHER_BRIDGE_TOKEN", "")
# 主人回答的标准窗口：**从 config 取**（那里是唯一真源，含旧名 AETHER_CLARIFY_TIMEOUT
# 的回退）。曾经这里自己 os.environ.get 读一遍 —— 同一个值两处定义（审计 L3-W1）。
try:
    import config as _cfg                      # noqa: PLC0415  （BACKEND_DIR 已在上面入 path）
    ASK_TIMEOUT = int(_cfg.ASK_TIMEOUT_SEC)
except Exception:                              # 拿不到就退回环境变量（bridge 必须能起来）
    ASK_TIMEOUT = int(os.environ.get("AETHER_ASK_TIMEOUT")
                      or os.environ.get("AETHER_CLARIFY_TIMEOUT") or "120")
ASK_TOOL = "ask_user"
ASK_WINDOW = ask_batch.WindowPool()   # 全局单批次（单回合，_TURN_GATE 保证）
# 编排器并行 worker 数（agent.py 建编排器时写死 8）。同一层的 ask_user 卡最多这么多张
# 并行挂着、共享一条等待窗口，所以 ask_user 的编排器时限按「最坏卡数」声明。
MAX_PARALLEL_CARDS = 8
MODE = os.environ.get("AETHER_AB_MODE", "")  # 扩展位：将来按 mode 换 persona/工具集

_EVENT_TYPES = {
    "turn_phase", "stage", "tool_begin", "tool_end", "progress",
    "text", "reasoning", "delta", "ask_request", "approval_request", "approval_expired",
    "done", "error", "heartbeat",
}


# ============================================================
# 2. 事件总线：广播到所有 SSE 订阅者
# ============================================================
class EventBus:
    def __init__(self, maxsize: int = 2000) -> None:
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self.seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, etype: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.seq += 1
        evt = dict(payload or {})  # 保留键后置，防止 payload 覆盖 type/seq/ts
        evt.update({"seq": self.seq, "type": etype, "ts": time.time()})
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass  # 慢消费者丢事件，绝不阻塞 agent 回合

BUS = EventBus()


# ============================================================
# 3. stdout 捕获：agent 的中期进度 print -> progress 事件
# ============================================================
class _BytesSink:
    """字节汇：接住 codecs.StreamWriter 编码后的字节，解码送回捕获器。

    存在的原因（实查）：agent_tools/multi_search.py 曾在 win32 上 import 时执行
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
    （2026-09-14 起它只在 stdout **没被宿主替换过**时才重包，所以本进程不会再触发；
      这里保留兼容写法，免得将来有人再引入同类 rewrap 时把捕获器写坏。）
    """

    def __init__(self, cap: "StdoutCapture") -> None:
        self._cap = cap

    def write(self, data) -> int:
        if isinstance(data, (bytes, bytearray)):
            self._cap._append(data.decode("utf-8", "replace"))
            return len(data)
        self._cap._append(str(data))
        return len(data)

    def flush(self) -> None:
        self._cap.flush()

    def writelines(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        pass

    @property
    def closed(self) -> bool:
        return False


class StdoutCapture:
    """把 agent 内部 print 按行转成 progress 事件（无活动回合时落 stderr）。

    兼容性：实现 detach/buffer/writable 等流协议成员，
    以便被第三方模块 detach 再包装后仍能收回输出。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._lock = threading.Lock()

    # ---- 流协议 ----
    def detach(self):
        return _BytesSink(self)

    @property
    def buffer(self):
        return _BytesSink(self)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def fileno(self) -> int:
        return sys.__stdout__.fileno()

    # ---- 写入 ----
    def write(self, s) -> int:
        if not s:
            return 0
        self._append(str(s))
        return len(s)

    def writelines(self, lines) -> None:
        for ln in lines:
            self._append(str(ln))

    def _append(self, text: str) -> None:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit_line(line)

    def _emit_line(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line.strip():
            return
        try:
            print(f"[ab] {line}", file=sys.__stderr__, flush=True)
        except Exception:
            pass
        ctx = current_run()
        if ctx is not None:
            _collect_out(ctx.run_id, line.strip()[:2000])   # 落盘版（界面还原用，见 3.6 节）
            BUS.emit("progress", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                                  "round": _current_round(ctx.run_id),
                                  "text": line.strip()[:2000]})

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._emit_line(self._buf)
            self._buf = ""


# ============================================================
# 4. 摘要工具（时间线用，控制事件体积）
# ============================================================
def brief(obj: Any, limit: int = 700) -> str:
    try:
        if isinstance(obj, dict):
            parts = []
            for k, v in list(obj.items())[:12]:
                if isinstance(v, (str, int, float, bool)) or v is None:
                    sval = "" if v is None else str(v).replace("\n", " ")
                    if len(sval) > 120:
                        sval = sval[:120] + "…"
                    parts.append(f"{k}={sval}")
                elif isinstance(v, (list, tuple)):
                    sval = ", ".join(str(x) for x in v[:8])
                    if len(v) > 8:
                        sval += f", …(+{len(v) - 8})"
                    if not sval:
                        sval = "(空)"
                    if len(sval) > 160:
                        sval = sval[:160] + "…"
                    parts.append(f"{k}=[{sval}]")
                else:
                    parts.append(f"{k}=<{type(v).__name__}>")
            text = " ".join(parts)
        else:
            text = str(obj)
    except Exception as e:  # noqa: BLE001
        text = f"<摘要失败 {type(e).__name__}>"
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + f"…(+{len(text) - limit}字)"
    return text


# ============================================================
# 5. 回合（run）管理
# ============================================================
class RunContext:
    def __init__(self, run_id: str, session_id: str, message: str,
                 attachments=None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.message = message
        # 附件：相对项目根的路径列表（可为空）。只搬元数据，不读内容 ——
        # 物化在 agent 咽喉做（见 _compose_user_content 的说明）。
        self.attachments = [str(x).strip() for x in (attachments or []) if str(x or "").strip()]
        self.thread_id: Optional[int] = None
        self.started_at = time.time()
        self.interrupt_requested = False
        self.tool_count = 0
        self.active_tools = 0
        self.lock = threading.Lock()
        self.result: Optional[Dict[str, Any]] = None
        # 回合是否已收口（_execute_turn 的 finally 置位）。
        # 后台作业会活过它所属的回合，收尾时**不许**再改回合状态 —— 见 _wrap_tool。
        self.finished = False


_RUNS: Dict[str, RunContext] = {}
_RUNS_LOCK = threading.RLock()  # 可重入：历史教训见 _stop_run（不可重入锁嵌套 acquire 会自死锁）
_TURN_GATE = threading.Lock()  # v1：同一 bridge 一次只跑一个回合（与 CLI 语义一致）

# 活动回合指针。工具跑在编排器的 worker 线程里（不是回合线程），
# 只按线程 ID 匹配会让 tool_begin/tool_end 全部丢失 —— 实查踩过这个坑。
# v1 单回合（_TURN_GATE 保证），故用全局指针归属即可。
_CURRENT: Optional["RunContext"] = None
_CURRENT_LOCK = threading.Lock()


def _set_current(ctx: Optional["RunContext"]) -> None:
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = ctx


def current_run() -> Optional[RunContext]:
    """归属当前事件应该挂到哪个回合。

    先按线程匹配（回合线程自身），否则取活动回合指针
    （编排器 worker 线程里的工具调用、以及工具内部的 print）。
    """
    tid = threading.get_ident()
    with _RUNS_LOCK:
        for ctx in _RUNS.values():
            if ctx.thread_id == tid:
                return ctx
    with _CURRENT_LOCK:
        return _CURRENT


def active_run() -> Optional[RunContext]:
    with _RUNS_LOCK:
        for ctx in _RUNS.values():
            if ctx.thread_id:
                return ctx
    return None


def _set_turn(ctx: RunContext, phase: str, **extra: Any) -> None:
    BUS.emit("turn_phase", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                            "phase": phase, **extra})


def _inject_keyboard_interrupt(tid: int) -> bool:
    """向指定线程抛 KeyboardInterrupt —— 让 agent 自己的 except 分支保存退出。

    这是不改 agent 源码的唯一正规停止通道：agent.py 的回合循环只在
    KeyboardInterrupt 时 save_session(status='interrupted')。
    线程若阻塞在 C 层（网络/锁），异常在下一个字节码边界生效。
    """
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt))
    if res == 0:
        return False
    if res > 1:  # 波及多个线程，撤销以免误伤
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        return False
    return True


# ============================================================
# 6. ask 通道（ask_user 的真实实现）
# ============================================================
# ============================================================
# 5.5 编排器状态机上报（运行期包装，零改 agent/）
# ============================================================
# 为什么必须包装：ToolPipeline 是 _run_one 里的局部变量，编排器不留存清单，
# 外部既看不到 pending（排队中）也看不到层号；而界面要"和编排器对齐颗粒度"。
# 全部包在 try/except 里：观测面绝不允许影响工具执行本身。
_PIPES: Dict[str, Dict[str, Any]] = {}
_PIPE_ORDER: List[str] = []
_PIPE_LOCK = threading.Lock()
_PIPE_MAX = 240                      # 长跑进程里防累积：终态即删 + 兜底裁剪
_TLS = threading.local()             # 让被包装的工具函数能看到真实 tool_call.id
PATCH_STATE = {"tpl": False, "pipe": False, "batch": False}


def _job_info(tc) -> "tuple":
    """(是不是跨回合作业, 作业号, 可读名) —— **以作业登记册为准**，不看入参。

    为什么读 `metadata["job"]` 而不是 `metadata["background"]`：超时被收编成作业的调用
    （`_adopt_background`）从来没有 background 入参，但它确实是跨回合作业 ——
    只看入参会把这种作业漏成"普通管道"，橙点自然亮不起来。
    """
    md = getattr(tc, "metadata", None) or {}
    job = md.get("job")
    if job is not None:
        return True, getattr(job, "job_id", None), getattr(job, "label", None)
    return bool(md.get("background")), None, None


def _pipe_emit(status: str, tc_id: str, tool: str, elapsed=None, layer=None,
               background=None, job_id=None, job_label=None) -> None:
    # 白名单外的未知状态一律降级为 failed：_PIPES 只认 done|failed|cancelled，
    # 透传未知值会让槽位永久挂在 pending 态（biz_failed 就是这种值）。
    if status not in ("pending", "running", "done", 
                    "failed", "cancelled"):
        status = "failed"
    try:
        ctx = active_run()
        with _PIPE_LOCK:
            if status in ("done", "failed", "cancelled"):
                _PIPES.pop(tc_id, None)
                if tc_id in _PIPE_ORDER:
                    _PIPE_ORDER.remove(tc_id)
            else:
                if tc_id not in _PIPES:
                    _PIPE_ORDER.append(tc_id)
                    for old in _PIPE_ORDER[:-_PIPE_MAX]:
                        _PIPES.pop(old, None)
                _prev = _PIPES.get(tc_id) or {}
                _PIPES[tc_id] = {"tool": tool, "status": status, "layer": layer,
                                 # 作业身份一旦拿到就不许被后续帧抹掉（pending 帧还没有它）
                                 "background": bool(background) or bool(_prev.get("background")),
                                 "job_id": job_id or _prev.get("job_id"),
                                 "job_label": job_label or _prev.get("job_label")}
        BUS.emit("pipeline", {
            "run_id": ctx.run_id if ctx else None,
            "session_id": ctx.session_id if ctx else None,
            "tc_id": tc_id, "tool": tool, "status": status,
            "layer": layer, "elapsed": elapsed,
            "thread": threading.current_thread().name,   # 槽位身份：orch-parallel_3 等
            "background": bool(background),   # 跨回合作业占用的槽位（前端画橙色）
            "job_id": job_id or None,         # 作业身份：让橙点能说出"是哪个作业在跑"
            "job_label": job_label or None,   # 可读名（工具名+参数摘要）
            "live": len(_PIPE_ORDER),
        })
    except Exception:
        pass


def _reset_pipes_for_turn() -> int:
    """回合起点清场：清掉上一回合的管道视图，**但跨回合作业的占用不清**。

    作业的定义就是"活过它出生的那个回合"。回合一起就把它的条目抹掉，等于
    前端唯一的真状态来源先瞎了 —— 橙点会在**下一个回合开始时**熄灭
    （真机现象：汇报完那盏灯立马灭了）。返回保住了几个槽位，供日志/诊断用。
    """
    kept = 0
    with _PIPE_LOCK:
        for tc_id in list(_PIPES.keys()):
            v = _PIPES.get(tc_id) or {}
            if v.get("background") and v.get("status") in ("running", "pending"):
                kept += 1
                continue
            _PIPES.pop(tc_id, None)
            if tc_id in _PIPE_ORDER:
                _PIPE_ORDER.remove(tc_id)
    return kept


def _orch_view() -> Dict[str, Any]:
    """编排器的**权威占用视图**（前端刷新/重连/定时对表都读它）。

    两个来源合并：
      · `_PIPES`    —— 谁占着哪个槽（线程名就是槽位身份）；
      · 作业登记册  —— 跨回合作业是否**还活着**（它活过回合边界，所以 `_PIPES`
        里没有它时也必须出现在这里）。

    合成规则一句话：**每一个还活着的作业，都必须在这个视图里占一格。**
    反向也成立：作业已结算 -> 不再算占用（否则橙点永远灭不掉）。

    纯读操作，任何异常都不许炸 —— 它是刷新路径上的常驻调用。
    """
    try:
        orch = getattr(_agent, "_ORCHESTRATOR", None)
        reg = getattr(orch, "jobs", None)
        jobs = list(reg.list_jobs(include_finished=True)) if reg is not None else []
    except Exception:
        jobs = []
    by_tc: Dict[str, Any] = {}
    for j in jobs:
        tc_id = getattr(j, "tool_call_id", "") or ""
        if tc_id:
            by_tc[tc_id] = j
    out: List[Dict[str, Any]] = []
    seen = set()
    with _PIPE_LOCK:
        raw = [(k, dict(v)) for k, v in _PIPES.items()]
    for tc_id, v in raw:
        seen.add(tc_id)
        if v.get("background") and reg is not None:
            # ── 跨回合占用**以登记册为准**（2026-09-30 真机修）──
            # `task_kill` 会把作业从登记册 drop 掉，而它的工具线程还在取消令牌上收尾。
            # 旧实现在这里查不到作业就"原样上报 running" -> 前端对表时又把橙灯维持住 ->
            # 「进程确实停了，灯却一直亮」。现在：
            #   · 登记册里查不到  -> **不是"看不见"，而是"确实没了"** -> 不算占用（出局）；
            #   · 查得到但已结算  -> 同样出局；
            #   · 查得到且还活着  -> 占用，并把身份/时长补上。
            job = by_tc.get(tc_id)
            if job is None or not job.alive:
                continue
            v["job_id"] = job.job_id
            v["job_label"] = job.label
            v["elapsed"] = round(job.elapsed, 1)
        v["tc_id"] = tc_id
        out.append(v)
    for tc_id, j in by_tc.items():
        if tc_id in seen or not j.alive:
            continue
        # 活着的作业却没有管道条目（池满排队 / 那一帧丢了）：补一格，**绝不让它消失**
        out.append({"tc_id": tc_id, "tool": j.tool, "status": "running", "layer": None,
                    "background": True, "thread": None, "elapsed": round(j.elapsed, 1),
                    "job_id": j.job_id, "job_label": j.label})
    return {"ok": True, "pipes": out}


def _sweep_dead_background_pipes(reason: str = "cancelled") -> int:
    """把"作业已经不在登记册里"的跨回合占用**收掉**（发终态帧 + 从视图移除）。

    为什么需要它（2026-09-30 真机）：`task_kill` 会把作业从登记册 **drop** 掉，而那个作业的
    工具线程还在自己的取消令牌上收尾 —— 于是 `_PIPES` 里那条 `background/running` 条目
    **没人来终结**，界面上那盏橙灯就一直亮着：进程确实停了，灯却还亮。

    只在**登记册可读**时动手；读不到就原样返回（fail-open：宁可多亮一格，
    也不误杀一个可能还在跑的作业）。返回收掉了几个。

    调用点：每个工具跑完（`pipe.run` 的 finally）—— 于是 `task_kill` 一返回，
    那盏灯下一秒就灭，不必等前端 5 秒一次的对表。
    """
    try:
        orch = getattr(_agent, "_ORCHESTRATOR", None)
        reg = getattr(orch, "jobs", None)
        if reg is None:
            return 0
        live = list(reg.list_jobs(include_finished=False))
        # 有作业没带 tool_call_id 时无法安全匹配 -> 一个都不动（fail-open）
        if any(not (getattr(j, "tool_call_id", "") or "") for j in live):
            return 0
        alive_tc = {j.tool_call_id for j in live}
        with _PIPE_LOCK:
            dead = [(tc_id, (v or {}).get("tool"))
                    for tc_id, v in _PIPES.items()
                    if (v or {}).get("background") and tc_id not in alive_tc]
    except Exception:
        return 0
    for tc_id, tool in dead:
        _pipe_emit(reason, tc_id, tool or "?", background=True)
    return len(dead)


def _patch_warn(what: str) -> None:
    """patch 目标消失时把话说清楚 —— 别让 WebUI 静默瘫一半。"""
    try:
        print("[bridge] ⚠️ %s 不存在，该 hook 无法安装（agent/ 侧可能已重构，"
              "请同步 bridge 的 patch 目标；界面会缺少对应的事件）" % what,
              file=sys.__stderr__, flush=True)
    except Exception:
        pass


def install_orchestrator_watch(orch) -> Dict[str, bool]:
    """包装 ToolTemplate.create_pipeline / ToolPipeline.run / _run_on_pool。

    每个目标都用 getattr 取（2026-09-17）：曾经直接 `tpl.create_pipeline` ——
    agent/ 侧一改名就 AttributeError 冒泡，只能靠调用方的 try/except 兜住，
    诊断信息也落在几百行之外。现在缺哪个就点名哪个，函数本身照常返回
    （对应项为 False），调用方按返回值判断即可。
    """
    try:
        import task_orchestrator as tot
    except Exception:
        return PATCH_STATE
    try:
        tpl, pipe = tot.ToolTemplate, tot.ToolPipeline
    except AttributeError:
        return PATCH_STATE
    if not getattr(orch, "_abw_watch", None):
        if not getattr(tpl, "_abw_patched", False):
            orig_cp = getattr(tpl, "create_pipeline", None)
            if orig_cp is None:
                _patch_warn("ToolTemplate.create_pipeline")
            else:
                def create_pipeline(self, tool_call):
                    p = orig_cp(self, tool_call)
                    _bg0, _jid0, _jlab0 = _job_info(tool_call)
                    _pipe_emit("pending", getattr(tool_call, "id", "?"),
                               getattr(self, "name", "?"), background=_bg0,
                               job_id=_jid0, job_label=_jlab0)
                    return p

                tpl.create_pipeline = create_pipeline
                tpl._abw_patched = True
                PATCH_STATE["tpl"] = True
        if not getattr(pipe, "_abw_patched", False):
            orig_run = getattr(pipe, "run", None)
            if orig_run is None:
                _patch_warn("ToolPipeline.run")
            else:
                def run(self):
                    tc = getattr(self, "tool_call", None)
                    tc_id = getattr(tc, "id", "?")
                    tool = getattr(getattr(self, "template", None), "name", "?")
                    _TLS.tc_id = tc_id
                    lay = None
                    try:
                        lay = _LAYER_CUR[0]
                    except Exception:
                        lay = None
                    # 作业身份以**登记册**为准（超时收编的作业没有 background 入参，
                    # 但它确实是跨回合作业）。终态帧也必须带上：前端靠它把那格橙色收掉。
                    _bg, _jid, _jlab = _job_info(tc)
                    _pipe_emit("running", tc_id, tool, layer=lay, background=_bg,
                               job_id=_jid, job_label=_jlab)
                    try:
                        r = orig_run(self)
                        st = getattr(self, "status", None) or ("done" if getattr(r, "success", True) else "failed")
                        # 新版 orchestrator 会标 biz_failed；_pipe_emit 白名单只认 done|failed|cancelled，
                        # 未知状态会被当作活跃槽位挂住不放，所以必须映射（getattr 兜住旧版无此字段）
                        if st == "biz_failed" or getattr(r, "biz_fail", False):
                            st = "failed"
                        _pipe_emit(st, tc_id, tool, elapsed=round(self.elapsed, 3), layer=lay,
                                   background=_bg, job_id=_jid, job_label=_jlab)
                        return r
                    except BaseException as e:
                        _pipe_emit("failed", tc_id, tool, layer=lay,
                                   background=_bg, job_id=_jid, job_label=_jlab)
                        raise
                    finally:
                        _TLS.tc_id = None
                        # 收尾扫一遍：被 task_kill 掉（已从登记册 drop）的跨回合占用要立刻出局，
                        # 否则那盏橙灯只能等前端 5 秒一次的对表才灭。
                        _sweep_dead_background_pipes()

                pipe.run = run
                pipe._abw_patched = True
                PATCH_STATE["pipe"] = True
    if not getattr(orch, "_abw_batch", False):
        orig_pool = getattr(orch, "_run_on_pool", None)
        if orig_pool is None:
            _patch_warn("TaskOrchestrator._run_on_pool")
        else:
            def _on_pool(pool, tasks, timeout=None, _orig=orig_pool, _orch=orch):
                global _LAYER_CUR
                try:
                    _LAYER_SEQ[0] += 1
                    _LAYER_CUR = (_LAYER_SEQ[0],)
                    label = "serial" if pool is getattr(_orch, "_serial_pool", None) else "parallel"
                except Exception:
                    pass
                return _orig(pool, tasks, timeout)

            orch._run_on_pool = _on_pool
            orch._abw_batch = True
            PATCH_STATE["batch"] = True
    orch._abw_watch = True
    return PATCH_STATE


def _pool_capacity(orch) -> Dict[str, Any]:
    """编排器池容量 + 线程名前缀 —— 前端据此预画固定槽位。

    关键：ThreadPoolExecutor 是懒起线程的，没用过的槽位在任何事件里都不会出现，
    所以"9 个常驻坑位"必须由容量显式告知，不能靠观察过的线程名倒推。
    """
    out = {"parallel": {"workers": 16, "prefix": "orch-parallel"},
           "serial": {"workers": 1, "prefix": "orch-serial"}}
    try:
        out["parallel"]["workers"] = int(getattr(orch, "max_workers", 8) or 8)
        out["serial"]["workers"] = int(getattr(orch, "serial_workers", 1) or 1)
        for key, pool in (("parallel", getattr(orch, "_parallel_pool", None)),
                          ("serial", getattr(orch, "_serial_pool", None))):
            pre = getattr(pool, "_thread_name_prefix", None)
            if pre:
                out[key]["prefix"] = str(pre)
            th = getattr(pool, "_threads", None)
            if th is not None:
                out[key]["started"] = len(th)
    except Exception:
        pass
    return out


def _ask_channel(question: str, options: List[str], kind: str,
                     timeout: Optional[int] = None) -> str:
    """问主人一个问题并阻塞等待答复。

    **支持同批并行多张卡**：一批卡的等待时间由 ASK_WINDOW 统一管理 ——
    有人作答就往后续窗，不会出现"人还在一张张答，第二三张卡已经到点"的静默吞答复。
    每张卡仍各有一个 ask_id / waiter / box，互不干扰；谁的答复回给谁。
    """
    ctx = active_run()
    ask_id = uuid.uuid4().hex[:12]
    waiter = threading.Event()
    box: Dict[str, Any] = {"answer": None}
    window = int(timeout or ASK_TIMEOUT)
    batch = ASK_WINDOW.join(window)       # 加入（或开启）本批共享窗口

    with _ASK_LOCK:
        # 与审批同一套道理：提问也得在服务端留一份，主人刷新/切会话才拿得回来。
        _ASK[ask_id] = {"waiter": waiter, "box": box, "ask_id": ask_id,
                            "question": question, "options": list(options or []),
                            "mode": kind, "timeout": window,
                            "born": time.time(),
                            "batch_id": batch["batch_id"], "batch_size": batch["size"],
                            "run_id": ctx.run_id if ctx else None,
                            "session_id": ctx.session_id if ctx else None}

    if ctx:
        _set_turn(ctx, "ASK_WAIT")
    BUS.emit("ask_request", {
        "run_id": ctx.run_id if ctx else None,
        "session_id": ctx.session_id if ctx else None,
        "ask_id": ask_id, "question": question, "options": options,
        "mode": kind, "timeout": window,
        # 批次信息：前端要显示「共 N 问 · 还有 M 个待答」，倒计时也用共享窗口的真值
        "batch_id": batch["batch_id"], "batch_size": batch["size"],
        "batch_live": batch["live"], "remaining": int(batch["remaining"]),
    })

    got = False
    try:
        # 等**共享窗口**，而不是各卡各算一个固定秒数：后者在并行多卡时，
        # 主人答第一张的时间里，后面几张就已经被自己的计时判死了。
        while True:
            rem = ASK_WINDOW.remaining()
            if rem <= 0:
                break
            if waiter.wait(min(rem, 0.5)):
                got = True
                break
    finally:
        with _ASK_LOCK:
            _ASK.pop(ask_id, None)
            left = sum(1 for x in _ASK.values()
                       if x.get("run_id") == (ctx.run_id if ctx else None))
        ASK_WINDOW.leave()
        # 只有本回合一张卡都不剩了，才把回合状态交还主循环 ——
        # 否则界面上还有卡挂着，状态却已显示「思考中」。
        if ctx and not left:
            _set_turn(ctx, "THINKING")
    if not got:
        return "⏱️ 主人在限定时间内没有回答。请基于合理假设继续，并在回复里说明你做了什么假设。"
    answer = box.get("answer") or ""
    if answer.startswith("__CANCEL__"):
        return "主人取消了这个回合，请停止后续动作并简短收尾。"
    return answer


def _cancel_all_ask(reason: str = "stopped") -> int:
    """让所有挂着的卡立即出局（回合被终止、回合收尾时用）。

    不这么做：主人点了停止，卡还留在界面上，worker 线程还要把整个窗口等满 ——
    而那些等待者永远等不到人（回合已经结束了），卡会在屏幕上多挂几分钟。
    """
    with _ASK_LOCK:
        items = list(_ASK.values())
    for it in items:
        box, waiter = it.get("box"), it.get("waiter")
        if box is None or waiter is None:
            continue
        box["answer"] = "__CANCEL__ %s" % reason
        waiter.set()
    if items:
        ASK_WINDOW.reset()
    return len(items)


_ASK: Dict[str, Any] = {}
_ASK_LOCK = threading.Lock()


# ============================================================
# 7. 零侵入注入：ask_user 工具 + 工具事件包装
# ============================================================
_agent = None          # agent 模块
_ToolCall = None
_injected: List[str] = []
ORCH_STATE: Dict[str, Any] = {"value": "pending"}
# 审批系统状态：注册结果 + 自检探针结论，供 /agent/status 与前端徽标查询
APPROVAL_STATE: Dict[str, Any] = {"value": "pending"}


# ============================================================
# 7.05 token 计量：直接从 API 返回值读 usage（零改 agent.py 源码）
# client 是 agent.py 的模块级对象，替换它绑定的 create 方法即对本进程内
# 所有调用生效。stream=False（compose_chat_kwargs 写死），故 response.usage
# 一定带在对象上，不需要 stream_options。
# ============================================================
USAGE: Dict[str, Any] = {
    "total": {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0, "total": 0},
    "by_sid": {},
}
USAGE_LOCK = threading.Lock()


def _usage_node() -> Dict[str, int]:
    # last_prompt = 最近一次请求的输入 token（= 当前上下文占用，压缩后会回落）；
    # cached = 命中前缀缓存的输入 token（厂商不返回则恒为 0）
    return {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0, "total": 0,
            "cached": 0, "last_prompt": 0}


def _pick_cached(u, extra) -> int:
    """命中缓存的输入 token：各家常放在 prompt_tokens_details.cached_tokens /
    prompt_cache_hit_tokens 等字段里，逐个探，拿不到就 0（前端显示 —）。"""
    cands = []
    for src in (u, extra or {}):
        if isinstance(src, dict):
            for key in ("prompt_tokens_details", "input_tokens_details"):
                cands.append(src.get(key))
        else:
            cands.append(getattr(src, "prompt_tokens_details", None))
    for det in cands:
        for key in ("cached_tokens", "cache_hit_tokens", "prompt_cache_hit_tokens"):
            try:
                v = int((det.get(key) if isinstance(det, dict) else getattr(det, key, 0)) or 0)
            except (TypeError, ValueError, AttributeError):
                v = 0
            if v > 0:
                return v
    return 0


_USAGE_CORE = {"prompt_tokens", "completion_tokens", "total_tokens",
               "completion_tokens_details", "prompt_tokens_details"}


def _norm_usage(resp):
    """把各家 OpenAI 兼容端点的 usage 归一成一套字段（纯函数，可离线单测）。

    返回 None 表示这次拿不到量；拿到则给 prompt/completion/total/reasoning/model/extra。
    SDK 的 CompletionUsage 是 extra=allow，厂商私有字段不会丢（都在 model_extra 里），
    一并回传 extra，以后适配新厂商时可直接看原始值。
    """
    if resp is None:
        return None
    u = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
    if u is None:
        return None

    if isinstance(u, dict):
        extra = {k: v for k, v in u.items() if k not in _USAGE_CORE}
    else:
        extra = dict(getattr(u, "model_extra", None) or {})

    def pick(names):
        for nm in names:
            try:
                v = u.get(nm) if isinstance(u, dict) else getattr(u, nm, None)
            except Exception:
                continue
            if v is None:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                return iv
        return None

    # 命名差异：OpenAI / DeepSeek / 智谱 / Kimi / Ollama-compat 走 *_tokens；
    # Anthropic 原生与部分转发端点走 input_tokens / output_tokens。
    prompt = pick(["prompt_tokens", "input_tokens"]) or 0
    completion = pick(["completion_tokens", "output_tokens"]) or 0
    total = pick(["total_tokens"]) or 0
    if total <= 0:
        total = prompt + completion

    # 思考链 token：按字段族逐个试，命中即止；再兜底扫一遍私有字段里的 *_details。
    if isinstance(u, dict):
        details = [u.get("completion_tokens_details"), u.get("output_tokens_details"),
                   u.get("reasoning_details")]
    else:
        details = [getattr(u, "completion_tokens_details", None),
                   getattr(u, "output_tokens_details", None),
                   getattr(u, "reasoning_details", None)]
    reasoning = 0
    for det in details:
        rv = 0
        if isinstance(det, dict):
            rv = det.get("reasoning_tokens")
        elif isinstance(det, (int, float)):
            rv = det
        elif det is not None:
            rv = getattr(det, "reasoning_tokens", None)
        try:
            rv = int(rv or 0)
        except (TypeError, ValueError):
            rv = 0
        if rv > 0:
            reasoning = rv
            break
    if reasoning == 0:
        for kv in (extra or {}).values():
            if not isinstance(kv, dict):
                continue
            try:
                rv = int(kv.get("reasoning_tokens") or 0)
            except (TypeError, ValueError, AttributeError):
                rv = 0
            if rv > 0:
                reasoning = rv
                break

    model = resp.get("model") if isinstance(resp, dict) else getattr(resp, "model", None)
    if not isinstance(model, str) or not model:
        mv = (extra or {}).get("model")
        model = mv if isinstance(mv, str) else None

    return {"prompt": prompt, "completion": completion, "total": total,
            "reasoning": reasoning, "cached": _pick_cached(u, extra),
            "model": model, "extra": extra}


# ============================================================
# 7.06 计量账本：跨网关重启常驻
# 主人 2026-09-13 要求：概况与上下文占比「任何情况下都在」，不必先发一条消息才有数。
# 做法：每次采到 usage 后落盘，bridge 启动时载入继续累加；前端用 hello 里的账本校准。
# 口径与概况卡一致（只覆盖 WebUI 回合）；文件损坏/写失败一律降级为空账本，绝不拖垮回合。
# ============================================================
LEDGER_PATH = os.path.join(
    os.environ.get("AETHER_USAGE_LEDGER") or os.path.join(PROJECT_ROOT, "agent_webui", ".state"),
    "usage_ledger.json",
)


def _load_ledger() -> str:
    """载入上次的计量（重启后接着累计，而不是从 0 起）。"""
    try:
        if not os.path.exists(LEDGER_PATH):
            return "empty"
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with USAGE_LOCK:
            for sid, node in ((data or {}).get("by_sid") or {}).items():
                if not isinstance(node, dict):
                    continue
                base = _usage_node()
                for k in list(base):
                    try:
                        base[k] = int(node.get(k) or 0)
                    except (TypeError, ValueError):
                        base[k] = 0
                USAGE["by_sid"][str(sid)] = base
            tot = (data or {}).get("total") or {}
            for k in list(USAGE["total"]):
                try:
                    USAGE["total"][k] = int(tot.get(k) or 0)
                except (TypeError, ValueError):
                    USAGE["total"][k] = 0
            n = len(USAGE["by_sid"])
        return "loaded(%d 会话)" % n
    except Exception as e:
        return "failed: %s: %s" % (e.__class__.__name__, e)


def _save_ledger() -> None:
    """原子落盘。每轮调用一次（几 KB），失败只忽略。"""
    try:
        with USAGE_LOCK:
            snap = {"total": dict(USAGE["total"]),
                    "by_sid": {k: dict(v) for k, v in USAGE["by_sid"].items()}}
        d = os.path.dirname(LEDGER_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        pass


def _ledger_snapshot() -> Dict[str, Any]:
    with USAGE_LOCK:
        return {"total": dict(USAGE["total"]),
                "by_sid": {k: dict(v) for k, v in USAGE["by_sid"].items()}}


class _MeteredStream:
    """把底层 Stream 包一层，好在 agent 收完流之后把 usage 结算掉（计量用）。

    时序是这个类的全部难点（2026-10-02 连踩两次）：
      · 生成器函数不行：`for x in gen` 的语义是"**下一个**元素取不到时才跑 finally"，
        而 agent 是 `for ... in stream:` **之后**才挂 usage —— finally 永远早于挂载。
      · 只在 __next__ 抛 StopIteration 时结算也不行：那一刻循环体已经结束、但**循环
        之后的代码还没跑**，所以 agent 的挂载仍然更晚。实测：对象对、时机错，
        "结算时读到的 usage = <缺失>"。
      · 正解 = **agent 主动通知**：本类把自己注册成 `_ab_usage_sink`，agent 收完流调它。
        这同时也是"宿主注入"模式的既有做法（agent 只认对象上有没有这个可调用属性，
        不认识 bridge）。

    仍然保留 `_ab_usage` 属性与排空结算作为**兜底**（万一 agent 那侧没调 sink）。
    它同时是透明代理：agent 写的属性写在**代理**上（`__setattr__` 不转发给底层），
    读取时先看自己再看底层。
    """

    def __init__(self, inner, kwargs):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_kwargs", kwargs)
        object.__setattr__(self, "_it", None)
        # 两个各自独立的闸：
        #   _counted      —— 已经记过账（任何路径都只有效一次）
        #   _sink_settled —— agent 已经把 usage 交过来了（权威已到，兜底一律让位）
        object.__setattr__(self, "_counted", False)
        object.__setattr__(self, "_sink_settled", False)
        # agent 收完流会调这个（它只认"对象上有没有这个可调用属性"）。
        # **这是主路径**：`for x in it` 在 __next__ 抛 StopIteration 时就结束了，
        # 所以我自己在迭代结束点结算是**早于** agent 挂 usage 的（时序坑见类注释）。
        object.__setattr__(self, "_ab_usage_sink", self._on_usage)

    def _on_usage(self, usage) -> None:
        """agent 把 usage 交过来：**这就是权威信号**，立刻结算。

        ⚠️ 这里**不能**用 `_counted` 做去重：排空兜底（`_account_once`）会在
        `__next__` 抛 StopIteration 的那一刻先跑一遍 —— 那时 usage 还没挂上、什么都没记到，
        却会把 `_counted` 立起来，于是权威信号反被自己的去重标志挡掉。
        （2026-10-02 连踩两次，第二次就是这个：账本一条不记、界面显示"本会话还没有计量"。）
        去重只认 `_sink_settled` 自己。
        """
        if usage is None:
            return
        if object.__getattribute__(self, "_sink_settled"):
            return
        object.__setattr__(self, "_sink_settled", True)   # 权威已到：兜底一律让位
        try:
            _account_usage_obj(usage, object.__getattribute__(self, "_kwargs"))
        except BaseException:
            pass

    def __iter__(self):
        return self

    def __next__(self):
        it = self._it
        if it is None:
            inner = object.__getattribute__(self, "_inner")
            it = inner.__iter__() if hasattr(inner, "__iter__") else iter(inner)
            object.__setattr__(self, "_it", it)
        try:
            return next(it)                      # ← 不是 next()，要触发 StopIteration
        except StopIteration:
            self._account_once()
            raise
        except BaseException:
            self._account_once()                 # 中途异常也把已拿到的量结算掉
            raise

    def _account_once(self) -> None:
        """兜底结算：agent **没有**主动交过 usage 时才记。

        两个闸都要看：
          · `_sink_settled` —— 权威已到，这里必须让位（否则 close()/排空会把同一笔账再记一遍）；
          · `_counted`      —— 只由**真正记过账**的路径立起来（见 _on_usage 的说明：
            兜底自己不许立这个标志，否则会把权威信号挡掉）。
        """
        if object.__getattribute__(self, "_counted"):
            return
        if object.__getattribute__(self, "_sink_settled"):
            return
        object.__setattr__(self, "_counted", True)
        try:
            _account_streamed(self, object.__getattribute__(self, "_kwargs"))
        except BaseException:
            pass

    def __getattr__(self, name):
        # 只在自身找不到时才到这儿：读底层 Stream 的属性（SDK 的流有些属性是惰性取的）
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

    def close(self):
        try:
            inner = object.__getattribute__(self, "_inner")
            if hasattr(inner, "close"):
                inner.close()
        finally:
            self._account_once()


def _install_usage_watch(ab_agent) -> str:
    """包装 client.chat.completions.create，把每次 API 返回的 usage 发成 usage 事件。"""
    client = getattr(ab_agent, "client", None)
    comp = getattr(getattr(client, "chat", None), "completions", None)
    orig = getattr(comp, "create", None)
    if orig is None or getattr(orig, "_ab_webui_usage", False):
        return "failed"

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        resp = orig(*args, **kwargs)
        try:
            if str(kwargs.get("stream")).lower() == "true":
                # 流式（2026-10-02 起默认）：usage 不在返回对象上，而在流的**最后一个
                # chunk** 里（agent 侧带了 stream_options.include_usage），agent 收完流会
                # 把它挂到"它收到的那个可迭代对象"上。所以这里交出去的必须是
                # _MeteredStream —— 它在**排空之后**才去读那份 usage（见类注释里的时序坑）。
                # 这是"改流式会让计量静默停摆"（审计 L3-W6）的正面修法：
                # 不再是发一条 unmetered 就算了，而是**真的把量记上**。
                if resp is not None and hasattr(resp, "__iter__"):
                    return _MeteredStream(resp, kwargs)
                # 不是可迭代的（端点忽略了 stream 直接给了整包）→ 退回普通记账
                _account_usage(resp, kwargs)
                return resp
            _account_usage(resp, kwargs)
        except BaseException as e:       # 计量绝不允许拖垮回合
            try:
                print("[bridge] usage 采集失败: " + type(e).__name__ + ": " + str(e),
                      file=sys.__stderr__, flush=True)
            except Exception:
                pass
        return resp

    wrapper._ab_webui_usage = True
    comp.create = wrapper
    return "on"


def _account_streamed(handed, kwargs) -> None:
    """流跑完后结算 usage：从**交给 agent 的那个可迭代对象**（_MeteredStream）上取。

    为什么不是底层 Stream：create() 的返回值会被 _MeteredStream 包一层再交给 agent，
    agent 挂 `_ab_usage` 是挂在**它收到的那个对象**上的。盯错对象 → 一条都不记
    （2026-10-02 就是这个 bug，界面显示"本会话还没有计量"）。

    取不到就什么都不做（宁可少记一次量，也不许瞎记）。
    """
    usage = getattr(handed, "_ab_usage", None)
    if usage is None:
        return
    _account_usage_obj(usage, kwargs)


def _account_usage(resp, kwargs) -> None:
    """整包返回路径的记账。"""
    n = _norm_usage(resp)
    if n is None:
        return
    _account_norm(n, kwargs)


def _account_usage_obj(usage, kwargs) -> None:
    """流式路径的记账：usage 是从最后一个 chunk 上摘下来的对象，形状与整包一致。"""
    if usage is None:
        return
    try:
        n = _norm_usage({"usage": usage})
    except Exception:
        n = None
    if n is None:
        return
    _account_norm(n, kwargs)


def _account_norm(n, kwargs) -> None:
    """把归一化后的用量累进账本 + 发 usage 事件（两条路径共用，口径只此一处）。"""
    prompt, completion = n["prompt"], n["completion"]
    total, reasoning = n["total"], n["reasoning"]
    cached = int(n.get("cached") or 0)
    ctx = current_run()          # 工具跑在别的线程，这里一定是回合线程
    sid = ctx.session_id if ctx else None
    with USAGE_LOCK:
        targets = [USAGE["total"]]
        if sid:
            targets.append(USAGE["by_sid"].setdefault(sid, _usage_node()))
        for node in targets:
            node["calls"] += 1
            node["prompt"] += prompt
            node["completion"] += completion
            node["reasoning"] += reasoning
            node["total"] += total
            node["cached"] = node.get("cached", 0) + cached
            node["last_prompt"] = prompt      # 当前上下文占用（压缩后回落）
        tot = USAGE["total"]
        sess = dict(USAGE["by_sid"].get(sid)) if sid else None
    thin_extra = {k: v for k, v in (n["extra"] or {}).items()
                  if isinstance(v, (int, float, str, bool))} or None
    BUS.emit("usage", {
        "run_id": ctx.run_id if ctx else None,
        "session_id": sid,
        "call": {"prompt": prompt, "completion": completion,
                 "total": total, "reasoning": reasoning, "cached": cached},
        "session": sess,
        "global": dict(tot),
        "model": n["model"] or kwargs.get("model"),
        "extra": thin_extra,
    })
    _save_ledger()               # 计量账本落盘：下次重启接着累计



# 业务失败判定（2026-09-09 加）。部分工具（file_read / execute_shell / execute_python / rag /
# create_tool 共 27 处）失败时是【返回以 ❌ 开头的字符串】而不是抛异常。落盘还原
# （sessions.py:132 的 startswith(❌)）认得这种失败，而实时链路只看异常 —— 于是同一批调用
# 实时显示成功、切走再切回显示失败。这里补上，两条链口径对齐。
#
# 戒律（上次事故：引用了未进模块作用域的名字，把全部工具的返回值换成了 NameError）：
#   1) 判定函数与调用点同在 bridge.py 模块作用域，绝不跨模块取名字、不用函数内 import；
#   2) 判定永不抛出：出错就退回原行为（ok=True），显示层绝不反噬工具本身。
def biz_failed(result) -> bool:
    """包装 agent 层的**唯一实现**（顶部已 import 到模块作用域）。

    原来的两条戒律都保留，只是判据本体不再复制一份：
      ① 名字在模块作用域可见，绝不在调用点 lazy import（上次事故就是"引用了未进
         模块作用域的名字"，把全部工具的返回值换成了 NameError）；
      ② 判定永不抛出：出错就退回 ok=True，显示层绝不反噬工具本身。
    """
    try:
        return bool(is_business_failure(result))
    except Exception:
        return False

# ============================================================
# 3.5 思考过程 -> reasoning 事件（2026-09-23，主人要求：像 Hermes 那样在卡片里可展开）
# ============================================================
# 历史那部分是网关从会话文件读的（sessions.py:get_history 透出 reasoning 字段）；
# **实时这部分在这里补**：agent 每一轮都是「模型返回 -> append + save_session -> 才执行工具」
# （agent/agent.py:909-911），所以工具事件到达时，本轮的 reasoning_content 已经落盘 ——
# 直接读盘就够，**一行 agent 代码都不用改**，也不会把几 KB 思考灌进 stdout
# （那条路每行截 2000 字，还会污染 bridge 日志）。
_REASONING_SEEN: Dict[str, str] = {}          # sid -> 已发出的那一条（同一轮不重发）
_REASONING_CACHE: Dict[str, Dict[str, Any]] = {}   # sid -> {"sig": (mtime_ns,size), "text": ...}


def _read_latest_reasoning(session_id: str) -> str:
    """会话文件里最后一条带 reasoning_content 的 assistant 消息。读不到/出错一律返回空串。

    带 (mtime,size) 指纹缓存：同一批工具会并行触发多次，只有指纹变了才真读盘
    （模型下一轮返回后文件必然变化，指纹自然失效）。
    """
    try:
        if _agent is None or not session_id:
            return ""
        base = str(getattr(_agent, "WORKING_MEMORY_DIR", "") or "")
        if not base:
            return ""
        path = os.path.join(base, f"{session_id}.json")
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
        hit = _REASONING_CACHE.get(session_id)
        if hit and hit.get("sig") == sig:
            return str(hit.get("text") or "")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        text = ""
        for m in reversed(data.get("messages") or []):
            if isinstance(m, dict) and m.get("role") == "assistant":
                rc = str(m.get("reasoning_content") or "").strip()
                if rc:
                    text = rc
                    break
        _REASONING_CACHE[session_id] = {"sig": sig, "text": text}
        return text
    except Exception:
        return ""


def _emit_reasoning(session_id: Optional[str]) -> None:
    """把本轮思考发成 `reasoning` 事件（同一条不重发）。

    ⚠️ 本函数跑在工具 wrapper 的必经路径上（_wrap_tool.inner）：整体 try/except 兜住 ——
    这里抛异常会**替换掉工具的正常返回值**（咽喉层铁律，2026-09-09 用全线工具瘫痪换来的）。
    """
    try:
        sid = str(session_id or "")
        if not sid:
            return
        text = _read_latest_reasoning(sid)
        if not text or _REASONING_SEEN.get(sid) == text:
            return
        _REASONING_SEEN[sid] = text
        ctx = current_run()
        if ctx is not None:                      # 新的一轮开始：本轮的中期输出归到这个轮号
            meta = _TURN_META.setdefault(ctx.run_id, {"session_id": sid, "round": -1})
            # ⚠️ 别写 `int(meta.get("round") or -1)`：round 到 0 之后 `0 or -1` == -1（0 是 falsy），
            # 于是轮号永远停在 0 —— 所有中期输出都堆进第一轮（2026-09-23 实际发生过）。
            try:
                cur = int(meta.get("round", -1))
            except (TypeError, ValueError):
                cur = -1
            meta["round"] = cur + 1
        BUS.emit("reasoning", {
            "run_id": ctx.run_id if ctx else None,
            "session_id": sid,
            "text": text,
            "round": _current_round(ctx.run_id if ctx else None),
        })
        # 思考原文也要进进行中快照：它是"这一轮想完了"的锚点，而且刷新后
        # 只靠会话文件里的 reasoning_content 会随上下文压缩被清空（网关实测已见到空轮）。
        if ctx is not None:
            _save_partial_turn_snapshot(ctx)
    except Exception:
        pass


# ============================================================
# 3.4b 流式增量 → 界面事件（2026-10-02）
# ============================================================
# 以前 agent 是 stream=False：模型思考的**整段时间**里进程一个字都吐不出来，
# 界面只能干等（实测单次生成最长 550s，主人只能掐掉重来）。现在 agent 边收边把
# 增量回调过来，这里把它转成 `delta` 事件推到界面。
#
# ⚠️ 两条纪律：
#   1. **绝不阻塞生成**：这是模型输出的热路径，任何异常都吞掉、绝不反噬回合
#      （同 _emit_reasoning 的咽喉层铁律）。
#   2. **合并后再发**：逐 token 发会把 SSE 和浏览器都淹掉（EventBus 是 maxsize=2000
#      的丢帧队列）。按时间片攒够再发，兼顾"看得见"与"不刷屏"。
DELTA_FLUSH_INTERVAL = 0.12          # 攒够这么久就发一次（秒）
DELTA_MAX_CHARS = 400                # 或者攒够这么多字符就先发（快模型下不让延迟变大）

_DELTA_BUF: Dict[str, Dict[str, Any]] = {}   # key(sid|kind) -> {"text":..., "round": n, "at": t}
_DELTA_LOCK = threading.Lock()


def _flush_deltas(only: Optional[str] = None) -> None:
    """把攒着的增量发出去（only=None 发全部；否则只发某个 key，用于回合收尾）。"""
    try:
        with _DELTA_LOCK:
            keys = [only] if only else list(_DELTA_BUF.keys())
            batch = []
            for k in keys:
                item = _DELTA_BUF.pop(k, None)
                if item and item.get("text"):
                    batch.append(item)
        for item in batch:
            BUS.emit("delta", {
                "run_id": item.get("run_id"),
                "session_id": item.get("session_id"),
                "round": item.get("round"),
                "kind": item.get("kind"),
                "text": item["text"],
            })
    except Exception:
        pass


def _on_agent_delta(evt: Dict[str, Any]) -> None:
    """agent 的 delta 钩子：把增量攒起来，到点就发（绝不抛）。"""
    try:
        text = str((evt or {}).get("text") or "")
        if not text:
            return
        kind = str((evt or {}).get("kind") or "content")
        sid = str((evt or {}).get("session_id") or "")
        ctx = current_run()
        key = f"{sid}|{kind}"
        now = time.time()
        with _DELTA_LOCK:
            item = _DELTA_BUF.get(key)
            if item is None:
                item = {"text": "", "at": now, "round": _current_round(ctx.run_id if ctx else None),
                        "run_id": ctx.run_id if ctx else None, "session_id": sid, "kind": kind}
                _DELTA_BUF[key] = item
            item["text"] += text
            # 轮号以"此刻"为准（新一轮开始时旧缓存也该跟着走）
            if ctx is not None:
                item["round"] = _current_round(ctx.run_id)
            due = (now - float(item.get("at") or now) >= DELTA_FLUSH_INTERVAL
                   or len(item["text"]) >= DELTA_MAX_CHARS)
        if due:
            _flush_deltas(key)
    except Exception:
        pass



# ============================================================
# 3.6 中期过程落盘（WebUI 专用 sidecar）—— 2026-09-23
# ============================================================
# 为什么要它：会话文件里只有**模型产物**（content / reasoning_content / tool_calls），
# 而「中期输出」是 agent 进程的 **stdout 流**（StdoutCapture 接住的那些行）。
# 不落盘的话刷新后这一段凭空消失 —— 界面在"刷新前 / 刷新后"长得不一样，
# 而主人 2026-09-23 明确裁决要**逐字一致**。
#
# 落盘位置独立：`agent_memory/.webui_turns/<sid>.json`
# （**绝不写进会话 JSON** —— 那是模型上下文，塞非标准消息会污染它）。
# 网关只读它来还原界面；界面的思考/工具/正文仍以会话文件为权威。
_TURN_STEPS: Dict[str, List[Dict[str, Any]]] = {}   # run_id -> [{"round": n, "text": line}]
_TURN_META: Dict[str, Dict[str, Any]] = {}          # run_id -> {"session_id": ..., "round": n}
_OUT_OPEN: Dict[str, int] = {}                      # run_id -> 当前进度段在 _TURN_STEPS 里的下标
TURN_SIDECAR_KEEP = 400                             # 只留最近 N 个回合，sidecar 不该无限长

# ---- 3.6b 进行中快照（2026-10-02）--------------------------------------------
# 为什么要有它：sidecar 原本**只在回合收尾时**写盘（见 _save_turn_sidecar 的调用点），
# 于是「本回合已经产出的中期输出」在磁盘上不存在 —— 刷新 / 切会话 = 当场丢失。
# 而 _TURN_STEPS 是进程内存态，做不到"刷新后还在"。
# 修法：本回合每产出一段就**节流写盘**（同一个 run_id 原地覆盖），刷新后由
# sessions._turn_sidecar 透出，前端据此在没有实时事件时也能把本回合画回来。
#
# 与"回合收尾写盘"的分工：收尾那次带 ended 并进 turns 列表；进行中这次带
# partial=true 单独放 "partial" 键，二者互不覆盖（收尾时把 partial 摘掉）。
_TURN_SIG: Dict[str, int] = {}                      # run_id -> 本回合产出信号数（去抖用）
_TURN_LAST_WRITE: Dict[str, float] = {}             # run_id -> 上次写盘时刻（节流用）
TURN_SNAPSHOT_INTERVAL = 0.4                        # 进行中快照最小写盘间隔（秒）
# 上一进程留下的"进行中快照"：本进程收尾时必须能把它摘掉，而它不在
# _TURN_STEPS 里（那是上个进程的内存态，进程一没就没了）。
_PARTIAL_TURN_SNAPSHOT: Dict[str, Dict[str, Any]] = {}


def _sidecar_dir() -> str:
    base = str(getattr(_agent, "PROJECT_ROOT", "") or "") if _agent is not None else ""
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "agent_memory", ".webui_turns")


def _session_messages(session_id: str) -> List[Dict[str, Any]]:
    """会话文件的 messages（只读，读不到返回空列表）。"""
    try:
        base = str(getattr(_agent, "WORKING_MEMORY_DIR", "") or "") if _agent is not None else ""
        if not base or not session_id:
            return []
        with open(os.path.join(base, f"{session_id}.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("messages") or []
    except Exception:
        return []


def _user_seq(session_id: str) -> int:
    """本回合是会话里第几次**真实用户输入**（0 起）。两类"不是主人说的话"**不算**一次：

      · 用户交代（中期交互）—— 主人趁活干到一半追加的话，挂在同一个回合里；
      · 后台作业的**自动交付**—— 它是 user 角色（必须持久化，见 agent._deliver_finished_jobs
        的注释），但它不是主人说的。漏掉这一条，每交付一次界面的轮次就整体错位一格。

    前缀常量只有一处定义（agent 侧 task_orchestrator），本文件 import 过来用 ——
    两边各写一份必然漂移。
    """
    try:
        n = 0
        for m in _session_messages(session_id):
            if m.get("role") != "user":
                continue
            text = str(m.get("content") or "")
            if (text.startswith(mid_turn.PREFIX) or text.startswith(JOB_DELIVERY_PREFIX)
                    or text.startswith(PM_NOTICE_PREFIX)):
                continue
            n += 1
        return max(0, n - 1)
    except Exception:
        return 0


MID_PROGRESS_TAG = "[中期进度]: "     # agent.py 打印中期进度用的前缀


def _current_round(run_id: Optional[str]) -> int:
    """本回合当前轮号（0 起；回合刚开始、模型还没返回时是 -1）。
    前端拿它把事件归到正确的轮 —— 光靠到达顺序不行：agent 的「中期进度」print
    发生在工具执行**之前**，而 reasoning 事件是工具开始执行时才发的。"""
    try:
        return int((_TURN_META.get(run_id or "") or {}).get("round", -1))
    except (TypeError, ValueError):
        return -1


def _collect_out(run_id: Optional[str], line: str) -> None:
    """把 agent 的「中期进度」**整段**记进本回合的中期输出（StdoutCapture 每行都会调；绝不抛）。

    三条口径：
      1. 段落的开头 = `[中期进度]:` 那一行；**它的续行也要收** ——
         agent 打的是多行 Markdown（`print(f"[中期进度]: {text}")`），漏掉续行等于话说一半。
      2. 段落在**工具开始执行时闭合**（见 _close_out）：那之后的行要么是工具自己的 print，
         要么是 agent 启动信息（📜 快照 / 📂 加载会话）—— 都是噪音，界面不该显示
         （主人 2026-09-23 拿两轮实拍对比定的口径）。
      3. **归属轮 = 当前轮 + 1**：这行 print 发生在**模型返回之后、该轮工具执行之前**，
         而 round 是工具开始执行时才推进的 —— 直接取当前值会整段错到上一轮。
    """
    try:
        if not run_id or _TURN_META.get(run_id) is None:
            return
        if line.startswith(MID_PROGRESS_TAG):
            segs = _TURN_STEPS.setdefault(run_id, [])
            _OUT_OPEN[run_id] = len(segs)
            segs.append({"round": _current_round(run_id) + 1, "text": line[:4000]})
            _snapshot_ctx = current_run()
            if _snapshot_ctx is not None:
                _save_partial_turn_snapshot(_snapshot_ctx)   # 本回合每产出一段就（节流）落盘
            return
        idx = _OUT_OPEN.get(run_id)
        if idx is None:
            return                                  # 不在任何进度段里 -> 噪音，丢
        segs = _TURN_STEPS.get(run_id) or []
        if 0 <= idx < len(segs) and line.strip():
            segs[idx]["text"] = (segs[idx]["text"] + "\n" + line)[:4000]
            _snapshot_ctx = current_run()
            if _snapshot_ctx is not None:
                _save_partial_turn_snapshot(_snapshot_ctx)   # 续行也要落盘，否则刷新只看到半句
    except Exception:
        pass


def _close_out(run_id: Optional[str]) -> None:
    """闭合当前「中期进度」段落。工具一开始执行，这一轮的进度 print 就结束了 ——
    之后的 stdout 全是噪音（工具自己的 print / 启动信息），不能再往段里拼。"""
    try:
        if run_id:
            _OUT_OPEN.pop(run_id, None)
    except Exception:
        pass


def _write_sidecar_file(sid: str, mutate) -> None:
    """读—改—原子写 sidecar（界面附属品，任何异常都吞掉，绝不能影响回合）。

    mutate(data) 就地改动；写盘一律 tmp + os.replace（原子），避免前端读到半个 JSON。
    """
    try:
        d = _sidecar_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sid}.json")
        data: Dict[str, Any] = {"session_id": sid, "turns": []}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or data
            except Exception:
                pass                       # 坏了就重来，别让界面附属品拖垮回合
        mutate(data)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _turn_reasoning_by_round(ctx) -> Dict[int, str]:
    """本回合**最近一次发出的**思考原文，挂到它所属的轮号上（round -> reasoning）。

    轮号口径与 `reasoning` 事件完全一致（同一个 `_current_round`）—— 两边不一致，
    刷新前后「思考过程」就会跑到别的轮里去。
    注：每轮只挂"那一条"（bridge 一直是"每轮发一次思考"的口径），不是累积全量。
    """
    out: Dict[int, str] = {}
    try:
        seen_round = _current_round(ctx.run_id)
        text = _REASONING_SEEN.get(ctx.session_id) or ""
        if text and seen_round >= 0:
            out[seen_round] = text
    except Exception:
        pass
    return out


def _save_partial_turn_snapshot(ctx, *, force: bool = False) -> None:
    """把**进行中**回合的已产出内容落盘（节流）。界面附属品：任何异常都吞掉。

    - 节流：距上次写盘 < TURN_SNAPSHOT_INTERVAL 且非 force → 直接返回；
    - 信号去抖：本回合产出数没变且非 force → 直接返回（省掉无谓的读—改—写）；
    - **空的不写**：宁可留着上一次的非空快照，也不要用"空"覆盖掉它
      （bridge 重启后 _TURN_STEPS 是空的，此时一写就把上一进程的快照清没了）。
    """
    try:
        steps = _TURN_STEPS.get(ctx.run_id) or []
        if not steps:
            return
        # 信号 = "段落数 + 全部文本长度"：只数段数的话，多行 Markdown 的**续行**
        # 追加进来时信号不变 → 快照不刷新 → 刷新页面只看到半句（续行恰恰是最常见的）。
        sig = len(steps) + sum(len(str(o.get("text") or "")) for o in steps)
        if not force:
            if _TURN_SIG.get(ctx.run_id) == sig:
                return
            if time.time() - float(_TURN_LAST_WRITE.get(ctx.run_id) or 0.0) < TURN_SNAPSHOT_INTERVAL:
                return
        _TURN_SIG[ctx.run_id] = sig
        _TURN_LAST_WRITE[ctx.run_id] = time.time()
        outs = [{"round": int(o.get("round") or 0), "text": str(o.get("text") or "")}
                for o in steps]
        reasoning = _turn_reasoning_by_round(ctx)
        sid = ctx.session_id

        def _mutate(data: Dict[str, Any]) -> None:
            data["partial"] = {
                "user_seq": _user_seq(sid),
                "run_id": ctx.run_id,
                "ended": "live",
                "partial": True,
                "at": round(time.time(), 3),
                "outs": outs,
                "reasoning": reasoning,
            }

        _write_sidecar_file(sid, _mutate)
    except Exception:
        pass


def _clear_partial_snapshot(sid: str) -> None:
    """摘掉进行中快照（回合收尾时调用）：它已经由 turns 里的正式条目接管。"""
    try:
        def _mutate(data: Dict[str, Any]) -> None:
            data.pop("partial", None)
        _write_sidecar_file(sid, _mutate)
        _PARTIAL_TURN_SNAPSHOT.pop(sid, None)
    except Exception:
        pass


def _save_turn_sidecar(ctx) -> None:
    """回合结束：把本回合的中期输出原子写盘。界面附属品，任何异常都吞掉，绝不能影响回合。"""
    steps = _TURN_STEPS.get(ctx.run_id) or []
    if not steps:
        return
    sid = ctx.session_id
    res = ctx.result if isinstance(getattr(ctx, "result", None), dict) else {}
    ended = ("error" if ("error" in res and not res.get("interrupted"))
             else "interrupted" if res.get("interrupted") else "done")
    entry = {"run_id": ctx.run_id, "user_seq": _user_seq(sid), "ended": ended,
             "at": round(time.time(), 3), "outs": steps}
    reasoning = _turn_reasoning_by_round(ctx)
    if reasoning:
        entry["reasoning"] = reasoning

    def _mutate(data: Dict[str, Any]) -> None:
        turns = [t for t in (data.get("turns") or []) if t.get("run_id") != ctx.run_id]
        turns.append(entry)
        turns.sort(key=lambda t: t.get("at") or 0)
        data["turns"] = turns[-TURN_SIDECAR_KEEP:]
        data.pop("partial", None)          # 进行中快照由这条正式条目接管

    _write_sidecar_file(sid, _mutate)
    _PARTIAL_TURN_SNAPSHOT.pop(sid, None)


def _wrap_tool(name: str, fn):
    """包装工具函数：调用前后发 tool_begin / tool_end 事件。

    用 functools.wraps 保留 __wrapped__，编排器的 inspect.signature()
    会跟随到原函数 -> logger 自动注入逻辑完全不受影响。
    异常按原对象重抛（保留类型与 traceback），事件里再记录摘要。
    """
    @functools.wraps(fn)
    def inner(**kwargs):
        ctx = current_run()
        # 优先用编排器的真实 tool_call.id（由被包的 ToolPipeline.run 放进 thread-local），
        # 这样 pipeline 事件与 tool_begin/tool_end 指向同一次调用，能拼到同一条管道上。
        call_id = getattr(_TLS, "tc_id", None) or uuid.uuid4().hex[:8]
        idx = 0
        thread_name = threading.current_thread().name
        started = time.time()
        if ctx is not None:
            # 先闭合这一轮的中期进度段（工具一跑，进度 print 就结束了），再发思考与工具事件：
            # 前端按"轮"分块，先思考后工具才对得齐（磁盘上已有本轮 reasoning，见 _emit_reasoning）。
            _close_out(ctx.run_id)
            _emit_reasoning(ctx.session_id)
            with ctx.lock:
                ctx.tool_count += 1
                ctx.active_tools += 1
                idx = ctx.tool_count
            BUS.emit("tool_begin", {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "call_id": call_id, "tool": name, "index": idx,
                "round": _current_round(ctx.run_id), "thread": thread_name,
                "args": brief({k: v for k, v in kwargs.items() if k != "logger"}, 900),
            })
            if not getattr(ctx, "finished", False):
                _set_turn(ctx, "TOOL_RUNNING")
        result, exc = None, None
        try:
            result = fn(**kwargs)
        except BaseException as e:  # noqa: BLE001 - 记录后原样重抛
            exc = e
        finally:
            elapsed = time.time() - started
            if ctx is not None:
                with ctx.lock:
                    ctx.active_tools = max(0, ctx.active_tools - 1)
                    still = ctx.active_tools
                BUS.emit("tool_end", {
                    "run_id": ctx.run_id, "session_id": ctx.session_id,
                    "call_id": call_id, "tool": name, "index": idx,
                    "thread": thread_name,
                    "ok": (exc is None) and not biz_failed(result),   # 业务失败也算失败，与落盘还原同口径
                    "biz_fail": (exc is None) and biz_failed(result),
                    "result": None if exc else brief(result, 900),
                    "error": None if exc is None else f"{type(exc).__name__}: {exc}",
                    # 工具耗时：原来只算了却没发出去，而前端 TimelinePanel 一直在渲染
                    # elapsed.toFixed(2)s（appStore 也照读 evt.elapsed）—— 结果时间线上
                    # 永远不显示耗时。又一个"跨层手工对字段"漏掉的一处（审计 W9 同族）。
                    "elapsed": round(elapsed, 2),
                })
                # 回合已经收口的（后台作业在 worker 线程里跑完的那种）**绝不再改回合状态**：
                # 这一条就是"挂完 30 秒作业后界面自己变回回合进行中"的断源点 ——
                # 旧写法在这里把网关状态机从"空闲"又推回了"忙"。
                if still == 0 and not getattr(ctx, "finished", False):
                    _set_turn(ctx, "THINKING")
        if exc is not None:
            raise exc
        return result

    return inner


def _wrap_pending_tools() -> int:
    """**启动期**全量包装（幂等）：把 AVAILABLE_TOOLS 里还没包装的工具补上，返回补了几个。

    界面事件通道（tool_begin / tool_end）靠它 —— 工具不包这一次，界面上就是隐形的。
    只在 `_install_runtime()` 里调一次（启动期）：v2 起 MCP 工具不再进常驻工具表、
    也没有"运行期热注册"这回事，所以不再需要每回合兜底（那套兜底是给已删的热注册钩子用的）。
    """
    import agent_tools                            # noqa: PLC0415
    n = 0
    for tool_name in list(agent_tools.AVAILABLE_TOOLS.keys()):
        raw = agent_tools.AVAILABLE_TOOLS[tool_name]
        if getattr(raw, "_ab_webui_wrapped", False):
            continue
        try:
            wrapped = _wrap_tool(tool_name, raw)
            wrapped._ab_webui_wrapped = True
            agent_tools.AVAILABLE_TOOLS[tool_name] = wrapped
            n += 1
        except Exception as e:
            print("[bridge] 包装工具失败 %s: %s: %s" % (tool_name, type(e).__name__, e),
                  file=sys.__stderr__, flush=True)
    return n


def _install_runtime() -> None:
    """import agent 并注入 ask_user + 事件包装。返回注入摘要。"""
    global _agent, _ToolCall, _injected
    import agent as ab_agent                      # noqa: PLC0415  AB 本体
    from task_orchestrator import ToolCall        # noqa: PLC0415
    import agent_tools                            # noqa: PLC0415
    import ask_user_tool                          # noqa: PLC0415

    _agent = ab_agent
    _ToolCall = ToolCall
    ask_user_tool.set_channel(_ask_channel)

    # 7.05 token 计量（patch 模块级 client，不动 agent 源码）
    USAGE_WATCH_STATE = _install_usage_watch(ab_agent)
    if USAGE_WATCH_STATE == "on":
        _injected.append("usage_watch")

    # 计量账本：重启后接着上次累计（概况/占比常驻，不必先发消息）
    LEDGER_STATE = _load_ledger()
    if LEDGER_STATE.startswith("loaded"):
        _injected.append("usage_ledger")
    print("[bridge] 计量账本: " + LEDGER_STATE, file=sys.__stderr__, flush=True)

    # 7.1 事件包装（**启动期一次性全量**）：替换 dict 的 value（同一 dict 对象 -> agent 模块可见），
    #     让每次工具执行都发得出 tool_begin / tool_end（界面事件通道）。
    _wrap_pending_tools()

    # 7.2 注册 ask_user（dict 追加 + list 追加，均不改源文件）
    if "ask_user" not in agent_tools.AVAILABLE_TOOLS:
        wrapped_ask = _wrap_tool("ask_user", ask_user_tool.ask_user)
        wrapped_ask._ab_webui_wrapped = True
        agent_tools.AVAILABLE_TOOLS["ask_user"] = wrapped_ask
        agent_tools.TOOLS_SCHEMA.append(ask_user_tool.ask_user_schema)
        # ask_user 的编排器时限 —— per-tool 超时声明的**正规通道**，取代原来的
        # _install_ask_window monkey-patch。它是在 worker 线程里**等主人回答**，
        # 绝不能被编排器抢先结算（抢先后主人晚到的答复投给已死的等待者，静默吞掉）。
        # 声明按「最坏卡数」兜住：同层最多 MAX_PARALLEL_CARDS 张卡共享一条窗口
        # （ask_batch），waiter 自己会关窗返回，这里的值只是兜底、正常用不到。
        agent_tools.TOOL_TIMEOUTS["ask_user"] = ask_batch.batch_timeout(
            MAX_PARALLEL_CARDS, ASK_TIMEOUT)
        # 重复提问 = 重复打扰主人（还可能是"上一个提问已被判超时、答复仍在路上"），
        # 按有副作用处理：超时后原样重发会被 agent 层拒绝。见 agent_tools.NON_IDEMPOTENT_TOOLS。
        agent_tools.NON_IDEMPOTENT_TOOLS.add("ask_user")
        _injected.append("ask_user")

    # 7.4 审批系统：把 WebUI 通道注册进 agent/approval.py（宿主注入，本体零改动）
    # 判定规则全在 approval.py，这里只负责送达与等待，所以 CLI 与 WebUI 同源。
    try:
        import approval as ab_approval                    # noqa: PLC0415
        import approval_adapter                           # noqa: PLC0415
        approval_adapter.set_emit(BUS.emit)
        ab_approval.set_port(approval_adapter.WebUIPort())
        APPROVAL_STATE["value"] = ab_approval.self_check(_tool_names())
        _injected.append("approval_channel")
    except Exception as e:
        APPROVAL_STATE["value"] = {"ok": False,
                                   "error": "%s: %s" % (e.__class__.__name__, e)}
        print("[bridge] approval channel init failed; system-drive ops denied: %s"
              % APPROVAL_STATE["value"].get("error"),
              file=sys.__stderr__, flush=True)

    # 7.6 中期交互（WebUI 专属；实现全在本目录 mid_turn.py，agent 本体不认识本功能）：
    #   ① 把 flush_after_tools 注册进 agent 的**通用** after-tools 钩子 —— 注入时机
    #      ＝每批工具结果写回 conversation 之后；CLI 下没有注册者，钩子表为空、零开销。
    #   ② 信箱的阶段回调 → SSE 事件。通道与呈现全归 WebUI，而不是让 mid_turn 去
    #      import 任何 WebUI 模块（CLI 下没有观察者，一切照常）。
    try:
        ab_agent.register_after_tools_hook(mid_turn.flush_after_tools)
        mid_turn.BOX.set_observer(lambda _stage, payload: BUS.emit("mid_turn", payload))
        _injected.append("mid_turn")
    except Exception as e:
        print("[bridge] mid-turn channel init failed: %s: %s"
              % (e.__class__.__name__, e), file=sys.__stderr__, flush=True)

    # 7.6b 流式增量通道（2026-10-02）：agent 边收流边回调，这里转成 `delta` 事件。
    #  与上面同构 —— 宿主注入实现，agent 本体只提供挂载点（CLI 下无注册者、零开销）。
    #  注入失败不影响功能，只影响"边想边看"：所以只打印，不抛。
    try:
        ab_agent.register_delta_hook(_on_agent_delta)
        _injected.append("delta_stream")
    except Exception as e:
        print("[bridge] delta channel init failed: %s: %s"
              % (e.__class__.__name__, e), file=sys.__stderr__, flush=True)

    # 7.3 【主线程】预建常驻编排器。
    # 实查发现的硬约束：TaskOrchestrator.__init__ -> _register_signal_handlers()
    # 调用 signal.signal()，只能在主线程执行。若留给回合线程首次创建，
    # 必然抛 ValueError: signal only works in main thread of the main interpreter。
    # 这里在主线程建好单例，回合线程直接复用（agent._ORCHESTRATOR 非 None）。
    try:
        orch = ab_agent._get_orchestrator()
        # ask_user 的等待时限由 per-tool 超时声明给出（见 7.2 注入处），
        # 不再需要在这里 monkey-patch 编排器的 _run_on_pool。
        _orch_state = "ready+wait%ds" % agent_tools.TOOL_TIMEOUTS.get(ASK_TOOL, ASK_TIMEOUT)
        # 状态机上报：成功几项就写几项，界面据此区分「真状态」与「推导模式」
        try:
            st = install_orchestrator_watch(orch)
            on = [k for k, v in st.items() if v]
            if on:
                _orch_state += "+watch(" + ",".join(sorted(on)) + ")"
        except Exception as e:
            print(f"[bridge] 状态机监视安装失败（界面将回落推导模式）: {type(e).__name__}: {e}",
                  file=sys.__stderr__, flush=True)
    except ValueError as e:
        _orch_state = f"failed: {e}"
        print(f"[bridge] 编排器主线程预建失败: {e}", file=sys.__stderr__, flush=True)
    ORCH_STATE["value"] = _orch_state

    return {
        "orchestrator": _orch_state,
        "pools": _pool_capacity(getattr(ab_agent, "_ORCHESTRATOR", None)),
        "tools": sorted(agent_tools.AVAILABLE_TOOLS.keys()),
        "injected": list(_injected),
        "model": getattr(ab_agent, "MODEL_NAME", "?"),
        "max_iterations": getattr(ab_agent, "MAX_ITERATIONS", "?"),
        "project_root": str(ab_agent.PROJECT_ROOT),
    }


# ============================================================
# 8. 回合执行
# ============================================================
# ---- 权限模式的落盘（2026-10 改版）------------------------------------------
# 口径（主人实测后两次收口，以他最后一次的原话为准）：
#   · **不往会话历史里追加切换通知**（切几次就堆几条、白占上下文）；
#   · **也不是每轮请求都带一行**"当前状态"（那会让每次发送消息都多一行，烧 token）;
#   · 只有**真的换了档**（切到不同的档）那一次，由 `agent._with_mode_notice` 在
#     **下一次请求**里以 **user 通道**追加一行 `旧权限->新权限：一句话`，取走即清。
# 界面那边是一块就地更新的状态块（实时态，不是消息）。这里只负责把**当前档**写进
# 会话文件（durability）。
def _persist_mode(session_id: str) -> bool:
    """把当前模式写进会话文件 —— 关机重开也不丢。

    ⚠️ **只读原始 JSON，绝不调 `_agent.load_session`。** 踩过一次真坑（2026-10 实测）：
    load_session 会把文件里的 permission_mode 读回引擎（那是"重启回档"要的），
    而本函数是在 `PM.set(新模式)` **之后**调用的 —— 于是它把刚设好的新档又盖回
    文件里的旧档，症状是"切档卡在曾经用过的某一档"：界面显示新档、模型上下文里也是
    新档，只有引擎与文件是旧档。读文件就该只是读文件。

    另两条规矩：
      1. **必须把 system 快照一起交给 save_session**：它只从 messages 里找 system
         消息，找不到就把 `system_prompt` 键整片丢掉 —— 那等于删掉冻结的语境快照；
      2. **只在没有并发回合（或并发回合属于别的会话）时调用** —— save_session 是全量
         覆盖写，与正在跑的那一回合互相覆盖会丢消息。并发时本函数不写，等回合自己的
         保存带上这一档（save_session 会读 PM.get），收口时再补一次兜底。
    """
    if _agent is None:
        return False
    try:
        from logger import SessionLogger                    # noqa: PLC0415
        path = os.path.join(str(_agent.WORKING_MEMORY_DIR), "%s.json" % session_id)
        history: List[Dict[str, Any]] = []
        snapshot = None
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            snapshot = raw.get("system_prompt")
            history = [m for m in (raw.get("messages") or []) if m.get("role") != "system"]
        msgs: List[Dict[str, Any]] = ([{"role": "system", "content": snapshot}]
                                      if snapshot else []) + history
        _agent.save_session(session_id, msgs, SessionLogger(session_id))
        return True
    except Exception as e:
        print("[bridge] 权限模式落盘失败: %s: %s" % (type(e).__name__, e),
              file=sys.__stderr__, flush=True)
        return False


def _ensure_mode_persisted(session_id: str) -> None:
    """回合收口时兜底：文件里的档与内存不一致才补写（代价：一次读 + 可能的写）。

    为什么需要它：并发回合期间切的档**故意不写文件**（防覆盖），指望回合自己的保存
    带上 —— 但回合若被强杀在中途，那一次切换就可能没落盘，重开就回到了旧档。
    """
    if _agent is None or not session_id:
        return
    try:
        import permission_modes as _pm                       # noqa: PLC0415
        path = os.path.join(str(_agent.WORKING_MEMORY_DIR), "%s.json" % session_id)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if str(raw.get("permission_mode") or "") != _pm.get(session_id):
            _persist_mode(session_id)
    except Exception:
        pass


def _build_messages(session_id: str, user_message: str, log):
    """复刻 agent.main() 的语境快照冻结逻辑（不改 agent 源码）。"""
    history, snapshot = _agent.load_session(session_id, log)
    if snapshot is not None:
        system_prompt = snapshot
        frozen = "reuse"
    else:
        system_prompt, _inj = _agent.load_system_prompt(log)
        frozen = "fresh"
    msgs = [{"role": "system", "content": system_prompt}] + list(history)
    return msgs, len(history), frozen


def _attachments_of(body: Dict[str, Any]) -> List[str]:
    """从 bridge 请求体里取附件路径列表（去空、限数）。

    合法性（是否落在附件目录内、文件是否存在）由**网关侧** api._clean_attachments
    校验 —— bridge 是与网关同机的内部接口，不重复做磁盘 IO，但也不据此放宽。
    """
    raw = body.get("attachments")
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for x in raw[:50]:
        s = str(x or "").strip()
        if s:
            out.append(s)
    return out


def _compose_user_content(message: str, attachments) -> str:
    """把正文与附件标记拼成一条 user 消息的**字符串**内容。

    为什么刻意保持字符串（而不是在这里就拼成 content 数组）：
      ContextManager、会话文件、前端渲染、mid_turn 注入、上下文视图 —— 全部按
      「content 是字符串」设计。附件真正的物化（读字节 → 图片块 / 文本块）只在
      agent 咽喉 `compose_chat_kwargs` 一处发生。改造面最小，并且保证
      **不含附件的消息与旧版逐字节一致**（纯文本模型接入零影响）。
    """
    lines: List[str] = []
    msg = str(message or "")
    if msg:
        lines.append(msg)
    for p in (attachments or []):
        s = str(p or "").strip()
        if s:
            lines.append(mid_turn.att_mark(s))     # 与中期交互共用同一种标记
    return "\n".join(lines)


def _execute_turn(ctx: RunContext) -> None:
    ctx.thread_id = threading.get_ident()
    _set_current(ctx)
    _LAYER_SEQ[0] = 0                      # 层号按回合计，避免越跑越大
    _TURN_META[ctx.run_id] = {"session_id": ctx.session_id, "round": -1}
    _TURN_STEPS[ctx.run_id] = []           # 中期过程收集（见 3.6 节）
    _TURN_SIG.pop(ctx.run_id, None)        # 进行中快照的节流状态按回合重置
    _TURN_LAST_WRITE.pop(ctx.run_id, None)
    _kept_pipes = _reset_pipes_for_turn()   # 跨回合作业占的槽位不清（见该函数）
    if _kept_pipes:
        print("[bridge] 本回合起点保留 %d 个跨回合作业占用的槽位" % _kept_pipes,
              file=sys.__stderr__, flush=True)
    log = None
    try:
        from logger import SessionLogger  # noqa: PLC0415
        log = SessionLogger(ctx.session_id)
        log.info("[WebUI] 回合开始", run_id=ctx.run_id, mode=MODE or "default")

        messages, history_count, frozen = _build_messages(ctx.session_id, ctx.message, log)
        BUS.emit("stage", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                           "stage": "context_ready", "history_count": history_count,
                           "snapshot": frozen, "model": getattr(_agent, "MODEL_NAME", "?")})
        messages.append({"role": "user", "content": _compose_user_content(ctx.message, ctx.attachments)})
        _set_turn(ctx, "THINKING")

        result = _agent.call_agent_with_tools(messages, ctx.session_id, log)
        ctx.result = result

        interrupted = bool(result.get("interrupted"))
        if "error" in result and not interrupted:
            _set_turn(ctx, "ERROR")
            BUS.emit("error", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                               "message": str(result["error"])[:2000],
                               "iterations": result.get("iterations")})
        else:
            _set_turn(ctx, "RESPONDING")
            content = result.get("content") or ""
            if content:
                BUS.emit("text", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                                  "text": content})
            _emit_reasoning(ctx.session_id)   # 最后一轮的思考：这轮没有工具调用，只能在这儿补
            _set_turn(ctx, "INTERRUPTED" if interrupted else "IDLE")
            # 本回合开头自动交付了哪些后台作业 —— 从 agent 侧取**真实发生**的事件
            # （交付是落盘的消息，不是实时事件；界面收到这个才去以磁盘为准重放一次历史，
            #  否则那条【后台作业交付】要等切会话/刷新才看得见）。取不到就为空，不加噪音。
            try:
                delivered_jobs = list(getattr(_agent, "take_delivered_notices", lambda: [])())
            except Exception:
                delivered_jobs = []
            payload = {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "content": content,
                "iterations": result.get("iterations"),
                "tool_calls": ctx.tool_count,
                "elapsed": round(time.time() - ctx.started_at, 2),
                "interrupted": interrupted,
            }
            if delivered_jobs:
                payload["delivered_jobs"] = delivered_jobs
            BUS.emit("done", payload)
    except BaseException as e:  # noqa: BLE001 - 回合兜底，绝不带崩进程
        msg = f"{type(e).__name__}: {e}"
        try:
            import traceback
            print("回合异常:\n" + traceback.format_exc(), file=sys.__stderr__, flush=True)
        except Exception:
            pass
        _set_turn(ctx, "ERROR")
        BUS.emit("error", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                           "message": msg[:2000]})
    finally:
        # 中期交互：过了本回合还没送出去的交代一律作废，并明确告诉主人 ——
        # 留着它会在下个回合诈尸，让模型执行一个早已不成立的要求。
        try:
            left = mid_turn.BOX.discard(ctx.session_id)
            if left:
                BUS.emit("mid_turn", {
                    "mid": "dropped", "run_id": ctx.run_id, "session_id": ctx.session_id,
                    "ids": [it["id"] for it in left], "count": len(left),
                    "texts": [it["text"] for it in left], "reason": "turn-ended",
                })
        except Exception:
            pass
        # 回合收尾兜底：还挂着的提问卡立刻出局（正常情况下批次结束时它们已各自收尾）
        try:
            _cancel_all_ask("turn-ended")
        except Exception:
            pass
        try:
            _save_turn_sidecar(ctx)                  # 中期输出落盘（界面还原用）
        except Exception:
            pass
        try:
            _flush_deltas()                          # 收尾：把还没发出去的增量补发掉
        except Exception:
            pass
        _clear_partial_snapshot(ctx.session_id)      # 进行中快照已被上面的正式条目接管
        _TURN_STEPS.pop(ctx.run_id, None)
        _TURN_META.pop(ctx.run_id, None)
        _OUT_OPEN.pop(ctx.run_id, None)
        _TURN_SIG.pop(ctx.run_id, None)
        _TURN_LAST_WRITE.pop(ctx.run_id, None)
        _REASONING_SEEN.pop(ctx.session_id, None)   # 本回合的"已发过"记录只对本回合有效
        # 本回合期间切过的档：并发时我们**故意没写文件**（防与回合保存互相覆盖），
        # 指望回合自己的保存带上。回合若被强杀在中途就可能没落盘 —— 收口时补一次。
        try:
            _ensure_mode_persisted(ctx.session_id)
        except Exception:
            pass
        ctx.finished = True                         # 回合收口：此后任何收尾都不许再改回合状态
        _set_current(None)
        with _RUNS_LOCK:
            ctx.thread_id = None
        _TURN_GATE.release()


def _start_run(session_id: str, message: str, attachments=None) -> Dict[str, Any]:
    # done 事件先于门闩释放（finally 里才放），故给一个短宽限，
    # 避免"上一回合刚结束就立刻追发"被误判 busy。
    if not _TURN_GATE.acquire(blocking=True, timeout=3.0):
        return {"ok": False, "busy": True,
                "error": "当前回合仍在进行，请等待或先停止"}
    ctx = RunContext(uuid.uuid4().hex[:12], session_id, message, attachments)
    with _RUNS_LOCK:
        _RUNS[ctx.run_id] = ctx
    t = threading.Thread(target=_execute_turn, args=(ctx,),
                         name=f"ab-turn-{ctx.run_id}", daemon=True)
    t.start()
    return {"ok": True, "run_id": ctx.run_id, "session_id": session_id}


def _stop_run(run_id: Optional[str]) -> Dict[str, Any]:
    try:                                    # 停止时清掉挂起的审批等待者，别留孤儿
        import approval_adapter             # noqa: PLC0415
        approval_adapter.cancel_all("stopped")
    except Exception:
        pass
    try:                                    # 提问卡同理：立刻出局，别在屏幕上多挂几分钟
        n_clar = _cancel_all_ask("stopped")
        if n_clar:
            BUS.emit("ask_resolved", {"reason": "stopped", "count": n_clar})
    except Exception:
        pass
    ctx = None
    # 注意：active_run()/current_run() 内部同样取 _RUNS_LOCK，
    # 绝不可在持有该锁时调用它们（曾经的自死锁会让 /stop 与 /health 永久无响应）。
    with _RUNS_LOCK:
        if run_id:
            ctx = _RUNS.get(run_id)
            if ctx is None:  # 指定了 run_id 却查不到：绝不回落到别人的回合
                return {"ok": False, "stopped": False, "reason": "run-not-found",
                        "error": f"未找到回合 {run_id}"}
    if ctx is None:
        ctx = active_run()
    if ctx is None or ctx.thread_id is None:
        return {"ok": True, "stopped": False, "reason": "no-active-run"}
    ctx.interrupt_requested = True
    _set_turn(ctx, "INTERRUPTED")
    hit = _inject_keyboard_interrupt(ctx.thread_id)
    return {"ok": True, "stopped": hit, "run_id": ctx.run_id,
            "note": None if hit else "线程阻塞在原生调用中，中断将在其返回后的边界生效"}


def _mid_turn(session_id: str, text: str, run_id: Optional[str] = None,
              attachments=None) -> Dict[str, Any]:
    """回合运行中追加「用户交代」：只投给当前活动回合，绝不新起回合、绝不落盘成新发言。

    三道校验（任一不成立就如实拒绝，不做「猜主人想投给谁」的兜底）：
      1. 确实有活动回合 —— 没有的话请直接发消息，这条路不该接；
      2. 给了 run_id 就必须命中该回合 —— 防「打错目标，话被投给下一个回合」；
      3. 会话必须与活动回合一致 —— 防在旁观会话里发交代、话却进了别人的语境。

    投递成功后由信箱的观察者广播 mid_turn:accepted（前端据此把消息落座，
    并拿 item_id 跟踪它后来是「已注入」还是「回合结束被作废」）。
    """
    body = str(text or "").strip()
    atts = [str(x).strip() for x in (attachments or []) if str(x or "").strip()]
    if not body and not atts:
        return {"ok": False, "error": "交代内容不能为空"}
    if len(body) > mid_turn.MAX_TEXT:
        return {"ok": False, "error": "交代过长（%d 字符，上限 %d）" % (len(body), mid_turn.MAX_TEXT)}
    ctx = active_run()                     # 注意：绝不在持有 _RUNS_LOCK 时调用它
    if ctx is None or ctx.thread_id is None:
        return {"ok": False, "reason": "no-active-run",
                "error": "当前没有正在跑的回合，直接发消息即可"}
    if run_id and run_id != ctx.run_id:
        return {"ok": False, "reason": "run-not-found",
                "error": "那一回合已经结束了，请直接发消息"}
    sid = str(session_id or "").strip()
    if sid and sid != ctx.session_id:
        return {"ok": False, "reason": "session-mismatch",
                "error": "AB 正在跑会话 %s，本会话的交代无处可投" % ctx.session_id}
    item = mid_turn.BOX.push(ctx.session_id, body, origin="webui",
                             run_id=ctx.run_id, attachments=atts)
    if item is None:
        return {"ok": False, "reason": "queue-full",
                "error": "待送达的交代已达上限（%d 条），等这批工具返回再发" % mid_turn.MAX_PENDING}
    return {"ok": True, "run_id": ctx.run_id, "session_id": ctx.session_id,
            "item_id": item["id"], "pending": item.get("pending", 1)}


def _ask_pending() -> list:
    """当前真挂着的 ask 提问（ask_user）。超时到点的不再报，免得复活僵尸卡。

    剩余时间一律按**批次共享窗口**算（不再逐卡 born+timeout）：主人正在一张张答，
    先答的那张留下的旧计时不该让后面的卡提前消失。
    """
    with _ASK_LOCK:
        items = list(_ASK.values())
    rem = int(ASK_WINDOW.remaining())
    live = ASK_WINDOW.live()
    out = []
    for it in items:
        if it.get("box", {}).get("answer") is not None:
            continue
        if rem <= 0:
            continue
        out.append({"type": "ask_request", "ask_id": it.get("ask_id"),
                    "run_id": it.get("run_id"), "session_id": it.get("session_id"),
                    "question": it.get("question"), "options": it.get("options"),
                    "mode": it.get("mode"), "timeout": it.get("timeout"),
                    "batch_id": it.get("batch_id"), "batch_size": it.get("batch_size"),
                    "batch_live": live, "remaining": rem, "restored": True})
    return out


def _answer_ask(ask_id: str, answer: str) -> Dict[str, Any]:
    with _ASK_LOCK:
        item = _ASK.get(ask_id)
    if not item:
        return {"ok": False, "error": "该提问已结束或超时"}
    waiter, box = item.get("waiter"), item.get("box")
    if waiter is None or box is None:
        return {"ok": False, "error": "提问记录不完整，已按失效处理"}
    box["answer"] = answer
    waiter.set()
    # 有人在答 → 把本批共享窗口往后续：主人正一张张回答，剩下的卡不该被判超时
    win = ASK_WINDOW.touch()
    return {"ok": True, "ask_id": ask_id,
            "batch_live": ASK_WINDOW.live(), "remaining": int(win.get("remaining") or 0)}


# ============================================================
# 9. HTTP 服务（127.0.0.1 随机端口 + token）
# ============================================================
class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AetherBridge/1.0"
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("X-Bridge-Token") or (
            self.path.split("token=", 1)[1].split("&", 1)[0] if "token=" in self.path else "")
        return got == TOKEN

    def _json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "bad token"})
        path = self.path.split("?", 1)[0]
        if path == "/health":
            ctx = active_run()
            return self._json(200, {
                "ok": True, "pid": os.getpid(), "mode": MODE or "default",
                "phase": "RUNNING" if ctx else "IDLE",
                "run_id": ctx.run_id if ctx else None,
                "session_id": ctx.session_id if ctx else None,
                "model": getattr(_agent, "MODEL_NAME", "?") if _agent else None,
                "tools": len(_tool_names()), "ask_pending": len(_ASK),
                "approval": APPROVAL_STATE.get("value"),
                "approval_pending": _approval_pending(),
            })
        if path == "/events":
            return self._sse()
        if path == "/tools":
            return self._json(200, {"ok": True, "tools": _tool_names(),
                                    "injected": list(_injected)})
        if path == "/orch":
            # 编排器占用视图：前端刷新/重连/定时对表用。
            # 观测面绝不允许 500（刷新路径上的一次异常就会让橙点全灭）。
            try:
                return self._json(200, _orch_view())
            except Exception as e:
                return self._json(200, {"ok": False, "pipes": [],
                                        "error": "%s: %s" % (e.__class__.__name__, e)})
        if path == "/ask/pending":
            return self._json(200, {"ok": True, "cards": _ask_pending()})
        if path == "/approval/pending":
            try:
                import approval_adapter                # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval channel not ready: %s" % e})
            try:
                return self._json(200, {"ok": True, "cards": approval_adapter.pending_cards()})
            except Exception as e:
                # 观测面绝不允许影响裁决本身：出错就报空列表，别把异常抛给网关
                return self._json(200, {"ok": False, "cards": [],
                                        "error": "%s: %s" % (e.__class__.__name__, e)})
        return self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "bad token"})
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/start":
            return self._json(200, {"ok": True, "pid": os.getpid(),
                                    "mode": MODE or "default"})
        if path == "/chat":
            sid = str(body.get("session_id") or "").strip()
            msg = str(body.get("message") or "").strip()
            atts = _attachments_of(body)
            # 有附件时允许正文为空（"只发一张图"是合理用法）
            if not sid or (not msg and not atts):
                return self._json(400, {"ok": False, "error": "session_id 必填，且 message 与 attachments 至少有一个"})
            res = _start_run(sid, msg, atts)
            return self._json(200 if res.get("ok") else 409, res)
        if path == "/stop":
            return self._json(200, _stop_run(body.get("run_id")))
        if path == "/mid_turn":
            # 回合运行中追加「用户交代」（WebUI 独有）：投递给当前活动回合，
            # 由主循环在「下一批工具返回」处取件注入。也可带附件。
            res = _mid_turn(str(body.get("session_id") or ""), str(body.get("text") or ""),
                            body.get("run_id"), _attachments_of(body))
            if res.get("ok"):
                return self._json(200, res)
            # 没有 reason 的失败只可能是入参问题（空文本 / 过长）→ 400；
            # 其余（无活动回合 / 回合或会话不符 / 队列满）都是状态不符 → 409。
            return self._json(400 if not res.get("reason") else 409, res)
        if path == "/ask/answer":
            ask_id = str(body.get("ask_id") or "").strip()
            if not ask_id:
                return self._json(400, {"ok": False, "error": "ask_id 必填"})
            answer = body.get("answer")
            if answer is None:
                answer = body.get("choice", "")
            if isinstance(answer, (list, tuple)):
                answer = "、".join(str(a) for a in answer)
            return self._json(200, _answer_ask(ask_id, str(answer)))
        if path == "/approval/answer":
            try:
                import approval_adapter                # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval channel not ready: %s" % e})
            res = approval_adapter.answer(str(body.get("ask_id") or ""),
                                       str(body.get("choice") or ""), body)
            return self._json(200 if res.get("ok") else 409, res)
        if path == "/approval/rules":
            # 面板查询：返回内存里当前真正生效的规则（永久 + 本会话会话级）
            try:
                import approval as _ab                    # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval not ready: %s" % e})
            sid = str(body.get("session_id") or "")
            return self._json(200, {"ok": True, "rules": _ab.SCOPES.list_all(sid),
                                    "session_id": sid})
        if path == "/approval/rules/revoke":
            try:
                import approval as _ab                    # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval not ready: %s" % e})
            sid = str(body.get("session_id") or "")
            try:
                n = _ab.SCOPES.revoke(str(body.get("path") or ""),
                                      str(body.get("scope") or ""), sid)
            except Exception as e:
                return self._json(500, {"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)})
            return self._json(200, {"ok": True, "removed": n,
                                    "rules": _ab.SCOPES.list_all(sid)})
        if path == "/permission/mode":
            # 会话级权限模式：查（不带 mode）与切（带 mode）。
            # 引擎在本进程里，所以模式必须**当场**写进引擎缓存 —— 下一次工具调用就按
            # 新模式判（主人定的语义：已发出的调用不受影响，只约束后面的）。
            sid = str(body.get("session_id") or "").strip()
            if not sid:
                return self._json(400, {"ok": False, "error": "session_id 必填"})
            try:
                import permission_modes as _pm             # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False,
                                        "error": "permission modes not ready: %s" % e})
            want = body.get("mode")
            prev = _pm.get(sid)
            if want is None:
                return self._json(200, {"ok": True, "session_id": sid, "mode": prev,
                                        "label": _pm.label(prev), "icon": _pm.icon(prev),
                                        "catalog": _pm.catalog()})
            # **切换路径严格校验，不做宽容收敛。** 读回来的值（会话文件、旧字段）
            # 可以宽容地收敛到默认档，但用户/前端递进来的模式名不行：把
            # "read-only" 这种手滑收敛成「普通」，等于一次拼写错误换来一档**更松**的
            # 权限 —— 那是 fail-open，正是本项目一直在堵的那类病。
            _want_raw = str(want).strip().lower()
            if _want_raw not in _pm.MODES:
                return self._json(400, {"ok": False, "error": "未知的权限模式 %r" % str(want),
                                        "valid": list(_pm.MODES),
                                        "catalog": _pm.catalog()})
            now = _pm.set(sid, _want_raw)
            if now != prev:
                act = active_run()
                if act is None or act.session_id != sid:
                    # 没有并发写者：当场落盘（关机重开也在）
                    _persist_mode(sid)
                # 本会话正在跑回合时**故意不写**：save_session 是全量覆盖写，会和那一回合
                # 互相覆盖（丢消息）。等它自己的保存带上这一档（save_session 读 PM.get），
                # 回合收口时 _ensure_mode_persisted 再兜一次。
                # 切换记录进**账本**：历史里从此不再记切换（主人要求不占上下文），
                # 但审计不能跟着消失 —— 账本不进 LLM 上下文，正合适。
                try:
                    import approval as _ab                  # noqa: PLC0415
                    _ab.ledger({"event": "mode_switch", "session_id": sid,
                                "mode": now, "previous": prev, "source": "webui"})
                except Exception:
                    pass
                try:
                    BUS.emit("permission_mode", {"session_id": sid, "mode": now,
                                                 "label": _pm.label(now),
                                                 "icon": _pm.icon(now),
                                                 "status": _pm.status_of(now),
                                                 "previous": prev})
                except Exception:
                    pass
            return self._json(200, {"ok": True, "session_id": sid, "mode": now,
                                    "label": _pm.label(now), "icon": _pm.icon(now),
                                    "previous": prev, "changed": now != prev,
                                    "catalog": _pm.catalog()})
        if path == "/exit":
            wait_sec = float(body.get("timeout") or 10)
            self._json(200, {"ok": True, "message": "正在优雅退出"})
            threading.Thread(target=_graceful_exit, args=(wait_sec,),
                             name="bridge-exit", daemon=True).start()
            return
        return self._json(404, {"ok": False, "error": "unknown path"})

    def _sse(self):
        sub = BUS.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": bridge-stream\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = sub.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(
                        b'event: heartbeat\ndata: {"seq":0,"type":"heartbeat"}\n\n')
                    self.wfile.flush()
                    continue
                payload = json.dumps(evt, ensure_ascii=False)
                self.wfile.write(
                    f"event: {evt['type']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(sub)

    def log_message(self, fmt, *args):  # 静音 access log，改道 stderr 并脱敏
        try:
            print("[bridge-http] " + _redact_secret(fmt % args), file=sys.__stderr__, flush=True)
        except Exception:
            pass


_SECRET_RE = re.compile("(X-Bridge-Token[=:] ?)[^&\s\"']+", re.IGNORECASE)


def _redact_secret(msg: str) -> str:
    """访问日志里的 bridge token 一律打码：日志可留，凭据不可留。"""
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", msg)


def _approval_pending() -> int:
    """挂起中的审批数；-1 = 审批通道不可用，前端据此显示异常态。"""
    try:
        import approval_adapter                     # noqa: PLC0415
        return approval_adapter.pending_count()
    except Exception:
        return -1


def _tool_names() -> List[str]:
    try:
        import agent_tools  # noqa: PLC0415
        return sorted(agent_tools.AVAILABLE_TOOLS.keys())
    except Exception:
        return []


def _graceful_exit(wait_sec: float) -> None:
    """优雅退出：中断活动回合 -> 等它在边界保存 -> 关编排器 -> 结束进程。"""
    ctx = active_run()
    if ctx is not None:
        _stop_run(ctx.run_id)
        deadline = time.time() + wait_sec
        while time.time() < deadline and active_run() is not None:
            time.sleep(0.2)
    try:
        if _agent is not None:
            _agent.shutdown_orchestrator()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.__stderr__.flush()
    except Exception:
        pass
    print("[bridge] 进程退出", file=sys.__stderr__, flush=True)
    os._exit(0)


# ============================================================
# 10. 入口
# ============================================================
def main() -> None:
    sys.stdout = StdoutCapture()  # import 期 print 也会安全落 stderr
    try:
        info = _install_runtime()
    except BaseException as e:  # noqa: BLE001
        print(f"[bridge] AB 模块加载失败: {type(e).__name__}: {e}",
              file=sys.__stderr__, flush=True)
        try:
            import traceback
            print(traceback.format_exc(), file=sys.__stderr__, flush=True)
        except Exception:
            pass
        os._exit(2)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
    except OSError as e:
        print(f"[bridge] 端口绑定失败: {e}", file=sys.__stderr__, flush=True)
        os._exit(3)
    server.daemon_threads = True

    port = server.server_address[1]
    _cm = getattr(_agent, "CM_PARAMS", None) or {}
    _win = int(_cm.get("window_tokens") or 0)
    hello = {"port": port, "pid": os.getpid(), "mode": MODE or "default",
             "model": info["model"], "tools": len(info["tools"]),
             "injected": info["injected"], "pools": info.get("pools"),
             "usage_watch": "on" if "usage_watch" in info["injected"] else "off",
             # 上下文管理器（视图层）：前端画「上下文占比」圆圈需要窗口与阈值，
             # 一律取 agent 进程里的同一份 CM_PARAMS，避免两边各写一份配置
             "ctx": {"enabled": bool(_cm.get("enabled")),
                     "window": _win,
                     "threshold": int(_win * float(_cm.get("trigger_ratio") or 0.55)),
                     "keep_recent_rounds": int(_cm.get("keep_recent_rounds") or 0)},
             # 计量账本：前端启动即拿到上次的每会话用量（不必先发一条消息）
             "usage_ledger": _ledger_snapshot()}
    _REAL_STDOUT.write(json.dumps(hello, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
