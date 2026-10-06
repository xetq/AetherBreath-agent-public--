# -*- coding: utf-8 -*-
"""会话读取层（只读扫描 agent_memory/working_memory/*.json）。

格式由 agent.save_session 决定：
  {session_id, created_at(=最后保存时间), message_count, status, messages,
   system_prompt?, permission_mode?}
删除走"移动到备份"而非物理删除（trash 优先原则）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

# 业务失败判据**只有一份**（task_orchestrator.is_business_failure），本模块不复制逻辑 ——
# 曾经这里用 `str(ret_text).startswith("❌")` 自带一份，且比编排器那份少了 lstrip，
# 于是同一个动作"实时界面标红、刷新后还原成绿"（两个视图两套事实）。
# 本模块也会被单独导入（网关脚本 / 测试），所以自己保证 agent/ 在 path 上。
_AGENT_DIR = str(Path(__file__).resolve().parents[2] / "agent")
if _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

from task_orchestrator import is_business_failure  # noqa: E402

# ---- 「引擎代投的 user 消息」的三种前缀（常量各自只有一处定义，这里 import 来用）----
# 为什么必须排除它们：会话卡片的默认标题取"主人说的第一句话"（下面的 first_user），
# 而这三类虽然也是 user 角色（**必须如此**：system 会被 save_session 丢掉、被
# assemble_request 提到最前当快照），却都不是主人说的。
# 实测反馈：切一次权限模式，会话标题就变成「🔐 [权限模式] 主人把本会话…」。
_BACKEND_DIR = str(Path(__file__).resolve().parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
try:
    from permission_modes import NOTICE_PREFIX as _MODE_NOTICE_PREFIX  # noqa: E402
except Exception:                       # 保险丝：常量取不到也不许把会话列表炸掉
    _MODE_NOTICE_PREFIX = "\U0001f510 [权限模式] "
try:
    from mid_turn import PREFIX as _MID_PREFIX  # noqa: E402
except Exception:
    _MID_PREFIX = "【用户交代 · 回合进行中追加】"
from task_orchestrator import JOB_DELIVERY_PREFIX as _JOB_PREFIX  # noqa: E402


def _is_injected_user_msg(content: Any) -> bool:
    """这条 user 消息是**引擎代投**的（不是主人说的）吗？"""
    text = str(content or "")
    return any(text.startswith(p) for p in
               (_MODE_NOTICE_PREFIX, _MID_PREFIX, _JOB_PREFIX) if p)

STATUS_LABEL = {"active": "进行中", "interrupted": "中断未完成", "complete": "正常结束"}


def validate_session_id(sid: str) -> str:
    sid = (sid or "").strip()
    if not sid or ".." in sid or not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,79}$", sid):
        raise ValueError(f"非法 session_id: {sid!r}（仅允许字母数字下划线点横线，≤80 字符）")
    return sid


def session_file(sid: str) -> Path:
    return config.WORKING_MEMORY_DIR / f"{validate_session_id(sid)}.json"


def _load_raw(sid: str) -> Optional[Dict[str, Any]]:
    p = session_file(sid)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _brief(text: Any, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


VIEW_DIR = config.PROJECT_ROOT / "agent_memory" / ".condensed_sessions"
# WebUI 中期过程（bridge 在回合结束时落盘：agent 的 stdout 行按轮分组）。
# 会话文件里只有模型产物，"中期输出"是进程 stdout —— 不落盘的话刷新后就没了，
# 而主人 2026-09-23 要求刷新前后逐字一致。这里只读，写入在 bridge 侧。
TURNS_DIR = config.PROJECT_ROOT / "agent_memory" / ".webui_turns"


def _ctx_meta(sid: str, msgs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """上下文视图元数据（agent/context_manager.py 落盘，只读）。

    视图 = 原文的确定性函数，缺文件即未压缩过；压缩次数从事件流实算（不缓存、不猜）。
    msgs 用于给出「原文占用估算」，让侧栏圆圈在没压缩过的会话上也有数。
    """
    out: Dict[str, Any] = {}
    try:
        vf = VIEW_DIR / f"{sid}.json"
        if vf.exists():
            v = json.loads(vf.read_text(encoding="utf-8"))
            st = v.get("stats") or {}
            tk = v.get("tokens") or {}
            out = {"version": v.get("version"), "source_len": v.get("source_len"),
                   "rounds": st.get("rounds"), "saved_ratio": st.get("saved_ratio"),
                   "est_before": tk.get("est_before"), "est_after": tk.get("est_after"),
                   "kept_rounds": v.get("kept_rounds"),
                   "last_compact_at": v.get("created_at")}
        ev = VIEW_DIR / f"{sid}.events.jsonl"
        if ev.exists():
            n = 0
            with open(ev, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        if json.loads(line).get("event") == "compact":
                            n += 1
                    except Exception:
                        continue
            out["compactions"] = n
        if msgs is not None:
            # 口径与 agent/context_manager.est_tokens 一致（cl100k 对中文高估 ≈1.8 倍，
            # 故按 1 字符 ≈ 0.5 token 折算）；只是给圆圈的兜底量，真实值以 usage 为准
            out["est_original"] = len(json.dumps(msgs, ensure_ascii=False)) // 2
    except Exception:
        return out
    return out


def list_sessions() -> Dict[str, Any]:
    d = config.WORKING_MEMORY_DIR
    items: List[Dict[str, Any]] = []
    if not d.exists():
        return {"sessions": [], "dir": str(d), "total": 0}
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            items.append({"session_id": p.stem, "unreadable": True,
                          "message_count": 0, "status": "unknown",
                          "summary": "（文件无法解析）"})
            continue
        msgs = [m for m in (data.get("messages") or []) if m.get("role") != "system"]
        # 会话卡片的默认标题取"第一条用户消息" —— 但那三类消息**不是主人说的**
        # （引擎代投：权限模式切换通知 / 用户交代 / 后台作业交付），必须跳过。
        # 实测反馈：切一次模式，会话标题就变成「🔐 [权限模式] 主人把本会话…」。
        first_user = next((m for m in msgs if m.get("role") == "user"
                           and not _is_injected_user_msg(m.get("content"))), None)
        tool_calls = sum(len(m.get("tool_calls") or []) for m in msgs
                         if m.get("role") == "assistant")
        st = p.stat()
        items.append({
            "session_id": data.get("session_id") or p.stem,
            "file": p.name,
            "message_count": data.get("message_count", len(msgs)),
            "user_turns": sum(1 for m in msgs if m.get("role") == "user"),
            "tool_calls": tool_calls,
            "status": data.get("status", "complete"),
            "status_label": STATUS_LABEL.get(data.get("status", "complete"), "未知"),
            "has_snapshot": bool(data.get("system_prompt")),
            "summary": _brief(first_user.get("content")) if first_user else "（空会话）",
            "last_activity": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "saved_at": data.get("created_at"),
            "size": st.st_size,
            "ctx": _ctx_meta(p.stem, msgs),   # 视图元数据 + 原文占用估算（未压缩过也有数）
        })
    return {"sessions": items, "total": len(items), "dir": str(d)}


def _full(text: Any, n: int = 4000) -> str:
    """展开查看用的原文：**保留换行**（工具返回里的换行是有意义的），只做长度上限。

    与 _brief 的分工：_brief 把所有空白压成空格，那是给一行摘要用的；
    这里供折叠块展开后的 <pre> 使用，压平了就没法读。
    上限 4000 字符：主人 2026-09-23 定（Hermes 给全文，我们保一个可控上限）。
    """
    s = str(text or "").strip()
    return s[:n] + (f"…（已截断，全文 {len(s)} 字符）" if len(s) > n else "")


def _args_full(raw: Any, n: int = 4000) -> str:
    """工具参数原文（展开查看用）：是 JSON 就美化缩进，否则原样；只做长度上限。"""
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
        s = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        s = str(raw or "")
    s = s.strip()
    return s[:n] + (f"…（已截断，全文 {len(s)} 字符）" if len(s) > n else "")


def split_attachments(content) -> Any:
    """把正文里的【附件】标记行剥出来 → (纯正文, [{"name":.., "path":..}, ...])。

    为什么要剥：消息落盘时附件是**行内标记**（那样 agent 侧只需认一种格式），
    但界面上不该把 `【附件】name=x.png | path=...` 当正文显示出来。
    标记格式的唯一真相源是 mid_turn.ATT_MARK —— 这里不复制常量，直接引用，
    免得两边各改一半（本项目已经吃过"名同实异"的亏）。
    """
    if not isinstance(content, str):
        return content, []
    try:
        import mid_turn as _mt                       # 同目录；延迟导入避免拉起顺序问题
        mark = _mt.ATT_MARK
    except Exception:
        mark = "【附件】"
    if mark not in content:
        return content, []
    body: List[str] = []
    atts: List[Dict[str, str]] = []
    for ln in content.split("\n"):
        s = ln.strip()
        if s.startswith(mark):
            meta: Dict[str, str] = {}
            for seg in s[len(mark):].split("|"):
                if "=" in seg:
                    k, _, v = seg.partition("=")
                    meta[k.strip().lower()] = v.strip()
            p = meta.get("path", "")
            if p:
                atts.append({"name": meta.get("name") or p.rsplit("/", 1)[-1], "path": p})
            continue
        body.append(ln)
    return "\n".join(body).strip(), atts


def _one_sidecar_turn(t: Dict[str, Any]) -> Dict[str, Any]:
    """sidecar 里的一条回合 → 前端结构（outs 逐条整形）。"""
    outs = []
    for o in (t.get("outs") or []):
        if isinstance(o, dict):
            outs.append({"round": int(o.get("round") or 0),
                         "text": str(o.get("text") or "")})
    item: Dict[str, Any] = {"user_seq": int(t.get("user_seq") or 0),
                            "run_id": str(t.get("run_id") or ""),
                            "ended": str(t.get("ended") or "done"),
                            "at": t.get("at"), "outs": outs}
    # 进行中快照带的每轮思考原文（round -> 文本）。老 bridge 不写这个键 → 不带，
    # 前端自然回退到"用会话文件里的 reasoning_content"，不做空壳。
    rc = t.get("reasoning")
    if isinstance(rc, dict) and rc:
        item["reasoning"] = {str(k): str(v) for k, v in rc.items() if v}
    if t.get("partial"):
        item["partial"] = True
    return item


def _turn_sidecar(sid: str) -> List[Dict[str, Any]]:
    """WebUI 侧的中期过程（bridge 落盘）：每个回合的 stdout 行 + 它属于第几轮。

    只有 WebUI 跑的回合才有（CLI 的回合、以及 2026-09-23 之前的回合没有）——
    读不到一律返回空列表，界面退回"只有思考与工具"的形态。
    """
    try:
        f = TURNS_DIR / f"{sid}.json"
        if not f.exists():
            return []
        data = json.loads(f.read_text(encoding="utf-8"))
        return [_one_sidecar_turn(t) for t in (data.get("turns") or [])
                if isinstance(t, dict)]
    except Exception:
        return []


def _partial_turn(sid: str) -> Optional[Dict[str, Any]]:
    """**进行中**回合并未收尾的那半条（bridge 节流落盘，2026-10-02）。

    为什么单独给一条：sidecar 的 `turns` 只装**已收尾**的回合，而刷新/切会话时
    主人正在跑的那个回合恰恰不在里面 —— 于是"已经产出的中期输出"当场消失，
    而这个回合其实还在跑。它落在 sidecar 的 `partial` 键上（收尾时被摘掉）。

    返回 None = 没有进行中的回合（或老 bridge 不写这个键）→ 前端行为完全不变。
    """
    try:
        f = TURNS_DIR / f"{sid}.json"
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        p = data.get("partial")
        if not isinstance(p, dict) or not p.get("outs"):
            return None
        return _one_sidecar_turn(p)
    except Exception:
        return None


def _mode_catalog() -> List[Dict[str, Any]]:
    """权限模式清单（含每档一句话状态）：界面那块"当前状态块"要用它。

    与模型每轮拿到的那行**同源**（permission_modes.STATUS）—— 界面块与提示词各写
    一份文案必然漂移，而这两处一旦不一致，主人看到的和 AB 以为的就成了两回事。
    """
    try:
        import permission_modes as pm
        return pm.catalog()
    except Exception:
        return []


def get_history(sid: str, limit: int = 400) -> Dict[str, Any]:
    """还原会话消息：user/assistant 正文 + 工具调用与返回配对。"""
    data = _load_raw(sid)
    if data is None:
        return {"session_id": sid, "exists": False, "messages": [], "turns": [],
                "partial": None}
    raw_msgs = [m for m in (data.get("messages") or []) if m.get("role") != "system"]

    tool_index: Dict[str, Dict[str, Any]] = {}
    for m in raw_msgs:
        if m.get("role") == "tool":
            tool_index[str(m.get("tool_call_id"))] = m

    out: List[Dict[str, Any]] = []
    for m in raw_msgs:
        role = m.get("role")
        if role == "tool":
            continue
        _clean, _atts = split_attachments(m.get("content") or "")
        item: Dict[str, Any] = {"role": role, "content": _clean}
        if _atts:
            # 附件作为独立字段给前端渲染成小标签；content 保持纯正文
            item["attachments"] = _atts
        if role == "assistant":
            # 这一轮模型的思考原文。context_manager 只保留最近 N 轮的 reasoning_content，
            # 更早的被清掉 —— 取不到就不放这个键，前端据此不渲染「思考过程」块（不做空壳）。
            _rc = str(m.get("reasoning_content") or "").strip()
            if _rc:
                item["reasoning"] = _rc
        calls = m.get("tool_calls") or []
        if calls:
            tcs = []
            for tc in calls:
                fn = (tc or {}).get("function") or {}
                cid = str(tc.get("id") or "")
                ret = tool_index.get(cid)
                ret_text = (ret or {}).get("content")
                tcs.append({
                    "id": cid,
                    "name": fn.get("name") or "?",
                    "args": _args_full(fn.get("arguments")),
                    "result": _full(ret_text) if ret_text is not None else None,
                    "failed": is_business_failure(ret_text),
                    "pending": ret_text is None,
                })
            item["tool_calls"] = tcs
            # 时间线兼容字段（前端复用同一渲染器）
            item["timeline"] = [{
                "tool": t["name"], "args": t["args"], "result": t["result"],
                "ok": not t["failed"] and not t["pending"], "error": None,
                "elapsed": None, "call_id": t["id"], "source": "history",
            } for t in tcs]
        if role == "assistant" and not (m.get("content") or "").strip() and not calls:
            continue
        out.append(item)

    if len(out) > limit:
        out = out[-limit:]
    return {
        "session_id": data.get("session_id") or sid,
        "exists": True,
        "status": data.get("status", "complete"),
        "status_label": STATUS_LABEL.get(data.get("status", "complete"), "未知"),
        "message_count": len(out),
        "saved_at": data.get("created_at"),
        "has_snapshot": bool(data.get("system_prompt")),
        "snapshot_head": _brief(data.get("system_prompt"), 160) if data.get("system_prompt") else None,
        # 会话级权限模式（2026-10）：界面开面板时要显示"现在是哪一档"。
        # 缺字段 = 本次优化之前的旧会话 → 普通档（引擎侧默认值也是它）。
        "permission_mode": str(data.get("permission_mode") or "normal"),
        # 四档清单 + 每档一句话状态（状态块与模型那行共用这份文案）
        "permission_catalog": _mode_catalog(),
        "messages": out,
        # 中期过程（按 user_seq 对齐到回合）：前端拿它把"中期输出"插回正确的轮里
        "turns": _turn_sidecar(sid),
        # 进行中回合的半个快照（可能有）：刷新/切会话时把"已经产出的东西"画回来
        # —— 它与 turns 里那条同名回合**互斥**（收尾时 partial 被摘掉、正式条目补上）
        "partial": _partial_turn(sid),
    }


def set_permission_mode(sid: str, mode: str) -> Dict[str, Any]:
    """把权限模式写进会话文件 —— **只在 bridge 没在跑时**由网关调用。

    为什么两条写路径而不是一条：AB 在跑时，模式的权威在 bridge 进程里（引擎就在那儿），
    由它写（顺带追加一条切换通知，并按"有没有并发回合"决定当场落盘还是排队）；
    AB 没开机时用户仍然可以预选模式，这一档要活到下次开机 —— 那就得由网关写进文件，
    `agent.load_session` 开机加载会话时会把它读回引擎。

    两个写者同时写同一个 JSON 必然互相覆盖，所以调用方（api.py）先问 bridge：
    只有 bridge 不可达才走这里。
    """
    sid = validate_session_id(sid)
    data = _load_raw(sid)
    if data is None:
        return {"ok": False, "error": "会话不存在"}
    data["permission_mode"] = str(mode)
    src = session_file(sid)
    tmp = src.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())          # 与 agent.save_session 同款：先落盘再替换
        tmp.replace(src)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "session_id": sid, "permission_mode": str(mode)}


def create_session_id(prefix: str = "session") -> str:
    base = f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    sid, n = base, 0
    while session_file(sid).exists():
        n += 1
        sid = f"{base}_{n}"
    return sid


def delete_session(sid: str) -> Dict[str, Any]:
    """删除 = 把该会话的**全部血缘**移入 agent_webui/.trash/（可恢复），绝不物理删除。

    血缘三件套（主人 2026-09-13 要求统一管上）：
      1. working_memory/{sid}.json              对话原文（唯一真相）
      2. .condensed_sessions/{sid}.json         上下文视图（原文的派生物）
      3. .condensed_sessions/{sid}.events.jsonl 压缩事件流
      4. .webui_turns/{sid}.json                WebUI 中期过程（2026-09-23 加入）
    视图与原文有血缘：只删原文、留下视图，就会冒出"没有会话却有视图"的孤儿——
    下次同名会话一出现，那份视图还可能被误认成它的历史。所以三件一起走。
    """
    src = session_file(sid)
    if not src.exists():
        return {"ok": False, "error": "会话不存在"}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved: List[Dict[str, str]] = []
    warn: List[str] = []

    # 1) 对话原文（主体，缺它就算删除失败）
    t1 = config.WEBUI_DIR / ".trash" / "working_memory"
    t1.mkdir(parents=True, exist_ok=True)
    d1 = t1 / f"{src.stem}.{stamp}.json"
    shutil.move(str(src), str(d1))
    moved.append({"kind": "session", "to": str(d1.relative_to(config.WEBUI_DIR))})

    # 2) 上下文视图 + 事件流（有则一起走；移不动只记警告，不影响会话已删的事实）
    t2 = config.WEBUI_DIR / ".trash" / "condensed_sessions"
    for p, kind in ((VIEW_DIR / f"{sid}.json", "view"),
                    (VIEW_DIR / f"{sid}.events.jsonl", "view_events")):
        if not p.exists():
            continue
        try:
            t2.mkdir(parents=True, exist_ok=True)
            dst = t2 / f"{p.stem}.{stamp}{p.suffix}"
            shutil.move(str(p), str(dst))
            moved.append({"kind": kind, "to": str(dst.relative_to(config.WEBUI_DIR))})
        except Exception as e:
            warn.append(f"{kind} 未随会话移走（{p.name}）: {e.__class__.__name__}: {e}")

    # 3) WebUI 中期过程（同样是该会话的派生物，别留孤儿）
    p3 = TURNS_DIR / f"{sid}.json"
    if p3.exists():
        try:
            t3 = config.WEBUI_DIR / ".trash" / "webui_turns"
            t3.mkdir(parents=True, exist_ok=True)
            dst3 = t3 / f"{p3.stem}.{stamp}{p3.suffix}"
            shutil.move(str(p3), str(dst3))
            moved.append({"kind": "webui_turns", "to": str(dst3.relative_to(config.WEBUI_DIR))})
        except Exception as e:
            warn.append(f"webui_turns 未随会话移走（{p3.name}）: {e.__class__.__name__}: {e}")

    out: Dict[str, Any] = {"ok": True, "session_id": src.stem,
                           "moved_to": str(d1.relative_to(config.WEBUI_DIR)),
                           "moved": moved}
    if warn:
        out["warnings"] = warn
    return out
