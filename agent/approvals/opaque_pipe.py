# -*- coding: utf-8 -*-
"""规范：把「编码后的内容」灌进解释器执行 —— 目标无法静态判定，需人工确认。

补的缺口（2026-09-11 安全审计 probe5，实测）：

    echo cm0gLXJmIEM6L1VzZXJzL0FTVVMvLnNzaA== | base64 -d | bash

判 **pass**；把同一段解码后直写（`rm -rf C:/Users/ASUS/.ssh`）判 **block**。
差的只是一个 base64 —— 这不是「绕过技巧」，而是**判据看不到目标**：
管道里流的是数据，静态层读不出它要做什么。

取向刻意很窄：**只问编码/混淆这一种**。
普通 `cat script.sh | bash` 不问 —— 那是本机脚本执行，日常操作且内容可见；
问它只制造噪音，而噪音会把人训练成闭眼点「允许」（本项目反复吃过的教训）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

KIND = "opaque.pipe"
TITLE = "执行编码后的内容"
RISK = 2

# 解码/还原工具：出现它们意味着「管道里流的不是人能读的形态」
_DECODERS = ("base64", "base32", "xxd", "uudecode", "uuencode", "certutil",
             "openssl", "frombase64string")
# 解释器：末端是它们，内容就会被执行/求值
_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "dash", "ksh", "csh", "tcsh", "fish",
    "python", "python3", "py", "perl", "ruby", "php", "node", "nodejs",
    "pwsh", "powershell", "cmd", "wsl", "awk",
})
# 长 base64 样式 token（没有解码器命令时的旁证）
_B64_TOKEN = re.compile(r"^[A-Za-z0-9+/]{40,}={0,2}$")


def _looks_b64(t: str) -> bool:
    """像不像真的 base64 —— 不是「四十个字母数字就算」。

    实测误报（2026-09-20 审计）：纯数字长 ID、小写十六进制哈希（git 对象名/SHA）
    都满足原正则，于是 cat <40位数字> | python 被判「疑似 base64 长串」。
    真 base64 的判别特征：含大写字母，或含 +。
    """
    if not _B64_TOKEN.match(t):
        return False
    if t.isdigit():
        return False                      # 纯数字：ID / 时间戳
    if re.match(r"^[0-9a-fA-F]{32,}$", t):
        return False                      # 十六进制哈希：md5 / sha / git 对象名
    return any(c.isupper() for c in t) or "+" in t


def _cmd_of(seg: str) -> str:
    """段文本 -> 命令词（取首 token，末段去后缀，大小写无关）。"""
    for tok in (seg or "").split():
        t = tok.strip().strip('"').strip("'")
        if not t or t.startswith("-"):
            continue
        head = t.split("=", 1)[0] if ("=" in t and "/" not in t.split("=", 1)[0]) else t
        head = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return head.rsplit(".", 1)[0] if "." in head else head
    return ""


_RAW_SLOTS = ("command", "cmd", "script", "code", "snippet")


def _raw_text(ctx: Dict[str, Any]) -> str:
    """工具参数的**原始**文本。

    管道符 `|` 不会出现在 control_text 里 —— 词法层按管道分段，分隔符被丢掉。
    实测 `echo <b64> | base64 -d | bash` 的 control_text 是
    `echo <b64> base64 -d bash`，于是「管道判据」整条失效。
    原始参数是唯一还看得出「谁接了谁」的地方。
    """
    kw = ctx.get("kwargs") or {}
    for slot in _RAW_SLOTS:
        v = kw.get(slot)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def applies(ctx: Dict[str, Any]) -> bool:
    """便宜初筛：没有管道就不必看。"""
    return "|" in (_raw_text(ctx) + " " + str(ctx.get("control") or ""))


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # 管道只存在于原始参数里（词法层的 control_text 已把分隔符丢掉）
    ctl = (_raw_text(ctx) + " " + str(ctx.get("control") or "")).strip()
    if "|" not in ctl:
        return None
    segs = [s.strip() for s in re.split(r"[|;&]", ctl) if s.strip()]
    if len(segs) < 2:
        return None
    tail = _cmd_of(segs[-1])
    if tail not in _INTERPRETERS:
        return None
    low = ctl.lower()
    # 只认**命令词位**的解码器。曾是整条命令裸子串匹配 ——
    # 于是 echo "decode base64 below" | awk ... 被判「上游出现解码器」（正文提到即命中）。
    decoders = sorted({_cmd_of(seg) for seg in segs if _cmd_of(seg) in _DECODERS})
    if "frombase64string" in low:        # PowerShell 方法名形态，不是命令词
        decoders = sorted(set(decoders) | {"frombase64string"})
    b64 = [t for seg in segs for t in seg.split() if _looks_b64(t)]
    if not decoders and not b64:
        return None
    how = ("上游出现解码器：%s" % "、".join(decoders[:3])) if decoders \
        else "上游出现疑似 base64 长串"
    return {
        "quiet": False,
        "action": "执行编码内容",
        "targets": [],                      # 目标确实无法确定，不许编一个给人看
        "intent": "把一段编码内容交给 %s 执行（内容无法静态判读）" % tail,
        "reason": "管道末端是解释器（%s），%s —— 解码后的内容可以是任意命令" % (tail, how),
        "notes": ["⚠️ 目标未能确定：解码结果可能是任何东西（含删改系统盘）",
                  "放行前建议让 AB 先把解码结果原文打出来给你看"],
        "critical": True,
    }
