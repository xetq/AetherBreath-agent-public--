# -*- coding: utf-8 -*-
"""规范：安装第三方技能需要人工确认。

缺口（2026-09-11 安全审计）：`skillhub_install` 会从 GitHub / skills.sh 拉取
**任意仓库**的技能目录，落到 `agent_skills/`，而技能注册表**每会话注入系统提示**。
也就是说：一条安装指令 = 把外部内容放进「指令层」。

而且它的参数是 `identifier`（例如 `owner/repo/skill-path`），**不是路径** ——
所有路径类规范都够不着它（实测：A12 判 pass）。必须按工具名单独成类。
本项目的 `approvals/README.md` 里「计划中的：下载执行」正是这一类。

取向：
  · 只拦「安装/下载」本身，不拦读取或检索技能；
  · **不拒**（第三方技能是正当需求），只要求人点头 —— 与 engine.selfmodify 同取向；
  · 卡片不给「目标路径」（这里没有路径），把来源写进 intent 与 notes。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

KIND = "skill.install"
TITLE = "安装第三方技能"
RISK = 2

_TOOLS = frozenset({"skillhub_install"})


def applies(ctx: Dict[str, Any]) -> bool:
    return str(ctx.get("tool") or "") in _TOOLS


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kw = ctx.get("kwargs") or {}
    ident = str(kw.get("identifier") or kw.get("source") or kw.get("url") or "").strip()
    return {
        "quiet": False,
        "action": "安装技能",
        "targets": [],                  # 没有文件路径可报，不许编一个
        "intent": "从外部仓库安装技能：%s" % (ident or "(未给出标识)"),
        "reason": "第三方技能会落进 agent_skills/，并随注册表**注入系统提示** —— "
                  "等于把外部内容放进指令层",
        "notes": ["建议装完先通读它的 SKILL.md，再决定是否让它在任务里生效",
                  "来源标识：%s" % (ident or "(未给出)")],
        "critical": False,
    }
