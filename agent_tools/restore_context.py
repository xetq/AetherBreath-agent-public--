# -*- coding: utf-8 -*-
"""restore_context —— 按需取回被上下文管理器折叠的原始消息。

设计：agent_workspace/上下文管理器/DESIGN.md §11

为什么需要它：上下文管理器把「较早轮次的工具参数 / 思考过程 / 工具结果」折叠成
占位符发给模型；模型若需要那段的**精确原文**（例如原始命令、原始返回），用本工具取回。

数据源由 agent.py 在运行时注入（`set_provider`）：视图层下**原文始终在内存里**
（working_memory 一字未改），所以取回不依赖任何归档文件，也不会失败于"归档缺失"。

只读、不写盘、不触网；未命中任何审批规则 → 不弹窗（approval.inspect_one 默认 PASS）。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

_PROVIDER: Optional[Callable[..., Optional[str]]] = None


def set_provider(fn: Optional[Callable[..., Optional[str]]]) -> None:
    """agent.py 注入取数实现：fn(round_no=..., tool_call_id=..., max_chars=...) -> str|None"""
    global _PROVIDER
    _PROVIDER = fn


def restore_context(round_no: Optional[int] = None, tool_call_id: Optional[str] = None,
                    max_chars: int = 20000) -> str:
    """取回被折叠的原始消息（轮号从 1 开始）。"""
    if round_no is None and not tool_call_id:
        return "❌ 需要给出 round_no（第几轮）或 tool_call_id（哪个工具调用）之一"
    if _PROVIDER is None:
        return "❌ restore_context 当前无数据源（未在 agent 会话中运行）"
    try:
        limit = int(max_chars or 20000)
    except (TypeError, ValueError):
        limit = 20000
    try:
        out = _PROVIDER(round_no=round_no, tool_call_id=tool_call_id, max_chars=limit)
    except Exception as e:                      # 取回失败绝不能把回合搞崩
        return f"❌ 取回失败: {e.__class__.__name__}: {e}"
    if out is None:
        return "❌ 未找到对应原文：请核对 round_no（从 1 开始）或 tool_call_id 是否正确"
    return out


restore_context_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "restore_context",
        "description": (
            "取回被上下文折叠（省略）的早期原始消息。"
            "当历史里出现「[已折叠 …]」「[进度汇报已折叠 …]」或工具参数显示为 "
            "_cleared 时，说明那段原文没有发给你；若你必须看到**精确原文**"
            "（原始命令、原始返回内容）才能继续，就调用本工具。"
            "round_no 指定第几轮（从 1 开始），tool_call_id 指定某个工具调用。"
            "不需要精确原文时不要调用——折叠内容已由占位符说明。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "round_no": {
                    "type": "integer",
                    "description": "要取回的轮次编号（从 1 开始；1 = 第一条用户消息那一轮）",
                },
                "tool_call_id": {
                    "type": "string",
                    "description": "要取回的工具调用 id（返回其原始参数或原始结果）",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "返回内容上限字符数（默认 20000，超出会截断）",
                },
            },
            "required": [],
        },
    },
}
