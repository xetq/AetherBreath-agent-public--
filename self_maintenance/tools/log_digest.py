#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日志摘要 —— 把两轨日志压成「一眼能看出问题」的一页（纯标准库）

AB 的日志是**两轨异构**的，排查时要人肉拼接：
    agent_logs/{sid}_{date}.jsonl     技术事件（脱敏、DEBUG 按 10% 采样）
    agent_memory/working_memory/{sid}.json   对话原文
再加一本跨会话的审批账本 agent_logs/approval-YYYYMM.jsonl。
这个脚本把它们按会话对齐，**默认只吐异常骨架 + 统计**，把重复噪音压成 "N× 同一件事"。

为什么默认只吐骨架：实测一个项目周期的日志里，INFO 39682 条 / WARNING 878 条，
而 WARNING 里 371 条是**同一条**编排器消息、268 条是同款技能提示。
原样倒出来既烧 token 又淹掉真信号 —— 一眼要能看出的是"有什么不对"，不是"发生过什么"。

用法：
    python self_maintenance/tools/log_digest.py --list            # 最近会话一览（先看这个）
    python self_maintenance/tools/log_digest.py --latest          # 最新会话的摘要
    python self_maintenance/tools/log_digest.py --session session_20260917_190934
    python self_maintenance/tools/log_digest.py --latest --full   # 展开全部事件（慎用，很大）
    python self_maintenance/tools/log_digest.py --latest --dialog # 附带对话正文摘要

诚实说明两件事：
  1) 审批账本**没有 session 字段**（只有 ts），所以账本事件是按"会话时间窗口"落进来的，
     输出里会标「按时间推断」；会话日志里带 tool 的审批痕迹才是硬关联。
  2) DEBUG 日志按 LOG_SAMPLING_RATE（默认 0.1）采样，logger 收尾时会写一条采样摘要。
     "日志里没有"不等于"没发生"—— 骨架里会把采样丢失量明确写出来。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = PROJECT_ROOT / "agent_logs"
WM_DIR = PROJECT_ROOT / "agent_memory" / "working_memory"

# 关键 INFO 级事件：它们不是 WARNING，但同样是"要一眼看到"的东西
KEY_PATTERNS = (
    "失败", "超时", "中断", "异常", "未通过", "拒绝", "幽灵", "回滚", "未能",
    "超过最大迭代", "Connection error", "Traceback",
)

# 审批账本里"纯内部"的事件类型（据 2026-09-17 实测分布设定：inspect 1856 条、
# self_check 101 条、lex_fallback 98 条 …）。默认折叠它们，让
# spec_block / payload_hit / verdict / batch_* 这些**有决策意义**的浮上来。
LEDGER_INTERNAL = {
    "inspect",          # 逐次判定留痕
    "self_check",       # 每次启动自检
    "lex_fallback",     # 词法层降级
    "opaque_body",      # 读不懂的代码体（风险提示，但量大）
    "quiet_allow",      # 免打扰放行
    "scope_allow",      # 作用域免审放行
    "tool_spec_hit",    # 无路径工具按名匹配
    "pass",             # 放行
    "answer_probe",     # 应答探针
}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def session_log_files(sid: str) -> List[Path]:
    """一个会话可能跨天，日志按 {sid}_{date}.jsonl 分文件。

    回退：历史日志里存在 sid 脏数据（`ession_20260903_102143` —— 少了首字母 s，
    文件名却是完整的），精确匹配会查不到，所以再试一次包含匹配。
    """
    exact = sorted(LOG_DIR.glob(f"{sid}_*.jsonl"))
    if exact:
        return exact
    return sorted(p for p in LOG_DIR.glob(f"*{sid}*.jsonl") if not p.name.startswith("approval"))


def session_events(sid: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for f in session_log_files(sid):
        events.extend(_read_jsonl(f))
    events.sort(key=lambda d: str(d.get("ts", "")))
    return events


def list_sessions(limit: int = 20) -> List[Tuple[str, str, int, int, int]]:
    """扫全部会话日志：(sid, 最后时间, 事件数, ERROR 数, 去重后异常类数)。"""
    agg: Dict[str, Dict[str, Any]] = {}
    for f in LOG_DIR.glob("*_20*.jsonl"):
        if f.name.startswith("approval"):
            continue
        for d in _read_jsonl(f):
            sid = d.get("sid") or ""
            if not sid:
                continue
            a = agg.setdefault(sid, {"ts": "", "n": 0, "err": 0, "sigs": set()})
            a["n"] += 1
            ts = str(d.get("ts", ""))
            if ts > a["ts"]:
                a["ts"] = ts
            lvl = d.get("lvl")
            if lvl == "ERROR":
                a["err"] += 1
            if lvl in ("WARNING", "ERROR"):
                a["sigs"].add(signature(str(d.get("msg", ""))))
    rows = [(sid, v["ts"], v["n"], v["err"], len(v["sigs"])) for sid, v in agg.items()]
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def approval_ledger(start_ts: str, end_ts: str) -> List[Dict[str, Any]]:
    """审批账本按**时间窗口**筛（账本没有 sid，这点无法回避）。"""
    out: List[Dict[str, Any]] = []
    if not start_ts or not end_ts:
        return out
    lo, hi = start_ts[:19], end_ts[:19]
    for f in sorted(LOG_DIR.glob("approval-*.jsonl")):
        for d in _read_jsonl(f):
            iso = str(d.get("iso", ""))
            if lo <= iso <= hi:
                out.append(d)
    return out


def signature(msg: str) -> str:
    """把消息压成"形态签名"，用于聚类重复项。

    例：`技能扫描提示: [browser-use] frontmatter 缺少 version，默认 0.1.0`
      → `技能扫描提示: […] frontmatter 缺少 version，默认 <N>.<N>.<N>`
    方括号内容、数字、路径、引号内容都归一 —— 同一件事重复 N 次只该占一行。
    """
    s = msg.replace("\n", " ")
    s = re.sub(r"[A-Za-z]:[\\/][^\s'\"|]+", "<路径>", s)
    s = re.sub(r"(?<![\w])/[\w./-]{6,}", "<路径>", s)
    s = re.sub(r"\[[^\]]{0,80}\]", "[…]", s)
    s = re.sub(r"'[^']{0,80}'", "'…'", s)
    s = re.sub(r"\d+", "<N>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:100]


def dialog_of(sid: str, n_head: int = 2, n_tail: int = 6, width: int = 110) -> List[str]:
    """对话正文摘要（working_memory 原文；文件可能不存在）。"""
    p = WM_DIR / f"{sid}.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    msgs = data.get("messages") or []
    picked = msgs[:n_head] + (msgs[-n_tail:] if len(msgs) > n_head + n_tail else msgs[n_head:])
    out = []
    for m in picked:
        role = m.get("role", "?")
        c = (m.get("content") or "")
        if isinstance(c, list):
            c = " ".join(str(x) for x in c)
        c = re.sub(r"\s+", " ", str(c)).strip()
        head = {"user": "用户", "assistant": "AB", "system": "系统", "tool": "工具"}.get(role, role)
        out.append(f"    {head:<3}| {c[:width]}")
    return out


def build_digest(sid: str, full: bool = False, with_dialog: bool = False,
                 max_classes: int = 20) -> str:
    events = session_events(sid)
    L: List[str] = []
    if not events:
        return f"（没有找到会话 {sid} 的技术日志；用 --list 看有哪些会话）"

    ts0, ts1 = str(events[0].get("ts", "")), str(events[-1].get("ts", ""))
    L.append(f"■ 会话 {sid}")
    L.append(f"  时间：{ts0[:19]} → {ts1[:19]}  ｜ 事件 {len(events)} 条 ｜ "
             f"日志源 {len(session_log_files(sid))} 个文件")

    lv = Counter(str(e.get("lvl", "?")) for e in events)
    L.append(f"  分布：ERROR {lv.get('ERROR', 0)} ｜ WARNING {lv.get('WARNING', 0)} ｜ "
             f"INFO {lv.get('INFO', 0)} ｜ DEBUG {lv.get('DEBUG', 0)}")

    # ---- 异常骨架：WARNING/ERROR 聚类 + 关键 INFO ----
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in events:
        lvl = str(e.get("lvl", ""))
        msg = str(e.get("msg", ""))
        key: Optional[Tuple[str, str]] = None
        if lvl in ("ERROR", "WARNING"):
            key = (lvl, signature(msg))
        elif lvl == "INFO" and any(p in msg for p in KEY_PATTERNS):
            # 「用户输入: …」「中期进度: …」是**对话内容**不是系统异常 —— 用户或 AB
            # 自己说了"失败/卡死"之类的话就会被关键词命中，那种假异常会把真信号挤掉。
            if not msg.startswith(("用户输入", "中期进度")):
                key = ("KEY", signature(msg))
        if key is None:
            continue
        b = buckets.setdefault(key, {"n": 0, "first": str(e.get("ts", ""))[:19], "sample": msg[:200]})
        b["n"] += 1
    order = {"ERROR": 0, "WARNING": 1, "KEY": 2}
    ranked = sorted(buckets.items(), key=lambda kv: (order.get(kv[0][0], 3), -kv[1]["n"]))
    L.append("")
    if not ranked:
        L.append("  ✓ 没有 ERROR / WARNING / 关键异常 —— 干净的一页")
    else:
        n_total = sum(b["n"] for _, b in ranked)
        L.append(f"  ── 异常骨架（{len(ranked)} 类 / {n_total} 条）──")
        for (lvl, sig), b in ranked[:max_classes]:
            tag = {"ERROR": "❌", "WARNING": "⚠", "KEY": "●"}[lvl]
            L.append(f"  {b['n']:>4}× {tag} {sig}")
            if b["n"] > 1 or lvl == "ERROR":
                L.append(f"        首现 {b['first']}｜样例：{b['sample']}")
        if len(ranked) > max_classes:
            L.append(f"  …另 {len(ranked) - max_classes} 类（--full 展开）")

    # ---- 工具调用统计 ----
    # 两代日志格式：现在的用 `tool` 字段；2026-09-03 前后用 `name` + 「单工具调用开始」。
    # 不兼容旧格式的话，早期会话会显示"没调用工具"——把有工具的会话误判成空会话。
    tools: Counter = Counter()
    tool_err: Counter = Counter()
    for e in events:
        t = e.get("tool")
        if not t and e.get("name") and "工具调用" in str(e.get("msg", "")):
            t = e.get("name")
        if not t:
            continue
        tools[str(t)] += 1
        if e.get("is_error") is True or e.get("ok") is False:
            tool_err[str(t)] += 1
    L.append("")
    if tools:
        parts = [f"{t}×{n}" + (f"(失败 {tool_err[t]})" if tool_err.get(t) else "")
                 for t, n in tools.most_common(12)]
        L.append(f"  ── 工具调用 {sum(tools.values())} 次 ──")
        L.append("    " + "   ".join(parts))
    else:
        L.append("  ── 工具调用 ── 无（本会话没调用工具）")

    # ---- 审批 ----
    # 账本里有两类事件：有决策意义的（batch_ask / verdict / spec_block …）
    # 与纯内部的（inspect 逐次判定 / lex_fallback / opaque_body …）。
    # 实测 inspect 一个会话 117 条 —— 全列出来就把真信号埋了，默认折叠内部类。
    led = approval_ledger(ts0, ts1)
    kinds = Counter(str(d.get("event", "?")) for d in led)
    decision = {k: n for k, n in kinds.items() if k not in LEDGER_INTERNAL}
    internal = {k: n for k, n in kinds.items() if k in LEDGER_INTERNAL}
    L.append("")
    L.append("  ── 审批 ──")
    if kinds:
        if decision:
            L.append("    决策：" + "   ".join(f"{k}×{n}" for k, n in
                                              sorted(decision.items(), key=lambda kv: -kv[1])))
        if internal:
            L.append(f"    内部：{len(internal)} 类 / {sum(internal.values())} 条已折叠"
                     f"（{'、'.join(sorted(internal))}；--full 看明细）")
        for d in [x for x in led if str(x.get("event")) not in LEDGER_INTERNAL][:3]:
            tgt = d.get("targets") or d.get("spec") or ""
            L.append(f"    · {str(d.get('iso',''))[:19]} {d.get('event')} {str(tgt)[:120]}")
        L.append("    （账本无 session 字段，按会话时间窗口推断）")
    else:
        L.append("    本会话窗口内无账本事件")

    # ---- 采样提示 ----
    for e in events:
        if "采样摘要" in str(e.get("msg", "")):
            L.append("")
            L.append(f"  ⚠ {str(e.get('msg'))[:160]}")
            break

    if with_dialog:
        d = dialog_of(sid)
        L.append("")
        L.append(f"  ── 对话（working_memory，共 {len(d)} 行节选）──")
        L.extend(d if d else ["    （没有对话原文文件）"])

    if full:
        L.append("")
        L.append(f"  ── 全部事件（{len(events)} 条）──")
        for e in events:
            extra = {k: v for k, v in e.items() if k not in ("ts", "lvl", "sid", "tid", "msg")}
            L.append(f"    {str(e.get('ts',''))[:23]} [{e.get('lvl')}] {str(e.get('msg',''))[:150]}"
                     + (f"  {json.dumps(extra, ensure_ascii=False)[:150]}" if extra else ""))

    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AB 日志摘要：异常骨架 + 统计（默认不刷全量）")
    ap.add_argument("--list", nargs="?", const=20, type=int, metavar="N",
                    help="列出最近 N 个会话（默认 20）：看哪个会话有异常")
    ap.add_argument("--session", help="会话 ID")
    ap.add_argument("--latest", action="store_true", help="最新会话")
    ap.add_argument("--full", action="store_true", help="展开全部事件（很大，慎用）")
    ap.add_argument("--dialog", action="store_true", help="附带对话正文摘要")
    ap.add_argument("--max-classes", type=int, default=20, help="骨架最多显示几类")
    args = ap.parse_args(argv)

    if args.session is None and not args.latest and args.list is None:
        args.latest = True

    if args.list is not None:
        rows = list_sessions(args.list)
        if not rows:
            print("（agent_logs 里没有会话日志）")
            return 0
        print(f"{'会话':<34} {'最后活动':<20} {'事件':>6} {'ERROR':>6} {'异常类':>6}")
        for sid, ts, n, err, sigs in rows:
            flag = "  ← 有 ERROR" if err else ""
            print(f"{sid:<34} {ts[:19]:<20} {n:>6} {err:>6} {sigs:>6}{flag}")
        print("\n提示：异常类 = 去掉重复后的 WARNING/ERROR 形态数（越小越干净）")
        return 0

    sid = args.session
    if args.latest:
        wm = sorted(WM_DIR.glob("session_*.json"), key=lambda p: p.stat().st_mtime)
        log_files = sorted(LOG_DIR.glob("*_20*.jsonl"), key=lambda p: p.stat().st_mtime)
        cand = []
        if wm:
            cand.append(wm[-1].stem)
        if log_files:
            cand.append(log_files[-1].name.split("_20")[0])
        if not cand:
            print("（没有找到任何会话）")
            return 1
        sid = cand[0]
    print(build_digest(sid, full=args.full, with_dialog=args.dialog,
                       max_classes=args.max_classes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
