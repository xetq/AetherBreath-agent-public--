# -*- coding: utf-8 -*-
"""ask_user —— ask 审批工具（WebUI 专属，运行时注入）。

⚠️ 设计红线：本文件不放在 agent_tools/ 下，也不修改 agent_tools/__init__.py。
由 bridge.py 在自己的进程内把它注册进 agent_tools.AVAILABLE_TOOLS /
TOOLS_SCHEMA（可变对象引用，agent 模块可见同一对象）。因此：
  - CLI 模式（python agent/agent.py）完全看不到本工具，行为零变化
  - WebUI 模式（bridge 子进程）额外拥有本工具
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ask 通道：由 bridge 注入。签名 (question, options, kind) -> str
_CHANNEL = None


def set_channel(fn) -> None:
    """bridge 启动时注入真实提问实现。"""
    global _CHANNEL
    _CHANNEL = fn


def ask_user(
    question: str,
    options: Optional[List[str]] = None,
    type: str = "choice",
    timeout: Optional[int] = None,
) -> str:
    """向主人提问并阻塞等待答复。

    无通道时（例如被 CLI/测试直接调用）返回明确错误，不静默吞掉。
    """
    if not question or not str(question).strip():
        return "❌ ask_user 需要非空 question"
    kind = (type or "choice").strip().lower()
    if kind not in ("choice", "multi", "freeform"):
        kind = "choice"
    opts = [str(o) for o in (options or []) if str(o).strip()][:20]
    if kind in ("choice", "multi") and not opts:
        kind = "freeform"

    if _CHANNEL is None:
        return "❌ ask_user 当前无提问通道（非 WebUI 环境），请直接给出结论或说明假设。"
    try:
        return str(_CHANNEL(str(question).strip(), opts, kind, timeout))
    except Exception as e:  # noqa: BLE001
        # 注意：本函数参数名 type 会遮蔽内置 type()，绝不可写 type(e).__name__
        return f"❌ 提问失败: {e.__class__.__name__}: {e}"


ask_user_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "向主人提问并等待答复（ask 审批机制）。当你不确定关键决策、"
            "需要人工确认或需要在多个方案间选择时使用。会暂停当前回合直到主人回答。"
            "彼此独立的问题可以**在同一条消息里并行发出多个**（会同时展示给主人，"
            "他一次答完、你再一起往下做），比一个个问省得多；"
            "options 提供 2-6 个候选项更省事。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问主人的问题，简洁明确",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "候选项（choice/multi 模式必填，最多 20 个）",
                },
                "type": {
                    "type": "string",
                    "enum": ["choice", "multi", "freeform"],
                    "description": "choice=单选, multi=多选, freeform=自由文本",
                    "default": "choice",
                },
            },
            "required": ["question"],
        },
    },
}
