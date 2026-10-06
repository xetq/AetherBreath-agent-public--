# -*- coding: utf-8 -*-
"""中期交互（mid-turn）—— 回合进行中主人追加的「用户交代」。

⚠️ 本文件属于 **WebUI 侧**（2026-09-22 从 agent/mid_turn.py 迁来）：agent 本体
不再认识"中期交互"这个概念，只提供一个**通用挂载点**
（`agent.register_after_tools_hook`，见 agent/agent.py「宿主扩展点」一节）。
信箱实体、渲染、注入时机全在本模块，由 bridge 在启动时把 flush_after_tools
注册进那个钩子：

    bridge._install_runtime() -> ab_agent.register_after_tools_hook(flush_after_tools)

于是 CLI 模式（python agent/agent.py）既不 import 本文件、也没有任何注册者，
钩子表为空 → 主循环遍历零开销、行为零差异。与 ask_user 那条注入链同构
（webui 侧模块持有实现，bridge 注入；agent 侧只是可变挂载点）。

问题：AB 干长活时会持续产生中期输出（工具时间线 / 中期进度）。主人看着它跑，
发现跑偏了，但**不想停掉整个回合**再重开 —— 那会把已经跑完的工具结果和
当前语境一起丢掉。他需要的是：在回合运行中把一个新交代送到模型下一步的动作里。

机制（只由 WebUI 使用；CLI 端不接入口 → 信箱恒空，行为零差异）：

    主人输入 → 网关 /api/chat/mid_turn → bridge /mid_turn → 本模块信箱
    → 回合里「下一批工具返回」那一刻，本模块被 agent 的 after-tools 钩子回调，
      把交代作为一条独立 user 消息随工具结果回灌模型

设计约束（每条都对应一个真实会踩的坑）：

1. **绝不污染工具输出**：交代永远走独立的 user 消息，不拼进 tool 返回的 content 里。
   拼进去会让模型把主人的话当成工具的真实内容（读文件时最危险：主人一句
   「别动 config.yaml」会被读成文件内容的一部分）。
2. **不反向依赖 agent，也不反向依赖 WebUI**：agent 侧只多一个通用挂载点；
   SSE 事件出口由宿主（bridge）用 set_observer 注入 —— 本模块不 import 任何
   宿主模块，CLI 下没有观察者时一切照常。
3. **线程安全**：push 来自 bridge 的 HTTP 处理线程，drain 来自回合线程，
   必须加锁（编排器 worker 线程不参与投递）。
4. **过期即作废，不许诈尸**：回合结束时仍没被注入的交代一律丢弃，并由上层
   （bridge → SSE）明确告诉主人「这条没送到」。宁可让主人重发一句，也不要
   留下一句会在下个回合突然冒出来的旧指令 —— 那会让模型执行一个已经不成立的要求。
5. **可审计**：注入后的消息会随会话落盘，正文带 PREFIX 标识；下次续聊、
   在 CLI 里打开同一会话、或事后翻历史，都能一眼认出「这是主人中途插的话」。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# 界面与模型两侧共用的标识（前端 frontend/src/lib/midTurn.ts 必须与此完全一致，
# tests/test_mid_turn.py 里有断言锁死，防止两边各改一半）。
MARK = "用户交代"
PREFIX = "【用户交代 · 回合进行中追加】"
GUIDE = "（这是主人趁你干活时追加的交代；与之前的计划冲突时，以此为准）"

# 附件标记：普通发送与中期交互共用，agent 咽喉按它物化。
# 单行、不用竖线以外的分隔符，便于稳定解析。
ATT_MARK = "【附件】"
ATT_FIELDS = ("name", "kind", "size", "path")


def att_mark(path: str, name: str = "", kind: str = "", size: int = -1) -> str:
    """生成一条附件标记行（字段缺失就省略）。"""
    import os as _os
    p = str(path or "").strip()
    if not name:
        name = _os.path.basename(p.replace("\\", "/"))
    parts = ["name=%s" % name, "path=%s" % p]
    if kind:
        parts.insert(1, "kind=%s" % kind)
    if size is not None and int(size) >= 0:
        parts.insert(2, "size=%d" % int(size))
    return "%s%s" % (ATT_MARK, " | ".join(parts))


MAX_TEXT = 4000        # 单条交代字符上限（和 /chat 的 60000 比刻意收紧：这是插话，不是长文）
MAX_PENDING = 20       # 单会话信箱上限，防连点灌爆；满了拒绝并如实告知


class MidTurnBox:
    """按会话归属的「用户交代」信箱（进程内，线程安全）。

    为什么按 session_id 而不是 run_id 归属：主人只认会话，不认回合。同会话
    同一时刻最多一个回合（_TURN_GATE 保证），所以按会话放就够了；回合结束时
    由上层把该会话残留清空，不会跨回合串台。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._box: Dict[str, List[Dict[str, Any]]] = {}
        self._observer: Optional[Callable[[str, Dict[str, Any]], None]] = None

    # ---------- 观察者：本模块不反向依赖 WebUI，事件出口由宿主注入 ----------
    def set_observer(self, fn: Optional[Callable[[str, Dict[str, Any]], None]]) -> None:
        """注册阶段回调 fn(stage, payload)。bridge 用它把事件转成 SSE。

        没有观察者（CLI 场景）时一切照常，只是没人听 —— 这也是 CLI 零行为差异的一部分。
        """
        with self._lock:
            self._observer = fn

    def _announce(self, stage: str, payload: Dict[str, Any]) -> None:
        obs = self._observer
        if obs is None:
            return
        data: Dict[str, Any] = {"mid": stage, "at": time.time()}
        data.update(payload)
        try:
            obs(stage, data)
        except Exception:
            pass   # 观测面绝不允许影响投递/注入本身

    # ---------- 写入（bridge 的 HTTP 线程） ----------
    def push(self, session_id: str, text: str, origin: str = "webui",
             run_id: Optional[str] = None, attachments=None) -> Optional[Dict[str, Any]]:
        """收下一条交代。返回投递回执（含 id），失败返回 None。

        run_id 必须由投递方（bridge 的 /mid_turn）带进来并一路传下去 —— 它进了
        accepted/injected/dropped 三阶段事件的公共字段。少了它，前端就只能靠
        session_id 判断归属，跨回合的迟到帧会变得无法区分（本项目所有事件都带
        run_id，这条不该是例外）。
        """
        sid = str(session_id or "").strip()
        body = str(text or "").strip()
        if not sid or not body:
            return None
        if len(body) > MAX_TEXT:
            body = body[:MAX_TEXT]
        item = {"id": uuid.uuid4().hex[:12], "text": body, "at": time.time(),
                "origin": origin, "run_id": run_id,
                # 附件只带「相对路径」这一种真相；物化（读字节/编码图片）在 agent 咽喉做，
                # 本模块不认识图片也不碰磁盘 —— 保持"通道只搬元数据"的分工。
                "attachments": [str(x) for x in (attachments or []) if str(x or "").strip()]}
        with self._lock:
            lst = self._box.setdefault(sid, [])
            if len(lst) >= MAX_PENDING:
                return None          # 满了就拒绝，绝不悄悄丢最老的那条
            lst.append(item)
            pending = len(lst)
        self._announce("accepted", {"session_id": sid, "run_id": run_id,
                                    "item_id": item["id"], "count": 1, "pending": pending,
                                    "text": body})
        return dict(item, pending=pending)

    # ---------- 读取（回合线程） ----------
    def drain(self, session_id: str) -> List[Dict[str, Any]]:
        """取走并清空本会话全部待投递交代（原子）。"""
        with self._lock:
            return self._box.pop(str(session_id or ""), [])

    def discard(self, session_id: str) -> List[Dict[str, Any]]:
        """作废本会话残留交代（回合收尾用）。与 drain 同语义，名字不同是为了
        让调用点的意图可读：一个是注入，一个是丢弃。"""
        with self._lock:
            return self._box.pop(str(session_id or ""), [])

    def peek(self, session_id: str) -> int:
        with self._lock:
            return len(self._box.get(str(session_id or ""), []))

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._box.items() if v}

    def clear(self) -> None:
        with self._lock:
            self._box.clear()


# 进程内单例：bridge（WebUI 宿主）与 agent 主循环共享同一个对象 —— 同进程，
# 因此不需要跨进程通道，也就不需要序列化/超时那一套。
BOX = MidTurnBox()


def render(items: List[Dict[str, Any]]) -> str:
    """把若干条交代渲染成注入会话的那段文本。

    格式固定为「标识行 + 引导行 + 正文」，标识行是前后端与模型三方的共同锚点。
    """
    lines = [PREFIX, GUIDE]
    if len(items) == 1:
        lines.append(items[0]["text"])
    else:
        for i, it in enumerate(items, 1):
            lines.append("%d. %s" % (i, it["text"]))
    # 附件以**独立标记行**附在末尾：格式与普通发送路径完全一致（MARK_ATT），
    # 这样 agent 咽喉只需认一种写法就能把两者都物化成 content 块。
    for it in items:
        for rel in (it.get("attachments") or []):
            lines.append(att_mark(rel))
    return "\n".join(lines)


def flush_after_tools(conversation: List[Dict[str, Any]], session_id: str,
                      log: Any = None) -> int:
    """【注入点】把信箱里的交代作为独立 user 消息追加到 conversation 末尾。

    由 bridge 注册进 agent 的 after-tools 钩子（agent.register_after_tools_hook），
    于是调用时机＝「一批工具的结果刚写进 conversation 之后、下一次 LLM 请求之前」。
    模型在「带着这批工具结果」继续推理时，能同时看到主人新插的话。

    返回注入条数（0 表示信箱为空，什么都没发生）。
    """
    items = BOX.drain(session_id)
    if not items:
        return 0
    conversation.append({"role": "user", "content": render(items)})
    if log is not None:
        try:
            log.info("[中期交互] 随本批工具返回注入 %d 条「%s」" % (len(items), MARK))
        except Exception:
            pass
    BOX._announce("injected", {
        "session_id": session_id,
        "run_id": items[0].get("run_id"),
        "ids": [it["id"] for it in items],
        "count": len(items),
        "texts": [it["text"] for it in items],
        "pending": 0,
    })
    return len(items)
