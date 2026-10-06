# -*- coding: utf-8 -*-
"""integration_pack —— 自集成包注册表引擎（AetherBreath）。

**给人看的设计说明**：`agent_integration_packs/README.md`（说明包）。

一句话：**一个集成包 = 一个文件夹**，里面有 `PACK.md`（说明书）；
**文件夹存在即注册，消失即注销** —— 与 `agent_skills/`、`agent_MCP/` 同构。

三个入口（与 `mcp_station.py` 对称）：

    scan_packs()              扫描包目录 → PackInfo 列表 + issues
    sync_registry()           把现状写进 PACK_REGISTRY.md（有差异才写盘）
    build_injection_block()   生成注入系统提示词的块（新对话时调一次，之后冻结）

注册表**只有三列**：包名 / 用途 / 说明书路径。刻意不含"包里有什么" ——
包内提供了哪些工具、怎么用，一律以该包自己的 `PACK.md` 为准（渐进式披露的下一层）。
别顺手加列：加一列就要多扫一遍盘、多一份会腐烂的状态。

命令行（工程师手动维护 / CI 检查）：

    python agent/integration_pack.py            # 同步注册表并打印摘要
    python agent/integration_pack.py --check    # 只报告差异，不写盘
    python agent/integration_pack.py --show     # 打印将注入上下文的注册表块
    python agent/integration_pack.py --list     # 列出包与问题
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# 常量（相对项目根；调用方可显式覆盖 —— GitHub 友好，零硬编码绝对路径）
# ---------------------------------------------------------------------------

PACK_DOC = "PACK.md"                      # 包说明书（手写：frontmatter + 正文）
REGISTRY_NAME = "PACK_REGISTRY.md"        # 机器生成：每包一行，注入系统提示词
DEFAULT_PACKS_DIR = "agent_integration_packs"
PACKS_ENV = "AETHER_INTEGRATION_PACKS"    # 覆盖包目录（测试 / 多套配置用）
TRASH_DIR = ".trash"                      # 移除的包挪这里（可恢复，不真删）
SKIP_DIRS = {"__pycache__", ".git", ".state"}
NAME_MAX = 32                             # 包名字符数上限

_INJECT_GUIDE = (
    "下面是**已注册的集成包**（一个包 = `agent_integration_packs/<包名>/` 一个文件夹）。\n"
    "注册表只说「有什么」—— **包里提供了哪些工具、怎么用，全在该包的 `PACK.md` 里**（按需读取）。\n"
    "要用某个包时（通常是主人点名，例如「用 xxx 包做 yyy」）：先读它的 `PACK.md`，再按里面的说明干活。\n"
    "**不得自行开始集成**：新建包、给包加东西之前，必须先给主人一份集成提案"
    "（目标能力 / 来源 / 方案 / 需要下载什么 / 依赖 / 风险 / 预期效果），等他确认；"
    "集成成功或失败都要如实汇报（例如某个 skill 实在没找到，也要说）。"
)

_HEADER_TEMPLATE = """# PACK_REGISTRY.md（集成包注册表）

> 本文件由 `agent/integration_pack.py` **自动生成**（机器所有，别手改；改包文件夹即可）。
> 一个集成包 = 一个文件夹，**文件夹存在即注册、消失即注销**（包内须有 `PACK.md`）。
> 注册表只说明「有什么」，**不含包内工具清单** —— 那是 `PACK.md` 的事，按需读取。
> 最后更新: {timestamp}

| 包名 | 用途 | 说明书 |
|---|---|---|
"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PackInfo:
    """一个已注册集成包的元数据（来自 `PACK.md` 的 frontmatter）。"""

    name: str
    description: str
    doc_file: Path
    project_root: Path

    @property
    def folder(self) -> Path:
        return self.doc_file.parent

    @property
    def doc_relpath(self) -> str:
        """说明书相对项目根路径（POSIX 风格，GitHub 友好）。"""
        try:
            return self.doc_file.relative_to(self.project_root).as_posix()
        except ValueError:
            return self.doc_file.as_posix()


@dataclass
class ScanResult:
    """一次包目录扫描的结果。"""

    packs: List[PackInfo] = field(default_factory=list)   # 合法包（按 name 排序）
    issues: List[str] = field(default_factory=list)       # 无效项 / 警告（人类可读）


@dataclass
class SyncResult:
    """注册表同步结果：本次启动与包目录的差异。"""

    changed: bool
    added: List[PackInfo]
    removed: List[str]
    updated: List[str]
    issues: List[str]
    registry_path: Path

    def summary(self) -> str:
        parts: List[str] = []
        if self.added:
            parts.append("新增: " + ", ".join(p.name for p in self.added))
        if self.removed:
            parts.append("移除: " + ", ".join(self.removed))
        if self.updated:
            parts.append("更新: " + ", ".join(self.updated))
        return "; ".join(parts) if parts else "无变化"


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

def _project_root_of() -> Path:
    """integration_pack.py 所在项目根（agent/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def packs_dir(path: Optional[Path] = None) -> Path:
    """包根目录：显式入参 > 环境变量 AETHER_INTEGRATION_PACKS > 项目根下的默认名。"""
    if path:
        return Path(path).resolve()
    env = (os.environ.get(PACKS_ENV) or "").strip()
    if env:
        return Path(env).resolve()
    return _project_root_of() / DEFAULT_PACKS_DIR


def registry_path(packs: Optional[Path] = None) -> Path:
    """注册表文件位置（默认在包根目录里，与 SKILL_REGISTRY.md / MCP_REGISTRY.md 同规矩）。"""
    return packs_dir(packs) / REGISTRY_NAME


def trash_dir(packs: Optional[Path] = None) -> Path:
    return packs_dir(packs) / TRASH_DIR


def pack_doc(name: str, packs: Optional[Path] = None) -> Path:
    """某个包的说明书路径（不检查存在性）。"""
    return packs_dir(packs) / str(name) / PACK_DOC


def pack_exists(name: str, packs: Optional[Path] = None) -> bool:
    return pack_doc(name, packs).is_file()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def valid_name(name: str) -> bool:
    """包名校验：字母/数字/下划线/短横，1~32 字符。

    不用正则：与 mcp_manage 的 `^[A-Za-z0-9_-]{1,32}$` 同口径，
    但手写判定更省事（也不必 import re）。
    """
    s = str(name or "")
    if not s or len(s) > NAME_MAX:
        return False
    for ch in s:
        if ch in "_-":
            continue
        if not (ch.isascii() and ch.isalnum()):
            return False
    return True


# ---------------------------------------------------------------------------
# 扫描：包目录 → PackInfo
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> Optional[Tuple[str, str]]:
    """把 md 文本切成 (frontmatter_yaml, body)；无 frontmatter 返回 None。"""
    if not text.startswith("---"):
        return None
    lines = text.splitlines(keepends=True)
    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return None
    return "".join(lines[1:end_idx]), "".join(lines[end_idx + 1:])


def _parse_pack(folder: Path, project_root: Path, issues: List[str]) -> Optional[PackInfo]:
    """解析单个包文件夹 → PackInfo；无效则记 issue 并返回 None。"""
    doc = folder / PACK_DOC
    if not doc.is_file():
        issues.append("[%s] 缺 %s，未注册（一个包须有说明书）" % (folder.name, PACK_DOC))
        return None

    try:
        text = doc.read_text(encoding="utf-8")
    except Exception as e:
        issues.append("[%s] 读 %s 失败：%s" % (folder.name, PACK_DOC, e))
        return None

    fm = _split_frontmatter(text)
    if fm is None:
        issues.append("[%s] %s 未以 --- frontmatter 开头，未注册" % (folder.name, PACK_DOC))
        return None

    try:
        meta = yaml.safe_load(fm[0]) or {}
    except yaml.YAMLError as e:
        issues.append("[%s] frontmatter YAML 解析失败：%s" % (folder.name, e))
        return None
    if not isinstance(meta, dict):
        issues.append("[%s] frontmatter 不是键值映射，未注册" % folder.name)
        return None

    name = str(meta.get("name", "")).strip()
    if not name:
        issues.append("[%s] frontmatter 缺 name，未注册" % folder.name)
        return None
    if name != folder.name:
        issues.append(
            "[%s] frontmatter name '%s' 与文件夹名不一致（须一致，"
            "否则注册表里的包名定位不到说明书），未注册" % (folder.name, name)
        )
        return None

    description = str(meta.get("description", "") or "").strip()
    if not description:
        issues.append("[%s] 未写 description（建议补上，AB 靠它判断要不要用这个包）" % folder.name)

    return PackInfo(name=name, description=description, doc_file=doc, project_root=project_root)


def scan_packs(packs: Optional[Path] = None) -> ScanResult:
    """扫描包根目录，返回合法包列表（按 name 排序）+ 问题列表。"""
    root = packs_dir(packs)
    result = ScanResult()
    if not root.is_dir():
        result.issues.append("集成包目录不存在: %s" % root)
        return result

    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue                      # README.md / 注册表等散文件不算包
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue                      # 点开头（.trash/.state）+ 常规噪音目录
        info = _parse_pack(entry, root.parent, result.issues)
        if info is not None:
            result.packs.append(info)

    result.packs.sort(key=lambda p: p.name.lower())
    return result


def known_names(packs: Optional[Path] = None) -> List[str]:
    """现有包名（给"写错了"的提示用）。"""
    return [p.name for p in scan_packs(packs).packs]


# ---------------------------------------------------------------------------
# 注册表渲染 / 比较
# ---------------------------------------------------------------------------

def _clean_cell(value: str) -> str:
    """清理表格单元格：压成单行、转义竖线。"""
    return str(value or "").replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def render_rows(packs: List[PackInfo]) -> str:
    """只渲染表格数据行（不含标题/表头），用于内容比较。"""
    rows: List[str] = []
    for p in packs:
        desc = _clean_cell(p.description) or "（未写用途）"
        rows.append("| %s | %s | `%s` |" % (p.name, desc, p.doc_relpath))
    return "\n".join(rows)


def render_registry(packs: List[PackInfo], timestamp: Optional[str] = None) -> str:
    """渲染注册表全文。"""
    ts = timestamp or _now()
    body = render_rows(packs)
    return _HEADER_TEMPLATE.format(timestamp=ts) + (body + "\n" if body else "")


def _extract_body(text: str) -> str:
    """从注册表文本里提取「数据行部分」（去掉标题/注释/表头），用于比较。"""
    rows: List[str] = []
    started = False
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("| 包名") or s.startswith("|包名"):
            started = True
            continue
        if s.startswith("|"):
            head = s.lstrip("|").lstrip()
            if head.startswith("-") or head.startswith(":"):
                continue                  # 表头分隔行
            if started:
                rows.append(s)
    return "\n".join(rows)


def _parse_registry_rows(text: str) -> Dict[str, str]:
    """解析已有注册表 → {包名: 用途}，用于 diff 归类（新增/移除/更新）。"""
    out: Dict[str, str] = {}
    for ln in _extract_body(text).splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].replace("\\|", "|")
        if name:
            out[name] = cells[1].replace("\\|", "|")
    return out


# ---------------------------------------------------------------------------
# 同步（核心入口）
# ---------------------------------------------------------------------------

def sync_registry(packs: Optional[Path] = None, registry: Optional[Path] = None,
                  write: bool = True) -> SyncResult:
    """扫描包目录并与现有注册表比较（多增少删）。

    表头「最后更新」时间戳不参与比较 —— 正文行没变就算没变，
    免得每次启动都因为时间戳而重写文件。
    """
    root = packs_dir(packs)
    reg = Path(registry).resolve() if registry else registry_path(root)
    scan = scan_packs(root)
    new_rows = render_rows(scan.packs)

    old_text = ""
    old_rows = ""
    if reg.is_file():
        try:
            old_text = reg.read_text(encoding="utf-8")
            old_rows = _extract_body(old_text)
        except Exception:
            old_text, old_rows = "", ""

    changed = (new_rows != old_rows)
    added: List[PackInfo] = []
    removed: List[str] = []
    updated: List[str] = []

    if changed:
        old = _parse_registry_rows(old_text)
        new = {p.name: p for p in scan.packs}
        added = [p for n, p in new.items() if n not in old]
        removed = sorted(n for n in old if n not in new)
        updated = sorted(n for n, p in new.items()
                         if n in old and old[n] != (_clean_cell(p.description) or "（未写用途）"))
        if write:
            try:
                reg.parent.mkdir(parents=True, exist_ok=True)
                reg.write_text(render_registry(scan.packs), encoding="utf-8", newline="\n")
            except Exception as e:
                scan.issues.append("注册表写盘失败：%s: %s" % (type(e).__name__, e))

    return SyncResult(changed=changed, added=added, removed=removed, updated=updated,
                      issues=scan.issues, registry_path=reg)


def build_injection_block(packs: Optional[Path] = None,
                          registry: Optional[Path] = None) -> str:
    """构建注入系统提示词的注册表块（引导说明 + 注册表全文，会话内冻结）。

    注册表文件不存在时先同步生成一次；包目录不存在 → 返回空串（不注入）。
    """
    root = packs_dir(packs)
    reg = Path(registry).resolve() if registry else registry_path(root)
    if not reg.is_file():
        sync_registry(packs=root, registry=reg)
    if not reg.is_file():
        return ""
    if not scan_packs(root).packs:
        return ""                  # 一个包都没有 → 不注入（与 MCP 空注册表同一条规矩）
    try:
        text = reg.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not text:
        return ""
    return ("## PACK_REGISTRY.md（集成包注册表）（会话快照 · 自动注入）\n\n"
            + _INJECT_GUIDE + "\n\n" + text)


# ---------------------------------------------------------------------------
# 建包 / 移除（工具层 pack_manage 调用）
# ---------------------------------------------------------------------------

_PACK_BODY = """# {name} —— {description}

> 这是**包说明书**：AB 要用这个包时，第一个读的就是它。
> 包内任何东西变了（加/删/改工具、换依赖、换来源），**记得同步改这里** ——
> 说明书与包内容对不上，比没有说明书更坏。

## 一、这个包是干什么的

{description}

## 二、提供了什么（包工具）

| 包工具 | 在哪 | 怎么用 |
|---|---|---|
| （示例） | 包内 `scripts/` | （示例：execute_shell 跑它） |

> 包工具不分地位高低（脚本 / 技能 / MCP 站点 / 文档都算），统一为这个包的目的服务。

## 三、怎么用（推荐顺序）

1. 先并行读：本说明书 + 包内文档 + 需要的配置；
2. 再按任务挑合适的工具干活；
3. 干完把这次踩到的坑补进第五节。

## 四、怎么管理（生命周期）

- 要不要常驻进程？（要的话：怎么启、怎么停、谁负责）
- 前置依赖？（环境 / 包 / 账号 / 凭证）
- 有没有需要定期更新的东西？（外部数据源、上游版本）

## 五、其它

- 来源 / 获取方式：
- 已知坑：
- 集成结果（全部成功 / 部分成功，缺了什么）：
"""


def create_pack(name: str, description: str = "",
                packs: Optional[Path] = None) -> Tuple[bool, str]:
    """建一个包的骨架（文件夹 + `PACK.md` 模板）。返回 (成功, 给人看的话)。"""
    name = str(name or "").strip()
    desc = str(description or "").strip()
    if not valid_name(name):
        return False, ("❌ 包名不合法：只允许字母/数字/下划线/短横，1~32 字符（拿到的是 %r）" % name)

    root = packs_dir(packs)
    folder = root / name
    doc = folder / PACK_DOC
    if doc.is_file():
        return False, ("❌ 包 `%s` 已经存在（%s）—— 要改内容直接改它，别无脑重建。" % (name, doc))
    if folder.exists() and not folder.is_dir():
        return False, "❌ %s 已被同名文件占用，换个包名。" % folder

    try:
        folder.mkdir(parents=True, exist_ok=True)
        fm = yaml.safe_dump({"name": name, "description": desc},
                            allow_unicode=True, sort_keys=False,
                            width=4096, default_flow_style=False).strip()
        body = _PACK_BODY.split("{name}")[0] + name + _PACK_BODY.split("{name}", 1)[1]
        body = body.split("{description}")[0] + (desc or "（待填）") + body.split("{description}", 1)[1]
        doc.write_text("---\n" + fm + "\n---\n\n" + body, encoding="utf-8", newline="\n")
    except Exception as e:
        return False, "❌ 建包失败：%s: %s" % (type(e).__name__, e)

    syn = sync_registry(packs=root)
    return True, ("✅ 已建包 `%s`\n  说明书：%s\n  注册表：%s\n"
                  "  接下来按 `PACK.md` 里的小节把内容填进去；改完记得让说明书与实际一致。\n"
                  "  ⚠️ 本会话注入的注册表是**快照** —— 新包在盘上立刻存在（read_file 就能读到），"
                  "但要让「注册表那一段」刷新，得开新对话。"
                  % (name, doc, syn.summary()))


def remove_pack(name: str, packs: Optional[Path] = None) -> Tuple[bool, str]:
    """移除一个包（整个文件夹挪进 `.trash/`，可恢复，不真删）。"""
    name = str(name or "").strip()
    root = packs_dir(packs)
    folder = root / name
    if not (folder / PACK_DOC).is_file():
        return False, ("❌ 没有叫 %s 的包。现有的：%s"
                       % (name, ", ".join(known_names(root)) or "（一个都没有）"))
    tr = trash_dir(root)
    dest = tr / name
    try:
        tr.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest = tr / ("%s-%s" % (name, datetime.now().strftime("%Y%m%d%H%M%S")))
        shutil.move(str(folder), str(dest))
    except Exception as e:
        return False, "❌ 移除失败：%s: %s" % (type(e).__name__, e)

    syn = sync_registry(packs=root)
    return True, ("✅ 已移除包 `%s`（挪到回收站，可恢复：%s）\n  注册表：%s"
                  % (name, dest, syn.summary()))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AetherBreath 集成包注册表同步工具")
    ap.add_argument("--check", action="store_true", help="只报告差异，不写盘")
    ap.add_argument("--show", action="store_true", help="打印注入上下文的注册表块")
    ap.add_argument("--list", action="store_true", help="列出包与问题")
    ap.add_argument("--packs-dir", default=None, help="包根目录（默认 agent_integration_packs）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = Path(args.packs_dir).resolve() if args.packs_dir else None

    if args.show:
        block = build_injection_block(packs=root)
        print(block if block else "(没有集成包)")
        return 0

    if args.list:
        scan = scan_packs(root)
        print("包根目录：%s" % packs_dir(root))
        print("注册表：%s" % registry_path(root))
        if not scan.packs:
            print("（还没有任何包；建一个 <包名>/PACK.md 文件夹即可）")
        for p in scan.packs:
            print("· %-18s %s" % (p.name, p.description or "（未写用途）"))
        for i in scan.issues:
            print("  - %s" % i)
        return 0

    res = sync_registry(packs=root, write=not args.check)
    verb = "待更新" if args.check else ("已更新" if res.changed else "已最新")
    print("[PACK_REGISTRY.md] %s: %s" % (verb, res.registry_path))
    print("  差异摘要: %s" % res.summary())
    for i in res.issues:
        print("  - %s" % i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
