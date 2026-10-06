# -*- coding: utf-8 -*-
"""SSE Hub —— 前端唯一事件出口。

两个来源汇入同一出口：
  1. bridge 转发的回合事件（stage/tool_begin/tool_end/progress/text/done/...）
  2. 网关自身的进程事件（agent_phase 等）
保留环形历史，支持 `?since=<seq>` 回放 —— 浏览器刷新后时间线不丢。
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from typing import Any, Dict, List, Optional, Set


class SSEHub:
    def __init__(self, history: int = 800) -> None:
        self._lock = threading.Lock()
        self._subs: Set[asyncio.Queue] = set()
        self._seq = 0
        self._hist: deque = deque(maxlen=history)

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def replay(self, since: int, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """重放 since 之后的历史。帧上打 replayed 标记并把"此刻状态"的裁量权留给前端 ——
        回放是补时间线用的，不是"现在正在发生什么"的权威来源。缺了这个标记，初次打开
        （since=0，整环回放）时上一回合的 done 会把正挂着的审批卡一起清掉。"""
        with self._lock:
            items = [dict(e, replayed=True) for e in self._hist if e.get("hub_seq", 0) > since]
        if session_id:
            items = [e for e in items if not e.get("session_id") or e["session_id"] == session_id]
        return items

    def publish(self, etype: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """线程安全入口（bridge 泵线程是普通线程，不能 await）。"""
        evt: Dict[str, Any] = {"type": etype, "ts": _now_iso()}
        evt.update(payload or {})
        with self._lock:
            self._seq += 1
            evt["hub_seq"] = self._seq
            self._hist.append(evt)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except Exception:
                pass  # 慢订阅者丢帧，绝不回压 agent
        return evt

    def history_snapshot(self, n: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._hist)[-n:]


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="milliseconds")


def sse_frame(evt: Dict[str, Any]) -> str:
    """编码为 SSE 帧（event 名 = type，前端可按类型 addEventListener）。"""
    data = json.dumps(evt, ensure_ascii=False)
    return f"event: {evt.get('type', 'message')}\ndata: {data}\n\n"
