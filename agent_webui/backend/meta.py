# -*- coding: utf-8 -*-
"""会话标题与搜索（WebUI 侧车层）。

为什么不写进会话 JSON：那 6 个键由 agent.save_session 定义并全量覆盖写，
往里面塞 title 需要改 AB 本体源码 —— 这里用侧车文件避开，零侵入。

存放：working_memory/_meta/titles.json
  sessions.list_sessions() 用 glob("*.json") 非递归扫描，子目录不会被误当成会话。
结构：{ "<session_id>": {"title": str, "renamed_at": iso} }
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import config
import sessions

MAX_TITLE_LEN = 60
META_SUBDIR = "_meta"
SNIPPET = 100          # 命中片段左右各取多少字符
MAX_HITS_PER_SESSION = 30


# ---------------- 侧车读写 ----------------
def _titles_file() -> Path:
    return config.WORKING_MEMORY_DIR / META_SUBDIR / "titles.json"


def clean_title(raw: Any) -> str:
    s = re.sub(r"[\r\n\t]+", " ", str(raw or ""))
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:MAX_TITLE_LEN]


def get_titles() -> Dict[str, Any]:
    p = _titles_file()
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_titles(mapping: Dict[str, Any]) -> None:
    p = _titles_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=1, sort_keys=True)
    tmp.replace(p)          # 原子替换，与 agent.save_session 同风格


def set_title(sid: str, title: Any) -> Dict[str, Any]:
    sid = sessions.validate_session_id(sid)
    t = clean_title(title)
    mapping = get_titles()
    if t:
        mapping[sid] = {"title": t, "renamed_at": datetime.now().isoformat(timespec="seconds")}
    else:
        mapping.pop(sid, None)
    _save_titles(mapping)
    return {"ok": True, "session_id": sid, "title": t}


def drop_title(sid: str) -> None:
    try:
        sid = sessions.validate_session_id(sid)
    except ValueError:
        return
    mapping = get_titles()
    if sid in mapping:
        mapping.pop(sid, None)
        _save_titles(mapping)


def merge_titles(listed: Dict[str, Any]) -> Dict[str, Any]:
    """把标题并进入会话列表：title=用户命名，display=界面该显示的那个。"""
    mapping = get_titles()
    items = listed.get("sessions") or []
    seen = set()
    for item in items:
        sid = item.get("session_id") or ""
        seen.add(sid)
        t = (mapping.get(sid) or {}).get("title")
        item["title"] = t or ""
        # 主人 2026-09-13 定：界面标题永不显示 session_id（标题 = 自定义 → 自动摘要）。
        # 会话 ID 唯一查看路径是「对话上方」（ChatView 头部），卡片里只在圆圈悬停可见。
        item["display"] = t or item.get("summary") or "（空会话）"
    # 新建会话只生成 id，首条消息才会由 agent.save_session 写出 JSON —— 不补就会
    # 出现「输了标题却看不见会话」的割裂感，故把这类占位条目也列出来。
    pending = 0
    for sid, meta in sorted(mapping.items()):
        if sid in seen or not (meta or {}).get("title"):
            continue
        items.append({"session_id": sid, "title": clean_title(meta["title"]),
                      "display": clean_title(meta["title"]),
                      "summary": "（尚无消息，发第一条后落盘）", "message_count": 0,
                      "status": "active", "status_label": "待开始",
                      "last_activity": meta.get("renamed_at"), "pending": True})
        pending += 1
    items.sort(key=lambda x: x.get("last_activity") or "", reverse=True)
    listed["sessions"] = items
    listed["total"] = len(items)
    listed["pending_titled"] = pending
    return listed


# ---------------- 搜索 ----------------
def _window(text: str, pos: int, needle_lower: str) -> str:
    lo = max(0, pos - SNIPPET)
    hi = min(len(text), pos + len(needle_lower) + SNIPPET)
    seg = re.sub(r"\s+", " ", text[lo:hi]).strip()
    return ("…" if lo > 0 else "") + seg + ("…" if hi < len(text) else "")


def search(q: str, scope: str = "all", status: str = "", limit: int = 30) -> Dict[str, Any]:
    """同时匹配【会话标题】与【会话记录】。

    scope: all | title | content      status: 可选按会话状态过滤
    标题/ id 命中排在前面（更接近"我要找那个会话"的意图）。
    """
    needle = re.sub(r"\s{2,}", " ", str(q or "")).strip()
    if not needle:
        return {"ok": True, "query": "", "matched": 0, "scanned": 0, "matches": []}
    low = needle.lower()
    scope = (scope or "all").lower()
    listed = merge_titles(sessions.list_sessions())
    matches: List[Dict[str, Any]] = []

    for item in listed.get("sessions") or []:
        sid = item.get("session_id") or ""
        if status and item.get("status") != status:
            continue
        title = item.get("title") or ""
        hits: List[Dict[str, Any]] = []

        if scope in ("all", "title") and (low in title.lower() or low in sid.lower()):
            hits.append({"field": "title",
                         "snippet": title or sid,
                         "at": 0, "role": "title"})

        if scope in ("all", "content"):
            data = sessions._load_raw(sid) or {}
            for idx, msg in enumerate(data.get("messages") or []):
                content = str(msg.get("content") or "")
                if not content:
                    continue
                pos = content.lower().find(low)
                if pos < 0:
                    continue
                hits.append({"field": "content", "at": idx,
                             "role": msg.get("role") or "?",
                             "snippet": _window(content, pos, low)})
                if len(hits) >= MAX_HITS_PER_SESSION:
                    break

        if not hits:
            continue
        title_hit = hits[0]["field"] == "title"
        matches.append({
            "session_id": sid,
            "title": title,
            "display": item.get("display"),
            "summary": item.get("summary"),
            "status": item.get("status"),
            "status_label": item.get("status_label"),
            "message_count": item.get("message_count"),
            "last_activity": item.get("last_activity"),
            "title_matched": title_hit,
            "hit_count": len(hits),
            "hits": hits[:4],
        })

    # 两次稳定排序：先按最近活跃倒序，再把"标题命中"整体顶到前面
    matches.sort(key=lambda m: m.get("last_activity") or "", reverse=True)
    matches.sort(key=lambda m: not m["title_matched"])
    return {"ok": True, "query": needle, "scope": scope or "all",
            "status": status or "", "scanned": len(listed.get("sessions") or []),
            "matched": len(matches), "matches": matches[:max(1, min(limit, 100))]}
