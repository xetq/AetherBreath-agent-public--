# -*- coding: utf-8 -*-
"""execute_python 的能力拦截器回归（2026-09-22 解除 os.walk 时立）。

背景：判定规则是「已知危险对象（os/sys/shutil/subprocess/…）一律拦，除非名字列进白名单」。
白名单里早就有 remove / rmtree / rename 这类**写**操作，却漏了只读的 os.walk ——
于是 os.walk 仅因名字没被列进去就被拒，而同为只读的 os.listdir 却放行：
自相矛盾，而且拦的是最不危险的那个。

本文件把「该放行的放行、该拦的仍拦」**双向**钉死，防以后有人加白名单加过头。
只调 validate_code()（纯 AST 判定），不执行任何代码、不碰磁盘、不调 LLM。

跑法：venv/Scripts/python -m pytest tests/test_execute_python_guard.py -q
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "agent_tools"))
sys.path.insert(0, str(ROOT))

import execute_python as EP                      # noqa: E402
from execute_python import validate_code         # noqa: E402

# 打错模块 = 等于没测（判据：真命中的必须是项目里的那份）
assert EP.__file__.replace("\\", "/").endswith("agent_tools/execute_python.py"), EP.__file__


# ---------------------------------------------------------------- 放行面
def test_os_walk_is_allowed():
    assert validate_code('os.walk(".")') is None


def test_os_walk_in_for_loop_is_allowed():
    assert validate_code('for root, dirs, files in os.walk("."):\n    pass') is None


def test_os_walk_in_comprehension_is_allowed():
    assert validate_code('paths = [p for p, _, _ in os.walk("agent_workspace")]') is None


def test_os_path_join_with_walk_is_allowed():
    assert validate_code('for r, d, f in os.walk("."):\n    x = os.path.join(r, "a")') is None


def test_os_scandir_is_allowed():
    """2026-09-22 与 walk 一起解禁：同为只读遍历，且它的 DirEntry 方法调用
    走的是"接收者是变量"那条宽松分支，本来就不误拦。"""
    assert validate_code('os.scandir(".")') is None


def test_os_scandir_with_statement_is_allowed():
    assert validate_code('with os.scandir(".") as it:\n    names = [e.name for e in it]') is None


def test_listdir_still_allowed():
    """对照组：同为只读遍历，它一直在白名单里 —— walk 应与它平权。"""
    assert validate_code('os.listdir(".")') is None


def test_remove_still_allowed():
    """对照组：写操作也早就在白名单里（所以"walk 被拦是为了安全"讲不通）。"""
    assert validate_code('os.remove("x")') is None


def test_rmtree_still_allowed():
    assert validate_code('shutil.rmtree("x")') is None


def test_generic_method_on_unknown_object_not_blocked():
    """历史误拦案例：x.replace() 这种通用名在普通对象上到处都是，必须放行。"""
    assert validate_code('s = "a"\ns = s.replace("a", "b")') is None


# ---------------------------------------------------------------- 拦截面（防加过头）
def test_os_system_still_blocked():
    assert validate_code('os.system("dir")') is not None


def test_os_popen_still_blocked():
    assert validate_code('os.popen("dir")') is not None


def test_os_execv_still_blocked():
    assert validate_code('os.execv("x", [])') is not None


def test_subprocess_run_still_blocked():
    """subprocess 是已知危险对象，run 不在它的白名单里 —— 加 walk 不该顺手放行它。"""
    assert validate_code('subprocess.run(["x"])') is not None


def test_subprocess_popen_still_blocked():
    assert validate_code('subprocess.Popen(["x"])') is not None


def test_eval_still_blocked():
    assert validate_code('eval("1+1")') is not None


def test_import_ctypes_still_blocked():
    assert validate_code('import ctypes') is not None


def test_from_os_import_then_call_is_blocked():
    """导入行本身放行（os 不在危险模块表里），真正拦在**调用处** ——
    裸名 system(...) 命中 _BARE_DANGER。写这条时我按「import 就该拦」的直觉断言，
    实测才发现判据错了：查危险要看它有没有被调用，不是看它有没有被导入。"""
    assert validate_code('from os import system\nsystem("dir")') is not None
    assert validate_code('from subprocess import Popen\nPopen(["x"])') is not None


# ---------------------------------------------------------------- 白名单没被整体放宽
def test_os_whitelist_gained_only_readonly_traversal():
    """白名单只多了两个**只读遍历**（walk / scandir）；执行类一律不许顺手进去。"""
    allowed = EP.ALLOWED_ATTRIBUTES["os"]
    assert "walk" in allowed and "scandir" in allowed
    for name in ("system", "popen", "execv", "execve", "spawnv", "fork", "kill"):
        assert name not in allowed, "os.%s 不该进白名单" % name


if __name__ == "__main__":      # tests/README 约定：每个文件都能独立直跑
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
