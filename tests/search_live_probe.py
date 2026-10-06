# -*- coding: utf-8 -*-
"""search 联网对照探针（**手工跑**，故意不叫 test_ 前缀，pytest 不会收集）。

跑法（项目根）：
    venv/Scripts/python tests/search_live_probe.py new A
    venv/Scripts/python tests/search_live_probe.py old B      # old = 改动前的 .bak

为什么留成手工：它依赖 Bing 的真实响应，网络抖一下就红，
放进回归集只会训练出「忽略红灯」的坏习惯。判据与降级语义已由
test_search_relevance.py / test_search_fallback.py 离线钉死。
"""
import os
import sys
import time
import json
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
which = sys.argv[1] if len(sys.argv) > 1 else "new"
setname = sys.argv[2] if len(sys.argv) > 2 else "A"
fname = "search.py" if which == "new" else "search.py.bak_relfix"
M = SourceFileLoader("probe_" + which, os.path.join(ROOT, "agent_tools", fname)).load_module()
print("### 被测 =", which, "|", os.path.basename(M.__file__))
print("### 状态 =", json.dumps(M.get_status(), ensure_ascii=False), flush=True)

A = [("北京 今日天气", False),
     ("latest MCP protocol specification changes 2026", False),
     ("python asyncio TaskGroup best practices", False),
     ("AetherBreath agent learning hub", True)]
B = [("使命召唤 最新消息", False), ("使命召唤 新作 公布", False),
     ("使命召唤：黑色行动7", False), ("使命召唤黑色行动7新赛季", False),
     ("使命召唤2026年新作消息", False)]
for q, pq in {"A": A, "B": B}[setname]:
    t = time.time()
    rs = M.search(q, 5, prefer_quality=pq)
    print("\nQ: %s%s -> %d 条 [%.1fs]" % (q, " (prefer_quality)" if pq else "",
                                          len(rs), time.time() - t), flush=True)
    for r in rs:
        print("   - (%s) %s" % (r.get("source"), r["title"][:58]), flush=True)
        if r.get("source") == "filter":
            print("     ", r["body"][:200], flush=True)
