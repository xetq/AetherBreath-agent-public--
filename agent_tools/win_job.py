# -*- coding: utf-8 -*-
"""Windows **Job Object** 封装（纯 stdlib ctypes）：让"强杀整棵进程树"真的杀干净。

## 为什么需要它（2026-09-30 实测）

`taskkill /F /T` 是**按父子链**遍历的：杀掉中间那层 shell 之后，真正干活的孙进程会被
**重新挂靠**（re-parent）、逃出这次遍历继续运行 —— 实测 `bash.exe` 已经没了而 `sleep.exe`
还活着，并且一直占着 stdout/stderr 管道，把工具拖到命令自然结束才返回。
Job Object 记的是**进程归属**（内核记账），不是父子链，所以 `TerminateJobObject`
能把整棵树 —— 含被重新挂靠的、以及它们后来派生的 —— 一起杀掉。

## 用法（零侵入，失败一律降级）

    job = create_for(proc)     # 建 job 并把刚起来的子进程挂进去；失败返回 None
    ... 正常执行 ...
    terminate(job)             # **只在"要杀"时调**：整棵树一起死
    close(job)                 # 收尾关句柄

**刻意不设 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`**：那会让"关句柄"变成"杀掉里面还活着的一切"，
于是成功路径上用户故意放到后台的子进程（`nohup server &`）会被顺手杀死 —— 那是**行为污染**。
本模块只提供"你想杀时能杀干净"，不改变"不杀时什么都不发生"。

## 平台

非 Windows 全部是 no-op（**POSIX 用进程组 `os.killpg`，本来就没这个问题**）。
拿不到 job 时调用方走原来的 taskkill —— 功能只会更好，不会更差。
"""
from __future__ import annotations

import sys
from typing import Any, Optional

_IS_WINDOWS = sys.platform.startswith("win")

# Job Object 的两种用法：TerminateJobObject（显式强杀）/ KILL_ON_JOB_CLOSE（关句柄即收树）
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9

# ---------------- ctypes 装配（只在 Windows 上做） ----------------
_k32: Any = None
_LAST_ERROR = ""
_LIMIT_ERROR = ""          # KILL_ON_JOB_CLOSE 设置失败的原因（空 = 没失败过）
_AVAILABLE = False

if _IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes as wt

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
                        ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),          # ULONG_PTR
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class _EXT_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                        ("IoInfo", _IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = wt.HANDLE
        _k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
        _k32.SetInformationJobObject.restype = wt.BOOL
        _k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
        _k32.AssignProcessToJobObject.restype = wt.BOOL
        _k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        _k32.TerminateJobObject.restype = wt.BOOL
        _k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
        _k32.CloseHandle.restype = wt.BOOL
        _k32.CloseHandle.argtypes = [wt.HANDLE]
        _AVAILABLE = True
    except Exception as e:                      # 拿不到就降级 —— 调用方走 taskkill
        _LAST_ERROR = "%s: %s" % (type(e).__name__, e)
        _k32 = None
        _AVAILABLE = False


def available() -> bool:
    """本机能不能用 Job Object（Windows 且 ctypes 装配成功）。"""
    return bool(_AVAILABLE)


def why_unavailable() -> str:
    """不能用时的原因（空串 = 能用）。**如实报告，不假装。**"""
    if _AVAILABLE:
        return ""
    if not _IS_WINDOWS:
        return "非 Windows（POSIX 用进程组强杀，无此问题）"
    return _LAST_ERROR or "ctypes 装配失败"


def create_for(proc, kill_on_close: bool = False) -> Optional[Any]:
    """给刚起来的子进程建一个 Job Object 并把它挂进去；失败返回 None（调用方降级）。

    `kill_on_close=True` 会给这个 job 设上 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`：
    **句柄一关（包括"AB 进程被硬杀、内核替它关句柄"），job 里还在跑的一切都被内核收掉。**
    P13 起**只对后台作业**这么设：

      · 后台作业的语义就是"跨回合跑、随时可被一起停" —— AB 一死，它没理由继续活着；
      · 前台调用**不设** —— 用户故意放到后台的活儿（`nohup server &`）不许被顺手杀掉。

    设不上也不影响强杀（`TerminateJobObject` 那条路照旧），但要能看见（`limit_error()`）。
    """
    if not _AVAILABLE or proc is None:
        return None
    handle = None
    try:
        import ctypes
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        if kill_on_close:
            global _LIMIT_ERROR
            info = _EXT_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(
                    handle, _JobObjectExtendedLimitInformation,
                    ctypes.byref(info), ctypes.sizeof(info)):
                _LIMIT_ERROR = ("SetInformationJobObject(KILL_ON_JOB_CLOSE) 失败: err=%d"
                                % ctypes.get_last_error())
        child = ctypes.wintypes.HANDLE(int(proc._handle))     # Popen 的子进程句柄
        if not _k32.AssignProcessToJobObject(handle, child):
            _k32.CloseHandle(handle)
            return None
        return handle
    except Exception:
        try:
            if handle:
                _k32.CloseHandle(handle)
        except Exception:
            pass
        return None


def limit_error() -> str:
    """`KILL_ON_JOB_CLOSE` 设置失败的原因（空串 = 没失败过）。"""
    return _LIMIT_ERROR


def terminate(job) -> bool:
    """把 job 里的**所有进程**（含被重新挂靠的孙进程与它们派生的）一起杀掉。

    返回 True = 真的终止了（调用方就不必再 taskkill）；False = 没做成，调用方走旧路径。
    """
    if not _AVAILABLE or job is None:
        return False
    try:
        return bool(_k32.TerminateJobObject(job, 1))
    except Exception:
        return False


def close(job) -> None:
    """关句柄（不设 KILL_ON_JOB_CLOSE，所以**不会**顺手杀死还在运行的子进程）。"""
    if job is None or not _AVAILABLE:
        return
    try:
        _k32.CloseHandle(job)
    except Exception:
        pass
