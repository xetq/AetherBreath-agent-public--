# -*- coding: utf-8 -*-
"""工作区探针纳管回归。

背景：项目里有两类测试资产 —— A) tests/ 下的 pytest 单测（纯函数、临时目录、可无人值守）；
B) 散落在 agent_workspace / agent_webui 的真机探针与验收脚本（脚本式：自带 main() + 退出码）。
B 类的老毛病是写完就没人再跑，直到某次改坏才发现网漏了。本文件把 B 类里**可无人值守**的部分
用子进程纳管进来（断言退出码 0），把**依赖外部服务**的部分登记在案：缺条件即 skip，但仍校验
文件存在且语法可编译 —— 误删或改坏会立刻红。

跑法（项目根）：
  venv/Scripts/python -m pytest tests/test_workspace_probes.py -q
连真机探针一起跑（需先起网关）：
  AETHER_PROBE_LIVE=1 venv/Scripts/python -m pytest tests/test_workspace_probes.py -q

设计约束：
  - 路径全部相对项目根，零盘符字面量：clone 到任何机器都能跑
  - 每个脚本跑在独立子进程：它们的 sys.path 引导互不干扰，也不污染 pytest 的 capture
    （历史上 agent_tools/multi_search.py 在 win32 import 期会 detach stdout、摧毁 pytest
      capture；2026-09-14 已修 —— 宿主替换过 stdout 时不再重包编码）
"""
from __future__ import annotations

import os
import py_compile
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join("agent_workspace", "审批弹窗可恢复")
CTX = os.path.join("agent_workspace", "上下文管理器")


def _py(script, timeout=300):
    e = dict(os.environ)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=e, timeout=timeout)


# ============ 可无人值守：真跑并断言通过 ============
GATE_SCRIPTS = [
    ("审批卡片恢复：pending_cards 可重新拉出", os.path.join(WS, "probe_pending.py")),
    ("审批僵尸卡：超时结算后不得复活", os.path.join(WS, "probe_expire.py")),
    ("审批待发卡清单 31 项契约（回投/超时/取消/多卡）", os.path.join("agent_webui", "scripts", "verify_pending_cards.py")),
    ("SSE 回放标记：直播帧不得带 replayed", os.path.join("agent_webui", "scripts", "verify_sse_replay.py")),
]

# ============ 需外部服务：登记 + 静态校验，按需执行 ============
LIVE_SCRIPTS = [
    ("WebUI P7 全流程（需网关 8900）", os.path.join("agent_webui", "scripts", "regression_p7.py")),
    ("卡片恢复三层验收（需网关 8900）", os.path.join("agent_webui", "scripts", "verify_gate_restore.py")),
]


@pytest.mark.parametrize("name,rel", GATE_SCRIPTS, ids=[s[0] for s in GATE_SCRIPTS])
def test_workspace_probe_passes(name, rel):
    script = os.path.join(ROOT, rel)
    assert os.path.isfile(script), "%s：探针文件不存在（被删/被移？）%s" % (name, rel)
    r = _py(script)
    tail = (r.stdout or "").strip().split(chr(10))[-3:]
    assert r.returncode == 0, "%s 退出码 %s\n%s\n%s" % (name, r.returncode, chr(10).join(tail), (r.stderr or "")[-500:])


@pytest.mark.parametrize("name,rel", LIVE_SCRIPTS, ids=[s[0] for s in LIVE_SCRIPTS])
def test_live_probe_registered(name, rel):
    """真机探针：默认只做存在性 + 语法可编译；AETHER_PROBE_LIVE=1 才真跑。"""
    script = os.path.join(ROOT, rel)
    assert os.path.isfile(script), "%s：真机探针文件丢失 %s" % (name, rel)
    py_compile.compile(script, doraise=True)
    if os.environ.get("AETHER_PROBE_LIVE", "").strip() not in ("1", "true", "yes"):
        pytest.skip("需外部服务（网关 8900）；AETHER_PROBE_LIVE=1 时执行")
    r = _py(script, timeout=600)
    assert r.returncode == 0, "%s 退出码 %s\n%s" % (name, r.returncode, (r.stdout or "")[-800:])


def test_documented_omissions_are_justified():
    """有些探针**刻意不纳管**，理由必须成文；写成断言防止将来被顺手塞进 GATE 清单。

    - 上下文管理器的 e2e_probe / replay_probe / bench_view：见该目录 DESIGN.md §17-6 ——
      multi_search 曾在 win32 import 期 detach stdout 摧毁 pytest capture（2026-09-14 已修）。
    - security-audit-aetherbreath 的 probe*/verify_t*：结论已固化进
      test_security_hardening.py 与 test_approval_engine.py，原件属取证脚本（dump 结构、无断言）。
    - webui延迟检测/probe*：性能采样，无恒定通过判据，不属回归网。
    """
    for rel in [os.path.join(CTX, "e2e_probe.py"),
                os.path.join("agent_workspace", "security-audit-aetherbreath", "probe5_executable_bypass.py")]:
        assert os.path.isfile(os.path.join(ROOT, rel)), "取证脚本丢失：" + rel
    banned = [os.path.basename(r) for _, r in GATE_SCRIPTS + LIVE_SCRIPTS]
    assert "e2e_probe.py" not in banned and "replay_probe.py" not in banned
