# -*- coding: utf-8 -*-
"""规范：系统盘（C 盘）文件的写入 / 删除 / 移动 需要人工审批。

KIND 沿用 "cdrive.files" —— 账本与免审规则里已有这个标识，改名会断链。

与旧版（cdrive_files.py）的**唯一实质差别**是目标的取法：
    旧：全段扫到动词 + 全段扫到路径  →  共现即成立
    新：目标必须是「某个动词的直接实参」→  见 approvals/_impact.py

这一步是为真实误报做的（1732 条历史调用重放出的三条）：
  · `cmd.lower().replace('a','b')` → 曾被判「移动文件」
  · 脚本里 `SD = '盘符'` 常量      → 曾被判「对盘符执行写入」，任务与系统盘无关
  · `cp 盘内/x D:/backup/`         → 曾被判「对盘内/x 写入」，其实只读了它

读操作仍然不问：venv、解释器、site-packages 全在系统盘，一次 import 上百次读，
读也弹会把人训练成闭眼点「允许」，那比没审批更危险。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import _impact as I          # 同目录共享层（loader 跳过下划线文件，它不成为规范）

BS = chr(92)

KIND = "cdrive.files"
TITLE = "在系统盘改动文件"
RISK = 2

# ---- 系统盘内免打扰：这些位置的写删移不打扰（但仍记账） ----
QUIET: tuple = (
    "/appdata/local/temp/", "/appdata/local/pip/cache/", "/appdata/local/npm-cache/",
    "/appdata/local/uv/", "/appdata/local/crashdumps/", "/pip/cache/", "/temp/",
    "/venv/", "/venv-gateway/", "/node_modules/", "/__pycache__/",
    "/appdata/local/microsoft/windows/inbox/", "/appdata/local/microsoft/clr",
)

# ---- 关键区：写删移它们要额外提示（同一审批，措辞更重） ----
CRITICAL: tuple = (
    "/windows/", "/winnt/", "/program files", "/program files (x86)/",
    "/programdata/", "/boot/", "/recovery/", "/efi/",
    "/startup/", "/start menu/programs/startup/",
)


def _hard(targets: List[str], ctx: Dict[str, Any]) -> str:
    """路径片段型绝对禁区：读它合法，写它才致命，所以必须带动作来判。"""
    pats = ctx.get("forbidden_path") or ()
    if not pats or not targets:
        return ""
    for t in targets:
        for pat in pats:
            frag = "/".join(pat.lower().split(BS))
            if frag and (("/" + frag) in t or t.endswith(frag)):
                return pat
    return ""


def applies(ctx: Dict[str, Any]) -> bool:
    """便宜的初筛：本盘内出现过任何一个候选路径就值得看一眼。动作在 finding 里判。"""
    # 初筛只管「值不值得看一眼」，区判定留在 finding()：那边才会把动作点实参
    # 完整解析成绝对路径。这里若依赖 ctx["paths"]（旧全文正则的产物）就会漏掉
    # 一切相对路径写法 —— 实测 io.open('agent/approval.py','w') 这样整条静默放行。
    if ctx.get("actions"):
        return True
    d = ctx.get("drive") or ""
    if any(p.startswith(d) for p in (ctx.get("paths") or [])):
        return True
    # 读不懂的代码体里出现过本盘路径：值得问一句（不是定罪，是让主人看见它）
    return any(p.startswith(d) for p in (ctx.get("opaque_paths") or []))


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d = ctx.get("drive") or ""
    acts = ctx.get("actions") or []
    if not acts:
        return _legacy(ctx, d)          # 词法层不可用/strict 档：退回共现，宁误拦不漏拦

    hits: List[Dict[str, Any]] = []
    opaque: List[str] = []
    for a in acts:
        if a.get("opaque"):
            opaque.append(str(a.get("word") or "?"))
            continue
        paths, how = I.targets_of(a)
        drive_paths = [p for p in paths if p.startswith(d)]
        if not drive_paths:
            continue
        cat = I.category_of(str(a.get("word") or ""))
        if not cat and (a.get("redirect") or a.get("mode")):
            cat = "写入/覆盖"
        if not cat:
            cat = "改动（动作类型未能确定）"
        hits.append({"verb": a.get("word"), "cat": cat, "targets": drive_paths,
                     "how": how or "全部实参"})

    if not hits and not opaque:
        op_paths = [p for p in (ctx.get("opaque_paths") or []) if p.startswith(d)]
        if op_paths:
            return {"quiet": False, "action": "无法确定", "targets": op_paths,
                    "intent": "执行一段读不懂的代码，其中出现系统盘路径",
                    "reason": "词法层解析不出这段代码的结构，无法把动词与文件绑定；"
                              "列出的路径只是提示，不是已确认的操作目标",
                    "notes": ["⚠️ 目标未能确定（代码读不懂）—— 请展开「操作源代码」自己看一眼"],
                    "critical": True}
        return None                     # 没有任何动词要吃系统盘的文件

    targets: List[str] = []
    for h in hits:
        for p in h["targets"]:
            if p not in targets:
                targets.append(p)

    hard = _hard(targets, ctx)
    if hard:
        return {"block": True, "targets": targets,
                "intent": "改写不可授权的关键系统文件",
                "reason": "%s 命中关键系统文件（%s），此类操作不提供授权入口"
                          % (targets[0], hard[-12:].rstrip(BS))}

    if targets and len([p for p in targets if any(q in p + "/" for q in QUIET)]) == len(targets):
        return {"quiet": True, "action": hits[0]["cat"], "targets": targets,
                "reason": "系统盘临时/依赖目录内的%s，记账放行" % hits[0]["cat"]}
    if not targets:
        # 只有读不懂的代码体：不编目标，直接问人（旧写法会把常量当目标）
        return {"quiet": False, "action": "无法确定", "targets": [],
                "intent": "执行一段无法解析的代码体（%s）" % "、".join(sorted(set(opaque))[:3]),
                "reason": "词法层读不懂这段代码，无法确定它要动什么；按最保守处理请人工确认",
                "notes": ["⚠️ 未能确定操作目标 —— 建议先看「操作源代码」再决定"],
                "critical": True}

    cats = []
    for h in hits:
        if h["cat"] not in cats:
            cats.append(h["cat"])
    crit = [p for p in targets if any(c in p for c in CRITICAL)]
    verb_bits = "、".join("%s→%s" % (I.verb_last(h["verb"]), "/".join(
        [p.rsplit("/", 1)[-1] or p for p in h["targets"][:2]])) for h in hits[:4])
    if len(hits) > 4:
        verb_bits += " 等 %d 个动作" % len(hits)
    return {
        "quiet": False,
        "action": "/".join(cats),
        "targets": targets,
        "intent": "%s：%s" % ("/".join(cats), verb_bits),
        "reason": "%d 个动作点的位置实参落在系统盘 %s%s" % (
            len(hits), d.rstrip("/") + "/", "（位于系统关键目录）" if crit else ""),
        "notes": ["依据：%s" % "；".join(
            "%s 的目标取自%s" % (I.verb_last(h["verb"]), h["how"]) for h in hits[:3])],
        "critical": bool(crit),
    }


def _legacy(ctx: Dict[str, Any], d: str) -> Optional[Dict[str, Any]]:
    """降级路径：没有动作点时，沿用旧的「全文共现」判据。

    它比新版粗（会把提到当成要动），但只在 strict 档或代码语法过不去时启用 ——
    那种情况下宁可多问一句。
    """
    ctl = (ctx.get("control") or ctx.get("blob") or "").lower()
    f = ctx.get("facts") or {}
    words = [w.lower() for w in (f.get("cmdwords") or [])]
    cats: List[str] = []
    for cat, keys in (("删除", I.CATEGORY["删除"] + I.CATEGORY_WIN["删除"]),
                      ("移动/重命名", I.CATEGORY["移动/重命名"] + I.CATEGORY_WIN["移动/重命名"]),
                      ("写入/覆盖", I.CATEGORY["写入/覆盖"] + I.CATEGORY_WIN["写入/覆盖"])):
        if any(I.verb_last(w) in keys for w in words) or any(k + " " in ctl for k in keys):
            cats.append(cat)
    if (f.get("redirect_write") or any(c in "".join(f.get("open_modes") or []) for c in "wax+")) \
            and "写入/覆盖" not in cats:
        cats.append("写入/覆盖")
    if not cats:
        return None
    pool = ctx.get("operands") or ctx.get("paths") or []
    targets = [p for p in pool if p.startswith(d)]
    if not targets:
        return None
    hard = _hard(targets, ctx)
    if hard:
        return {"block": True, "targets": targets, "intent": "改写不可授权的关键系统文件",
                "reason": "%s 命中关键系统文件（%s）" % (targets[0], hard[-12:].rstrip(BS))}
    crit = [p for p in targets if any(c in p for c in CRITICAL)]
    return {"quiet": False, "action": "/".join(cats), "targets": targets,
            "intent": "对 %s 执行：%s" % ("、".join(targets[:3]), "/".join(cats)),
            "reason": "降级判定（词法层不可用）：%s 落在系统盘" % "/".join(cats),
            "notes": ["⚠️ 本次为降级判定，动词与目标未绑定，措辞可能偏宽"],
            "critical": bool(crit)}
