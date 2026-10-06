# -*- coding: utf-8 -*-
"""
上下文管理器测试（agent/context_manager.py）
=============================================
P1（纯函数核心）覆盖：
  切轮 / 保护段规则 / 用户消息零改动 / 结构完整性（tool 配对不断头）/
  清理矩阵（args·reasoning·工具结果·进度正文）/ 幂等 / 触发判定

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_context_manager.py -q
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))

from context_manager import (  # noqa: E402
    ContextManager,
    build_view,
    est_tokens,
    iter_rounds,
    load_params,
    protected_rounds,
    should_compact,
    window_for,
)


# ---------------------------------------------------------------------------
# fixture：造一个 12 轮的会话（每轮 4 条：user / assistant(tool_calls) / tool / assistant）
# ---------------------------------------------------------------------------

def make_round(r: int, user_text: str = "") -> list:
    big_args = {"code": "print('x')\n" * 60, "note": "内嵌的大段脚本正文" * 20}
    thinking = "我的思考过程。" * 40
    return [
        {"role": "user", "content": user_text or f"第 {r} 轮任务：查一下并汇报"},
        {"role": "assistant", "content": f"第 {r} 轮我先执行脚本。", "reasoning_content": thinking,
         "refusal": None, "annotations": None, "audio": None, "function_call": None,
         "tool_calls": [{"id": f"call_{r}", "type": "function",
                         "function": {"name": "execute_python",
                                      "arguments": json.dumps(big_args, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": f"call_{r}", "content": "工具输出行。" * 200},
        {"role": "assistant", "content": f"第 {r} 轮最终结论：已完成。", "reasoning_content": thinking,
         "refusal": None, "annotations": None, "audio": None, "function_call": None},
    ]


def make_session(n_rounds: int = 12) -> list:
    msgs = []
    for r in range(n_rounds):
        msgs += make_round(r)
    return msgs


def calls_of(view: list) -> dict:
    """assistant.tool_calls 的 id → 该 id 是否有配对 tool 消息。"""
    have = {m.get("tool_call_id") for m in view if m.get("role") == "tool"}
    out = {}
    for m in view:
        for tc in (m.get("tool_calls") or []):
            out[tc.get("id")] = tc.get("id") in have
    return out


def msg_by_round(view: list, r: int) -> dict:
    """取某一轮的四条消息（按 fixture 的固定布局：4 条一轮）。"""
    base = r * 4
    return {"user": view[base], "call": view[base + 1], "tool": view[base + 2], "final": view[base + 3]}


# ---------------------------------------------------------------------------
# 1. 切轮
# ---------------------------------------------------------------------------

def test_iter_rounds_basic():
    msgs = make_session(3)
    rounds = iter_rounds(msgs)
    assert rounds == [(0, 4), (4, 8), (8, 12)]


def test_iter_rounds_leading_messages_merged_into_first_round():
    msgs = [{"role": "assistant", "content": "孤儿"}] + make_round(0)
    rounds = iter_rounds(msgs)
    assert rounds[0][0] == 0 and rounds[0][1] == 5     # 前导消息不丢


def test_iter_rounds_empty():
    assert iter_rounds([]) == []


# ---------------------------------------------------------------------------
# 2. 保护段规则
# ---------------------------------------------------------------------------

def test_protected_rounds_recent_plus_first():
    msgs = make_session(12)
    p = load_params({})
    rounds = iter_rounds(msgs)
    prot = protected_rounds(msgs, rounds, p)
    assert prot == [0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]   # 最近 10 轮 + 首轮
    assert 1 not in prot


def test_protected_rounds_absolute_word_pulls_old_round_back_in():
    msgs = make_session(12)
    msgs[4]["content"] = "必须记住：这个结论不要改"          # 第 1 轮（会被折叠的轮）含绝对词
    p = load_params({})
    prot = protected_rounds(msgs, iter_rounds(msgs), p)
    assert 1 in prot


def test_protected_rounds_absolute_words_configurable():
    msgs = make_session(12)
    msgs[4]["content"] = "禁止访问该目录"
    p = load_params({"absolute_words": ["禁止"]})
    assert 1 in protected_rounds(msgs, iter_rounds(msgs), p)
    p2 = load_params({"absolute_words": []})
    assert 1 not in protected_rounds(msgs, iter_rounds(msgs), p2)


# ---------------------------------------------------------------------------
# 3. 用户消息零改动 + 结构完整性
# ---------------------------------------------------------------------------

def test_user_messages_untouched():
    msgs = make_session(12)
    view, _ = build_view(msgs)
    for src, out in zip(msgs, view):
        if src["role"] == "user":
            assert out["content"] == src["content"]
            assert out == {"role": "user", "content": src["content"]}


def test_view_length_unchanged_and_order_preserved():
    msgs = make_session(12)
    view, stats = build_view(msgs)
    assert len(view) == len(msgs)                      # 不删消息、不重排
    assert [m["role"] for m in view] == [m["role"] for m in msgs]
    assert stats["messages"] == len(msgs)


def test_tool_call_pairing_never_broken():
    msgs = make_session(12)
    view, _ = build_view(msgs)
    pairing = calls_of(view)
    assert pairing and all(pairing.values())           # 每个 tool_call 都有配对 tool


# ---------------------------------------------------------------------------
# 4. 清理矩阵
# ---------------------------------------------------------------------------

def test_folded_round_tool_result_becomes_placeholder():
    msgs = make_session(12)
    view, stats = build_view(msgs)
    old = msg_by_round(view, 1)
    assert old["tool"]["content"].startswith("[已折叠")
    assert "restore_context" in old["tool"]["content"]
    assert old["call"]["content"].startswith("[进度汇报已折叠")
    assert old["final"]["content"] == "第 1 轮最终结论：已完成。"   # 最终回答保留
    assert stats["folded_tool_results"] == 1
    assert stats["folded_progress"] == 1


def test_recent_round_keeps_tool_result_and_final_answer():
    msgs = make_session(12)
    view, _ = build_view(msgs)
    recent = msg_by_round(view, 11)
    assert recent["tool"]["content"] == "工具输出行。" * 200
    assert recent["call"]["content"] == "第 11 轮我先执行脚本。"
    assert recent["final"]["content"] == "第 11 轮最终结论：已完成。"


def test_tool_args_cleared_everywhere_even_in_recent_round():
    msgs = make_session(12)
    view, stats = build_view(msgs)
    for r in (1, 11):                                   # 无论远近，args 都是死重
        args = json.loads(msg_by_round(view, r)["call"]["tool_calls"][0]["function"]["arguments"])
        assert "_cleared" in args
        assert args["_cleared"].startswith("execute_python: ")
    tc = msg_by_round(view, 1)["call"]["tool_calls"][0]
    assert tc["id"] == "call_1" and tc["function"]["name"] == "execute_python"


def test_reasoning_kept_only_in_recent_n_rounds():
    msgs = make_session(12)
    view, stats = build_view(msgs)
    assert "reasoning_content" not in msg_by_round(view, 8)["call"]   # 第 8 轮：超 3 轮，剥
    assert msg_by_round(view, 11)["call"]["reasoning_content"]        # 最近轮：留
    assert stats["dropped_reasoning"] > 0


def test_null_fields_dropped():
    msgs = make_session(12)
    view, _ = build_view(msgs)
    for m in view:
        if m["role"] == "assistant":
            assert "refusal" not in m and "annotations" not in m
            assert "audio" not in m and "function_call" not in m


def test_build_view_reduces_estimate():
    msgs = make_session(12)
    view, stats = build_view(msgs)
    assert stats["est_after"] < stats["est_before"]
    assert stats["saved_ratio"] > 0.3                   # args+thinking 一剥就该明显下降


# ---------------------------------------------------------------------------
# 5. 幂等（视图再进一次不重复折叠）
# ---------------------------------------------------------------------------

def test_build_view_is_idempotent():
    msgs = make_session(12)
    view1, _ = build_view(msgs)
    view2, _ = build_view(view1)
    assert view1 == view2


# ---------------------------------------------------------------------------
# 6. 参数与触发判定
# ---------------------------------------------------------------------------

def test_load_params_merges_config_keeps_other_defaults():
    p = load_params({"keep_recent_rounds": 3, "model_windows": {"qwen": 131072}})
    assert p["keep_recent_rounds"] == 3
    assert p["model_windows"] == {"qwen": 131072}       # 整表替换
    assert p["trigger_ratio"] == 0.55                   # 未提到的键保留默认（2026-09-22 由 0.5 改）
    assert window_for(p, "unknown") == 1_000_000        # 表内无 default → 回落 window_tokens


def test_window_for_model_lookup():
    p = load_params({"model_windows": {"a": 32768, "default": 1_000_000}})
    assert window_for(p, "a") == 32768
    assert window_for(p, "unknown") == 1_000_000
    assert window_for(load_params({"window_tokens": 65536, "model_windows": {}}), None) == 65536


def test_should_compact_disabled_by_default():
    p = load_params({})
    ok, why = should_compact(prompt_tokens=10 ** 9, rounds_total=99, rounds_since_compact=99,
                             window=1_000_000, params=p)
    assert ok is False and why == "disabled"


def test_should_compact_token_threshold():
    p = load_params({"enabled": True})
    ok, why = should_compact(prompt_tokens=600_000, rounds_total=5, rounds_since_compact=5,
                             window=1_000_000, params=p)
    assert ok and why == "token_threshold"


def test_should_compact_cooldown_blocks():
    p = load_params({"enabled": True})
    ok, why = should_compact(prompt_tokens=900_000, rounds_total=30, rounds_since_compact=1,
                             window=1_000_000, params=p)
    assert ok is False and why.startswith("cooldown")


def test_should_compact_rounds_no_longer_triggers():
    """2026-09-22 主人裁决：轮数防线已删 —— 跑再多轮，token 没到闸门就是不压。"""
    p = load_params({"enabled": True, "keep_recent_rounds": 10})
    ok, why = should_compact(prompt_tokens=1000, rounds_total=12, rounds_since_compact=10,
                             window=1_000_000, params=p)
    assert ok is False and why == "below_threshold"


def test_should_compact_without_usage_never_fires():
    """拿不到 usage（如重启后首个回合）→ 不压；轮数再多也救不了（防线已删）。"""
    p = load_params({"enabled": True})
    ok, why = should_compact(prompt_tokens=None, rounds_total=99, rounds_since_compact=99,
                             window=1_000_000, params=p)
    assert ok is False and why == "below_threshold"


def test_est_tokens_stable():
    assert est_tokens([{"role": "user", "content": "你好"}]) > 0


# ---------------------------------------------------------------------------
# 7. 落盘与命中（P2）
# ---------------------------------------------------------------------------

def cm_for(tmp_path, **cfg) -> ContextManager:
    return ContextManager(project_root=tmp_path, params=load_params({"enabled": True, **cfg}))


def test_compact_writes_view_and_view_for_hits(tmp_path):
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    meta = cm.compact("s1", msgs, prompt_tokens=600_000, trigger="token_threshold")
    assert meta and meta["version"] == 1 and cm.view_path("s1").exists()
    got, how = cm.view_for("s1", msgs)
    assert how == "view_hit"
    assert len(got) == len(msgs)
    assert got[6]["content"].startswith("[已折叠")            # 第 1 轮的 tool 是折叠版
    assert got[3]["content"] == "第 0 轮最终结论：已完成。"     # 首轮保护：原文


def test_view_hit_appends_new_tail(tmp_path):
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    cm.compact("s1", msgs)
    more = msgs + make_round(12)
    got, how = cm.view_for("s1", more)
    assert how == "view_hit" and len(got) == len(more)
    assert got[-4]["content"] == "第 12 轮任务：查一下并汇报"   # 尾部是原文新消息
    assert got[-1]["content"] == "第 12 轮最终结论：已完成。"


def test_view_stale_when_tail_changed(tmp_path):
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    cm.compact("s1", msgs)
    msgs[-1] = {"role": "assistant", "content": "被改过的最后一条"}
    got, how = cm.view_for("s1", msgs)
    assert how == "stale_view" and got == msgs
    assert "fingerprint_mismatch" in cm.events_path("s1").read_text(encoding="utf-8")


def test_fingerprint_only_covers_tail_by_design(tmp_path):
    """已知边界（诚实记录）：指纹只覆盖「已压缩部分的最后一条」，
    因为正常流程历史只追加不改写；中间被篡改检测不到，代价是 O(1) 校验。"""
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    cm.compact("s1", msgs)
    msgs[6]["content"] = "中间被篡改"
    _, how = cm.view_for("s1", msgs)
    assert how == "view_hit"                                  # 中间篡改不触发重建


def test_shorter_history_is_not_hit(tmp_path):
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    cm.compact("s1", msgs)
    short = msgs[:8]
    got, how = cm.view_for("s1", short)
    assert how == "no_view" and got == short                   # 原文反而更短 → 宁可用原文


def test_no_view_by_default(tmp_path):
    cm = cm_for(tmp_path)
    msgs = make_session(3)
    got, how = cm.view_for("s1", msgs)
    assert how == "no_view" and got is msgs


def test_broken_view_file_is_ignored(tmp_path):
    cm = cm_for(tmp_path)
    cm.view_path("s1").parent.mkdir(parents=True, exist_ok=True)
    cm.view_path("s1").write_text("{ 这不是合法 json", encoding="utf-8")
    got, how = cm.view_for("s1", make_session(3))
    assert how == "no_view"


def test_atomic_write_leaves_no_tmp(tmp_path):
    cm = cm_for(tmp_path)
    cm.compact("s1", make_session(3))
    assert list(cm.dir.glob("*.tmp")) == []


def test_compact_failure_does_not_raise_and_keeps_original(tmp_path, monkeypatch):
    cm = cm_for(tmp_path)
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(cm, "_write_json_atomic", boom)
    assert cm.compact("s1", make_session(3)) is None            # 不抛
    assert not cm.view_path("s1").exists()
    assert cm.view_for("s1", make_session(3))[1] == "no_view"   # 仍走原文


def test_clean_orphans(tmp_path):
    cm = cm_for(tmp_path)
    cm.dir.mkdir(parents=True, exist_ok=True)
    (cm.dir / "s1.json.tmp").write_text("x", encoding="utf-8")
    (cm.dir / "s1.events.jsonl").write_text("x", encoding="utf-8")
    assert cm.clean_orphans() == 1
    assert (cm.dir / "s1.events.jsonl").exists()                # 事件流不是孤儿


def test_maybe_compact_disabled_writes_nothing(tmp_path):
    cm = ContextManager(project_root=tmp_path, params=load_params({}))   # enabled: false
    assert cm.maybe_compact("s1", make_session(12), prompt_tokens=900_000) is None
    assert not cm.view_path("s1").exists()


def test_maybe_compact_triggers_on_token_threshold(tmp_path):
    cm = cm_for(tmp_path)
    msgs = make_session(12)
    meta = cm.maybe_compact("s1", msgs, prompt_tokens=900_000)
    assert meta and meta["trigger"]["reason"] == "token_threshold"
    assert cm.view_path("s1").exists()
    assert cm.view_for("s1", msgs)[1] == "view_hit"


def test_maybe_compact_cooldown_blocks_immediate_repack(tmp_path):
    cm = cm_for(tmp_path)
    msgs = make_session(12)
    assert cm.maybe_compact("s1", msgs, prompt_tokens=900_000) is not None
    assert cm.maybe_compact("s1", msgs, prompt_tokens=900_000) is None      # 同一位置再压 → 冷却


def test_maybe_compact_no_usage_writes_nothing(tmp_path):
    """2026-09-22：轮数防线删除后，拿不到 prompt_tokens 就不压（旧行为是满 12 轮硬压）。"""
    cm = cm_for(tmp_path)
    msgs = make_session(12)
    assert cm.maybe_compact("s1", msgs, prompt_tokens=None) is None
    assert not cm.view_path("s1").exists()


def test_version_increments_on_repack(tmp_path):
    cm = cm_for(tmp_path, cooldown_rounds=0)
    msgs = make_session(12)
    assert cm.maybe_compact("s1", msgs, prompt_tokens=900_000)["version"] == 1
    more = msgs + make_round(12)
    assert cm.maybe_compact("s1", more, prompt_tokens=900_000)["version"] == 2


def test_restore_by_round_and_tool_call_id(tmp_path):
    msgs = make_session(12)
    cm = cm_for(tmp_path)
    r = cm.restore("s1", msgs, round_no=1)
    assert "第 0 轮任务" in r and "工具输出行。" in r
    assert cm.restore("s1", msgs, tool_call_id="call_3") == "工具输出行。" * 200
    assert cm.restore("s1", msgs, round_no=99) is None
    assert cm.restore("s1", msgs, tool_call_id="nope") is None
    assert cm.restore("s1", msgs) is None                                     # 两参数都不给


def test_restore_tool_call_arguments_when_no_tool_message(tmp_path):
    """只有 tool_call、没有配对 tool 响应时，按 id 取回工具参数。"""
    cm = cm_for(tmp_path)
    msgs = [{"role": "user", "content": "跑脚本"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_x", "type": "function",
                             "function": {"name": "execute_python",
                                          "arguments": "{\"code\": \"print(1)\"}"}}]}]
    out = cm.restore("s1", msgs, tool_call_id="call_x")
    assert out is not None and "execute_python" in out and "print(1)" in out


def test_restore_prefers_tool_result_over_arguments(tmp_path):
    """同一个 id 既有 tool 响应也有 assistant 参数时，优先返回工具结果。"""
    msgs = make_session(1)
    cm = cm_for(tmp_path)
    assert cm.restore("s1", msgs, tool_call_id="call_0") == "工具输出行。" * 200


def test_restore_clips_long_content(tmp_path):
    msgs = make_session(1)
    cm = cm_for(tmp_path)
    out = cm.restore("s1", msgs, tool_call_id="call_0", max_chars=50)
    assert "已截断" in out


def test_stats_reports_window_and_threshold(tmp_path):
    cm = cm_for(tmp_path)
    cm.compact("s1", make_session(12), prompt_tokens=600_000)
    st = cm.stats("s1")
    assert st["has_view"] is True and st["window"] == 1_000_000
    assert st["threshold"] == 550_000 and st["est_after"] < st["est_before"]
    assert st["rounds"] == 12


def test_reopen_reuses_view_without_recompute(tmp_path):
    """主人最关心的场景：重开对话不重压。新进程（新 ContextManager 实例）
    只读到已落盘视图 + 指纹命中，一次 build_view 都不该发生。"""
    msgs = make_session(12)
    cm1 = cm_for(tmp_path)
    cm1.compact("s1", msgs)

    cm2 = cm_for(tmp_path)                                  # 模拟重开：全新实例、空内存缓存
    called = {"n": 0}
    import context_manager as _m
    orig = _m.build_view
    _m.build_view = lambda *a, **k: (called.__setitem__("n", called["n"] + 1), orig(*a, **k))[1]
    try:
        got, how = cm2.view_for("s1", msgs)
    finally:
        _m.build_view = orig
    assert how == "view_hit" and len(got) == len(msgs) and called["n"] == 0
