# -*- coding: utf-8 -*-
"""pack_manage —— 让 AB 自己维护集成包（自集成系统）。

设计与纪律：`agent_integration_packs/README.md`（说明包）；引擎：`agent/integration_pack.py`。

三个动作（工具参数 `action`）：

| action | 干什么 |
|---|---|
| `create` | 建一个包的骨架（文件夹 + `PACK.md` 模板），并同步注册表 |
| `read`   | 打印某个包的 `PACK.md` + 包内文件清单（省得记路径） |
| `remove` | 移除一个包（整个文件夹挪 `.trash/`，可恢复） |

**刻意不做的事**（别顺手加回来）：

· **不做 `list`**：所有包都已经在每轮注入的 `PACK_REGISTRY.md` 里了，再列一遍纯属多此一举。
· **不做 `add_tool`**：包内文件用普通文件工具（`read_file` / `execute_python` / `execute_shell`）改就够了 ——
  专门做个动作等于把同一件事实现两遍，必然有一份常年没被验证。
· **不做挂载**：包内的 skill / MCP 站点怎么生效，由该包的 `PACK.md` 说明；
  系统层不复制实体，避免"同一份东西有两个注册源"。

纪律（AB 自己的行为约束，引擎不设闸门）：

· **不得自行开始集成**：`create` 之前必须先给主人一份集成提案，等他确认。
· 包工具由主人提供（文件或获取方式），除非主人说"你自己挑"。
· 集成成功 / 失败都要如实汇报（部分失败也要说，例如某个 skill 实在没找到）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AGENT_DIR = PROJECT_ROOT / "agent"
if str(_AGENT_DIR) not in sys.path:              # 与运行时同一套扁平导入（模块身份唯一）
    sys.path.insert(0, str(_AGENT_DIR))

import integration_pack as ip                    # noqa: E402

MAX_TEXT = 20000        # read 动作返回的说明书上限（超了截断并指路）
MAX_FILES = 80          # 包内文件清单上限


def _log(logger: Any, msg: str, **extra: Any) -> None:
    if logger is None:
        return
    try:
        lg = logger.with_span("pack:manage") if hasattr(logger, "with_span") else logger
        lg.info(msg, **extra)
    except Exception:
        pass


def _file_listing(folder: Path) -> List[str]:
    """包内文件清单（相对包目录的 POSIX 路径，已排序；跳过 __pycache__）。"""
    out: List[str] = []
    try:
        for base, dirs, files in os.walk(str(folder)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = Path(base) / f
                try:
                    out.append(p.relative_to(folder).as_posix())
                except ValueError:
                    out.append(p.name)
    except Exception:
        pass
    out.sort()
    return out


def _act_create(name: str, description: str, logger: Any = None) -> str:
    ok, msg = ip.create_pack(name, description)
    _log(logger, "集成包 create", name=str(name), ok=ok)
    return msg


def _act_read(name: str, logger: Any = None) -> str:
    name = str(name or "").strip()
    if not name:
        return "❌ 要读哪个包？给 name（包名就在注入的 PACK_REGISTRY.md 里）"
    doc = ip.pack_doc(name)
    if not doc.is_file():
        return ("❌ 没有叫 %s 的包。现有的：%s"
                % (name, ", ".join(ip.known_names()) or "（一个都没有）"))
    try:
        text = doc.read_text(encoding="utf-8")
    except Exception as e:
        return "❌ 读 %s 失败：%s: %s" % (doc, type(e).__name__, e)

    files = _file_listing(doc.parent)
    lines = ["# 包 `%s` 的说明书（%s）" % (name, doc), "", "## 包内文件", ""]
    if files:
        for f in files[:MAX_FILES]:
            lines.append("- `%s`" % f)
        if len(files) > MAX_FILES:
            lines.append("- …（还有 %d 个，要看目录）" % (len(files) - MAX_FILES))
    else:
        lines.append("（只有 PACK.md）")
    lines += ["", "## PACK.md 正文", ""]
    if len(text) > MAX_TEXT:
        lines.append(text[:MAX_TEXT])
        lines.append("")
        lines.append("…（超长已截断，完整内容用 read_file 读：%s）" % doc)
    else:
        lines.append(text)
    return "\n".join(lines)


def _act_remove(name: str, logger: Any = None) -> str:
    ok, msg = ip.remove_pack(name)
    _log(logger, "集成包 remove", name=str(name), ok=ok)
    return msg


_ACTIONS = ("create", "read", "remove")


def pack_manage(action: str = "", name: str = "", description: str = "",
                logger: Any = None) -> str:
    """自维护入口：建包 / 读包说明书 / 移除包。"""
    act = str(action or "").strip().lower()
    if act not in _ACTIONS:
        return "❌ action 必须是 %s 之一（拿到的是 %r）" % ("/".join(_ACTIONS), action)
    try:
        if act == "create":
            return _act_create(name, description, logger)
        if act == "read":
            return _act_read(name, logger)
        return _act_remove(name, logger)
    except Exception as e:                        # 兜底：绝不把异常抛给编排器
        _log(logger, "pack_manage 异常", action=act, err=repr(e))
        return "❌ pack_manage 内部异常（%s）：%s: %s" % (act, type(e).__name__, e)


pack_manage_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "pack_manage",
        "description": (
            "自维护：管理**集成包**（agent_integration_packs/，一个包 = 一个文件夹 + PACK.md 说明书）。"
            "create=建包骨架（文件夹 + PACK.md 模板，自动同步注册表）；"
            "read=打印某包的说明书 + 包内文件清单（不用记路径）；"
            "remove=移除包（整个文件夹挪进 .trash/，可恢复）。"
            "⚠️ 纪律：**不得自行开始集成** —— create 之前必须先给主人一份集成提案"
            "（目标能力/来源/方案/要下载什么/依赖/风险/预期效果）并等他确认；"
            "集成成功或失败都要如实汇报。包内文件用普通文件工具改就行，不必再调本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS),
                           "description": "要做的动作"},
                "name": {"type": "string",
                         "description": "包名（字母/数字/下划线/短横，1-32 字符）。文件夹名与它一致"},
                "description": {"type": "string",
                                "description": "create 动作：一句话用途 —— 会进注册表，AB 靠它判断要不要用这个包"},
            },
            "required": ["action"],
        },
    },
}
