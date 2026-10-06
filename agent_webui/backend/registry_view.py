# -*- coding: utf-8 -*-
"""技能 / 集成包快照 —— 给「运行时」面板的数据组装（**不依赖 fastapi**，可单测）。

与 `mcp_view.py` **同构、同口径**（同一个面板里三块，行为必须一致）：
  · **常驻** —— 只读盘（扫文件夹），不起任何进程，也不需要先发一条消息；
  · **只放会变的量**：名字 / 描述 / 版本 / 标签 / 问题，不堆累计空指标；
  · **出错如实返回 `ok=False`**（面板照实显示，不装死）；目录不存在 = 空态，不是崩溃。

为什么**不读** `SKILL_REGISTRY.md` / `PACK_REGISTRY.md`：
那两份 .md 是**新会话启动时冻结、注入给模型看的注入快照**，与磁盘现状可能不一致
（会话开着时新加的技能，要等新会话才会进注册表、进提示词）。面板是镜子，
照的是**磁盘现状**；注册表只报个路径 + 更新时间，供人对照。

为什么**另起一个模块**而不是塞进 `mcp_view.py`：与那边同一个理由 —— 网关跑在
`venv-gateway`（有 fastapi），测试跑在主 venv（没有）。组装逻辑放这里、`api.py` 只做
一层薄包装，测试就能直接 import 它（`tests/test_registries_api.py`）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# agent/ 在仓库根下（agent_webui/backend/registry_view.py -> parents[2]），与 mcp_view.py 同款兜底。
_AGENT_DIR = Path(__file__).resolve().parents[2] / "agent"


def _agent_modules():
    """按需导入 agent/ 下的两个注册表引擎（缺一个就整体失败，调用方如实上报）。"""
    if str(_AGENT_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENT_DIR))
    import integration_pack as ip                       # noqa: PLC0415
    import skill_system as ss                           # noqa: PLC0415
    return ss, ip


def _project_root() -> Path:
    return _AGENT_DIR.parent


def _mtime_of(path: Path) -> Optional[str]:
    """文件的最后修改时间（人类可读）；不存在或读不了就如实返回 None。"""
    try:
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:                                   # noqa: BLE001
        return None


def _rows(objs: List[Any]) -> List[Dict[str, Any]]:
    """统一的「一行」形状：名字 + 描述（面板只显示这两样）。

    version/tags/doc 是**附赠**：前端把它们塞进 title 提示里，不占视觉空间 ——
    主人 2026-09-23 裁定「名字 + 描述（两行，与 MCP 站同口径，最简洁）」。
    """
    out: List[Dict[str, Any]] = []
    for o in objs:
        row: Dict[str, Any] = {
            "name": o.name,
            "description": o.description or "",
            "doc": _relpath(o),
        }
        version = getattr(o, "version", None)
        if version:
            row["version"] = str(version)
        tags = getattr(o, "tags", None)
        if tags:
            row["tags"] = list(tags)
        out.append(row)
    return out


def _relpath(obj: Any) -> str:
    """正文/说明书相对项目根的 POSIX 路径（拿不到相对路径就退回绝对，绝不抛）。"""
    try:
        return obj.doc_relpath
    except Exception:                                   # noqa: BLE001
        try:
            return Path(obj.doc_file).as_posix()
        except Exception:                               # noqa: BLE001
            return ""


def skills_snapshot(skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """技能库现状：`agent_skills/<名字>/SKILL.md`（文件夹存在即注册）。

    返回结构（前端 `SkillsResp`）：
      ok / dir / registry / registry_mtime / count / issues / skills[{name,description,version,tags,doc}]
    """
    try:
        ss, _ = _agent_modules()
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "技能模块不可用：%s: %s" % (type(e).__name__, e),
                "skills": [], "count": 0}
    try:
        root = _project_root()
        base = Path(skills_dir).resolve() if skills_dir else (root / ss.DEFAULT_SKILLS_DIR)
        reg = base / ss.DEFAULT_REGISTRY_NAME
        res = ss.scan_skills(None, base)                # 传目录：测试可指临时目录
        return {
            "ok": True,
            "dir": str(base),
            "registry": ss.DEFAULT_REGISTRY_NAME,
            "registry_mtime": _mtime_of(reg),
            "count": len(res.skills),
            "issues": list(res.issues),
            "skills": _rows(res.skills),
        }
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "扫描技能库失败：%s: %s" % (type(e).__name__, e),
                "skills": [], "count": 0}


def packs_snapshot(packs_dir: Optional[Path] = None) -> Dict[str, Any]:
    """集成包现状：`agent_integration_packs/<名字>/PACK.md`（文件夹存在即注册）。

    返回结构（前端 `PacksResp`）：
      ok / dir / registry / registry_mtime / count / issues / packs[{name,description,doc}]
    """
    try:
        _, ip = _agent_modules()
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "集成包模块不可用：%s: %s" % (type(e).__name__, e),
                "packs": [], "count": 0}
    try:
        base = Path(packs_dir).resolve() if packs_dir else ip.packs_dir(None)
        reg = base / ip.REGISTRY_NAME
        res = ip.scan_packs(base)                       # 传目录：测试可指临时目录
        return {
            "ok": True,
            "dir": str(base),
            "registry": ip.REGISTRY_NAME,
            "registry_mtime": _mtime_of(reg),
            "count": len(res.packs),
            "issues": list(res.issues),
            "packs": _rows(res.packs),
        }
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": "扫描集成包失败：%s: %s" % (type(e).__name__, e),
                "packs": [], "count": 0}


def registries_snapshot(skills_dir: Optional[Path] = None,
                        packs_dir: Optional[Path] = None) -> Dict[str, Any]:
    """面板一次拿全两区（两块各自独立成败：一个坏了另一个照样显示）。"""
    return {"skills": skills_snapshot(skills_dir), "packs": packs_snapshot(packs_dir)}
