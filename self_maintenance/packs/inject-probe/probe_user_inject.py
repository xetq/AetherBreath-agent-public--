# 自述：系统提示注入探针——真 import agent.agent 并用 stub logger 真调 load_system_prompt()，
# 断言注入清单与顺序（SOUL -> AGENTS -> USER -> MEMORY，其后是技能/MCP 注册表）、四份各只一次。
# 怎么跑：见同目录 README.md（需要假 LLM 环境变量；顶层要求 key 非空，但不会真发请求）。
# 副作用：会触发技能/MCP 注册表同步 —— 只在内容真变化时写盘（比较不含时间戳）。不改任何生产文件。
import sys, pathlib

ROOT = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "config.yaml").is_file())
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))

import agent as ab          # agent/agent.py


class L:
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): print("[err]", *a)


prompt, injected = ab.load_system_prompt(L())
names = [pathlib.Path(s.split(": ", 1)[1]).name for s in injected if ": " in s]
print("injected names:", names)

FILES = ["SOUL.md", "AGENTS.md", "USER.md", "MEMORY.md"]
heads = {f: (ROOT / "agent_memory" / "long_memory" / f).read_text(encoding="utf-8").strip().split("\n")[0]
         for f in FILES}
pos = {f: prompt.find(heads[f]) for f in FILES}
print("head positions:", pos)
print("prompt chars:", len(prompt))

checks = [
    ("all four injected", all(f in names for f in FILES)),
    ("no dup names", len(names) == len(set(names))),
    ("all heads found", all(p >= 0 for p in pos.values())),
    ("each .md exactly once", all(prompt.count(heads[f]) == 1 for f in FILES)),
    ("order SOUL<AGENTS<USER<MEMORY", all(pos[FILES[i]] < pos[FILES[i + 1]] for i in range(3))),
    ("project info tail", "项目信息" in prompt[-900:]),
]
for name, v in checks:
    print(("PASS " if v else "FAIL ") + name)
print("RESULT:", "PASS" if all(v for _, v in checks) else "FAIL")
