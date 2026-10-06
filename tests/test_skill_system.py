# -*- coding: utf-8 -*-
"""
技能系统测试（agent/skill_system.py）
=====================================
覆盖：扫描发现 / frontmatter 解析与容错 / 注册表渲染 / 首次创建 /
幂等（无变化不重写）/ 增删 diff 摘要 / 注入块。

运行（项目根）：
    venv/Scripts/python -m pytest tests/test_skill_system.py -q
"""

import sys
from pathlib import Path

import pytest
import yaml

# 让测试能直接 import agent/ 下的 skill_system（与 agent.py 同目录导入方式一致）
AGENT_DIR = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT_DIR))

from skill_system import (  # noqa: E402
    ScanResult,
    SyncResult,
    build_injection_block,
    render_registry,
    render_rows,
    scan_skills,
    sync_registry,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def write_skill(base: Path, folder: str, fm: dict, doc_name: str = "SKILL.md",
                body: str = "# 正文\n技能说明内容") -> Path:
    """在 base 下创建 <folder>/<doc_name>，frontmatter 由 dict 序列化。"""
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )
    p = d / doc_name
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def project(tmp_path: Path):
    """临时项目：agent_skills/ 下含 2 个合法技能 + 若干非法样本。"""
    skills = tmp_path / "agent_skills"
    skills.mkdir(parents=True, exist_ok=True)

    # 合法 1：大写 SKILL.md + tags 数组
    write_skill(
        skills, "alpha",
        {"name": "alpha", "description": "搜索与抓取", "version": "1.0.0",
         "tags": ["search", "web"]},
    )
    # 合法 2：小写 skill.md + tags 字符串 + 缺 version（用默认）
    write_skill(
        skills, "beta",
        {"name": "beta", "description": "代码审查", "tags": "code, review"},
        doc_name="skill.md",
    )
    # 非法：name 与文件夹不一致
    write_skill(
        skills, "bad-name",
        {"name": "other-name", "description": "名字对不上"},
    )
    # 非法：无 frontmatter
    (skills / "no-fm").mkdir()
    (skills / "no-fm" / "SKILL.md").write_text("# 没有 frontmatter\n", encoding="utf-8")
    # 非法：文件夹里没有正文文件
    (skills / "empty-folder").mkdir()
    # 散文件：不算技能
    (skills / "loose.md").write_text("hi", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# 扫描与解析
# ---------------------------------------------------------------------------

def test_scan_discovers_valid_skills(project: Path):
    result = scan_skills(project_root=project)
    names = [s.name for s in result.skills]
    assert names == ["alpha", "beta"]  # 按名字排序
    assert all(isinstance(s.doc_file, Path) for s in result.skills)


def test_scan_reports_invalid_skills(project: Path):
    result = scan_skills(project_root=project)
    joined = "\n".join(result.issues)
    assert "[bad-name]" in joined and "不一致" in joined
    assert "[no-fm]" in joined
    assert "[empty-folder]" in joined


def test_frontmatter_defaults_and_tag_formats(project: Path):
    result = scan_skills(project_root=project)
    by_name = {s.name: s for s in result.skills}
    # beta 缺 version → 默认；字符串 tags → 解析为数组
    assert by_name["beta"].version == "0.1.0"
    assert by_name["beta"].tags == ["code", "review"]
    assert by_name["alpha"].tags == ["search", "web"]
    assert by_name["alpha"].version == "1.0.0"


def test_doc_relpath_is_github_friendly(project: Path):
    result = scan_skills(project_root=project)
    for s in result.skills:
        rp = s.doc_relpath
        assert not rp.startswith("/") and "\\" not in rp
        assert rp.startswith("agent_skills/")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def test_render_rows_escapes_cells():
    from skill_system import SkillInfo

    s = SkillInfo(
        name="weird",
        folder="weird",
        description="含 | 竖线\n和换行",
        version="1.2.3",
        tags=["a", "b"],
        doc_file=Path("agent_skills/weird/SKILL.md"),
        project_root=Path("."),
    )
    row = render_rows([s])
    assert "\\|" in row  # 竖线被转义
    # 单元格已被压成单行（换行 → 空格），只留一个逻辑行
    assert row.count("\n") == 0
    assert "含 | 竖线 和换行" in row.replace("\\|", "|")


def test_render_registry_has_header_and_rows(project: Path):
    scan = scan_skills(project_root=project)
    text = render_registry(scan.skills, timestamp="2026-09-06 12:00:00")
    assert text.startswith("# SKILL_REGISTRY.md(技能注册表)")
    assert "> 自动生成，请勿手动编辑。" in text
    assert "> 最后更新: 2026-09-06 12:00:00" in text
    assert "| 技能名 | 描述 | 版本 | 标签 |" in text
    assert "| alpha | 搜索与抓取 | 1.0.0 | search, web |" in text
    assert "| beta | 代码审查 | 0.1.0 | code, review |" in text
    # 非法样本不进表
    assert "bad-name" not in text
    assert "no-fm" not in text


# ---------------------------------------------------------------------------
# 同步：首次创建 / 幂等 / 增删
# ---------------------------------------------------------------------------

def test_sync_first_run_creates_registry(project: Path):
    registry = project / "agent_skills" / "SKILL_REGISTRY.md"
    assert not registry.exists()
    result = sync_registry(project_root=project)
    assert result.changed is True
    assert result.registry_path == registry
    assert registry.exists()
    text = registry.read_text(encoding="utf-8")
    assert "| alpha |" in text and "| beta |" in text


def test_sync_is_idempotent(project: Path):
    registry = project / "agent_skills" / "SKILL_REGISTRY.md"
    first = sync_registry(project_root=project)
    assert first.changed is True
    text_after_first = registry.read_text(encoding="utf-8")

    second = sync_registry(project_root=project)
    assert second.changed is False
    assert second.added == [] and second.removed == [] and second.updated == []
    # 无变化时不应重写（时间戳保持不变 = 文件未被触碰）
    assert registry.read_text(encoding="utf-8") == text_after_first


def test_sync_detects_add_and_remove(project: Path):
    skills = project / "agent_skills"
    sync_registry(project_root=project)

    # 新增 gamma
    write_skill(skills, "gamma", {"name": "gamma", "description": "新技能",
                                  "version": "0.2.0", "tags": ["new"]})
    add_result = sync_registry(project_root=project)
    assert add_result.changed is True
    assert [s.name for s in add_result.added] == ["gamma"]
    assert add_result.removed == []
    assert "| gamma | 新技能 | 0.2.0 | new |" in registry_text(project)

    # 删除 gamma
    import shutil
    shutil.rmtree(skills / "gamma")
    del_result = sync_registry(project_root=project)
    assert del_result.changed is True
    assert del_result.removed == ["gamma"]
    assert "| gamma |" not in registry_text(project)


def test_sync_detects_update(project: Path):
    skills = project / "agent_skills"
    sync_registry(project_root=project)

    # alpha 升版本
    write_skill(skills, "alpha", {"name": "alpha", "description": "搜索与抓取",
                                  "version": "1.1.0", "tags": ["search", "web"]})
    result = sync_registry(project_root=project)
    assert result.changed is True
    assert result.updated, "应检测到版本更新"
    old, new = result.updated[0]
    assert old.name == "alpha" and old.version == "1.0.0" and new.version == "1.1.0"
    assert "v1.0.0→v1.1.0" in result.summary()


def test_check_mode_does_not_write(project: Path):
    registry = project / "agent_skills" / "SKILL_REGISTRY.md"
    result = sync_registry(project_root=project, write=False)
    assert result.changed is True
    assert not registry.exists()


# ---------------------------------------------------------------------------
# 注入块
# ---------------------------------------------------------------------------

def test_build_injection_block(project: Path):
    block = build_injection_block(project_root=project)
    assert "技能注册表" in block
    assert "agent_skills/<技能名>/SKILL.md" in block or "SKILL.md" in block
    assert "| alpha | 搜索与抓取 | 1.0.0 | search, web |" in block
    assert "| beta |" in block


def test_injection_block_auto_creates_registry(project: Path):
    registry = project / "agent_skills" / "SKILL_REGISTRY.md"
    assert not registry.exists()
    block = build_injection_block(project_root=project)
    assert registry.exists()  # 注入前自动同步了一次
    assert "alpha" in block


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def registry_text(project: Path) -> str:
    p = project / "agent_skills" / "SKILL_REGISTRY.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
