# -*- coding: utf-8 -*-
"""WebUI 审批通道 —— agent/approval.py 的 ApprovalPort 宿主实现。

只负责"送达 + 等待 + 回投"。判定与批次规则全在 agent/approval.py，
所以 CLI 与 WebUI 同源，不会出现一处严一处松。

交互形态（2026-09-10 与主人定稿）
--------------------------------
  · 单条待审批  -> 一张 approval_request 卡
  · 多条待审批  -> **一张合并卡** approval_batch：列出全部条目、可勾选、
                    勾中的批准、未勾的即拒绝，批准范围由底部三选一决定。
  两种形态对本引擎都只表现为一件事：request_many(reqs) -> [Decision, ...]。
  因此批次原子性、原因文本、作用域写入全在 approval.py 里，这里换个皮而已。

三条不可退让的规则：
  1. 超时/无通道/异常都带明确 how —— "用户拒绝"与"用户没看到"是两回事；
  2. 没有通道时绝不冒充"用户拒绝"（返回 no_channel）；
  3. 锁内绝不 emit（emit 走网络与队列，持锁 emit 曾把整个 bridge 毒死过）。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

_EMIT: Optional[Callable[[str, Dict[str, Any]], None]] = None
# 一个挂起项 = 一次交互（单条或整批），批内各 ask_id 共用同一个 Event
_PENDING: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
# scope -> 引擎认识的选项键
_SCOPE_KEY = {"once": "A", "session": "B", "persistent": "D"}


def set_emit(fn: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
    global _EMIT
    _EMIT = fn


def _now() -> float:
    return round(time.time(), 3)


class WebUIPort:
    """approval.ApprovalPort 实现。"""

    name = "webui"

    def request(self, req: Dict[str, Any]) -> Any:
        return self.request_many([req])[0]

    def request_many(self, reqs: List[Dict[str, Any]]) -> List[Any]:
        import approval
        D = approval.Decision
        emit = _EMIT
        if emit is None or not reqs:
            return [D("E", "no_channel") for _ in reqs]
        try:
            timeout = max(int(reqs[0].get("timeout") or 300), 5)
        except (TypeError, ValueError):
            timeout = 300

        if len(reqs) == 1:
            ask_id = str(reqs[0].get("ask_id") or uuid.uuid4().hex[:12])
            box = {"ev": threading.Event(), "kind": "single",
                   "choice": None, "how": None, "ids": [ask_id],
                   # 卡片内容必须留在服务端：主人刷新页面、切会话、关页面再打开时
                   # 前端 store 是全新的，只有这里还知道「有一张卡正等人点」。
                   "reqs": reqs, "born": time.time(), "timeout": timeout}
            with _LOCK:
                _PENDING[ask_id] = box
            try:
                emit("approval_request", dict(reqs[0], ask_id=ask_id,
                                              channel="webui", ts=_now()))
                # reqs[0] 已含 accepts_note / note_hint，前端据此决定要不要显示输入框
            except Exception:
                with _LOCK:
                    _PENDING.pop(ask_id, None)
                return [D("E", "error")]
            got = box["ev"].wait(timeout=timeout)
            with _LOCK:
                _PENDING.pop(ask_id, None)
            if not got:
                _safe(emit, "approval_expired", {"ask_id": ask_id, "timeout": timeout, "ts": _now()})
                return [D("E", "expired")]
            how = str(box.get("how") or "answered")
            if box.get("choice") is None:
                how = "expired"
            choice = str(box.get("choice") or "E")
            note = _clean_note(box.get("note"))
            _safe(emit, "approval_resolved", {"ask_id": ask_id, "choice": choice,
                                              "via": how, "note": note, "ts": _now()})
            return [D(choice, how, note)]

        # ---- 多条：一张合并卡 ----
        batch_id = uuid.uuid4().hex[:12]
        ids = [str(r.get("ask_id") or uuid.uuid4().hex[:12]) for r in reqs]
        box = {"ev": threading.Event(), "kind": "batch", "approved": None,
               "scope": None, "how": None, "ids": ids,
               "reject_unchecked": True,
               "reqs": reqs, "born": time.time(), "timeout": timeout,
               "session_id": reqs[0].get("session_id", "")}
        with _LOCK:
            _PENDING[batch_id] = box
        _safe(emit, "approval_batch", {
            "batch_id": batch_id, "timeout": timeout, "total": len(reqs),
            "session_id": reqs[0].get("session_id", ""), "ts": _now(),
            "scopes": [{"key": "once", "label": "仅批准本次", "emoji": "🟢"},
                       {"key": "session", "label": "本会话允许这些路径", "emoji": "🟡"},
                       {"key": "persistent", "label": "永久允许这些路径", "emoji": "⚪"}],
            "accepts_note": bool(reqs[0].get("accepts_note")),
            "note_hint": reqs[0].get("note_hint", ""),
            # 整包透传，不手工搬字段。同类病犯过三次（reducer 漏 accepts_note、
            # 网关丢 note、这里丢字段）：白名单一多新字段就静默消失，
            # 表现为「你打字我收不到」这种谁也看不出问题的缺陷。
            "items": [dict(r, ask_id=ids[i]) for i, r in enumerate(reqs)],
        })
        got = box["ev"].wait(timeout=timeout)
        with _LOCK:
            _PENDING.pop(batch_id, None)
        if not got or box.get("approved") is None:
            how = "expired" if not got else "cancelled"
            _safe(emit, "approval_batch_resolved",
                  {"batch_id": batch_id, "via": how, "approved": [], "ts": _now()})
            return [D("E", how) for _ in reqs]
        approved = set(str(x) for x in (box.get("approved") or []))
        scope_key = _SCOPE_KEY.get(str(box.get("scope") or "once"), "A")
        how = str(box.get("how") or "answered")
        # 一张卡一条说明，可注明针对第几项；逐项独立说明留给需要时再加
        # —— 上一轮卡片塌陷就是被塞太多东西导致的，高度是硬约束。
        batch_note = _clean_note(box.get("note"))
        item_notes = box.get("item_notes") or {}
        out = []
        for aid in ids:
            note = _clean_note(item_notes.get(aid)) or batch_note
            if aid in approved:
                out.append(D(scope_key, how, note))
            else:
                # 主人看过整批后主动没勾它 —— 这是"拒绝"，不是"没响应"
                out.append(D("E", how if how != "answered" else "answered", note))
        _safe(emit, "approval_batch_resolved", {"batch_id": batch_id, "via": how,
                                                "approved": sorted(approved), "ts": _now()})
        return out


def _safe(emit, etype: str, payload: Dict[str, Any]) -> None:
    """事件发不出去绝不能反过来影响裁决本身。"""
    try:
        emit(etype, payload)
    except Exception:
        pass


def _clean_note(val: Any) -> str:
    """裁主的原话：只限长度与空白，不改写内容（改写就是替他说话）。"""
    txt = str(val or "").strip()
    if len(txt) > 400:
        txt = txt[:400] + "…"
    return txt


def answer(ask_id: str, choice: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """HTTP 回投。单条收 {choice, note}；合并卡收 {approved:[id...], scope, note,
    item_notes:{ask_id:note}}。note 一律原样转交引擎，通道不作解释。"""
    payload = payload or {}
    key = str(ask_id or "").strip()
    if not key:
        return {"ok": False, "error": "ask_id / batch_id 必填"}
    with _LOCK:
        box = _PENDING.get(key)
        if box is None:
            live = list(_PENDING.keys())
        elif box["kind"] == "batch":
            raw = payload.get("approved")
            if not isinstance(raw, (list, tuple)):
                return {"ok": False, "error": "合并卡必须提交 approved 列表"}
            box["approved"] = [str(x) for x in raw]
            box["scope"] = str(payload.get("scope") or "once")
            box["note"] = _clean_note(payload.get("note"))
            raw_notes = payload.get("item_notes")
            if isinstance(raw_notes, dict):
                box["item_notes"] = {str(k): _clean_note(v) for k, v in raw_notes.items()}
            box["how"] = "answered"
            if payload.get("stop"):
                box["approved"] = []
                box["how"] = "stop"
        else:
            raw = payload.get("approved")
            if isinstance(raw, (list, tuple)):
                # 合并卡也适用于单条：勾了=按 scope 批准，没勾=拒绝
                sk = _SCOPE_KEY.get(str(payload.get("scope") or "once"), "A")
                box["choice"] = sk if key in [str(x) for x in raw] else "E"
            else:
                c = str(choice or payload.get("choice") or "").strip().upper()[:1] or "E"
                box["choice"] = c if c in ("A", "B", "D", "E", "F") else "E"
            box["note"] = _clean_note(payload.get("note"))
            box["how"] = "answered"
    if box is None:
        return {"ok": False, "ask_id": key, "live": live[:4],
                "error": "该审批已结束（多半已超时按拒绝处理），这次答复未被采纳"}
    box["ev"].set()
    return {"ok": True, "ask_id": key,
            "approved": len(box.get("approved") or []) if box["kind"] == "batch" else 1,
            "choice": box.get("choice") or ""}


def cancel_all(reason: str = "turn_end") -> int:
    """回合结束/被停止时清理挂起者，绝不留下永远等不到的等待者。"""
    with _LOCK:
        items = list(_PENDING.items())
        _PENDING.clear()
    for _, box in items:
        if box["kind"] == "batch":
            if box.get("approved") is None:
                box["approved"] = []
        elif box.get("choice") is None:
            box["choice"] = "E"
        if not box.get("how"):
            box["how"] = "cancelled"
        box["ev"].set()
    return len(items)


def pending_count() -> int:
    with _LOCK:
        return len(_PENDING)


def _remaining(box: Dict[str, Any]) -> int:
    """还剩几秒。用服务端算出的秒数，而不是让浏览器去对两台机器的时钟。"""
    born = float(box.get("born") or 0.0)
    win = int(box.get("timeout") or 300)
    if not born:
        return win
    return max(0, int(born + win - time.time()))


def pending_cards() -> List[Dict[str, Any]]:
    """当前真挂着的审批卡片，按 SSE 原形状返回（前端可直接喂给 reducer）。

    刻意不用 SSE 环形回放做这件事：回放会把已裁决、或已超时结束的卡一并复活，
    那比看不到卡片更糟 —— 主人会对着一个早就不存在的 ask_id 点「允许」。
    这里只报还在等人的那些。
    """
    with _LOCK:
        items = list(_PENDING.items())
    out: List[Dict[str, Any]] = []
    for key, box in items:
        reqs = list(box.get("reqs") or [])
        if not reqs or box.get("choice") is not None or box.get("approved") is not None:
            continue
        rem = _remaining(box)
        if rem <= 0:
            continue                    # 已到点，等待线程马上会按拒绝结算
        if box.get("kind") == "batch":
            ids = [str(r.get("ask_id") or "") for r in reqs]
            out.append({
                "type": "approval_batch", "ask_id": key, "batch_id": key,
                "timeout": int(box.get("timeout") or 300), "remaining": rem,
                "total": len(reqs), "session_id": box.get("session_id", ""),
                "scopes": [{"key": "once", "label": "仅批准本次", "emoji": "🟢"},
                           {"key": "session", "label": "本会话允许这些路径", "emoji": "🟡"},
                           {"key": "persistent", "label": "永久允许这些路径", "emoji": "⚪"}],
                "accepts_note": bool(reqs[0].get("accepts_note")),
                "note_hint": reqs[0].get("note_hint", ""),
                "restored": True,
                "items": [dict(r, ask_id=ids[i]) for i, r in enumerate(reqs)],
            })
        else:
            # 单条也要显式带上 timeout：前端按 (timeout - remaining) 折算出生时刻来
            # 续算倒计时，漏了这个字段就会按默认 300s 折算 —— 秒数对不上真实窗口。
            out.append(dict(reqs[0], ask_id=key, channel="webui",
                            type="approval_request", timeout=int(box.get("timeout") or 300),
                            remaining=rem, restored=True))
    out.sort(key=lambda d: str(d.get("ask_id") or ""))
    return out


def status_snapshot() -> Dict[str, Any]:
    return {"channel": "webui", "pending": pending_count(), "emit_bound": _EMIT is not None}
