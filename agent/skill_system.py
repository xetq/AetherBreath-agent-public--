# -*- coding: utf-8 -*-
"""
AetherBreath 技能系统
====================

以「技能文件夹 = 一个技能」为单位，复制粘贴即注册：

    agent_skills/<技能名>/SKILL.md      (或 skill.md，大小写不强制)

元数据写在技能正文开头的 YAML frontmatter 中：

    ---
    name: research          # 必填，须与所在文件夹名一致
    description: ...        # 建议填写
    version: 1.0.0          # 缺省 0.1.0
    tags: [search, web]     # 缺省空；也接受 "search, web" 字符串
    ---

每次新会话启动时调用一次 sync_registry()：

    1. 扫描技能库 → 解析每个技能的 frontmatter；
    2. 渲染注册表 Markdown 表格（SKILL_REGISTRY.md，与旧文件比较）；
    3. 有差异 → 重写注册表（多增少删），返回变更摘要；无差异 → 不写盘；
    4. load_system_prompt() 把注册表全文作为「技能目录快照」注入上下文，
       会话内冻结；需要执行技能时再按路径读取技能正文（渐进式披露）。

工程师可读/可解析的注册表格式（Markdown 表格）：

    # 技能注册表

    > 自动生成，请勿手动编辑。
    > 最后更新: 2026-09-06 14:32:01

    | 技能名 | 描述 | 版本 | 标签 |
    |--------|------|------|------|
    | research | ... | 1.0.0 | search, web |

命令行用法（工程师手动维护/CI 检查时用）：

    python agent/skill_system.py            # 同步注册表并打印摘要
    python agent/skill_system.py --check    # 只报告差异，不写文件
    python agent/skill_system.py --show     # 打印将注入 LLM 上下文的注册表块
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# 常量与默认值（相对项目根，GitHub 友好；调用方可显式覆盖）
# ---------------------------------------------------------------------------

DEFAULT_SKILLS_DIR = "agent_skills"
DEFAULT_REGISTRY_NAME = "SKILL_REGISTRY.md"

# 技能正文文件名候选（大小写不强制，两个都找；同目录同时存在时 SKILL.md 优先）
DOC_CANDIDATES: Tuple[str, ...] = ("SKILL.md", "skill.md")

DEFAULT_VERSION = "0.1.0"  # frontmatter 缺 version 时的默认值

_HEADER_TEMPLATE = """# SKILL_REGISTRY.md(技能注册表)

> 自动生成，请勿手动编辑。
> 最后更新: {timestamp}

| 技能名 | 描述 | 版本 | 标签 |
|--------|------|------|------|
"""

_INJECT_GUIDE = (
    "下方为当前已注册的全部技能（技能名即所在文件夹名，正文文件为 "
    "`agent_skills/<技能名>/SKILL.md` 或 `skill.md`）。\n"
    "需要用到某技能时，先读取对应正文了解步骤，再按其执行；正文内容不在此处展开。"
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SkillInfo:
    """一个已注册技能的元数据（来自 frontmatter + 目录探测）。"""

    name: str                 # frontmatter name（须与文件夹名一致）
    folder: str               # 所在文件夹名
    description: str
    version: str
    tags: List[str]
    doc_file: Path            # 技能正文文件绝对路径
    project_root: Path

    @property
    def doc_relpath(self) -> str:
        """技能正文相对项目根路径（POSIX 风格，GitHub 友好）。"""
        return self.doc_file.relative_to(self.project_root).as_posix()

    @property
    def folder_relpath(self) -> str:
        """技能文件夹相对项目根路径（POSIX 风格）。"""
        return self.doc_file.parent.relative_to(self.project_root).as_posix()


@dataclass
class ScanResult:
    """一次技能库扫描的结果。"""

    skills: List[SkillInfo] = field(default_factory=list)  # 合法技能（按 name 排序）
    issues: List[str] = field(default_factory=list)        # 无效项 / 警告（人类可读）


@dataclass
class SyncResult:
    """注册表同步结果：本次启动与技能库的差异。"""

    changed: bool
    added: List[SkillInfo]          # 本次新增的技能
    removed: List[str]              # 本次移除的技能（名称）
    updated: List[Tuple[SkillInfo, SkillInfo]]  # (旧, 新)
    issues: List[str]               # 扫描中发现的问题（不进注册表）
    registry_path: Path

    def summary(self) -> str:
        parts: List[str] = []
        if self.added:
            parts.append("新增: " + ", ".join(f"{s.name}(v{s.version})" for s in self.added))
        if self.removed:
            parts.append("移除: " + ", ".join(self.removed))
        if self.updated:
            parts.append(
                "更新: "
                + ", ".join(f"{o.name}: v{o.version}→v{n.version}" for o, n in self.updated)
            )
        if not parts:
            return "无变化"
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------

def _project_root_of() -> Path:
    """skill_system.py 所在项目根（agent/ 的上一级）。"""
    return Path(__file__).resolve().parent.parent


def _resolve_paths(
    project_root: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
    registry_path: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """统一解析 skills 目录与注册表路径；缺省时按项目根相对默认值。"""
    root = Path(project_root).resolve() if project_root else _project_root_of()
    skills = Path(skills_dir).resolve() if skills_dir else (root / DEFAULT_SKILLS_DIR)
    registry = (
        Path(registry_path).resolve()
        if registry_path
        else (root / DEFAULT_SKILLS_DIR / DEFAULT_REGISTRY_NAME)
    )
    return skills, registry


# ---------------------------------------------------------------------------
# frontmatter 解析
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
    fm = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1:])
    return fm, body


def _parse_tags(value) -> List[str]:
    """tags 字段宽松解析：YAML list、逗号字符串均可。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for v in value:
            out.extend(_parse_tags(v))
        return out
    return []


def _clean_cell(value: str) -> str:
    """清理表格单元格：压成单行、转义竖线。"""
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def _read_skill_doc(folder: Path) -> Optional[Path]:
    """在技能文件夹中寻找正文文件（SKILL.md 或 skill.md，大小写不强制）。"""
    for cand in DOC_CANDIDATES:
        p = folder / cand
        if p.is_file():
            return p
    return None


def _parse_skill(folder: Path, project_root: Path, issues: List[str]) -> Optional[SkillInfo]:
    """解析单个技能文件夹 → SkillInfo；无效则记录 issue 并返回 None。"""
    doc = _read_skill_doc(folder)
    if doc is None:
        issues.append(
            f"[{folder.name}] 缺少技能正文文件（{'/'.join(DOC_CANDIDATES)}），未注册"
        )
        return None

    text = doc.read_text(encoding="utf-8")
    fm = _split_frontmatter(text)
    if fm is None:
        issues.append(f"[{folder.name}] 正文未以 --- frontmatter 开头，未注册")
        return None

    try:
        meta = yaml.safe_load(fm[0]) or {}
    except yaml.YAMLError as e:
        issues.append(f"[{folder.name}] frontmatter YAML 解析失败: {e}")
        return None
    if not isinstance(meta, dict):
        issues.append(f"[{folder.name}] frontmatter 不是键值映射，未注册")
        return None

    name = str(meta.get("name", "")).strip()
    if not name:
        issues.append(f"[{folder.name}] frontmatter 缺少 name，未注册")
        return None
    if name != folder.name:
        issues.append(
            f"[{folder.name}] frontmatter name '{name}' 与文件夹名不一致（须一致，"
            "保证注册表技能名可定位到正文），未注册"
        )
        return None

    version = str(meta.get("version", DEFAULT_VERSION)).strip() or DEFAULT_VERSION
    tags = _parse_tags(meta.get("tags"))
    description = str(meta.get("description", "")).strip()

    if not description:
        issues.append(f"[{folder.name}] 未提供 description（建议补充，便于注册表检索）")
    if "version" not in meta:
        issues.append(f"[{folder.name}] frontmatter 缺少 version，默认 {DEFAULT_VERSION}")

    return SkillInfo(
        name=name,
        folder=folder.name,
        description=description,
        version=version,
        tags=tags,
        doc_file=doc,
        project_root=project_root,
    )


def scan_skills(
    project_root: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
) -> ScanResult:
    """扫描技能库目录，返回合法技能列表（按 name 排序）+ 问题列表。"""
    skills, _ = _resolve_paths(project_root, skills_dir)
    result = ScanResult()

    if not skills.is_dir():
        result.issues.append(f"技能目录不存在: {skills}")
        return result

    for entry in sorted(skills.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue  # 注册表/README 等散文件不算技能
        if entry.name.startswith("."):
            continue
        skill = _parse_skill(entry, skills.parent, result.issues)
        if skill is not None:
            result.skills.append(skill)

    result.skills.sort(key=lambda s: s.name.lower())
    return result


# ---------------------------------------------------------------------------
# 注册表渲染 / 比较
# ---------------------------------------------------------------------------

def render_rows(skills: List[SkillInfo]) -> str:
    """只渲染表格数据行（不含标题/表头），用于内容比较。"""
    if not skills:
        return ""
    rows = []
    for s in skills:
        tags = ", ".join(s.tags)
        rows.append(f"| {s.name} | {_clean_cell(s.description)} | {s.version} | {tags} |")
    return "\n".join(rows)


def render_registry(skills: List[SkillInfo], timestamp: Optional[str] = None) -> str:
    """渲染注册表全文（Markdown 表格，格式见模块 docstring）。"""
    ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = render_rows(skills)
    return _HEADER_TEMPLATE.format(timestamp=ts) + (body + "\n" if body else "")


def _extract_body(text: str) -> str:
    """从已有注册表文本中提取「数据行部分」（去掉标题/注释/表头），用于比较。"""
    rows: List[str] = []
    started = False
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("| 技能名") or s.startswith("|技能名"):
            started = True
            continue
        if s.startswith("|"):
            if s.lstrip("|").lstrip().startswith("-") or s.lstrip("|").lstrip().startswith(":"):
                continue  # 表头分隔行
            if started:
                rows.append(s)
    return "\n".join(rows)


def _parse_registry_rows(text: str) -> Dict[str, Dict[str, str]]:
    """解析已有注册表 → {name: {description, version, tags}}，用于 diff 摘要。"""
    out: Dict[str, Dict[str, str]] = {}
    for ln in _extract_body(text).splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        name = cells[0].replace("\\|", "|")
        if not name:
            continue
        out[name] = {
            "description": cells[1].replace("\\|", "|"),
            "version": cells[2].replace("\\|", "|"),
            "tags": cells[3].replace("\\|", "|"),
        }
    return out


# ---------------------------------------------------------------------------
# 同步（核心入口）
# ---------------------------------------------------------------------------

def sync_registry(
    project_root: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    write: bool = True,
) -> SyncResult:
    """
    扫描技能库并与现有注册表比较（多增少删）。

    返回 SyncResult；write=False 时只报告不写盘（--check）。
    注：表头「最后更新」时间戳不参与内容比较——正文行无变化即视为无变化，
    避免每次启动都因为时间戳而重写文件。
    """
    skills, registry = _resolve_paths(project_root, skills_dir, registry_path)
    scan = scan_skills(skills_dir=skills)

    new_rows = render_rows(scan.skills)

    old_rows = ""
    old_text = ""
    if registry.is_file():
        old_text = registry.read_text(encoding="utf-8")
        old_rows = _extract_body(old_text)

    changed = (new_rows != old_rows)
    added: List[SkillInfo] = []
    removed: List[str] = []
    updated: List[Tuple[SkillInfo, SkillInfo]] = []

    if changed:
        old_skills = _parse_registry_rows(old_text)
        new_skills = {s.name: s for s in scan.skills}
        for name, s in new_skills.items():
            if name not in old_skills:
                added.append(s)
        for name in old_skills:
            if name not in new_skills:
                removed.append(name)
        for name, s in new_skills.items():
            if name in old_skills:
                o = old_skills[name]
                if (
                    o["version"] != s.version
                    or o["tags"] != ", ".join(s.tags)
                    or o["description"] != _clean_cell(s.description)
                ):
                    old_skill = SkillInfo(
                        name=name,
                        folder=name,
                        description=o["description"],
                        version=o["version"],
                        tags=_parse_tags(o["tags"]),
                        doc_file=skills / name / DOC_CANDIDATES[0],
                        project_root=skills.parent,
                    )
                    updated.append((old_skill, s))

        if write:
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(render_registry(scan.skills), encoding="utf-8")

    return SyncResult(
        changed=changed,
        added=added,
        removed=sorted(removed),
        updated=updated,
        issues=scan.issues,
        registry_path=registry,
    )


# ---------------------------------------------------------------------------
# 注入块（load_system_prompt 调用）
# ---------------------------------------------------------------------------

def build_injection_block(
    project_root: Optional[Path] = None,
    skills_dir: Optional[Path] = None,
    registry_path: Optional[Path] = None,
) -> str:
    """
    构建注入 LLM 上下文的注册表块：引导说明 + 注册表全文（会话快照）。

    注册表文件不存在时先同步生成一次。
    """
    skills, registry = _resolve_paths(project_root, skills_dir, registry_path)
    if not registry.is_file():
        sync_registry(project_root=skills.parent, skills_dir=skills, registry_path=registry)
    if not registry.is_file():
        return ""
    text = registry.read_text(encoding="utf-8").strip()
    return "## SKILL_REGISTRY.md(技能注册表)（会话快照 · 自动注入）\n\n" + _INJECT_GUIDE + "\n\n" + text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AetherBreath 技能注册表同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  python agent/skill_system.py            # 同步注册表并打印摘要\n"
        "  python agent/skill_system.py --check    # 只报告差异, 不写文件\n"
        "  python agent/skill_system.py --show     # 打印注入 LLM 上下文的注册表块",
    )
    parser.add_argument("--check", action="store_true", help="只报告差异，不写盘")
    parser.add_argument("--show", action="store_true", help="打印注入上下文的注册表块")
    parser.add_argument("--skills-dir", default=None, help="技能库目录（默认 agent_skills）")
    parser.add_argument("--registry", default=None, help="注册表文件（默认 agent_skills/SKILL_REGISTRY.md）")
    args = parser.parse_args(argv)

    if args.show:
        block = build_injection_block(skills_dir=Path(args.skills_dir) if args.skills_dir else None,
                                      registry_path=Path(args.registry) if args.registry else None)
        print(block if block else "(技能库为空)")
        return 0

    result = sync_registry(
        skills_dir=Path(args.skills_dir) if args.skills_dir else None,
        registry_path=Path(args.registry) if args.registry else None,
        write=not args.check,
    )
    verb = "待更新" if args.check else ("已更新" if result.changed else "已最新")
    print(f"[SKILL_REGISTRY.md(技能注册表)] {verb}: {result.registry_path}")
    print(f"  差异摘要: {result.summary()}")
    if result.issues:
        print("  提示:")
        for issue in result.issues:
            print(f"    - {issue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
