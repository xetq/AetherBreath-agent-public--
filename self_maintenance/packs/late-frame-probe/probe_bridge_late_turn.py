# -*- coding: utf-8 -*-
"""探针（bridge 侧）：回合已收口的工具收尾，还会不会发 `turn_phase`。

这是"断源"那一半：网关的迟到帧防护是兜底，真正该做的是**不发那条帧**。
用法：
    python probe_bridge_late_turn.py                    # 生产（未打补丁）
    python probe_bridge_late_turn.py <打过补丁的 backend 目录>
"""
import queue
import sys
import threading
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "agent_webui").is_dir())
BACKEND = ROOT / "agent_webui" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
for _extra in reversed([a for a in sys.argv[1:] if not a.startswith("-")]):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

_REAL_STDOUT = sys.stdout
import bridge                     # noqa: E402
sys.stdout = _REAL_STDOUT         # bridge 在 import 期把 stdout 让给了 stderr


def drain(q, timeout=0.4):
    out = []
    while True:
        try:
            out.append(q.get(timeout=timeout if not out else 0.05))
        except queue.Empty:
            return out


def run_case(finished):
    ctx = bridge.RunContext("r-probe", "s-probe", "probe")
    ctx.thread_id = threading.get_ident()
    ctx.finished = finished
    with bridge._RUNS_LOCK:
        bridge._RUNS[ctx.run_id] = ctx
    q = bridge.BUS.subscribe()
    try:
        wrapped = bridge._wrap_tool("probe", lambda **kw: "ok")
        wrapped()
        evts = drain(q)
    finally:
        bridge.BUS.unsubscribe(q)
        with bridge._RUNS_LOCK:
            bridge._RUNS.pop(ctx.run_id, None)
    return evts


def main():
    print("bridge 模块：%s" % bridge.__file__)
    ok = True

    print("\n== A. 回合还在跑（finished=False）：应当正常发 turn_phase=THINKING ==")
    evts = run_case(False)
    kinds = [e.get("type") for e in evts]
    tp = [e for e in evts if e.get("type") == "turn_phase"]
    print("   事件: %s" % kinds)
    print("   turn_phase: %s" % [{k: e.get(k) for k in ("phase", "run_id")} for e in tp])
    if not tp or tp[-1].get("phase") != "THINKING":
        print("   [FAIL] 正常路径没发 THINKING —— 修过头了（会伤到在跑的回合）")
        ok = False

    print("\n== B. 回合已经收口（finished=True）：不该再发任何 turn_phase ==")
    evts2 = run_case(True)
    kinds2 = [e.get("type") for e in evts2]
    tp2 = [e for e in evts2 if e.get("type") == "turn_phase"]
    print("   事件: %s" % kinds2)
    if tp2:
        print("   [FAIL] 仍然发了 turn_phase: %s（这就是把网关状态机又推回忙的那一帧）"
              % [{k: e.get(k) for k in ("phase", "run_id")} for e in tp2])
        ok = False
    else:
        print("   [PASS] 没发 turn_phase（tool_end 仍在，时间线照旧收尾）")
    if "tool_end" not in kinds2:
        print("   [FAIL] tool_end 丢了 —— 时间线上的工具会永远停在 running")
        ok = False

    print("\n探针结论：%s" % ("通过" if ok else "未通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
