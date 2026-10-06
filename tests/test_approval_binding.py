# -*- coding: utf-8 -*-
"""审批重构回归集：动词与目标绑定 + 后果分区 + 历史误报存档。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_approval_binding.py -q

这个文件的存在理由：2026-09-11 重放 1700+ 条历史真实调用，量出当前引擎的
误报全部来自「共现即判定」——动词和路径在同一份参数里出现过就算数，
于是「提到文件」＝「改动文件」。这里把每一条都钉成用例，防止将来又退回共现。

零绝对路径：盘符与项目根都在运行期取，clone 到任何机器上都能跑。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "agent")]
_tmp = tempfile.mkdtemp(prefix="aether_bind_t_")
os.environ["AETHER_AUDIT_LEDGER"] = os.path.join(_tmp, "ledger.jsonl")
os.environ["AETHER_AUDIT_RULES"] = os.path.join(_tmp, "rules.jsonl")
os.environ["AETHER_AUDIT_MODE"] = "smart"

import approval as A                                  # noqa: E402

# 搬家防呆：只许测生产位置（曾因 sys.path 把部署前快照排在前面而误报"全绿"）
_want = os.path.normcase(os.path.abspath(os.path.join(ROOT, "agent", "approval.py")))
assert os.path.normcase(os.path.abspath(A.__file__)) == _want, "测的不是生产引擎: " + str(A.__file__)

SD = A.drive()                       # 'c:/'
DT = SD.rstrip("/")                  # 'c:'
PRJ = A.unify(A._root())             # 项目根，归一化
LF = chr(10)
FAILS, PASSES = [], []


def ck(name, cond, extra=""):
    (PASSES if cond else FAILS).append(name)
    print("%s %-52s %s" % ("OK  " if cond else "FAIL", name, str(extra)[:66]))


def decide(tool, kw):
    return A.inspect_one(tool, kw, cwd=A._root())


def is_dec(name, tool, kw, want):
    v = decide(tool, kw)
    ck(name, v["decision"] == want, "实得 %s | %s" % (v["decision"], str(v.get("targets"))[:40]))
    return v


# 2026-10：outzone.write 下线后，「项目外写入」在普通模式下不再弹卡。但**绑定纪律**
# 这些用例要守的东西（源文件不算被改动对象 / 只提及不算目标）一条都没变，所以改成
# 在**工作区模式**下断言：那里区外写是硬拒，绑定事实照样可查，而且顺手把新模式钉住。
import permission_modes as PM                          # noqa: E402


def decide_as(mode, tool, kw):
    sid = "bind_probe_" + str(mode)
    PM.set(sid, mode)
    try:
        return A.inspect_one(tool, kw, session_id=sid, cwd=A._root())
    finally:
        PM.forget(sid)


def is_dec_as(mode, name, tool, kw, want):
    v = decide_as(mode, tool, kw)
    ck("[%s] %s -> %s" % (PM.label(mode), name, want), v["decision"] == want,
       "实得 %s | %s" % (v["decision"], str(v.get("targets"))[:40]))
    return v


# ===== 1. 三条真实历史误报：任务与系统盘无关，旧引擎弹了 =====
print("--- 1. 历史误报存档 ---")
is_dec("脚本正文含盘符常量（旧:弹成对盘符写入）", "execute_shell",
       {"command": "cat > work/tmp/x.py <<" + chr(39) + "PY" + chr(39) + LF
        + "SD = '" + SD + "'" + LF + "print(len(SD))" + LF + "PY"}, "pass")
is_dec("链式 .replace()（旧:弹成移动文件）", "execute_python",
       {"code": 'cmd = "a b"' + LF + "y = cmd.lower().replace('a', 'b')" + LF + "print(y)"},
       "pass")
is_dec("只读引擎源码（旧:弹成写 approval.py）", "execute_shell",
       {"command": "grep -c def agent/approval.py 2>/dev/null"}, "pass")

# ===== 2. 绑定纪律：不是动词实参的路径一律不算目标 =====
print("--- 2. 绑定纪律 ---")
# 2026-10：outzone.write 下线，改用**工作区模式**验同一条绑定事实：
# 「源不算被改动对象」的意图不变，只是区外写从「问」变成「拒」。
_cat = is_dec_as(PM.MODE_WORKSPACE,
                 "cat 源 > 工作区外目标：源不算被改动（目的地越界，故拒）", "execute_shell",
                 {"command": "cat " + DT + "/Users/x/src.txt > D:/out/dst.txt"}, "block")
ck("  源文件未被当成被改动对象",
   DT + "/users/x/src.txt" not in [t.lower() for t in (_cat.get("targets") or [])],
   _cat.get("targets"))
v = is_dec_as(PM.MODE_WORKSPACE, "写工作区外路径（D:/logs）硬拒", "execute_shell",
              {"command": "echo x > D:/logs/app.log"}, "block")
is_dec("仅提及 glob 路径、没有写动词：不问", "execute_shell",
       {"command": "echo 报告在 D:/logs/*.log"}, "pass")
is_dec("shell 命令名撞上 py 局部函数 rd()", "execute_python",
       {"code": "def rd(p):" + LF + "    return 1" + LF
        + "rd('" + DT + "/Users/x/bridge.py')"}, "pass")
is_dec("python 裸名 rm() 局部函数不算删命令", "execute_python",
       {"code": "def rm(p):" + LF + "    return p" + LF + "rm('" + DT + "/Users/x/a.txt')"},
       "pass")
is_dec("os.remove 真删除仍要问", "execute_python",
       {"code": "import os" + LF + "os.remove(r'" + DT + "/Users/x/a.txt')"}, "ask")

# ===== 3. 方向：读走的不是写 =====
print("--- 3. 复制/移动的方向 ---")
# 2026-09-11：cp 的**目的地**跨出项目 = 数据离场的第一步（安全审计实测：
# `cp .env D:/…` 全 pass）。改成要问；同时断言源文件仍不被算作被改动对象。
# 2026-10：同上 —— 盘外目的地现在由**工作区模式**硬拒（普通模式放行）。
_cp_out = is_dec_as(PM.MODE_WORKSPACE, "cp 盘内→盘外：硬拒（目的地跨出工作区）", "execute_shell",
                    {"command": "cp " + DT + "/Users/x/secret.txt D:/backup/k.txt"}, "block")
ck("  源文件未被当成被改动对象",
   DT + "/users/x/secret.txt" not in [t.lower() for t in (_cp_out.get("targets") or [])],
   _cp_out.get("targets"))
_mv = is_dec("mv 盘内→盘外：要问（源会被搬走）", "execute_shell",
             {"command": "mv " + DT + "/Users/x/gone.txt D:/backup/k.txt"}, "ask")
if _mv.get("targets"):
    ck("  mv 目标含源文件", DT + "/users/x/gone.txt" in [t.lower() for t in _mv["targets"]],
       _mv["targets"])
_cp_in = is_dec("cp 盘外→盘内：要问（目的地在盘内）", "execute_shell",
                {"command": "cp D:/work/a.txt " + DT + "/Users/x/b.txt"}, "ask")
if _cp_in.get("intent"):
    ck("  措辞不得把源说成被写对象", "secret" not in _cp_in["intent"], _cp_in["intent"][:50])

# ===== 4. pathlib 一族：路径挂在接收者上 =====
print("--- 4. 接收者路径 ---")
is_dec("Path(盘内).unlink() 要问", "execute_python",
       {"code": "import pathlib" + LF + "pathlib.Path(r'" + DT + "/Users/x/u.txt').unlink()"},
       "ask")
is_dec("Path(盘内).write_text() 要问", "execute_python",
       {"code": "import pathlib" + LF + "pathlib.Path(r'" + DT + "/Users/x/w.txt').write_text('hi')"},
       "ask")
is_dec("Path(盘内).read_text() 不问", "execute_python",
       {"code": "import pathlib" + LF + "pathlib.Path(r'" + DT + "/Users/x/r.txt').read_text()"},
       "pass")

# ===== 5. 路径经变量传递：靠绑定，不靠全文回退 =====
print("--- 5. 变量传路径 ---")
is_dec("p=盘内; os.remove(p) 要问", "execute_python",
       {"code": "import os" + LF + "p = r'" + DT + "/Users/x/v.txt'" + LF + "os.remove(p)"},
       "ask")
is_dec("p=盘内; print(p) 不问", "execute_python",
       {"code": "p = r'" + DT + "/Users/x/v.txt'" + LF + "print(p)"}, "pass")

# ===== 6. 引擎自留地：改自己要问，引用不问，且永不 block =====
print("--- 6. 自留地 ---")
v6 = is_dec("写 agent/approval.py 要问", "execute_python",
            {"code": "import io" + LF + "io.open('agent/approval.py', 'w').write('x')"}, "ask")
ck("  自留地不得判 block（否则我修不了自己）", v6["decision"] != "block", v6["decision"])
is_dec("读 agent/approval.py 不问", "execute_python",
       {"code": "import io" + LF + "print(len(io.open('agent/approval.py', encoding='utf-8').read()))"},
       "pass")
is_dec("python 跑自留地文件不算改动", "execute_shell",
       {"command": "python agent/approval.py --help"}, "pass")
is_dec("往账本追加免审规则要问", "execute_shell",
       {"command": "echo '{}' >> agent_logs/approval_rules.jsonl"}, "ask")
is_dec("普通工作区文件不问", "execute_shell",
       {"command": "echo x > agent_workspace/some_note.md"}, "pass")
is_dec("venv 内文件不问", "execute_shell",
       {"command": "echo x > venv/pyvenv.cfg"}, "pass")

# ===== 7. fail-closed：规范内部异常不许静默放行 =====
print("--- 7. 规范异常取向 ---")
class Boom:
    KIND = "boom.spec"; TITLE = "炸掉的规范"; RISK = 2
    def applies(self, ctx):
        return True
    def finding(self, ctx):
        raise ValueError("模拟一个笔误")
_orig = A.specs
A.specs = lambda: [Boom()]
vb = decide("execute_shell", {"command": "echo x > " + DT + "/Windows/probe.txt"})
ck("规范抛异常时必须弹窗而非放行", vb["decision"] == "ask",
   "实得 " + vb["decision"] + " | " + vb.get("reason", "")[:40])
A.specs = _orig
va = decide("execute_shell", {"command": "echo x > " + DT + "/Windows/probe.txt"})
ck("恢复后仍判 ask（对照组）", va["decision"] == "ask", va["decision"])

# ===== 8. 字段穿透：卡片要显示的东西必须活着穿过每一层 =====
print("--- 8. 字段穿透 ---")
REQ_KEYS = {"ask_id", "kind", "risk", "title", "intent", "reason", "notes", "paths",
            "critical", "timeout", "total", "accepts_note", "note_hint",
            "purpose", "reversibility", "user_request", "source_code", "options"}
class Rec:
    def __init__(self):
        self.seen = []
    def request_many(self, reqs):
        self.seen = list(reqs)
        return [A.Decision("E", "answered", "测试")]
_p = A.get_port()
A.set_port(Rec())
dec, gr = A.gate_batch([("tc1", "execute_shell", {"command": "echo x > " + DT + "/Windows/p.txt"})],
                       session_id="t8", cwd=A._root(), user_request="把这段话送到卡片上")
A.set_port(_p)
_sent = Rec()
A.set_port(_sent)
A.gate_batch([("tc1", "execute_shell", {"command": "echo x > " + DT + "/Windows/p.txt"})],
             session_id="t8", cwd=A._root(), user_request="把这段话送到卡片上")
A.set_port(_p)
req = _sent.seen[0] if _sent.seen else {}
missing = sorted(REQ_KEYS - set(req))
ck("引擎送出的卡片字段齐全", not missing, "缺 " + str(missing))
ck("卡片带上了自述用途/可逆性字段", "purpose" in req and "reversibility" in req)
ck("主人原话进了卡片", req.get("user_request", "").startswith("把这段话"))
# 合并卡通道用的是显式白名单，字段一多就漏 —— 这条用例盯住 adapter
ad = os.path.join(ROOT, "agent_webui", "backend", "approval_adapter.py")
if os.path.exists(ad):
    src = open(ad, encoding="utf-8").read()
    ck("合并卡不得手工搬字段（必须整包透传）", '"ask_id": ids[i], **r' in src
       or "dict(r, ask_id" in src, "见 approval_adapter.py 的 items 构造")

print(LF + "===== %d 通过 / %d 失败 =====" % (len(PASSES), len(FAILS)))
for f in FAILS:
    print("  FAIL:", f)


def test_approval_binding():
    assert not FAILS, "失败用例：" + str(FAILS)
