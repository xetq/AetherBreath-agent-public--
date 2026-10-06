# -*- coding: utf-8 -*-
"""工作区状态快照（只读）：任务子目录、最近活动文件、会话与技能概况。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import config

MAX_FILES_PER_DIR = 40
MAX_RECENT = 25


def _iso(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        return "?"


# 工作区里常塞学习用完整 venv / 依赖树（实测 agent_workspace 有 6.3 万文件、3.7GB，
# 全量 rglob 要 28s，直接把面板拖死）。这些目录不是"任务产物"，扫描时整棵剪掉。
SKIP_DIRS = {
    "site-packages", "node_modules", "__pycache__", ".git", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".idea", "dist", "build", ".next",
    ".cache", "hf_cache", ".Trash", ".recycle", ".trash",
}
_SKIP_LOWER = {d.lower() for d in SKIP_DIRS}


def _walk_files(root: Path, cap: int = 20000):
    """os.walk + 目录剪枝。返回 [(path, size, mtime), ...]，相对 root 的遍历上限 cap 个文件。"""
    found = []
    try:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns
                      if d not in SKIP_DIRS and d.lower() not in _SKIP_LOWER]
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                found.append((Path(fp), st.st_size, st.st_mtime))
                if len(found) >= cap:
                    return found
    except Exception:
        pass
    return found


def _dir_stat(root: Path, max_files: int = MAX_FILES_PER_DIR) -> Dict[str, Any]:
    entries = _walk_files(root)
    total = len(entries)
    size = sum(e[1] for e in entries)
    entries.sort(key=lambda e: e[2], reverse=True)
    return {
        "file_count": total,
        "size": size,
        "files": [
            {
                "name": str(f.relative_to(root)).replace("\\", "/"),
                "mtime": datetime.fromtimestamp(mt).isoformat(timespec="seconds"),
                "size": sz,
            }
            for f, sz, mt in entries[:max_files]
        ],
    }


def dir_detail(rel: str) -> Dict[str, Any]:
    """展开某个工作区子目录（路径穿越防护：必须落在 workspace 内）。"""
    root = config.WORKSPACE_ROOT.resolve()
    target = (root / (rel or "")).resolve()
    if root != target and root not in target.parents:
        return {"ok": False, "error": "路径越界：仅允许访问工作区内部"}
    if not target.exists():
        return {"ok": False, "error": f"目录不存在: {rel}"}
    if not target.is_dir():
        try:
            return {"ok": True, "file": rel, "size": target.stat().st_size,
                    "mtime": _iso(target),
                    "preview": target.read_text(encoding="utf-8", errors="replace")[:4000]}
        except Exception as e:
            return {"ok": False, "error": f"读取失败: {e}"}
    info = _dir_stat(target)
    info.update({"ok": True, "name": rel or ".", "mtime": _iso(target)})
    return info


def status(recent_limit: int = MAX_RECENT) -> Dict[str, Any]:
    ws = config.WORKSPACE_ROOT
    out: Dict[str, Any] = {"workspace": str(ws), "exists": ws.exists()}
    dirs: List[Dict[str, Any]] = []
    all_files: List[Path] = []
    if ws.exists():
        for d in sorted(ws.iterdir(), key=lambda x: x.name):
            if not d.is_dir():
                continue
            info = _dir_stat(d)
            info["name"] = d.name
            info["mtime"] = _iso(d)
            for f in info["files"]:
                f["path"] = f"{d.name}/{f['name']}"      # 跨目录汇总要用带目录前缀的路径
            dirs.append(info)
            all_files.extend(info["files"])
        loose = [f for f in ws.iterdir() if f.is_file()]
        if loose:
            out["loose_files"] = [
                {"name": f.name, "path": f.name, "mtime": _iso(f)} for f in loose[:20]]
    dirs.sort(key=lambda x: x["mtime"], reverse=True)
    out["task_dirs"] = dirs
    out["task_dir_count"] = len(dirs)

    # 最近活动文件：直接复用上面每个任务目录已经扫过的结果，不再整库扫第二遍
    # （rglob 两遍是工作区面板加载不出来的主因）。顶层散文件也一并计入。
    rec: List[Dict[str, Any]] = [dict(f) for f in all_files]
    for f in ws.iterdir() if ws.exists() else []:
        if f.is_file():
            try:
                st = f.stat()
            except OSError:
                continue
            rec.append({"path": f.name, "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
    rec.sort(key=lambda x: x["mtime"], reverse=True)
    out["recent_files"] = rec[:recent_limit]

    # 会话概况
    wd = config.WORKING_MEMORY_DIR
    sess = {"dir": str(wd), "count": 0, "by_status": {}, "latest": None}
    if wd.exists():
        js = list(wd.glob("*.json"))
        sess["count"] = len(js)
        tmp = list(wd.glob("*.json.tmp"))
        sess["tmp_files"] = len(tmp)
        by: Dict[str, int] = {}
        for p in js:
            try:
                import json
                with open(p, "r", encoding="utf-8") as f:
                    stt = json.load(f).get("status", "complete")
                by[stt] = by.get(stt, 0) + 1
            except Exception:
                by["unreadable"] = by.get("unreadable", 0) + 1
        sess["by_status"] = by
        if js:
            newest = max(js, key=lambda x: x.stat().st_mtime)
            sess["latest"] = {"session_id": newest.stem, "mtime": _iso(newest)}
    out["sessions"] = sess

    # 技能库概况（只读，供"模式切换"扩展位参考）
    sk = config.SKILLS_DIR
    if sk.exists():
        try:
            out["skills"] = {
                "dir": str(sk),
                "count": sum(1 for d in sk.iterdir() if d.is_dir()),
                "registry": (_iso(sk / "SKILL_REGISTRY.md")
                             if (sk / "SKILL_REGISTRY.md").exists() else None),
            }
        except Exception:
            pass
    out["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return out
