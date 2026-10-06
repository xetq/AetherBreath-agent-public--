"""
任务编排器（Task Orchestrator）
基于工具调用粒度的并行调度器，满足：
- 8 个 worker 并发，超出排队
- 主进程被 kill 时优雅关闭线程池
- 批量结果聚合后统一返回
- 三种依赖类型处理：数据依赖、文件冲突、交互/危险工具

架构（常驻双通道 + 工具模板 → 管道实例）：
- 编排器【常驻】：实例化一次即创建 串行管道（1 worker）+ 并行管道（8 worker）
  两个常驻线程池，整场会话复用，不再每次 execute() 反复建销线程池
- 路由规则（agent.py 仅作路由器，一切工具执行都经此）：
  · 单工具调用 → 串行管道（serial pool，并发=1）
  · 多工具批 → 依赖分层 → 并行管道（parallel pool，层内并发、层间串行）
- 工具库（AVAILABLE_TOOLS）中的工具以【模板】形式注册，保持只读、永不修改
- 每次执行一个 tool_call 时，编排器以对应工具模板 create_pipeline() 创建
  一个【独立管道实例】来执行该次任务
- 同一工具可以被并发创建多个管道并行执行，各管道状态/结果互不干扰
- RPC 扩展点：ToolPipeline.run() 是唯一执行入口，未来可替换为
  “通过 RPC 通道调用远端工具服务”以实现跨进程执行与批量超时中断
"""

import json
import os
import signal
import threading
import time
import inspect
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import List, Callable, Any, Dict, Optional, Set

# ============================================================
# 1. 常量配置
# ============================================================

MAX_WORKERS = 16  # 最大并发 worker 数（2026-09-29：8 -> 16，容纳跨回合作业）

# 工具自己声明的超时之外，编排器额外留的缓冲（秒）。
# 工具内部有自己的 deadline 时（execute_shell 强杀进程树、ask_user 关窗返回），
# 让工具先到点、返回**它自己的**结果，编排器只兜底 —— 否则编排器抢先判「超时」，
# 而工具线程还在跑，模型收到「失败」就会合法重试（幽灵执行的成因，见 _run_on_pool）。
ORCH_GRACE = 10

# 需要强制串行的工具（交互、危险）——全局兜底，模板级 never_parallel 优先
# 注意工具名必须写**真名**：这里曾经写着 "clarify"，而 WebUI 注入的提问工具叫
# "ask_user" —— 那条名字从来没匹配上，等于"交互工具要串行"的设计意图从未生效。
_NEVER_PARALLEL_TOOLS = {
    "ask_user",         # 需要用户交互（ask 提问）
    "delegate_task",    # 派发子任务，需等待
    "terminal",         # 终端交互
    "browser_exec",     # 浏览器交互
    # 自维护（AB 自己集成 MCP server）：它的每个动作都是"读注册表 → 改 → 写回"
    # 的序列，还写失败计数状态文件。同批里并发跑两个（或与别的写盘工具交错）
    # 会**丢更新**——后写的把先写的覆盖掉，表现为"明明集成了却没那条"。
    # 所以强制它单独占一层、串行执行。
    "mcp_manage",
}


# ===== 后台作业（Background Jobs）=====
# 一个工具调用声明 background=true 时，它不再阻塞本回合：编排器立即返回占位结果，
# 真正的执行交给后台线程跑，结果登记进 JobRegistry，稍后（**跨回合**）用 task_output 取回。
# 设计依据：agent_workspace/DSH框架参考对AB优化/PLAN_编排器后台作业机制.md
JOB_LOG_RING_LINES = 200        # 每作业日志环形缓冲：行数上限
JOB_LOG_RING_BYTES = 16 * 1024  # 同上，字节上限（先到先裁）
JOB_INLINE_LIMIT = 4096         # 单作业输出内联进看板/结果时的上限
JOB_BOARD_BUDGET = 6144         # （已废弃：看板不再按预算截断 —— 见 JOB_DELIVERY_*）
JOB_DELIVERY_PREFIX = "【后台作业交付 · 跨回合任务（不是你本回合发起的动作）】"
JOB_DELIVERY_LOG_TAIL = 30      # 没有结果时，交付里附多少行日志
JOB_MAX_LIFETIME = 900          # 作业硬寿命（秒）——P2 看门狗消费（当前未启用）
JOB_WAIT_DEFAULT = 30           # task_output 的默认等待秒数
JOB_OUTPUT_TAIL_LINES = 20      # task_output 默认给最近多少行日志
JOB_WATCHDOG_INTERVAL = 30.0    # 寿命看门狗的扫描间隔（秒）；测试会调小它
JOB_STOP_WAIT = 2.0             # 「一起停」等强杀落地的最长秒数（有界：绝不卡住停止按钮）

# 交互/等待类工具**禁止**转后台：后台没有人在旁边看着，它们在那里会卡死或抢终端/浏览器。
# 与 _NEVER_PARALLEL_TOOLS 是同一批工具（"必须独占一层"和"不许离开回合"本就是同一类），
# 直接引用同一个集合对象 —— 不新增第二份名单，避免两处漂移。
NO_BACKGROUND_TOOLS = _NEVER_PARALLEL_TOOLS

# ============================================================
# 2. 数据结构
# ============================================================

@dataclass
class ToolCall:
    """单个工具调用（来自 LLM 的 tool_calls）"""
    id: str
    name: str
    arguments: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# 业务失败契约（对外工具遵守；编排器 / WebUI / 落盘还原三处共同判定）：
#   · 文本方言：返回值以 ❌ 开头 —— 工具层 27 处的老约定，也是模型最容易识别的形态
#   · 结构方言：dict 里 success=False / ok=False —— time_weather、skillhub_install 的形态
#   · ⚠️ 开头**不算失败**：它是"限制 / 降级说明"（如 mcp_gateway 的"station 不可用先修它"），
#     语义是"有前提、没做成"，不该混进失败计数
# 之所以要单独判一层：success（=没抛异常）会把上面两种失败都记成成功 —— 日志与界面因此虚报。
# 本函数只做显示/计数层识别，不参与 success 语义，运行时行为与模型所见内容完全不变。
# **唯一实现**：编排器、WebUI bridge、会话还原层全部调它。历史教训——这三处曾各留一份
# 复制品且实测不等价（sessions 那份少了 lstrip），同一动作在实时界面标红、刷新后标绿。
BIZ_FAIL_MARKS = ("❌",)          # 文本方言：真正的错误（**新工具唯一允许的失败形态**）
# 历史方言：**只读兼容，禁止新代码使用**。
# 中文「错误：」曾经是 fetch_url / calculator 的失败形态，2026-09-17 已统一成 ❌。
# 之所以保留识别：sessions 还原读的是**已经落盘的旧会话原文**，那些数据不会因为
# 改代码而变成成功 —— 不认它，"刷新后标绿"这个老毛病在老会话上会继续存在。
# 守门的是契约测试：test_no_chinese_error_prefix_left_in_tools 保证新工具不再产生它。
LEGACY_FAIL_MARKS = ("错误：",)


def is_business_failure(result: Any) -> bool:
    """返回值本身是否表达业务失败（与 success 是两回事，别混）。"""
    if isinstance(result, str):
        head = result.lstrip()
        return head.startswith(BIZ_FAIL_MARKS) or head.startswith(LEGACY_FAIL_MARKS)
    if isinstance(result, dict):
        # 结构方言：显式失败位。两个字段都没有的结构不判死（宁可不报，也不误报）。
        return result.get("success") is False or result.get("ok") is False
    return False


@dataclass
class ToolResult:
    """单个工具执行结果"""
    tool_call_id: str
    success: bool
    result: Any = None
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # 业务失败（函数正常返回、但返回值是错误文本）。只用于展示/计数，
    # 不改 success —— 避免污染传给 LLM 的 content 与依赖层判定。
    biz_fail: bool = False


@dataclass
class BatchResult:
    """一批工具调用的聚合结果"""
    results: List[ToolResult]
    success_count: int
    failure_count: int
    total_time: float
    is_interrupted: bool = False


# ============================================================
# 2.5 后台作业：Job 与 JobRegistry（跨回合取回）
# ============================================================
# 与 ToolPipeline 的分工：管道描述"一次工具调用"的生命周期（回合内）；
# 作业描述"一件已经交出去、可以稍后再看"的事（跨回合）。
# 一个后台作业 = 一个正在跑的管道 + 一份可被 task_* 三个工具读写的登记条目。

JOB_STATUS_CN = {
    "running": "运行中",
    "done": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "expired": "因超寿命被丢弃",
}


class Job:
    """一个后台作业。

    并发模型：收割回调跑在 **worker 线程**（future 完成时），而 task_list /
    task_output / 看板渲染跑在**请求线程** —— 所有字段读写都走自己的 RLock。
    """

    def __init__(self, job_id: str, label: str, sid: str, tool: str,
                 tool_call_id: str = ""):
        self.job_id = job_id
        self.label = label            # 可读名：工具名(关键参数摘要)
        self.sid = sid or "-"         # 归属会话（作业全局共享，但标注来源）
        self.tool = tool
        self.tool_call_id = tool_call_id
        self.status = "running"
        self.created_at = time.time()
        self.started_at = self.created_at
        self.finished_at: Optional[float] = None
        self.result: Any = None
        self.error: Optional[str] = None
        self.biz_fail = False
        # 刻意**没有** delivered 标记（P8 加过、P12 删掉）：交付完成的作业会**直接出册**，
        # 所以"交付过没有"这件事由"还在不在册里"表达 —— 不留一个写了没人读的字段（P6 的教训）。
        self.output_chars = 0         # 输出字符数（释放内存后仍要能如实报大小）
        self.log_lines: List[str] = []
        self.log_bytes = 0
        self.interrupter: Optional[Callable] = None   # P2：尽力中断（杀子进程等）
        self._lock = threading.RLock()

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        start = self.started_at or self.created_at
        return end - start

    @property
    def alive(self) -> bool:
        return self.status == "running"

    def append_log(self, line: Any) -> None:
        """收一行工具日志（环形裁剪）。非字符串先降级成文本，绝不让它炸掉缓冲。"""
        try:
            text = line if isinstance(line, str) else str(line)
        except Exception:
            return
        with self._lock:
            self.log_lines.append(text)
            self.log_bytes += len(text) + 1
            while self.log_lines and (len(self.log_lines) > JOB_LOG_RING_LINES
                                      or self.log_bytes > JOB_LOG_RING_BYTES):
                gone = self.log_lines.pop(0)
                self.log_bytes -= len(gone) + 1
            if self.log_bytes < 0:
                self.log_bytes = 0

    def finish(self, result: Any = None, error: Optional[str] = None,
               biz_fail: bool = False, status: Optional[str] = None) -> None:
        """结算作业。**已结算的作业不会被覆盖** —— 被 kill 之后 worker 才返回的
        "迟到结果"必须丢弃，否则界面会出现"已取消的作业又变成成功"。"""
        with self._lock:
            if self.status != "running":
                return
            self.result = result
            self.error = error
            self.biz_fail = biz_fail
            # 先记下输出大小：交付之后内存会被释放，那时还要能如实报"有多大"
            try:
                if result is not None:
                    body = result if isinstance(result, str) else repr(result)
                    self.output_chars = len(body or "")
            except Exception:
                self.output_chars = 0
            if status:
                self.status = status
            elif error:
                self.status = "failed"
            else:
                self.status = "done"
            self.finished_at = time.time()

    def one_line(self) -> str:
        """`task_list` 用的一行摘要：**只报身份/状态/耗时**，不带任何内容。

        P12 起 `task_list` 只列"编排器当前状况"（在跑 / 待交付），所以这里只有两种尾巴：
        运行中的没有尾巴，已结算而尚未交付的标「待交付」。
        """
        st = JOB_STATUS_CN.get(self.status, self.status)
        tail = "" if self.alive else " · 待交付"
        return "[%s] %s · %s · %s %.1fs%s" % (
            self.sid, self.job_id, self.label, st, self.elapsed, tail)

    def delivery_text(self) -> str:
        """**自动交付**给会话的全文（不截断 —— 它跟一次前台工具结果同级）。

        为什么不再有 4KB 内联上限：旧实现把交付截到 4096 字符，还在文案里写
        "全文用 task_output 取"，而 task_output 走的是同一个渲染器 —— 那句承诺根本
        兑现不了（真机上被主人抓出来）。现在**交付即全文**，取回通道不再返回内容，
        于是"全文"只有这一条路，也就必须给全。
        """
        with self._lock:
            st = JOB_STATUS_CN.get(self.status, self.status)
            out = [JOB_DELIVERY_PREFIX,
                   "作业 %s：%s（%s，已跑 %.1fs，归属会话 %s）"
                   % (self.job_id, self.label, st, self.elapsed, self.sid)]
            if self.error:
                out.append("错误：%s" % self.error)
            if self.result is not None:
                body = self.result if isinstance(self.result, str) else repr(self.result)
                out.append("输出（全文 %d 字符）：" % len(body))
                out.append(body)
            elif self.log_lines:
                tail = self.log_lines[-JOB_DELIVERY_LOG_TAIL:]
                out.append("日志（最后 %d 行）：" % len(tail))
                out.extend(tail)
            out.append("· 这是自动交付，已写入本会话历史；该作业的内存已释放")
            return chr(10).join(out)

    def progress_text(self, max_lines: int = JOB_OUTPUT_TAIL_LINES) -> str:
        """`task_output` 用的**运行情况**：运行中给状态 + 最近日志；已结算只说"待交付"。

        这就是"砍掉读内容"：内容走自动交付，这里只回答"它现在怎么样"。
        P12 起交付完成的作业**已经出册**，所以不存在"已交付"这一支（`task_output` 会走
        "没有这个作业"那条路 —— 文案里会点明原因）。
        """
        with self._lock:
            st = JOB_STATUS_CN.get(self.status, self.status)
            out = ["作业 %s：%s（%s，已跑 %.1fs）" % (self.job_id, self.label, st, self.elapsed)]
            if self.alive:
                tail = self.log_lines[-max_lines:] if max_lines > 0 else []
                if tail:
                    out.append("最近日志（%d 行）：" % len(tail))
                    out.extend("  " + x for x in tail)
                else:
                    out.append("（还没有日志输出）")
                return chr(10).join(out)
            # 已结算、还没交付：不返回内容，只说清"接下来会发生什么"
            out.append("已结算，将在本会话下一次请求时**自动交付**全文"
                       "（这里不再返回内容；交付之后它就从登记册出册了）。")
            if self.output_chars:
                out.append("输出大小：约 %d 字符。" % self.output_chars)
            if self.error:
                out.append("错误：%s" % self.error)
            return chr(10).join(out)

    def release(self) -> int:
        """交付完成：**释放结果与日志占的内存**，只留身份/状态（列表还要列它）。

        返回释放掉的字符数（给日志/诊断用）。已交付的作业不该再往看板/交付里凑，
        所以清空是安全的；`output_chars` 留着，好让 `task_output` 仍能如实报大小。
        """
        with self._lock:
            freed = 0
            try:
                if self.result is not None:
                    body = self.result if isinstance(self.result, str) else repr(self.result)
                    freed = len(body or "")
            except Exception:
                freed = 0
            self.result = None
            self.error = None
            self.log_lines = []
            self.log_bytes = 0
            return freed


class JobRegistry:
    """作业登记册（进程内单例，挂在 TaskOrchestrator.jobs 上）。

    线程安全：一把 RLock 管所有读写。**持锁区内不再取同一把非重入锁**
    （历史 P0：threading.Lock 不可重入 -> 持锁的端点全部超时、不持锁的照常响应）。
    """

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._seq = 0
        self._lock = threading.RLock()

    def register(self, tool: str, label: str, sid: str, tool_call_id: str = "") -> Job:
        with self._lock:
            self._seq += 1
            job = Job(job_id="j%d" % self._seq, label=label, sid=sid or "-",
                      tool=tool, tool_call_id=tool_call_id)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, include_finished: bool = True) -> List[Job]:
        with self._lock:
            jobs = [self._jobs[k] for k in self._order if k in self._jobs]
        return jobs if include_finished else [j for j in jobs if j.alive]

    def drop(self, job_id: str) -> bool:
        """从登记册移除（不动线程，只让它不再出现在看板里）。"""
        with self._lock:
            if job_id in self._jobs:
                self._jobs.pop(job_id, None)
                if job_id in self._order:
                    self._order.remove(job_id)
                return True
            return False

    def clear_all(self) -> int:
        """清空整个登记册（一起停收尾用）。**不再结算**已经在跑的作业 ——
        调用方需要先自己 finish 它们（`TaskOrchestrator.stop_all_jobs` 就是这么做的）。"""
        with self._lock:
            n = len(self._jobs)
            self._jobs.clear()
            self._order.clear()
            return n

    def wait(self, job_id: str, seconds: float = JOB_WAIT_DEFAULT) -> Optional[Job]:
        """最多等这么久：作业结算即刻返回，到点回当前快照（不报错）。"""
        job = self.get(job_id)
        if job is None:
            return None
        deadline = time.time() + max(0.0, float(seconds))
        while job.alive and time.time() < deadline:
            time.sleep(0.05)
        return job

    def pending_delivery(self, session_id: Optional[str] = None) -> List[Job]:
        """**待自动交付**的作业：本会话 + 已结算（还在册里 = 还没交付过）。

        三条边界都是真机踩出来的：
          · 只给归属会话（`sid` 相等）—— 跨会话交付 = 串台（真机：新开会话不停收到
            别的会话的作业）；
          · 只交付**已结算**的 —— 运行中的作业已经以「⏳ 已转后台」写进了本回合的
            工具结果，模型自己的历史里就有它；
          · 只交付**一次** —— 这条现在由"交付即出册"保证（见 `mark_delivered`），
            不再需要额外的 delivered 标记。

        `session_id=None` 表示不按会话过滤（只给测试/诊断用；生产调用点永远带会话）。
        """
        with self._lock:
            return [self._jobs[k] for k in self._order
                    if k in self._jobs
                    and not self._jobs[k].alive
                    and (session_id is None or self._jobs[k].sid == session_id)]

    def live_summary(self) -> List[Job]:
        """**编排器当前状况**：还在册里的全部作业（= 在跑 + 待交付）。

        P12 起 `task_list` 只列这个 —— 交付完的作业已经出册，所以登记册不会随着
        "跑过的作业越来越多"而一直长大（主人 2026-09-30 的要求）。
        """
        with self._lock:
            return [self._jobs[k] for k in self._order if k in self._jobs]

    def mark_delivered(self, job_id: str) -> bool:
        """交付完成后：**释放内存并从登记册出册**（主人 P12 定：交付过的不用再留着）。

        顺序红线：调用方必须**先**把交付落盘（写进会话历史），**再**调这里 ——
        反过来的话，一次写盘失败就等于这份结果永久消失。

        为什么出册是安全的：登记册是**内存态**，进程一重启本来就没了；而"交付过没有"
        这件事从此由"还在不在册里"表达（不再需要 delivered 标记）。
        """
        job = self.get(job_id)
        if job is None:
            return False
        freed = job.release()
        self.drop(job_id)
        if freed:
            self._freed_chars = getattr(self, "_freed_chars", 0) + freed
        return True

    def freed_chars(self) -> int:
        """已释放的字符总量（诊断用：交付到底省下多少内存）。"""
        return int(getattr(self, "_freed_chars", 0))

    @staticmethod
    def pool_pressure_note(self) -> str:
        """池压力提示：占用逼近容量时明说"这是作业堆积"。

        主人定的口径：**池满 = 症状，不是要用扩容去掩盖的问题** ——
        所以这里不自动扩容、不静默排队，而是把它摆到模型眼前。
        """
        try:
            total = int(self.max_workers)
            with self._lock:
                used = len(self._active_futures)
            if total > 0 and used >= max(1, total - 2):
                return ("⚠️ 池压力：%d/%d 个并行槽位被占用 —— 作业在堆积。"
                        "先 task_list 看清是什么在跑：等它、取回它、或 task_kill 掉不再需要的。"
                        % (used, total))
        except Exception:
            return ""
        return ""

    def make_label(tool: str, args: Any, width: int = 60) -> str:
        """label = 工具名(关键参数摘要) —— 让模型**一眼看出这是啥活**。"""
        key = None
        for k in ("command", "code", "query", "url", "file_path", "path",
                  "prompt", "station", "task"):
            if isinstance(args, dict) and args.get(k):
                key = k
                break
        summary = ""
        if key is None:
            bits = []
            for k, v in list((args or {}).items())[:2]:
                if k == "background":
                    continue
                bits.append("%s=%s" % (k, str(v)[:24]))
            summary = ", ".join(bits)
        else:
            raw = str(args.get(key) or "").strip()
            summary = raw.splitlines()[0] if raw else ""
        summary = summary.strip()
        if len(summary) > width:
            summary = summary[:width] + "..."
        return "%s(%s)" % (tool, summary) if summary else "%s()" % tool


class _JobLogger:
    """给后台作业用的 logger 包装：**边转发边记账 + 延迟绑定**。

    - 转发：同一行进原 logger，原有行为不变；
    - 记账：同一行进作业的环形缓冲 -> task_output 的"进度"就是这么来的；
    - 延迟绑定：持的是 **tool_call** 而不是 job，每次写日志时现查它身上的 `job` ——
      这样"**超时之后**才被收编成后台作业"的调用也能立刻开始攒进度
      （那时 job 才被建出来，绑定得太早会收不到）。
    """

    def __init__(self, base: Any, tool_call: Any = None, job: Optional[Job] = None):
        self._base = base
        self._tc = tool_call
        self._job = job

    def _current_job(self):
        if self._job is not None:
            return self._job
        try:
            return (getattr(self._tc, "metadata", None) or {}).get("job")
        except Exception:
            return None

    def _relay(self, level: str, msg: Any, *a, **kw) -> None:
        job = self._current_job()
        if job is not None:
            try:
                job.append_log("[%s] %s" % (level, msg if not a else (str(msg) + " " + str(a))))
            except Exception:
                pass
        if self._base is None:
            return
        try:
            fn = getattr(self._base, level, None)
            if callable(fn):
                fn(msg, *a, **kw)
        except Exception:
            pass

    def __getattr__(self, name):
        """其余属性/方法一律转发给原 logger —— 包装**不许改变**工具看到的接口。

        教训（2026-09-29 实测）：只实现 info/warning/error 是不够的，
        SessionLogger 还有 span/child 这类接口；包装缺了它们，日志会静默消失。
        """
        return getattr(self._base, name)

    def debug(self, msg, *a, **kw): self._relay("debug", msg, *a, **kw)
    def info(self, msg, *a, **kw): self._relay("info", msg, *a, **kw)
    def warning(self, msg, *a, **kw): self._relay("warning", msg, *a, **kw)
    def warn(self, msg, *a, **kw): self._relay("warning", msg, *a, **kw)
    def error(self, msg, *a, **kw): self._relay("error", msg, *a, **kw)
    def ok(self, msg, *a, **kw): self._relay("info", msg, *a, **kw)

# ============================================================
# 3. 工具模板与管道实例
# ============================================================

class ToolTemplate:
    """
    工具模板：以工具函数为蓝本的可复用定义。

    - 本身【只读】：保存工具名、函数、元信息，不执行任何逻辑
    - 每次执行通过 create_pipeline() 创建独立的管道实例
    - 同一模板可被并发创建多个管道，互不干扰
    - 工具库的原函数对象保持只读——模板仅引用它，不修改、不包装替换
    """

    def __init__(
        self,
        name: str,
        func: Callable,
        timeout: Optional[int] = None,
        never_parallel: bool = False,
        metadata: Dict[str, Any] = None,
    ):
        if not callable(func):
            raise TypeError(f"工具 {name} 不可调用: {type(func).__name__}")
        self.name = name
        self.func = func
        self.timeout = timeout
        self.never_parallel = never_parallel
        self.metadata = metadata or {}

    # 曾经这里有一个 `max_retries` 字段（审计 L1-B5 / L2-T2）——全项目零读取，
    # 却让人以为"工具级重试"已经具备。2026-09-17 删除，不再加回来：
    #   · 重试决策**下沉在工具自己**（如 mcp_manage 的有界自愈：只有它知道"再试一次
    #     是否有意义"，包名拼错再试一百次也不会成）；
    #   · 编排器面对的只是返回值字符串，统一重试反而会把不可重试的失败重放成灾难；
    #   · 真正需要声明的属性是"这个工具重复执行安不安全"，即 NON_IDEMPOTENT_TOOLS。
    # 若将来真要做编排器级重试，请先读 docs/ 与 skill agent-tool-execution-orchestration，
    # 别只把字段加回来。

    def create_pipeline(self, tool_call: ToolCall) -> "ToolPipeline":
        """以本模板为蓝本，为一次具体调用创建独立管道实例"""
        return ToolPipeline(tool_call=tool_call, template=self)

    def __repr__(self) -> str:
        return f"<ToolTemplate {self.name} never_parallel={self.never_parallel}>"


class ToolPipeline:
    """
    一次工具调用的独立执行管道。

    - 绑定一个 ToolCall + 模板引用
    - 拥有独立的执行状态 / 结果 / 时间戳
    - 同一模板的多个管道可并行，互不干扰
    - 状态机: pending → running → done | biz_failed | failed | cancelled
      （biz_failed = 函数正常返回但返回值是错误文本；success 仍为 True，
        只有展示层与计数按失败算）

    RPC 扩展点：run() 是工具执行的唯一入口；未来可改为通过 RPC 通道
    调用远端工具服务，实现跨进程执行与真正的超时中断。
    """

    def __init__(self, tool_call: ToolCall, template: ToolTemplate, created_at: float = None):
        self.tool_call = tool_call
        self.template = template
        self.created_at = created_at if created_at is not None else time.time()
        self.status = "pending"
        self.result: Any = None
        self.error: Optional[str] = None
        self.biz_fail: bool = False      # 异常路径也要有这个属性，别让读的人踩 AttributeError
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._func_kwargs = None  # 存储注入后的参数

    @property
    def elapsed(self) -> float:
        """管道执行耗时（秒）"""
        end = self.finished_at if self.finished_at is not None else time.time()
        start = self.started_at if self.started_at is not None else self.created_at
        return end - start

    def set_kwargs(self, kwargs: Dict[str, Any]):
        """设置注入后的参数（由编排器调用）"""
        self._func_kwargs = kwargs

    def run(self) -> ToolResult:
        """执行管道：真正调用工具函数，维护状态机"""
        if self.status == "running":
            return ToolResult(self.tool_call.id, False, error="管道已在执行中")

        self.status = "running"
        self.started_at = time.time()

        # 使用注入后的参数，如果没有则使用原始 arguments
        kwargs = self._func_kwargs if self._func_kwargs is not None else self.tool_call.arguments

        try:
            self.result = self.template.func(**kwargs)
            # 正常返回，但返回值可能是错误文本：标出来给日志/界面用
            self.biz_fail = is_business_failure(self.result)
            self.status = "done" if not self.biz_fail else "biz_failed"
            return ToolResult(
                self.tool_call.id,
                True,
                result=self.result,
                metadata={"pipeline": repr(self)},
                biz_fail=self.biz_fail,
            )
        except Exception as e:
            self.status = "failed"
            self.error = f"{type(e).__name__}: {str(e)}"
            return ToolResult(self.tool_call.id, False, error=self.error)
        finally:
            self.finished_at = time.time()

    def cancel(self):
        """取消管道（仅 pending 可取消；running 由调用方决定中断策略）"""
        if self.status == "pending":
            self.status = "cancelled"

    def __repr__(self) -> str:
        return f"<ToolPipeline {self.tool_call.id} ({self.template.name}) status={self.status}>"


# ============================================================
# 4. 依赖解析器
# ============================================================

class DependencyResolver:
    """
    解析工具调用之间的依赖关系
    三种依赖类型：
    1. 数据依赖：由模型声明（跨回合串行）
    2. 文件冲突：引擎自动检测（write → read 拆开）
    3. 交互/危险：引擎强制串行（全局 _NEVER_PARALLEL_TOOLS + 模板 never_parallel）
    """

    @staticmethod
    def resolve(
        tool_calls: List[ToolCall],
        tools_map: Optional[Dict[str, "ToolTemplate"]] = None,
    ) -> List[List[ToolCall]]:
        """
        返回分层列表：每层内部可并行，层与层之间串行

        tools_map: 归一化后的模板注册表（可选）；用于读取模板级 never_parallel
        """
        # 第一步：构建基础依赖图
        dep_map = {tc.id: set(tc.depends_on) for tc in tool_calls}

        # 第二步：检测并添加文件冲突依赖
        DependencyResolver._add_file_conflict_deps(tool_calls, dep_map)

        # 第三步：强制串行工具（全局集合 + 模板标记）添加屏障
        DependencyResolver._add_barrier_deps(tool_calls, dep_map, tools_map)

        # 第四步：拓扑排序 → 分层
        return DependencyResolver._topological_layers(tool_calls, dep_map)

    @staticmethod
    def _add_file_conflict_deps(tool_calls: List[ToolCall], dep_map: Dict[str, Set[str]]):
        """检测 write_file + read_file 同一路径 → 强制串行"""
        writes = {}
        reads = {}

        for tc in tool_calls:
            if tc.name == "write_file":
                path = tc.arguments.get("file_path") or tc.arguments.get("path")
                if path:
                    writes[path] = tc.id
            elif tc.name == "read_file":
                path = tc.arguments.get("file_path") or tc.arguments.get("path")
                if path:
                    reads.setdefault(path, []).append(tc.id)

        for path, write_id in writes.items():
            if path in reads:
                for read_id in reads[path]:
                    if read_id != write_id:
                        dep_map[read_id].add(write_id)

    @staticmethod
    def _add_barrier_deps(
        tool_calls: List[ToolCall],
        dep_map: Dict[str, Set[str]],
        tools_map: Optional[Dict[str, "ToolTemplate"]] = None,
    ):
        """
        强制串行工具：
        所有普通工具依赖它，它不依赖任何人。
        效果：该工具单独一层，前后层不能与它并行。

        判定来源（并集）：
        1. 全局 _NEVER_PARALLEL_TOOLS（兼容旧用法）
        2. 模板级 never_parallel=True
        """
        barrier_ids = []
        for tc in tool_calls:
            if tc.name in _NEVER_PARALLEL_TOOLS:
                barrier_ids.append(tc.id)
            elif (
                tools_map
                and tc.name in tools_map
                and getattr(tools_map[tc.name], "never_parallel", False)
            ):
                barrier_ids.append(tc.id)

        if not barrier_ids:
            return

        all_ids = {tc.id for tc in tool_calls}
        normal_ids = all_ids - set(barrier_ids)

        for barrier_id in barrier_ids:
            for normal_id in normal_ids:
                dep_map[normal_id].add(barrier_id)

    @staticmethod
    def _topological_layers(tool_calls: List[ToolCall], dep_map: Dict[str, Set[str]]) -> List[List[ToolCall]]:
        """拓扑排序：返回分层列表"""
        task_map = {tc.id: tc for tc in tool_calls}
        remaining = set(task_map.keys())
        layers = []

        while remaining:
            current_layer = []
            for task_id in list(remaining):
                if all(dep not in remaining for dep in dep_map.get(task_id, set())):
                    current_layer.append(task_map[task_id])

            if not current_layer:
                raise RuntimeError(f"无法解析依赖关系，剩余节点: {remaining}, 依赖图: {dep_map}")

            layers.append(current_layer)
            for task in current_layer:
                remaining.remove(task.id)

        return layers


# ============================================================
# 5. 核心编排器
# ============================================================

class TaskOrchestrator:
    """
    任务编排器
    - 8 个 worker 并发
    - 超时控制
    - 优雅关闭（shutdown hook）
    - 结果聚合
    - 工具模板 → 管道实例：每次 tool_call 创建独立管道执行
    - 自动注入 logger 到支持该参数的工具
    """

    def __init__(
        self,
        max_workers: int = MAX_WORKERS,
        default_timeout: int = 30,
        tools_map: Dict[str, Callable] = None,
        tool_timeouts: Optional[Dict[str, int]] = None,
        side_effect_tools: Optional[Set[str]] = None,
        log_enabled: bool = True,
        log_instance=None,
        serial_workers: int = 1,
    ):
        self.max_workers = max_workers
        self.serial_workers = serial_workers
        self.default_timeout = default_timeout
        self.tools_map = self._normalize_tools(tools_map)
        # per-tool 超时声明表（工具名 → 秒）。与 tools_map 一样**保留引用**而非快照：
        # 宿主（WebUI bridge）会在运行期往表里追加它注入的工具（ask_user），
        # 快照会让后注入的工具拿不到自己的时限，退回到全局默认。
        self.tool_timeouts: Dict[str, int] = tool_timeouts if tool_timeouts is not None else {}
        # 有副作用的工具（非幂等）名单，同样保留引用：超时结果的文案强度、
        # 以及 agent 层"拒绝原样重试"都以它为准。
        self.side_effect_tools: Set[str] = side_effect_tools if side_effect_tools is not None else set()
        self.log_enabled = log_enabled
        self.log_instance = log_instance

        # ===== 常驻双通道：一次性创建，整场会话复用 =====
        # 串行管道：并发 = serial_workers（默认 1），服务单工具调用等顺序任务
        # 并行管道：并发 = max_workers（默认 8），服务多工具批（依赖分层，层内并行）
        self._serial_pool = ThreadPoolExecutor(
            max_workers=self.serial_workers, thread_name_prefix="orch-serial"
        )
        self._parallel_pool = ThreadPoolExecutor(
            max_workers=self.max_workers, thread_name_prefix="orch-parallel"
        )

        self._shutdown_event = threading.Event()
        self._active_futures: Dict[Any, str] = {}
        self._lock = threading.Lock()


        # 后台作业登记册（task_list/task_output/task_kill 三个工具与看板都读它）
        self.jobs = JobRegistry()
        self._session_id: Optional[str] = None
        self._watchdog: Optional[threading.Thread] = None
        self._register_signal_handlers()
        self._start_watchdog()

    @staticmethod
    def _normalize_tools(tools_map: Optional[Dict[str, Any]]) -> Dict[str, ToolTemplate]:
        """工具库归一化：把裸函数/任意 callable 包装为 ToolTemplate。"""
        normalized: Dict[str, ToolTemplate] = {}
        for name, tool in (tools_map or {}).items():
            if isinstance(tool, ToolTemplate):
                normalized[name] = tool
            else:
                normalized[name] = ToolTemplate(name=name, func=tool)
        return normalized

    # ==================== 日志 ====================

    def _log(self, msg: str, level: str = "INFO"):
        if not self.log_enabled:
            return
        if self.log_instance:
            level_lower = level.lower()
            level_map = {"warn": "warning", "err": "error", "ok": "info"}
            level_lower = level_map.get(level_lower, level_lower)
            getattr(self.log_instance, level_lower)(f"[Orchestrator] {msg}")

    # ==================== 信号处理 ====================

    def _register_signal_handlers(self):
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        self._log(f"⚠️ 收到信号 {signum}，正在关闭编排器...", "WARN")
        self.shutdown()

    def set_logger(self, log_instance):
        """运行时更新日志实例（常驻单例跨会话复用时，日志归属当前会话）"""
        self.log_instance = log_instance

    def set_session(self, session_id: Optional[str]) -> None:
        """运行时更新当前会话 id：后台作业要标注归属（作业全局共享，但需知道来源）。"""
        self._session_id = session_id

    def shutdown(self, wait: float = JOB_STOP_WAIT) -> Dict[str, int]:
        """关闭编排器：**强杀所有作业（有界等待落地）**，取消未完成任务，回收双通道线程池。

        ⚠️ 关机链路上的关键一环（2026-09-30 P13）：调用方（bridge 的 `_graceful_exit`）
        在这之后**紧接着 `os._exit(0)`**。所以这里必须等强杀真的落地 —— 否则进程一没，
        没来得及杀的作业子树就成了孤儿（Job Object 刻意没设 KILL_ON_JOB_CLOSE，
        为的是不污染"用户故意放到后台的活儿"）。
        """
        self._shutdown_event.set()
        ledger = self.stop_all_jobs("编排器关闭", wait=wait)   # 一起停：等它真的停
        if isinstance(ledger, dict) and ledger.get("total"):
            self._log("🛑 关机：%d 个后台作业已取消（%d 个确认已收手%s）"
                      % (ledger.get("total", 0), ledger.get("settled", 0),
                         "，%d 个没在时限内收手" % ledger.get("stuck", 0)
                         if ledger.get("stuck") else ""), "WARN")
        self._log("🛑 正在取消未完成的任务...", "WARN")
        with self._lock:
            for future, tc_id in list(self._active_futures.items()):
                future.cancel()
        self._serial_pool.shutdown(wait=False)
        self._parallel_pool.shutdown(wait=False)
        self._log("✅ 编排器已关闭", "OK")
        return ledger if isinstance(ledger, dict) else {"total": 0, "settled": 0, "stuck": 0}

    # ==================== 主体执行 ====================

    def execute(
        self,
        tool_calls: List[ToolCall],
        timeout: Optional[int] = None,
    ) -> BatchResult:
        """
        执行一批工具调用。

        路由规则：单任务批 → 串行管道；多任务批 → 依赖分层 → 并行管道。
        agent.py 作为路由器，所有工具执行统一经此入口。
        """
        if not tool_calls:
            return BatchResult(results=[], success_count=0, failure_count=0, total_time=0.0)

        # ---- 后台作业：把 background 入参从参数里**摘出来** ----
        # 绝不能把它留在 arguments 里：工具函数签名没有这个参数，传进去就是 TypeError。
        # 交互/等待类工具即使声明了也拒绝 —— 后台没人在旁边，它们在那里会卡死或抢资源。
        for tc in tool_calls:
            bg = False
            if isinstance(tc.arguments, dict):
                bg = bool(tc.arguments.pop("background", False))
            if bg and tc.name in NO_BACKGROUND_TOOLS:
                self._log(f"⚠️ {tc.name} 是交互/等待类工具，不允许转后台（background 已忽略）", "WARN")
                bg = False
            tc.metadata["background"] = bg

        if self._shutdown_event.is_set():
            return BatchResult(
                results=[ToolResult(tc.id, False, error="编排器已关闭") for tc in tool_calls],
                success_count=0,
                failure_count=len(tool_calls),
                total_time=0.0,
                is_interrupted=True,
            )

        self._log(f"📋 收到 {len(tool_calls)} 个工具调用")
        for tc in tool_calls:
            deps = f" (依赖: {tc.depends_on})" if tc.depends_on else ""
            self._log(f"  └─ {tc.id}: {tc.name}{deps}")

        # ===== 路由：单工具 → 串行管道；多工具批 → 依赖分层 → 并行管道 =====
        if len(tool_calls) == 1:
            self._log("📐 单工具调用 → 串行管道（1 worker）")
            layer_groups = [(self._serial_pool, [tool_calls[0]])]
        else:
            try:
                layers = DependencyResolver.resolve(tool_calls, tools_map=self.tools_map)
                self._log(f"📐 分层结果: {len(layers)} 层 → 并行管道（{self.max_workers} worker）")
                for i, layer in enumerate(layers):
                    ids = [tc.id for tc in layer]
                    self._log(f"  Layer {i}: {ids}")
            except RuntimeError as e:
                self._log(f"❌ 依赖解析失败: {e}", "FAIL")
                return BatchResult(
                    results=[ToolResult(tc.id, False, error=f"依赖解析失败: {e}") for tc in tool_calls],
                    success_count=0,
                    failure_count=len(tool_calls),
                    total_time=0.0,
                )
            layer_groups = [(self._parallel_pool, layer) for layer in layers]

        all_results: Dict[str, ToolResult] = {}
        total_start = time.time()

        total_layers = len(layer_groups)
        for layer_idx, (pool, layer_tasks) in enumerate(layer_groups):
            if self._shutdown_event.is_set():
                self._log(f"⚠️ 执行到 Layer {layer_idx+1} 时被中断", "WARN")
                for tc in tool_calls:
                    if tc.id not in all_results:
                        all_results[tc.id] = ToolResult(tc.id, False, error="执行被中断")
                break

            self._log(f"▶️  执行 Layer {layer_idx+1}/{total_layers} ({len(layer_tasks)} 个任务)")

            layer_results = self._run_on_pool(pool, layer_tasks, timeout)

            for res in layer_results:
                all_results[res.tool_call_id] = res
                # 三态：异常失败 ❌ / 业务失败 ⚠ / 成功 ✅
                if not res.success:
                    mark, shown = "❌", res.error
                elif res.biz_fail:
                    mark, shown = "⚠️", res.result
                else:
                    mark, shown = "✅", res.result
                self._log(f"  {mark} {res.tool_call_id}: {shown}")

            if any((not r.success) or r.biz_fail for r in layer_results):
                self._log(f"⚠️ Layer {layer_idx+1} 有失败任务，继续执行后续层", "WARN")

        total_time = time.time() - total_start

        ordered_results = [all_results.get(tc.id, ToolResult(tc.id, False, error="结果丢失")) for tc in tool_calls]
        # 计数含业务失败，与上面逐条标记保持同一口径（虚报"全成功"就是这里来的）
        success_count = sum(1 for r in ordered_results if r.success and not r.biz_fail)
        failure_count = len(ordered_results) - success_count

        self._log(f"🏁 全部完成 | 成功: {success_count} | 失败: {failure_count} | 耗时: {total_time:.2f}s")

        return BatchResult(
            results=ordered_results,
            success_count=success_count,
            failure_count=failure_count,
            total_time=total_time,
            is_interrupted=self._shutdown_event.is_set(),
        )


    # ==================== 后台作业（跨回合） ====================

    def _launch_background(self, pool: ThreadPoolExecutor,
                           tasks: List[ToolCall]) -> List[ToolResult]:
        """把声明了 background 的调用挂到后台：提交 -> 立即登记作业 -> 返回占位结果。

        关键就一句：**不 join**。这就是"本回合不阻塞"的全部秘密 —— 结果由
        done-callback 收割进 Job，稍后（跨回合）用 task_output 取回。

        池子一律用**并行池**，无视传进来的那个（2026-09-29 真机事故修正）：
        单工具调用走的是串行池，而串行池只有 **1 个 worker** —— 它的语义是"服务必须
        独占顺序执行的单工具调用"，是稀缺通道。一个 30 秒的后台作业若占住它，
        所有单工具调用都得排队 —— 那就把"后台"变成了"堵住前台的石头"。
        """
        pool = self._parallel_pool          # 后台作业只走并行池
        out: List[ToolResult] = []
        for tc in tasks:
            if self._shutdown_event.is_set():
                out.append(ToolResult(tc.id, False, error="编排器已关闭"))
                continue
            self._prepare_cancel_event(tc)   # 显式挂起这条路径不经过 _run_on_pool 的提交循环
            label = JobRegistry.make_label(tc.name, tc.arguments)
            job = self.jobs.register(tool=tc.name, label=label,
                                     sid=self._session_id or "-", tool_call_id=tc.id)
            tc.metadata["job"] = job      # _run_one 靠它挂 logger 包装
            # 告诉工具"你是个跨回合作业" -> 它给自己的 Job Object 设 KILL_ON_JOB_CLOSE：
            # AB 被硬杀（任务管理器结束进程/崩溃）时，内核会替我们把这棵树收掉（P13 兜底）。
            tc.metadata["is_background_job"] = True
            self._bind_interrupter(tc, job)   # 能杀子进程的工具：绑上"尽力中断"
            self._log(f"🛫 {tc.id}: {tc.name} 转后台 -> {job.job_id}（{label}）")
            try:
                future = pool.submit(self._run_one, tc)
            except Exception as e:
                job.finish(error=f"提交后台失败: {type(e).__name__}: {e}")
                out.append(ToolResult(tc.id, False, error=job.error))
                continue
            with self._lock:
                self._active_futures[future] = tc.id
            self._harvest(future, job)
            out.append(ToolResult(
                tc.id, True,
                result=("⏳ 已转后台：作业 %s（%s）—— 它不占用本回合，请继续做别的事；"
                        "结果用 task_output(job_id=%s) 取回。" % (job.job_id, label, job.job_id)),
                metadata={"background_job": job.job_id},
            ))
        return out

    def _harvest(self, future, job: Job) -> None:
        """注册收割回调：worker 跑完即把结果写进作业。

        时序保证（已在 workbench 原型验证）：future 已完成后再注册回调也会触发；
        cancel 之后回调同样触发（结果处 CancelledError）；已结算的作业不被覆盖。
        """
        def _cb(fut, _job=job):
            try:
                res = fut.result()
            except Exception as e:
                _job.finish(error="%s: %s" % (type(e).__name__, e))
            else:
                if getattr(res, "success", False):
                    _job.finish(result=res.result, biz_fail=bool(getattr(res, "biz_fail", False)))
                else:
                    _job.finish(error=getattr(res, "error", None) or "未知失败")
            finally:
                with self._lock:
                    self._active_futures.pop(fut, None)
        future.add_done_callback(_cb)

    # ==================== P2：超时收编 / 看门狗 / 一起停 / 尽力中断 ====================

    def _prepare_cancel_event(self, tc: ToolCall) -> None:
        """工具若支持 `cancel_event` 参数，**提交前**就把事件放进 metadata。

        为什么必须提前：超时收编发生在 worker 已经跑起来之后 —— 那时参数早已绑定，
        再往里塞是塞不进去的。提前放，谁都能拿到（收编时只需把它的 set 绑给作业）。
        """
        try:
            tpl = self.tools_map.get(tc.name)
            func = getattr(tpl, "func", None)
            if func is None or not callable(func):
                return
            if "cancel_event" not in inspect.signature(func).parameters:
                return
            md = tc.metadata if isinstance(tc.metadata, dict) else None
            if md is None:
                return
            if md.get("cancel_event") is None:
                md["cancel_event"] = threading.Event()
        except Exception:
            pass

    def _bind_interrupter(self, tc: ToolCall, job: Job) -> None:
        """把"尽力中断"绑到作业上：能收到取消令牌的工具，中断 = set 那个事件。

        没有令牌（工具不支持）-> interrupter 保持 None：那就**只能丢弃结果**，
        文案必须如实说这一点，不许含糊成"已终止"。
        """
        try:
            ev = (tc.metadata or {}).get("cancel_event")
            if ev is not None:
                job.interrupter = ev.set
        except Exception:
            pass

    def _auto_background(self, tc: ToolCall) -> bool:
        """这次超时是否该转成后台作业（而不是判失败）。判据与显式挂起同一套名单。"""
        if self._shutdown_event.is_set():
            return False                       # 正在一起停：不该再产生新作业
        if tc.name in NO_BACKGROUND_TOOLS:
            return False                       # 交互/等待类：后台没人在旁边
        if (tc.metadata or {}).get("job") is not None:
            return False                       # 已经是作业了，别重复登记
        return True

    def _adopt_background(self, tc: ToolCall, future=None) -> Job:
        """把一次"已经在跑"的调用**收编**成后台作业（登记 + 收割 + 绑中断）。"""
        label = JobRegistry.make_label(tc.name, tc.arguments)
        job = self.jobs.register(tool=tc.name, label=label,
                                 sid=self._session_id or "-", tool_call_id=tc.id)
        tc.metadata["job"] = job
        self._bind_interrupter(tc, job)
        if future is not None:
            self._harvest(future, job)
        self._log(f"🛫 {tc.id}: {tc.name} 超时收编 -> {job.job_id}（{label}）", "WARN")
        return job

    def stop_all_jobs(self, reason: str = "被主人停止",
                      wait: float = JOB_STOP_WAIT) -> Dict[str, int]:
        """一起停：**先发中断信号，再有界等待它落地**，然后结算 + 清空登记册。

        返回值是台账：`{"total": n, "settled": k, "stuck": n-k}`
          · `settled` = 在我们等的时间内**工具真的收手了**（对它来说进程树已被杀）；
          · `stuck`   = 令牌发了但它没在时限内收手（Python 线程杀不掉那一类）。

        ⚠️ 口径（不许含糊）：`cancelled` 只代表**结果被丢弃**。**能杀子进程的工具**
        （execute_shell / execute_python，Windows 上还带 Job Object）会被真正打断；
        纯线程动作杀不掉 —— 台账把这两件事分开报，调用方文案也照这个说。

        ⚠️ **为什么必须等**（2026-09-30 P13 真机分析）：取消令牌是**异步**的 ——
        工具的 poll 循环要到下一拍（约 50ms）才看见令牌、才去杀进程树。而"关机"那条路
        在 `stop_all_jobs` 之后**紧接着 `os._exit(0)`**：不等的话进程一没，
        没来得及杀的子树就成了孤儿。所以这里取一个有界等待：**快，但不撒谎**。
        """
        live = list(self.jobs.list_jobs(include_finished=False))
        total = len(live)
        if not total:
            return {"total": 0, "settled": 0, "stuck": 0}
        for job in live:
            if callable(job.interrupter):
                try:
                    job.interrupter()
                except Exception:
                    pass
        # 先把状态定死（结果一律丢弃）。**必须在等待之前**：
        # 支持取消令牌的工具是"协作式返回"（返回「⏹ 已被取消」这种文本），harvest 会把它
        # 记成 done —— 那不是"作业没被取消"。先 finish 则 harvest 的那次 finish 成为 no-op
        # （已结算的作业不许被改写），台账与状态才是确定的。
        for job in live:
            job.finish(error=reason + "：作业被取消，结果已丢弃", status="cancelled")
        # 等待的是"**工具线程真的收手了**"：用 future.done()，而不是 job.alive
        # （状态刚被我们改成 cancelled，用 job.alive 会立刻"等完"，等了个寂寞）。
        tc_ids = {j.tool_call_id for j in live if j.tool_call_id}
        with self._lock:
            futs = [f for f, tcid in self._active_futures.items() if tcid in tc_ids]
        deadline = time.time() + max(0.0, float(wait))
        while time.time() < deadline and any(not f.done() for f in futs):
            time.sleep(0.05)
        stuck = sum(1 for f in futs if not f.done())
        settled = total - stuck        # 没起线程的（还在排队）也算"收手"——它永远不会再跑
        self.jobs.clear_all()
        self._log("🛑 一起停：%d 个后台作业被取消并清空（%d 个确认已收手%s）"
                  % (total, settled, "，%d 个没在 %.1fs 内收手（纯线程动作杀不掉）"
                     % (stuck, wait) if stuck else ""), "WARN")
        return {"total": total, "settled": settled, "stuck": stuck}

    def _start_watchdog(self) -> None:
        """启动作业寿命看门狗（常驻守护线程，随编排器一起活着）。"""
        if self._watchdog is not None:
            return
        try:
            t = threading.Thread(target=self._watchdog_loop, name="orch-watchdog", daemon=True)
            self._watchdog = t
            t.start()
        except Exception as e:
            self._log(f"⚠️ 看门狗启动失败（作业寿命将不受限）: {e}", "WARN")

    def _watchdog_loop(self) -> None:
        """作业寿命看门狗：超过 JOB_MAX_LIFETIME 仍在跑的作业被强制结算为 expired。

        为什么必须有它：作业占着池里的 worker。寿命无上限 = 僵尸能把管道占死，
        最后连新工具都没地方跑 —— 而"池满"本来是给人看的信号，不该被慢慢吃掉。
        """
        while not self._shutdown_event.wait(JOB_WATCHDOG_INTERVAL):
            try:
                now = time.time()
                for job in self.jobs.list_jobs(include_finished=False):
                    if (now - job.created_at) <= JOB_MAX_LIFETIME:
                        continue
                    self._log(f"⌛ 作业 {job.job_id} 超过寿命上限 {JOB_MAX_LIFETIME}s → 丢弃其结果", "WARN")
                    if callable(job.interrupter):
                        try:
                            job.interrupter()
                        except Exception:
                            pass
                    job.finish(
                        error=("因超寿命被丢弃：已跑超过 %d 秒仍未结束。注意这只是**丢弃结果**，"
                               "它的线程可能仍在跑（Python 线程无法强杀）；若它支持取消令牌，"
                               "已同时请求中断。" % JOB_MAX_LIFETIME),
                        status="expired")
            except Exception:
                continue

    # ==================== 单层执行（常驻池提交） ====================

    def _tool_timeout(self, tc: ToolCall) -> int:
        """本次调用的编排器时限（秒）。

        取值优先级：**工具入参 timeout > 模板声明 > 声明表 > 全局默认**。
        - 入参 timeout：模型在 schema 里显式传的值（execute_shell/python 都有这个参数），
          工具内部自己还会 clamp 到它自己的上限；编排器按它放宽，才不会再出现
          「schema 承诺 120、实际 30 秒被杀」。
        - 模板/声明表：工具**自己**能跑多久（长任务工具如 execute_browser=180、
          rag 冷启动、ask_user 等主人）。
        - 都没有 → 全局默认（快工具 30 秒）。
        前两档额外加 ORCH_GRACE，让工具先到点返回它自己的结果，编排器只兜底。
        """
        own = None
        if isinstance(tc.arguments, dict):
            try:
                own = int(tc.arguments.get("timeout"))
            except (TypeError, ValueError):
                own = None
        if own and own > 0:
            return own + ORCH_GRACE

        template = self.tools_map.get(tc.name)
        declared = getattr(template, "timeout", None) or self.tool_timeouts.get(tc.name)
        if declared:
            try:
                return int(declared) + ORCH_GRACE
            except (TypeError, ValueError):
                pass
        return int(self.default_timeout)

    def _has_side_effects(self, tc: ToolCall) -> bool:
        """该工具重复执行是否可能在外部留下痕迹（非幂等）。

        清单在 agent_tools.NON_IDEMPOTENT_TOOLS（单一真源），这里只查表。
        """
        return tc.name in self.side_effect_tools

    def _timeout_message(self, tc: ToolCall, limit: int) -> str:
        """超时结果的文案：表达「**结果未知**」，而不是「失败」。

        机制原因：`future.cancel()` 对已 start 的任务无效 → 工具线程仍在后台跑到结束，
        也就是说**副作用可能已经发生**（审计 L1-B1「幽灵执行」，实测坐实）。模型若把它
        当普通失败，最自然的反应就是原样重试 —— 于是写盘变重复、发送变两次、
        删除类不可逆动作做两遍。所以有副作用的工具必须显式警告"先核对再动"。
        """
        head = (f"⏰ 超时（{limit} 秒）：**结果未知，不是失败** —— 该工具的线程仍在后台运行，"
                "稍后可能有结果。")
        if self._has_side_effects(tc):
            return (head + " 它**有副作用**（写盘/提交/发送/删除类动作可能已经发生）："
                    "不要原样重试 —— 先核对目标状态；确认没做成再调整参数重新发起"
                    "（原样重发会被拒绝）。")
        return head + " 它只读、无副作用，重试是安全的；也可以稍后自行核对结果。"

    def _run_on_pool(self, pool: ThreadPoolExecutor, tasks: List[ToolCall], timeout: Optional[int] = None) -> List[ToolResult]:
        """
        在指定常驻线程池上执行一组任务（池的 worker 数决定并发度）。

        - serial pool（1 worker）→ 任务顺序执行
        - parallel pool（8 worker）→ 同批任务并行执行
        - **逐任务结算**：每个任务按自己的时限到点（`_tool_timeout`），不再让全批
          共用一个 30 秒 —— 同一层里的长任务（浏览器/冷启动检索）不会拖累短任务，
          短任务也不会因为旁边有个长任务就被人为放宽。
        - 池常驻，不随 execute() 建销；被判定超时的 future 其线程仍在后台跑完
          （`future.cancel()` 对已 start 的任务无效），所以超时结果的文案表达的是
          「结果未知」而不是「失败」 —— 别让模型据此重试同一个副作用。
        """
        if not tasks:
            return []

        # ===== 后台作业分流：声明 background 的调用**不 join**，立即返回占位 =====
        bg_results: List[ToolResult] = []
        bg_tasks = [tc for tc in tasks if (tc.metadata or {}).get("background")]
        if bg_tasks:
            bg_results = self._launch_background(pool, bg_tasks)
            tasks = [tc for tc in tasks if not (tc.metadata or {}).get("background")]
            if not tasks:
                return bg_results

        if self._shutdown_event.is_set():
            return [ToolResult(tc.id, False, error="编排器已关闭") for tc in tasks]

        results: List[ToolResult] = []
        if pool is self._serial_pool:
            pool_label = f"串行管道 ×{self.serial_workers}"
        else:
            pool_label = f"并行管道 ×{self.max_workers}"

        self._log(f"🚀 [{pool_label}] 提交 {len(tasks)} 个任务")

        futures: Dict[Any, ToolCall] = {}
        limits: Dict[Any, int] = {}
        deadlines: Dict[Any, float] = {}
        for tc in tasks:
            limit = int(timeout or self._tool_timeout(tc))
            # 取消令牌必须**提交前**备好：超时收编发生在 worker 已开始跑之后，
            # 那时再往工具里塞参数是塞不进去的（签名早就绑定了）。
            self._prepare_cancel_event(tc)
            future = pool.submit(self._run_one, tc)
            with self._lock:
                self._active_futures[future] = tc.id
            futures[future] = tc
            limits[future] = limit
            deadlines[future] = time.time() + max(1, limit)

        pending: Set[Any] = set(futures.keys())
        while pending:
            now = time.time()
            nxt = min(deadlines[f] for f in pending)
            done, pending = wait(pending, timeout=max(0.0, nxt - now))

            for future in done:
                tc_id = futures[future].id
                try:
                    results.append(future.result())
                except Exception as e:
                    results.append(ToolResult(tc_id, False, error=f"执行异常: {type(e).__name__} - {str(e)}"))
                finally:
                    with self._lock:
                        self._active_futures.pop(future, None)

            # 到点仍未完成：**能转后台的就转后台**，其余照旧判「结果未知」。
            # 为什么转：主人定的口径是"超时兜底自动转后台"—— 一个跑了很久还没回来的调用，
            # 与其把它当失败（模型很可能原样重试 = 同一个副作用做两遍），不如承认它
            # "还在跑"，给它一个作业号，让结果有地方回来。
            # 不转的：交互/等待类（后台没人在旁边，转了毫无意义）与关闭中的编排器。
            now = time.time()
            for future in [f for f in pending if deadlines[f] <= now]:
                tc = futures[future]
                if self._auto_background(tc):
                    job = self._adopt_background(tc, future)
                    results.append(ToolResult(
                        tc.id, True,
                        result=("⏰ 超过本次时限（%d 秒）→ **已自动转入后台**：作业 %s（%s）。\n"
                                "本回合不等它；结果用 task_output(job_id=\"%s\") 取回"
                                "（它跑到完成，或到作业寿命上限为止）。" %
                                (limits[future], job.job_id, job.label, job.job_id)),
                        metadata={"background_job": job.job_id}))
                else:
                    future.cancel()
                    results.append(ToolResult(
                        tc.id, False, error=self._timeout_message(tc, limits[future])))
                pending.discard(future)
                with self._lock:
                    self._active_futures.pop(future, None)

        return bg_results + results

    # ==================== 单个工具执行（核心：注入 logger） ====================

    def _run_one(self, tool_call: ToolCall) -> ToolResult:
        """
        执行单个工具调用：
        1. 以工具模板为蓝本创建独立管道实例
        2. 检测工具函数是否支持 logger 参数，如果支持则自动注入
        3. 执行管道
        """
        if self._shutdown_event.is_set():
            return ToolResult(tool_call.id, False, error="编排器已关闭")

        template = self.tools_map.get(tool_call.name)
        if not template:
            return ToolResult(tool_call.id, False, error=f"未知工具: {tool_call.name}")

        if not callable(template.func):
            return ToolResult(tool_call.id, False, error=f"工具 {tool_call.name} 不可调用")

        # ===== 核心：为工具注入 logger（如果支持） =====
        func_kwargs = tool_call.arguments.copy()
        sig = inspect.signature(template.func)
        # 后台作业：logger 换成"边转发边记账"的包装 —— 同一行进原 logger（行为不变），
        # 也进作业环形缓冲（task_output 的进度来源，不另造上报 API）。
        job = (tool_call.metadata or {}).get("job")
        if "logger" in sig.parameters and self.log_instance is not None:
            # 前台调用**原样**注入（零行为变化：这条链被端到端探针盯着 ——
            # "编排器按签名注入 logger"）；只有后台作业才套包装。
            # 教训：包装哪怕只少一个接口（SessionLogger 还有 span/child），
            # 日志就会静默消失，而且只在真任务上才看得出来。
            if job is not None:
                func_kwargs["logger"] = _JobLogger(self.log_instance, tool_call, job)
            else:
                func_kwargs["logger"] = self.log_instance
        # 取消令牌：工具若支持，就把它收到的事件传进去（中断时它会杀自己的进程树）
        _ev = (tool_call.metadata or {}).get("cancel_event")
        if _ev is not None and "cancel_event" in sig.parameters:
            func_kwargs["cancel_event"] = _ev
        # 后台作业：告诉工具"你是跨回合作业" -> 它给自己的 Job Object 设 KILL_ON_JOB_CLOSE
        # （AB 被硬杀时由内核收掉这棵树；见 agent_tools/win_job.py 与 PLAN §13）。
        # 注意键名用 is_background_job：metadata 里的 "background_job" 存的是**作业号**，别撞车。
        if (tool_call.metadata or {}).get("is_background_job") and \
                "background_job" in sig.parameters:
            func_kwargs["background_job"] = True

        # 创建管道并注入参数
        pipeline = template.create_pipeline(tool_call)
        pipeline.set_kwargs(func_kwargs)

        self._log(f"   🧵 {tool_call.id}: 模板 '{template.name}' → 管道 #{id(pipeline):x} @{threading.current_thread().name}")

        return pipeline.run()



# ---- 当前常驻编排器的模块级引用 ----
# task_* 三个工具（agent_tools/task_jobs.py）靠它拿到作业登记册。由宿主在创建常驻
# 编排器时注册（agent.py::_get_orchestrator），关闭时注销 —— 这样工具不必 import agent
# （避免循环依赖），也不会自己另 new 一个编排器（那会得到一份空登记册）。
_CURRENT_ORCHESTRATOR: Optional["TaskOrchestrator"] = None


def set_current_orchestrator(orch: "TaskOrchestrator") -> None:
    """注册当前常驻编排器（宿主调用）。"""
    global _CURRENT_ORCHESTRATOR
    _CURRENT_ORCHESTRATOR = orch


def get_current_orchestrator() -> Optional["TaskOrchestrator"]:
    """取当前常驻编排器；没有则返回 None（工具据此如实报"编排器未启动"）。"""
    return _CURRENT_ORCHESTRATOR

# ============================================================
# 6. 便捷函数（供 agent.py 调用）
# ============================================================

def orchestrate_tool_calls(
    tool_calls_data: List[Dict[str, Any]],
    tools_map: Dict[str, Callable],
    max_workers: int = MAX_WORKERS,
    default_timeout: int = 30,
    log_enabled: bool = True,
    log_instance=None,
) -> List[Dict[str, Any]]:
    """
    编排工具调用（便捷入口）

    参数:
        tool_calls_data: LLM 返回的 tool_calls 原始数据列表
        tools_map: AVAILABLE_TOOLS 字典（裸函数或 ToolTemplate 均可，内部自动归一化）
        max_workers: 最大并发数
        default_timeout: 默认超时
        log_enabled: 是否开启日志
        log_instance: 日志实例（用于注入到工具）

    返回:
        结果列表，每个元素包含 tool_call_id, content, success
    """
    tool_calls = []
    for tc_data in tool_calls_data:
        func = tc_data.get("function", {})
        tool_calls.append(ToolCall(
            id=tc_data.get("id", ""),
            name=func.get("name", ""),
            arguments=json.loads(func.get("arguments", "{}")),
            depends_on=[],
        ))

    # ---- W1 守卫：本便捷入口自建编排器，会绕过 agent.py 主循环的审批闸门 ----
    # grep 实测当前零调用方；留着不管 = 给未来留一条没人看守的旁路。
    # 确有需要在审批之外批量跑工具时，显式设 AETHER_AUDIT_ALLOW_UNGATED=1。
    _ungated = os.environ.get("AETHER_AUDIT_ALLOW_UNGATED", "").strip().lower()
    if _ungated not in ("1", "true", "yes"):
        raise RuntimeError(
            "orchestrate_tool_calls 会绕过审批闸门（它自建编排器，不经 agent.py 的 gate），"
            "已默认禁用。请改走主循环，或显式设 AETHER_AUDIT_ALLOW_UNGATED=1 承担该风险。")

    orchestrator = TaskOrchestrator(
        max_workers=max_workers,
        default_timeout=default_timeout,
        tools_map=tools_map,
        log_enabled=log_enabled,
        log_instance=log_instance,
    )

    try:
        batch_result = orchestrator.execute(tool_calls)
    finally:
        # 便捷入口是一次性用法：用完即关，避免常驻线程池泄漏
        orchestrator.shutdown()

    output = []
    for res in batch_result.results:
        output.append({
            "tool_call_id": res.tool_call_id,
            "success": res.success,
            "content": str(res.result) if res.success else f"❌ {res.error}",
        })

    return output
