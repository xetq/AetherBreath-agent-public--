# -*- coding: utf-8 -*-
"""多卡 ask 真机验收（L3：真模型 + 真工具询问通道）

要证明的三件事：
  1. 一个回合里 AB 并行问 N 个问题 → N 张卡**同时**挂在服务端台帐上（不是排队）
  2. **有人作答就把共享窗口续上**：等窗口衰减后再答一张，剩余时间应当回到满窗
     —— 这是"人还在一张张答，卡片却被各自计时判死"的反面证据
  3. 逐个答完后，这一批的答案**一起**回到模型（回复里能同时看到三个答案）
  4. 回合被终止时，挂着的卡立即出局（不留僵尸卡）

跑法（需网关 8900 + AB 已开机）：
    agent_webui/venv-gateway/Scripts/python.exe agent_webui/scripts/verify_multi_ask.py
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
API = "http://127.0.0.1:8900/api"

PROMPT = ("请立刻并行调用 ask_user 三次 —— 在**同一条消息**里发出三个工具调用，"
          "分别问：1) 更想喝哪种咖啡？（选项：美式、拿铁）"
          "2) 今天想听什么音乐？（选项：民谣、摇滚）"
          "3) 周末更想去哪？（选项：图书馆、公园）\n"
          "三个问题同时问、不要等回答。收齐三个答案后，用一行把三个答案都复述出来。")

results = []
events = []
lock = threading.Lock()
stop = threading.Event()
bad = 0


def rec(name, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print("%s %s | %s" % ("PASS" if ok else "FAIL", name, str(detail)[:400]), flush=True)


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
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def sse(sid):
    url = "http://127.0.0.1:8900/api/events?since=0&session_id=%s" % sid
    while not stop.is_set():
        try:
            r = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(r, timeout=300) as resp:
                buf = []
                while not stop.is_set():
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
                            with lock:
                                events.append(json.loads(blob))
                        except Exception:
                            pass
        except Exception:
            if not stop.is_set():
                time.sleep(0.3)


def wait_cond(pred, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def cl_events():
    with lock:
        return [e for e in events if e.get("type") == "ask_request"]


def pending():
    st, res = req("POST", "/ask/pending", {})
    return (res.get("cards") or []), res


def ask_round(sid, prompt):
    st, ch = req("POST", "/chat", {"session_id": sid, "message": prompt})
    return ch.get("run_id")


def main():
    st, h = req("GET", "/health", timeout=8)
    if st != 200 or h.get("agent", {}).get("phase") != "ON":
        rec("0.0 前置：网关在线且 AB 已开机", False, "phase=%s" % h.get("agent", {}).get("phase"))
        return finish()
    st, s = req("POST", "/sessions", {"prefix": "multiver", "title": "多卡 ask 验收"})
    sid = s.get("session_id")
    rec("0.1 新建会话", bool(sid), sid)
    threading.Thread(target=sse, args=(sid,), daemon=True).start()
    time.sleep(1.2)

    run = ask_round(sid, PROMPT)
    rec("0.2 回合已发起", bool(run), "run_id=%s" % run)

    ok3 = wait_cond(lambda: len(cl_events()) >= 3, timeout=150)
    time.sleep(4)                                   # 给同一批的其余 ask_user 到齐的时间
    evs = cl_events()
    cards, _ = pending()

    # —— 1. 多卡并存 ——
    rec("1. 同批并行提问：SSE 收到 3 个 ask_request", len(evs) >= 3, "收到 %d 个" % len(evs))
    rec("1b. 服务端台帐同时挂着 3 张卡（不是排队等）", len(cards) >= 3,
        "pending=%d" % len(cards))
    rec("1c. 事件带批次信息（共几问 / 还剩几问 / 共享剩余秒数）",
        all(("batch_size" in e and "batch_live" in e) for e in evs[:3]),
        json.dumps(evs[0], ensure_ascii=False)[:220] if evs else "(无事件)")
    rec("1d. 三张卡的 timeout 都是标准窗口 120s，但共享同一条 deadline",
        all(int(e.get("timeout") or 0) == 120 for e in evs[:3]),
        [e.get("timeout") for e in evs[:3]])

    # —— 2. 作答续窗（核心新行为）——
    time.sleep(25)                                  # 让窗口自然衰减
    mid_cards, _ = pending()
    before = int(mid_cards[0].get("remaining") or 0) if mid_cards else 0
    rec("2. 窗口自然衰减（等 25s 后剩余 ≈95s）", 80 <= before <= 105, "remaining=%s" % before)

    first = next((c for c in cards if c.get("ask_id")), None)
    picked = []
    first_pick = ""
    if first:
        # 每张卡答**它自己选项里**的值：给所有卡回同一个答案会让断言变成假阳性
        # （上一轮就这样：给音乐题回了"拿铁"，模型如实指出该值不在选项里）
        first_pick = (first.get("options") or ["美式"])[0]
        st1, ans1 = req("POST", "/ask/answer", {"ask_id": first["ask_id"], "answer": first_pick})
        rec("2b. 答第 1 张成功", st1 == 200 and ans1.get("ok"), json.dumps(ans1, ensure_ascii=False)[:160])
        after_cards, _ = pending()
        after = int(after_cards[0].get("remaining") or 0) if after_cards else 0
        rec("2c. **作答把共享窗口续上**：剩余时间回到满窗（不是继续 95s 往下掉）",
            after >= 110, "答前 %ss → 答后 %ss（期望回到 ≈120）" % (before, after))
        rec("2d. 台帐还剩 2 张（答一张只摘一张）", len(after_cards) == 2,
            "pending=%d" % len(after_cards))
        rec("2e. 回执带 batch_live / remaining（前端据此续算倒计时）",
            "batch_live" in ans1 and "remaining" in ans1, json.dumps(ans1, ensure_ascii=False)[:160])

        for c in after_cards:
            pick = (c.get("options") or ["?"])[0]
            req("POST", "/ask/answer", {"ask_id": c["ask_id"], "answer": pick})
            picked.append(pick)
        time.sleep(3)
        rest, _ = pending()
        rec("2f. 全部答完后台帐归零", len(rest) == 0, "pending=%d" % len(rest))
    else:
        rec("2b. 拿到第一张卡", False, "没有可用卡")

    # —— 3. 整批一起回模型 ——
    wait_cond(lambda: any(e.get("type") == "done" for e in events), timeout=200)
    with lock:
        done = [e for e in events if e.get("type") == "done"]
    reply = str(done[-1].get("content") or "") if done else ""
    rec("3. 回合收尾", bool(done), reply[:120])
    chosen = ([first_pick] if first_pick else []) + picked
    rec("3b. 三个答案都进了同一批 → 回复里能同时看到我实际提交的值 %s" % chosen,
        len(chosen) == 3 and all(x in reply for x in chosen), reply[:280])

    # —— 4. 回合终止 → 卡立即出局 ——
    time.sleep(1.5)
    run2 = ask_round(sid, PROMPT)
    got2 = wait_cond(lambda: len(cl_events()) >= 4, timeout=150)
    time.sleep(3)
    cards2, _ = pending()
    rec("4. 第二回合又挂上卡", got2 and len(cards2) >= 1, "pending=%d" % len(cards2))
    if cards2:
        req("POST", "/chat/stop", {"run_id": run2})
        time.sleep(4)
        left, _ = pending()
        rec("4b. 终止回合后卡立即出局（不留僵尸卡在屏幕上挂着）", len(left) == 0,
            "pending=%d" % len(left))
        with lock:
            resolved = [e for e in events if e.get("type") == "ask_resolved"]
        rec("4c. SSE 收到 ask_resolved（前端据此即时摘卡）", bool(resolved),
            json.dumps(resolved[-1], ensure_ascii=False)[:200] if resolved else "(无)")
    return finish(sid, run)


def finish(sid=None, run=None):
    stop.set()
    ok = sum(1 for r in results if r["ok"])
    print("\n==== 多卡 ask 真机验收：%s (%d/%d) ===="
          % ("全部通过" if ok == len(results) else "有失败", ok, len(results)), flush=True)
    try:
        out = __file__.replace("verify_multi_ask.py", "verify_multi_ask.result.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"session_id": sid, "run_id": run, "passed": ok,
                       "total": len(results), "results": results}, f,
                      ensure_ascii=False, indent=2)
        print("结果落盘：%s" % out, flush=True)
    except Exception as e:
        print("结果落盘失败：%s" % e, flush=True)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
