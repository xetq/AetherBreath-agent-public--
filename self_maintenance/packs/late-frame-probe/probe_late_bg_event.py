# -*- coding: utf-8 -*-
"""探针：回合结束后，后台作业的收尾事件会不会把网关状态机**又置忙**。

现象（主人 2026-09-29 真机）：
  挂一个 30 秒的后台作业 + 立即汇报 → 回合正常收口（服务端 done）→ 界面却在
  约 30 秒后**自己**变回"回合进行中"（草稿变成中期交互），点「停止本回合」
  却被告知 no-active-run。

本探针只走**网关侧**真实代码（state + agent_proc._forward），不起进程、不连网、
不碰 LLM。判定：回合结束后的迟到 turn_phase 不得把 busy 置回 True。
"""
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents
            if (p / "agent_webui").is_dir())
BACKEND = ROOT / "agent_webui" / "backend"
# 先用命令行指定的模块目录（未打补丁的生产 / 打过补丁的临时副本），
# 其余模块仍从生产 backend 解析 —— 于是同一个探针能跑"修前 / 修后"对照。
# 顺序很重要：生产 backend 先入列，命令行给的目录后入列 → 它排在 sys.path[0]。
# 用法：python probe_late_bg_event.py [<另一份 backend 目录>]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
for _extra in reversed([a for a in sys.argv[1:] if not a.startswith("-")]):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import events                     # noqa: E402
import sse                        # noqa: E402
import state                      # noqa: E402
import agent_proc                 # noqa: E402


def show(tag, snap):
    print("  %-34s turn=%-12s busy=%s dict=%s" % (
        tag, snap["turn_phase"], snap["busy"], snap))


def main():
    hub = sse.SSEHub()
    sm = state.StateMachine()
    mgr = agent_proc.AgentManager(hub, sm)
    sm.set_phase(events.PHASE_ON)
    sm.set_session("session_probe")

    print("== 1. 主人发消息，网关受理回合 r1 ==")
    sm.set_run("r1")
    show("受理后", sm.snapshot())

    print("== 2. 回合里：后台作业启动（tool_begin） ==")
    mgr._forward({"type": "tool_begin", "run_id": "r1", "session_id": "session_probe",
                  "tool": "execute_shell", "call_id": "c1"})
    show("tool_begin 后", sm.snapshot())

    print("== 3. AB 挂上作业、汇报完毕 → 回合正常收口（done） ==")
    mgr._forward({"type": "done", "run_id": "r1", "session_id": "session_probe",
                  "content": "已挂上 j1"})
    after_done = sm.snapshot()
    show("done 后", after_done)

    print("== 4. 【30 秒后】后台作业在 worker 线程里跑完，发出迟到的收尾事件 ==")
    # bridge 的 _wrap_tool finally: if still == 0: _set_turn(ctx, "THINKING")
    mgr._forward({"type": "tool_end", "run_id": "r1", "session_id": "session_probe",
                  "tool": "execute_shell", "call_id": "c1", "ok": True, "elapsed": 30.2})
    mgr._forward({"type": "turn_phase", "run_id": "r1", "session_id": "session_probe",
                  "phase": "THINKING"})
    late = sm.snapshot()
    show("迟到事件后", late)

    events_out = hub.history_snapshot(60)
    late_busy = [e for e in events_out if e.get("busy") is True]
    print("\n== 事件流里 busy=True 的帧 ==")
    for e in late_busy:
        print("   seq=%s type=%-12s run_id=%s busy=%s phase=%s" % (
            e.get("hub_seq"), e.get("type"), e.get("run_id"), e.get("busy"),
            e.get("phase") or e.get("turn_phase")))

    ok = True
    if late["busy"]:
        print("\n[FAIL] 回合已结束，迟到事件却把 busy 置回 True -> 前端会自己变回"
              "「回合进行中」，而 /chat/stop 会说 no-active-run（与真机现象一致）")
        ok = False
    else:
        print("\n[PASS] 迟到事件没有污染回合状态机")
    if any(e.get("busy") is True for e in events_out[3:]):
        print("[FAIL] 迟到的帧带着 busy=True 发给了前端（前端会照它置忙）")
        ok = False
    print("\n探针结论：%s" % ("通过" if ok else "复现了故障"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
