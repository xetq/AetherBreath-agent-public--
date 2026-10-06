# -*- coding: utf-8 -*-
"""真机验证 SSE 回放的 replayed 标记（网关侧）。

为什么需要它：前端对"回放帧"和"直播帧"的处理必须不同 —— 回放只是补时间线，
不是"此刻谁在等人"的证据。少了这个标记，初次打开（since=0，整环回放）时上一回合
的 done 会把正挂着的审批卡清掉。所以这里断言两件事：标记打上了，且环形历史本身
没被污染（回放是只读操作，不能给历史帧永久贴上 replayed）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "agent_webui", "backend"))

from sse import SSEHub  # noqa: E402

FAILS = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    if not cond:
        FAILS.append("%s %s" % (name, detail))


def main():
    h = SSEHub(history=10)
    h.publish("approval_batch", {"batch_id": "B_old", "session_id": "sA"})
    h.publish("tool_begin", {"call_id": "c1", "session_id": "sA"})
    h.publish("done", {"session_id": "sA", "content": "old", "interrupted": True})

    rp = h.replay(0)
    check("replay 回放出全部历史帧", len(rp) == 3, "实得 %d" % len(rp))
    check("replay 给每一帧打 replayed=True",
          all(e.get("replayed") is True for e in rp),
          repr([e.get("replayed") for e in rp]))
    check("replay 保留原字段（type/hub_seq/session_id 都在）",
          all(e.get("type") and e.get("hub_seq") and e.get("session_id") for e in rp),
          repr([(e.get("type"), e.get("hub_seq")) for e in rp]))

    hist = h.history_snapshot()
    check("replay 是只读的：环形历史本身没被贴上 replayed",
          all("replayed" not in e for e in hist),
          repr([e.get("type") for e in hist if "replayed" in e]))
    check("replay 返回的是副本（改它不影响历史）",
          hist[0].get("batch_id") == "B_old")

    check("replay 尊重 since 下界", h.replay(3) == [], repr(h.replay(3)))
    check("replay 只回放 since 之后的部分", len(h.replay(1)) == 2, "%d" % len(h.replay(1)))
    check("replay 按 session_id 过滤（别的会话不进来）",
          h.replay(0, "sB") == [], repr(h.replay(0, "sB")))
    check("replay 对无 session_id 的帧不设过滤门槛",
          len(h.replay(0, "sA")) == 3, "%d" % len(h.replay(0, "sA")))

    # 直播帧（publish 的返回值）不许带 replayed —— 它是"此刻"的权威来源
    evt = h.publish("ask_request", {"ask_id": "C_new", "session_id": "sA"})
    check("publish 出的直播帧不带 replayed", "replayed" not in evt, repr(evt.get("replayed")))

    print("=" * 68)
    for name, ok, detail in CHECKS:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, (" | " + detail) if detail else ""))
    print("=" * 68)
    print("结论：%d/%d 通过" % (len(CHECKS) - len(FAILS), len(CHECKS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
