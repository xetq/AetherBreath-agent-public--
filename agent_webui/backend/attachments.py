# -*- coding: utf-8 -*-
"""附件：安全落盘 + 类型分类（网关侧，**纯标准库**）。

职责边界（刻意划清，别越界）
---------------------------
网关（本模块）—— 收字节、安全落盘、分类、给元数据。**不做任何内容理解**。
agent（`compose_chat_kwargs` 咽喉）—— 把附件变成 content 块（图片编码 / 文本提取）。
    为什么这么切：网关 venv 只有 fastapi + uvicorn（见 backend/requirements.txt），
    而 PIL / pypdf / python-docx 与咽喉本身都在 agent 侧。

安全（主人硬要求）
-----------------
本模块**只做字节落盘**。永不 import / exec / eval 上传物，
永不按扩展名调用外部程序，永不解压。文件名做穿越清洗。

GitHub 友好：路径全部从 `config.PROJECT_ROOT` 推导，零硬编码；开关读 config.yaml。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict

import config as _cfg

# ===== 类型白名单（未列即拒；D3 决策）=====
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".py", ".json", ".csv", ".tsv", ".log",
    ".yml", ".yaml", ".ini", ".cfg", ".toml", ".xml", ".html", ".htm",
    ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sql",
    ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".php", ".sh", ".bash", ".bat", ".ps1", ".lua", ".vue", ".r",
}
DOC_EXT = {".docx", ".pdf", ".xlsx"}

KINDS = ("image", "text", "doc", "reject")

# 与 config.yaml 的 attachments 段同源；这里是**兜底默认**（无 pyyaml 时用）
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "max_file_mb": 20,
    "max_per_message": 10,
    "max_text_chars": 200000,
    "root": "agent_workspace/attachments",
}


def settings() -> Dict[str, Any]:
    """读 config.yaml 的 attachments 段；解析失败退回 DEFAULTS（不抛）。"""
    d = dict(DEFAULTS)
    cfg = _cfg.PROJECT_ROOT / "config.yaml"
    if not cfg.exists():
        return d
    try:
        import yaml  # type: ignore
        with open(cfg, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sec = data.get("attachments") or {}
        if isinstance(sec, dict):
            for k, v in sec.items():
                if v is not None:
                    d[k] = v
        paths = data.get("paths") or {}
        if isinstance(paths.get("attachments"), str) and paths["attachments"].strip():
            d["root"] = paths["attachments"].strip()
    except Exception:
        pass        # 网关没 pyyaml 是常态：用默认值即可
    return d


def root_dir() -> Path:
    """附件落盘根目录（绝对路径）。"""
    return (_cfg.PROJECT_ROOT / str(settings().get("root") or DEFAULTS["root"])).resolve()


def safe_name(name: str) -> str:
    """把任意文件名清洗成一个安全的单段文件名（防穿越 / 防非法字符 / 防超长）。

    只取最后一段（剥掉所有目录成分），再逐字符过滤 —— 这样 `../../etc/passwd`
    与 `C:\\Windows\\x.txt` 都只会剩下 `passwd` / `x.txt`。
    """
    s = str(name or "").replace("\\", "/").split("/")[-1]
    out = []
    for ch in s:
        if ch in '<>:"|?*' or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    s = "".join(out).strip(" .")
    if not s:
        s = "file"
    if len(s) > 120:
        stem, ext = os.path.splitext(s)
        s = stem[: 120 - len(ext)] + ext
    return s


def classify(filename: str) -> str:
    """按扩展名分类：image | text | doc | reject。"""
    ext = os.path.splitext(str(filename or ""))[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    if ext in DOC_EXT:
        return "doc"
    return "reject"


def accepted_exts() -> Dict[str, list]:
    """给前端/接口自描述用。"""
    return {"image": sorted(IMAGE_EXT), "text": sorted(TEXT_EXT), "doc": sorted(DOC_EXT)}


def save(session_id: str, filename: str, data: bytes) -> Dict[str, Any]:
    """落盘一个附件，返回元数据。任何不合规都 raise ValueError（调用方转 4xx）。

    返回字段：id / name / path(相对项目根, posix) / abs / size / kind / ext / session_id
    """
    st = settings()
    if not st.get("enabled", True):
        raise ValueError("附件功能已关闭（config.yaml: attachments.enabled=false）")
    if not data:
        raise ValueError("空文件")
    max_mb = float(st.get("max_file_mb", 20) or 20)
    if len(data) > max_mb * 1024 * 1024:
        raise ValueError("文件过大：%.1f MB（上限 %.0f MB）" % (len(data) / 1048576.0, max_mb))
    kind = classify(filename)
    if kind == "reject":
        raise ValueError("不支持的文件类型（支持：图片 / 文本 / 文档 白名单）")

    sid = safe_name(str(session_id or ""))[:64] or "unknown"
    base = safe_name(filename)
    aid = uuid.uuid4().hex[:12]
    dest_dir = root_dir() / sid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("%s_%s" % (aid, base))
    dest.write_bytes(data)

    try:
        rel = dest.resolve().relative_to(_cfg.PROJECT_ROOT).as_posix()
    except Exception:
        rel = str(dest)

    # 穿越自检：落点必须在根目录内（防未来改动引入漏洞）
    if root_dir() not in dest.resolve().parents:
        try:
            dest.unlink()
        except Exception:
            pass
        raise ValueError("落点越界，已拒绝")

    return {
        "id": aid,
        "name": base,
        "path": rel,
        "abs": str(dest),
        "size": len(data),
        "kind": kind,
        "ext": os.path.splitext(base)[1].lower(),
        "session_id": sid,
    }


def list_session(session_id: str) -> Dict[str, Any]:
    """只读列出一个会话已落盘的附件（诊断/界面用）。"""
    sid = safe_name(str(session_id or ""))[:64]
    d = root_dir() / sid
    items = []
    if d.is_dir():
        for f in sorted(d.iterdir()):
            if f.is_file():
                items.append({"name": f.name, "size": f.stat().st_size})
    return {"session_id": sid, "dir": str(d), "count": len(items), "items": items}
