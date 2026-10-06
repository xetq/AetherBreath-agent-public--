# -*- coding: utf-8 -*-
"""P11 双向补丁（Job Object 强杀 + 提示词同步）。

**为什么有它**：P11 的快照是在改动**之后**才 `begin` 的（我的流程错误）——
前像 = 改后状态，所以那次快照**不能**回滚 P11 的代码。
好在这批改动全是**局部、可逆**的字符串补丁，于是这里给出双向补丁，并用
"改前 -> 改后 -> 再改前" 的**往返逐字节一致**来验证它确实是精确的逆操作。

用法：
    python revert_or_apply_p11.py --root <树根> --to pre     # 回退到 P11 之前
    python revert_or_apply_p11.py --root <树根> --to post    # 应用 P11（与生产逐字节一致）
    （默认只检查锚点，不写盘；加 --apply 才写盘）

`--to pre` 还会**删除**新增的 `agent_tools/win_job.py`（P11 的新文件）。
"""
import argparse
import sys
from pathlib import Path

# ── 每个文件：[(标签, P11 之后的样子, P11 之前的样子)] ─────────────────────────
PATCHES = {
    "agent_tools/execute_shell.py": [
        ("win_job 导入", """try:                                    # 生产：作为包导入（agent_tools.execute_shell）
    from . import win_job as _win_job
except Exception:                       # 测试/CLI：顶格导入（sys.path 里有 agent_tools）
    try:
        import win_job as _win_job
    except Exception:
        _win_job = None                 # 拿不到就降级：只走 taskkill / 进程组


def _kill_process_tree(proc: subprocess.Popen) -> None:
    \"\"\"强杀整个进程树。

    **先试 Job Object**（Windows，见 `win_job` 模块）：它记的是**进程归属**，能把
    "杀掉中间 shell 之后被重新挂靠的孙进程"一起杀掉 —— `taskkill /T` 按父子链遍历会漏掉它们
    （2026-09-30 实测：bash.exe 已消失、sleep.exe 还活着，还占着输出管道）。
    拿不到 job（非 Windows / 建 job 失败）就退回原来的做法 —— **功能只会更好，不会更差**。
    \"\"\"
    job = getattr(proc, "_ab_job", None)
    if job is not None and _win_job is not None and _win_job.terminate(job):
        return
    if proc.poll() is not None:
""", """def _kill_process_tree(proc: subprocess.Popen) -> None:
    \"\"\"强杀整个进程树。Windows 用 taskkill /T，POSIX 用进程组 SIGKILL。

    ⚠️ 已知边界（2026-09-30 实测）：Windows 的 `taskkill /T` 会漏掉**被重新挂靠的孙进程** ——
    复合命令（`a && b`）多出一层 shell，杀掉中间那层之后，真正干活的孙进程会被挂到别人名下、
    逃出 /T 的遍历，继续运行（实测：bash.exe 已消失，sleep.exe 还活着）。它还会一直占着
    stdout/stderr 管道 —— 见 `_close_pipes_nonblocking` 的注释（那才是"超时返回很晚"的根因）。
    \"\"\"
    if proc.poll() is not None:
"""),
        ("Popen 后挂进 job", """        proc = subprocess.Popen([bash_path, "-c", command], **popen_kwargs)
        # Windows：把子进程挂进 Job Object —— 强杀时才能连"被重新挂靠的孙进程"一起杀掉
        # （taskkill /T 按父子链遍历会漏）。失败就静默降级：_kill_process_tree 会走原路。
        # 刻意不设 KILL_ON_JOB_CLOSE —— 成功路径上用户故意放到后台的子进程照旧活着（不污染）。
        if _win_job is not None:
            try:
                proc._ab_job = _win_job.create_for(proc)
            except Exception:
                proc._ab_job = None
""", """        proc = subprocess.Popen([bash_path, "-c", command], **popen_kwargs)
"""),
        ("finally 关句柄", """        _close_pipes_nonblocking(proc, t_out, t_err)
        if _win_job is not None and proc is not None:
            _win_job.close(getattr(proc, "_ab_job", None))     # 关句柄（不杀里面还活着的进程）
""", """        _close_pipes_nonblocking(proc, t_out, t_err)
"""),
        ("工具描述", """"命令运行在受限子进程中：有超时（默认 30 秒，最大 120 秒；**到点强杀整棵进程树**"
            "—— Windows 用 Job Object，连被重新挂靠的子进程也一起杀 —— 本次输出作废并立刻返回）、"
""", """"命令运行在受限子进程中：有超时（默认 30 秒，最大 120 秒）、"
"""),
        ("timeout 参数说明", """                    "description": ("执行超时时间（秒），默认 30，最大 120。**到点强杀整棵进程树**"
                                    "（含子进程；Windows 用 Job Object 保证不漏），本次输出作废"
                                    "并**立刻返回**。把它设得比命令自然时长小 = 主动放弃这条命令。"),
""", """                    "description": "执行超时时间（秒），默认 30，最大 120。",
"""),
    ],
    "agent_tools/execute_python.py": [
        ("win_job 导入", """try:                                    # 生产：作为包导入（agent_tools.execute_python）
    from . import win_job as _win_job
except Exception:                       # 测试/CLI：顶格导入（sys.path 里有 agent_tools）
    try:
        import win_job as _win_job
    except Exception:
        _win_job = None                 # 拿不到就降级：只走 taskkill / 进程组


def _kill_process_tree(proc: subprocess.Popen) -> None:
    \"\"\"强杀整个进程树（**先试 Job Object**，拿不到再退回 taskkill / 进程组）。

    与 execute_shell 同一套判据：`taskkill /T` 按父子链遍历，漏得掉被重新挂靠的孙进程
    （用户代码里起的子进程就可能这样逃掉，并一直占着输出管道）。
    \"\"\"
    job = getattr(proc, "_ab_job", None)
    if job is not None and _win_job is not None and _win_job.terminate(job):
        return
    if proc.poll() is not None:
""", """def _kill_process_tree(proc: subprocess.Popen) -> None:
    \"\"\"强杀整个进程树。Windows 用 taskkill /T，POSIX 用进程组 SIGKILL。

    ⚠️ 已知边界（2026-09-30 实测，同 execute_shell）：Windows 的 `taskkill /T` 会漏掉
    **被重新挂靠的孙进程**（用户代码里起的子进程就可能这样逃掉），它还一直占着输出管道 ——
    这正是"超时返回被拖到命令自然结束"的根因，见 `_close_pipes_nonblocking`。
    \"\"\"
    if proc.poll() is not None:
"""),
        ("Popen 后挂进 job", """        start_sec = _time.perf_counter() - t_start0
        # Windows：挂进 Job Object（强杀时才能连被重新挂靠的孙进程一起杀掉）；失败静默降级。
        # 不设 KILL_ON_JOB_CLOSE —— 成功路径上用户故意放到后台的子进程照旧活着（不污染）。
        if _win_job is not None:
            try:
                proc._ab_job = _win_job.create_for(proc)
            except Exception:
                proc._ab_job = None
""", """        start_sec = _time.perf_counter() - t_start0
"""),
        ("finally 关句柄", """        _close_pipes_nonblocking(proc, t_out, t_err)
        if _win_job is not None and proc is not None:
            _win_job.close(getattr(proc, "_ab_job", None))     # 关句柄（不杀里面还活着的进程）
""", """        _close_pipes_nonblocking(proc, t_out, t_err)
"""),
        ("工具描述", """"代码运行在受限子进程中：有超时（默认 30 秒，最大 120 秒；**到点强杀整棵进程树**"
            "—— Windows 用 Job Object，连代码里起的子进程也一起杀 —— 本次输出作废并立刻返回）、"
""", """"代码运行在受限子进程中：有超时（默认 30 秒，最大 120 秒）、"
"""),
        ("timeout 参数说明", """                    "description": ("执行超时时间（秒），默认 30，最大 120。**到点强杀整棵进程树**"
                                    "（含代码里起的子进程；Windows 用 Job Object 保证不漏），"
                                    "本次输出作废并**立刻返回**。"),
""", """                    "description": "执行超时时间（秒），默认 30，最大 120。",
"""),
    ],
    "agent_tools/task_jobs.py": [
        ("task_kill 文档口径", """    \"\"\"终止后台作业（标记取消 + 丢弃结果；能真杀子进程的会真的杀）。

    ⚠️ 口径：**线程杀不掉，进程树能杀干净**。这个动作保证的是"结果被丢弃、作业从登记册
    消失"；对 shell/python 这类起子进程的工具，Windows 上用 Job Object 把**整棵树**
    （含被重新挂靠的子进程）一起杀掉，POSIX 用进程组。返回文案如实区分这两件事。
    \"\"\"
""", """    \"\"\"终止后台作业（标记取消 + 丢弃结果；能真杀子进程的会尽力杀）。

    ⚠️ 口径：Python 线程杀不掉 —— 这个动作保证的是"结果被丢弃、作业从登记册消失"，
    而**不能**保证"对端动作已停"。返回文案如实区分这两件事。
    \"\"\"
"""),
        ("task_kill 返回文案", """    tail = ("已同时请求中断它的子进程（整棵进程树）。" if killed else
            "它是个**没有子进程可杀**的工具（例如纯 Python 线程）—— 结果会被丢弃，"
            "但那件事可能仍在后台跑完。若它会产生副作用（写盘/提交/删除），"
            "请先核对目标状态再决定是否重做。")
""", """    tail = ("已同时请求中断它的子进程。" if killed else
            "它的线程仍在后台跑（Python 线程无法强杀），只是结果会被丢弃 —— "
            "若它会产生副作用（写盘/提交/删除），请先核对目标状态再决定是否重做。")
"""),
        ("task_kill schema 描述", """        "description": ("终止一个后台作业：标记取消、丢弃它的结果、从登记册移除。"
                        "口径：**线程杀不掉，进程树能杀干净** —— 对 shell/python 这类起子进程的"
                        "工具，Windows 上用 Job Object 把整棵树（含被重新挂靠的子进程）一起杀掉，"
                        "POSIX 用进程组；但那条工具如果是纯线程动作，「结果不再回来」成立、"
                        "「对端已停」不成立。所以对一个有副作用的作业这么做之后，先核对目标状态。"),
""", """        "description": ("终止一个后台作业：标记取消、丢弃它的结果、从登记册移除。"
                        "注意它保证的是「结果不再回来」，**不保证**对端动作已停 —— "
                        "Python 线程无法强杀，只有能杀子进程的工具会被尽力中断。"
                        "对一个有副作用的作业这么做之后，先核对目标状态再决定是否重做。"),
"""),
    ],
}

NEW_FILES = ["agent_tools/win_job.py"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--to", choices=["pre", "post"], required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    bad = 0
    changed = []
    for rel, hunks in PATCHES.items():
        path = root / rel
        if not path.exists():
            print("[FAIL] 找不到 %s" % path)
            bad += 1
            continue
        text = path.read_text(encoding="utf-8")
        out = text
        for label, post, pre in hunks:
            src, dst = (pre, post) if args.to == "post" else (post, pre)
            n = out.count(src)
            if n != 1:
                print("[FAIL] %s :: %s 锚点命中 %d 次（要求 1）" % (rel, label, n))
                bad += 1
                continue
            out = out.replace(src, dst)
            print("[OK]   %s :: %s -> %s" % (rel, label, args.to))
        if out != text:
            try:
                compile(out, str(path), "exec")
            except SyntaxError as e:
                print("[FAIL] %s 改完语法不过：%s" % (rel, e))
                bad += 1
                continue
            if args.apply:
                path.write_text(out, encoding="utf-8", newline="\n")
                changed.append(rel)
    # 新文件：回到 pre 时删掉；回到 post 时它必须已存在（不重建 —— 重建等于凭空写代码）
    for rel in NEW_FILES:
        path = root / rel
        if args.to == "pre":
            if path.exists():
                if args.apply:
                    path.unlink()
                    changed.append(rel + "（已删除）")
                print("[OK]   %s :: 回退时删除该新文件" % rel)
            else:
                print("[ok]   %s 本就不存在" % rel)
        elif not path.exists():
            print("[FAIL] %s 不存在（--to post 不会凭空重建它）" % rel)
            bad += 1
    print("\n锚点失败 %d 处；%s" % (bad, ("已写盘: " + ", ".join(changed)) if args.apply else "未写盘"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
