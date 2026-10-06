# -*- coding: utf-8 -*-
"""探针：验证「中期交互已迁 WebUI 侧」这条链在**离线**下是否真的闭合。

怎么跑（项目根）：
    venv/Scripts/python.exe self_maintenance/packs/midturn-hook-probe/probe_midturn_hook.py
期望末行：RESULT: PASS

它做什么（全在**子进程**内，不碰运行中的 AB、不发 HTTP、不调模型）：
  1) import agent（新代码）→ 断言它有通用挂载点，且**不再认识** mid_turn（搬家成功的硬判据）
  2) import mid_turn → 断言它来自 agent_webui/backend（不是 agent/）
  3) 复刻 bridge 的注册动作：register_after_tools_hook(mid_turn.flush_after_tools)
  4) 复刻主循环的一批工具返回：tool 消息写进 conversation 后调 _run_after_tools_hooks
  5) 断言：交代是**独立 user 消息**追加在 tool 消息之后；tool 内容零污染；不重复注入
  6) 断言：坏钩子不把回合带崩，且留下 warning（不静默）
  7) **反向对照**：清空钩子表后同样的投递不进 conversation、信箱保持不动
     —— 直接证明「CLI 无注册者 = 零行为差异」这条设计承诺
副作用：只在探针自己的内存里折腾；不写任何生产文件。
"""
import io
import os
import sys

try:   # 控制台可能是 GBK：agent 顶层会 print emoji（既有行为），bridge 启动时也是这么转的
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "agent"))
sys.path.insert(0, os.path.join(ROOT, "agent_webui", "backend"))
sys.path.insert(0, ROOT)

FAILS = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" | " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


import agent                     # noqa: E402
import mid_turn                  # noqa: E402

check("agent 提供通用挂载点", hasattr(agent, "register_after_tools_hook")
      and hasattr(agent, "_run_after_tools_hooks"))
check("agent 不再认识 mid_turn（搬家硬判据）", not hasattr(agent, "mid_turn"),
      "agent.mid_turn 仍存在" if hasattr(agent, "mid_turn") else "")
check("agent.py 源码不再提及 flush_after_tools",
      "flush_after_tools" not in io.open(os.path.join(ROOT, "agent", "agent.py"), encoding="utf-8").read())
check("mid_turn 来自 WebUI 侧 backend/",
      os.path.basename(os.path.dirname(os.path.abspath(mid_turn.__file__))) == "backend",
      mid_turn.__file__)
check("agent/ 下已无 mid_turn.py",
      not os.path.exists(os.path.join(ROOT, "agent", "mid_turn.py")))


class L:
    def __init__(self):
        self.msgs = []
    def info(self, m, **k):
        self.msgs.append(("info", m))
    def warning(self, m, **k):
        self.msgs.append(("warn", m))


log = L()

# ---- 3) 复刻 bridge 的注册动作 ----
agent.register_after_tools_hook(mid_turn.flush_after_tools)
check("bridge 式注册成功", mid_turn.flush_after_tools in agent._AFTER_TOOLS_HOOKS)
agent.register_after_tools_hook(mid_turn.flush_after_tools)
check("重复注册幂等", agent._AFTER_TOOLS_HOOKS.count(mid_turn.flush_after_tools) == 1)

# ---- 4) 复刻主循环：一批工具返回 ----
mid_turn.BOX.clear()
mid_turn.BOX.push("s-probe", "探针：别动 config.yaml")
conv = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "文件内容：xxx"}]
agent._run_after_tools_hooks(conv, "s-probe", log)

check("交代注入为独立 user 消息",
      len(conv) == 3 and conv[-1]["role"] == "user" and "探针：别动 config.yaml" in conv[-1]["content"])
check("tool 消息零污染", conv[1]["content"] == "文件内容：xxx")
check("信箱已清空", mid_turn.BOX.peek("s-probe") == 0)
check("注入写了日志", any("中期交互" in m for _, m in log.msgs))

agent._run_after_tools_hooks(conv, "s-probe", log)
check("不重复注入（第二次零变化）", len(conv) == 3)

# ---- 6) 坏钩子 ----
def boom(conversation, session_id, log_):
    raise RuntimeError("探针故意炸")


agent.register_after_tools_hook(boom)
reached = False
agent._run_after_tools_hooks([], "s-probe", log)
reached = True
check("坏钩子不把主循环带崩", reached)
check("坏钩子留痕不静默", any("钩子失败" in m for _, m in log.msgs))

# ---- 7) 反向对照：无注册者 = 零行为差异 ----
saved = list(agent._AFTER_TOOLS_HOOKS)
del agent._AFTER_TOOLS_HOOKS[:]
mid_turn.BOX.push("s-probe2", "无钩子时这条不该进对话")
conv2 = []
agent._run_after_tools_hooks(conv2, "s-probe2", log)
check("无注册者时 conversation 不变（CLI 零行为差异）", conv2 == [])
check("无注册者时信箱保持不动（没人取）", mid_turn.BOX.peek("s-probe2") == 1)
agent._AFTER_TOOLS_HOOKS[:] = saved
mid_turn.BOX.clear()

print()
if FAILS:
    print("RESULT: FAIL (%d)" % len(FAILS))
    sys.exit(1)
print("RESULT: PASS")
