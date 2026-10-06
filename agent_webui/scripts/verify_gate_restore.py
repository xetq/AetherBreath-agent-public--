# -*- coding: utf-8 -*-
"""卡片恢复的三层验收（一键跑）。

这张网是补一个教训的 ——
    上一版验收脚本用 page.request.get() 直接打后端端点。它测的是"接口在不在"，
    而不是"前端刷新之后卡片还在不在"。于是四层链路里前端那一层整段失效
    （前端拿 POST 打只注册了 GET 的端点，每个请求吃 405，又被 catch 静默吞掉），
    验收照样报"通过"。测错对象的验收网，比没有网更危险。
所以现在按层分开，每层断言自己那一层的行为：

  1. 服务端待裁决台账  verify_pending_cards.py   纯标准库，不花额度、不用开机
  1b. SSE 回放标记     verify_sse_replay.py      回放帧不得改写"此刻谁在等人"
  2. 前端状态语义      verify_gate_store.mjs     node + frontend/node_modules
  3. 真实浏览器链路    verify_gate_browser.mjs   自拉 headless Edge，需网关在跑

跑法：
  <项目根>/venv/Scripts/python.exe -X utf8 agent_webui/scripts/verify_gate_restore.py
  第 2/3 层需要 node；可用 AETHER_NODE 指定解释器路径。
  缺依赖的层会明确报 SKIP 并说清补法，不会静默跳过。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
BASE = os.environ.get("AETHER_WEBUI_BASE", "http://127.0.0.1:8900")
OUT = os.path.join(HERE, "verify_gate_restore.result.json")


def gateway_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def find_node() -> str:
    p = os.environ.get("AETHER_NODE") or ""
    if p and os.path.exists(p):
        return p
    hit = shutil.which("node")
    if hit:
        return hit
    # 用户机器上 node 装在非 PATH 位置时的常见候选
    for cand in (r"D:\BrainMind_Project\nodejs\node.exe",):
        if os.path.exists(cand):
            return cand
    return ""


def run_layer(name: str, cmd: list, pre: str = "") -> dict:
    rec = {"layer": name, "cmd": " ".join(cmd), "ok": False, "skipped": False}
    if pre == "gateway" and not gateway_up():
        rec["skipped"] = True
        rec["why"] = "网关没在跑（先运行 agent_webui/webui.bat），本层无法执行"
        return rec
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except Exception as e:
        rec["why"] = "%s: %s" % (type(e).__name__, e)
        return rec
    rec["exit"] = p.returncode
    rec["seconds"] = round(time.time() - t0, 1)
    tail = (p.stdout or "").strip().splitlines()
    rec["ok"] = p.returncode == 0
    rec["summary"] = [ln for ln in tail if ln.startswith("结论")][-1:] or tail[-3:]
    rec["fails"] = [ln for ln in tail if ln.startswith("FAIL")][:12]
    if not rec["ok"] and not rec["fails"]:
        rec["stderr_tail"] = (p.stderr or "").strip().splitlines()[-6:]
    return rec


def main() -> int:
    node = find_node()
    layers = [
        ("1. 服务端待裁决台账（pending_cards / answer / 超时）",
         [sys.executable, "-X", "utf8", os.path.join(HERE, "verify_pending_cards.py")], ""),
        ("1b. SSE 回放标记（回放只补时间线，不改写此刻状态）",
         [sys.executable, "-X", "utf8", os.path.join(HERE, "verify_sse_replay.py")], ""),
        ("2. 前端状态语义（切会话·刷新·重开·剪枝守卫·回放帧）",
         [node, os.path.join(HERE, "verify_gate_store.mjs")], "" if node else "node"),
        ("3. 真实浏览器链路（DOM 层：卡片真的出现在屏幕上）",
         [node, os.path.join(HERE, "verify_gate_browser.mjs")], "gateway" if node else "node"),
    ]

    print("=" * 74)
    print("卡片恢复验收：审批弹窗 + ask_user 提问 · 逐层跑")
    print("=" * 74)

    results = []
    for name, cmd, pre in layers:
        if pre == "node":
            rec = {"layer": name, "ok": False, "skipped": True,
                   "why": "找不到 node（设 AETHER_NODE 指向 node.exe，或把 nodejs 加进 PATH）"}
        else:
            rec = run_layer(name, cmd, pre)
        results.append(rec)
        mark = "SKIP" if rec.get("skipped") else ("PASS" if rec.get("ok") else "FAIL")
        print("\n[%s] %s" % (mark, name))
        if rec.get("why"):
            print("      原因：%s" % rec["why"])
        for ln in rec.get("summary") or []:
            print("      " + ln)
        for ln in rec.get("fails") or []:
            print("      " + ln)
        for ln in rec.get("stderr_tail") or []:
            print("      ! " + ln)

    scored = [r for r in results if not r.get("skipped")]
    bad = [r for r in scored if not r.get("ok")]
    print("\n" + "=" * 74)
    verdict = ("全部通过：%d/%d 层" % (len(scored), len(scored))) if not bad and scored \
        else ("未通过：%d/%d 层失败" % (len(bad), len(scored)))
    print("结论：%s" % verdict)
    if any(r.get("skipped") for r in results):
        print("注意：有层被跳过，上面的原因写清了补法 —— 跳过的层不算通过。")
    json.dump({"base": BASE, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "verdict": verdict, "layers": results},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("完整结果 ->", OUT)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
