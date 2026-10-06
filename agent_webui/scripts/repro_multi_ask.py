# -*- coding: utf-8 -*-
"""多卡 ask 取证探针：让 AB 并行调 3 次 ask_user，如实记录链路各层的真实表现。

不修任何东西，只观察与打印，用来回答三个问题：
  A. 三个 ask_user 会不会真的并行进入 ask 通道（SSE 收到几个 ask_request）
  B. 服务端台帐里同时挂着几张卡（/api/ask/pending）
  C. 回答第一张之后，剩下两张还在不在台帐里、能不能接着答

跑法（需网关 8900 + AB 已开机）：
    agent_webui/venv-gateway/Scripts/python.exe agent_webui/scripts/repro_multi_ask.py
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
          "分别问下面三个彼此独立的问题（各带两个选项）：\n"
          "1) 你现在更想喝哪种咖啡？选项：美式、拿铁\n"
          "2) 今天想听什么音乐？选项：民谣、摇滚\n"
          "3) 周末更想去哪？选项：图书馆、公园\n"
          "三个问题要同时问，不要等第一个的回答再问第二个。")

events = []
lock = threading.Lock()
stop = threading.Event()


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


def ask_events():
    with lock:
        return [e for e in events if e.get("type") == "ask_request"]


def wait_cond(pred, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def show_pending(tag):
    st, res = req("POST", "/ask/pending", {})
    cards = res.get("cards") or []
    print("\n[%s] /api/ask/pending → %s 张（authoritative=%s）"
          % (tag, len(cards), res.get("authoritative")), flush=True)
    for c in cards:
        print("      ask_id=%s  q=%s" % (c.get("ask_id"), str(c.get("question"))[:34]), flush=True)
    return cards


def main():
    st, h = req("GET", "/health", timeout=8)
    if st != 200 or h.get("agent", {}).get("phase") != "ON":
        print("前置失败：网关或 AB 未就绪"); return 1
    st, s = req("POST", "/sessions", {"prefix": "multiclar", "title": "多卡 ask 取证"})
    sid = s.get("session_id")
    print("会话：%s" % sid, flush=True)
    threading.Thread(target=sse, args=(sid,), daemon=True).start()
    time.sleep(1.2)

    st, ch = req("POST", "/chat", {"session_id": sid, "message": PROMPT})
    print("回合：%s (status=%s)" % (ch.get("run_id"), st), flush=True)

    got = wait_cond(lambda: len(ask_events()) >= 1, timeout=120)
    print("\n第 1 个 ask_request 到达：%s" % got, flush=True)
    time.sleep(6)                                  # 给并行的其它 ask_user 足够时间到达

    evs = ask_events()
    print("\n=== A. SSE 收到的 ask_request ===")
    print("共 %d 个：" % len(evs))
    for e in evs:
        print("   ask_id=%s  timeout=%s  q=%s" % (e.get("ask_id"), e.get("timeout"),
                                                  str(e.get("question"))[:34]))

    cards = show_pending("B. 回答前")
    print("=== B 结论：链路里同时挂着 %d 张卡（SSE 事件 %d 个）===" % (len(cards), len(evs)))

    if cards:
        first = cards[0]["ask_id"]
        st, ans = req("POST", "/ask/answer", {"ask_id": first, "answer": "美式"})
        print("\n[C] 回答第 1 张 ask_id=%s → status=%s %s" % (first, st, ans), flush=True)
        time.sleep(4)
        left = show_pending("C. 回答第 1 张之后")
        print("=== C 结论：回答一张后，台帐里还剩 %d 张 ===" % len(left))
        for c in left:
            st2, a2 = req("POST", "/ask/answer", {"ask_id": c["ask_id"], "answer": "拿铁/民谣"})
            print("      补答 ask_id=%s → %s %s" % (c["ask_id"], st2, a2), flush=True)
        time.sleep(3)
        show_pending("D. 全部补答之后")

    wait_cond(lambda: any(e.get("type") == "done" for e in events), timeout=180)
    with lock:
        done = [e for e in events if e.get("type") == "done"]
    if done:
        print("\n回合收尾：%s" % str(done[-1].get("content"))[:200], flush=True)
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
