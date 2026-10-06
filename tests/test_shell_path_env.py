# -*- coding: utf-8 -*-
"""execute_shell / execute_python 的 PATH 前置回归（修「git-bash 里 find/sort 被 DOS 版抢走」）。

跑法：
    venv/Scripts/python -m pytest tests/test_shell_path_env.py -q

钉住的行为：
  1. 两个工具都能从磁盘上定位到 MSYS coreutils 目录（find/sort 同目录才算数）。
  2. 清洗后的 PATH 里 coreutils 一定排在 C:\\Windows\\System32 之前 —— 否则
     System32 自带的 DOS 版 FIND.EXE/SORT.EXE 会抢走同名命令。
  3. 项目 venv 仍是 execute_shell 的 PATH 第 0 位（python/pip 优先不能被顶掉）。
  4. AETHER_MSYS_PATH_PREPEND=0 能零代码回退（注释里向人承诺过，必须可兑现）。
  5. 两份 _msys_bin_dirs 探到同一个目录 —— 它们是刻意复制的同构实现，
     一旦漂移（改了 A 忘了 B）由这条抓出来。
  6. 真起 bash 子进程，find/sort 报 GNU 版本、bash 不再是 WSL 启动器。
  7. 顺带确认危险命令护栏没被这次改动影响。

⚠️ 全程从磁盘 import 生产位置（当前 agent 会话里是旧 import，测它等于没测）。
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent_tools                                  # noqa: E402  走生产 import 路径
ES = sys.modules["agent_tools.execute_shell"]      # noqa: E402  包 __init__ 会把模块名遮蔽成同名函数
EP = sys.modules["agent_tools.execute_python"]     # noqa: E402
USRBIN = os.path.normcase("usr" + os.sep + "bin")


def _idx(items, frag):
    for i, x in enumerate(items):
        if frag in os.path.normcase(x):
            return i
    return None


def _parent_path_without_usrbin():
    """父 PATH 里摘掉 usr/bin。

    必须摘：从 git-bash 启动的进程天然带着 /usr/bin，而这两个 rollback
    测试断言的是"开关=0 时**不前置**"——父进程继承来的那一份会让断言
    必然失败（那测的不是被测对象）。摘掉它，测的才是开关本身。
    """
    keep = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        s = p.replace("\\", "/").rstrip("/").lower()
        if s.endswith("/usr/bin"):
            continue
        keep.append(p)
    return os.pathsep.join(keep)


@pytest.fixture(autouse=True)
def _clean_switch(monkeypatch):
    monkeypatch.delenv("AETHER_MSYS_PATH_PREPEND", raising=False)


def test_loaded_from_production_location():
    assert os.path.normcase(ES.__file__).startswith(os.path.normcase(ROOT))
    assert os.path.normcase(EP.__file__).startswith(os.path.normcase(ROOT))


def test_shell_detects_coreutils_dir():
    dirs = ES._msys_bin_dirs(ES._find_bash())
    assert dirs, "没能从 bash 路径推导出 coreutils 目录"
    for g in dirs:
        assert os.path.isfile(os.path.join(g, "find.exe" if os.name == "nt" else "find"))


def test_python_detects_coreutils_dir():
    assert EP._msys_bin_dirs(), "execute_python 侧没探到 coreutils 目录"


def test_shell_prepends_before_system32():
    parts = ES.clean_environment(ES._msys_bin_dirs(ES._find_bash()))["PATH"].split(os.pathsep)
    i_m, i_s = _idx(parts, USRBIN), _idx(parts, "system32")
    assert i_m is not None and i_s is not None and i_m < i_s, "msys=%s system32=%s" % (i_m, i_s)


def test_shell_keeps_venv_first():
    parts = ES.clean_environment(ES._msys_bin_dirs(ES._find_bash()))["PATH"].split(os.pathsep)
    assert "venv" in os.path.normcase(parts[0]), parts[0]


def test_python_prepends_before_system32():
    parts = EP.clean_environment()["PATH"].split(os.pathsep)
    i_m, i_s = _idx(parts, USRBIN), _idx(parts, "system32")
    assert i_m is not None and (i_s is None or i_m < i_s)


def test_switch_can_rollback_shell(monkeypatch):
    monkeypatch.setenv("AETHER_MSYS_PATH_PREPEND", "0")
    monkeypatch.setenv("PATH", _parent_path_without_usrbin())
    parts = ES.clean_environment(ES._msys_bin_dirs(ES._find_bash()))["PATH"].split(os.pathsep)
    i_m, i_s = _idx(parts, USRBIN), _idx(parts, "system32")
    assert i_s is not None and (i_m is None or i_m > i_s), "开关=0 没能回退"


def test_switch_can_rollback_python(monkeypatch):
    monkeypatch.setenv("AETHER_MSYS_PATH_PREPEND", "0")
    monkeypatch.setenv("PATH", _parent_path_without_usrbin())
    parts = EP.clean_environment()["PATH"].split(os.pathsep)
    assert _idx(parts, USRBIN) is None or _idx(parts, USRBIN) > _idx(parts, "system32")


def test_two_implementations_do_not_drift():
    a = [os.path.normcase(x) for x in ES._msys_bin_dirs(ES._find_bash())]
    b = [os.path.normcase(x) for x in EP._msys_bin_dirs()]
    assert a and b and a[0] == b[0], "两份 _msys_bin_dirs 结果不一致：%s vs %s" % (a, b)


def test_child_shell_gets_gnu_tools():
    bash = ES._find_bash()
    assert bash, "找不到 bash"
    env = ES.clean_environment(ES._msys_bin_dirs(bash))
    cmd = ('find --version 2>&1 | head -1; sort --version 2>&1 | head -1; '
           'bash --version 2>&1 | head -1')
    r = subprocess.run([bash, "-c", cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=40, cwd=ROOT)
    out = (r.stdout or "")
    assert "GNU findutils" in out, "find 仍是 DOS 版：" + out[:120]
    assert "GNU coreutils" in out, "sort 仍是 DOS 版：" + out[:120]
    assert "GNU bash" in out and "WSL" not in out, "bash 仍指向 WSL：" + out[:120]


def test_guardrails_untouched():
    assert ES.check_command_safety("rm -rf /") is not None
    assert ES.check_command_safety("rm -rf node_modules") is None
    assert ES.check_command_safety("shutdown") is not None
