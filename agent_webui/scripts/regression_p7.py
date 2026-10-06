"""P7 全流程回归：真实走一遍 多工具 -> ask -> 优雅停 -> 强杀重启续聊。
用法（网关已在 8900 运行）：python scripts/regression_p7.py
结果：logs/regression_p7.json + 控制台摘要。退出码 0=全通过。
"""
import json, os, sys, threading, time, urllib.request

BASE = os.environ.get("AB_WEBUI", "http://127.0.0.1:8900")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "logs", "regression_p7.json")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def req(path, payload=None, method=None, timeout=30):
    """payload=None 时默认 GET；需要 POST 无体就传 {}。"""
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode() or "{}")

EV = []
LOCK = threading.Lock()

def sse_stop():
    pass

def sse_thread(stop):
    """长连接读事件；LLM 静默可能 >30s，故 socket 超时给 90s，断了按 hub_seq 续订。"""
    since = 0
    while not stop.is_set():
        try:
            with urllib.request.urlopen(BASE + "/api/events?since=%d" % since, timeout=90) as f:
                for raw in f:
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if isinstance(d.get("hub_seq"), int):
                        since = max(since, d["hub_seq"])
                    if d.get("type") == "heartbeat":
                        continue
                    with LOCK:
                        EV.append(d)
        except Exception as e:
            with LOCK:
                EV.append({"type": "sse_error", "text": repr(e)})
            time.sleep(1)

def wait_done(run_id, sec=150):
    t0 = time.time()
    while time.time() - t0 < sec:
        with LOCK:
            for e in EV:
                if e.get("run_id") == run_id and e["type"] in ("done", "error"):
                    return e
        time.sleep(0.5)
    return None

R = []
def check(name, ok, detail=""):
    R.append({"case": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + name + " | " + str(detail)[:180], flush=True)

def tools_of(run_id):
    with LOCK:
        return [e for e in EV if e.get("run_id") == run_id and e["type"] in ("tool_begin", "tool_end")]

def texts_of(run_id):
    with LOCK:
        return "".join(e.get("text", "") for e in EV if e.get("run_id") == run_id and e["type"] == "text")

# ---------- 环境 ----------
h = req("/api/health")
check("P7.0 网关健康", h.get("ok") is True, "dist_ready=%s" % h["paths"]["dist_ready"])
if h["agent"]["phase"] != "ON":
    req("/api/agent/start", {"mode": ""})
    time.sleep(22)
st = req("/api/agent/status")
check("P7.0 AB 已开机", st["phase"] == "ON", "pid=%s tools=%s" % (st.get("pid"), st.get("tools")))

sid = req("/api/sessions", {"prefix": "p7reg"})["session_id"]
stop = threading.Event()
threading.Thread(target=sse_thread, args=(stop,), daemon=True).start()
time.sleep(1.5)

# ---------- A 多工具 ----------
n0 = len(EV)
run = req("/api/chat", {"session_id": sid, "message":
      "请用 calculator 分别算 7*8 和 11*13（一次调用里发两个请求），然后用 read_file 读取 agent_webui/webui设计说明.txt 所在项目的 README.md 前几行，最后用一句话汇总三个结果。"})
ev = wait_done(run["run_id"])
tl = tools_of(run["run_id"])
names = sorted({e.get("tool") for e in tl if e["type"] == "tool_begin"})
ends = len([e for e in tl if e["type"] == "tool_end"])
check("P7.A 多工具时间线", bool(ev) and ev["type"] == "done" and ends >= 2 and len(names) >= 2,
      "ends=%d names=%s ev=%s" % (ends, names, ev and ev["type"]))
check("P7.A 文本含结果", "56" in (texts_of(run["run_id"]) + (ev or {}).get("content", "")),
      (texts_of(run["run_id"]) + (ev or {}).get("content", ""))[:60])

# ---------- B ask ----------
run = req("/api/chat", {"session_id": sid, "message":
      "必须调用 ask_user 工具问我一个单选题（正好3个选项，关于今晚喝什么），等我回答之后把我选的内容原样复述一遍。"})
ask = None
t0 = time.time()
while time.time() - t0 < 150 and not ask:
    with LOCK:
        for e in EV:
            if e.get("run_id") == run["run_id"] and e["type"] == "ask_request":
                ask = e
                break
    time.sleep(0.5)
if ask:
    time.sleep(1)
    ans = req("/api/ask/answer", {"ask_id": ask["ask_id"], "answer": "黑咖啡（苦到清醒）"})
    ev = wait_done(run["run_id"], 150)
    txt = texts_of(run["run_id"])
    check("P7.B ask 提问送达", bool(ask), "q=%s" % str(ask.get("question"))[:40])
    check("P7.B 事件类型未被 payload 覆盖", ask.get("type") == "ask_request" and ask.get("mode") in (None, "choice", "multi", "freeform"),
          "type=%s mode=%s" % (ask.get("type"), ask.get("mode")))
    check("P7.B 回答后续跑完成", ev and ev["type"] == "done" and "咖啡" in txt, "done=%s txt=%s" % (ev and ev["type"], txt[:60]))
else:
    check("P7.B ask 提问送达", False, "150s 内未收到 ask 事件")
    check("P7.B 回答后续跑完成", False, "跳过")

# ---------- C 优雅停止 ----------
run = req("/api/chat", {"session_id": sid, "message":
      "依次用 calculator 计算 11*11、12*12、13*13、14*14、15*15、16*16，每一步都单独调用工具，最后汇总。"})
time.sleep(12)
req("/api/chat/stop", {"run_id": run["run_id"]})
ev = wait_done(run["run_id"], 60)
time.sleep(1)
st = req("/api/agent/status")
check("P7.C 优雅中断生效", ev and ev["type"] == "done" and ev.get("interrupted") and st["turn_phase"] == "IDLE",
      "ev=%s turn=%s" % (ev and ev["type"], st["turn_phase"]))

# ---------- D 强杀 + 重开 + 续聊 ----------
req("/api/agent/kill", {})
time.sleep(3)
st = req("/api/agent/status")
check("P7.D 强杀后 OFF", st["phase"] == "OFF", "phase=%s" % st["phase"])
req("/api/agent/start", {"mode": ""})
t0 = time.time()
while time.time() - t0 < 70:
    st = req("/api/agent/status")
    if st["phase"] == "ON" and st.get("can_send"):
        break
    time.sleep(2)
check("P7.D 重开可发", st["phase"] == "ON", "pid=%s tools=%s" % (st.get("pid"), st.get("tools")))

run = req("/api/chat", {"session_id": sid, "message": "回顾本会话上文：我之前让你算过哪两个数的乘法？只回答数字，别调工具。"})
ev = wait_done(run["run_id"], 90)
txt = texts_of(run["run_id"])
ok_hist = bool(ev) and ev["type"] == "done" and (("56" in txt or "143" in txt) or ("7" in txt and "8" in txt and "11" in txt))
check("P7.D 重启后续聊（历史恢复）", ok_hist,
      "ev=%s txt=%s" % (ev and ev["type"], txt[:70]))

# ---------- E 追发竞态 + 越权 stop ----------
runA = req("/api/chat", {"session_id": sid, "message": "用 calculator 算 3*3，只回数字。"})
evA = wait_done(runA["run_id"], 90)
try:
    runB = req("/api/chat", {"session_id": sid, "message": "再用 calculator 算 4*4，只回数字。"})
    ok_race = True
    evB = wait_done(runB["run_id"], 90)
except Exception as e:
    ok_race = "409" not in repr(e)
    evB = None
check("P7.E done 后立刻追发不 409", bool(evA) and ok_race, "A=%s B=%s" % (evA and evA["type"], evB and evB["type"]))
try:
    req("/api/chat/stop", {"run_id": "deadbeef0000"})
    check("P7.E 未知 run_id 的 stop 不误伤", False, "竟然接受了 bogus run_id")
except Exception as e:
    check("P7.E 未知 run_id 的 stop 不误伤", "409" in repr(e) or "run-not-found" in repr(e) or "404" in repr(e), repr(e)[:80])

# ---------- F 界面默认停止路径（v1 曾在 bridge 内自死锁） ----------
t_f = time.time()
try:
    r_f = req("/api/chat/stop", {})          # 前端 run_id 为 null 时就是这个形状
    ok_f = (time.time() - t_f) < 3.0 and r_f.get("reason") in ("no-active-run", None) or r_f.get("stopped") is not None
    detail_f = "%.2fs %s" % (time.time() - t_f, json.dumps(r_f, ensure_ascii=False)[:80])
except Exception as e:
    ok_f, detail_f = False, "挂了/超时：%s" % repr(e)[:70]
check("P7.F 无 run_id 的 stop 不卡死", ok_f, detail_f)
# 上一发若死锁，/health 会一起烂掉 —— 这就是判据
h_f = req("/api/health", timeout=8)
check("P7.F stop 之后 /health 仍存活", bool(h_f.get("ok")), "health_at=%s" % h_f["agent"].get("health_at"))

stop.set()
req("/api/sessions/%s" % sid, None, "DELETE")
allok = all(x["ok"] for x in R)
json.dump({"session_id": sid, "all_ok": allok, "cases": R, "ts": time.strftime("%F %T")},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n==== 回归总结：%s (%d/%d) ====" % ("全部通过" if allok else "有失败", sum(x["ok"] for x in R), len(R)))
sys.exit(0 if allok else 1)
