# -*- coding: utf-8 -*-
"""探针：上下文压缩的触发判据（纯 token 闸门）。

跑法（任意目录，脚本自己找项目根）：
    venv/Scripts/python self_maintenance/packs/compact-gate-probe/probe_compact_gate.py

期望：末行 `RESULT: PASS (0 failed)`，退出码 0。
若 FAIL —— 说明 `agent/context_manager.py::should_compact()` 的判据变了或写歪了。

背景（2026-09-22 主人裁决）：
  · 压缩**只由 token 闸门决定**（prompt_tokens ≥ window × trigger_ratio，默认 0.55）
  · 轮数防线（rounds_threshold）**已删除** —— 它与真实占用脱钩，会把进行中的任务历史
    反复折叠 → 任务循环、agent 空转（实测 9/9 个视图都是轮数触发，最高才 358k tokens）
  · 冷却 cooldown_rounds 保留，只做节流（防连续压），不参与「该不该压」的决策

副作用：无。纯函数调用，不读也不写任何会话 / 视图文件。
"""
import sys
from pathlib import Path


def _find_root(start):
    p = Path(start).resolve()
    for cand in [p.parent] + list(p.parents):
        if (cand / "agent" / "context_manager.py").exists():
            return cand
    raise SystemExit("找不到项目根（向上找不到 agent/context_manager.py）")


ROOT = _find_root(__file__)
sys.path.insert(0, str(ROOT / "agent"))
from context_manager import should_compact, load_params, DEFAULT_PARAMS, window_for  # noqa: E402

CFG = {"enabled": True, "window_tokens": 1000000, "trigger_ratio": 0.55,
       "keep_recent_rounds": 10, "cooldown_rounds": 2, "model_windows": {"default": 1000000}}
P = load_params(CFG)
W = window_for(P, "m")
GATE = int(W * 0.55)


def judge(pt, since, p=P, w=W):
    ok, why = should_compact(prompt_tokens=pt, rounds_total=999, rounds_since_compact=since,
                             window=w, params=p)
    return ok, why


CASES = [
    ("rounds-only no longer fires",      1000,    100, False, "below_threshold"),
    ("rounds-only 1000 rounds",          1000,   1000, False, "below_threshold"),
    ("token exactly at gate",          550000,      5, True,  "token_threshold"),
    ("token one below gate",           549999,      5, False, "below_threshold"),
    ("token hit but cooldown active",  900000,      1, False, "cooldown"),
    ("token hit cooldown passed",      900000,      2, True,  "token_threshold"),
    ("no usage (None) never fires",      None,     99, False, "below_threshold"),
    ("classic incident: 358k @ 28rd",  358674,     28, False, "below_threshold"),
]

fails = 0
for name, pt, since, w_ok, w_why in CASES:
    ok, why = judge(pt, since)
    good = (ok == w_ok) and (why == w_why or why.startswith(w_why))
    if not good:
        fails += 1
    print("%-34s token=%-8s since=%-4s -> %-8s %-18s %s"
          % (name, pt, since, "COMPRESS" if ok else "skip", why, "PASS" if good else "FAIL"))

print("-" * 78)

d = DEFAULT_PARAMS["trigger_ratio"]
good = abs(d - 0.55) < 1e-9
print("DEFAULT trigger_ratio = %s  %s" % (d, "PASS" if good else "FAIL"))
if not good:
    fails += 1

ok, why = judge(900000, 5, p=load_params({"enabled": False}))
good = (not ok) and why == "disabled"
print("disabled wins            -> %-8s %-18s %s"
      % ("COMPRESS" if ok else "skip", why, "PASS" if good else "FAIL"))
if not good:
    fails += 1

p0 = load_params(dict(CFG, cooldown_rounds=0))
ok, why = judge(900000, 1, p=p0)
good = ok and why == "token_threshold"
print("cooldown_rounds=0        -> %-8s %-18s %s"
      % ("COMPRESS" if ok else "skip", why, "PASS" if good else "FAIL"))
if not good:
    fails += 1

print("-" * 78)
print("GATE = %d (window %d x 0.55)" % (GATE, W))
print("RESULT: %s (%d failed)" % ("PASS" if fails == 0 else "FAIL", fails))
sys.exit(1 if fails else 0)
