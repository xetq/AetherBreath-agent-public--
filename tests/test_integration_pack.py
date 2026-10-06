# -*- coding: utf-8 -*-
"""
集成包系统测试（agent/integration_pack.py + agent_tools/pack_manage.py）
=========================================================================
覆盖：扫描发现 / frontmatter 容错 / 注册表渲染与 diff 归类 / 幂等 /
建包模板 / 移除进 .trash / 注入块（含"一个包都没有 → 不注入"）/ 工具层三动作。

运行（项目根）：
    venv/Scripts/python -m pytest tests/test_integration_pack.py -q
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

from integration_pack import (          # noqa: E402
    PACK_DOC,
    REGISTRY_NAME,
    build_injection_block,
    create_pack,
    known_names,
    pack_doc,
    pack_exists,
    remove_pack,
    render_rows,
    scan_packs,
    sync_registry,
    valid_name,
)


# ---------------------------------------------------------------------------
# fixtures / 小工具
# ---------------------------------------------------------------------------

def write_pack(base: Path, folder: str, fm: dict, body: str = "# 正文\n包说明") -> Path:
    """在 base 下创建 <folder>/PACK.md，frontmatter 由 dict 序列化。"""
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    content = ("---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
               + "---\n\n" + body)
    p = d / PACK_DOC
    p.write_text(content, encoding="utf-8", newline="\n")
    return p


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    """临时包根目录：2 个合法包 + 3 个非法样本。"""
    base = tmp_path / "packs"
    base.mkdir()
    write_pack(base, "alpha", {"name": "alpha", "description": "第一个包"})
    write_pack(base, "beta", {"name": "beta", "description": "第二个包"})
    d = base / "nofm"                      # 非法 1：没有 frontmatter
    d.mkdir()
    (d / PACK_DOC).write_text("正文而已", encoding="utf-8")
    write_pack(base, "mismatch", {"name": "other", "description": "x"})   # 非法 2：name 不符
    (base / "empty").mkdir()               # 非法 3：没有 PACK.md
    return base


def reg_text(base: Path) -> str:
    p = base / REGISTRY_NAME
    return p.read_text(encoding="utf-8") if p.exists() else ""


def fm_of(doc: Path) -> dict:
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("---")
    return yaml.safe_load(text.split("---", 2)[1])


# ---------------------------------------------------------------------------
# 扫描与容错
# ---------------------------------------------------------------------------

def test_scan_finds_valid_packs_only(packs: Path):
    res = scan_packs(packs)
    assert [p.name for p in res.packs] == ["alpha", "beta"]


def test_scan_reports_each_bad_sample(packs: Path):
    res = scan_packs(packs)
    joined = " | ".join(res.issues)
    assert "nofm" in joined            # 没有 frontmatter
    assert "mismatch" in joined        # name 与文件夹名不一致
    assert "empty" in joined           # 缺 PACK.md


def test_empty_description_flagged_and_rendered(tmp_path: Path):
    base = tmp_path / "p"
    base.mkdir()
    write_pack(base, "nodesc", {"name": "nodesc"})
    res = scan_packs(base)
    assert [p.name for p in res.packs] == ["nodesc"]
    assert any("description" in i for i in res.issues)
    assert "（未写用途）" in render_rows(res.packs)


def test_dotted_dirs_are_not_packs(packs: Path):
    (packs / ".trash").mkdir()
    (packs / ".trash" / "old").mkdir()
    assert [p.name for p in scan_packs(packs).packs] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def test_rows_have_exactly_three_columns(packs: Path):
    rows = render_rows(scan_packs(packs).packs)
    line = [l for l in rows.splitlines() if l.startswith("| alpha |")][0]
    assert line.count("|") == 4        # 三列 = 4 个竖线
    assert PACK_DOC in line            # 第三列是说明书路径


def test_pipe_in_description_does_not_break_table(tmp_path: Path):
    base = tmp_path / "p"
    base.mkdir()
    write_pack(base, "pipey", {"name": "pipey", "description": "a | b"})
    sync_registry(packs=base)
    line = [l for l in reg_text(base).splitlines() if l.startswith("| pipey |")]
    assert len(line) == 1
    assert "\\|" in line[0]


def test_sync_writes_once_then_is_idempotent(packs: Path):
    res = sync_registry(packs=packs)
    assert res.changed
    assert sorted(p.name for p in res.added) == ["alpha", "beta"]
    assert (packs / REGISTRY_NAME).exists()
    assert sync_registry(packs=packs).changed is False


def test_sync_classifies_add_update_remove(packs: Path):
    sync_registry(packs=packs)
    write_pack(packs, "gamma", {"name": "gamma", "description": "第三包"})
    r1 = sync_registry(packs=packs)
    assert r1.changed and [p.name for p in r1.added] == ["gamma"]

    write_pack(packs, "beta", {"name": "beta", "description": "改过的用途"})
    r2 = sync_registry(packs=packs)
    assert r2.changed and r2.updated == ["beta"]

    # 直接删文件夹（绕过 remove_pack —— 它内部自带一次 sync，会先把注册表刷新掉），
    # 这样注册表仍停在旧状态，才能真正验证 sync 的 removed 归类。
    import shutil
    shutil.rmtree(str(packs / "gamma"))
    r3 = sync_registry(packs=packs)
    assert "gamma" in r3.removed
    assert "| gamma |" not in reg_text(packs)


# ---------------------------------------------------------------------------
# 建包 / 移除
# ---------------------------------------------------------------------------

def test_create_pack_writes_template_and_syncs(tmp_path: Path):
    base = tmp_path / "p"
    ok, msg = create_pack("demo", "演示包", packs=base)
    assert ok and "demo" in msg
    doc = base / "demo" / PACK_DOC
    assert doc.exists()
    meta = fm_of(doc)
    assert meta["name"] == "demo"
    assert meta["description"] == "演示包"
    body = doc.read_text(encoding="utf-8")
    assert body.count("## ") >= 5                     # 五个小节都在
    assert (base / REGISTRY_NAME).exists()            # create 内部同步了一次
    assert "| demo |" in reg_text(base)


def test_create_rejects_bad_name_and_duplicate(tmp_path: Path):
    base = tmp_path / "p"
    assert create_pack("bad name", "x", packs=base)[0] is False
    assert create_pack("", "x", packs=base)[0] is False
    assert create_pack("ok_pack", "x", packs=base)[0] is True
    assert create_pack("ok_pack", "x", packs=base)[0] is False


def test_remove_moves_to_trash_and_unregisters(tmp_path: Path):
    base = tmp_path / "p"
    create_pack("demo", "演示包", packs=base)
    ok, msg = remove_pack("demo", packs=base)
    assert ok
    assert (base / ".trash" / "demo" / PACK_DOC).exists()   # 可恢复
    assert not pack_exists("demo", packs=base)
    assert "| demo |" not in reg_text(base)
    assert [p.name for p in scan_packs(base).packs] == []    # .trash 不算包


def test_remove_missing_pack_is_business_failure(tmp_path: Path):
    base = tmp_path / "p"
    base.mkdir()
    ok, msg = remove_pack("nope", packs=base)
    assert ok is False and msg.startswith("❌")


# ---------------------------------------------------------------------------
# 注入块
# ---------------------------------------------------------------------------

def test_injection_block_skips_when_no_packs(tmp_path: Path):
    base = tmp_path / "p"
    base.mkdir()
    assert build_injection_block(packs=base) == ""


def test_injection_block_carries_guide_and_rows(packs: Path):
    sync_registry(packs=packs)
    blk = build_injection_block(packs=packs)
    assert "PACK_REGISTRY.md（集成包注册表）" in blk
    assert "不得自行开始集成" in blk            # 纪律必须随注册表一起到模型手里
    assert "| alpha |" in blk and "| beta |" in blk
    assert PACK_DOC in blk


# ---------------------------------------------------------------------------
# 包名校验（防路径逃逸）
# ---------------------------------------------------------------------------

def test_valid_name_rules():
    assert valid_name("a_b-1")
    assert not valid_name("bad name")
    assert not valid_name("")
    assert not valid_name("a" * 33)
    assert not valid_name("../evil")
    assert not valid_name("包名")


# ---------------------------------------------------------------------------
# 工具层
# ---------------------------------------------------------------------------

def test_pack_manage_tool_end_to_end(tmp_path: Path, monkeypatch):
    base = tmp_path / "packs"
    base.mkdir()
    monkeypatch.setenv("AETHER_INTEGRATION_PACKS", str(base))

    import agent_tools
    assert "pack_manage" in agent_tools.AVAILABLE_TOOLS
    names = [s.get("function", {}).get("name") for s in agent_tools.TOOLS_SCHEMA]
    assert "pack_manage" in names
    assert "pack_manage" in agent_tools.NON_IDEMPOTENT_TOOLS
    pm = agent_tools.AVAILABLE_TOOLS["pack_manage"]

    assert pm(action="create", name="tpack", description="工具层测试").startswith("✅")
    out = pm(action="read", name="tpack")
    assert "PACK.md 正文" in out and "包内文件" in out
    assert pm(action="read", name="ghost").startswith("❌")
    assert pm(action="bogus").startswith("❌")
    assert pm(action="remove", name="tpack").startswith("✅")
    assert pack_doc("tpack", packs=base).exists() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
