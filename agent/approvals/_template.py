# -*- coding: utf-8 -*-
"""模板：新增一类审批规范。

用法：把本文件复制成**不带下划线**的名字（例如 `email_send.py`）即生效 ——
引擎的 `load_specs()` 只跳过以 `_` 开头的模块，所以本文件躺在这里不会生效，
可以放心当骨架留着。

下面以「邮件外发确认」为例。启用时改掉 KIND / TITLE / RISK 与两个判据即可。
契约、ctx 字段、返回语义的完整说明见同目录 README.md。

注意（踩过的坑，改判据时别重犯）：
  · 动作词只认「函数名精确等值」，绝不裸子串 —— `k in w` 会让 json.dumps
    命中「dump」、csv.writer 命中「write」，把只读判成写盘。
  · 先定动作，再取目标；动作判不出来时，目标提得再准也没用。
  · 不硬编码任何本机路径 —— 需要盘符就用 ctx["drive"]。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

KIND = "email.send"          # 稳定标识：会写进账本与免审规则，发布后不要改名
TITLE = "对外发送邮件"          # 审批卡标题里的动词短语
RISK = 1                     # 0 低 / 1 中 / 2 高（高风险排前面、卡片措辞更重）

# ---- 判据原料 ----
_TOOLS = ("send_email", "smtp_send", "mail_send", "sendmail")
_WORDS = ("sendmail", "smtplib", "smtp", "send_message",
          "mailx", "mutt", "sendgrid", "boto3.ses")      # 函数名/模块链的最后一段
_RECIPIENT_KEYS = ("to", "recipients", "recipient", "cc", "bcc", "mail_to")
_QUIET_DOMAINS = ("example.com", "localhost", "test.invalid")   # 测试收件人不打扰


def _last_segments(ctx: Dict[str, Any]) -> List[str]:
    """命令词/函数名链的「最后一段」—— 精确等值用的就是它，绝不裸子串。"""
    f = ctx.get("facts") or {}
    return [str(w).lower().rsplit(".", 1)[-1] for w in (f.get("cmdwords") or [])]


def _recipients(kwargs: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for k in _RECIPIENT_KEYS:
        v = (kwargs or {}).get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
        elif isinstance(v, (list, tuple)):
            out += [str(x).strip() for x in v if str(x).strip()]
    return out


def _is_send(ctx: Dict[str, Any]) -> bool:
    tool = str(ctx.get("tool") or "").lower()
    if tool in _TOOLS:
        return True
    return any(w in _WORDS for w in _last_segments(ctx))


def applies(ctx: Dict[str, Any]) -> bool:
    """这类审批与本次调用相关吗 —— 只做便宜初筛，动作判定在 finding 里。"""
    return _is_send(ctx)


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _is_send(ctx):
        return None
    tos = _recipients(ctx.get("kwargs") or {})
    if not tos:
        return None                     # 提不出收件人 = 不是外发语义，放过

    if all(any(d in t.lower() for d in _QUIET_DOMAINS) for t in tos):
        return {"quiet": True, "action": "发送邮件", "targets": tos,
                "reason": "收件人全在测试域名，记账放行"}

    kw = ctx.get("kwargs") or {}
    subject = str(kw.get("subject") or kw.get("title") or "")[:80]
    return {
        "quiet": False,
        "action": "发送邮件",
        "targets": tos,
        "intent": "向外发送邮件给 %s%s" % (
            "、".join(tos[:4]), ("（主题：%s）" % subject) if subject else ""),
        "reason": "外发动作不可撤回，发送前需主人确认",
        "critical": False,
    }
