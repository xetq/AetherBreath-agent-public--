# -*- coding: utf-8 -*-
"""SSE 事件类型枚举（backend 与 bridge 共享的唯一约定来源）。

扩展位：新增事件类型只需在此追加常量，前端 types.ts 的 WsEventType 同步。
"""


class Ev:
    AGENT_PHASE = "agent_phase"      # 进程级状态迁移（OFF/STARTING/ON/STOPPING）
    TURN_PHASE = "turn_phase"        # 回合级状态迁移（IDLE/THINKING/TOOL_RUNNING/...）
    STAGE = "stage"                  # 回合阶段粗粒度提示
    TOOL_BEGIN = "tool_begin"        # 工具开始执行
    TOOL_END = "tool_end"            # 工具执行结束（含结果摘要/耗时）
    PROGRESS = "progress"            # agent 自身 print 出来的中期进度/文本
    REASONING = "reasoning"          # 模型这一轮的思考原文（bridge 读会话文件后发出，卡片内可展开）
    TEXT = "text"                    # 最终回复文本块
    ASK_REQUEST = "ask_request"  # agent 向主人提问（ask_user）
    ASK_RESOLVED = "ask_resolved"  # 挂着的提问卡被整批作废（回合终止等）
    MID_TURN = "mid_turn"            # 中期交互：主人趁回合运行追加的「用户交代」
    DONE = "done"                    # 回合结束
    ERROR = "error"                  # 回合异常
    HEARTBEAT = "heartbeat"          # 保活


ALL_EVENT_TYPES = {
    Ev.AGENT_PHASE, Ev.TURN_PHASE, Ev.STAGE, Ev.TOOL_BEGIN, Ev.TOOL_END,
    Ev.PROGRESS, Ev.TEXT, Ev.REASONING, Ev.ASK_REQUEST, Ev.ASK_RESOLVED,
    Ev.MID_TURN, Ev.DONE, Ev.ERROR, Ev.HEARTBEAT,
}

# 进程级阶段
PHASE_OFF = "OFF"
PHASE_STARTING = "STARTING"
PHASE_ON = "ON"
PHASE_STOPPING = "STOPPING"

# 回合级阶段
TURN_IDLE = "IDLE"
TURN_THINKING = "THINKING"
TURN_TOOL_RUNNING = "TOOL_RUNNING"
TURN_RESPONDING = "RESPONDING"
TURN_ASK_WAIT = "ASK_WAIT"
TURN_AUDIT_WAIT = "AUDIT_WAIT"
TURN_INTERRUPTED = "INTERRUPTED"
TURN_ERROR = "ERROR"
