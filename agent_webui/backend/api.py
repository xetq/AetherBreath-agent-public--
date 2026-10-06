# -*- coding: utf-8 -*-
"""REST + SSE 路由。自动文档见网关根路径下的 /docs。"""
from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import attachments as attachments_mod
import config
import mcp_view
import registry_view
import meta
import sessions as sessions_mod
import workspace as ws_mod
from agent_client import BridgeError
from agent_proc import AgentStartError, get_manager
from events import Ev, PHASE_ON
from sse import sse_frame

router = APIRouter(prefix="/api", tags=["webui"])


# ---------------- 请求模型 ----------------
class SessionCreate(BaseModel):
    session_id: Optional[str] = Field(None, description="留空则自动新建；给出则续聊")
    prefix: str = Field("session", description="自动新建时的 id 前缀")
    title: Optional[str] = Field(None, description="会话标题（WebUI 侧车，留空则用默认摘要）")


class TitleIn(BaseModel):
    title: str = Field("", description="新标题；留空=清除自定义标题，回落默认")


class ChatIn(BaseModel):
    session_id: str
    message: str
    # 附件：相对项目根的路径列表（由 POST /attachments 落盘后返回）。
    # 只传路径、不传内容 —— 路径合法性与类型由后端重新校验，前端说的不算。
    attachments: Optional[List[str]] = None


class StopIn(BaseModel):
    run_id: Optional[str] = None


class MidTurnIn(BaseModel):
    # 中期交互：回合运行中追加的「用户交代」（只由 WebUI 使用）
    session_id: str
    text: str
    # 目标回合。给了就必须命中 —— 防止「话投给已经结束的回合，却悄悄进了下一个」
    run_id: Optional[str] = Field(None, description="目标回合 id；可留空表示投给当前活动回合")
    # 附件与 /chat 同一套（相对路径列表），走同一份校验
    attachments: Optional[List[str]] = None


class ApprovalIn(BaseModel):
    # 单条与合并卡共用一份结构：勾中的放 approved，批准范围由 scope 决定
    ask_id: str
    choice: str = ""
    approved: Optional[List[str]] = None
    scope: str = "once"
    stop: bool = False
    # 主人的补充说明。曾经这里没有它，Pydantic 就把前端发来的 note 静默丢掉，
    # 表现为"我打了字但你没收到"，而前后端各自检查都觉得自己没错。
    note: str = ""
    item_notes: Optional[Dict[str, str]] = None


class AskIn(BaseModel):
    ask_id: str
    answer: str


class StartIn(BaseModel):
    mode: str = Field("", description="扩展位：将来按 mode 加载不同 persona/工具集")


class UploadIn(BaseModel):
    # 附件上传：base64 JSON（不用 multipart —— 网关依赖里没有 python-multipart）
    session_id: str = Field("", description="归属会话；留空则落到 unknown/")
    filename: str = Field(..., description="原始文件名（仅用于取名与判类型）")
    data_b64: str = Field(..., description="文件内容的 base64；也接受整串 data URL")


def _mgr():
    return get_manager()


def _clean_attachments(items: Optional[List[str]]) -> List[str]:
    """把前端给的附件路径收成一份可信列表。

    这里**必须重新校验**，不能信前端：路径要落在 attachments 落盘根内、且文件真实存在。
    否则「附件」就成了任意读文件的后门（把 /etc/passwd 当"附件"递进来）。
    """
    if not items:
        return []
    root_dir = attachments_mod.root_dir()
    out: List[str] = []
    for raw in items[:50]:
        s = str(raw or "").strip().replace("\\", "/")
        if not s:
            continue
        p = (config.PROJECT_ROOT / s).resolve()
        if p != root_dir and root_dir not in p.parents:
            raise HTTPException(400, "附件路径越界（不在附件目录内）：%s" % s)
        if not p.is_file():
            raise HTTPException(400, "附件不存在：%s" % s)
        out.append(s)
    return out


# ---------------- 会话 ----------------
@router.get("/sessions", summary="会话列表（含 WebUI 标题）")
def api_sessions() -> Dict[str, Any]:
    return meta.merge_titles(sessions_mod.list_sessions())


@router.get("/sessions/search", summary="搜索会话（同时匹配标题与对话记录）")
def api_sessions_search(q: str = Query("", max_length=200),
                        scope: str = Query("all", pattern="^(all|title|content)$"),
                        status: str = Query("", max_length=30),
                        limit: int = Query(30, ge=1, le=100)) -> Dict[str, Any]:
    return meta.search(q, scope=scope, status=status, limit=limit)


@router.post("/sessions", summary="新建或续聊会话")
def api_session_create(body: SessionCreate) -> Dict[str, Any]:
    if body.session_id:
        try:
            sid = sessions_mod.validate_session_id(body.session_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        exists = sessions_mod.session_file(sid).exists()
        title = meta.set_title(sid, body.title) if body.title is not None else None
        return {"session_id": sid, "created": not exists, "resumed": exists,
                "title": (title or {}).get("title", meta.get_titles().get(sid, {}).get("title", ""))}
    sid = sessions_mod.create_session_id((body.prefix or "session").strip() or "session")
    title = meta.set_title(sid, body.title) if body.title else None
    return {"session_id": sid, "created": True, "resumed": False,
            "title": (title or {}).get("title", "")}


@router.post("/sessions/{sid}/title", summary="重命名会话（清空则回落默认摘要）")
def api_session_title(sid: str, body: TitleIn) -> Dict[str, Any]:
    try:
        return meta.set_title(sid, body.title)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/sessions/{sid}/history", summary="会话历史（含工具调用配对）")
def api_session_history(sid: str, limit: int = Query(400, ge=1, le=2000)) -> Dict[str, Any]:
    try:
        sid = sessions_mod.validate_session_id(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return sessions_mod.get_history(sid, limit=limit)


@router.delete("/sessions/{sid}", summary="删除会话（移入 .trash 备份）")
def api_session_delete(sid: str) -> Dict[str, Any]:
    try:
        sid = sessions_mod.validate_session_id(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res = sessions_mod.delete_session(sid)
    if res.get("ok"):
        meta.drop_title(sid)      # 标题跟着会话走，不留孤儿条目
    if not res.get("ok"):
        raise HTTPException(404, res.get("error", "删除失败"))
    return res


# ---------------- 对话 ----------------
@router.post("/chat", summary="发起回合（异步，事件走 SSE）")
def api_chat(body: ChatIn) -> Dict[str, Any]:
    sid = (body.session_id or "").strip()
    msg = (body.message or "").strip()
    if not sid:
        raise HTTPException(400, "session_id 不能为空")
    try:
        sid = sessions_mod.validate_session_id(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    atts = _clean_attachments(body.attachments)
    if not msg and not atts:
        raise HTTPException(400, "消息不能为空")
    if len(msg) > 60000:
        raise HTTPException(400, f"消息过长（{len(msg)} 字符，上限 60000）")
    mgr = _mgr()
    if mgr.sm.phase != PHASE_ON:
        raise HTTPException(409, "AB 进程未运行，请先点击开机")
    try:
        res = mgr.chat(sid, msg, atts)
    except BridgeError as e:
        raise HTTPException(502, f"bridge 通信失败: {e}")
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "回合启动失败"))
    return {"ok": True, "run_id": res["run_id"], "session_id": sid}


@router.post("/chat/stop", summary="中断当前回合（在工具/请求边界保存退出）")
def api_chat_stop(body: StopIn) -> Dict[str, Any]:
    mgr = _mgr()
    try:
        res = mgr.stop_run(body.run_id)
    except BridgeError as e:
        raise HTTPException(502, f"bridge 通信失败: {e}")
    if res.get("reason") == "run-not-found":   # 别拿 200 骗客户端说"已中断"
        raise HTTPException(404, str(res.get("error") or "未找到该回合"))
    return res


@router.post("/chat/mid_turn", summary="回合运行中追加「用户交代」（随下一批工具返回注入）")
def api_chat_mid_turn(body: MidTurnIn) -> Dict[str, Any]:
    """不打断当前回合，把主人的一句话送到模型的下一步动作里。

    与 /chat 的区别是本质的：这里**不起新回合**，也不落盘成一条新发言 ——
    它只是投给正在跑的那个回合，由 AB 在「下一批工具返回」处注入。
    所以没有活动回合时它必须失败（400/409），而不是悄悄变成一次普通发送。
    """
    sid = (body.session_id or "").strip()
    text = (body.text or "").strip()
    if not sid:
        raise HTTPException(400, "session_id 不能为空")
    try:
        sid = sessions_mod.validate_session_id(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    atts = _clean_attachments(body.attachments)
    if not text and not atts:
        raise HTTPException(400, "交代内容不能为空")
    mgr = _mgr()
    if mgr.sm.phase != PHASE_ON:
        raise HTTPException(409, "AB 进程未运行，请先点击开机")
    try:
        res = mgr.mid_turn(sid, text, body.run_id, atts)
    except BridgeError as e:
        raise HTTPException(502, f"bridge 通信失败: {e}")
    if not res.get("ok"):
        if res.get("reason") == "run-not-found":   # 与 /chat/stop 保持一致：404
            raise HTTPException(404, str(res.get("error") or "该回合已结束"))
        raise HTTPException(409, str(res.get("error") or "交代未能送达"))
    return res


@router.post("/ask/answer", summary="回答 agent 的 ask 提问")
def api_ask(body: AskIn) -> Dict[str, Any]:
    ask_id = (body.ask_id or "").strip()
    if not ask_id:
        raise HTTPException(400, "ask_id 不能为空")
    try:
        res = _mgr().ask_answer(ask_id, body.answer or "")
    except BridgeError as e:
        raise HTTPException(502, f"bridge 通信失败: {e}")
    if not res.get("ok"):
        raise HTTPException(410, res.get("error", "提问已失效"))
    return res


@router.post("/approval/answer", summary="裁决 agent 的系统盘/高危操作审批")
def api_approval(body: ApprovalIn) -> Dict[str, Any]:
    ask_id = (body.ask_id or "").strip()
    if not ask_id:
        raise HTTPException(400, "ask_id 不能为空")
    choice = (body.choice or "E").strip()[:2] or "E"
    try:
        res = _mgr().approval_answer(ask_id, choice, {
            "approved": body.approved, "scope": body.scope or "once",
            "stop": bool(body.stop), "note": (body.note or "")[:400],
            "item_notes": body.item_notes})
    except BridgeError as e:
        raise HTTPException(502, "bridge 通信失败: %s" % e)
    if not res.get("ok"):
        # 410：审批已结束（超时或回合终止），答复未被采纳。必须如实报错，
        # 否则主人以为批过了、AB 其实被拒，是最难排查的一类错位。
        raise HTTPException(410, res.get("error") or "该审批已结束，答复未被采纳")
    return res


@router.api_route("/approval/pending", methods=["GET", "POST"],
                  summary="拉取当前挂起的审批卡片（刷新/切会话/重开后恢复）")
def api_approval_pending() -> Dict[str, Any]:
    # 恢复通道是"锦上添花"，绝不允许它的失败把页面加载带崩：
    # bridge 没起来、超时、异常，一律回空列表 + 如实写明原因。
    #
    # 同时收 GET 与 POST 是有意的：前端曾经把它写成 POST（后端只注册 GET），
    # 于是每个恢复请求都吃 405、又在前端被静默吞掉 —— 通道全废而无人察觉。
    # 观测面不该靠"调用方恰好写对方法"来成立。
    #
    # authoritative 是给前端的裁量依据："这条回答代表服务端的权威快照吗？"
    # False（没问到 / bridge 出错）时前端不得据此剪枝，否则会把屏幕上真挂着的
    # 卡删掉。bridge 自己报 ok:false 时也一并算作非权威。
    try:
        res = _mgr().approval_pending()
    except BridgeError as e:
        return {"ok": True, "cards": [], "authoritative": False,
                "error": "bridge 未就绪：%s" % e}
    except Exception as e:
        return {"ok": True, "cards": [], "authoritative": False,
                "error": "拉取失败：%s: %s" % (e.__class__.__name__, e)}
    auth = bool(res.get("authoritative", res.get("ok", True)))
    return {"ok": bool(res.get("ok", True)), "cards": res.get("cards") or [],
            "authoritative": auth, "error": res.get("error") or ""}


@router.api_route("/ask/pending", methods=["GET", "POST"],
                  summary="拉取当前挂起的 ask_user 提问（同上，可恢复）")
def api_ask_pending() -> Dict[str, Any]:
    try:
        res = _mgr().ask_pending()
    except BridgeError as e:
        return {"ok": True, "cards": [], "authoritative": False,
                "error": "bridge 未就绪：%s" % e}
    except Exception as e:
        return {"ok": True, "cards": [], "authoritative": False,
                "error": "拉取失败：%s: %s" % (e.__class__.__name__, e)}
    auth = bool(res.get("authoritative", res.get("ok", True)))
    return {"ok": bool(res.get("ok", True)), "cards": res.get("cards") or [],
            "authoritative": auth, "error": res.get("error") or ""}


@router.api_route("/orch", methods=["GET", "POST"],
                  summary="编排器占用视图（跨回合作业占了哪些槽位）")
def api_orch() -> Dict[str, Any]:
    # 与卡片恢复同源：这是"刷新后仍然看得见作业在跑"的通道，绝不允许它的失败把页面带崩。
    # authoritative=False 时前端**不得**据此剪枝 —— 没问到 ≠ 没有作业在跑。
    # 同时收 GET/POST：方法写错就整条通道失效，这个坑在 /approval/pending 上踩过。
    try:
        res = _mgr().orch()
    except BridgeError as e:
        return {"ok": True, "pipes": [], "authoritative": False,
                "error": "bridge 未就绪：%s" % e}
    except Exception as e:
        return {"ok": True, "pipes": [], "authoritative": False,
                "error": "拉取失败：%s: %s" % (e.__class__.__name__, e)}
    return {"ok": bool(res.get("ok", True)), "pipes": res.get("pipes") or [],
            "authoritative": bool(res.get("authoritative")),
            "error": res.get("error") or ""}


# ---------------- SSE：唯一事件出口 ----------------
@router.get("/events", summary="SSE 事件流（bridge 事件 + 网关事件同源）")
async def api_events(request: Request, since: int = Query(0, ge=0),
                     session_id: Optional[str] = None):
    mgr = _mgr()
    hub = mgr.hub
    q = hub.subscribe()

    async def stream():
        try:
            for evt in hub.replay(since, session_id):
                yield sse_frame(evt)
            yield sse_frame({"type": Ev.AGENT_PHASE, **mgr.status(),
                             "reason": "snapshot"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if session_id and evt.get("session_id") not in (None, session_id):
                    continue
                yield sse_frame(evt)
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------- 电源（AB 进程生命周期）----------------
@router.post("/approval/rules", summary="列出当前生效的免审规则（永久 + 本会话）")
def api_approval_rules(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    # 整包透传：网关不再维护字段白名单，避免「新字段静默消失」重演
    try:
        return _mgr().approval_rules(str((body or {}).get("session_id") or ""))
    except BridgeError as e:
        raise HTTPException(502, "bridge 通信失败: %s" % e)


@router.post("/approval/rules/revoke", summary="撤销免审规则（path 为空 = 全部撤销）")
def api_approval_revoke(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    try:
        return _mgr().approval_revoke(dict(body or {}))
    except BridgeError as e:
        raise HTTPException(502, "bridge 通信失败: %s" % e)


@router.post("/permission/mode", summary="查询 / 切换会话级权限模式（仅读/工作区/普通/完全）")
def api_permission_mode(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """带 mode = 切换；不带 = 只查。返回四档清单供前端画下拉。

    写路径的归属（**两个写者写同一份会话 JSON 必然互相覆盖**）：
      · AB 在跑 → 全交给 bridge：它把模式当场写进引擎缓存（下一次工具调用即按新档判），
        并按「有没有并发回合」决定当场落盘，还是排队到下个回合开头补进上下文；
      · AB 没开机 → 网关把这一档写进会话文件。开机加载会话时由 agent.load_session
        读回引擎 —— 这就是「重启后自动回到退出前的模式」那条要求的落地方式。
    """
    sid = str((body or {}).get("session_id") or "").strip()
    if not sid:
        raise HTTPException(400, "session_id 不能为空")
    try:
        sid = sessions_mod.validate_session_id(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    raw_mode = (body or {}).get("mode")
    mode = None if raw_mode in (None, "") else str(raw_mode).strip().lower()
    valid: set = set()
    try:
        import permission_modes as pm            # noqa: PLC0415
        valid = set(pm.MODES)
        catalog = pm.catalog()
    except Exception:
        catalog = []
    # 切换路径严格校验（与 bridge 同一口径）：手滑传个 "read-only" 若被收敛成
    # 「普通」，就是一次拼写错误换来一档更松的权限。**尤其**在 AB 未开机的分支里
    # 这一档会直接写进会话文件、下次开机生效 —— 更不能放行。
    if mode is not None and valid and mode not in valid:
        raise HTTPException(400, "未知的权限模式 %r；可选：%s"
                            % (raw_mode, "、".join(sorted(valid))))
    mgr = _mgr()
    if mgr.sm.phase == PHASE_ON:
        try:
            res = mgr.permission_mode(sid, mode)
        except BridgeError as e:
            raise HTTPException(502, "bridge 通信失败: %s" % e)
        if isinstance(res, dict) and res.get("ok") is False:
            raise HTTPException(400, str(res.get("error") or "切换权限模式失败"))
        return res
    if mode is None:
        data = sessions_mod._load_raw(sid) or {}
        return {"ok": True, "session_id": sid, "offline": True,
                "mode": str(data.get("permission_mode") or "normal"),
                "catalog": catalog}
    res = sessions_mod.set_permission_mode(sid, mode)
    if not res.get("ok"):
        # 会话文件还没有（全新会话 + AB 没开机）时写不进去。**必须如实报错**：
        # 前端若把"没有 mode 字段"当成成功，就会弹一句"已存入会话，开机后生效"——
        # 那是假承诺，用户以为设好了，开机却是旧的档（静默降级那一类病）。
        raise HTTPException(409, str(res.get("error") or "无法把权限模式存入会话"))
    return {**res, "offline": True, "catalog": catalog}


@router.post("/agent/start", summary="开机：拉起 bridge 子进程")
def api_agent_start(body: StartIn) -> Dict[str, Any]:
    try:
        return _mgr().start(mode=(body.mode or "").strip())
    except AgentStartError as e:
        return {"ok": False, "error": str(e), **_mgr().status()}


@router.post("/agent/stop", summary="优雅关机：中断回合 -> 等待保存 -> 退出进程")
def api_agent_stop() -> Dict[str, Any]:
    return _mgr().stop()


@router.post("/agent/kill", summary="强制关闭：直接终止子进程（防卡死）")
def api_agent_kill() -> Dict[str, Any]:
    return _mgr().kill()


@router.get("/agent/status", summary="进程级 + 回合级状态")
def api_agent_status() -> Dict[str, Any]:
    return _mgr().status()


@router.get("/agent/tools", summary="当前 AB 进程可见的工具集")
def api_agent_tools() -> Dict[str, Any]:
    return _mgr().tools()


@router.get("/agent/log", summary="bridge stderr 尾部（诊断用）")
def api_agent_log(lines: int = Query(60, ge=1, le=400)) -> Dict[str, Any]:
    mgr = _mgr()
    tail = mgr.stderr_tail(lines)
    return {"ok": True, "tail": tail, "lines": len(tail.splitlines()) if tail else 0}


# ---------------- 工作区 ----------------
@router.get("/workspace/status", summary="工作区状态快照（只读）")
def api_workspace_status() -> Dict[str, Any]:
    return ws_mod.status()


@router.get("/workspace/dir", summary="展开工作区子目录")
def api_workspace_dir(path: str = Query("", description="相对 agent_workspace 的路径")) -> Dict[str, Any]:
    return ws_mod.dir_detail(path)


# ---------------- MCP 服务站（v2 运行时面板） ----------------
@router.get("/mcp/stations", summary="MCP 服务站状态快照（常驻，不必先发消息）")
def api_mcp_stations() -> Dict[str, Any]:
    """「运行时」面板的数据源。组装逻辑在 `mcp_view.py`（不依赖 fastapi，便于单测）。"""
    return mcp_view.stations_snapshot()


# ---------------- 技能 / 集成包（运行时面板） ----------------
@router.get("/registries", summary="技能与集成包快照（常驻，只读盘）")
def api_registries() -> Dict[str, Any]:
    """「运行时」面板的另两块数据源（SKILL / 集成包）。

    组装逻辑在 `registry_view.py`（与 `mcp_view.py` 同构，不依赖 fastapi，便于单测）。
    两块**各自独立成败**：技能库读崩了集成包照样显示，反之亦然。
    """
    return registry_view.registries_snapshot()


# ---------------- 网关健康 ----------------
@router.post("/attachments", summary="上传附件（base64 JSON，只落盘，绝不执行）")
def api_upload_attachment(body: UploadIn) -> Dict[str, Any]:
    """收下前端选中的文件：**只落盘 + 分类**，不做任何内容理解、绝不执行。

    返回的 attachment.path 就是后续 /chat、/chat/mid_turn 要带的那个值。
    """
    sid = (body.session_id or "").strip()
    if sid:
        try:
            sid = sessions_mod.validate_session_id(sid)
        except ValueError as e:
            raise HTTPException(400, str(e))
    raw = (body.data_b64 or "").strip()
    if raw.startswith("data:"):          # 容错：前端可能直接给整串 data URL
        _head, _, raw = raw.partition(",")
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception as e:
        raise HTTPException(400, "附件内容不是合法的 base64（%s）" % type(e).__name__)
    try:
        meta = attachments_mod.save(sid, body.filename, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "attachment": meta}


@router.get("/attachments/limits", summary="附件开关、上限与类型白名单（前端自描述用）")
def api_attachment_limits() -> Dict[str, Any]:
    st = attachments_mod.settings()
    return {
        "ok": True,
        "enabled": bool(st.get("enabled", True)),
        "max_file_mb": st.get("max_file_mb"),
        "max_per_message": st.get("max_per_message"),
        "max_text_chars": st.get("max_text_chars"),
        "accepted": attachments_mod.accepted_exts(),
    }


@router.get("/health", summary="网关健康 + 关键配置")
def api_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "aetherbreath-webui-gateway",
        "gateway_pid": os.getpid(),
        "agent": _mgr().status(),
        "paths": {
            "project_root": str(config.PROJECT_ROOT),
            "working_memory": str(config.WORKING_MEMORY_DIR),
            "workspace": str(config.WORKSPACE_ROOT),
            "frontend_dist": str(config.FRONTEND_DIST),
            "dist_ready": config.FRONTEND_DIST.exists(),
        },
        "interpreters": {
            "gateway": str(config.GATEWAY_PYTHON),
            "agent": str(config.AGENT_PYTHON),
            "agent_exists": config.AGENT_PYTHON.exists(),
        },
        "timeouts": {
            "start_sec": config.START_TIMEOUT_SEC,
            "graceful_stop_sec": config.GRACEFUL_STOP_TIMEOUT_SEC,
            "ask_sec": config.ASK_TIMEOUT_SEC,
            "approval_sec": config.APPROVAL_TIMEOUT_SEC,
        },
        "subscribers": _mgr().hub.subscriber_count(),
    }
