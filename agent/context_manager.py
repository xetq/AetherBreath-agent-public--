# -*- coding: utf-8 -*-
"""上下文管理器 —— 原文不动，压缩只生产「视图」。

设计：agent_workspace/上下文管理器/DESIGN.md
分层：
  P1（本阶段）纯函数核心：切轮 / 分段 / 清理矩阵 / 触发判定 —— 无 IO、无状态、可独立单测
  P2 落盘与命中：.condensed_sessions/{sid}.json + 指纹 + 原子写（追加）
  P3 agent.py 接线

三条铁律（改本文件前先读 DESIGN.md §5）：
  1) 绝不删除/重排消息 —— 只收缩字段内容，保证 tool_calls 与 tool 配对永不断头
  2) user 消息原话永不动（审计与可追责底线）
  3) 幂等：build_view(build_view(x)) == build_view(x)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ========== 0. 参数 ==========

DEFAULT_PARAMS: Dict[str, Any] = {
    "enabled": False,                 # 施工惯例：先接线默认关，实测通过后置 true
    "window_tokens": 1_000_000,       # 主人 2026-09-13 定：1M
    "trigger_ratio": 0.55,
    "keep_recent_rounds": 10,         # 保护段 = 最近 N 轮
    "keep_recent_thinking_rounds": 3,  # 最近 N 轮保留 reasoning_content
    "cooldown_rounds": 2,             # 压缩后至少隔 N 轮再压（防抖）
    "clear_tool_args": True,
    "arg_preview_chars": 120,
    "tool_result_preview_chars": 200,  # 供将来「折叠段保留前 N 字符」用
    "protect_first_round": True,
    "absolute_words": ["不要", "必须", "禁止", "不对", "错了", "别再", "停止", "记住"],
    "model_windows": {"default": 1_000_000},
}

# 占位符前缀：用于幂等识别（已折叠的内容不再二次折叠）
FOLD_MARKS = ("[已折叠", "[进度汇报已折叠")


def load_params(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并默认参数与 config.yaml 的 context_manager 段（缺键向后兼容）。

    注意 model_windows 是「整表替换」语义：配置里写了就用配置那张表，
    表内未命中的模型 → 表的 default → window_tokens。
    """
    p = json.loads(json.dumps(DEFAULT_PARAMS))          # 深拷贝，避免污染默认值
    for k, v in (cfg or {}).items():
        p[k] = v                                        # dict 值同样整体替换（表语义）
    return p


def window_for(params: Dict[str, Any], model: Optional[str] = None) -> int:
    """模型窗口：model_windows[model] → model_windows['default'] → window_tokens。"""
    mw = params.get("model_windows") or {}
    if model and isinstance(mw.get(model), int):
        return int(mw[model])
    if isinstance(mw.get("default"), int):
        return int(mw["default"])
    return int(params.get("window_tokens") or 1_000_000)


# ========== 1. 切轮（主人定义：一条 user → 下一条 user 之前算一轮） ==========

def iter_rounds(messages: List[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """切成轮，返回 [(start, end), ...]（半开区间）。

    审批/ask_user 不额外算轮；被打断也算一轮（user 消息已发出）。
    user 之前若有前导消息（正常流程不会有），并入首轮，保证不丢消息。
    """
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not starts:
        return [(0, len(messages))] if messages else []
    if starts[0] != 0:
        starts[0] = 0          # 前导消息并入首轮，而不是自成一轮
    out = []
    for k, s in enumerate(starts):
        out.append((s, starts[k + 1] if k + 1 < len(starts) else len(messages)))
    return out


def _round_hits_words(msgs: List[Dict[str, Any]], words: List[str]) -> bool:
    """该轮的用户消息里是否出现绝对词/纠错词（规则识别，不做语义判断）。"""
    for m in msgs:
        if m.get("role") != "user":
            continue
        text = m.get("content")
        if isinstance(text, str) and any(w and w in text for w in words):
            return True
    return False


def protected_rounds(messages: List[Dict[str, Any]], rounds: List[Tuple[int, int]],
                     params: Dict[str, Any]) -> List[int]:
    """保护段 = 最近 keep_recent_rounds 轮 + 首轮 + 含绝对词/纠错的轮。"""
    n = len(rounds)
    if n == 0:
        return []
    keep = max(0, int(params.get("keep_recent_rounds") or 0))
    prot = set(range(max(0, n - keep), n))
    if params.get("protect_first_round", True):
        prot.add(0)
    words = [w for w in (params.get("absolute_words") or []) if w]
    if words:
        for r, (a, b) in enumerate(rounds):
            if _round_hits_words(messages[a:b], words):
                prot.add(r)
    return sorted(prot)


# ========== 2. 字段收缩（清理矩阵，DESIGN.md §5.3） ==========

_PURPOSE_KEYS = ("command", "query", "path", "url", "code", "prompt", "question", "file_path", "name")


def _is_folded(text: Any) -> bool:
    return isinstance(text, str) and text.startswith(FOLD_MARKS)


def _preview_of(args: Any, n: int) -> str:
    """从工具参数里取一段"目的"预览：优先语义键，否则首个字符串值，再否则原文。"""
    s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    val = ""
    try:
        d = json.loads(s)
    except Exception:
        d = None
    if isinstance(d, dict):
        for k in _PURPOSE_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                val = v
                break
        if not val:
            for v in d.values():
                if isinstance(v, str) and v.strip():
                    val = v
                    break
    if not val:
        val = s or ""
    val = re.sub(r"\s+", " ", val).strip()
    return val[:n] + ("…" if len(val) > n else "")


def _clear_arguments(tc: Dict[str, Any], preview_chars: int) -> Dict[str, Any]:
    """工具参数 → {"_cleared": "工具名: 目的预览"}（保持 id/name 结构不变）。"""
    fn = tc.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str) and "_cleared" in args:
        return tc                                    # 已清过，幂等
    name = fn.get("name") or "?"
    return {
        "id": tc.get("id"),
        "type": tc.get("type") or "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"_cleared": f"{name}: {_preview_of(args, preview_chars)}"},
                                    ensure_ascii=False),
        },
    }


def shrink_message(msg: Dict[str, Any], *, protected: bool, keep_thinking: bool,
                   idx: int, params: Dict[str, Any]) -> Dict[str, Any]:
    """按清理矩阵收缩单条消息（结构不变，只动字段内容）。"""
    role = msg.get("role")

    if role == "user":                                # 铁律 2：原话永不动
        return {"role": "user", "content": msg.get("content", "")}

    if role == "tool":
        content = msg.get("content", "")
        if not protected and not _is_folded(content):
            content = f"[已折叠 {len(content or '')} 字符｜原文 working_memory idx={idx}｜restore_context 可取回]"
        return {"role": "tool", "tool_call_id": msg.get("tool_call_id"), "content": content}

    if role == "assistant":
        content = msg.get("content")
        tcs = msg.get("tool_calls")
        out: Dict[str, Any] = {"role": "assistant", "content": content}
        if tcs:
            out["tool_calls"] = ([_clear_arguments(tc, int(params.get("arg_preview_chars") or 120))
                                  for tc in tcs]
                                 if params.get("clear_tool_args", True) else tcs)
            # 折叠段里带 tool_calls 的 assistant 正文 = 进度汇报（每轮最终回答保留原文）
            if not protected and isinstance(content, str) and content.strip() and not _is_folded(content):
                out["content"] = f"[进度汇报已折叠 {len(content)} 字符]"
        if keep_thinking:
            rc = msg.get("reasoning_content")            # 只在最近 N 轮保留思考原文
            if rc:
                out["reasoning_content"] = rc
        return out                                       # 丢弃 refusal/annotations/audio/function_call

    # 罕见角色（如 function）：只留必要键，避免结构脏字段
    return {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}


# ========== 3. 视图构建（纯函数） ==========

def est_tokens(messages: List[Dict[str, Any]]) -> int:
    """本地 token 粗估。实测 cl100k 对中文高估 ≈1.8 倍，故按 1 字符 ≈0.5 token 折算。
    仅作兜底——有 API 真实 usage 时一律用真值（DESIGN.md §6）。"""
    return len(json.dumps(messages, ensure_ascii=False)) // 2


def build_view(messages: List[Dict[str, Any]], params: Optional[Dict[str, Any]] = None
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """原文 → 视图。返回 (视图消息, 元数据/统计)。

    纯函数：同输入同输出；不删消息、不重排、可重复调用（幂等）。
    """
    p = params or load_params()
    rounds = iter_rounds(messages)
    prot = protected_rounds(messages, rounds, p)
    keep_think = int(p.get("keep_recent_thinking_rounds") or 0)
    think_cut = max(0, len(rounds) - keep_think)          # 该轮号及之后保留思考原文

    view: List[Dict[str, Any]] = []
    stats = {"cleared_args": 0, "dropped_reasoning": 0, "folded_tool_results": 0,
             "folded_progress": 0, "messages": len(messages), "rounds": len(rounds),
             "protected_rounds": prot}

    for r, (a, b) in enumerate(rounds):
        protected = r in prot
        for i in range(a, b):
            src = messages[i]
            out = shrink_message(src, protected=protected, keep_thinking=(r >= think_cut),
                                 idx=i, params=p)
            # 统计（只为日志与验收，不参与逻辑）
            if src.get("role") == "assistant":
                if src.get("tool_calls") and p.get("clear_tool_args", True):
                    stats["cleared_args"] += len(src["tool_calls"])
                if src.get("reasoning_content") and "reasoning_content" not in out:
                    stats["dropped_reasoning"] += 1
                if not protected and out.get("content") != src.get("content") and src.get("tool_calls"):
                    stats["folded_progress"] += 1
            elif src.get("role") == "tool" and out.get("content") != src.get("content"):
                stats["folded_tool_results"] += 1
            view.append(out)

    stats["est_before"] = est_tokens(messages)
    stats["est_after"] = est_tokens(view)
    stats["saved_ratio"] = round(1 - stats["est_after"] / stats["est_before"], 4) if stats["est_before"] else 0.0
    return view, stats


# ========== 4. 触发判定（纯函数） ==========

def should_compact(*, prompt_tokens: Optional[int], rounds_total: int, rounds_since_compact: Optional[int],
                   window: int, params: Dict[str, Any]) -> Tuple[bool, str]:
    """是否压缩 + 原因 —— 唯一判据 = token 闸门。

    上一轮 API 真实 prompt_tokens ≥ 窗口×ratio 才触发；冷却 cooldown_rounds 只做节流
    （防连续压缩），不参与「该不该压」的决策。

    2026-09-22 主人裁决：删除「轮数防线」（原 rounds_threshold = 距上次压缩满
    keep_recent_rounds 轮就压）。原因：它与 token 实际占用脱钩，会把一个还在进行中的
    任务历史反复折叠 → 任务陷入循环、agent 空转。
    keep_recent_rounds 参数保留，但只剩「保护段」一个职责（最近 N 轮不折叠，见
    protected_rounds）。

    注：rounds_total / rounds_since_compact 保留在签名里，仅供冷却计算使用。
    """
    if not params.get("enabled", False):
        return False, "disabled"
    ratio = float(params.get("trigger_ratio") or 0.55)
    cool = int(params.get("cooldown_rounds") or 0)
    since = rounds_since_compact if rounds_since_compact is not None else rounds_total

    if cool > 0 and since < cool:
        return False, f"cooldown({since}<{cool})"
    if prompt_tokens is not None and window > 0 and prompt_tokens >= window * ratio:
        return True, "token_threshold"
    return False, "below_threshold"


# ========== 5. 落盘与命中（P2） ==========

DEFAULT_VIEW_DIR = "agent_memory/.condensed_sessions"


def _clip(text: Any, n: int) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"\n…（已截断，原长 {len(s)} 字符）"


class ContextManager:
    """视图的落盘、命中与触发执行。

    - 原文永不动：本类只读写 .condensed_sessions/ 下的视图与事件流
    - 视图 = 原文的确定性函数：坏了/过期了删掉重算即可，无不可逆损失
    - 任何失败都只记日志、绝不中断回合（压缩是纯收益功能）
    """

    def __init__(self, project_root: Any, params: Optional[Dict[str, Any]] = None, log: Any = None):
        self.root = Path(project_root)
        self.params = params or load_params()
        self.dir = self.root / str(self.params.get("view_dir") or DEFAULT_VIEW_DIR)
        self.log = log
        self._mem: Dict[str, Optional[Dict[str, Any]]] = {}   # sid → 视图元数据（进程内缓存）
        self._lock = threading.Lock()

    # ---------- 路径与日志 ----------

    def view_path(self, sid: str) -> Path:
        return self.dir / f"{sid}.json"

    def events_path(self, sid: str) -> Path:
        return self.dir / f"{sid}.events.jsonl"

    def _log(self, level: str, msg: str, **extra: Any) -> None:
        if self.log is None:
            return
        try:
            getattr(self.log, level)(msg, **extra)
        except Exception:
            pass

    # ---------- 指纹：视图是否还基于同一批原文 ----------

    @staticmethod
    def fingerprint(messages: List[Dict[str, Any]], n: int) -> str:
        if n <= 0 or n > len(messages):
            return ""
        blob = json.dumps(messages[n - 1], ensure_ascii=False, sort_keys=True)
        return hashlib.md5(blob.encode("utf-8")).hexdigest()

    # ---------- 原子写 ----------

    def _write_json_atomic(self, path: Path, obj: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def append_event(self, sid: str, event: Dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            ev = {"t": datetime.now().isoformat(timespec="seconds"), **event}
            with open(self.events_path(sid), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception as e:
            self._log("warning", f"上下文事件写入失败: {e}")

    # ---------- 读取 / 命中 ----------

    def load_saved(self, sid: str) -> Optional[Dict[str, Any]]:
        """读视图元数据（进程内缓存）。文件损坏 → None，绝不抛。"""
        if sid in self._mem:
            return self._mem[sid]
        p = self.view_path(sid)
        data: Optional[Dict[str, Any]] = None
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
                    raise ValueError("视图结构不合法")
                data = raw
            except Exception as e:
                self._log("warning", f"视图文件不可用（将按未压缩处理）: {e}")
                data = None
        self._mem[sid] = data
        return data

    def view_for(self, sid: str, messages: List[Dict[str, Any]]
                 ) -> Tuple[List[Dict[str, Any]], str]:
        """返回 (发给模型的消息, 来源)。

        有可用视图 → 视图 + 原文尾部（前缀稳定，缓存友好）；否则 → 原文原样。
        本方法**只读不算**：视图只由 compact() 产生，避免每轮白算。
        """
        data = self.load_saved(sid)
        if not data:
            return messages, "no_view"
        n = int(data.get("source_len") or 0)
        if n <= 0 or n > len(messages):
            return messages, "no_view"
        if self.fingerprint(messages, n) != (data.get("source_fp") or ""):
            self.append_event(sid, {"event": "view_rebuild", "reason": "fingerprint_mismatch",
                                    "source_len": n})
            with self._lock:
                self._mem.pop(sid, None)
            return messages, "stale_view"
        return list(data["messages"]) + list(messages[n:]), "view_hit"

    # ---------- 生成与落盘 ----------

    def compact(self, sid: str, messages: List[Dict[str, Any]], *, prompt_tokens: Optional[int] = None,
                trigger: str = "manual", model: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """生成视图并原子落盘。失败只记日志并返回 None（调用方继续用原文）。"""
        try:
            view, stats = build_view(messages, self.params)
            prev = self.load_saved(sid) or {}
            meta = {
                "session_id": sid,
                "version": int(prev.get("version") or 0) + 1,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "source_len": len(messages),
                "source_fp": self.fingerprint(messages, len(messages)),
                "trigger": {"reason": trigger, "prompt_tokens": prompt_tokens,
                            "window": window_for(self.params, model),
                            "ratio": self.params.get("trigger_ratio")},
                "tokens": {"est_before": stats["est_before"], "est_after": stats["est_after"]},
                "kept_rounds": int(self.params.get("keep_recent_rounds") or 0),
                "protected_rounds": stats["protected_rounds"],
                "messages": view,
                "stats": {k: stats[k] for k in ("cleared_args", "dropped_reasoning",
                                                "folded_tool_results", "folded_progress",
                                                "messages", "rounds", "saved_ratio")},
            }
            self._write_json_atomic(self.view_path(sid), meta)
            with self._lock:
                self._mem[sid] = meta
            self.append_event(sid, {"event": "compact", "version": meta["version"], "reason": trigger,
                                    "source_len": meta["source_len"], "prompt_tokens": prompt_tokens,
                                    "est_before": stats["est_before"], "est_after": stats["est_after"]})
            self._log("info", f"上下文视图已生成 v{meta['version']}（{trigger}）",
                      source_len=meta["source_len"], est_before=stats["est_before"],
                      est_after=stats["est_after"], saved_ratio=stats["saved_ratio"])
            return meta
        except Exception as e:
            self._log("warning", f"上下文压缩失败（本次仍用原文，下一轮重试）: {e}")
            return None

    def last_compact_rounds(self, sid: str) -> Optional[int]:
        data = self.load_saved(sid)
        if not data:
            return None
        return int((data.get("stats") or {}).get("rounds") or 0) or None

    def maybe_compact(self, sid: str, messages: List[Dict[str, Any]], *,
                      prompt_tokens: Optional[int] = None,
                      model: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """轮次边界调用：判定 → 生成 → 落盘。未触发返回 None。"""
        rounds = len(iter_rounds(messages))
        last = self.last_compact_rounds(sid)
        since = rounds if last is None else max(0, rounds - last)
        ok, why = should_compact(prompt_tokens=prompt_tokens, rounds_total=rounds,
                                 rounds_since_compact=since,
                                 window=window_for(self.params, model), params=self.params)
        if not ok:
            return None
        return self.compact(sid, messages, prompt_tokens=prompt_tokens, trigger=why, model=model)

    # ---------- 回捞（P4 的 restore_context 工具后端） ----------

    def restore(self, sid: str, messages: List[Dict[str, Any]], *, round_no: Optional[int] = None,
                tool_call_id: Optional[str] = None, max_chars: int = 20000) -> Optional[str]:
        """从原文取回被折叠的内容。轮号 1-based（第 1 轮 = 第一条 user 开始）。"""
        if tool_call_id:
            for m in messages:
                if m.get("role") == "tool" and m.get("tool_call_id") == tool_call_id:
                    return _clip(m.get("content"), max_chars)
            for m in messages:
                for tc in (m.get("tool_calls") or []):
                    if tc.get("id") == tool_call_id:
                        return _clip(tc.get("function"), max_chars)
            return None
        if round_no is not None:
            rounds = iter_rounds(messages)
            if round_no < 1 or round_no > len(rounds):
                return None
            a, b = rounds[round_no - 1]
            return _clip(messages[a:b], max_chars)
        return None

    # ---------- 维护与观测 ----------

    def clean_orphans(self) -> int:
        """清理写盘中断残留的 *.tmp（只在本模块目录内）。"""
        if not self.dir.exists():
            return 0
        n = 0
        for p in self.dir.glob("*.tmp"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def stats(self, sid: str, model: Optional[str] = None) -> Dict[str, Any]:
        """给 WebUI / 日志用的一览（P5 消费）。"""
        data = self.load_saved(sid) or {}
        win = window_for(self.params, model)
        return {
            "has_view": bool(data),
            "version": data.get("version"),
            "source_len": data.get("source_len"),
            "est_before": (data.get("tokens") or {}).get("est_before"),
            "est_after": (data.get("tokens") or {}).get("est_after"),
            "protected_rounds": data.get("protected_rounds") or [],
            "rounds": (data.get("stats") or {}).get("rounds"),
            "window": win,
            "threshold": int(win * float(self.params.get("trigger_ratio") or 0.55)),
        }
