# -*- coding: utf-8 -*-
"""后台作业三工具：task_list / task_output / task_kill。

它们读的是**常驻编排器**的作业登记册（`TaskOrchestrator.jobs`）。编排器由宿主在创建时
用 `task_orchestrator.set_current_orchestrator()` 注册进来 —— 本模块**不 import agent**
（避免循环依赖），也不自己 new 编排器（那会得到一份空登记册，看起来像"没有作业"）。
"""

try:
    import task_orchestrator as _T
except Exception:                      # 找不到编排器模块也要能 import 成功（如实降级）
    _T = None

_MAX_WAIT = 120                        # wait_seconds 上限（别把回合钉死太久）


def _jobs():
    """取作业登记册；没有编排器则 None。"""
    if _T is None:
        return None
    try:
        orch = _T.get_current_orchestrator()
    except Exception:
        return None
    return getattr(orch, "jobs", None)


def _clamp_wait(v) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = float(getattr(_T, "JOB_WAIT_DEFAULT", 30) if _T else 30)
    return max(0.0, min(n, float(_MAX_WAIT)))


def task_list(logger=None) -> str:
    """列出**编排器当前的运行状况**

    刻意**不是**一张"历史表"（主人 2026-09-30 定）：作业交付完就出册、内容也已写进会话，
    留着一张越来越长的表既占内存又没意义。所以这里只有"现在还在册"的作业 ——
    **交付过的不会出现**（要回看交付内容，请在会话历史里找那条交付消息）。
    """
    reg = _jobs()
    if reg is None:
        return "ℹ️ 当前没有可用的任务编排器（后台作业机制未启用或编排器尚未创建）。"
    try:
        jobs = reg.live_summary()
    except Exception as e:
        return "❌ 读取作业登记册失败：%s: %s" % (type(e).__name__, e)
    if not jobs:
        return ("ℹ️ 编排器当前空闲：没有在跑或待交付的后台作业。\n"
                "  · 已结算的作业会自动把**全文**交付到它的归属会话，交付完就从登记册出册 —— "
                "要回看交付内容，"
                "请在会话历史里找那条【后台作业交付】消息。\n"
                "  · 作业是内存态：进程重启会清空登记册，"
                "重启前挂的作业与结果不再回来。")
    lines = ["后台作业（编排器当前 %d 个）：" % len(jobs)]
    lines.extend("  " + j.one_line() for j in jobs)
    lines.append("→ 看某个作业的运行情况：task_output(job_id=...)；终止：task_kill(job_id=...)。")
    lines.append("  · 已结算的会自动交付全文给归属会话并**出册**，所以这里只有「在跑」"
                 "与「待交付」。")
    return chr(10).join(lines)


def task_output(job_id: str = "", wait_seconds: int = 30, max_lines: int = 40,
                logger=None) -> str:
    """看一个后台作业的**运行情况**（进度）。

    - **运行中** → 状态 + 已跑时长 + 最近若干行日志（本工具的主用途：长作业想看看它现在怎么样）。
    - **已经交付过** → 它已从登记册出册，这里如实说"没有这个作业"并告诉你去哪儿找内容。

    所以**没有"取回"这个概念**：交付是自动的。
    """
    reg = _jobs()
    if reg is None:
        return "ℹ️ 当前没有可用的任务编排器（拿不到后台作业）。"
    jid = str(job_id or "").strip()
    if not jid:
        return "❌ 必须给 job_id（用 task_list 看现在在跑的作业号）。"
    job = reg.get(jid)
    if job is None:
        return ("❌ 没有这个后台作业：%s\n"
                "  · 它可能**已经结算并交付进会话历史**了（交付即出册，登记册里不再留）；"
                "也可能进程重启过（登记册是内存态）。\n"
                "  · 想看现在在跑的，用 task_list。" % jid)
    secs = _clamp_wait(wait_seconds)
    if secs > 0:
        job = reg.wait(jid, secs) or job
    try:
        return job.progress_text(max_lines=int(max_lines or 40))
    except Exception as e:
        return "❌ 渲染作业信息失败：%s: %s" % (type(e).__name__, e)


def task_kill(job_id: str = "", reason: str = "", logger=None) -> str:
    """终止后台作业（标记取消 + 丢弃结果；能真杀子进程的会真的杀）。

    ⚠️ 口径：**线程杀不掉，进程树能杀干净**。这个动作保证的是"结果被丢弃、作业从登记册
    消失"；对 shell/python 这类起子进程的工具，Windows 上用 Job Object 把**整棵树**
    （含被重新挂靠的子进程）一起杀掉，POSIX 用进程组。返回文案如实区分这两件事。
    """
    reg = _jobs()
    if reg is None:
        return "ℹ️ 当前没有可用的任务编排器（没有可终止的作业）。"
    jid = str(job_id or "").strip()
    if not jid:
        return "❌ 必须给 job_id（用 task_list 看现有作业号）。"
    job = reg.get(jid)
    if job is None:
        return "❌ 没有这个后台作业：%s（用 task_list 看现有作业号）" % jid
    why = ("主人/模型要求终止：" + reason) if reason else "被 task_kill 终止"
    if not job.alive:
        already = "（它本来就已结算：%s）" % job.status
        reg.drop(jid)
        return "ℹ️ 作业 %s 已从登记册移除%s。" % (jid, already)
    killed = False
    if callable(job.interrupter):
        try:
            job.interrupter()
            killed = True
        except Exception:
            killed = False
    job.finish(error=why, status="cancelled")
    reg.drop(jid)
    tail = ("已同时请求中断它的子进程（整棵进程树）。" if killed else
            "它是个**没有子进程可杀**的工具（例如纯 Python 线程）—— 结果会被丢弃，"
            "但那件事可能仍在后台跑完。若它会产生副作用（写盘/提交/删除），"
            "请先核对目标状态再决定是否重做。")
    return "⏹ 作业 %s 已标记取消并从登记册移除。%s" % (jid, tail)


task_list_schema = {
    "type": "function",
    "function": {
        "name": "task_list",
        "description": ("看**编排器当前的运行状况**：哪些后台作业正在跑、哪些已结算还没交付。"
                        "**这不是历史表** —— 作业交付完就出册（内容已写进会话历史），"
                        "开工前先看一眼有没有在跑的旧作业。"),
        "parameters": {"type": "object", "properties": {}},
    },
}

task_output_schema = {
    "type": "function",
    "function": {
        "name": "task_output",
        "description": ("看一个后台作业的**运行情况**：运行中 -> 状态、已跑时长与"
                        "最近若干行日志。"
                        "**交付过的作业**查它会说「没有这个作业」——"
                        "要看交付内容请到会话历史里找那条【后台作业交付】消息。"
                        "想知道一个跑了很久的作业现在怎么样，就用它。"),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "作业号，如 \"j3\"。"},
                "wait_seconds": {
                    "type": "integer",
                    "description": "最多阻塞等待秒数（默认 30，上限 %d）。0 = 只看当前快照。" % _MAX_WAIT,
                    "default": 30, "minimum": 0, "maximum": _MAX_WAIT,
                },
                "max_lines": {
                    "type": "integer",
                    "description": "运行中时返回最近多少行日志（默认 40）。",
                    "default": 40, "minimum": 0, "maximum": 200,
                },
            },
            "required": ["job_id"],
        },
    },
}



                        #这是 task_kill_schema 的description原版描述_FromUser
                        #"终止一个后台作业：标记取消、丢弃它的结果、从登记册移除。"
                        # "口径：**线程杀不掉，进程树能杀干净** —— 对 shell/python 这类起子进程的"
                        # "工具，Windows 上用 Job Object 把整棵树（含被重新挂靠的子进程）一起杀掉，"
                        # "POSIX 用进程组；但那条工具如果是纯线程动作，「结果不再回来」成立、"
                        # "「对端已停」不成立。所以对一个有副作用的作业这么做之后，先核对目标状态。"





task_kill_schema = {
    "type": "function",
    "function": {
        "name": "task_kill",
        "description": ("终止一个后台作业：标记取消、丢弃它的结果、从登记册移除。"
                        "对一个有副作用的作业这么做之后，先核对目标状态。"),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要终止的作业号，如 \"j3\"。"},
                "reason": {"type": "string", "description": "终止原因（记录用，可空）。"},
            },
            "required": ["job_id"],
        },
    },
}


# ---- 统一注入 background 保留参数 ----
_BG_DESC = ("true = 本调用不阻塞当前回合：交给后台作业继续跑，结果自动返回。"
            "（长任务用；默认 false）。")


def inject_background_params(schemas, only_names=None) -> int:
    """给工具 schema 统一注入 `background` 保留参数，返回注入个数。

    为什么在这里统一注入、而不去改 16 个工具文件：background 是**编排器层的保留参数**
    （调用前会被摘掉，工具函数签名里根本没有它），它属于机制，不属于某个工具的能力。
    交互/等待类工具跳过（名单取自编排器的 NO_BACKGROUND_TOOLS —— 唯一真源）。
    拿不到编排器模块时返回 0（**不注入**）：保守优先，宁可不给这个能力，
    也不给出一个"会在后台卡死"的入口。
    """
    if _T is None:
        return 0
    no_bg = set(getattr(_T, "NO_BACKGROUND_TOOLS", ()) or ())
    n = 0
    for s in schemas or []:
        fn = (s or {}).get("function") or {}
        name = fn.get("name")
        if not name or name in no_bg:
            continue
        if only_names is not None and name not in only_names:
            continue
        props = fn.setdefault("parameters", {}).setdefault("properties", {})
        if "background" in props:
            continue
        props["background"] = {"type": "boolean", "description": _BG_DESC, "default": False}
        n += 1
    return n
