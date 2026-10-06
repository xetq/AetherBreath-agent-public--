# -*- coding: utf-8 -*-
"""两层状态机：进程级（AB bridge 生死）+ 回合级（当前在忙什么）。

状态机只持有状态，不推送事件（推送由 AgentManager 负责）。
非法迁移不抛错，记录后强制落位，避免 UI 卡死在半路。
"""
from __future__ import annotations

import threading
from typing import Optional

from events import (
    PHASE_OFF, PHASE_STARTING, PHASE_ON, PHASE_STOPPING,
    TURN_IDLE, TURN_THINKING, TURN_TOOL_RUNNING, TURN_RESPONDING,
    TURN_ASK_WAIT, TURN_AUDIT_WAIT, TURN_INTERRUPTED, TURN_ERROR,
)

PROCESS_TRANSITIONS = {
    PHASE_OFF: {PHASE_STARTING},
    PHASE_STARTING: {PHASE_ON, PHASE_OFF, PHASE_STOPPING},
    PHASE_ON: {PHASE_STOPPING, PHASE_OFF},
    PHASE_STOPPING: {PHASE_OFF, PHASE_ON},
}

TURN_STATES = {
    TURN_IDLE, TURN_THINKING, TURN_TOOL_RUNNING, TURN_RESPONDING,
    TURN_ASK_WAIT, TURN_AUDIT_WAIT, TURN_INTERRUPTED, TURN_ERROR,
}
TURN_BUSY = {TURN_THINKING, TURN_TOOL_RUNNING, TURN_RESPONDING,
           TURN_ASK_WAIT, TURN_AUDIT_WAIT}


class StateMachine:
    """线程安全的两层状态持有者。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._phase = PHASE_OFF
        self._turn = TURN_IDLE
        self._pid: Optional[int] = None
        self._bridge_url: Optional[str] = None
        self._session_id: Optional[str] = None
        self._run_id: Optional[str] = None
        self._last_error: Optional[str] = None
        self._health_at: Optional[str] = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "turn_phase": self._turn,
                "pid": self._pid,
                "bridge_url": self._bridge_url,
                "session_id": self._session_id,
                "run_id": self._run_id,
                "last_error": self._last_error,
                "health_at": self._health_at,
                "can_send": self._phase == PHASE_ON and self._turn
                in (TURN_IDLE, TURN_INTERRUPTED, TURN_ERROR),
                "busy": self._phase == PHASE_ON and self._turn in TURN_BUSY,
            }

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def turn_phase(self) -> str:
        with self._lock:
            return self._turn

    def set_phase(self, new: str) -> bool:
        with self._lock:
            allowed = PROCESS_TRANSITIONS.get(self._phase, set())
            legal = new in allowed
            self._phase = new
            if new == PHASE_OFF:
                self._turn = TURN_IDLE
                self._pid = None
                self._bridge_url = None
                self._run_id = None
            return legal

    def set_turn(self, new: str) -> None:
        with self._lock:
            self._turn = new if new in TURN_STATES else TURN_IDLE

    def set_process_info(self, *, pid=None, bridge_url=None, health_at=None) -> None:
        with self._lock:
            if pid is not None:
                self._pid = pid
            if bridge_url is not None:
                self._bridge_url = bridge_url
            if health_at is not None:
                self._health_at = health_at

    def set_session(self, session_id: Optional[str]) -> None:
        with self._lock:
            self._session_id = session_id

    def set_run(self, run_id: Optional[str]) -> None:
        with self._lock:
            self._run_id = run_id

    def set_error(self, msg: Optional[str]) -> None:
        with self._lock:
            self._last_error = msg
