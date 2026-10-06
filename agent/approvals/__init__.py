# -*- coding: utf-8 -*-
"""approvals/ —— 审批规范包（策略外置）。

约定：本目录下每个模块声明一类审批（一个 ApprovalSpec），
由 agent/approval.py 的引擎在启动时自动发现并加载。

新增一类审批 = 往这里放一个文件，不改引擎、不改接线、不改前端。
每个 spec 必须提供：
    KIND            稳定标识（写进账本、给作用域用）
    TITLE           审批卡标题动词，如 "写入" / "删除" / "移动"
    RISK            0 低 / 1 中 / 2 高
    applies(ctx)     这类审批是否与本次调用相关
    finding(ctx)     -> dict | None，命中就返回给主人看的描述
上下文 ctx 由引擎注入：tool / kwargs / paths(已归一) / drive(系统盘根) / mode
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Dict, List

SPECS: List[Any] = []


def load_specs() -> List[Any]:
    """发现并返回所有规范模块。加载失败绝不静默跳过 —— 那等于门悄悄拆了。"""
    global SPECS
    if SPECS:
        return SPECS
    found: List[Any] = []
    errors: List[str] = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module("%s.%s" % (__name__, mod.name))
        except Exception as e:
            errors.append("%s: %s: %s" % (mod.name, e.__class__.__name__, e))
            continue
        if all(hasattr(m, a) for a in ("KIND", "TITLE", "RISK", "applies", "finding")):
            found.append(m)
        else:
            errors.append("%s: 缺少 KIND/TITLE/RISK/applies/finding 成员" % mod.name)
    found.sort(key=lambda m: -getattr(m, "RISK", 0))
    SPECS = found
    if errors:
        # 规范加载不全 = 覆盖面有洞，必须被探针与界面看到
        SPECS_LOAD_ERRORS[:] = errors
    return found


SPECS_LOAD_ERRORS: List[str] = []


def describe() -> Dict[str, Any]:
    return {"loaded": [getattr(m, "KIND", "?") for m in load_specs()],
            "failed": list(SPECS_LOAD_ERRORS)}
