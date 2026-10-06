# -*- coding: utf-8 -*-
"""approval_lex.py —— 词法角色切分（修 bug A 的正解）。

一句话：回答「这段文本是**要执行的指令**，还是**被搬运的内容**」。

旧判定直接扫参数全文，于是「我在文档里提到某个禁区词」被当成「我要执行它」，
连构造这个 bug 的测试用例、写交接文档都做不下去（现场复现三次）。

三层角色：
    control   指令层：命令词、flag、重定向符、函数名链、注入位里的二级代码
    operand   操作数层：真正要被读/写/删/移的对象（命令与调用的操作数）
    mention   提及层：正文、注释、文档字符串 —— 只提路径备查，不当作目标

只有 control 参与「绝对禁区」与「动词」判定；只有 operand 参与审批目标。
mention 里出现的路径与危险词，记入账本留痕，但不打断人。

两条不可退让的约束：
  1. 嵌套必须展开。解释器 -c / -Command / --eval 之后的引号内容是**要执行的代码**，
     按提及处理等于给绕过开门（而 python -c 本身就是本机常用写法）。
  2. 解析失败即降级回全文扫（宁误拦不漏拦），并记 lex_fallback。

角色由**命令的性质**决定，不由引号决定：
  echo "cp the file" > d:/x   -> 引号内是数据（echo 的货），> 后的是操作数
  mv "c:/My Docs/a.txt" d:/b  -> 引号只是转义空格，它仍是操作数
所以只有 DATA_COMMANDS 名单里的「输出内容类」命令，其参数才降为提及。
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Tuple

# 参数名表明「这个槽里是要执行的代码」，不是纯文本
CODE_ARG_SLOTS = ("command", "cmd", "code", "script", "implementation_code",
                  "shell", "snippet", "entry_point", "invoke")

# 引号内容紧跟其后 = 二级代码位（必须递归展开，不是载荷）
INJECT_FLAGS = frozenset({
    "-c", "--command", "-e", "--eval", "-command", "-encodedcommand",
    "/c", "/k", "-import", "-p", "--program",
})

# 输出内容类命令：其非重定向参数是「货」，不是文件目标
DATA_COMMANDS = frozenset({
    "echo", "printf", "type", "cat", "head", "tail", "more", "less",
    "set", "export", "title", "write-host", "write-output", "writeline",
    "msg", "comment", "label",
})

# 提权/包装命令：真正的命令词在它们后面
WRAPPERS = frozenset({"sudo", "doas", "env", "time", "nohup", "command", "call",
                 "start", "cmd", "powershell", "pwsh", "bash", "sh", "zsh",
                 "dash", "wsl"})

# 解释器家族：注入位（-c/-e/heredoc/命令替换）里的代码按「喂给谁」选解析器，
# 绝不按 flag 猜。事故实据：`bash -c "rm …"` 因为 -c 在白名单里被送进 python
# 解析器，ast.parse 立刻 SyntaxError，整段退化成「仅操作数」，命令词 rm 丢失，
# 于是删除判不出动作、静默放行 —— 漏判比误判严重，这里必须按解释器走。
PY_INTERPRETERS = frozenset({"python", "python2", "python3", "py", "node", "nodejs",
                             "deno", "perl", "ruby", "php"})
SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd",
                                "powershell", "pwsh", "wsl"})
_CMD_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".ps1")

# Python 里参数应当按 shell 语法递归解析的 sink
# 接收者是字面量或容器字面量时，调的是「数据的方法」而不是「文件系统的方法」：
# "abc".replace(x, y) 是改字符串，os.replace(a, b) 才是移动文件。过去只看末段方法名，
# 于是前者被卡片写成「移动/重命名」—— 动作张冠李戴比不弹更坏。
_DATA_RECV = (ast.Constant, ast.List, ast.Dict, ast.Tuple, ast.Set, ast.JoinedStr)

SHELL_SINKS = frozenset({
    "os.system", "os.popen", "subprocess.run", "subprocess.Popen",
    "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.getoutput", "subprocess.getstatusoutput",
})
# 只读语义的 python 调用：其参数是文本而非目标（避免 print 一句路径就被当成要动它）
# 只读探测与纯字符串计算：参数是「提到的路径」，不是「要动的对象」。
# 事故实据：只为检查文件存在而写的 os.path.join(盘符目录, 文件名) 被判成
# "写入/覆盖 c:/programdata"，卡片目标还被截断失真 —— 拿假措辞让人点批准，
# 比不弹窗更坏：它训练人批准自己看不懂的东西。
SILENT_CALLS = frozenset({
    "print", "pprint", "assert", "len", "format", "repr", "str", "log",
    "exists", "isfile", "isdir", "islink", "lexists", "samefile", "getsize",
    "getmtime", "getatime", "getctime", "join", "split", "splitext",
    "basename", "dirname", "abspath", "realpath", "normpath", "expanduser",
    "expandvars", "getcwd", "listdir", "scandir", "walk", "read", "readline",
    "readlines", "get", "startswith", "endswith", "strip", "splitlines",
})


BS = chr(92)                        # 反斜杠，集中定义避免转义层层失真
ESC_CHARS = chr(34) + chr(36) + chr(96) + BS + chr(10)   # shell 双引号内真正的转义字笡

_RE_BASENAME = re.compile(r"[\\/]([^\\/]+)$")


def _basename(tok: str) -> str:
    m = _RE_BASENAME.search(tok or "")
    return (m.group(1) if m else (tok or "")).lower()


def _new() -> Dict[str, Any]:
    return {"control": [], "operands": [], "mentions": [], "cmdwords": [],
            "open_modes": [], "redirect_write": False, "opaque": [], "segments": [],
            "data_methods": [], "actions": []}


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k in ("control", "operands", "mentions", "cmdwords", "open_modes", "opaque",
              "data_methods", "actions"):
        dst[k].extend(src.get(k) or [])
    for seg in src.get("segments") or []:
        dst["segments"].append(seg)
    if src.get("redirect_write"):
        dst["redirect_write"] = True


# ============================================================
# 1. shell：引号感知的粗切
# ============================================================
def _scan_shell(text: str) -> List[Tuple[str, str]]:
    """返回 (kind, value)：word / sq / dq / op / comment。"""
    out: List[Tuple[str, str]] = []
    i, n = 0, len(text)
    buf = ""

    def flush(kind: str = "word") -> None:
        nonlocal buf
        if buf:
            out.append((kind, buf))
            buf = ""

    while i < n:
        ch = text[i]
        if ch == "'":
            flush()
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            out.append(("sq", text[i + 1:j]))
            i = j + 1
            continue
        if ch == '"':
            flush()
            j, body = i + 1, ""
            while j < n:
                # 双引号内只有 " $ ` \ 与换行是转义字笡；其它地方的 \ 必须保留（否则 c:\Windows 被扯成 c:Windows，路径型禁区词静默失效）
                if (text[j] == BS and j + 1 < n
                        and text[j + 1] in ESC_CHARS):
                    body += text[j + 1]; j += 2; continue
                if text[j] == '"':
                    break
                body += text[j]; j += 1
            out.append(("dq", body))
            i = j + 1
            continue
        if ch == "`":
            flush()
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            out.append(("dq", text[i + 1:j]))          # 反引号：命令替换，按代码位处理
            out.append(("bq", "`"))
            i = j + 1
            continue
        if ch == "$" and text[i:i + 2] == "$(":
            flush()
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            out.append(("subst", text[i + 2:j - 1]))
            i = j
            continue
        if ch == "#" and not buf:
            flush()
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", text[i:j]))
            i = j
            continue
        if ch in ";|\n":
            flush()
            if text[i:i + 2] in ("&&", "||"):
                out.append(("op", text[i:i + 2])); i += 2; continue
            out.append(("op", ch)); i += 1
            continue
        if ch == "&":
            flush(); out.append(("op", "&")); i += 1
            continue
        if ch in "<>":
            flush()
            j = i
            while j < n and text[j] in "012<>&":
                j += 1
            op = text[i:j]
            i = j
            # heredoc：<<MARKER / <<-MARKER / <<'MARKER' / <<"MARKER"
            # 体是「喂给命令的代码」，必须整体收成一个 token。过去 heredoc 体被
            # 当普通文本切碎，里面的每一行都成了新命令段 —— `python - <<'PY' …
            # os.remove(路径) … PY` 的删除动作就是这样从判定里消失的（真实漏判，
            # 主人桌面的文件被删掉而闸门报 pass）。
            if op in ("<<", "<<-"):
                k = i
                while k < n and text[k] in " \t":
                    k += 1
                quote = ""
                if k < n and text[k] in ("'", '"'):
                    quote = text[k]
                    k += 1
                m = k
                if quote:
                    while m < n and text[m] != quote:
                        m += 1
                    marker = text[k:m]
                    k = m + 1 if m < n else n
                else:
                    while m < n and (not text[m].isspace()) and text[m] not in ";|&<>":
                        m += 1
                    marker = text[k:m]
                    k = m
                if marker:
                    nl = text.find("\n", k)
                    if nl >= 0:
                        pos, body_end, end_i = nl + 1, n, n
                        while pos <= n:
                            line_end = text.find("\n", pos)
                            if line_end < 0:
                                line_end = n
                            if text[pos:line_end].strip() == marker:
                                body_end = pos
                                end_i = line_end + 1 if line_end < n else n
                                break
                            if line_end >= n:
                                break
                            pos = line_end + 1
                        # 同行剩余（`cat <<EOF > 目标` 里的 `> 目标`）必须先按普通
                        # token 扫掉，否则重定向目标会连同 heredoc 一起被吞掉 ——
                        # 那会让「写盘」退化成无动作、静默放行。
                        tail = text[k:nl]
                        if tail.strip():
                            out.extend(_scan_shell(tail))
                        out.append(("heredoc", text[nl + 1:body_end]))
                        i = end_i
                        continue
            out.append(("op", op))
            continue
        if ch.isspace():
            flush(); i += 1
            continue
        buf += ch
        i += 1
    flush()
    return out


def _split_segments(toks: List[Tuple[str, str]]) -> List[List[Tuple[str, str]]]:
    segs: List[List[Tuple[str, str]]] = [[]]
    for kind, val in toks:
        if kind == "op" and val in (";", "&&", "||", "|", "\n", "&"):
            if segs[-1]:
                segs.append([])
            continue
        segs[-1].append((kind, val))
    return [s for s in segs if s]


def _interp_kind(cmd: str) -> str:
    """把命令词归到解释器家族；带 .exe/.cmd 后缀的也认（python.exe -c …）。"""
    c = (cmd or "").lower()
    for suf in _CMD_SUFFIXES:
        if c.endswith(suf):
            c = c[:-len(suf)]
            break
    if c in PY_INTERPRETERS:
        return "python"
    if c in SHELL_INTERPRETERS:
        return "shell"
    return ""


def _lex_injected(cmd: str, text: str, depth: int) -> Dict[str, Any]:
    """注入位/heredoc 里的内容按「喂给谁」解析，而不是按 flag 猜。

    未知解释器时两种语法都试，取能解析出东西的那个 —— 宁可多解析一层
    （顶多多拦），也不许因为解析器选错把动作丢掉（漏拦）。
    """
    kind = _interp_kind(cmd)
    if kind == "shell":
        return lex_shell(text, depth)
    if kind == "python":
        r = lex_python(text, depth)
        if r.get("failed"):
            alt = lex_shell(text, depth)
            return alt if not alt.get("failed") else r
        return r
    py = lex_python(text, depth)
    if not py.get("failed") and (py.get("cmdwords") or py.get("operands") or py.get("segments")):
        return py
    return lex_shell(text, depth)


def lex_shell(text: str, depth: int = 0) -> Dict[str, Any]:
    """按 shell 语法切段并标注角色。

    每段产出 segments[] = {"cmd": 命令词, "text": 从命令词起算的文本}，作为
    「命令位」与「操作数位」的分界依据。三条容易漏判的规则在这里兜住：
      · 提权与前缀类命令让出段首（否则整盘删除那种写法配不上禁区短语）；
      · 以 - 或 / 开头的 token 是 flag，绝不充当命令词（否则包装器的 /c 会抢位，
        使「包装器 + 那类关机命令」从命令位上消失）；
      · 注入位优先判定，与命令词是否已定无关（引号里的代码要先展开再看）。

    另产出 actions[] = {"lang","word","args","redirect","text"}：动词与它的
    **直接实参**绑成一个动作点。规范层只从这里取审批目标，不再从全文捞路径 ——
    这是消灭「提到即定罪」的关键一步（旧误报：脚本里一行 SD = '盘符' 常量被
    弹成「对盘符执行写入」，而那次任务与系统盘毫无关系）。
    """
    f = _new()
    if depth > 3 or not (text or "").strip():
        return f
    for seg in _split_segments(_scan_shell(text)):
        cmd = ""
        after = ""                       # 上一个 token（判断注入位）
        redirect = False                 # 刚吃到一个写重定向符
        seg_ctl: List[str] = []          # 从命令词起算的段文本
        seg_args: List[str] = []         # 本段动词的位置参数 = 它要动的对象
        seg_redir: List[str] = []        # > / >> 指向的对象 = 它要写的对象
        seg_first = ""                   # 段内第一个实义 token：bash/sh 这类词既是
                                         # 包装器又是解释器，命令词位空着的话，
                                         # heredoc 体就认不出该按哪种语法解析
        for kind, val in seg:
            if kind == "comment":
                f["mentions"].append(val)
                continue
            if kind == "heredoc":
                # 体是「喂给命令的东西」。角色由命令的性质决定：
                #   命令是解释器      -> 体是代码，按解释器递归解析
                #   命令不是解释器    -> 体是**数据**（cat/tee/mysql 写进文件的内容），
                #                       归提及层；真正的目标是同段的 > 目标或命令自身实参。
                # 旧实现一律按代码处理且解析不动就整段降级成操作数，于是数据里的
                # 字符串常量凭空变成审批目标 —— 已证实的两条误报路径之一就是它。
                probe = cmd or seg_first
                if _interp_kind(probe):
                    sub = _lex_injected(probe, val, depth + 1)
                    _merge(f, sub)
                    f["control"].append(val)
                    if sub.get("failed") or not (sub.get("control") or sub.get("segments")):
                        # 看得懂是代码、却解析不出结构 = 可能被混淆，标不透明由规范层问人。
                        # 不再塞进 operands：那等于拿一段读不懂的文本里的字面量当目标。
                        f["opaque"].append(val[:400])
                        f["failed"] = True
                        f["actions"].append({"lang": "sh", "word": probe, "args": [],
                                             "redirect": [], "opaque": True,
                                             "blob": val[:400],
                                             "text": (probe + " <<BODY").lower()})
                else:
                    f["mentions"].append(val)
                    f["control"].append((cmd or "?") + " <<BODY")
                    f["actions"].append({"lang": "sh", "word": cmd or seg_first or "?",
                                         "args": [], "redirect": [], "data_body": True,
                                         "blob": val[:200],
                                         "text": ((cmd or seg_first or "?") + " <<BODY").lower()})
                after = val
                continue
            if kind == "op":
                # 文件描述符要分清：「>」「>>」「1>」「&>」是写文件；「2>」只是把 stderr
                # 丢进 /dev/null，**不是**改动任何东西。旧实现一律 lstrip("012") 之后判
                # 写成，于是「grep -c 关键词 自留地文件 2>/dev/null」被拖进写路径 ——
                # 实测重放历史调用时它一下制造出几十条「ls/find/grep 改动了引擎文件」的误报。
                digits = val[:len(val) - len(val.lstrip("012"))]
                base = val[len(digits):]
                is_write = base in (">", ">>", "&>") and digits != "2"
                if is_write:
                    # redirect_write 改在**取到目标时**置位（见下面两处 append）：
                    # 目标若是空设备（> NUL）就不算写，这里先置位会撤不回来。
                    seg_ctl.append(val)
                    redirect = True
                elif val in ("<", "<<", "<<<"):
                    seg_ctl.append(val)
                    redirect = False
                after = ""
                continue
            if kind == "subst":           # 命令替换里的内容是要执行的代码
                _merge(f, lex_shell(val, depth + 1))
                continue
            if kind == "bq":              # 反引号的收尾标记，本身无信息
                continue
            low = val.lower()
            # ---- 注入位优先：紧跟代码注入 flag 的引号内容是二级代码 ----
            if kind in ("sq", "dq") and after and after.lower() in INJECT_FLAGS:
                sub = _lex_injected(cmd or seg_first, val, depth + 1)
                _merge(f, sub)
                f["control"].append(val)
                if sub.get("failed") or not (sub.get("control") or sub.get("segments")):
                    # 看不懂的代码不许静默放行：标成不透明动作点交给规范层问人。
                    # 旧写法是整段并入 operands（= 全文扫），那会把代码里的常量当目标。
                    f["opaque"].append(val[:400])
                    f["failed"] = True
                    f["actions"].append({"lang": "sh", "word": cmd or "?", "args": [],
                                         "redirect": [], "opaque": True,
                                         "text": ((cmd or "?") + " " + after).lower()})
                after = val
                continue
            if not cmd:
                bare = _basename(val.split("=")[0])
                if bare.startswith("-") or (len(bare) == 1 and val.startswith("/")):
                    f["control"].append(val)      # flag 不充当命令词，但可成为注入锚点
                    after = val
                    continue
                if bare in WRAPPERS:
                    f["cmdwords"].append(bare)
                    f["control"].append(val)      # 前缀不进段文本，真命令词才是段首
                    seg_first = seg_first or bare
                    after = val
                    continue
                if kind in ("sq", "dq"):
                    f["mentions"].append(val)     # 段首就是引号：是内容不是命令
                    after = val
                    continue
                cmd = bare
                seg_first = seg_first or bare
                f["cmdwords"].append(cmd)
                f["control"].append(val)
                seg_ctl.append(val)
                after = val
                continue
            # ---- 已有命令词：定本 token 的角色 ----
            if kind in ("sq", "dq"):
                if redirect:
                    f["control"].append(val)
                    seg_ctl.append(val)
                    if _is_null_device(val):
                        pass                       # 空设备：不是写（见 _is_null_device）
                    else:
                        f["operands"].append(val)  # 重定向目标带引号也是真目标
                        seg_redir.append(val)
                        f["redirect_write"] = True
                elif cmd in DATA_COMMANDS:
                    f["mentions"].append(val)
                else:
                    f["operands"].append(val)
                    f["control"].append(val)
                    seg_ctl.append(val)
                    seg_args.append(val)
                after = val
                continue
            f["operands"].append(val)              # 未引号：一律保守当操作数
            f["control"].append(val)
            seg_ctl.append(val)
            if redirect:
                # 空设备上的重定向不是改文件（NUL / /dev/null）—— 既不算目标、
                # 也不置 redirect_write（fs_drive 在消费那个旗标）。
                if not _is_null_device(val):
                    seg_redir.append(val)
                    f["redirect_write"] = True
            else:
                seg_args.append(val)
            if low in ("-encodedcommand", "/encodedcommand"):
                f["opaque"].append(low)
            after = val
        if seg_ctl:
            f["segments"].append({"cmd": cmd, "text": " ".join(seg_ctl).lower()})
        if cmd or seg_args or seg_redir:
            f["actions"].append({"lang": "sh", "word": cmd or "?", "args": seg_args,
                                 "redirect": seg_redir,
                                 "text": " ".join(seg_ctl).lower()})
    return f


# ============================================================
# 2. python：AST 角色标注（注释天然被丢弃）
# ============================================================
def _chain(node: ast.AST) -> str:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts)).lower()


def _is_path_join(chain: str) -> bool:
    """是不是**路径拼接**（os.path.join / ntpath.join / Path.joinpath …）。

    刻意不认 `" ".join([...])`：那是字符串方法，产物是**数据**不是路径。
    """
    c = (chain or "").lower()
    last = c.rsplit(".", 1)[-1]
    if last not in ("join", "joinpath"):
        return False
    return ("path" in c) or last == "joinpath"


def _join_text(node: ast.AST, varmap: Optional[Dict[str, str]] = None) -> Optional[str]:
    """`os.path.join(a, b, …)` 折叠成一条完整路径文本；折不出来就返回 None。

    为什么必须**折全**而不是只折第一段（2026-10 主人实测翻车）：
    旧实现 `return _text_of(node.args[0])` 把 `os.path.join('agent_workspace','c1b.txt')`
    折成了 `agent_workspace`（只剩目录）—— 再被操作数层那条「token 里得有 . 或 /」
    的过滤丢掉，于是路径经变量传入 open() 时目标为空，工作区档把**区内合法写入**
    判成"无法判定"而整条拒掉（C1b/C2 全灭的根因）。

    只认「全部实参都折得出来」的情形（常量、f-string、常量加法、**已知常量变量**）：
    任一段折不出来就整体放弃 —— 拼出一个"看着像路径"的假目标比认不出来更糟。
    """
    parts = [_text_of(a, varmap) for a in (node.args or [])]
    if not parts or any(p is None for p in parts):
        return None
    clean = [str(p).replace(BS, "/").rstrip("/") for p in parts if str(p).strip()]
    return "/".join(clean) if clean else None


def _text_of(node: ast.AST, varmap: Optional[Dict[str, str]] = None) -> Optional[str]:
    """尽量把「字符串常量 / 常量拼接 / f-string / 常量列表 / **已知常量变量**」还原成文本。

    2026-10 工作区档实测（区内合法写入被弹卡）后扩了两条，取向都是「折得全才算数」：

      · **认变量**：`d = 'agent_workspace'` 之后 `open(os.path.join(d, 'x.txt'), 'w')`
        也要折得出来。旧实现只认字面量，路径经变量传一次（哪怕这个变量在同一段代码里
        就是字面量）就判"目标无法判定"，于是**区内写入弹卡**。变量表在 `_const_vars`
        里按**源码顺序**累积，所以 `d` 一定先于用到它的那一行入表。
      · **f-string / 加法必须整段可折**：旧实现只把其中的常量片段拼起来，把
        `f"{d}/x.txt"` 折成 `/x.txt` 这种**半截路径**。半截路径是 fail-open：
        `f'agent_workspace/{sub}/x'` 会折成 `agent_workspace//x`（看着在区内），
        而真实目标可能被 `sub` 里的 `../..` 带到区外。折不全就返回 None，
        交给上层「认不出就交人确认」—— 宁可多问一句，不猜。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        v = node.value
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else v
    if isinstance(node, ast.Name):
        # 变量查表：表里存的是**已经折好的字符串**，所以不存在递归展开的问题
        return varmap.get(node.id) if varmap else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text_of(node.left, varmap), _text_of(node.right, varmap)
        if a is not None and b is not None:
            return a + b
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        # pathlib 的 `/` 拼接（`Path('agent_workspace') / 'a.txt'`）。数字除法折不出来
        # （_text_of 只认字符串常量），所以这里不会把 `4 / 2` 当路径。
        a, b = _text_of(node.left, varmap), _text_of(node.right, varmap)
        if a is not None and b is not None:
            return str(a).replace(BS, "/").rstrip("/") + "/" + str(b).replace(BS, "/").lstrip("/")
        return None
    if isinstance(node, ast.JoinedStr):
        out: List[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                out.append(piece.value)
                continue
            if isinstance(piece, ast.FormattedValue):
                inner = _text_of(piece.value, varmap)
                if inner is None:
                    return None             # 有一段折不出来 → 整条不折（半截路径 = fail-open）
                out.append(inner)
                continue
            return None
        return "".join(out) if out else None
    if isinstance(node, (ast.List, ast.Tuple)):
        return " ".join(x for x in (_text_of(e, varmap) for e in node.elts) if x)
    if isinstance(node, ast.Call):
        chain = _chain(node.func)
        if _is_path_join(chain):
            return _join_text(node, varmap)
        # `Path('路径')` 本身就是路径（下面 _open_target 记变量时也用它）
        if chain.rsplit(".", 1)[-1] in _PATH_CTORS and len(node.args) == 1:
            return _text_of(node.args[0], varmap)
    return None


# 返回字符串的内置方法：链式调用的接收者若由它们产出，则本次调用是「改数据」，
# 不是「改文件」。旧实现只把字面量容器（"x".replace / [].append）认作数据，
# 于是 `cmd.lower().replace('a','b')` 被当成移动文件 —— 实测它让一次与文件
# 毫无关系的分析脚本背上了「移动/重命名」的动作。
STR_METHODS = frozenset({
    "lower", "upper", "strip", "lstrip", "rstrip", "title", "capitalize", "casefold",
    "swapcase", "expandtabs", "format", "zfill", "ljust", "rjust", "center",
    "split", "rsplit", "splitlines", "join", "partition", "rpartition",
    "encode", "decode", "removeprefix", "removesuffix", "translate", "expandtabs",
})


def _recv_is_text(node: Optional[ast.AST], varmap: Dict[str, str]) -> bool:
    """接收者是不是「一段文本/一个数据容器」（而非模块或对象）。"""
    if node is None:
        return False
    if isinstance(node, _DATA_RECV):
        return True
    if isinstance(node, ast.Call):
        return _chain(node.func).split(".")[-1] in STR_METHODS
    if isinstance(node, ast.Name):
        v = varmap.get(node.id)
        return v is not None and not any(sep in v for sep in ("/", chr(92), ":"))
    if isinstance(node, ast.Attribute):
        return _recv_is_text(node.value, varmap)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _recv_is_text(node.left, varmap) or _recv_is_text(node.right, varmap)
    return False


def _is_null_device(token: str) -> bool:
    """这个重定向目标是不是**空设备**（NUL / /dev/null / $null …）。

    2026-10 加：空设备上的重定向**不是改文件** —— 数据直接丢弃。旧实现把它当写，
    于是 `find … 2>/dev/null; echo …` 这种**纯只读**命令在仅读档被判"含无法判定的动作"
    整条拒掉（主人实测报的就是这条）。

    注意分词层会把 `2>/dev/null` 拆成 `2`、`>`、`/dev/null` 三个 token，
    所以 373 行那句「digits != '2'」等不到 —— 只能在**取目标**这一层判空设备。
    """
    v = str(token or "").strip().lower().replace(BS, "/").rstrip(":")
    return v in ("nul", "/dev/null", "dev/null", "$null", "/dev/zero",
                 "/dev/stdout", "/dev/stderr")


def recv_texts(node: Any, varmap: Dict[str, str]) -> List[str]:
    """接收者表达式里的文本常量。

    为了接住 pathlib 这一族写法：`Path(盘内文件).unlink()`、`Path(p).write_text(x)`
    —— 目标挂在**接收者**上，实参里一个字都没有。只按实参绑定的话，这一族会
    整族漏判（实测用例「heredoc python unlink」就是这么掉的）。
    """
    out: List[str] = []
    cur = node
    for _ in range(4):                       # 只往上剥几层，不递归展开
        if cur is None:
            break
        # 先试「整段折成文本」：变量、`Path(a) / b`、`os.path.join(...)` 都在这一步拿下
        whole = _text_of(cur, varmap)
        if whole:
            out.append(whole)
            break
        if isinstance(cur, ast.Call):
            # `open('路径', 'w').write(内容)` 这种链式写法：接收者自己就是 open 调用，
            # 只有**第一个**实参是路径，第二个是模式。旧实现把全部实参当候选，
            # 于是 `open(p, 'w')` 在 p 认不出时会把模式串 "w" 当成路径候选（实测），
            # 目标层拿到一个不是路径的 token → 判"无法判定"。
            _args = cur.args[:1] if _chain(cur.func).endswith("open") else cur.args
            for a in _args:
                s = _text_of(a, varmap)
                if s is None and isinstance(a, ast.Name):
                    s = varmap.get(a.id)
                if s:
                    out.append(s)
            cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
        elif isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Name):
            v = varmap.get(cur.id)
            if v:
                out.append(v)
            break
        else:
            break
    return out


# 路径对象构造器：`Path('路径')` / `PurePath(...)`。它们的实例**内容就是路径**，
# 与 open() 的文件对象同理（`p.write_text(x)` 的目标挂在接收者 p 上）。
_PATH_CTORS = frozenset({"path", "purepath", "windowspath", "posixpath",
                         "purewindowspath", "pureposixpath"})


def _open_target(node: ast.AST, varmap: Optional[Dict[str, str]] = None) -> Optional[str]:
    """`f = open('路径', 'w')` / `p = Path('路径')` -> 那条路径（文件对象的"内容"就是它包的路径）。

    2026-10 补：不记这一条，`f.write(内容)` 的**接收者**就解析不出来 —— 目标为空，
    目标层判"无法判定"，工作区档因此把 `f.write` 的合法写入也拒了（主人实测 C1''）。
    记进来之后 `recv_texts(Name f)` 能取到路径，目标重新落在接收者上（正确语义）。

    2026-10 再补（本轮实测「读->写回 MEMORY.md 弹卡」）：
      · `with open(路径) as f:` 这种绑定也要记 —— 它和 `f = open(...)` 是同一件事，
        漏了它，with 写法里的 `f.write(...)` 就还是"认不出接收者"；
      · `p = Path(路径)` 同理（pathlib 那一族的接收者目标此前整族解析不出来）；
      · `Path(路径).open('w')` 的路径在**接收者**上，不在实参里（实参是模式）。
    """
    if not isinstance(node, ast.Call):
        return None
    last = _chain(node.func).rsplit(".", 1)[-1]
    if last == "open":
        if isinstance(node.func, ast.Attribute):
            # Path(x).open('w') / os.open(...)：路径在接收者表达式里
            return _open_target(node.func.value, varmap) or _text_of(node.func.value, varmap)
        return _text_of(node.args[0], varmap) if node.args else None
    if last in _PATH_CTORS and len(node.args) == 1:
        return _text_of(node.args[0], varmap)
    return None


def _const_vars(tree: ast.AST) -> Dict[str, str]:
    """常量变量表：`p = '路径'` / `f = open('路径')` / `p = Path('路径')` 之后再
    经变量使用它，路径仍要认得出来 —— 但只认「它作为某个动词的直接实参」这一次出现。
    旧实现靠「operands 为空就采纳 mentions」的全局回退兜这个场景，代价是把代码里
    任何被提到的路径都变成审批目标（漏判补丁制造误报的典型）。

    **按源码顺序累积**（2026-10 实测）：`d = 'agent_workspace'` 必须先进表，
    后面那行 `p = os.path.join(d, 'x.txt')` 才折得出来。`ast.walk` 是广度优先、
    不保证源码顺序，所以这里显式按 lineno 排序。
    """
    out: Dict[str, str] = {}
    nodes = [n for n in ast.walk(tree)
             if isinstance(n, (ast.Assign, ast.With, ast.AsyncWith))]
    nodes.sort(key=lambda n: getattr(n, "lineno", 0))
    for node in nodes:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            s = _text_of(node.value, out)
            if s is None:
                s = _open_target(node.value, out)   # f = open('路径') / p = Path('路径')
            if s is not None:
                out[node.targets[0].id] = s
            continue
        # with open('路径') as f:  —— 与赋值同义，漏掉它 with 写法整段认不出接收者
        for item in getattr(node, "items", []):
            if not isinstance(item.optional_vars, ast.Name):
                continue
            s = _open_target(item.context_expr, out)
            if s is not None:
                out[item.optional_vars.id] = s
    return out


def lex_python(text: str, depth: int = 0) -> Dict[str, Any]:
    f = _new()
    if depth > 3:
        return f
    try:
        tree = ast.parse(text or "")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return {"failed": True}
    varmap = _const_vars(tree)

    def arg_text(a: ast.AST):
        """实参 -> (文本, 是否经变量)。常量折叠沿用 _text_of，额外支持查表。"""
        # 裸变量名先查表并**保留「经变量」标记**（_text_of 也会查表，但会丢掉这个标记）
        if isinstance(a, ast.Name) and a.id in varmap:
            return varmap[a.id], True
        s = _text_of(a, varmap)
        if s is not None:
            return s, False
        if isinstance(a, ast.Call) and _is_path_join(_chain(a.func)):
            # 折**全部**实参（旧版只取 args[0]，见 _join_text 的说明）
            folded = _join_text(a, varmap)
            if folded is not None:
                return folded, False
        return None, False

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            s = _text_of(node.value)
            if s is not None:
                f["mentions"].append(s)        # 赋给变量的文本 = 内容，不是目标
            continue
        if isinstance(node, ast.Call):
            chain = _chain(node.func)
            f["cmdwords"].append(chain)
            f["control"].append(chain)
            if chain in SHELL_SINKS:
                for a in node.args[:1]:
                    s = _text_of(a)
                    if s:
                        _merge(f, lex_shell(s, depth + 1))
                continue
            last = chain.split(".")[-1]
            _recv = getattr(node.func, "value", None)
            data_recv = isinstance(node.func, ast.Attribute) and _recv_is_text(_recv, varmap)
            silent = data_recv or (last in SILENT_CALLS) or (chain in SILENT_CALLS)
            if data_recv:
                f["data_methods"].append(chain)
            act_args: List[str] = []
            act_kw: Dict[str, str] = {}
            via_var = False
            for a in node.args:
                s, via = arg_text(a)
                if s is None:
                    continue
                via_var = via_var or via
                act_args.append(s)
                (f["mentions"] if silent else f["operands"]).append(s)
                if not silent:
                    f["control"].append(s)
            for kw in node.keywords:
                s, via = arg_text(kw.value)
                if s is None:
                    continue
                via_var = via_var or via
                act_kw[kw.arg or ""] = s
                if (kw.arg or "") in ("file", "path", "filename", "cwd",
                                      "dest", "src", "target", "command", "mode"):
                    f["operands"].append(s)
                    f["control"].append(s)
                else:
                    f["mentions"].append(s)
            omode = ""
            # mode_given：源码里**给过**模式（第二个实参或 mode=）。它和「模式认不出」
            # 必须分开：`open(p)` 不写模式 = 默认只读（读，不是写）；而 `open(p, m)`
            # 里 m 认不出来 = **不知道**是不是写 —— 后者要保守（交人确认），
            # 前者不能当成写（2026-10 实测：读一下文件就弹卡就是这么来的）。
            omode_given = False
            if chain.endswith("open"):
                if len(node.args) > 1:
                    omode_given = True
                    omode = (_text_of(node.args[1], varmap) or "").lower()
                for kw in node.keywords:
                    if kw.arg == "mode":
                        omode_given = True
                        omode = (_text_of(kw.value, varmap) or "").lower()
                if omode:
                    f["open_modes"].append(omode)
                    f["control"].append("mode:" + omode)
                    if any(c in omode for c in "wax+"):
                        f["redirect_write"] = True
            if not silent and (act_args or act_kw or recv_texts(_recv, varmap)):
                f["actions"].append({"lang": "py", "word": chain, "args": act_args,
                                     "kw": act_kw, "mode": omode, "mode_given": omode_given,
                                     "recv": _recv_is_text(_recv, varmap),
                                     "recv_paths": recv_texts(_recv, varmap),
                                     "via_var": via_var})
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node.names:
                f["control"].append(node.names[0].name.lower())
    return f


# ============================================================
# 3. 入口：按工具与参数名分派
# ============================================================
_SHELL_SLOTS = ("command", "cmd", "shell", "bash")
_PY_SLOTS = ("code", "implementation_code", "script", "snippet")
_PATH_HINTS = ("path", "file", "dir", "cwd", "workdir", "target",
               "dest", "output", "input", "filename", "folder")


def _tool_lang(tool: str) -> str:
    """工具名里带的语言线索。分派以参数名为准，工具名只作兜底，
    免得将来出现 body/payload 这类没登记过的代码槽时整段不解析。"""
    t = (tool or "").lower()
    for k in ("shell", "bash", "powershell", "pwsh", "cmd"):
        if k in t:
            return "shell"
    for k in ("python", "py"):
        if k in t:
            return "python"
    return ""


def _kind_of(tool: str, slot: str) -> str:
    s = (slot or "").lower()
    if s in _SHELL_SLOTS:
        return "shell"
    if s in _PY_SLOTS:
        return "python"
    if s in CODE_ARG_SLOTS:
        return _tool_lang(tool) or "shell"
    # 名字里带 path/file/dir 的槽 = 纯路径参数，不必解析语法
    if any(h in s for h in _PATH_HINTS):
        return "path"
    # 剩下的槽：若它挂在一个执行类工具上，按工具名兜底，别当纯文本放过
    return _tool_lang(tool) or "text"


def lex(tool: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """-> control / operands / mentions / cmdwords / open_modes / redirect_write /
           actions / opaque / failed + 三段拼好的文本。

    只负责分层与结构提取，不做任何判定。判定仍归 approval.py 与 approvals/ 规范。

    actions[] 是本模块对规范层的**唯一新增承诺**：每个动作点都带着「动词 +
    它的直接实参」，规范层因此可以说「是这个动词要吃这些文件」，而不是
    「这段文本里同时出现过动词和路径」。
    """
    f = _new()
    failed = False
    lang_hint = _tool_lang(tool)
    for slot, val in (kwargs or {}).items():
        if val is None or isinstance(val, bool):
            continue
        items = ([str(x) for x in val.values()] if isinstance(val, dict)
                 else [str(x) for x in val] if isinstance(val, (list, tuple))
                 else [str(val)])
        kind = _kind_of(tool, slot)
        for text in items:
            if kind == "shell":
                _merge(f, lex_shell(text))
            elif kind == "python":
                r = lex_python(text)
                if r.get("failed"):
                    failed = True
                    # 语法都过不去的代码不许静默放行：留成不透明动作点，
                    # 由规范层问人。旧写法是整段进 operands —— 那等价于
                    # 退回全文扫，把代码里的字符串常量当成审批目标。
                    f["opaque"].append(text[:400])
                    f["actions"].append({"lang": "py", "word": "?unparsed",
                                         "args": [], "kw": {}, "opaque": True,
                                         "blob": text[:400],
                                         "text": (slot + " <unparsed>")})
                else:
                    _merge(f, r)
            elif kind == "path":
                f["operands"].append(text)
            else:
                hints = any(h in (slot or "").lower() for h in _PATH_HINTS)
                (f["operands"] if hints else f["mentions"]).append(text)
    f["failed"] = failed or bool(f.get("failed"))
    f["control_text"] = " ".join(f["control"]).lower()
    f["operand_text"] = " ".join(f["operands"])
    f["mention_text"] = " ".join(f["mentions"])
    f["cmdwords"] = sorted(set(f["cmdwords"]))
    f["open_modes"] = sorted(set(f["open_modes"]))
    f["operands"] = sorted(set(f["operands"]))
    f["mentions"] = sorted(set(f["mentions"]))
    f["control"] = sorted(set(f["control"]))
    f["segments"] = f["segments"][:40]
    # 动作点保序（方向判据要按参数位置取），只去重、限量
    seen, acts = set(), []
    for a in f["actions"]:
        key = (a.get("lang"), a.get("word"), tuple(a.get("args") or ()),
               tuple(sorted(tuple(a.get("redirect") or []) + tuple((a.get("kw") or {}).keys()))))
        if key in seen:
            continue
        seen.add(key)
        acts.append(a)
    f["actions"] = acts[:40]
    return f
