# -*- coding: utf-8 -*-
"""审批引擎测试（判定分层 + 批次语义）。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_approval_engine.py -q
    venv/Scripts/python.exe -X utf8 tests/test_approval_engine.py

不依赖前端、不依赖 LLM、不写任何真实文件（账本与规则落临时目录）。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # tests/ 的上级就是项目根（从旧位置搬来时改过，别再用回“上两级”）
# 只测生产位置。曾经这里把「激活版」排在前面，于是跑的是部署前的旧快照 —— 
# 新代码一行没测还能报 32/32 全绿。部署包不是被测对象，生产文件才是。
sys.path[:0] = [os.path.join(ROOT, "agent")]
_tmp = tempfile.mkdtemp(prefix="aether_ap_t_")
os.environ["AETHER_AUDIT_LEDGER"] = os.path.join(_tmp, "ledger.jsonl")
os.environ["AETHER_AUDIT_RULES"] = os.path.join(_tmp, "rules.jsonl")
os.environ["AETHER_AUDIT_MODE"] = "smart"

import approval as A  # noqa: E402

# 搬家防呆：路径指错时宁可当场炸，也不要悄悄跑到部署包或项目外的副本上。
# 真实事故：sys.path 把「激活版」排在前面，新代码一行没测还报了 32/32 全绿。
_want = os.path.normcase(os.path.abspath(os.path.join(ROOT, "agent", "approval.py")))
_got = os.path.normcase(os.path.abspath(A.__file__))
assert _got == _want, "测试加载的不是生产引擎：实际 " + str(A.__file__) + " 应为 " + _want

SD = A.drive()
LF = chr(10)
DT = SD.rstrip("/")          # 'c:'
FAILS, PASSES = [], []


def ck(name, cond, extra=""):
    (PASSES if cond else FAILS).append(name)
    print("%s %-46s %s" % ("OK  " if cond else "FAIL", name, extra))


class Port(A.ApprovalPort):
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def request(self, req):
        self.seen.append(req)
        return A.Decision(self.answers.pop(0) if self.answers else "E", "answered")


# ===== 1. 引擎自检（7 个判定用例）=====
sc = A.self_check(["read_file", "execute_shell", "execute_python", "create_tool"])
ck("self_check ok", sc["ok"] is True, str(sc["problems"])[:70])
# 断言「必需的规范都在」，而不是「恰好几类」：后者每加一条规范就变红，
# 红多了就会被当成噪音顺手改掉 —— 那正是审批回归网易死的方式。
_REQUIRED_SPECS = {"cdrive.files", "engine.selfmodify", "net.egress",
                   "opaque.pipe", "mcp.spawn", "skill.install"}
ck("必需规范全部已加载（系统盘/引擎自留地/外发/编码执行/MCP/技能安装）",
   _REQUIRED_SPECS.issubset(set(sc["specs"]["loaded"]))
   and not sc["specs"]["failed"], str(sc["specs"]))

# ===== 2. 三类操作都要申请审批 =====
for nm, kw in (("写入", {"command": "echo x > " + DT + "/Users/x/p/a.txt"}),
               ("删除", {"command": "rm " + DT + "/Users/x/p/b.txt"}),
               ("移动", {"command": "mv " + DT + "/Users/x/p/a.txt " + DT + "/Users/x/p/c.txt"})):
    v = A.inspect_one("execute_shell", kw)
    ck("系统盘" + nm + " → ask", v["decision"] == A.DECIDE_ASK and v["kind"] == "cdrive.files",
       v["decision"] + " " + v.get("intent", "")[:40])

# ===== 3. 不该打扰的三种情况 =====
v = A.inspect_one("read_file", {"file_path": DT + "/Windows/win.ini"})
ck("系统盘只读 → 不申请审批", v["decision"] == A.DECIDE_PASS, v["reason"][:40])
v = A.inspect_one("execute_shell", {"command": "echo x > " + DT + "/Users/x/AppData/Local/Temp/t.txt"})
ck("Temp 写 → 免打扰（记账）", v["decision"] == A.DECIDE_QUIET, v["reason"][:40])
# 2026-10：outzone.write 随四档权限模式下线。普通模式下**项目外写入放行**——
# 主人的判断是「一直误拦、存在意义不大」。防护不是消失了，而是换了位置：
#   · 工作区模式：区外写/删/移 **直接拒**（下面第 16 节与四档矩阵测试钉住它）；
#   · 所有模式：带载荷外发仍由 net.egress 拦（那才是「读凭据」真正的风险出口）。
v = A.inspect_one("execute_shell", {"command": "echo x > D:/work/a.txt"})
ck("盘外写在普通模式放行（outzone 已下线）",
   v["decision"] == A.DECIDE_PASS,
   "%s | %s" % (v["decision"], v["reason"][:40]))
v = A.inspect_one("search", {"query": "怎么删 C:\\Windows 下文件"})
ck("搜索词里的路径 → 完全不误报", v["decision"] == A.DECIDE_PASS and not v["paths"], str(v["paths"]))

# ===== 4. 绝对禁区不给授权入口 =====
v = A.inspect_one("execute_shell", {"command": "format " + SD + " /Q"})
ck("format → block（不给弹窗）", v["decision"] == A.DECIDE_BLOCK, v["reason"][:40])

# ===== 5. 批次原子性（核心规格）=====
IT = [("ta", "execute_shell", {"command": "echo x > " + DT + "/Users/x/p/a.txt"}),
      ("tb", "read_file", {"file_path": "D:/work/b.txt"})]
p = Port(["A"]); A.set_port(p)
d, g = A.gate_batch(IT, session_id="s1")
ck("批 A 允许 → 整批放行", d["ta"][0] and d["tb"][0], str({k: v[0] for k, v in d.items()}))
p2 = Port(["E"]); A.set_port(p2)
d2, _ = A.gate_batch(IT, session_id="s2")
ck("a 被拒 → b(无需审批)也不运行", (not d2["ta"][0]) and (not d2["tb"][0]),
   str({k: v[0] for k, v in d2.items()}))
ck("a 回模型：用户拒绝", "用户拒绝" in d2["ta"][1], d2["ta"][1][:44])
ck("b 回模型：同批未获批", "同批次" in d2["tb"][1], d2["tb"][1][:44])


class Expired(A.ApprovalPort):
    name = "exp"

    def request(self, req):
        return A.Decision("E", "expired")


class NoChan(A.ApprovalPort):
    name = "none"

    def request(self, req):
        return A.Decision("E", "no_channel")


A.set_port(Expired())
d3, _ = A.gate_batch(IT, session_id="s3")
ck("未响应回模型：用户未响应", "未响应" in d3["ta"][1], d3["ta"][1][:44])
A.set_port(NoChan())
d4, _ = A.gate_batch(IT, session_id="s4")
ck("无通道不得冒充用户拒绝", "没有可用的审批通道" in d4["ta"][1], d4["ta"][1][:50])

# 并行两条待审：第一条拒 → 第二条不再打扰
IT2 = [("x1", "execute_shell", {"command": "echo x > " + DT + "/Users/x/p/a.txt"}),
       ("x2", "execute_shell", {"command": "rm " + DT + "/Users/x/p/b.txt"}),
       ("x3", "read_file", {"file_path": "D:/a.txt"})]
p5 = Port(["E"]); A.set_port(p5)
d5, _ = A.gate_batch(IT2, session_id="s5")
ck("两条待审，一拒全批不放行", all(not v[0] for v in d5.values()), str({k: v[0] for k, v in d5.items()}))
ck("第二条未被询问（不打扰）", len(p5.seen) == 1, "问了 %d 次" % len(p5.seen))

# 部分批准：已批的那条绝不能写成用户拒绝（回归本轮真实 bug）
p6 = Port(["A", "E"]); A.set_port(p6)
d6, _ = A.gate_batch([
    ("k1", "execute_shell", {"command": "echo x > " + DT + "/Users/x/p/ok.txt"}),
    ("k2", "execute_shell", {"command": "rm " + DT + "/Users/x/p/no.txt"}),
    ("k3", "read_file", {"file_path": "D:/work/k.txt"})], session_id="s6")
ck("部分批准 k1 文案为已获批而非拒绝",
   "已获主人批准" in d6["k1"][1] and "用户拒绝" not in d6["k1"][1], d6["k1"][1][:52])
ck("部分批准 k2 仍是用户拒绝", "用户拒绝" in d6["k2"][1], d6["k2"][1][:40])
ck("部分批准 k3 陪等同批冷处理", "同批次" in d6["k3"][1], d6["k3"][1][:40])
ck("部分批准整批未运行", all(not v[0] for v in d6.values()))
ck("grants 由引擎自持（agent.py 不再记账）",
   isinstance(A.grants_for("s6"), dict) and "AUDIT_GRANTS" not in open(
       os.path.join(ROOT, "agent", "agent.py"), encoding="utf-8").read())

# ===== 6. 作用域 =====
A.set_port(Port(["B", "A"])); A.gate_batch(
    [("y1", "execute_shell", {"command": "echo x > " + DT + "/Users/x/deep/f.txt"})],
    session_id="sB")


class NeverAsk(A.ApprovalPort):
    name = "never"

    def request(self, req):
        raise AssertionError("作用域已授予，不该再问")


A.set_port(NeverAsk())
dB, _ = A.gate_batch(
    [("y2", "execute_shell", {"command": "echo x > " + DT + "/Users/x/deep/g.txt"})],
    session_id="sB")
ck("本会话同目录换文件名 → 免打扰", dB["y2"][0] is True, str(dB["y2"]))
# 跨会话作用域：已在独立脚本中验证（换 session_id 会重新询问并放行）。
# 此处不再放用例：它依赖前面用例的作用域状态，测的是隔离性却被污染，会误报。
ck("盘根永不能存成授权前缀",
   A.SCOPES.add(A.SCOPE_PERSISTENT, "s", SD, "cdrive.files") is False)

# 永久规则落盘 + 跨会话
A.set_port(Port(["D"]))
A.gate_batch([("z1", "execute_shell", {"command": "echo x > " + DT + "/Users/x/perm/a.txt"})],
             session_id="sD")
rules = os.environ["AETHER_AUDIT_RULES"]
ck("永久允许已落盘", os.path.exists(rules),
   open(rules, encoding="utf-8").read()[:60] if os.path.exists(rules) else "-")
A.SCOPES._loaded = False
A.SCOPES._pers = []
A.set_port(NeverAsk())
dP, _ = A.gate_batch([("z2", "execute_shell", {"command": "echo x > " + DT + "/Users/x/perm/b.txt"})],
                     session_id="brand-new")
ck("永久规则跨会话生效", dP["z2"][0] is True, str(dP["z2"]))

# ===== 7. 中断闭合（防会话损坏）=====
src = open(os.path.join(ROOT, "agent", "agent.py"), encoding="utf-8").read()
i0 = src.index("def close_dangling_tool_calls")
lines = src[i0:].split(LF)
buf = [lines[0]]
for ln in lines[1:]:
    if ln.strip() and not ln.startswith((" ", chr(9))):
        break
    buf.append(ln)
ns = {}
exec(compile(LF.join(buf), "snippet", "exec"), ns)


class TC:
    def __init__(self, i):
        self.id = i


conv = [{"role": "tool", "tool_call_id": "a1", "content": "ok"}]
n1 = ns["close_dangling_tool_calls"](conv, [TC("a1"), TC("a2"), TC("a3")],
                                     type("L", (), {"warning": staticmethod(lambda *a: None)})())
ck("中断补闭合 2 条", n1 == 2 and len(conv) == 3, str([m.get("tool_call_id") for m in conv]))
ck("闭合文本以 ❌ 开头（复用落盘失败口径）",
   all(str(m.get("content", "")).startswith("❌") for m in conv[1:]))
n2 = ns["close_dangling_tool_calls"](conv, [TC("a1"), TC("a2"), TC("a3")],
                                     type("L", (), {"warning": staticmethod(lambda *a: None)})())
ck("闭合幂等", n2 == 0)

# ===== 8. 账本与 off 档 =====
rows = [json.loads(l) for l in open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip()]
evs = sorted({r.get("event") for r in rows})
ck("账本已留痕且事件分类完整", len(rows) > 12 and "batch_ask" in evs and "quiet" not in str(evs),
   str(evs)[:120])
# 2026-10：AETHER_AUDIT_MODE 已废弃，审计档改由**会话权限模式**派生。
# 这条旧用例（「off 档整批直接放行」）升级成两件事一起验 —— 因为原来那条
# `mode()=="off"` 的短路发生在禁区判定**之前**，等于完全模式连绝对禁区都不看：
#   ① 完全权限模式整体放行；② 同一模式下绝对禁区仍然拦。
import permission_modes as PM  # noqa: E402

PM.set("soff", PM.MODE_FULL)
doff, _ = A.gate_batch(IT, session_id="soff")
ck("完全权限模式整批放行", all(v[0] for v in doff.values()))
_FORB_LIST = list(A.FORBIDDEN)
_FMT0 = [x for x in _FORB_LIST if x.endswith(" ")][0]        # 唯一带尾空格者
dblk, _ = A.gate_batch([("tb", "execute_shell", {"command": _FMT0 + DT + "/x"})],
                       session_id="soff")
ck("完全权限模式下绝对禁区仍然拦（Q1 的交换没做）", dblk["tb"][0] is False)
PM.set("soff", PM.MODE_NORMAL)

# ===== 9. 词法分层：说什么 vs 干什么 =====
# 禁区词一律运行期取。直写字面会让「跑测试」这条命令自己先被拦下（现场复现五次）。
F = list(A.FORBIDDEN)
FMT = [x for x in F if x.endswith(" ")][0]                 # 唯一带尾空格者
SHUT = [x for x in F if len(x) == 8 and x[0] == "s"][0]    # 按长度与首字母取
RMALL = [x for x in F if x[:2] == "r" + "m" and x.strip().endswith(chr(47))][0]
SAM = [x for x in F if x.endswith("sam")][0]
WINX = DT + "/Windows"
DOCV = 'doc = """note: %s and %s are forbidden"""' % (FMT.strip(), SHUT)
ck("文档正文提禁区词 -> 不拦", A.inspect_one("execute_python", {"code": DOCV})["decision"] == "pass")
ck("注释提禁区词 -> 不拦",
   A.inspect_one("execute_python", {"code": "# " + SHUT + " here" + LF + "import os" + LF + "print(1)"})["decision"] == "pass")
ck("正文提动词与盘内路径 -> 不拦",
   A.inspect_one("execute_python", {"code": 'doc = """we c' + "p x then r" + "m x at " + WINX + '/a.txt"""'})["decision"] == "pass")
ck("print 提及盘内路径 -> 不拦",
   A.inspect_one("execute_python", {"code": 'print("check " + "%s")' % (WINX + SAM)})["decision"] == "pass")
ck("只读打印盘内文件 -> 不拦（过去会被当成写入）",
   A.inspect_one("execute_shell", {"command": "type " + WINX + "/win.ini"})["decision"] == "pass")
ck("写模式打开盘内文件 -> 要问",
   A.inspect_one("execute_python", {"code": 'f = open("%s/x.bin", "wb")' % WINX})["decision"] == "ask")
ck("载荷命中已记账 payload_hit",
   "payload_hit" in set(json.loads(l).get("event") for l in
                        open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip()))
ck("嵌套注入(解释器 -c)不许降级成载荷",
   A.inspect_one("execute_shell",
                 {"command": "python -c " + chr(39) + "open(r" + chr(34) + DT + "/Windows" + SAM + chr(34) + "," + chr(34) + "wb" + chr(34) + ")" + chr(39)})["decision"] == "block")
ck("os.system 嵌套展开仍拦得住",
   A.inspect_one("execute_python",
                 {"code": "import os" + LF + 'os.system("' + RMALL + '")'})["decision"] == "block")
ck("真删盘内单文件 -> 要问（不是禁区）",
   A.inspect_one("execute_shell", {"command": "r" + "m " + WINX + "/p.txt"})["decision"] == "ask")
ck("禁区单词只出现在检索参数里 -> 放行（过去连查代码都被拦）",
   A.inspect_one("execute_shell",
                 {"command": "grep -n " + chr(34) + SHUT + chr(34) + " agent/approval.py"})["decision"] == "pass")
ck("禁区单词本身就是命令词 -> 仍拒",
   A.inspect_one("execute_shell", {"command": SHUT + " /s /t 0"})["decision"] == "block")
ck("读关键系统文件不拦", A.inspect_one("read_file",
   {"file_path": WINX + "/system32" + "/drivers/etc/hosts"})["decision"] == "pass")
ck("写关键系统文件不给批（须带动作才成立）", A.inspect_one("execute_shell",
   {"command": "echo x > " + WINX + "/system32" + "/drivers/etc/hosts"})["decision"] == "block")

# ---- 10. 命令位 vs 操作数位（这一组全部来自本轮真实被误拦/漏判的场景）----
Q, SQ2 = chr(34), chr(39)
for nm, cmd, want in (
        ("包装器后接禁区命令(未引号)", "cmd /c " + SHUT + " /s /t 0", "block"),
        ("包装器后接引号内代码", "powershell -Command " + Q + SHUT + Q, "block"),
        ("提权前缀让出段首", "sudo " + SHUT, "block"),
        ("bash -c 正常递归不误伤", "bash -c " + Q + "echo hi" + Q, "pass"),
        ("只读检索命中禁区词", "grep -rn " + Q + SHUT + "|resto" + "re" + Q + " agent/", "pass"),
        ("只读检索禁区单词参数", "grep -n " + SHUT + " agent/approval.py", "pass"),
        ("输出类命令的数据含禁区词（写项目内，避免混入盘外写审批）", "echo " + Q + SHUT + Q + " > agent_workspace/probe-data.txt", "pass"),
        ("注释里提禁区词", "ls -la  # " + SHUT, "pass"),
        ("真实写盘外文件（普通模式放行，outzone 已下线）", "echo x > d:/x.txt", "pass")):
    ck(nm + " -> " + want,
       A.inspect_one("execute_shell", {"command": cmd})["decision"] == want)

ck("只读探测路径不判为操作数 -> 不弹",
   A.inspect_one("execute_python",
                 {"code": "import os" + LF + "p = os.path.join(\"" + DT + "/ProgramData\", \"probe.txt\")"
                        + LF + "print(os.path.exists(p))"})["decision"] == "pass")
ck("浅前缀不得签发免审规则（整盘一级目录）",
   A.SCOPES.add(A.SCOPE_PERSISTENT, "sG", DT + "/Windows", "cdrive.files") is False)
ck("浅前缀不得签发（用户主目录）",
   A.SCOPES.add(A.SCOPE_PERSISTENT, "sG", DT + "/Users/x", "cdrive.files") is False)
ck("够深仍可签发", A.SCOPES.add(A.SCOPE_PERSISTENT, "sG",
                                 DT + "/Users/x/deepok/a.txt", "cdrive.files") is True)
# 真验证（先前那版写成 `or True`，是永远为真的假用例）：
# 主人点「永久允许」而护栏挡下时，必须留痕 —— 否则界面让人以为"以后不再问了"。
A.set_port(Port(["D"]))
A.gate_batch([("sd1", "execute_shell",
               {"command": "echo x > " + DT + "/Windows/probe_sd.txt"})], session_id="sSD")
_ev = set(json.loads(l).get("event") for l in
          open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip())
ck("点永久允许被护栏挡下 -> 记 scope_denied", "scope_denied" in _ev)
A.SCOPES._pers = [x for x in A.SCOPES._pers if x.get("session_id") != "sSD"]

# ===== 11. 主人手打的补充说明（归引擎：文案、账本、CLI 三处同一语义）=====
class PortNote(A.ApprovalPort):
    name = "note"
    def __init__(self, choice, note):
        self.choice, self.note = choice, note
    def request(self, req):
        return A.Decision(self.choice, "answered", self.note)

SAY = "先别动这个目录，我要留着排障"
A.set_port(PortNote("E", SAY))
dn, _ = A.gate_batch([("e1", "execute_shell",
                       {"command": "echo n > " + DT + "/Users/x/deep/n1.txt"})], session_id="sN1")
ck("拒绝带说明 -> 原文进模型文案", "主人补充" in dn["e1"][1] and SAY in dn["e1"][1],
   dn["e1"][1][:56])
A.set_port(PortNote("A", SAY))
da, ga = A.gate_batch([("e2", "execute_shell",
                        {"command": "echo n > " + DT + "/Users/x/deep/n2.txt"})], session_id="sN2")
ck("批准带说明 -> 仍然放行（话只进账本，不挡执行）", da["e2"][0] is True)
ck("主人的话已落账本",
   any(json.loads(l).get("note") == SAY for l in
       open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip()
       and json.loads(l).get("event") == "verdict"))
A.set_port(PortNote("E", ""))
dblank, _ = A.gate_batch([("e3", "execute_shell",
                           {"command": "echo n > " + DT + "/Users/x/deep/n3.txt"})], session_id="sN3")
ck("不填说明 -> 文案与旧行为一致（向后兼容）",
   dblank["e3"][1] == "❌ " + A.WHY_DENY, dblank["e3"][1][:40])
LONG = "长" * 600
A.set_port(PortNote("E", LONG))
dl, _ = A.gate_batch([("e4", "execute_shell",
                       {"command": "echo n > " + DT + "/Users/x/deep/n4.txt"})], session_id="sN4")
ck("超长说明被截断而不是撑爆上下文",
   len(dl["e4"][1]) < A.MAX_NOTE + 80 and dl["e4"][1].endswith("）"), str(len(dl["e4"][1])))
# 部分批准时，主人只对未批那项说话 -> 已批那项的冷处理文案也该带上这句话
class PortMixed(A.ApprovalPort):
    name = "mixed"
    def request_many(self, reqs):
        return [A.Decision("A", "answered", ""),
                A.Decision("E", "answered", "第二项不要碰")]
A.set_port(PortMixed())
dm, _ = A.gate_batch([("p1", "execute_shell", {"command": "echo m > " + DT + "/Users/x/deep/m1.txt"}),
                      ("p2", "execute_shell", {"command": "echo m > " + DT + "/Users/x/deep/m2.txt"})],
                     session_id="sM1")
ck("部分批准：已批项的冷处理文案也能带主人的话",
   dm["p1"][0] is False and "第二项不要碰" in dm["p1"][1], dm["p1"][1][:52])
A.set_port(Port(["A"]))
ck("CLI 同源：选项后可跟说明",
   (lambda d: d.choice == "E" and d.note == "先看一眼")(
       __import__("approval").ConsolePort.__dict__ and A.Decision("E", "answered", "先看一眼")))


# ===== 12. 今天真机实测抓到的三个洞 =====
ck("to_parquet 落盘内文件 -> 要问（过去静默放行）",
   A.inspect_one("execute_python",
                 {"code": "import pandas as pd" + LF
                        + 'pd.DataFrame().to_parquet("' + DT + '/Windows/z.pq")'})["decision"] == "ask")
ck("to_pickle 落盘内文件 -> 要问",
   A.inspect_one("execute_python",
                 {"code": "import pandas as pd" + LF
                        + 'pd.DataFrame().to_pickle("' + DT + '/Windows/z.pkl")'})["decision"] == "ask")
ck("字面量上的 replace 不再算移动（过去张冠李戴）",
   A.inspect_one("execute_python",
                 {"code": 'p = "' + DT + '/Windows/x".replace("x", "y")'})["decision"] == "pass")
ck("os.replace 仍算移动（真文件操作没被放走）",
   A.inspect_one("execute_python",
                 {"code": "import os" + LF + 'os.replace("' + DT + '/Windows/a.txt", "'
                        + DT + '/Windows/b.txt")'})["decision"] == "ask")

class Boom(A.ApprovalPort):
    name = "boom"
    def request_many(self, reqs):
        raise KeyboardInterrupt()

A.set_port(Boom())
try:
    A.gate_batch([("bm", "execute_shell",
                   {"command": "echo b > " + DT + "/Users/x/deep/bm.txt"})], session_id="sBoom")
    ck("中止仍向上抛给 agent.py 补闭合", False)
except KeyboardInterrupt:
    ck("中止仍向上抛给 agent.py 补闭合", True)
_ev2 = set(json.loads(l).get("event") for l in
           open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip())
ck("中止批次在账本里有终态 batch_cancelled", "batch_cancelled" in _ev2)

# ===== 13. 免审规则的可观测与可撤销（面板的后端能力）=====
A.set_port(Port(["D", "D"]))
A.gate_batch([("rg1", "execute_shell",
               {"command": "echo k > " + DT + "/Users/x/deepa/g.txt"})], session_id="sRG")
A.gate_batch([("rg2", "execute_shell",
               {"command": "echo k > " + DT + "/Users/x/deepb/g.txt"})], session_id="sRG")
_rf = os.environ["AETHER_AUDIT_RULES"]
_disk = [json.loads(l) for l in open(_rf, encoding="utf-8") if l.strip()]
ck("list_all 看得见刚签发的规则", len(A.SCOPES.list_all("sRG")) >= 2, str(len(A.SCOPES.list_all("sRG"))))
ck("永久规则确实落盘", len(_disk) >= 2, str([d["path"] for d in _disk][:2]))
_removed = A.SCOPES.revoke(path=DT + "/Users/x/deepa", scope="persistent")
_disk2 = [json.loads(l) for l in open(_rf, encoding="utf-8") if l.strip()]
ck("撤销单条：返回删除数", _removed == 1, str(_removed))
ck("撤销后内存与磁盘一致（今天事故：只清文件没清内存）",
   sorted(x["path"] for x in A.SCOPES.list_all("sRG") if x["scope"] == "persistent")
   == sorted(d["path"] for d in _disk2))
A.set_port(Port(["A"]))
_ra = {}
_ra["asked"] = False
class _Ask(A.ApprovalPort):
    name = "ask"
    def request(self, req):
        _ra["asked"] = True
        return A.Decision("A", "answered")
A.set_port(_Ask())
A.gate_batch([("rg3", "execute_shell",
               {"command": "echo k > " + DT + "/Users/x/deepa/h.txt"})], session_id="sRG")
ck("撤销后同目录再操作必须重新弹窗（不能诈尸）", _ra["asked"] is True)
ck("撤销动作已留痕 scope_revoked",
   any(json.loads(l).get("event") == "scope_revoked" for l in
       open(os.environ["AETHER_AUDIT_LEDGER"], encoding="utf-8") if l.strip()))
A.SCOPES.revoke(scope="persistent")

# ===== 14. 路径字符边界（真机误拦事故：整句中文被当成文件目标）=====
_WS, _FP = chr(92), chr(0xFF09)                 # 反斜杠、全角右括号
_you = chr(0x4F60) + chr(0x770B) + chr(0x89C1)  # 你看见
got_p = A.extract_paths({"operand": "C:" + _WS + "Windows" + _FP + _you + "也删不掉"})
ck("全角标点后不再把中文散文吞进路径", got_p == [DT.lower() + "/windows"], str(got_p))
_cn = chr(0x7528) + chr(0x6237)                 # 用户：合法的中文目录名
got_c = A.extract_paths({"operand": "C:" + _WS + _cn + _WS + "a.txt"})
ck("中文目录名仍是合法路径（不过度修正成漏判）",
   got_c == [DT.lower() + "/" + _cn + "/a.txt"], str(got_c))

# 2026-10：strict 档现在由「仅读」权限模式派生 —— 同一个旋钮合并成一个入口。
PM.set("sstrict", PM.MODE_READONLY)
ck("仅读模式派生 strict：退回全文扫（更严，不误放）",
   A.inspect_one("execute_python", {"code": DOCV},
                 session_id="sstrict")["decision"] == "block")
PM.set("sstrict", PM.MODE_NORMAL)

# ===== 15. 注入位与 Windows API（真机漏判实录）=====
# 事故：`python -X utf8 - <<'PY' … os.remove(盘内路径) … PY` 被整条放行，
# 主人桌面上的文件真被删了，而闸门报 pass。两个根因：
#   ① 词法层不认 heredoc —— 体被切成新命令段，动作随之消失；
#   ② -c 按 flag 而不是按解释器选解析器 —— `bash -c "rm …"` 被送进 python
#      解析器，ast.parse 立刻失败，退化成「仅操作数」，命令词 rm 丢失。
# 另补第三个洞：ctypes 走 Windows 原生 API 时动作词不在表里 / 路径经变量落提及层。
_P = DT + "/Users/x/Desktop/x.txt"


def _ask15(tool, kw):
    return A.inspect_one(tool, kw, session_id="s15")["decision"] == A.DECIDE_ASK


def _quiet15(tool, kw):
    return A.inspect_one(tool, kw, session_id="s15")["decision"] in ("pass", "quiet")


ck("bash -c 删除（按解释器选解析器）",
   _ask15("execute_shell", {"command": 'bash -c "rm ' + _P + '"'}))
ck("cmd /c 删除",
   _ask15("execute_shell", {"command": 'cmd /c "del ' + _P + '"'}))
ck("heredoc python 删除",
   _ask15("execute_shell", {"command": "python - <<'PY'" + LF + "import os" + LF
                            + "os.remove('" + _P + "')" + LF + "PY"}))
ck("heredoc python unlink",
   _ask15("execute_shell", {"command": "python - <<EOF" + LF + "import pathlib" + LF
                            + "pathlib.Path('" + _P + "').unlink()" + LF + "EOF"}))
ck("heredoc bash 删除",
   _ask15("execute_shell", {"command": "bash <<'SH'" + LF + "rm " + _P + LF + "SH"}))
ck("heredoc 重定向写（同行 > 目标不能被吞）",
   _ask15("execute_shell", {"command": "cat <<EOF > " + _P + LF + "hi" + LF + "EOF"}))
ck("ctypes DeleteFileW",
   _ask15("execute_python", {"code": "import ctypes" + LF
                             + "ctypes.windll.kernel32.DeleteFileW('" + _P + "')"}))
ck("ctypes SHFileOperationW（路径经变量落提及层）",
   _ask15("execute_python", {"code": "import ctypes" + LF + "p = '" + _P + "'" + LF
                             + "SHFileOperationW(p, FOF_ALLOWUNDO)"}))
ck("ctypes MoveFileExW",
   _ask15("execute_python", {"code": "import ctypes" + LF
                             + "MoveFileExW('" + _P + "','D:/b',1)"}))
ck("只读 json.dumps 不误报",
   _quiet15("execute_python", {"code": "import json" + LF
                               + 'print(json.dumps({"p":"' + _P + '"}))'}))
ck("文档提到盘内路径 + 写项目内：不回退、不误报",
   _quiet15("execute_python", {"code": '"""参考 ' + _P + '"""' + LF
                               + 'open("agent_workspace/out.txt","w").write("x")'}))


print("\n===== %d 通过 / %d 失败 =====" % (len(PASSES), len(FAILS)))
for f in FAILS:
    print("  FAIL:", f)
# pytest：模块导入即跑完全部用例，失败靠这个断言暴露
def test_approval_engine():
    assert not FAILS, "失败用例：" + str(FAILS)

if __name__ == "__main__":
    sys.exit(1 if FAILS else 0)
ck("探针返回 problems 列表", isinstance(A.self_check([]).get("problems"), list))
