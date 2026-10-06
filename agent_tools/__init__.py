# agent_tools/__init__.py
from typing import Any, Dict                    # 下面 station 自检报告的注解要用（别删）

from .file_read import read_file, read_file_schema
from .calculator import calculator, calculator_schema
from .search import search, search_schema
from .web_reader import fetch_url, fetch_url_schema
from .web_extract import web_extract, web_extract_schema
from .rag import rag_query, rag_query_schema
from .execute_python import execute_python, execute_python_schema
from .execute_shell import execute_shell, execute_shell_schema
from .execute_browser import execute_browser, execute_browser_schema
from .create_tool import create_tool, create_tool_schema
from .skillhub_download import skillhub_install, skillhub_install_schema

from .time_weather import time_weather, time_weather_schema
from .restore_context import restore_context, restore_context_schema
# 自维护：AB 自己集成/管理 MCP server（建 station 文件夹、固定版本、连通性测试）
# ⚠️ 小坑（写给将来的人）：这条 import 会让**包的属性名** `agent_tools.mcp_manage`
#    指向这个**函数**，而不是同名子模块 —— 模块名与导出函数同名就会这样。
#    要拿模块本体请用 importlib.import_module("agent_tools.mcp_manage")（测试里就是这么做的）。
from .mcp_manage import mcp_manage, mcp_manage_schema
# 自维护：AB 自己管理**集成包**（agent_integration_packs/，自集成系统；一个包 = 一个文件夹）
from .pack_manage import pack_manage, pack_manage_schema
# gateway：MCP 的**两个元工具**（v2 —— MCP 工具不再进常驻工具表，见 docs/MCP设计.md v2 §五）。
# 每个 MCP 工具都注册成一等工具是 v1 的做法；一个 station 可能有上百个工具（官方 GitHub MCP
# = 112 个），全量常驻上下文是纯浪费。现在改为：注册表注入（只报"有什么"）+ 按需 mcp_search 取 schema。
from .mcp_gateway import mcp_search, mcp_call, mcp_search_schema, mcp_call_schema
# 后台作业三工具：读常驻编排器的作业登记册（跨回合任务的查看 / 取回 / 终止）
from .task_jobs import (task_list, task_output, task_kill,
                        task_list_schema, task_output_schema, task_kill_schema)


# ========== 2. 自动构建注册表（给 agent.py 直接用） ==========
from .vision_read import vision_read, vision_read_schema
AVAILABLE_TOOLS = {
    "read_file": read_file,
    "calculator": calculator,
    "search": search,
    "fetch_url": fetch_url,
    "web_extract": web_extract,
    "rag_query": rag_query,
    "execute_python": execute_python,
    "execute_shell": execute_shell,
    "execute_browser": execute_browser,
    "create_tool": create_tool,
    "skillhub_install": skillhub_install,
    "time_weather": time_weather,
    "restore_context": restore_context,
    "mcp_manage": mcp_manage,
    "pack_manage": pack_manage,
    "vision_read": vision_read,
    # 后台作业三工具（跨回合任务）
    "task_list": task_list,
    "task_output": task_output,
    "task_kill": task_kill,
}


# ========== 2.1 per-tool 超时声明（**单一真源**） ==========
# 编排器按这张表给每个工具算「多久算超时」（消费方：task_orchestrator._tool_timeout）。
# 优先级：工具入参 timeout（模型显式传的）> 这张表 > 编排器全局默认（30s）。
# 只列**确实需要比 30 秒更长**的工具；快工具（read_file/calculator/restore_context/
# mcp_search）不写，自然落回默认，省得维护一张全量表。
# 声明值 = 工具自己愿意跑的上限；编排器会再加 ORCH_GRACE(10s) 缓冲，让工具**先**到点、
# 返回它自己的结果（强杀进程树 / 关等待窗口），编排器只兜底 —— 反过来就会出现
# 「模型收到失败、工具还在跑」的幽灵执行。
# ⚠️ 这张表必须是**运行时可变**的同一个 dict 对象：WebUI bridge 会在运行期往里面
#    追加它注入的工具（ask_user），编排器持有引用、不持有快照。
TOOL_TIMEOUTS = {
    # 网络 IO：单请求 10~20s，多请求/多页要留量
    "search": 60,
    "fetch_url": 60,
    "time_weather": 60,
    "web_extract": 90,
    # 重活：向量库冷启动 / 下载写盘 / 起进程
    "rag_query": 90,
    "skillhub_install": 90,
    "create_tool": 60,
    "mcp_manage": 120,
    "mcp_call": 120,
    # 子进程执行：工具内部上限就是 120（各自的 MAX_TIMEOUT），编排器只在它彻底失控时兜底
    "execute_shell": 120,
    "execute_python": 120,
    # 浏览器：它的 timeout 是**单步**超时（默认 180），多步任务的整批时间要留足
    "execute_browser": 300,
    # 视觉：多模态请求要送图，OCR 首次加载引擎也要几秒
    "vision_read": 120,
    # 后台作业：等待上限 120s + 收尾余量（编排器会再加 ORCH_GRACE）
    "task_output": 150,
}


# ========== 2.2 工具副作用声明（非幂等清单） ==========
# 判据：**重复执行一次会不会在外部留下痕迹或造成不可逆后果**（不是"能不能重复跑"）。
# 消费方：编排器判超时后按此决定文案强度（有副作用的必须警告"别原样重试"）；
#        agent 层据此拒绝"曾被判超时→又原样重发"的调用（幽灵执行的唯一止损点）。
# 只列**有副作用**的；只读工具（read_file/calculator/search/rag_query/fetch_url/
# web_extract/time_weather/restore_context/mcp_search）不列，重试它们安全。
# ⚠️ 与 TOOL_TIMEOUTS 一样必须是**运行时可变**的同一个对象：WebUI bridge 会把注入的
#    ask_user 加进来（重复提问 = 重复打扰主人，属有副作用）。
NON_IDEMPOTENT_TOOLS = {
    "execute_shell",       # 能改天改地：写文件、装包、提交、删除
    "execute_python",      # 同上
    "create_tool",         # 写工具源文件（覆盖）
    "skillhub_install",    # 下载并写盘
    "mcp_manage",          # 改 MCP 注册表 / 起停进程
    "pack_manage",         # 建/移除集成包文件夹、写集成包注册表
    "mcp_call",            # MCP 工具行为未知，按最保守处理
    "execute_browser",     # 在真实网页上点击/提交
}


def _mcp_gate_enabled() -> bool:
    """MCP 总闸（`config.yaml` 的 `mcp.enabled`）。口径：关掉 = 连两个元工具都不给。

    拿不到配置就默认**开**：这两个工具本身不做危险事（一个读文件夹，一个走 mcp_client 的
    审批+日志链路），不注册它们只会让模型 "没有 MCP 能力"，而不是安全增益。
    """
    try:
        import mcp_station                       # noqa: PLC0415（运行期扁平导入）
        return bool(mcp_station.mcp_enabled())
    except Exception:
        return True


MCP_GATE_ON = _mcp_gate_enabled()
if MCP_GATE_ON:
    AVAILABLE_TOOLS["mcp_search"] = mcp_search
    AVAILABLE_TOOLS["mcp_call"] = mcp_call

TOOLS_SCHEMA = [
    read_file_schema,
    calculator_schema,
    search_schema,
    fetch_url_schema,
    web_extract_schema,
    rag_query_schema,
    execute_python_schema,
    execute_shell_schema,
    execute_browser_schema,
    create_tool_schema,
    skillhub_install_schema,
    time_weather_schema,
    restore_context_schema,
    mcp_manage_schema,
    pack_manage_schema,
    vision_read_schema,
]
if MCP_GATE_ON:
    TOOLS_SCHEMA += [mcp_search_schema, mcp_call_schema]

TOOLS_SCHEMA += [task_list_schema, task_output_schema, task_kill_schema]

# ===== background 保留参数：统一注入 =====
# 它是**编排器层**的保留参数：调用前会被摘掉，工具函数签名里根本没有它。
# 交互/等待类工具不注入（名单来自编排器的 NO_BACKGROUND_TOOLS，同一真源）。
# 拿不到编排器模块 -> 不注入（保守：宁可不给这个能力，也不给一个会在后台卡死的入口）。
try:
    from .task_jobs import inject_background_params as _inject_bg
    _BG_INJECTED = _inject_bg(TOOLS_SCHEMA)
except Exception:
    _BG_INJECTED = 0

# ========== 3. MCP：**不再**把每个 MCP 工具注册成一等工具（v2）==========
# v1 在这里把 registry 里每个 server 的每个工具注册成 `mcp__<server>__<tool>`；v2 改成
# **两个元工具**（上面的 mcp_search / mcp_call）+ 注册表注入（docs/MCP设计.md v2 §五）。
# 为什么改：一个 station 可能上百个工具（官方 GitHub MCP = 112 个），全量常驻工具表 = 每轮
# 白吃几万 token；而且新增 station 在 v1 里要重启 agent（import 期注册的硬约束）。
# 这段只做一次**只读体检**（station 目录有几个、有没有坏 station）——不起进程、绝不抛，
# 因为它跑在 agent 的 import 路径上（这里炸掉 = agent 起不来）。
def _mcp_station_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {"mode": "station", "enabled": False, "stations": 0,
                              "servers": 0, "registered": 0, "problems": [], "errors": []}
    try:
        import mcp_station                                        # noqa: PLC0415
        found, issues = mcp_station.scan_stations()
        report["enabled"] = bool(mcp_station.mcp_enabled())
        report["servers"] = len(found)
        report["stations"] = len([s for s in found if s.enabled and s.usable])
        report["problems"] = list(issues)
        report["errors"] = list(issues)        # 老字段名保留：谁读它都能看到"哪里不对"
    except Exception as e:
        report["errors"] = ["MCP station 自检失败：%s: %s" % (type(e).__name__, e)]
    return report


MCP_REGISTRATION = _mcp_station_report()

# ========== 4.（已删）运行期热注册 ==========
# v1 这里曾有一整套"运行期热注册"：把新集成的 MCP 工具原地写进 AVAILABLE_TOOLS / TOOLS_SCHEMA、
# 再灌进常驻编排器、并让宿主（bridge）补上界面事件包装 —— 为的是"装完就能用，不必重启"。
# v2 之后 MCP 工具**不再**进常驻工具表（常驻的只有 mcp_search / mcp_call 两个元工具），
# "装完就能用"改由**每次实时读 station 文件夹**天然拿到，不需要往工具表里写任何东西 ——
# 于是这套机制成了孤儿代码：把 MCP 工具编译成函数的那一整个模块、编排器的运行期注册方法、
# bridge 的 late-wrap 钩子，都在 2026-09-15 一并删除。设计依据：docs/MCP设计.md v2 §五。
# 别再顺手加回来：让上百个 station 工具重新常驻 = 每轮白烧几万 token，正是改 v2 的直接原因。

# 暴露所有工具和 Schema 供外部导入
__all__ = [
    "read_file", "read_file_schema",
    "calculator", "calculator_schema",
    "search", "search_schema",
    "fetch_url", "fetch_url_schema",
    "web_extract", "web_extract_schema",
    "rag_query", "rag_query_schema",
    "execute_python", "execute_python_schema",
    "execute_shell", "execute_shell_schema",
    "execute_browser", "execute_browser_schema",
    "create_tool","create_tool_schema",
    "skillhub_install","skillhub_install_schema",
    "time_weather","time_weather_schema",
    "restore_context","restore_context_schema",
    "pack_manage","pack_manage_schema",
    # 审计 B19：以下 6 个名字此前漏在 __all__ 之外（`from agent_tools import *`
    # 拿不到它们，而它们与 pack_manage 是同一批自维护工具）。
    "mcp_manage", "mcp_manage_schema",
    "mcp_search", "mcp_search_schema",
    "mcp_call", "mcp_call_schema",
    "MCP_REGISTRATION",
    "TOOL_TIMEOUTS",
    "NON_IDEMPOTENT_TOOLS",
    "vision_read",
]
