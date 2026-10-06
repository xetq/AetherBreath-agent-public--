# -*- coding: utf-8 -*-
"""进程身份判定与 bridge 回收（网关侧唯一权威）。

旧实现在 agent_proc.py 里，判据是 CommandLine -like '*bridge.py*'，于是任何命令行
出现过 bridge.py 字样的 python 进程都会被强杀 —— 实测包含 `pytest test_bridge.py`、
`python -c "...bridge.py..."`、以及我自己写的诊断脚本。这里换成两道锚定判据：
  ① 命令行必须以 backend/bridge.py 结尾（真 bridge 的固定形态）
  ② 该进程的父链上没有活着的主网关（有 = 别人正在用的 AB，绝不碰）
因此多网关共存天然安全，也不再需要靠 AETHER_KILL_ORPHANS=0 来避免误杀。
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

# 锚定：目录必须是 backend，文件名必须到结尾（允许可选闭引号与尾随空白）
BRIDGE_ARG_RE = re.compile(r"[^\\]*backend[\\/]bridge\.py[\"\']?\s*$", re.IGNORECASE)
GATEWAY_ARG_RE = re.compile(r"\w*backend[\\/]main\.py[\"\']?\s*$", re.IGNORECASE)


def posix_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def python_procs():
    """现采 python 进程表 [(pid, ppid, cmdline)]。

    PowerShell 只负责吐 JSON，匹配与父链判断全在 Python 侧做：判据可读可测，
    也不掉进跨 shell 的引号地狱。任何异常都返回空表 —— 宁可不杀，也不误杀。
    """
    data = []
    try:
        if sys.platform == "win32":
            filt = "Name=" + chr(39) + "python.exe" + chr(39)
            ps = ('Get-CimInstance Win32_Process -Filter "' + filt + '" | '
                  "Select ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, timeout=25)
            raw = out.stdout.decode("utf-8", "replace").strip()
            data = json.loads(raw) if raw else []
        else:
            out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="],
                                 capture_output=True, text=True, timeout=20)
            for line in out.stdout.splitlines():
                f = line.strip().split(None, 2)
                if len(f) == 3 and f[0].isdigit() and f[1].isdigit():
                    data.append({"ProcessId": f[0], "ParentProcessId": f[1], "CommandLine": f[2]})
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    res = []
    for x in data:
        try:
            res.append((int(x.get("ProcessId") or 0), int(x.get("ParentProcessId") or 0),
                        x.get("CommandLine") or ""))
        except Exception:
            continue
    return res


def ancestry(pid: int, table) -> set:
    seen, cur, hops = set(), pid, 0
    while cur and cur not in seen and hops < 24:
        seen.add(cur)
        cur = table.get(cur, (0, ""))[0]
        hops += 1
    return seen


def find_orphan_bridges():
    """返回 (孤儿 pid 列表, 受保护 pid 列表, 进程表可用)。"""
    procs = python_procs()
    if not procs:
        return [], [], False
    table = {pid: (ppid, cmd) for pid, ppid, cmd in procs}
    live_gw = {pid for pid, (_, cmd) in table.items() if GATEWAY_ARG_RE.search(cmd)}
    me = os.getpid()
    # 本函数由活网关自己调用，所以"我"与我的全部祖先必然属于一个活网关 ——
    # 无条件加入保护集，判据正则的好坏就不再决定"会不会杀掉活动 AB"。
    live_gw.add(me)
    live_gw |= ancestry(me, table)
    orphans, kept = [], []
    for pid in sorted(table):
        cmd = table[pid][1]
        if pid == me or not BRIDGE_ARG_RE.search(cmd):
            continue
        (kept if ancestry(pid, table) & live_gw else orphans).append(pid)
    return orphans, kept, True


def kill_pids(pids):
    """按进程树杀一批 pid，再一次性批量复核存活。返回 (dead, survivors, notes)。"""
    notes = {}
    for pid in pids:
        try:
            if sys.platform == "win32":
                r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=15)
                if r.returncode != 0:
                    raw = r.stderr if (r.stderr or r.stdout) else r.stdout
                    txt = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
                    notes[pid] = "taskkill rc=%s: %s" % (r.returncode, " ".join(txt.split())[:110])
                    try:
                        subprocess.run(["powershell", "-NoProfile", "-Command",
                                        "Stop-Process -Id " + str(pid) + " -Force"],
                                       capture_output=True, timeout=12)
                        notes[pid] += "；已用 Stop-Process 兜底"
                    except Exception as e:
                        notes[pid] += "；兜底也失败 " + type(e).__name__
            else:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    os.kill(pid, signal.SIGKILL)
                    notes[pid] = "killpg 不可用，已单进程 SIGKILL"
        except Exception as e:
            notes[pid] = "杀进程异常 %s: %s" % (type(e).__name__, e)
    alive = set()
    for _ in range(6):
        time.sleep(0.3)
        if sys.platform == "win32":
            table = {pid for pid, _, _ in python_procs()}
            alive = {pid for pid in pids if pid in table}
        else:
            alive = {pid for pid in pids if posix_alive(pid)}
        if not alive:
            break
    return [pid for pid in pids if pid not in alive], sorted(alive), notes


def cleanup_orphan_bridges():
    """回收上一代网关遗留的 bridge，返回明细供日志与界面展示。"""
    if os.environ.get("AETHER_KILL_ORPHANS", "1").strip().lower() in ("0", "false", "no", "off"):
        return {"killed": 0, "candidates": [], "kept": [], "survivors": [], "skipped": "disabled"}
    orphans, kept, ok = find_orphan_bridges()
    if not ok:
        return {"killed": 0, "candidates": [], "kept": [], "survivors": [], "skipped": "no-proc-table"}
    if not orphans:
        return {"killed": 0, "candidates": [], "kept": kept, "survivors": [], "skipped": ""}
    dead, survivors, notes = kill_pids(orphans)
    for pid in survivors:
        print("[gateway] ⚠️ 孤儿 bridge(pid=%s) 回收失败：%s" % (pid, notes.get(pid, "仍存活")),
              file=sys.stderr, flush=True)
    return {"killed": len(dead), "candidates": orphans, "kept": kept,
            "survivors": survivors, "notes": {str(k): v for k, v in notes.items()}, "skipped": ""}
