# -*- coding: utf-8 -*-
"""approval.py —— 审批引擎（与 agent.py 平级）。

流程（唯一职责：决定"请求能不能传到任务编排器"）
------------------------------------------------
    agent.py 解析完一批 tool_call
      -> gate_batch(items, session_id)
           每项按 approvals/ 里的规范判定：不需批 / 免打扰 / 需人批 / 禁区
           需人批的一律**整批一起问**，全部通过才放行
      -> 通过的原样进 parsed_calls（编排器该怎么串行/并行完全不受影响）
         未通过的补一条 role=tool 响应，把原因交还模型判断

批次原子性（这是设计要点，不是便利）
------------------------------------
同批 a、b 并行时，若 a 未获批，b 也**不得运行**。理由：模型把 a、b 放在同一批，
说明它认为这组动作是**一个意图的整体**；只执行一半可能留下比全不执行更糟的中间态
（例如"移动配置 + 改注册表引用"只做前一步）。审批因此必须按批结算。

失效模式一律 fail-closed：无审批通道 / 超时 / 通道异常 -> 拒绝，且三种情况
返回给模型的文本各不相同（模型需要知道"主人没看到"和"主人说了不行"是两回事）。

环境变量（AETHER_ 前缀）
  AETHER_AUDIT_MODE         **已废弃**（2026-10）。审计档现由会话的权限模式派生：
                            仅读->strict / 工作区->smart / 普通->smart / 完全->off。
                            用户入口是 WebUI 发送框左侧的模式下拉（会话级，见
                            permission_modes）。原来的 `off` 是一条「改一行 .env
                            就关掉整道闸门」的后门，随这次改动一并封掉。
  AETHER_AUDIT_ON_ERROR     deny(默认) | allow     引擎自身异常时的取向。
                            曾经默认 allow，结果一个笔误（用了不存在的常量）就让
                            所有该弹窗的操作静默放行且表现完全正常 —— 审批在最需要
                            它的时刻消失。宁可拦住并明写原因，也不静默放过。
  AETHER_APPROVAL_TIMEOUT   等待人类裁决秒数，默认 300
  AETHER_AUDIT_LEDGER       账本，默认 <根>/agent_logs/approval-<YYYYMM>.jsonl
  AETHER_AUDIT_RULES        永久规则，默认 <根>/agent_logs/approval_rules.jsonl
  AETHER_AUDIT_SYSTEMDRIVE  覆盖系统盘判定（默认取 SystemDrive，绝不硬编码 C）
  AETHER_AUDIT_ALLOWLIST    追加密钥分隔的免打扰前缀
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 词法角色切分（approval_lex）。缺失只影响精度，不影响可用性：
# 拿不到它就退回全文扫，那条路径由 self_check 的 lex_layer 字段报出来。
try:
    import approval_lex as _LEX
except Exception:
    _LEX = None

# 会话级权限模式（仅读 / 工作区 / 普通 / 完全）。这里**裸导入、不吞异常**：
# 它是核心策略，缺了它整道闸门会静默退回「普通」而没人知道 —— 那正是
# approvals 包曾经 fail-open 的老路（见 _RulesUnavailable 的由来），不重演。
import permission_modes

# ============================================================
# 0. 常量
# ============================================================
DECIDE_PASS = "pass"         # 不需审批
DECIDE_QUIET = "quiet"       # 免打扰（记账）
DECIDE_ASK = "ask"           # 需要人批
DECIDE_BLOCK = "block"       # 绝对禁区：不给批

SCOPE_ONCE = "once"
SCOPE_SESSION = "session"
SCOPE_PERSISTENT = "persistent"

# 回给模型的三种未通过原因（第 3 条规格：措辞必须可区分）
WHY_DENY = "本次操作需审批，用户拒绝"
WHY_NO_REPLY = "本次操作需审批，用户未响应（超时）"
WHY_NO_CHANNEL = "本次操作需审批，但当前环境没有可用的审批通道"
WHY_BATCH_COLD = "同批次内有其它操作未获批，本批全部未执行"
WHY_APPROVED_COLD = "本项已获主人批准，但同批次内另有操作未获批准，因此本批一个都未执行。请重新规划：可只提交已获批准的操作，或向主人说明为什么需要被拒的那几项。"

# 绝对禁区：已知必然灾难。不申请审批、不给放行按钮 —— 手滑一次就没有终端了
FORBIDDEN: Tuple[str, ...] = (
    "format ", "mkfs", "diskpart", "clean disk", "dd if=", "cipher /w",
    "rm -rf /", "rm -rf c:/", "rd /s /q c:\\", "del /f /s /q c:\\",
    "remove-item -force -recurse c:\\", "bcdedit", "vssadmin delete",
    "\\system32\\config\\sam", "\\system32\\drivers\\etc\\hosts",
    "shutdown", "reboot", "stop-computer",
)

# 按形态自动分三档，判法不同（手维护三份表迟早漏一条）：
#   PATH   路径片段型：不是命令位能表达的，交给规范层带动作判定 —— 读它合法，写删才致命
#   CMD    单词型：只与命令词精确等值，绝不做子串（否则连查日志都能撞上）
#   PHRASE 短语型：只匹配「从命令词起算」的段文本前缀
BS = chr(92)
EXE_SUFFIX = (".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".msc")
FORBIDDEN_PATH = tuple(x for x in FORBIDDEN if x.startswith(BS))
FORBIDDEN_CMD = tuple(x for x in FORBIDDEN
                      if not x.startswith(BS) and " " not in x.strip() and not x.endswith(" "))
FORBIDDEN_PHRASE = tuple(x for x in FORBIDDEN
                         if x not in FORBIDDEN_PATH and x not in FORBIDDEN_CMD)

SKIP_ARG_NAMES = ("timeout", "top_k", "max_results", "lines", "limit", "depth",
                  "verbose", "logger", "headless", "channel", "model", "force",
                  "record_source", "since", "expression", "type", "mode")
# 不碰本地盘的工具：参数里出现路径字样只是文本（搜索词/URL/提问），扫它们必然误报——
# 而被误报烦到闭眼点「允许」，比没有审批更危险。
#
# 2026-09-17 对账修正（审计 L1-B7）：这份名单里曾写着 `multi_search` 与 `web_reader`，
# 而**注册表里根本没有这两个名字**（multi_search 早已随 search 重构删除；抓网页的工具
# 真名是 `fetch_url`）。也就是说这两条豁免声明从未生效，名单读起来却像"已经覆盖"。
# 现在补上 mcp_search（只读 station 文件夹），并去掉两个失效名字。守护这份名单的是
# self_check 的「豁免名单无失效名字」一项 —— 以后写错名字会当场报警，不再静默。
NON_FS_TOOLS = frozenset({"search", "fetch_url", "web_extract", "rag_query",
                          "time_weather", "calculator", "mcp_search"})


PATH_ARG_HINTS = ("path", "file", "dir", "cwd", "workdir", "target", "dest",
                  "output", "input", "filename", "folder")

_RE_QUOTED = re.compile(r"""['"]([^'"\n]{2,300})['"]""")
# 路径的停止符集合。汉字**不算**停止符：本项目自己就有中文目录名，
# 把它当边界会把真路径截断成漏判。真正要挡的是 CJK 标点与全角符号 ——
# 实测事故：一句 commit 正文里的「C:\Windows）你既看不见…」被整段当成
# 文件目标弹了审批，而主人真给它批了。误拦的代价是训练人闭眼点批准。
_PATH_STOP = ''.join(sorted(set(
    [chr(9), chr(10), chr(13), chr(32), chr(34), chr(39), chr(96),
     chr(59), chr(124), chr(38), chr(40), chr(41), chr(60), chr(62)]
    + [chr(c) for c in range(0x3000, 0x3040)]     # 。、《》「」等 CJK 符号
    + [chr(c) for c in range(0xff00, 0xfff0)]     # ！？＃（）％ 等全角形式
)))
_RE_WIN = re.compile("[A-Za-z]:[" + chr(92) + chr(92) + "/][^"
                     + re.escape(_PATH_STOP) + "]*")
_RE_STOP = re.compile("[" + re.escape(_PATH_STOP) + "]")
_RE_POSIX = re.compile(r"(?<![\w])(/[a-f])(?=[\\/])", re.I)
_RE_ENV = re.compile(r"(?:%|\$env:)(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)(?:%|\b)([^\s\"'`;|&)<>\r\n]*)", re.I)


# ---- shell 家目录引用的展开（判据必须与解释器同源）----
# 实测事故（2026-09-11 安全审计 probe5，10/10 漏判）：审批只认字面路径，下面这些
# 写法在旧版里全是 pass，而它们在 bash/python 里**真能执行**：
#     printf x > $USERPROFILE/Desktop/leak.txt
#     printf x > ~/Desktop/leak.txt
#     open(os.path.expanduser('~/Desktop/leak.txt'),'w')
#     open(os.environ['USERPROFILE']+'/Desktop/leak.txt','w')
# 不展开它们，「系统盘写要审批」这条规则只要多打一个 $ 或 ~ 就能绕过。
# 注：_RE_ENV 只认 %VAR% 与 $env:VAR（cmd/PowerShell 形态），shell 的 $VAR 不在其中。
_RE_SHVAR = re.compile(
    r"\$\{?(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)\}?",
    re.I)
# cmd/PowerShell 形态的同一个变量：%USERPROFILE%\…
_RE_CMDVAR = re.compile(
    r"%(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)%",
    re.I)
_RE_TILDE = re.compile(r"(?<![\w~])~(?=[\\/]|$)")
# 本机管理共享：\\localhost\c$\… 与 c:\… 是同一个位置
_RE_UNC_LOCAL = re.compile(r"^//(?:localhost|127\.0\.0\.1|\[::1\])/([a-z])\$(?:/(.*))?$")


def _expand_shell_env(text: str, droot: str) -> str:
    """把 shell 风格的家目录引用就地展开成绝对路径文本（只展开已知变量）。

    认不出的变量原样留着：凭空造路径只会制造误报，而误报会把主人训练成
    闭眼点「允许」—— 那比没有审批更坏（本项目的既有取向）。
    """
    if not text or ("$" not in text and "~" not in text and "%" not in text):
        return text
    user = (os.environ.get("USERPROFILE") or os.environ.get("HOME")
            or (droot.rstrip("/") + "/Users/user"))
    windir = os.environ.get("WINDIR") or (droot.rstrip("/") + "/Windows")
    tmp = (os.environ.get("TEMP") or os.environ.get("TMP")
           or (user + "/AppData/Local/Temp"))
    table = {
        "systemdrive": droot.rstrip("/"), "homedrive": droot.rstrip("/"),
        "systemroot": windir, "windir": windir,
        "userprofile": user, "home": user, "temp": tmp, "tmp": tmp,
    }
    out = _RE_SHVAR.sub(lambda m: table.get(m.group(1).lower(), m.group(0)), text)
    out = _RE_CMDVAR.sub(lambda m: table.get(m.group(1).lower(), m.group(0)), out)
    return _RE_TILDE.sub(lambda m: user, out)


# ============================================================
# 1. 配置
# ============================================================
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def mode(session_id: str = "") -> str:
    """审计档（off / smart / strict），现在由该会话的**权限模式派生**。

    2026-10：新增四档会话级权限模式后，`AETHER_AUDIT_MODE` 不再是用户入口 ——
    它降级为派生值（见 permission_modes.AUDIT_MODE）。留着「读 .env 就能关掉
    整道闸门」那条路，等于给 engine.selfmodify 里记录的那个后门发永久通行证。
    """
    return permission_modes.audit_mode(permission_modes.get(session_id))


def timeout_sec() -> int:
    try:
        return max(5, min(3600, int(_env("AETHER_APPROVAL_TIMEOUT", "300"))))
    except ValueError:
        return 300


def drive() -> str:
    """系统盘根，形如 'c:/'。默认读 SystemDrive，绝不硬编码。"""
    raw = _env("AETHER_AUDIT_SYSTEMDRIVE") or os.environ.get("SystemDrive") or "C:"
    raw = raw.strip().rstrip("\\/")
    raw = raw[:2] if len(raw) >= 2 and raw[1] == ":" else "c:"
    return raw.lower() + "/"


def ledger_path() -> str:
    return _env("AETHER_AUDIT_LEDGER") or os.path.join(
        _root(), "agent_logs", "approval-%s.jsonl" % time.strftime("%Y%m"))


def rules_path() -> str:
    return _env("AETHER_AUDIT_RULES") or os.path.join(
        _root(), "agent_logs", "approval_rules.jsonl")


# ============================================================
# 2. 路径归一与提取
# ============================================================
def unify(s: str, cwd: str = "") -> str:
    """小写盘符、正斜杠、无尾斜杠、尽量绝对化。

    归一**之前**先展开 shell 家目录引用（`$USERPROFILE/…`、`~/…`）：
    判据必须与解释器同源 —— 多打一个 `$` 不该让同一个位置变成两个结论。
    放在这里，是因为所有路径都经此函数（规范层取目标走 `resolve()` → 本函数）。
    """
    if not s:
        return ""
    t = _expand_shell_env(str(s).strip().strip('"').strip("'"), drive())
    if not t:
        return ""
    if t.startswith("\\\\?\\") or t.startswith("//?/"):
        t = t[4:]
    t = "/".join(t.split("\\"))
    low = t.lower()
    if len(low) >= 2 and low[1] == ":":
        t = low
    else:
        m = re.match(r"^/+([a-f])(?:/(.*|))$", low)
        if m:
            t = m.group(1) + ":/" + (m.group(2) or "")          # git-bash 的 /c/...
        elif low.startswith("//"):
            m_unc = _RE_UNC_LOCAL.match(low)
            if m_unc:
                # 本机管理共享 \\localhost\c$\x 与 c:\x 是同一个文件。实测该写法
                # 能真写系统盘，而旧归一把它当「网络路径」整条放过。
                t = "%s:/%s" % (m_unc.group(1), m_unc.group(2) or "")
            else:
                return low.strip("/")
        elif not low.startswith("/"):
            try:
                t = "/".join(os.path.abspath(os.path.join(cwd or os.getcwd(), t)).split("\\"))
            except Exception:
                t = low
        else:
            t = "/" + low.lstrip("/")
    while len(t) > 3 and t.endswith("/"):
        t = t[:-1]
    return t


def expand_env(token: str, droot: str) -> List[str]:
    win = droot.rstrip("/")
    user = os.environ.get("USERPROFILE") or os.environ.get("HOME") or (win + "/Users/user")
    windir = os.environ.get("WINDIR") or (win + "/Windows")
    temp = os.environ.get("TEMP") or (user + "/AppData/Local/Temp")
    table = {"systemdrive": win + "/", "homedrive": win + "/", "systemroot": windir,
             "windir": windir, "userprofile": user, "home": user, "temp": temp, "tmp": temp}
    low = token.lower()
    out: List[str] = []
    for name, val in table.items():
        for pat in ("%" + name + "%", "$env:" + name):
            idx = low.find(pat)
            if idx < 0:
                continue
            rest = token[idx + len(pat):]
            m = _RE_STOP.search(rest)
            if m:
                rest = rest[:m.start()]
            base = (val or "").rstrip("/")
            if not base:
                continue
            out.append(base)
            if rest:
                out.append(base + "/" + rest.lstrip("\\/"))
    return out


def extract_paths(kwargs: Dict[str, Any], cwd: str = "") -> List[str]:
    """扫**全部**字符串参数（不按名字表白名单），新工具/新参数自动被覆盖。"""
    cands: List[str] = []
    for slot, val in (kwargs or {}).items():
        if val is None or slot in SKIP_ARG_NAMES or isinstance(val, bool):
            continue
        items = ([str(x) for x in val.values()] if isinstance(val, dict)
                 else [str(x) for x in val] if isinstance(val, (list, tuple))
                 else [str(val)])
        whole = any(h in slot.lower() for h in PATH_ARG_HINTS)
        for text in items:
            text = text.strip()
            if not text:
                continue
            if whole and chr(10) not in text:
                cands.append(text)
            for q in _RE_QUOTED.findall(text):
                qs = q.strip()
                if re.match(r"^[A-Za-z]:[\\/]", qs) or qs.startswith("//"):
                    cands.append(qs)
            cands.extend(_RE_WIN.findall(text))
            for m in _RE_POSIX.finditer(text):
                rest = text[m.end():]
                stop = _RE_STOP.search(rest)
                cands.append(m.group(1) + (rest[:stop.start()] if stop else rest))
            for mm in _RE_ENV.finditer(text):
                cands.extend(expand_env(mm.group(0), drive()))
    seen, out = set(), []
    for c in cands:
        u = unify(c, cwd)
        if u and len(u) > 2 and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_path(text: str, cwd: str = "") -> str:
    """给规范层用的路径归一：实参文本 -> 绝对路径。

    归一规则只有一处（unify），规范层不重复实现 —— 两处规则迟早分叉。
    """
    return unify(str(text or ""), cwd or _root())


def parent_of(p: str) -> str:
    base = p.rstrip("/").rsplit("/", 1)
    return base[0] if base and base[0] else p


def is_drive_root(p: str) -> bool:
    """盘根判定：绝不允许把整盘存成授权前缀（点一下=授权全盘是陷阱）。"""
    return not p or p.rstrip("/") + "/" == drive()


# 授权前缀在盘符之后至少要有几段。事故实据（账本 16:15）：主人点了一次「永久允许」，
# 目标文件位于用户主目录，父目录即 c:/users/xxx —— 桌面、文档、下载从此全部免审；
# 同一批还签发了 c:/Windows。免打扰的便利换来一次点击拆掉审批门，不划算。
MIN_SCOPE_SEGMENTS = 3


def scope_depth_ok(prefix: str) -> bool:
    """前缀够不够深。太浅等于把整棵子树交出去，一律不签发。"""
    pu = unify(prefix)
    if not pu or is_drive_root(pu):
        return False
    parts = pu.split(":", 1)
    segs = [x for x in (parts[1] if len(parts) > 1 else parts[0]).split("/") if x]
    return len(segs) >= MIN_SCOPE_SEGMENTS


# ============================================================
# 3. 审批规范装载（agent/approvals/）
# ============================================================
_SPEC_STATE: Dict[str, Any] = {"loaded": [], "failed": []}


class _RulesUnavailable:
    """规则包不可用时的哨兵：把「读不到规则」变成「必须人工确认」，而不是静默放行。

    背景（实测）：specs() 原先在加载失败时 return []，而 inspect_one 把
    「没有任何规范命中」当成 pass —— 于是 approvals 包一坏，整道闸门静默失效。
    引擎在别处都是 fail-closed（spec_error 强制 ask、AETHER_AUDIT_ON_ERROR 默认 deny），
    唯独这条路径是 fail-open。
    """
    KIND = "engine.rules_unavailable"
    TITLE = "审批规则包不可用"
    RISK = 3

    def applies(self, ctx):
        return True

    def finding(self, ctx):
        why = (_SPEC_STATE.get("failed") or ["未知"])[0]
        return {
            "quiet": False,
            "action": "无法审计",
            "targets": ["（规则包未加载）"],
            "intent": "审批规则包未能加载，本次操作无法判定",
            "reason": "审批规则包加载失败：%s" % why,
            "notes": ["⚠️ 这是闸门自身的故障，不是这次操作有问题 —— "
                      "批准前建议先修规则包，否则同一处会持续失效"],
            "critical": True,
        }


def specs():
    try:
        import approvals                      # noqa: PLC0415
        found = approvals.load_specs()
        _SPEC_STATE["loaded"] = [getattr(s, "KIND", "?") for s in found]
        _SPEC_STATE["failed"] = list(getattr(approvals, "SPECS_LOAD_ERRORS", []))
        return found
    except Exception as e:
        _SPEC_STATE["failed"] = ["approvals 包不可用: %s: %s" % (e.__class__.__name__, e)]
        _SPEC_STATE["loaded"] = []
        # 返回哨兵而不是空列表：空列表会被 inspect_one 当成「没有规范要审」→ pass。
        return [_RulesUnavailable()]


def specs_state() -> Dict[str, Any]:
    specs()
    return dict(_SPEC_STATE)


# ============================================================
# 4. 作用域（once / session / persistent）
# ============================================================
class Scopes:
    """锁纪律：本类方法内**只操作内存字典与文件**，绝不调用审批通道（防自死锁）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sess: Dict[str, List[Dict[str, Any]]] = {}
        self._pers: List[Dict[str, Any]] = []
        self._loaded = False

    def add(self, scope: str, session_id: str, prefix: str, kind: str,
            ask_id: str = "") -> bool:
        pu = unify(prefix)
        parent = parent_of(pu)
        if is_drive_root(pu) or is_drive_root(parent) or not scope_depth_ok(parent):
            return False
        rule = {"path": parent_of(pu).rstrip("/").lower(), "kind": kind or "",
                "scope": scope, "session_id": session_id or "-", "ask_id": ask_id,
                "ts": round(time.time(), 3)}
        with self._lock:
            if scope == SCOPE_PERSISTENT:
                self._ensure()
                if not any(r["path"] == rule["path"] for r in self._pers):
                    self._pers.append(rule)
                    self._persist_rule(rule)
            elif scope == SCOPE_SESSION and session_id:
                b = self._sess.setdefault(session_id, [])
                if not any(r["path"] == rule["path"] for r in b):
                    b.append(rule)
            else:
                return False
        return True

    def match(self, session_id: str, path: str, kind: str = "") -> str:
        pu = unify(path)
        if not pu:
            return ""
        with self._lock:
            self._ensure()
            for name, bucket in ((SCOPE_PERSISTENT, self._pers),
                                 (SCOPE_SESSION, self._sess.get(session_id or "", []))):
                for r in bucket:
                    if r.get("kind") and kind and r.get("kind") != kind:
                        continue
                    pre = (r.get("path") or "").rstrip("/")
                    if not pre or is_drive_root(pre):
                        continue
                    if pu == pre or pu.startswith(pre + "/"):
                        return name
        return ""

    def list_all(self, session_id: str = "") -> List[Dict[str, Any]]:
        """当前**真正生效**的全部规则：永久 + 该会话的会话级。
        面板必须看这个，只看 jsonl 会漏掉会话级规则，也看不出内存与磁盘已经不一致。"""
        with self._lock:
            self._ensure()
            rows: List[Dict[str, Any]] = []
            for name, bucket in ((SCOPE_PERSISTENT, self._pers),
                                 (SCOPE_SESSION, self._sess.get(session_id or "", []))):
                for r in bucket:
                    d = dict(r)
                    d["scope"] = name
                    d["drive_root"] = drive()
                    rows.append(d)
            return rows

    @staticmethod
    def _same(rule: Dict[str, Any], pu: str) -> bool:
        if not pu:
            return True                      # 空路径 = 全部撤销（一键止血用）
        return unify(rule.get("path") or "") == pu

    def _rewrite(self) -> None:
        """全量重写规则文件：撤销必须落盘，否则重启就诈尸。先写临时文件再替换。"""
        rp = rules_path()
        os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
        tmp = rp + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as fh:
            for r in self._pers:
                fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
        os.replace(tmp, rp)

    def revoke(self, path: str = "", scope: str = "", session_id: str = "") -> int:
        """撤销规则：内存与落盘一起改。
        今天的事故就是只清了文件、没清内存 —— 进程里那 3 条越权规则照旧免审。"""
        pu = unify(path)
        n = 0
        with self._lock:
            self._ensure()
            if scope in ("", SCOPE_PERSISTENT):
                keep = [r for r in self._pers if not self._same(r, pu)]
                n += len(self._pers) - len(keep)
                self._pers = keep
                try:
                    self._rewrite()
                except OSError:
                    ledger({"event": "revoke_write_failed", "path": pu})
            if scope in ("", SCOPE_SESSION) and session_id in self._sess:
                b = self._sess.get(session_id) or []
                keep2 = [r for r in b if not self._same(r, pu)]
                n += len(b) - len(keep2)
                self._sess[session_id] = keep2
        if n:
            ledger({"event": "scope_revoked", "count": n, "path": pu,
                    "scope": scope or "any", "session_id": session_id or "-"})
        return n

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            rp = rules_path()
            if not os.path.exists(rp):
                return
            with open(rp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("path"):
                        self._pers.append(obj)
        except OSError:
            pass

    @staticmethod
    def _persist_rule(rule: Dict[str, Any]) -> None:
        try:
            rp = rules_path()
            os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
            with open(rp, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rule, ensure_ascii=False) + chr(10))
        except OSError:
            pass


SCOPES = Scopes()


# ============================================================
# 5. 账本（留痕 != 打断：免打扰与放行的也记，靠它替代弹窗）
# ============================================================
_LEDGER_LOCK = threading.Lock()
_LEDGER_ERR: Dict[str, str] = {"why": ""}


def ledger(rec: Dict[str, Any]) -> None:
    r = dict(rec)
    r.setdefault("ts", round(time.time(), 3))
    r.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        lp = ledger_path()
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        line = json.dumps(r, ensure_ascii=False, default=str)
        if len(line) > 12000:
            line = line[:9000] + json.dumps({"truncated": True})[1:-1] + "}"
        with _LEDGER_LOCK:
            with open(lp, "a", encoding="utf-8") as fh:
                fh.write(line + chr(10))
    except OSError as e:
        _LEDGER_ERR["why"] = "%s: %s" % (e.__class__.__name__, e)


# ============================================================
# 6. 审批端口（宿主注入；引擎永不知道 UI 存在）
# ============================================================
@dataclass
class Decision:
    choice: str = "E"        # A 本次 / B 本会话此路径 / D 永久 / E 拒 / F 拒并中止
    how: str = "answered"    # answered|expired|no_channel|skipped|error|cancelled
    # 主人裁决时手打的话。归引擎而不是归界面：回给模型的文案、账本留痕、CLI 解析
    # 都得同一份语义，前端只负责把字送回来。
    note: str = ""


MAX_NOTE = 400               # 给模型的补充说明上限，防止一段长文把上下文挤掉


def _with_note(base: str, note: str) -> str:
    n = (note or "").strip()
    if not n:
        return base
    if len(n) > MAX_NOTE:
        n = n[:MAX_NOTE] + "…"
    return base + "（主人补充：" + n + "）"


class ApprovalPort:
    """宿主实现 request_many（并发/批量）。默认实现逐条调用 request。"""

    name = "abstract"

    def request(self, req: Dict[str, Any]) -> Decision:
        raise NotImplementedError

    def request_many(self, reqs: List[Dict[str, Any]]) -> List[Decision]:
        out: List[Decision] = []
        for r in reqs:
            try:
                d = self.request(r)
            except Exception as e:
                d = Decision("E", "error")
                ledger({"event": "channel_error", "ask_id": r.get("ask_id"),
                        "err": "%s: %s" % (e.__class__.__name__, e)})
            out.append(d)
            if d.choice in ("E", "F"):
                # 本批已注定不放行，剩下的不再打扰主人（但要把原因交代清楚）
                for rest in reqs[len(out):]:
                    out.append(Decision("E", "skipped"))
                break
        return out


class NullPort(ApprovalPort):
    """没有任何通道：明确报"没有通道"，绝不冒充"用户拒绝"。"""
    name = "null"

    def request(self, req: Dict[str, Any]) -> Decision:
        return Decision("E", "no_channel")


class ConsolePort(ApprovalPort):
    """CLI：纯文字，但把"要干什么 + 源代码 + 后果"一次说清。"""
    name = "console"

    def request(self, req: Dict[str, Any]) -> Decision:
        if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            return Decision("E", "no_channel")
        bar = "─" * 62
        print(chr(10) + "┌" + bar + "┐")
        print("│ 🔴 审批请求  %s%s" % (req.get("title", ""), " " * max(0, 50 - _w(req.get("title", "")))))
        print("├" + bar + "┤")
        print("│ AB 想要：%s" % req.get("intent", ""))
        print("│ 依据    ：%s" % req.get("reason", ""))
        for ln in (req.get("notes") or []):
            print("│ 提示    ：%s" % ln)
        src = (req.get("source_code") or "").split(chr(10))
        print("├" + bar + "┤")
        print("│ 操作源代码（%d 行）：" % len(src))
        for ln in src[:20]:
            print("│   %s" % ln[:150])
        if len(src) > 20:
            print("│   …（另有 %d 行，完整见会话记录）" % (len(src) - 20))
        print("├" + bar + "┤")
        print("│ 🟢 A 允许本次    🟡 B 本会话允许此路径    ⚪ D 永久允许")
        print("│ 🔴 E 拒绝        ⛔ F 拒绝并中止本回合")
        print("└" + bar + "┘")
        try:
            ans = input("请选择 [A/B/D/E/F]（直接回车 = 拒绝；"
                        "可加说明，如「E 先别动这个目录」）: ").strip()
        except (EOFError, OSError, KeyboardInterrupt):
            return Decision("E", "cancelled")
        parts = ans.split(None, 1)
        key = (parts[0] if parts else "").upper()
        note = (parts[1] if len(parts) > 1 else "")
        if len(note) > MAX_NOTE:
            note = note[:MAX_NOTE] + "…"
        return Decision(key if key in ("A", "B", "D", "E", "F") else "E", "answered", note)


def _w(s: str) -> int:
    """粗略宽度（中文按 2 格），只为对齐边框，不参与判定。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in (s or ""))


_PORT: ApprovalPort = ConsolePort()
_PORT_LOCK = threading.Lock()


def set_port(p: Optional[ApprovalPort]) -> None:
    global _PORT
    with _PORT_LOCK:
        _PORT = p if p is not None else NullPort()


def get_port() -> ApprovalPort:
    with _PORT_LOCK:
        return _PORT


# ============================================================
# 7. 判定
# ============================================================
def _forbidden_at_cmdline(lx: Dict[str, Any]) -> str:
    """只在命令位上匹配绝对禁区。命中返回该模式，否则空串。

    这一层存在的原因：绝对区里的单词（那类关机重启命令）做子串匹配时，
    连「在代码里查这些端点名」这种纯只读动作都会被拦下 —— 现场被咬过九次。
    """
    words = set()
    for w in (lx.get("cmdwords") or []):
        w = (w or "").lower()
        for cand in (w, w.rsplit(BS, 1)[-1].rsplit("/", 1)[-1], w.rsplit(".", 1)[-1]):
            words.add(cand)
            # 带可执行后缀的写法（绝对路径或裸扩展名）剥掉后缀再比一次，
            # 否则加个 .exe 就能从命令位上溜走
            for suf in EXE_SUFFIX:
                if cand.endswith(suf):
                    words.add(cand[:-len(suf)])
    segs = [(x.get("text") or "") for x in (lx.get("segments") or [])]
    for pat in FORBIDDEN_CMD:
        if pat.lower() in words:
            return pat
    for pat in FORBIDDEN_PHRASE:
        pre = pat.lower()
        for t in segs:
            if t.startswith(pre):
                return pat
    return ""


def _tool_only_hit(tool: str, kwargs: Dict[str, Any], cwd: str,
                   session_id: str = ""
                   ) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """没有路径/动作点时，仍按**工具名**问一遍规范。

    存在的理由：「参数里没有路径」被当成了「没事可审」。实测事故 ——
    `skillhub_install` 的参数是 `owner/repo/技能路径` 形式的标识，
    于是「安装第三方技能（内容会随注册表注入系统提示）」整条免审。
    任何新工具只要不用路径参数，就自动落到这个盲区里。

    只问规范层，不做文件系统分析；规范内部异常按 fail-closed 记账并跳过
    （与 inspect_one 主路径同口径）。
    """
    ctx = {"tool": tool, "kwargs": kwargs, "paths": [], "operands": [], "mentions": [],
           "actions": [], "facts": {}, "control": "",
           "forbidden_path": FORBIDDEN_PATH, "drive": drive(),
           "root": unify(_root()), "cwd": cwd or _root(),
           "resolve": resolve_path, "opaque_paths": [], "blob": "", "mode": mode(session_id)}
    for sp in specs():
        try:
            if not sp.applies(ctx):
                continue
            f = sp.finding(ctx)
        except Exception as e:
            ledger({"event": "spec_error", "spec": getattr(sp, "KIND", "?"),
                    "err": "%s: %s" % (e.__class__.__name__, e), "handling": "ask",
                    "where": "tool_only"})
            continue
        if f:
            return sp, f
    return None


def _inspect_core(tool: str, kwargs: Dict[str, Any],
                  session_id: str = "", cwd: str = "") -> Dict[str, Any]:
    v: Dict[str, Any] = {"tool": tool, "decision": DECIDE_PASS, "paths": [],
                         "operands": [], "mentions": [], "facts": {},
                         "targets": [], "kind": "", "risk": 0, "reason": "",
                         "intent": "", "notes": [], "critical": False,
                         "fingerprint": "", "args": kwargs or {},
                         # 本次调用的工作目录：模式层要拿它把"相对路径/裸目录名"
                         # 绝对化（`curl -o x` 的落点、`certutil` 不给落点时落在当前目录）。
                         # 不带上它，`v["actions"]` 里的相对写法就解析不出来（实测漏判）。
                         "cwd": cwd or _root()}
    try:
        blob = " ".join(str(x) for x in (kwargs or {}).values()).lower()
        # 词法分层：只有「指令层」参与禁区判定。
        # 2026-10 修正（strict 档的潜伏 bug）：原来 strict 会**跳过词法层**，于是
        # `actions` 为空 —— 而规范层的 finding() 全靠动作点取目标（fs_drive.applies
        # 还能靠 paths 命中，finding 却无可解析的实参），结果是「最严档」反而把
        # 写系统盘判成 pass。实测：AETHER_AUDIT_MODE=strict 下
        # `echo x > C:/Windows/probe.txt` → pass。它一直没被暴露，只因 strict 过去
        # 只能由环境变量触发、生产从不设它。
        # 现在 strict = smart 的超集：照常建动作点，**额外**加一道全文扫。
        lx = None
        if _LEX is not None:
            try:
                lx = _LEX.lex(tool, kwargs)
                if lx.get("failed"):
                    ledger({"event": "lex_fallback", "tool": tool})
            except Exception as e:
                ledger({"event": "lex_error", "tool": tool,
                        "err": "%s: %s" % (e.__class__.__name__, e)})
                lx = None
        instr = blob if lx is None else lx["control_text"]
        if lx is None:
            # 词法层不可用：退回全文扫，宁误拦不漏拦
            pat = next((x for x in FORBIDDEN if x.lower() in instr), "")
        else:
            pat = _forbidden_at_cmdline(lx)
            if mode(session_id) == "strict":
                # strict 的额外那道：命令位没命中，就全文找一遍（正文提到也算）
                pat = pat or next((x for x in FORBIDDEN if x.lower() in blob), "")
        if pat:
            v["decision"] = DECIDE_BLOCK
            v["kind"] = "forbidden"
            v["reason"] = "命中绝对禁区「%s」：此类操作不提供授权入口" % pat
            v["intent"] = "执行被禁止的破坏性命令"
            return v
        if mode(session_id) == "off":
            # 「完全权限」模式：初始审批整体自动同意。
            # **位置必须在这里** —— 绝对禁区（上面那段）在任何模式下都拦：
            # 把最后一道机械防线换成系统提示词里的一句劝告，是这次优化唯一
            # 不能做的交换。原来这条短路在最前面，等于完全模式连禁区也不看。
            v["reason"] = "权限模式：完全 —— 初始审批自动同意（绝对禁区仍拦）"
            ledger({"event": "mode_auto_allow_all", "tool": tool,
                    "session_id": session_id or "-"})
            return v
        if lx is not None:
            men = lx["mention_text"].lower()
            hits = [pat for pat in FORBIDDEN if pat.lower() in men]
            if hits:
                # 禁区词只出现在正文/注释里 = 有人在读写一段提到它的文本，不是要执行它。
                # 记账不打扰：拦这个只会把人训练成闭眼点「允许」，那比没审批更危险。
                ledger({"event": "payload_hit", "tool": tool, "patterns": hits[:6],
                        "session_id": session_id or "-"})
                v["payload_hits"] = hits[:6]
        if tool in NON_FS_TOOLS:
            v["reason"] = "工具不接触本地文件系统，跳过路径分析"
            return v
        actions = list((lx or {}).get("actions") or [])
        if lx is None:
            v["operands"] = extract_paths(kwargs, cwd)
            v["mentions"] = []
        else:
            # operand_text 是**多个操作数的拼接文本**（head -3 x.env 的
            # operand_text 就是 "-3 x.env"）。整段送进 extract_paths 会被当成
            # **一个**路径去绝对化，拼出不存在的假路径 —— 实测 targets 出现过
            # 「.../aetherbreath -3 x.env -2 agent_logs/...」这种字符串，
            # 假路径末尾不是示例后缀时，凭据指纹照样命中 → 误报。
            # 逐 token 收：只认含 . 或 / 的 token。选项（-3）与裸词（stash、docs）
            # 都不是路径。这样既保住相对路径（read_file 的 .env），又不造假目标。
            _ops: List[str] = []
            for _tok in str(lx["operand_text"] or "").split():
                if ("." not in _tok) and ("/" not in _tok) and (chr(92) not in _tok):
                    continue
                if "://" in _tok or _tok.lower().startswith(("mailto:", "data:")):
                    # URL 不是本地路径：unify 会把 http://x/y 拆成「.../http:/x/y」
                    # 这种垃圾，而 egress._hosts 会据此误判外发目标
                    # （实测本机回环 POST 因此被判「数据将离开本机」）。
                    continue
                if _tok.startswith("@"):
                    # curl 的 @file 语法：@ 不属于文件名。不剥则 operands 出现
                    # 「.../@f.txt」，_USERHOST_RE 会从 @ 处切出假主机 "f.txt"。
                    _tok = _tok[1:]
                if not _tok:
                    continue
                _ops += extract_paths({"operand_path": _tok}, cwd)
            v["operands"] = _ops
            ops = set(v["operands"])
            v["mentions"] = [x for x in extract_paths({"mention": lx["mention_text"]}, cwd)
                             if x not in ops]
        v["actions"] = actions
        v["paths"] = v["operands"] + v["mentions"]
        if not v["paths"] and not actions:
            # 「没有路径参数」不等于「没事可审」：有些规范按**工具名**判定
            # （skillhub_install 的参数是 owner/repo 标识，不是路径）。
            # 实测事故：这条 return 让「安装第三方技能」整条免审。
            th = _tool_only_hit(tool, kwargs, cwd, session_id)
            if th is None:
                v["reason"] = "无路径参数，且按工具名的规范均未命中"
                return v
            sp, f = th
            v["kind"] = getattr(sp, "KIND", "?")
            v["title"] = getattr(sp, "TITLE", "")
            v["risk"] = int(getattr(sp, "RISK", 1))
            v["targets"] = list(f.get("targets") or [])
            v["intent"] = f.get("intent", "")
            v["reason"] = f.get("reason", "")
            v["notes"] = list(f.get("notes") or [])
            v["critical"] = bool(f.get("critical"))
            if f.get("block"):
                v["decision"] = DECIDE_BLOCK
            elif f.get("quiet"):
                v["decision"] = DECIDE_QUIET if mode(session_id) != "strict" else DECIDE_ASK
            else:
                v["decision"] = DECIDE_ASK
                v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
            ledger({"event": "tool_spec_hit", "tool": tool, "kind": v["kind"],
                    "decision": v["decision"]})
            return v
        droot, rroot = drive(), unify(_root())
        ctx = {"tool": tool, "kwargs": kwargs, "paths": v["paths"],
               "operands": v["operands"], "mentions": v["mentions"],
               # 每个动作点补上 cwd：相对路径必须在「命令实际所在目录」下绝对化，
               # 否则同一个参数在判定里会变成另一个文件。
               "actions": [dict(a, cwd=a.get("cwd") or (cwd or _root())) for a in actions],
               "facts": lx or {}, "control": instr,
               "forbidden_path": FORBIDDEN_PATH,
               "drive": droot, "root": rroot, "cwd": cwd or _root(),
               "resolve": resolve_path, "opaque_paths": [],
               "blob": blob, "session_id": session_id, "mode": mode(session_id)}
        # 候选目标只取**操作数层**。这里曾有一条「操作数为空就采纳提及层」的回退，
        # 用来兜「路径经变量传递」的 ctypes 写法 —— 那是漏判补丁制造误报的典型：
        # 实测它把脚本里一行盘符常量弹成「对盘符执行写入/覆盖」，而那次任务与系统盘
        # 毫无关系。变量传路径现在由词法层的常量变量表负责（approval_lex._const_vars），
        # 既保住那个漏判，也不再拿被提到的路径当目标。
        targets = [p for p in v["operands"] if p.startswith(droot) or p.startswith(rroot)]
        if not targets and lx is None:
            # 词法层不可用时没有任何结构可依，退回旧的全量判定（宁误拦不漏拦）。
            targets = [p for p in v["operands"]]
        # 读不懂的代码体（语法过不去 / 两种解析器都出不了结构）单独走一条窄路：
        # 它没有可绑定的实参，若就此静默放行，等于「把代码写乱就能过关」。
        # 但也不许把体内的字面量当操作目标（那正是刚修掉的误报来源）——
        # 所以这些路径只作为「提示性上下文」交给规范层，规范层负责明说它是猜的。
        opaque_paths = []
        for a in actions:
            if not (a.get("opaque") or a.get("word") == "?unparsed"):
                continue
            for p in extract_paths({"m": str(a.get("blob") or a.get("opaque") or a.get("text") or "")}, cwd):
                if (p.startswith(droot) or p.startswith(rroot)) and p not in opaque_paths:
                    opaque_paths.append(p)
        ctx["opaque_paths"] = opaque_paths[:6]
        if opaque_paths:
            ledger({"event": "opaque_body", "tool": tool, "paths": opaque_paths[:4]})
        if not targets and not opaque_paths and not actions and not v["operands"]:
            # 有动作点、或操作数层非空，都必须往下走：规范层是从动作点的实参
            # （以及操作数层）取目标的，而上面那份 targets 只收「系统盘 + 项目根」——
            # 项目外的读与写在它眼里是空的（outzone/secrets 两条规范已随四档权限
            # 模式下线；盘外防护改由「工作区模式」的硬边界承担，不再靠规范层）。
            # 若在这里返回，等于「换个盘就没人管」。
            v["reason"] = ("%d 个路径均非本次操作目标（盘外或仅被提及）" % len(v["paths"])
                           if v["mentions"] else
                           "%d 个路径均在覆盖面之外" % len(v["paths"]))
            return v
        hit = None
        spec_err = None
        for sp in specs():
            try:
                if not sp.applies(ctx):
                    continue
                f = sp.finding(ctx)
            except Exception as e:
                # 🔴 规范内部异常**不许**当成「没命中」放行。这条 continue 曾是残留的
                # fail-open：实测规范炸掉时「写系统盘」直接判 pass，而 AETHER_AUDIT_ON_ERROR
                # 管不到它（那管的是引擎顶层异常）。异常一律升级为需人裁决 —— 可批，
                # 所以不至于把修引擎的路也堵死。
                spec_err = (sp, e)
                ledger({"event": "spec_error", "spec": getattr(sp, "KIND", "?"),
                        "err": "%s: %s" % (e.__class__.__name__, e),
                        "handling": "ask"})
                continue
            if f:
                hit = (sp, f)
                break
        if not hit and spec_err is not None:
            sp, e = spec_err
            v["decision"] = DECIDE_ASK
            v["kind"] = getattr(sp, "KIND", "?")
            v["title"] = getattr(sp, "TITLE", "审批规范异常")
            v["risk"] = int(getattr(sp, "RISK", 2))
            v["targets"] = targets[:6]
            v["critical"] = True
            v["reason"] = "审批规范 %s 内部异常（%s: %s），按 fail-closed 交人工确认" % (
                v["kind"], e.__class__.__name__, str(e)[:120])
            v["intent"] = "规范判定失败，改动目标按操作数层原样列出"
            v["notes"] = ["⚠️ 这是闸门自身的缺陷，不是这次操作有问题 —— "
                          "批准前建议先让 AB 修引擎，否则同一处会反复失效"]
            v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
            ledger({"event": "spec_fail_closed", "spec": v["kind"],
                    "targets": v["targets"][:4]})
            return v
        if not hit:
            v["decision"] = DECIDE_PASS
            v["reason"] = "系统盘内只读语义，不申请审批（已记账）"
            return v
        sp, f = hit
        if f.get("block"):
            v["decision"] = DECIDE_BLOCK
            v["kind"] = getattr(sp, "KIND", "?")
            v["targets"] = f.get("targets") or targets
            v["critical"] = True
            v["reason"] = f.get("reason", "命中不可授权的关键系统路径")
            v["intent"] = f.get("intent", "改写关键系统文件")
            ledger({"event": "spec_block", "spec": v["kind"], "targets": v["targets"][:6]})
            return v
        v["kind"] = getattr(sp, "KIND", "?")
        v["title"] = getattr(sp, "TITLE", "")
        v["risk"] = int(getattr(sp, "RISK", 1))
        _gt = f.get("targets")
        # 规范可以显式给空列表（「看不懂这段代码、目标无法确定」），
        # 那种情况不许偷偷回退成整条操作数 —— 那是拿假目标骗人点批准。
        v["targets"] = list(_gt) if _gt is not None else list(targets)
        v["critical"] = bool(f.get("critical"))
        v["reason"] = f.get("reason", "")
        v["intent"] = f.get("intent", "")
        v["notes"] = list(f.get("notes") or []) + (
            ["目标位于系统关键目录，误改可能导致系统异常"] if v["critical"] else [])
        v["reversibility"] = _reversibility(str(f.get("action") or ""), v["targets"])
        v["purpose"] = _purpose_of(kwargs)
        if v["reversibility"]:
            v["notes"].append("可逆性：" + v["reversibility"])
        if f.get("quiet"):
            v["decision"] = DECIDE_QUIET if mode(session_id) != "strict" else DECIDE_ASK
            return v
        v["decision"] = DECIDE_ASK
        v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
        return v
    except Exception as e:
        v["decision"] = (DECIDE_BLOCK if _env("AETHER_AUDIT_ON_ERROR", "deny").lower() != "allow"
                         else DECIDE_PASS)
        v["kind"] = "engine.error"
        v["reason"] = "引擎异常，按 %s 取向处理: %s: %s" % (
            "allow" if v["decision"] == DECIDE_PASS else "deny",
            e.__class__.__name__, e)
        ledger({"event": "engine_error", "tool": tool, "detail": v["reason"]})
        return v


# ============================================================
# 8. 会话权限模式的判定层
# ============================================================
# 位置：**包在核心判定之外**。先让 _inspect_core 按老规矩算出结果，这里再按模式
# 改写。为什么不写成一条「审批规范」：规范只能**加**判定，而模式还必须能
# **压掉**规范（工作区内免问、完全模式整体自动同意），那要的是引擎级覆盖权。
_MODE_EXEC_KINDS = frozenset({"opaque.pipe"})
# 天生就在工作区之外落地的规范：技能库（agent_skills/）、引擎自留地（代码与审批链）。
# 它们不一定带得出路径（skillhub_install 的参数是 owner/repo），所以按 KIND 兜。
_MODE_OUTSIDE_KINDS = frozenset({"skill.install", "engine.selfmodify"})
# 注：`mcp.spawn` **不在**上面两张表里（2026-10 主人要求放开）。MCP 调用的落点由它自己的
# 参数决定 —— 有路径就按路径判（区内放行 / 区外拒），没路径就放行。原先一律拒是"拿不准
# 就全否"的懒办法，代价是工作区档里连"走 MCP 把文件下到工作区"都做不了（主人实测）。

_IMPACT = None


def _impact():
    """动作点 -> 动作类别的分类器。

    **复用审批规范那套 `_impact`，绝不另写一份**：两份动词表必然漂移，
    而漂移的那一刻就是「同一个命令在规范和模式层得到两种结论」。
    """
    global _IMPACT
    if _IMPACT is None:
        try:
            from approvals import _impact as _m       # noqa: PLC0415
            _IMPACT = _m
        except Exception as e:
            ledger({"event": "mode_impact_unavailable",
                    "err": "%s: %s" % (e.__class__.__name__, e)})
    return _IMPACT


def _destructive_actions(v: Dict[str, Any]) -> Tuple[List[Tuple[str, str]], bool, List[str]]:
    """挑出破坏性动作点 -> ([(动作类别, 目标路径), ...], 有没有判不准的, 指向工作区根的原始实参)。

    判不准（opaque 代码体；认得出是写、却取不到目标）单独报出来：模式层**不许**
    把「没看见区外目标」当成「目标在区内」—— 那正是 fail-open。

    第三个返回值是「掀桌子」判据（2026-10 补）：`rm -rf agent_workspace` 里的
    `agent_workspace` 不带分隔符/扩展名，`_impact` 的路径判定会认为它"不像路径"
    而不产出目标 —— 那条命令此前一直是靠"目标无法判定→拒"兜着的，一旦把无法判定
    改成弹卡，掀桌子就漏了（实测回归）。所以这里**拿原始实参按 cwd 拼一次**专门看它。
    """
    impact = _impact()
    hits: List[Tuple[str, str]] = []
    roots: List[str] = []
    unknown = False
    # 会话级 cwd：动作点里的相对写法要靠它绝对化（`v["actions"]` 不带 cwd，
    # 而 ctx 里那份是带 cwd 的副本 —— 模式层读的是前者，所以要自己补）。
    _sess_cwd = str(v.get("cwd") or "")
    for a in (v.get("actions") or []):
        if a.get("opaque") or a.get("word") == "?unparsed":
            unknown = True
            continue
        _act = dict(a, cwd=str(a.get("cwd") or _sess_cwd or _root()))
        word = str(_act.get("word") or "")
        cat = ""
        # ---- 读模式的 open 是「读」，不是「写」（2026-10 主人实测）----
        # 病根：`CATEGORY_WIN` 里为 Win32 的 CreateFile 登记过 "open"，于是
        # `open(p, 'r')` 也被算成「写入/覆盖」；而只读的 open 在 `targets_of` 里
        # 按设计**取不到目标**（读没有目标）→ 落到下面「认得出是写、却取不到目标」，
        # 判成"无法判定"→ 工作区档弹卡 / 仅读档直接拒。
        # 症状：python 里读一下 MEMORY.md（读→改→写回的常规改法）就弹卡。
        # 判据与 `_impact` 同源（MODE_WRITE_CHARS），不另写一份动词表。
        _mode = str(_act.get("mode") or "")
        _mode_write = any(c in _mode for c in getattr(impact, "MODE_WRITE_CHARS", "wax+")
                          ) if impact is not None else False
        # 「没给模式」= 默认只读；「给了模式但认不出」= 不知道是不是写 —— 后者不许放过
        _mode_unknown = bool(_act.get("mode_given")) and not _mode
        if impact is not None and impact.verb_last(word) == "open" \
                and not _mode_write and not _mode_unknown:
            continue
        if impact is not None:
            try:
                cat = impact.category_of(word)
            except Exception:
                cat = ""
        if not cat and (_act.get("redirect") or _mode_write):
            cat = "写入/覆盖"
        # 先取目标，再决定要不要跳过 —— 顺序很重要（2026-10 实测）：
        # `curl -o D:/x` 这类**输出/下载落点**由 targets_of 内部的 flag 规则给出，
        # 而 "curl" 本身不在任何动作类别里。原先先按类别 `continue`，于是那条落点
        # 永远没被问过 —— 工作区档里"下载到区外"静默通过。
        try:
            paths, _how = impact.targets_of(_act) if impact is not None else ([], "")
        except Exception:
            paths = []
        if not cat and not paths:
            continue                       # 既非已知破坏性动词、也解析不出目标：不管
        if not cat:
            cat = "写入/覆盖"              # 有目标却无类别（输出落点类）：按写入对待
        _cwd = str(_act.get("cwd") or _root())
        # 掀桌子只看「**删除/移动**工作区文件夹本身」。2026-10 收紧：原先任何动作
        # 只要**提到**工作区根就算掀桌子，于是 `tar -czf agent_workspace/bak.tar
        # agent_workspace`、`Compress-Archive agent_workspace -DestinationPath …`
        # 这类"拿工作区当源"的正常备份/打包也被判成掀桌子 —— 违反了本仓库
        # 「提到 ≠ 执行」的那条老规矩。拷贝/打包的源是**读**，不是拆桌子。
        if cat in ("删除", "移动/重命名"):
            for _raw in (_act.get("args") or []):
                _s = str(_raw or "").strip()
                if not _s:
                    continue
                if permission_modes.is_workspace_root(unify(_s, _cwd)):
                    roots.append(unify(_s, _cwd))
        if not paths:
            unknown = True
            continue
        for p in paths:
            hits.append((cat, p))
    return hits, unknown, roots


def _short(p: str, n: int = 90) -> str:
    p = str(p or "")
    return p if len(p) <= n else "…" + p[-n:]


def _mode_deny(v: Dict[str, Any], pm: str, why: str, session_id: str) -> Dict[str, Any]:
    """模式硬拒。

    复用 DECIDE_BLOCK 的语义（**不提供授权入口**），但用 `mode_denied` 与
    `kind = mode.<档>` 标明来源：这样卡片/账本能区分「绝对禁区」与「模式限制」，
    而 gate_batch 那条整批原子拒绝的现成路径也不用改。
    """
    v["decision"] = DECIDE_BLOCK
    v["mode_denied"] = True
    v["kind"] = "mode." + pm
    v["risk"] = 1
    v["critical"] = False
    v["reason"] = "%s。%s" % (why, permission_modes.MODE_DENY_HINT)
    v["intent"] = "被权限模式直接拒绝"
    ledger({"event": "mode_deny", "mode": pm, "tool": v.get("tool"),
            "why": why[:160], "session_id": session_id or "-"})
    return v


def _apply_permission_mode(v: Dict[str, Any], session_id: str) -> Dict[str, Any]:
    """按该会话的权限模式改写核心判定。普通模式原样返回（零行为变化）。"""
    try:
        pm = permission_modes.get(session_id)
        if pm == permission_modes.MODE_NORMAL:
            return v
        # 绝对禁区优先级最高：任何模式下都不许被模式层改写。
        if v.get("decision") == DECIDE_BLOCK:
            return v
        acts, unknown, root_hits = _destructive_actions(v)
        kind = str(v.get("kind") or "")

        if pm == permission_modes.MODE_FULL:
            # 完全 = 初始审批自动同意。BLOCK 已在上面提前返回，所以禁区仍然拦。
            if v.get("decision") in (DECIDE_ASK, DECIDE_QUIET):
                v["decision"] = DECIDE_PASS
                v["reason"] = "权限模式：完全 —— 初始审批自动同意（绝对禁区仍拦）"
            return v

        if pm == permission_modes.MODE_READONLY:
            if acts:
                cat, p = acts[0]
                return _mode_deny(v, pm, "仅读模式：「%s」属于操作（%s），本模式一律直接拒绝"
                                  % (cat, _short(p)), session_id)
            if unknown:
                return _mode_deny(v, pm, "仅读模式：本次调用含无法判定的动作（可能是执行或写入），"
                                  "仅读只允许「知晓」不允许「操作」", session_id)
            if kind:
                # 到这儿剩下的规范（net.egress / opaque.pipe / skill.install ...）
                # 按主人的定义「初始审批在仅读下不生效」——不弹卡，直接拒。
                return _mode_deny(v, pm, "仅读模式：命中审批规范「%s」，"
                                  "本模式下初始审批不生效（一律直接拒绝）" % kind, session_id)
            v["decision"] = DECIDE_PASS
            v["reason"] = "仅读模式：只读调用放行（初始审批不适用）"
            return v

        if pm == permission_modes.MODE_WORKSPACE:
            if kind == "net.egress":
                return _mode_deny(v, pm, "工作区模式：带载荷外发数据一律直接拒绝"
                                  "（数据发出去就收不回来）", session_id)
            # MCP 家族（主人 2026-10 要求**放开**）：判据从"这条通道一律拒"改成
            # **看落点** —— 参数里有区外路径就拒（"落在其他地方就拒"），
            # 只有区内路径或压根没路径就免问放行（"落在工作区就没毛病"）。
            # 这样"走 MCP 把技能下到工作区"能用，而"借 MCP 写区外"仍然拦住。
            if kind == "mcp.spawn" or str(v.get("tool") or "").startswith("mcp"):
                # operands + paths 都要看：MCP 的路径藏在 arguments 的嵌套 dict 里，
                # 提取层把它们归到 mentions（→ paths）而不是 operands（实测：
                # `{'out': 'D:/x'}` 只出现在 paths 里）。这里没有"提到 vs 操作数"的
                # 区分意义 —— 工具的全部参数就是它的动作面。
                _cands = [str(p) for p in (list(v.get("operands") or [])
                                           + list(v.get("paths") or [])) if p]
                _out = [p for p in _cands if not permission_modes.in_workspace(p)]
                if _out:
                    return _mode_deny(v, pm, "工作区模式：MCP 调用的落点在区外（%s）—— "
                                      "落在工作区之外一律拒绝" % _short(_out[0]), session_id)
                v["decision"] = DECIDE_PASS
                v["reason"] = "工作区模式：MCP 调用落在工作区内（或没有文件落点），免审批"
                return v
            if kind in _MODE_EXEC_KINDS or kind in _MODE_OUTSIDE_KINDS:
                return _mode_deny(v, pm, "工作区模式：命中「%s」—— 它必然在工作区之外生效"
                                  "（技能库 / 引擎文件 / 外部进程），本模式直接拒绝"
                                  % kind, session_id)
            # 掀桌子判据先于其它（见 _destructive_actions 的第三个返回值）
            if root_hits:
                return _mode_deny(v, pm, "工作区模式：目标是工作区文件夹本身（%s）——"
                                  "叫你在桌上作业，不等于可以把桌子掀了"
                                  % _short(root_hits[0]), session_id)
            root_hit = [(c, p) for (c, p) in acts if permission_modes.is_workspace_root(p)]
            if root_hit:
                return _mode_deny(v, pm, "工作区模式：%s 的目标就是工作区文件夹本身（%s）——"
                                  "叫你在桌上作业，不等于可以把桌子掀了"
                                  % (root_hit[0][0], _short(root_hit[0][1])), session_id)
            outside = [(c, p) for (c, p) in acts if not permission_modes.in_workspace(p)]
            if outside:
                return _mode_deny(v, pm, "工作区模式：%s 的目标在工作区之外（%s）——区外只读，"
                                  "外部文件只允许先拷进工作区再操作"
                                  % (outside[0][0], _short(outside[0][1])), session_id)
            if unknown:
                # 「目标无法判定」不硬拒，**交人**（2026-10 主人反馈后改）。
                # 理由：工作区档的契约是"区内免问 / 区外拒 / 其余照旧问"（初始审批在
                # 区外生效），而"拿不准"属于"其余"。硬拒的代价实测很重 —— 路径经变量、
                # f-string、函数返回等写法构造时分析器认不出，`agent_workspace/` 里的
                # 合法写入会被整条拒掉（C1b/C2 全灭就是这么来的）。
                # 改成弹卡后人可以当场判断；而**确证**区外的仍然直接拒（上面那两条）。
                v["decision"] = DECIDE_ASK
                v["kind"] = "mode." + pm
                v["risk"] = max(int(v.get("risk") or 0), 1)
                v["reason"] = ("工作区模式：本次调用的目标无法判定，可能有区外改动 —— "
                               "交你确认。可以批准（真在区内就没事），也可以拒绝并要求 AB "
                               "把路径写成能看清的形式。")
                v["intent"] = "目标无法判定，需人工确认是否只动工作区"
                v["notes"] = list(v.get("notes") or []) + [
                    "⚠️ 路径经变量 / 拼接 / 函数返回构造时，分析器可能认不出目标。"
                    "把它写成字面量路径（如 'agent_workspace/x.txt'）就能免问。"]
                v["fingerprint"] = _fp(str(v.get("tool") or ""), v["kind"],
                                       list(v.get("targets") or []))
                ledger({"event": "mode_ask_undecidable", "mode": pm,
                        "tool": v.get("tool"), "session_id": session_id or "-"})
                return v
            if acts:
                v["decision"] = DECIDE_PASS
                v["reason"] = "工作区模式：目标全在工作区内，免审批"
                return v
            # 没有破坏性动作：规范若指名了目标且全在区内，也算「区内完全权限」
            if v.get("decision") in (DECIDE_ASK, DECIDE_QUIET):
                tg = [t for t in (v.get("targets") or []) if t]
                if tg and all(permission_modes.in_workspace(t) for t in tg):
                    v["decision"] = DECIDE_PASS
                    v["reason"] = "工作区模式：目标全在工作区内，免审批"
            return v

        return v
    except Exception as e:
        # 模式层自己炸了：fail-closed，交人工（与 spec_error 同一取向）
        v["decision"] = DECIDE_ASK
        v["kind"] = "engine.mode_error"
        v["critical"] = True
        v["reason"] = ("权限模式判定异常（%s: %s），按 fail-closed 交人工确认"
                       % (e.__class__.__name__, str(e)[:120]))
        v["notes"] = list(v.get("notes") or []) + [
            "⚠️ 这是闸门自身的问题，不是这次操作有问题 —— "
            "批准前建议先让 AB 修 permission_modes 接入"]
        ledger({"event": "mode_error", "err": "%s: %s" % (e.__class__.__name__, e)})
        return v


def inspect_one(tool: str, kwargs: Dict[str, Any],
                session_id: str = "", cwd: str = "") -> Dict[str, Any]:
    """公开入口 = 核心判定 + 会话权限模式改写（普通模式下两者等价）。"""
    v = _inspect_core(tool, kwargs, session_id, cwd)
    return _apply_permission_mode(v, session_id)


def _reversibility(action: str, targets: Sequence[str]) -> str:
    """人做裁决时最在意的一件事：能不能撤回。引擎来判，不靠 AB 自述。

    过去卡片上「写个探针文件」和「删掉已有文件」长得一模一样，
    于是主人只能靠猜 —— 靠猜的批准不算审批。
    """
    if not targets:
        return ""
    first = (targets[0] or "").replace("/", os.sep)
    try:
        exists = os.path.exists(first)
    except OSError:
        exists = False
    if "删除" in action:
        return ("不可逆：目标已存在，删除后无法自动恢复（回收站不收命令行删除）"
                if exists else "目标不存在，这次删除实际不会改动任何东西")
    if "移动" in action or "重命名" in action:
        return "改路径/改名：可逆，但请记住原名"
    if "写入" in action or "覆盖" in action:
        return ("覆盖已存在文件：原内容会丢失，不可自动撤回" if exists
                else "新建文件：可逆，删掉即可撤回")
    return ""


def _purpose_of(kwargs: Dict[str, Any]) -> str:
    """从 AB 自己写的命令里提取它声明的用途（首行注释）。

    这是一条问责机制：没声明就在卡片上明写「未声明，建议先问它」，
    让主人手里有比"猜"更好的选项。
    """
    for key in ("command", "code", "script", "implementation_code", "cmd"):
        val = (kwargs or {}).get(key)
        if not isinstance(val, str):
            continue
        for line in val.split(chr(10))[:8]:
            t = line.strip()
            if t.startswith("#") or t.startswith("//"):
                body = t.lstrip("#/").strip()
                if len(body) >= 6:
                    return body[:160]
    return ""


def _fp(tool: str, kind: str, paths: Sequence[str]) -> str:
    import hashlib
    return hashlib.sha1((tool + "|" + kind + "|" + "|".join(sorted(paths)[:6]))
                        .encode("utf-8", "replace")).hexdigest()[:12]


def _source_of(kwargs: Dict[str, Any], limit: int = 6000) -> str:
    parts = []
    for k, v in (kwargs or {}).items():
        if k == "logger" or v is None:
            continue
        parts.append("%s = %r" % (k, v))
    s = chr(10).join(parts)
    return s if len(s) <= limit else s[:limit] + chr(10) + "…（已截断）"


# ============================================================
# 8. 批次闸门：agent.py 唯一调用点
# ============================================================
_GRANTS: Dict[str, Any] = {}
_GRANTS_LOCK = threading.Lock()


def grants_for(session_id: str = "") -> Dict[str, Any]:
    """行为层用：取该会话最近一次批准的授权前缀摘要。

    这份状态由引擎自持 —— agent.py 只管路由，不该替审批记账。
    """
    with _GRANTS_LOCK:
        return dict(_GRANTS.get(session_id or "-", {}) or {})


def last_user_text(conversation: Optional[Sequence[Any]]) -> str:
    """从会话里取最近一条 user 消息的文本（多模态只取文本段）。

    为什么这段在引擎里：卡片要能回显「主人为什么被问」，否则主人只看见一串
    路径，只能靠猜 —— 而靠猜的批准不算审批。取上下文是审批的领域知识，
    因此归引擎；agent.py 只负责把整个 conversation 交过来，不做任何加工。

    兼容两种会话形态：dict 列表与带属性的对象列表。取不到返回空串 ——
    卡片照实显示「未提供」，绝不替主人编一句话。
    """
    for m in reversed(list(conversation or ())):
        is_dict = isinstance(m, dict)
        if (m.get("role") if is_dict else getattr(m, "role", "")) != "user":
            continue
        c = m.get("content") if is_dict else getattr(m, "content", "")
        if isinstance(c, list):          # 多模态：只取文本段，忽略图片等非文本段
            parts = [str(x.get("text", "") or "") for x in c
                     if isinstance(x, dict) and x.get("type", "text") == "text"]
            c = " ".join(p for p in parts if p)
        return str(c or "")
    return ""


def gate_batch(items: Sequence[Tuple[str, str, Dict[str, Any]]],
               session_id: str = "", cwd: str = "",
               user_request: str = "",
               conversation: Optional[Sequence[Any]] = None
               ) -> Tuple[Dict[str, Tuple[bool, str]], Dict[str, Any]]:
    """items: [(tool_call_id, tool_name, kwargs), ...]

    -> (decisions, grants)
       decisions[tc_id] = (是否放行, 不放行时给模型看的文本)
       grants 记录本批已批准的路径前缀，供后续行为校验使用。

    整批原子：只要有一项没拿到批准，本批所有工具都不提交编排器（含本来不需要审批的）。

    上下文：传 `conversation` 即由引擎自己取「主人最近一句话」用于卡片回显 ——
    调用方（agent.py）只负责把整个会话交过来，不替审批准备上下文。
    显式给定 `user_request` 时以它为准（测试与宿主直调用留口）。
    """
    grants: Dict[str, Any] = {"approved_prefixes": [], "session_id": session_id}
    with _GRANTS_LOCK:
        _GRANTS[session_id or "-"] = grants
    if not items:
        return {}, grants
    ids = [it[0] for it in items]
    # 这里曾有一条 `if mode() == "off": 全放行` 的短路。四档权限模式上线后它必须删掉：
    # 「完全权限」正是派生到 off 的那一档，而这条短路发生在 inspect_one **之前** ——
    # 留着它，完全模式就绕过了绝对禁区判定（BLOCK），与「禁区任何模式都拦」直接冲突。
    # 现在完全模式由 inspect_one 逐项判完（先禁区、后自动同意），结果一样，禁区仍在。
    # 主人最近那句话：由引擎自己从会话里取。取上下文是审批的领域知识，
    # agent.py 只把 conversation 交过来，不做任何加工。
    if not user_request and conversation:
        user_request = last_user_text(conversation)

    vmap = {it[0]: inspect_one(it[1], it[2], session_id, cwd) for it in items}
    for it in items:
        ledger({"event": "inspect", "tc_id": it[0], "tool": it[1],
                "decision": vmap[it[0]]["decision"], "kind": vmap[it[0]]["kind"],
                "targets": vmap[it[0]]["targets"][:8], "session_id": session_id or "-",
                "reason": vmap[it[0]]["reason"][:200]})

    blocked = [i for i in ids if vmap[i]["decision"] == DECIDE_BLOCK]
    if blocked:
        # ⚠️ 每条**各自**的理由。旧版这里是先取 `b0 = vmap[blocked[0]]["reason"]`，
        # 再把它发给**所有**被拦项 —— 表面上像"整批一个原因"，实际是把第一条的理由
        # 复读给全部。2026-10 实测踩到：一批 4 条被权限模式拒掉，主人收到的 4 条回执
        # 逐字相同（全是第一条的「写入/覆盖 t.txt」），连那条 execute_shell 也拿到
        # 文件类的理由。账本里四条各自独立、判定层完全正确 —— 坏在呈现层。
        # 旧代码没暴露它，是因为 DECIDE_BLOCK 过去只在真禁区出现（一次至多一条）。
        out = {i: (False, "❌ %s" % (vmap[i]["reason"] if i in blocked else WHY_BATCH_COLD))
               for i in ids}
        ledger({"event": "batch_block", "blocked": blocked, "session_id": session_id or "-"})
        return out, grants

    asks = [i for i in ids if vmap[i]["decision"] == DECIDE_ASK]
    if not asks:
        for i in ids:
            if vmap[i]["decision"] == DECIDE_QUIET:
                grants["approved_prefixes"] += vmap[i]["targets"]
        return {i: (True, "") for i in ids}, grants

    # 作用域预筛：ask 项若所有目标路径都已被授予，则免打扰（但仍计入 grants）
    pending: List[str] = []
    for i in asks:
        t = vmap[i]["targets"]
        via = [SCOPES.match(session_id, x, vmap[i]["kind"]) for x in t[:4]] if t else []
        if t and all(via):
            vmap[i]["scope_via"] = via[0]
            grants["approved_prefixes"] += t
            ledger({"event": "scope_allow", "tc_id": i, "via": via[0],
                    "kind": vmap[i]["kind"], "targets": t[:8]})
        else:
            pending.append(i)
    if not pending:
        return {i: (True, "") for i in ids}, grants

    reqs = []
    for i in pending:
        v = vmap[i]
        # 卡片给人做裁决所需的信息，顺序即重要度：
        # 它到底想干什么 -> 动哪个文件、能不能撤回 -> 你刚才说了什么
        notes = [("AB 自述用途：" + v["purpose"]) if v.get("purpose") else
                 "⚠️ AB 未声明这次操作的目的 —— 建议先问它要干什么再批"]
        notes += list(v["notes"])
        if len(user_request or "") > 3:
            notes.append("你的原话：" + user_request[:120])
        reqs.append({
            "ask_id": i, "session_id": session_id, "kind": v["kind"], "risk": v["risk"],
            "title": v.get("title") or ("对系统盘文件的%s操作" % (
                v["intent"].split("：")[-1] if "：" in v["intent"] else "改动")),
            "intent": v["intent"], "reason": v["reason"], "notes": notes,
            "paths": v["targets"][:12], "critical": v["critical"],
            "timeout": timeout_sec(), "total": len(pending),
            "accepts_note": True,          # 通道据此显示输入框；引擎负责用不用
            "note_hint": "可补充说明（拒绝时尤其有用，会原样送回 AB 与账本）",
            "purpose": v.get("purpose", ""),
            "reversibility": v.get("reversibility", ""),
            "user_request": (user_request or "")[:200],
            "source_code": _source_of(v["args"]),
            "options": [{"key": "A", "label": "允许本次", "scope": SCOPE_ONCE, "emoji": "🟢"},
                        {"key": "B", "label": "本会话允许此路径", "scope": SCOPE_SESSION,
                         "emoji": "🟡"},
                        {"key": "D", "label": "永久允许（需确认）", "scope": SCOPE_PERSISTENT,
                         "emoji": "⚪", "confirm": True},
                        {"key": "E", "label": "拒绝", "scope": SCOPE_ONCE, "emoji": "🔴"},
                        {"key": "F", "label": "拒绝并中止本回合", "scope": SCOPE_ONCE,
                         "emoji": "⛔", "stop": True}],
        })
    ledger({"event": "batch_ask", "ask_id": [r["ask_id"] for r in reqs],
            "count": len(reqs), "session_id": session_id or "-"})

    t0 = time.time()
    try:
        decs = get_port().request_many(reqs)
    except BaseException as _bx:               # KeyboardInterrupt / SystemExit 也算
        # 没有终态的账本无法区分「主人拒了」与「中途断了」—— 有天实测就是这样：
        # 只剩一条 inspect(ask) 挂在那儿。记完再抛，不改变原有的中断语义。
        ledger({"event": "batch_cancelled", "asked": len(pending),
                "ask_id": [r["ask_id"] for r in reqs],
                "why": "%s 等待裁决时回合被中止" % _bx.__class__.__name__,
                "session_id": session_id or "-"})
        raise
    except Exception as e:
        decs = [Decision("E", "error") for _ in reqs]
        ledger({"event": "channel_fatal", "err": "%s: %s" % (e.__class__.__name__, e)})
    while len(decs) < len(reqs):
        decs.append(Decision("E", "skipped"))

    def reason_of(idx: int) -> str:
        d = decs[idx]
        if d.how == "expired":
            base = WHY_NO_REPLY
        elif d.how == "no_channel":
            base = WHY_NO_CHANNEL
        elif d.how == "skipped":
            base = WHY_BATCH_COLD
        elif d.how in ("error", "cancelled"):
            base = "审批通道异常或已取消（%s）" % d.how
        else:
            base = WHY_DENY
        # 主人手打的话优先于模板：他写了字，说明"用户拒绝"那四个字不够表达意图
        return _with_note(base, says_txt or (getattr(d, "note", "") or ""))

    # 主人在这一批里说过的话（按出现顺序去重）。附给每一条未放行项：
    # 只挂在"他针对的那一项"上，模型看到别条回执时就会以为那是无来由的拒绝。
    says: List[str] = []
    for _d in decs[:len(pending)]:
        _n = (getattr(_d, "note", "") or "").strip()
        if _n and _n not in says:
            says.append(_n)
    says_txt = "；".join(says)[:MAX_NOTE]

    approved: Dict[str, Decision] = {}
    for idx, i in enumerate(pending):
        d = decs[idx]
        _vn = {"event": "verdict", "tc_id": i, "ask_id": i, "choice": d.choice,
               "how": d.how, "kind": vmap[i]["kind"], "targets": vmap[i]["targets"][:8],
               "elapsed_sec": round(time.time() - t0, 2), "session_id": session_id or "-"}
        if getattr(d, "note", ""):
            _vn["note"] = d.note.strip()[:MAX_NOTE]     # 主人的话是审计材料，必须落账
        ledger(_vn)
        if d.choice in ("A", "B", "D"):
            approved[i] = d
            if d.choice in ("B", "D"):
                sc = SCOPE_SESSION if d.choice == "B" else SCOPE_PERSISTENT
                made = [SCOPES.add(sc, session_id, t, vmap[i]["kind"], ask_id=i)
                        for t in vmap[i]["targets"][:3]]
                if not any(made):
                    # 批准本身仍然有效（这次照做），只是不许建立免审规则。必须留痕，
                    # 否则主人以为"以后不再问了"，实际下次照样弹 —— 那是骗人。
                    ledger({"event": "scope_denied", "tc_id": i, "scope": sc,
                            "targets": vmap[i]["targets"][:3],
                            "why": "前缀过浅（盘符后不足 %d 段），整棵目录树不可免审"
                                   % MIN_SCOPE_SEGMENTS})
            grants["approved_prefixes"] += vmap[i]["targets"]
        if d.choice == "F":
            break

    batch_ok = len(approved) == len(pending)
    out: Dict[str, Tuple[bool, str]] = {}
    stop = any(d.choice == "F" for d in decs)
    for idx, i in enumerate(pending):
        if batch_ok:
            out[i] = (True, "")
        elif i in approved:
            # 主人批了它，只是同批别人没批 —— 这不是拒绝。
            # 若写成用户拒绝，模型会放弃一条已获授权的路径。
            out[i] = (False, "❌ " + _with_note(WHY_APPROVED_COLD, says_txt))
        else:
            out[i] = (False, "❌ " + reason_of(idx))
    for i in ids:
        if i not in out:
            out[i] = (True, "") if batch_ok else (
                False, "❌ " + _with_note(WHY_BATCH_COLD, says_txt))
    if not batch_ok and stop:
        for i in out:
            out[i] = (False, out[i][1] + " （主人要求中止本回合）")
    ledger({"event": "batch_result", "ok": batch_ok, "asked": len(pending),
            "approved": len(approved), "stop": stop, "session_id": session_id or "-"})
    return out, grants


# ============================================================
# 9. 自检：保护"是否在"必须是可查询的事实
# ============================================================
def self_check(registered_tools: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"mode": mode(), "drive": drive(),
                           "port": get_port().name, "ok": False, "checks": {}}
    d = drive()
    cases = [
        ("写系统盘要问", "execute_shell",
         {"command": "echo x > " + d.rstrip("/") + "/Windows/probe.txt"}, DECIDE_ASK),
        ("删系统盘要问", "execute_python",
         {"code": "import os; os.remove(r" + d.rstrip("/") + "/Windows/probe.txt" + chr(39)}, DECIDE_ASK),
        ("移动要问", "execute_shell",
         {"command": "mv " + d.rstrip("/") + "/Users/x/a.txt " + d.rstrip("/") + "/Users/x/b.txt"}, DECIDE_ASK),
        ("只读不问", "read_file", {"file_path": d.rstrip("/") + "/Windows/win.ini"}, DECIDE_PASS),
        ("临时目录免打扰", "execute_shell",
         {"command": "echo x > " + d.rstrip("/") + "/Users/x/AppData/Local/Temp/t.txt"}, DECIDE_QUIET),
        # 2026-10：outzone.write 随四档权限模式下线 —— 普通模式下「项目外写入」不再
        # 申请审批（主人的判断：那条规范一直误拦，存在意义不大）。盘外防护改由
        # **工作区模式**的硬边界承担，所以这里断言的是「普通模式放行」，
        # 而模式的接入由下面那段四档探针证明（不是靠删掉断言蒙过去）。
        ("盘外写在普通模式放行（outzone 已下线）", "execute_shell",
         {"command": "echo x > D:/work/a.txt"}, DECIDE_PASS),
        ("禁区直接拒", "execute_shell", {"command": "format " + d + " /Q"}, DECIDE_BLOCK),
    ]
    if _LEX is not None:
        _F = list(FORBIDDEN)
        _verb = [x for x in _F if x.endswith(" ")][0]
        _sam = [x for x in _F if x.endswith("sam")][0]
        _say = [x for x in _F if len(x) == 8 and x[0] == "s"][0]
        cases += [
            ("文档正文提禁区词不拦", "execute_python",
             {"code": 'doc = """note: ' + _verb.strip() + " and " + _say
                      + ' are forbidden"""' + chr(10) + "print(len(doc))"}, DECIDE_PASS),
            ("正文提及盘内路径不拦", "execute_python",
             {"code": 'doc = "see ' + d + "/Windows" + _sam
                      + ' in the ticket"' + chr(10) + "print(len(doc))"}, DECIDE_PASS),
            ("只读读取不误判为写入", "execute_shell",
             {"command": "type " + d + "/Windows/win.ini"}, DECIDE_PASS),
            ("解释器注入不许降级成载荷", "execute_shell",
             {"command": "python -c " + chr(39) + "open(r" + chr(34) + d + "/Windows" + _sam + chr(34) + "," + chr(34) + "wb" + chr(34) + ")" + chr(39)}, DECIDE_BLOCK),
        ]
        _hp = [x for x in FORBIDDEN_PATH if x.endswith("hosts")]
        if _hp:
            host = "/" + _hp[0].lstrip(BS).replace(BS, "/")
            cases += [
                ("读关键系统文件不拦", "read_file",
                 {"file_path": d + "/Windows" + host}, DECIDE_PASS),
                ("写关键系统文件直接拒", "execute_shell",
                 {"command": "echo x > " + d + "/Windows" + host}, DECIDE_BLOCK),
            ]
    bad = []
    # 浅前缀护栏（通过时不落盘，只有失效才会）
    guard = SCOPES.add(SCOPE_PERSISTENT, "self_check_probe", d + "/Windows", "probe")
    out["checks"]["浅前缀不得签发免审规则"] = (guard is False, guard)
    if guard is not False:
        bad.append("浅前缀护栏失效：整盘一级目录被存成了永久规则")
    for name, tool, kw, want in cases:
        if want == DECIDE_QUIET and mode() == "strict":
            want = DECIDE_ASK
        try:
            got = inspect_one(tool, kw)["decision"]
        except Exception as e:
            got = "raise:%s" % e.__class__.__name__
        out["checks"][name] = (got == want, got)
        if got != want:
            bad.append("%s(期望%s 实得%s)" % (name, want, got))
    # ---- 权限模式接入自检（2026-10）----
    # 四档模式是新的头号旋钮，它必须**当场可证**接上了。同一条命令（写系统盘）
    # 在四档下各判一次：普通 ask / 工作区 block / 仅读 block / 完全 pass。
    # 探针跑在一个一次性的假会话上，跑完清掉 —— 绝不污染真实会话。
    _pm_probe = "self_check_permission_mode"
    _pm_cmd = {"command": "echo x > " + d.rstrip("/") + "/Windows/probe_mode.txt"}
    try:
        for _m, _want in ((permission_modes.MODE_NORMAL, DECIDE_ASK),
                          (permission_modes.MODE_WORKSPACE, DECIDE_BLOCK),
                          (permission_modes.MODE_READONLY, DECIDE_BLOCK),
                          (permission_modes.MODE_FULL, DECIDE_PASS)):
            permission_modes.set(_pm_probe, _m)
            try:
                _got = inspect_one("execute_shell", _pm_cmd, session_id=_pm_probe)["decision"]
            except Exception as e:
                _got = "raise:%s" % e.__class__.__name__
            _name = "权限模式「%s」：写系统盘 -> %s" % (permission_modes.label(_m), _want)
            out["checks"][_name] = (_got == _want, _got)
            if _got != _want:
                bad.append("%s(期望%s 实得%s)" % (_name, _want, _got))
    finally:
        permission_modes.forget(_pm_probe)
    problems = [] if _LEX is not None else [
        "词法层 approval_lex 不可用：退回全文扫，文档里提到危险词就会被误拦"]
    # 豁免名单对账（审计 L1-B7）：参数 `registered_tools` 此前**完全没用** —— 自检只跑
    # 硬编码用例，于是"名单里写了一个不存在的工具名"没人发现。本项目真发生过同型事故：
    # `_NEVER_PARALLEL_TOOLS` 里写着 "clarify" 而运行时真名是 "ask_user"，
    # 于是"交互工具要串行"的设计意图从未生效。这里把名单与注册表对一次账。
    # 注意：**不进 `ok`**。失效名字不会出现在任何调用里，既不会误拦也不会漏拦，
    # 它属于"维护漂移"而不是"策略失效"—— 把它算成 ok=False 会让界面把
    # "审批好好地在工作"误报成故障。但它必须看得见：进 checks（结构化）+ stderr。
    if registered_tools:
        stale = sorted(set(NON_FS_TOOLS) - set(registered_tools))
        out["checks"]["豁免名单无失效名字"] = (
            not stale, "一致" if not stale else "名单里这些工具不存在: %s" % stale)
        out["stale_tool_names"] = stale
        if stale:
            try:
                print("[approval] 警告：NON_FS_TOOLS 含失效工具名 %s —— 这几条豁免声明从未生效"
                      % stale, file=sys.stderr, flush=True)
            except Exception:
                pass
    err = [x for x in out["checks"].values() if "异常" in str(x[1])]
    if err:
        problems.append("引擎判定内部异常（不是判定不符，是代码出错）：%d 处" % len(err))
    st = specs_state()
    out["specs"] = st
    out["ledger_writable"] = not _LEDGER_ERR["why"]
    if _LEDGER_ERR["why"]:
        out["ledger_error"] = _LEDGER_ERR["why"]
    out["lex_layer"] = _LEX is not None
    out["no_spec"] = not st["loaded"]
    out["spec_failed"] = bool(st["failed"])
    out["strategy_live"] = not bad
    out["problems"] = problems + bad + (
        ["审批规范一个都没加载成功"] if out["no_spec"] else [])
    out["ok"] = bool(out["strategy_live"] and out["ledger_writable"] and problems == []
                     and not out["no_spec"] and not out["spec_failed"])
    ledger({"event": "self_check", "ok": out["ok"], "mode": out["mode"],
            "drive": out["drive"], "port": out["port"], "problems": out["problems"]})
    return out
