# -*- coding: utf-8 -*-
"""中期交互端到端验收（L3 真机层，需网关 8900 且 AB 已开机）

走的是**与前端完全相同**的那条链路：网关 REST + SSE
（/api/chat/mid_turn → bridge /mid_turn → 信箱 → AB 在下一批工具返回处注入）。

分两个场景，都用真模型、真工具、真落盘：

场景一 · 送达
  E2E.0 工具真跑起来（此刻回合在跑，投递窗口是确定的，不靠 sleep 猜时间）
  E2E.1 跑动中投递 → 200 + item_id
  E2E.1b SSE 收到 mid_turn:accepted（带 run_id/item_id，前端据此落座）
  E2E.2 会话不符 → 409（绝不投进别人的语境）
  E2E.3 假 run_id → 404（不回落到别人的回合）
  E2E.4 SSE 收到 mid_turn:injected，且时机在「本批工具返回之后」
  E2E.5 **原始会话文件**里：交代是独立的 user 消息、紧跟 tool 消息、
        与 tool_call_id 配对完整、后接 AB 的收尾回复
  E2E.6 模型真收到：AB 回复体现交代内容
  E2E.7 回合结束后投递 → 409（不会悄悄变成一次普通发送）

场景二 · 作废（主人选定的语义：回合结束仍未送达 = 丢弃 + 明确提示）
  E2E.8 投递后立刻中断回合 → SSE 收到 mid_turn:dropped(count=1, turn-ended)
  E2E.9 该交代**没有**落进会话文件（作废就是真作废，不留到下一个回合诈尸）

跑法（项目根）：
    agent_webui/venv-gateway/Scripts/python.exe agent_webui/scripts/mid_turn_e2e.py
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GW = "http://127.0.0.1:8900"
API = GW + "/api"
ROOT = Path(__file__).resolve().parent.parent.parent
WORKING_MEMORY = ROOT / "agent_memory" / "working_memory"

MID_TEXT = ("追加一条临时要求：最终回复里必须原样包含口令「紫电青霜」四个字，"
            "并在句末写（已按中期交代执行）。")
DISCARD_TEXT = "这句会被作废的口令是「寒江孤影」"
PROMPT = ("请只做一件事：用 execute_shell 执行命令 `ping 127.0.0.1 -n 6`（耗时约 5 秒）。"
          "跑完后用一句话告诉我输出大意，不要调用其它工具。")

results = []
events = []
ev_lock = threading.Lock()
stop_flag = threading.Event()


def rec(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print("%s %s | %s" % ("PASS" if ok else "FAIL", name, str(detail)[:500]), flush=True)


def req(method, path, payload=None, timeout=30):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def why(body):
    """网关的错误文案统一落在 detail（HTTPException 语义）。"""
    return json.dumps(body, ensure_ascii=False)


def sse_reader(since, sid):
    url = "%s/events?since=%d&session_id=%s" % (API, since, sid)
    while not stop_flag.is_set():
        try:
            r = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(r, timeout=300) as resp:
                buf = []
                while not stop_flag.is_set():
                    line = resp.readline()
                    if not line:
                        break
                    s = line.decode("utf-8", "replace").rstrip("\r\n")
                    if s.startswith("data:"):
                        buf.append(s[5:].strip())
                    elif s == "" and buf:
                        blob = "\n".join(buf)
                        buf.clear()
                        try:
                            with ev_lock:
                                events.append(json.loads(blob))
                        except Exception:
                            pass
        except Exception:
            if stop_flag.is_set():
                return
            time.sleep(0.4)


def wait_for(pred, timeout=200):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with ev_lock:
            for e in events:
                if pred(e):
                    return e
        if stop_flag.is_set():
            return None
        time.sleep(0.12)
    return None


def session_file(sid):
    return WORKING_MEMORY / ("%s.json" % sid)


def raw_messages(sid):
    """直接读落盘原文 —— 比 /history API 更接近真相（那个接口把 tool 消息折叠了）。"""
    f = session_file(sid)
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("messages", [])
    except Exception:
        return []


def mid_events(run_id=None):
    with ev_lock:
        return [e for e in events if e.get("type") == "mid_turn" and e.get("run_id") == run_id]


def scenario_deliver(sid):
    st, ch = req("POST", "/chat", {"session_id": sid, "message": PROMPT})
    run_id = (ch or {}).get("run_id")
    rec("0.2 回合已发起", st == 200 and bool(run_id), "run_id=%s status=%s" % (run_id, st))
    if not run_id:
        return None, None

    begin = wait_for(lambda e: e.get("type") == "tool_begin" and e.get("session_id") == sid, 180)
    rec("E2E.0 工具真跑起来了（投递窗口开启）", begin is not None,
        "tool=%s 线程=%s" % ((begin or {}).get("tool"), (begin or {}).get("thread")))

    st2, bad = req("POST", "/chat/mid_turn",
                   {"session_id": "mide2e_wrong_one", "text": "这句不该被接收", "run_id": run_id})
    rec("E2E.2 会话不符被拒（不投进别人的语境）",
        st2 == 409 and "无处可投" in why(bad), "status=%s %s" % (st2, why(bad)))

    st3, badrun = req("POST", "/chat/mid_turn",
                      {"session_id": sid, "text": "投给一个不存在的回合", "run_id": "ffffffffffff"})
    rec("E2E.3 假 run_id 被拒（不回落投给别人）",
        st3 == 404 and "已经结束" in why(badrun), "status=%s %s" % (st3, why(badrun)))

    st4, acc = req("POST", "/chat/mid_turn",
                   {"session_id": sid, "text": MID_TEXT, "run_id": run_id})
    item_id = (acc or {}).get("item_id")
    rec("E2E.1 跑动中投递被受理", st4 == 200 and acc.get("ok") and bool(item_id),
        "status=%s item_id=%s pending=%s" % (st4, item_id, acc.get("pending")))

    acc_evt = wait_for(lambda e: e.get("type") == "mid_turn" and e.get("mid") == "accepted"
                       and e.get("run_id") == run_id, 30)
    rec("E2E.1b SSE 收到 accepted（带 run_id + item_id）",
        acc_evt is not None and acc_evt.get("item_id") == item_id,
        json.dumps(acc_evt or {}, ensure_ascii=False)[:300])

    inj = wait_for(lambda e: e.get("type") == "mid_turn" and e.get("mid") == "injected"
                   and e.get("run_id") == run_id, 200)
    rec("E2E.4 SSE 收到 injected（带 run_id + ids）",
        inj is not None and item_id in (inj.get("ids") or []),
        json.dumps(inj or {}, ensure_ascii=False)[:300])

    with ev_lock:
        t_end = [e for e in events if e.get("type") == "tool_end" and e.get("session_id") == sid]
    if inj and t_end:
        # 注意：网关会剥掉 bridge 的 seq/ts 再重新编号（hub_seq 才是 SSE 帧里的单调序号）
        rec("E2E.4b 注入时机正确：在本批工具返回之后",
            min(x.get("hub_seq", 0) for x in t_end) < inj.get("hub_seq", 0),
            "tool_end hub_seq=%s < injected hub_seq=%s"
            % ([x.get("hub_seq") for x in t_end], inj.get("hub_seq")))

    done = wait_for(lambda e: e.get("type") == "done" and e.get("run_id") == run_id, 260)
    rec("0.3 回合正常结束", done is not None and not done.get("interrupted"),
        json.dumps(done or {}, ensure_ascii=False)[:200])
    return run_id, done


def check_landing(sid, done):
    msgs = raw_messages(sid)
    idx = next((i for i, m in enumerate(msgs)
                if m.get("role") == "user" and "紫电青霜" in (m.get("content") or "")), None)
    if idx is None:
        rec("E2E.5 落盘原文里存在该「用户交代」", False,
            "messages=%d 文件=%s" % (len(msgs), session_file(sid)))
        return
    content = msgs[idx]["content"]
    rec("E2E.5 落盘原文里存在该「用户交代」（带标识前缀）",
        content.startswith("【用户交代"), content.splitlines()[0])
    prev = msgs[idx - 1] if idx > 0 else {}
    rec("E2E.5b 它紧跟本批工具返回（独立消息，未污染工具输出）",
        prev.get("role") == "tool", "前一条 role=%s" % prev.get("role"))
    if prev.get("role") == "tool":
        prev_tid = prev.get("tool_call_id")
        owner = msgs[idx - 2] if idx >= 2 else {}
        ids = [tc.get("id") for tc in (owner.get("tool_calls") or [])]
        rec("E2E.5c tool_call_id 配对完整（会话不会因此断头）",
            prev_tid in ids, "tool_call_id=%s ⊆ %s" % (prev_tid, ids))
    nxt = msgs[idx + 1] if idx + 1 < len(msgs) else {}
    rec("E2E.5d 它后面是 AB 的收尾回复（模型带着它继续推理）",
        nxt.get("role") == "assistant", "后一条 role=%s" % nxt.get("role"))

    reply = (done or {}).get("content") or ""
    rec("E2E.6 AB 回复体现了交代内容（口令 + 遵从声明）",
        ("紫电青霜" in reply) and ("中期交代" in reply),
        "reply=%s" % reply.replace("\n", " ")[:260])


def scenario_discard(sid, run_id):
    """场景二：投递后立刻中断回合 —— 交代必须被作废并明确回报。"""
    st, ch = req("POST", "/chat", {"session_id": sid, "message": PROMPT})
    run2 = (ch or {}).get("run_id")
    begin = wait_for(lambda e: e.get("type") == "tool_begin" and e.get("run_id") == run2, 180)
    rec("E2E.7 第二回合工具已开跑", begin is not None and bool(run2), "run_id=%s" % run2)
    if not run2:
        return
    st2, acc = req("POST", "/chat/mid_turn",
                   {"session_id": sid, "text": DISCARD_TEXT, "run_id": run2})
    rec("E2E.7b 投递成功（接下来它必须因为回合被中断而作废）",
        st2 == 200 and acc.get("ok"), "status=%s item_id=%s" % (st2, acc.get("item_id")))
    req("POST", "/chat/stop", {"run_id": run2})          # 立刻中断

    dropped = wait_for(lambda e: e.get("type") == "mid_turn" and e.get("mid") == "dropped"
                       and e.get("run_id") == run2, 200)
    rec("E2E.8 SSE 收到 dropped（回合结束未送达 → 明确回报）",
        dropped is not None and dropped.get("count") == 1
        and dropped.get("reason") == "turn-ended"
        and acc.get("item_id") in (dropped.get("ids") or []),
        json.dumps(dropped or {}, ensure_ascii=False)[:300])

    msgs = raw_messages(sid)
    landed = any("寒江孤影" in (m.get("content") or "") for m in msgs)
    rec("E2E.9 作废就是真作废：该交代未落进会话文件", not landed,
        "落盘命中=%s messages=%d" % (landed, len(msgs)))


def finish(sid=None, run_id=None):
    stop_flag.set()
    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    print("\n==== 中期交互端到端：%s (%d/%d) ====" % ("全部通过" if ok == total else "有失败", ok, total),
          flush=True)
    try:
        out = Path(__file__).resolve().with_name("mid_turn_e2e.result.json")
        out.write_text(json.dumps({"session_id": sid, "run_id": run_id, "passed": ok, "total": total,
                                   "results": results, "mid_events": mid_events(run_id)},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print("结果落盘：%s" % out, flush=True)
    except Exception as e:
        print("结果落盘失败：%s" % e, flush=True)
    return 0 if ok == total else 1


def main():
    st, health = req("GET", "/health", timeout=8)
    agent = health.get("agent", {})
    if st != 200 or agent.get("phase") != "ON":
        rec("0.0 前置：网关在线且 AB 已开机", False, "status=%s phase=%s" % (st, agent.get("phase")))
        return finish()
    injected = agent.get("injected") or []
    rec("0.0 前置：bridge 已载入 mid_turn 通道", "mid_turn" in injected, "injected=%s" % injected)

    st, s = req("POST", "/sessions", {"prefix": "mide2e", "title": "中期交互E2E"})
    sid = (s or {}).get("session_id")
    rec("0.1 新建会话", st == 200 and bool(sid), "sid=%s" % sid)
    if not sid:
        return finish()

    threading.Thread(target=sse_reader, args=(0, sid), daemon=True).start()
    time.sleep(1.5)

    run_id, done = scenario_deliver(sid)
    if run_id and done is not None:
        check_landing(sid, done)

    time.sleep(1.5)                                   # 让 bridge 收尾干净
    st, after = req("POST", "/chat/mid_turn", {"session_id": sid, "text": "太晚了"})
    rec("E2E.7c 无活动回合时投递被拒（不悄悄变成新回合）",
        st == 409 and "没有正在跑的回合" in why(after), "status=%s %s" % (st, why(after)))

    scenario_discard(sid, run_id)
    return finish(sid, run_id)


if __name__ == "__main__":
    sys.exit(main())
