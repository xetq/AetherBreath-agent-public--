# -*- coding: utf-8 -*-
"""规范：改动审批链与引擎自身的文件，必须由人点头。

这条规范补的是一个结构性缺口：旧审批的入口判据是「路径在系统盘」，
于是被审对象可以自由改写审查者自己 —— 实测三条全部静默放行：

    改 agent/approval.py      -> pass
    往 approval_rules.jsonl 追加免审规则 -> pass
    删掉整个项目工作区        -> pass

那不是"覆盖面窄"，那是**没有门**：谁拿到执行权谁就能拆门。

取向刻意与系统盘规范不同：**只问不拒**（永不返回 block）。
如果改引擎本身是不可授权的，一旦新规则有误我就再也修不了自己。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import _impact as I

KIND = "engine.selfmodify"
TITLE = "改动审批链自身"
RISK = 2

# 自留地清单在 _impact.SELF_PREFIXES（相对项目根，零绝对路径）。
# 这里只负责把命中的前缀翻译成人能读的严重度。
_WORDING = {
    "agent/approval.py": "这是审批引擎本体：改它等于改变「什么会被拦」的规则",
    "agent/approval_lex.py": "这是词法层：改坏它会让动作与目标从判定里消失",
    "agent/approvals": "这是审批规范目录：新增/删除规范文件会直接改变覆盖面",
    "agent_logs/approval": "这是账本与免审规则：改它等于自签免审或抹掉留痕",
    ".env": "这里可能有 AETHER_AUDIT_MODE=off —— 一行就能关掉整道闸门",
    ".git": "版本库：改历史或删对象会让回滚网失效",
    "agent/agent.py": "主程序：审批闸门的调用点就在这里",
    "agent/task_orchestrator.py": "任务编排器：工具实际执行的地方",
    "agent_webui/backend/approval_adapter.py": "WebUI 审批通道：卡片与回投的链路",
    "agent_webui/backend/bridge.py": "bridge：审批弹窗的传输层",
    "agent_webui/backend/api.py": "网关 API",
    "agent_webui/backend/main.py": "网关主进程",
    "tests/test_approval": "审批回归测试：改它可能是在把失败用例改成通过",
}


def applies(ctx: Dict[str, Any]) -> bool:
    # 同 fs_drive：初筛放到底，真正的区判定在 finding() 里用完整解析做。
    return bool(ctx.get("actions")) or bool(ctx.get("paths"))


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    root = ctx.get("root") or ""
    if not root:
        return None
    targets: List[str] = []
    marks: List[str] = []
    for a in (ctx.get("actions") or []):
        paths, _how = I.targets_of(a)
        for p in paths:
            hit = I.self_hit(p, root)
            if not hit:
                continue
            if p not in targets:
                targets.append(p)
            if hit not in marks:
                marks.append(hit)
        if a.get("redirect"):
            for p in (a.get("redirect") or []):
                rp = I.resolve(str(p), ctx.get("cwd") or "")
                hit = I.self_hit(rp, root)
                if hit and rp not in targets:
                    targets.append(rp)
                    if hit not in marks:
                        marks.append(hit)
    if not targets:
        return None
    notes = []
    for m in marks[:4]:
        w = _WORDING.get(m)
        if w:
            notes.append("要害：%s（%s）" % (m, w))
    cats = []
    for a in (ctx.get("actions") or []):
        c = I.category_of(str(a.get("word") or ""))
        if c and c not in cats:
            cats.append(c)
    return {"quiet": False,
            "action": "/".join(cats) or "改动",
            "targets": targets,
            "intent": "%s审批链自身文件：%s" % (
                "/".join(cats) or "改动", "、".join(t.rsplit("/", 1)[-1] for t in targets[:3])),
            "reason": "被改动的对象包含审批引擎/规则/账本本身，改完闸门行为会变",
            "notes": notes or ["改动的是引擎自留地"],
            "critical": False}
