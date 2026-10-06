# -*- coding: utf-8 -*-
"""self_maintenance 工具集回归测试。

判据（与 tests/ 其它文件一致）：一条命令、无需 LLM、无需网关、**不写真实文件**
（全部在 tmp_path 里造世界）。被测对象是三个自维护脚本里的**纯逻辑**：

    tools/snapshot.py    指纹扫描、排除清单、密钥判定、变更 diff、范围判定
    tools/log_digest.py  消息签名归一、账本事件分层
    tools/verify.py      代码目录扫描

⚠ 重点守护的是**踩过的坑**，不是"看起来对"：
  · JSON 往返把 tuple 变 list → 指纹比对全量误报（曾把 31413 个文件报成"已修改"）
  · 裸段名排除 → 差点把 agent_webui/frontend/src 真代码排除掉
  · 虚拟环境名写法多样 → agent_workspace/venvs/ 的 2.4 万个库文件混进指纹
  · 次生物目录在项目里不止一处 → 子代理的 working_memory 混进指纹
"""
import importlib.util
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "self_maintenance" / "tools"


def _load(name: str):
    """按路径加载自维护脚本（tools/ 不是包，用 importlib 直接取）。"""
    spec = importlib.util.spec_from_file_location(f"sm_{name}", _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SNAP = _load("snapshot")
DIGEST = _load("log_digest")


# ==================== snapshot：指纹与排除 ====================

def test_fingerprint_diff_survives_json_roundtrip(tmp_path):
    """守卫：指纹经 JSON 存盘再读回来（tuple→list）后，比对结果必须正确。

    这是真踩过的坑 —— 不做归一的话 `[a,b] != (a,b)` 恒为真，end 会把**每一个**
    文件都报成"已修改"，真正改的那个淹在几万条噪音里。
    """
    import json

    before = {"a.py": (10, 111), "b.py": (20, 222), "gone.py": (5, 1)}
    after = {"a.py": (10, 111), "b.py": (21, 999), "new.py": (7, 3)}
    # 模拟 begin 存盘 → end 读回
    roundtripped = json.loads(json.dumps(before))

    d = SNAP.diff_fingerprint(roundtripped, after)
    assert d["modified"] == ["b.py"], d
    assert d["added"] == ["new.py"], d
    assert d["deleted"] == ["gone.py"], d


def test_exclude_does_not_swallow_real_source_dirs():
    """守卫：排除清单不能用裸目录名，否则真代码目录（前端 src）会被静默排除。"""
    assert not SNAP._excluded("agent_webui/frontend/src/App.tsx")
    assert not SNAP._excluded("agent_webui/frontend/src/components/ChatView.tsx")
    assert not SNAP._excluded("agent_tools/execute_shell.py")
    assert not SNAP._excluded("agent/approvals/fs_drive.py")
    # 真正该排除的
    assert SNAP._excluded("agent_webui/frontend/dist/index.js")
    assert SNAP._excluded("agent_tools/cache/x.json")
    assert SNAP._excluded("agent/__pycache__/agent.cpython-311.pyc")


@pytest.mark.parametrize("path", [
    "venv/Scripts/python.exe",
    "venv-gateway/Lib/site-packages/x.py",
    "agent_workspace/venvs/abc/Lib/site-packages/typing_extensions.pyi",
    "agent_webui/frontend/node_modules/react/index.js",
    "AetherBreath.egg-info/SOURCES.txt",
])
def test_exclude_covers_env_name_variants(path):
    """守卫：虚拟环境/依赖目录的名字有十几种写法，写死一种就会漏。

    实测漏掉 `agent_workspace/venvs/` 时，指纹从 2488 飙到 31413 条（manifest 1.8MB）。
    """
    assert SNAP._excluded(path), f"应当排除：{path}"


def test_exclude_is_substring_for_secondary_dirs():
    """守卫：次生物目录在项目里不止一处（子代理各有自己的一份）。"""
    assert SNAP._excluded("agent_memory/working_memory/session_x.json")
    assert SNAP._excluded("subagents/多agent系统/critic/agent_memory/working_memory/s.json")
    assert SNAP._excluded("agent_memory/.condensed_sessions/session.json")
    # 但真正的长期记忆是资产，不能排除
    assert not SNAP._excluded("agent_memory/long_memory/MEMORY.md")


def test_secret_detection_and_example_exception():
    assert SNAP.is_secret(".env")
    assert SNAP.is_secret("subagents/x/.env")
    assert SNAP.is_secret("certs/server.key")
    assert SNAP.is_secret("cert.pem")
    # 模板不是密钥：维护"补一个环境变量模板"是常见动作，必须可回滚
    assert not SNAP.is_secret(".env.example")
    assert not SNAP.is_secret("config.yaml")


def test_fingerprint_tree_skips_secrets_and_secondary(tmp_path):
    """真扫一遍临时目录：指纹不许收录次生物与密钥。"""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "agent" / "__pycache__").mkdir()
    (tmp_path / "agent" / "__pycache__" / "a.pyc").write_bytes(b"\x00")
    (tmp_path / "agent_memory" / "working_memory").mkdir(parents=True)
    (tmp_path / "agent_memory" / "working_memory" / "s.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agent_memory" / "long_memory").mkdir()
    (tmp_path / "agent_memory" / "long_memory" / "MEMORY.md").write_text("m", encoding="utf-8")

    fp = SNAP.fingerprint_tree(tmp_path)
    assert "agent/agent.py" in fp
    assert "agent_memory/long_memory/MEMORY.md" in fp
    assert not any("working_memory" in k for k in fp)
    assert not any(k.endswith(".pyc") for k in fp)


def test_in_scope_prefix_rules():
    paths = ["agent/mcp_client.py", "agent/approvals"]
    assert SNAP.in_scope("agent/mcp_client.py", paths)
    assert SNAP.in_scope("agent/approvals/fs_drive.py", paths)
    # 前缀相同但不是同一目录/文件（不许把 approvals2 当成 approvals）
    assert not SNAP.in_scope("agent/approvals2/x.py", paths)
    assert not SNAP.in_scope("agent/logger.py", paths)


def test_diff_reports_all_three_kinds():
    before = {"m.py": (1, 1), "d.py": (1, 1)}
    after = {"m.py": (2, 2), "a.py": (1, 1)}
    d = SNAP.diff_fingerprint(before, after)
    assert d["modified"] == ["m.py"]
    assert d["added"] == ["a.py"]
    assert d["deleted"] == ["d.py"]


# ==================== log_digest：摘要归并与分层 ====================

def test_signature_collapses_repeated_noise():
    """同一件事重复 N 次必须压成同一签名（这是"一眼看出问题"的前提）。"""
    a = DIGEST.signature("技能扫描提示: [browser-use] frontmatter 缺少 version，默认 0.1.0")
    b = DIGEST.signature("技能扫描提示: [ponytail] frontmatter 缺少 version，默认 0.1.0")
    assert a == b, (a, b)


def test_signature_normalizes_paths_and_numbers():
    a = DIGEST.signature("保存会话失败: [WinError 5] 拒绝访问。: 'D:\\x\\y\\session.json'")
    b = DIGEST.signature("保存会话失败: [WinError 5] 拒绝访问。: 'C:\\a\\b\\other.json'")
    assert a == b, (a, b)
    assert "D:\\x" not in a


def test_ledger_internal_keeps_decisions_visible():
    """内部事件可折叠，但"有决策意义"的必须留下（否则摘要等于没有审批信息）。"""
    for decisive in ("spec_block", "payload_hit", "verdict", "batch_ask",
                     "batch_block", "batch_cancelled", "scope_denied", "deny"):
        assert decisive not in DIGEST.LEDGER_INTERNAL, decisive
    for noise in ("inspect", "self_check", "lex_fallback", "quiet_allow"):
        assert noise in DIGEST.LEDGER_INTERNAL, noise


def test_key_patterns_do_not_flag_dialog_content():
    """风险提示：'用户输入/中期进度' 是对话正文，不该被当成系统异常。

    这里只锁死意图（常量存在），实际过滤在 build_digest 内；用户自己说"失败"时
    不能凭空多出一条"异常"。
    """
    assert "失败" in DIGEST.KEY_PATTERNS
    assert "超时" in DIGEST.KEY_PATTERNS


# ==================== verify：扫描范围 ====================

def test_verify_scans_core_dirs_and_skips_envs(tmp_path, monkeypatch):
    VERIFY = _load("verify")
    monkeypatch.setattr(VERIFY, "PROJECT_ROOT", tmp_path)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "agent.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "agent" / "venv").mkdir()
    (tmp_path / "agent" / "venv" / "lib.py").write_text("y=1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")

    files = {p.relative_to(tmp_path).as_posix() for p in VERIFY.py_files_in(("agent", "tests"))}
    assert files == {"agent/agent.py", "tests/test_a.py"}


def test_verify_compile_check_reports_syntax_error(tmp_path, monkeypatch):
    VERIFY = _load("verify")
    monkeypatch.setattr(VERIFY, "PROJECT_ROOT", tmp_path)
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    good.write_text("x = 1\n", encoding="utf-8")
    bad.write_text("def broken(:\n", encoding="utf-8")

    ok, detail = VERIFY.check_compile([good])
    assert ok and "1 个 .py" in detail
    ok2, detail2 = VERIFY.check_compile([bad])
    assert not ok2 and "bad.py" in detail2
