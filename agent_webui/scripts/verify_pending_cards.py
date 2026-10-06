# -*- coding: utf-8 -*-
"""真机验证「卡片恢复」的服务端一侧：approval_adapter.pending_cards()。

为什么单独有这个脚本：
  WebUI 的四个"卡片会消失"现象，前端那一侧靠 Playwright 验；服务端这一侧要
  证明的是"卡还挂着的时候，pending_cards() 真能把完整内容报出来，且裁决/超时
  之后立刻不再报"—— 报早了是复活僵尸卡，报晚了等于没恢复。

跑法（不需要 LLM、不需要开机、不产生任何真实操作）：
  <项目根>/venv/Scripts/python.exe -X utf8 agent_webui/scripts/verify_pending_cards.py

它直接调用生产文件 agent_webui/backend/approval_adapter.py，
用与 bridge 完全相同的入口（WebUIPort.request_many）挂起等待者。
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "agent_webui", "backend"))
sys.path.insert(0, os.path.join(ROOT, "agent"))

import approval_adapter  # noqa: E402

FAILS = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    if not cond:
        FAILS.append("%s %s" % (name, detail))


def single_req(ask_id, timeout=30):
    return {
        "ask_id": ask_id, "kind": "write_file", "risk": 3, "title": "写系统盘目录",
        "intent": "在 C:/Windows/Temp 下写一个探针文件", "reason": "用户要求清理临时文件",
        "paths": ["C:/Windows/Temp/aether-probe.txt"], "notes": ["来源：用户上一条指令"],
        "critical": False, "source_code": "open(r'C:/Windows/Temp/aether-probe.txt','w')",
        "session_id": "probe_session_alpha", "timeout": timeout,
        "accepts_note": True, "note_hint": "可补充说明",
    }


def main():
    events = []
    approval_adapter.set_emit(lambda t, p: events.append((t, dict(p))))
    port = approval_adapter.WebUIPort()
    result = {}

    # ---------- 1. 单条：挂起后必须能报出完整卡片 ----------
    req = single_req("probe_single_1", timeout=30)
    th = threading.Thread(target=lambda: result.setdefault("single", port.request_many([req])),
                          daemon=True)
    th.start()
    time.sleep(0.4)

    cards = approval_adapter.pending_cards()
    check("单条：pending 恰好 1 张", len(cards) == 1, "实得 %d" % len(cards))
    if cards:
        c = cards[0]
        check("单条：type=approval_request", c.get("type") == "approval_request", repr(c.get("type")))
        check("单条：ask_id 原样回传", c.get("ask_id") == "probe_single_1", repr(c.get("ask_id")))
        check("单条：restored 标记", c.get("restored") is True)
        check("单条：intent 在", "C:/Windows/Temp" in str(c.get("intent")), repr(c.get("intent")))
        check("单条：paths 在", c.get("paths") == ["C:/Windows/Temp/aether-probe.txt"],
              repr(c.get("paths")))
        check("单条：notes 在", c.get("notes") == ["来源：用户上一条指令"], repr(c.get("notes")))
        check("单条：source_code 在", bool(c.get("source_code")))
        check("单条：session_id 在", c.get("session_id") == "probe_session_alpha",
              repr(c.get("session_id")))
        check("单条：timeout 显式给出（前端倒计时靠它折算）",
              int(c.get("timeout") or 0) == 30, repr(c.get("timeout")))
        rem1 = int(c.get("remaining") or 0)
        check("单条：remaining 落在 (0,30]", 0 < rem1 <= 30, repr(rem1))
        time.sleep(1.1)
        cards2 = approval_adapter.pending_cards()
        rem2 = int(cards2[0].get("remaining")) if cards2 else 0
        check("单条：remaining 随时间递减", rem2 < rem1, "%s -> %s" % (rem1, rem2))

    # ---------- 2. 裁决之后立刻不再报（防复活僵尸卡） ----------
    ans = approval_adapter.answer("probe_single_1", "", {"approved": ["probe_single_1"],
                                                        "scope": "once", "note": "探针批准"})
    check("单条：裁决被接受", ans.get("ok") is True, json.dumps(ans, ensure_ascii=False))
    th.join(timeout=3)
    check("单条：等待者被唤醒（没超时）", not th.is_alive(),
          "线程仍在等" if th.is_alive() else "已返回")
    time.sleep(0.2)
    check("单条：裁决后 pending 为空", approval_adapter.pending_cards() == [],
          repr(approval_adapter.pending_cards()))
    d = (result.get("single") or [None])[0]
    check("单条：引擎拿到 A/批准的裁决", getattr(d, "choice", None) == "A", repr(getattr(d, "choice", None)))

    # ---------- 3. 合并卡：字段与审批一致，裁决后消失 ----------
    reqs = [single_req("probe_batch_1"), single_req("probe_batch_2")]
    reqs[1]["paths"] = ["C:/Windows/System32/probe.dll"]
    reqs[1]["critical"] = True
    th2 = threading.Thread(target=lambda: result.setdefault("batch", port.request_many(reqs)),
                           daemon=True)
    th2.start()
    time.sleep(0.4)
    bc = approval_adapter.pending_cards()
    check("合并卡：pending 恰好 1 张", len(bc) == 1, "实得 %d" % len(bc))
    if bc:
        b = bc[0]
        check("合并卡：type=approval_batch", b.get("type") == "approval_batch", repr(b.get("type")))
        check("合并卡：items 2 条", len(b.get("items") or []) == 2, repr(len(b.get("items") or [])))
        check("合并卡：total=2", int(b.get("total") or 0) == 2, repr(b.get("total")))
        check("合并卡：critical 透传",
              [bool(i.get("critical")) for i in (b.get("items") or [])] == [False, True],
              repr([i.get("critical") for i in (b.get("items") or [])]))
        check("合并卡：scopes 三档", len(b.get("scopes") or []) == 3)
        check("合并卡：session_id 在", b.get("session_id") == "probe_session_alpha",
              repr(b.get("session_id")))
        check("合并卡：accepts_note 透传", b.get("accepts_note") is True)
    # 合并卡的 key 是 batch_id（前端提交的也是它，见 reducer 里 ask_id: id）——
    # 拿子项的 ask_id 去 answer 会找不到批卡，等待者就一直挂着。
    batch_key = (bc[0].get("ask_id") if bc else "") or ""
    check("合并卡：key 是 batch_id 而非子项 id",
          bool(batch_key) and batch_key not in ("probe_batch_1", "probe_batch_2"), repr(batch_key))
    ans2 = approval_adapter.answer(batch_key, "", {"approved": ["probe_batch_1"], "scope": "once"})
    check("合并卡：裁决被接受", ans2.get("ok") is True, json.dumps(ans2, ensure_ascii=False))
    th2.join(timeout=3)
    time.sleep(0.2)
    check("合并卡：裁决后 pending 为空", approval_adapter.pending_cards() == [],
          repr(len(approval_adapter.pending_cards())))

    # ---------- 4. 超时：到点后不再报（等待者自己会按拒绝结算） ----------
    req3 = single_req("probe_timeout_1", timeout=6)
    th3 = threading.Thread(target=lambda: result.setdefault("timeout", port.request_many([req3])),
                           daemon=True)
    th3.start()
    time.sleep(0.3)
    check("超时：挂起时能报出", len(approval_adapter.pending_cards()) == 1)
    time.sleep(7.0)
    check("超时：到点后不再报（不许复活）", approval_adapter.pending_cards() == [],
          repr(approval_adapter.pending_cards()))
    th3.join(timeout=3)
    d3 = (result.get("timeout") or [None])[0]
    check("超时：引擎拿到 expired（拒绝，不是放行）",
          getattr(d3, "how", None) == "expired", repr(getattr(d3, "how", None)))

    # ---------- 5. 无通道时不得静默放行（fail-closed 复查） ----------
    approval_adapter.set_emit(None)
    res5 = port.request_many([single_req("probe_nochan_1")])
    check("无通道：返回 E/no_channel（拒绝）",
          getattr(res5[0], "choice", None) == "E" and getattr(res5[0], "how", None) == "no_channel",
          repr((getattr(res5[0], "choice", None), getattr(res5[0], "how", None))))

    # ---------- 输出 ----------
    print("=" * 68)
    for name, ok, detail in CHECKS:
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, (" | " + detail) if detail else ""))
    print("=" * 68)
    print("结论：%d/%d 通过" % (len(CHECKS) - len(FAILS), len(CHECKS)))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
    print("事件类型序列：", [t for t, _ in events])
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
