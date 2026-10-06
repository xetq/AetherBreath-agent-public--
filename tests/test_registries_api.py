# -*- coding: utf-8 -*-
"""技能 / 集成包快照（WebUI「运行时」面板的数据源）—— `agent_webui/backend/registry_view.py`

判据（照 tests/README.md）：一条命令、无需 LLM、无需网关、不起进程、不写真实文件。

钉住的东西（都是主人 2026-09-23 六条裁决里的）：
  · **实时扫文件夹**、**不读 .md 注册表** —— 那份 .md 是「新会话启动时冻结、注入给模型看」
    的注入快照，与磁盘现状可以不一致；有专门的哨兵用例守着这条；
  · 每行的**脸面只有名字 + 描述**（与 MCP 站同口径），version/tags 只是附赠字段；
  · 常驻：目录在就得出现；目录不存在 = 空态，不是崩溃；
  · 两块**各自独立成败**：技能库读崩了不许连累包区；
  · **只读盘**：调用前后目录内容一模一样。

跑法（项目根）：venv/Scripts/python -m pytest tests/test_registries_api.py -q
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent_webui" / "backend"))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))

import registry_view as V            # noqa: E402


class Env:
    """临时技能库 + 包目录（走 `skills_dir` / `packs_dir` 参数，不碰环境变量）。"""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="ab_regview_"))
        self.skills = self.root / "agent_skills"
        self.packs = self.root / "agent_integration_packs"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.packs.mkdir(parents=True, exist_ok=True)

    def add_skill(self, name, description="一个技能", version=None, tags=None):
        folder = self.skills / name
        folder.mkdir(parents=True, exist_ok=True)
        fm = ["---", "name: %s" % name, "description: %s" % description]
        if version:
            fm.append("version: %s" % version)
        if tags:
            fm.append("tags: [%s]" % ", ".join(tags))
        fm.append("---")
        (folder / "SKILL.md").write_text("\n".join(fm) + "\n\n# " + name + "\n", encoding="utf-8")
        return folder

    def add_pack(self, name, description="一个包"):
        folder = self.packs / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "PACK.md").write_text(
            "---\nname: " + name + "\ndescription: " + description + "\n---\n\n# " + name + "\n",
            encoding="utf-8")
        return folder

    def close(self):
        shutil.rmtree(str(self.root), ignore_errors=True)


@pytest.fixture()
def env():
    e = Env()
    try:
        yield e
    finally:
        e.close()


# ---------------- 常驻与形状 ----------------

def test_skills_are_resident_and_shaped(env):
    env.add_skill("alpha", description="甲技能", version="1.2.0", tags=["a", "b"])
    env.add_skill("beta")
    snap = V.skills_snapshot(env.skills)
    assert snap["ok"] is True, snap
    assert snap["count"] == 2 == len(snap["skills"]), snap
    a = [r for r in snap["skills"] if r["name"] == "alpha"][0]
    assert a["description"] == "甲技能"
    assert a["version"] == "1.2.0" and a["tags"] == ["a", "b"]
    assert a["doc"] == "agent_skills/alpha/SKILL.md", a["doc"]
    assert set(a) <= {"name", "description", "doc", "version", "tags"}, a


def test_packs_are_resident_and_shaped(env):
    env.add_pack("p1", description="包一")
    env.add_pack("p2")
    snap = V.packs_snapshot(env.packs)
    assert snap["ok"] is True, snap
    assert snap["count"] == 2 == len(snap["packs"])
    assert [r["name"] for r in snap["packs"]] == ["p1", "p2"], "按名字排序"
    assert snap["packs"][0]["doc"] == "agent_integration_packs/p1/PACK.md", snap["packs"][0]


# ---------------- Q1 哨兵：磁盘是真相，不是 .md ----------------

def test_md_registry_is_not_the_source_of_truth(env):
    """放一份写着"幽灵技能"的注册表 + 一个真技能 → 快照只报磁盘上真实存在的那个。"""
    env.add_skill("alpha")
    (env.skills / "SKILL_REGISTRY.md").write_text(
        "# SKILL_REGISTRY.md(技能注册表)\n\n| 技能名 | 描述 | 版本 | 标签 |\n|---|---|---|---|\n"
        "| ghost | 磁盘上并不存在的技能 | 9.9.9 | x |\n", encoding="utf-8")
    snap = V.skills_snapshot(env.skills)
    names = [r["name"] for r in snap["skills"]]
    assert names == ["alpha"], names
    assert "ghost" not in names, "注册表不是真相源（它是会话启动时冻结的注入快照）"
    assert snap["registry"] == "SKILL_REGISTRY.md"
    assert snap["registry_mtime"], "注册表在 → 要报更新时间，供人对照"


def test_registry_mtime_is_none_when_absent(env):
    snap = V.skills_snapshot(env.skills)
    assert snap["registry_mtime"] is None, "没有注册表就如实说没有"


# ---------------- 空态与独立成败 ----------------

def test_missing_dirs_are_empty_state_not_crash(env):
    missing = env.root / "nope"
    sk = V.skills_snapshot(missing)
    pk = V.packs_snapshot(missing)
    for s in (sk, pk):
        assert s["ok"] is True, s
        assert s["count"] == 0
        assert s["issues"], "目录不存在要说一句，别装没事"


def test_a_file_instead_of_a_dir_is_not_a_crash(env):
    """技能目录指向一个**文件**：技能区如实报空，不抛异常。"""
    junk = env.root / "not_a_dir.txt"
    junk.write_text("x", encoding="utf-8")
    snap = V.skills_snapshot(junk)
    assert snap["ok"] is True and snap["count"] == 0 and snap["issues"], snap


def test_two_zones_fail_independently(env):
    """一块坏了不许连累另一块。"""
    env.add_skill("alpha")
    env.add_pack("p1")
    whole = V.registries_snapshot(env.skills, env.root / "no_such_packs")
    assert whole["skills"]["ok"] is True and whole["skills"]["count"] == 1, whole["skills"]
    assert whole["packs"]["ok"] is True and whole["packs"]["count"] == 0, whole["packs"]


def test_snapshot_is_read_only(env):
    """只读盘：调用前后目录内容一模一样（不起进程、不写任何快照文件）。"""
    env.add_skill("alpha")
    env.add_pack("p1")
    before = sorted(p.as_posix() for p in env.root.rglob("*"))
    V.registries_snapshot(env.skills, env.packs)
    after = sorted(p.as_posix() for p in env.root.rglob("*"))
    assert before == after, after


# ---------------- 真仓库端到端 ----------------

def test_real_repo_loads(env=None):
    """真项目根下的技能库与包目录都要能读出来，且每行都有名字 + 描述。"""
    sk = V.skills_snapshot()
    pk = V.packs_snapshot()
    assert sk["ok"] is True, sk
    assert pk["ok"] is True, pk
    assert sk["count"] > 0 and sk["count"] == len(sk["skills"]), sk
    assert pk["count"] > 0 and pk["count"] == len(pk["packs"]), pk
    for r in sk["skills"] + pk["packs"]:
        assert r["name"] and r["description"], r
        assert r["doc"].startswith(("agent_skills/", "agent_integration_packs/")), r["doc"]


if __name__ == "__main__":                                       # 直跑（省得记 pytest 参数）
    sys.exit(pytest.main([__file__, "-q"]))
