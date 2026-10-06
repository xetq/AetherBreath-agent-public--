# -*- coding: utf-8 -*-
"""被超时/取消杀掉之后，工具**必须立刻返回**（2026-09-30 真机）。

现象（主人转述 AB 的观察）：
    挂 `sleep 130 && echo BG130_OK`（timeout=120）-> 结果报「⏰ 执行超时（超过 120 秒），
    已强杀整个进程树」，但**耗时是 130.2 秒**。AB 由此推断"timeout 不生效、是跑完再判"。

实测（本次维护的取证，见 NOTES P10）**推翻了那个推断，但确认了另一处真缺陷**：

```
sleep 3   (timeout=1) -> 1.45s 返回   ✅ 准点
sleep 5 && echo DONE > MARK.txt (timeout=1) -> 5.15s 返回   ❌ 拖满自然时长
    但 MARK.txt **不存在** -> 树确实在超时点被杀了（不是"跑完再判"）
杀死后 2 秒的进程表: bash.exe 已消失，**sleep.exe(PID) 还活着**
```

机制：`taskkill /F /T` 杀掉中间那层 shell 后，真正干活的**孙进程被重新挂靠、逃出 /T 的遍历**；
它继续持有 stdout/stderr 管道 -> 读线程卡在 `read()` -> 收尾处 `_pipe.close()` 在等读线程的锁
-> 工具要等那条命令**自然结束**才返回。

两条判据（本文件就是这两条的回归门）：
  1. 被杀之后**立刻返回**（不等自然结束）；
  2. 若确实有孙进程逃过强杀，**如实说出来**（不许让"还在跑"变成静默事实）。
"""
import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent"), str(ROOT / "agent_tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import execute_python as EPY      # noqa: E402
import execute_shell as ESH       # noqa: E402


def _has_bash() -> bool:
    try:
        return bool(ESH._find_bash())
    except Exception:
        return False


needs_bash = pytest.mark.skipif(not _has_bash(), reason="没有 bash（git-bash/MSYS）")


# ============================================================
# 1. 真机形态：复合命令被超时杀掉后必须**立刻**返回
# ============================================================

@needs_bash
def test_shell_compound_timeout_returns_promptly(tmp_path):
    """`a && b` 这类复合命令：超时后不许拖到命令自然结束（修前实测拖满 5.15s）。"""
    mark = tmp_path / "MARK.txt"
    t0 = time.time()
    out = ESH.execute_shell("sleep 3 && echo DONE > MARK.txt",
                            workdir=str(tmp_path), timeout=1)
    dt = time.time() - t0
    assert "执行超时" in out, out[:80]
    assert dt < 2.5, "超时之后拖了 %.2fs 才返回（修前就是这么拖满自然时长的）" % dt
    # 命令若真跑完，标记文件会在 sleep 结束时出现 —— 等过它的自然时长再确认
    time.sleep(3.0)
    assert not mark.exists(), "命令居然跑完了 —— 那才叫「没杀成功」"


@needs_bash
def test_shell_simple_timeout_returns_promptly(tmp_path):
    t0 = time.time()
    out = ESH.execute_shell("sleep 3", workdir=str(tmp_path), timeout=1)
    assert "执行超时" in out and (time.time() - t0) < 2.5


@needs_bash
def test_shell_success_path_still_returns_output(tmp_path):
    """正常路径零行为变化：输出照拿、不许多等。"""
    t0 = time.time()
    out = ESH.execute_shell("echo hello-timeout-probe", workdir=str(tmp_path), timeout=10)
    assert "hello-timeout-probe" in out
    assert (time.time() - t0) < 5


def test_python_timeout_returns_promptly(tmp_path):
    t0 = time.time()
    out = EPY.execute_python("import time\ntime.sleep(3)\n", timeout=1)
    assert "执行超时" in out, out[:120]
    assert (time.time() - t0) < 2.5, "python 侧同款拖累：%.2fs" % (time.time() - t0)


def test_python_success_path_still_returns_output():
    out = EPY.execute_python("print('py-ok-probe')", timeout=10)
    assert "py-ok-probe" in out


# ============================================================
# 2. 收尾关管道绝不许阻塞（两个工具共用同一套判据）
# ============================================================

def _blocked_reader():
    """造一个"读线程卡在 read() 上、管道写端没关"的场景 —— 就是真机里的那个死结。"""
    r, w = os.pipe()
    f = io.open(r, "rb", buffering=0)
    t = threading.Thread(target=lambda: f.read(1), daemon=True)
    t.start()
    time.sleep(0.1)
    assert t.is_alive(), "读线程没卡住，构造失败"
    return f, w, t


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = None
        self.pid = 999999

    def poll(self):
        return 0


@pytest.mark.parametrize("mod", [ESH, EPY], ids=["execute_shell", "execute_python"])
def test_close_pipes_never_blocks_on_busy_reader(mod):
    """**核心判据**：读线程还卡着时，关管道必须立刻返回（把收尾交给后台）。

    修前这里直接 `pipe.close()` —— 而 `BufferedReader.close()` 要等读线程手里的锁，
    那个锁只在"持有管道的孙进程"退出时才放开 -> 工具被拖满整条命令的自然时长。
    """
    f, w, t = _blocked_reader()
    proc = _FakeProc(f)
    t0 = time.time()
    mod._close_pipes_nonblocking(proc, t)
    dt = time.time() - t0
    assert dt < 0.5, "关管道阻塞了 %.2fs —— 就是它把工具拖到命令自然结束的" % dt
    os.close(w)                       # 放读线程走，回收线程随后把它收掉
    t.join(timeout=2)


@pytest.mark.parametrize("mod", [ESH, EPY], ids=["execute_shell", "execute_python"])
def test_close_pipes_closes_when_reader_done(mod):
    """反向判据：读线程已经结束（正常路径）时，照旧把管道关掉，别变成 fd 泄漏。"""
    r, w = os.pipe()
    f = io.open(r, "rb", buffering=0)
    os.close(w)                       # 立刻 EOF -> 读线程马上结束
    t = threading.Thread(target=lambda: f.read(1), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    mod._close_pipes_nonblocking(_FakeProc(f), t)
    assert f.closed, "读线程都结束了却没关管道 -> fd 会漏"


# ============================================================
# 3. 口径：逃过强杀的子进程，**要么被真杀掉、要么被说出来**
# ============================================================
#
# 判据用"**子进程自己写标记文件**"来判它有没有活下来（不看进程名，避免误判）：
#   命令 = `<python> -c "sleep 3; 写标记"` —— bash 起 python，python 是那个会被
#   taskkill /T 漏掉的角色。若它活过超时点，3 秒后标记文件就会出现。

def _escapee_cmd(mark) -> str:
    """复合命令：bash 里起一个"会自己写标记"的子进程（绝对解释器路径，避免 PATH 被清洗）。"""
    code = ("import sys,time;time.sleep(3);"
            "open(sys.argv[1],'w').write('ESCAPED')")
    return '"%s" -c "%s" "%s" && echo TAIL' % (sys.executable, code, str(mark))


@needs_bash
def test_escapee_mechanism_sanity(tmp_path):
    """先证明这个"逃逸探针"本身有效：不设超时地跑一遍，标记必须出现。"""
    mark = tmp_path / "SANITY.txt"
    out = ESH.execute_shell(_escapee_cmd(mark), workdir=str(tmp_path), timeout=20)
    assert mark.exists(), "探针无效（命令都没跑成）：%r" % out[:200]


@needs_bash
def test_timeout_leaves_no_escapee_or_says_so(tmp_path):
    """超时之后：要么没有逃逸进程，要么**如实提示** —— 静默逃逸不合格，误报也不合格。"""
    mark = tmp_path / "ESCAPED.txt"
    out = ESH.execute_shell(_escapee_cmd(mark), workdir=str(tmp_path), timeout=1)
    assert "执行超时" in out
    time.sleep(3.5)                       # 给"活下来的那个"足够时间写标记
    said = ("逃过强杀" in out) or ("仍在运行" in out) or ("可能还在跑" in out)
    if mark.exists():
        assert said, "子进程活下来写出标记，工具却没有如实提示 —— 静默事实"
    else:
        assert not said, "什么都没逃掉，却报了「可能有子进程逃过强杀」—— 误报也是失真"


@needs_bash
def test_windows_job_kills_the_escapee(tmp_path):
    """**Job Object 的验收**：Windows 上超时必须把那个子进程一起杀掉（标记不许出现）。"""
    if not _win_job_available():
        pytest.skip("本机拿不到 Job Object（非 Windows 或 ctypes 装配失败）")
    mark = tmp_path / "ESCAPED.txt"
    out = ESH.execute_shell(_escapee_cmd(mark), workdir=str(tmp_path), timeout=1)
    assert "执行超时" in out
    time.sleep(3.5)
    assert not mark.exists(), "Job Object 没杀掉那个逃逸的子进程（它还写出了标记）"
    assert "逃过强杀" not in out, "杀干净了却还在提示有孤儿 —— 误报"


def _win_job_available() -> bool:
    try:
        return bool(ESH._win_job and ESH._win_job.available())
    except Exception:
        return False


# ============================================================
# 3.5 「不污染其它功能」的回归门
# ============================================================
#
# 主人明确要求：这次改动不许污染其它功能。Job Object 刻意**没设 KILL_ON_JOB_CLOSE**，
# 所以"关句柄"不会顺手杀死里面还活着的进程 —— 成功路径上用户故意放到后台的活儿照旧活着。

@needs_bash
def test_success_path_does_not_kill_backgrounded_child(tmp_path):
    """成功结束的调用不许把用户故意放到后台的子进程杀死（行为污染）。"""
    mark = tmp_path / "BG_DONE.txt"
    t0 = time.time()
    out = ESH.execute_shell("( sleep 2; touch BG_DONE.txt ) & echo started",
                            workdir=str(tmp_path), timeout=15)
    dt = time.time() - t0
    assert "started" in out, out[:120]
    assert dt < 8, "工具被后台子进程拖住了 %.1fs（收尾的既有代价不该变成更久）" % dt
    time.sleep(2.6)
    assert mark.exists(), "后台子进程被顺手杀了 —— Job Object 污染了成功路径的行为"


@needs_bash
def test_success_path_still_reports_exit_code(tmp_path):
    """退出码语义不许变（非 0 仍是失败）。"""
    out = ESH.execute_shell("exit 3", workdir=str(tmp_path), timeout=10)
    assert "退出码 3" in out, out[:120]


# ============================================================
# 4. 提示词与行为同步（主人 2026-09-30 明确要求：返回 AB 的提示也要一起改）
# ============================================================

def test_shell_timeout_prompt_matches_behavior():
    fn = ESH.execute_shell_schema["function"]
    assert "强杀整棵进程树" in fn["description"], "工具描述没写超时会杀整棵树"
    assert "Job Object" in fn["description"], "工具描述没写 Windows 靠 Job Object 不漏"
    d = fn["parameters"]["properties"]["timeout"]["description"]
    assert "强杀" in d and "立刻返回" in d, "timeout 参数说明与真实行为脱节：%s" % d


def test_python_timeout_prompt_matches_behavior():
    fn = EPY.execute_python_schema["function"]
    assert "强杀整棵进程树" in fn["description"] and "Job Object" in fn["description"]
    d = fn["parameters"]["properties"]["timeout"]["description"]
    assert "强杀" in d and "立刻返回" in d, "timeout 参数说明与真实行为脱节：%s" % d


def test_task_kill_prompt_matches_behavior():
    from agent_tools import task_jobs as J
    desc = J.task_kill_schema["function"]["description"]
    assert "Job Object" in desc, "task_kill 的描述还停在「尽力中断」的旧口径"
    assert "线程杀不掉" in desc, "口径必须把「线程杀不掉 / 进程树能杀干净」讲清"
    body = J.task_kill.__doc__ or ""
    assert "Job Object" in body, "task_kill 的实现注释/文档没同步"
