# -*- coding: utf-8 -*-
"""中期交互测试（agent_webui/backend/mid_turn.py + 两侧接线契约）
=================================================
覆盖：信箱读写与并发、渲染格式、注入时机与形态（独立 user 消息，绝不污染 tool 输出）、
过期作废语义、观察者通道，以及两条**契约锁**——
  1) 前端 lib/midTurn.ts 里的标识前缀必须与后端逐字一致（历史消息靠它认出来，
     两边各改一半会静默降级成"交代被当成普通发言"，tsc/构建都不会报）；
   2) agent.py 只提供**通用**钩子（不认识 mid_turn），注入点由 bridge 注册。
      2026-09-22 前 agent/ 里那份 mid_turn.py 已迁到 agent_webui/backend/。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_mid_turn.py -q
"""

import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# mid_turn 的实现 2026-09-22 迁到 WebUI 侧（agent_webui/backend/），agent/ 已无此模块。
# 把 backend 排在 sys.path 前面，并**断言真命中的就是它**（打错模块 = 等于没测）。
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "agent_webui" / "backend"))

import mid_turn  # noqa: E402
from mid_turn import (  # noqa: E402
    BOX, GUIDE, MARK, MAX_PENDING, MAX_TEXT, PREFIX, MidTurnBox,
    flush_after_tools, render,
)

assert Path(mid_turn.__file__).resolve().parent.name == "backend", mid_turn.__file__

WEBUI_MID_TS = ROOT / "agent_webui" / "frontend" / "src" / "lib" / "midTurn.ts"
AGENT_PY = ROOT / "agent" / "agent.py"
BRIDGE_PY = ROOT / "agent_webui" / "backend" / "bridge.py"


@pytest.fixture(autouse=True)
def _clean_box():
    """信箱是进程级单例，用例之间必须清干净（否则会互相读到对方的交代）。"""
    BOX.clear()
    yield
    BOX.clear()


def fresh() -> MidTurnBox:
    return MidTurnBox()


# ---------------------------------------------------------------- 信箱读写
class TestMailbox:
    def test_push_returns_receipt(self):
        box = fresh()
        item = box.push("s1", "别动 config.yaml")
        assert item and item["id"] and item["text"] == "别动 config.yaml"
        assert item["pending"] == 1

    def test_drain_takes_all_in_order(self):
        box = fresh()
        box.push("s1", "第一句")
        box.push("s1", "第二句")
        got = box.drain("s1")
        assert [x["text"] for x in got] == ["第一句", "第二句"]

    def test_drain_is_destructive(self):
        box = fresh()
        box.push("s1", "只说一次")
        assert len(box.drain("s1")) == 1
        assert box.drain("s1") == []            # 第二次什么也拿不到
        assert box.peek("s1") == 0

    def test_sessions_are_isolated(self):
        box = fresh()
        box.push("s1", "给甲")
        box.push("s2", "给乙")
        assert [x["text"] for x in box.drain("s1")] == ["给甲"]
        assert [x["text"] for x in box.drain("s2")] == ["给乙"]

    def test_discard_drops_everything(self):
        box = fresh()
        box.push("s1", "作废这句")
        assert [x["text"] for x in box.discard("s1")] == ["作废这句"]
        assert box.snapshot() == {}

    def test_blank_text_rejected(self):
        box = fresh()
        assert box.push("s1", "   ") is None
        assert box.push("", "有内容但没会话") is None
        assert box.peek("s1") == 0

    def test_text_is_stripped_and_capped(self):
        box = fresh()
        item = box.push("s1", "  " + "长" * (MAX_TEXT + 500) + "  ")
        assert item is not None
        assert len(item["text"]) == MAX_TEXT      # 截断到上限，不是整条丢掉

    def test_queue_full_is_refused_not_silently_dropped(self):
        box = fresh()
        for i in range(MAX_PENDING):
            assert box.push("s1", "第%d条" % i) is not None
        assert box.push("s1", "超出的这条") is None   # 明确拒绝
        # 关键：被拒绝时，先前那些一条都不能少（绝不"丢最老的腾位子"）
        assert [x["text"] for x in box.drain("s1")] == ["第%d条" % i for i in range(MAX_PENDING)]

    def test_concurrent_push_loses_nothing(self, monkeypatch):
        # 临时抬高上限：本用例要验的是"锁有没有丢件"，不是上限本身
        # （上限行为由 test_queue_full_is_refused_not_silently_dropped 单独钉住）。
        monkeypatch.setattr(mid_turn, "MAX_PENDING", 8 * 25 + 10)
        box = fresh()
        n_threads, per_thread = 8, 25

        def worker(tag: int) -> None:
            for i in range(per_thread):
                box.push("s1", "t%d-%d" % (tag, i))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = box.drain("s1")
        assert len(got) == n_threads * per_thread
        assert len({x["id"] for x in got}) == n_threads * per_thread   # 无重复
        assert len({x["text"] for x in got}) == n_threads * per_thread  # 无丢件


# ---------------------------------------------------------------- 渲染格式
class TestRender:
    def test_single_item_has_mark_and_no_numbering(self):
        text = render([{"text": "只改 agent/ 下的文件"}])
        assert text.startswith(PREFIX)
        assert GUIDE in text
        assert text.endswith("只改 agent/ 下的文件")
        assert not text.splitlines()[-1].startswith("1.")

    def test_multi_items_are_numbered_in_order(self):
        text = render([{"text": "甲"}, {"text": "乙"}, {"text": "丙"}])
        assert "1. 甲" in text and "2. 乙" in text and "3. 丙" in text
        assert text.index("1. 甲") < text.index("2. 乙") < text.index("3. 丙")


# ---------------------------------------------------------------- 注入
class TestFlush:
    def test_empty_box_is_zero_cost(self):
        conv = [{"role": "user", "content": "原来的消息"}]
        snapshot = list(conv)
        assert flush_after_tools(conv, "s1") == 0
        assert conv == snapshot              # 空信箱绝不动 conversation

    def test_injected_as_separate_user_message(self):
        """交代必须是独立 user 消息 —— 绝不拼进 tool 返回里（否则模型会把它
        当成工具输出的真实内容）。"""
        BOX.push("s1", "别动 config.yaml")
        conv = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容：xxx"},
        ]
        assert flush_after_tools(conv, "s1") == 1
        assert len(conv) == 3
        last = conv[-1]
        assert last["role"] == "user"
        assert PREFIX in last["content"] and "别动 config.yaml" in last["content"]
        # tool 消息原封不动
        assert conv[1]["content"] == "文件内容：xxx"

    def test_injected_once_only(self):
        BOX.push("s1", "一次性")
        conv = []
        assert flush_after_tools(conv, "s1") == 1
        assert flush_after_tools(conv, "s1") == 0     # 不会二次注入
        assert len(conv) == 1

    def test_session_isolation_on_flush(self):
        BOX.push("s1", "给甲的")
        conv = []
        assert flush_after_tools(conv, "s2") == 0     # 别的会话见不到
        assert conv == []

    def test_logger_called_only_when_something_injected(self):
        calls = []

        class L:
            def info(self, msg, **kw):
                calls.append(msg)

        conv = []
        flush_after_tools(conv, "s1", L())
        assert calls == []                             # 空信箱不写"已注入"日志
        BOX.push("s1", "真投递了")
        flush_after_tools(conv, "s1", L())
        assert len(calls) == 1 and "中期交互" in calls[0]

    def test_many_turns_share_one_message(self):
        """一次工具返回里把这一批交代合成一条消息，不是 N 条。"""
        BOX.push("s1", "甲")
        BOX.push("s1", "乙")
        conv = []
        assert flush_after_tools(conv, "s1") == 2
        assert len(conv) == 1
        assert "1. 甲" in conv[0]["content"] and "2. 乙" in conv[0]["content"]


# ---------------------------------------------------------------- 观察者（事件出口）
class TestObserver:
    def test_stages_are_announced(self):
        box = fresh()
        seen = []
        box.set_observer(lambda stage, payload: seen.append((stage, payload)))
        item = box.push("s1", "观察我", run_id="run-abc")
        assert seen[0][0] == "accepted"
        assert seen[0][1]["item_id"] == item["id"]
        assert seen[0][1]["mid"] == "accepted"        # 阶段字段随载荷一起下发
        assert seen[0][1]["session_id"] == "s1"
        # run_id 必须一路带着：本项目所有事件都靠它标回合归属，少一个字段
        # 前端就只能退回 session 级判断，跨回合的迟到帧会变得无法分辨。
        assert seen[0][1]["run_id"] == "run-abc"

    def test_run_id_survives_to_injection(self):
        seen = []
        BOX.set_observer(lambda stage, payload: seen.append((stage, payload)))
        BOX.push("s1", "带回合号的交代", run_id="run-xyz")
        flush_after_tools([], "s1")
        assert seen[-1][1]["run_id"] == "run-xyz"

    def test_broken_observer_never_breaks_delivery(self):
        box = fresh()

        def boom(stage, payload):
            raise RuntimeError("观察者自己炸了")

        box.set_observer(boom)
        item = box.push("s1", "照常投递")        # 不能让观测面把主流程带崩
        assert item is not None
        assert len(box.drain("s1")) == 1

    def test_flush_announces_injected(self):
        seen = []
        BOX.set_observer(lambda stage, payload: seen.append((stage, payload)))
        BOX.push("s1", "甲")
        conv = []
        flush_after_tools(conv, "s1")
        assert [s for s, _ in seen] == ["accepted", "injected"]
        inj = seen[-1][1]
        assert inj["ids"] and inj["count"] == 1 and inj["session_id"] == "s1"


# ---------------------------------------------------------------- 契约锁
class TestContracts:
    def test_frontend_prefix_matches_backend(self):
        """前端靠这行字从会话历史里认出「用户交代」。两边不一致 = 静默降级。"""
        src = WEBUI_MID_TS.read_text(encoding="utf-8")
        assert 'export const MID_PREFIX = %s' % _ts_literal(PREFIX) in src
        assert 'export const MID_MARK = %s' % _ts_literal(MARK) in src

    def test_agent_py_offers_generic_hook_only(self):
        """agent.py 只提供**通用**挂载点，不认识中期交互（2026-09-22 起）。
        它一旦重新 import mid_turn / 调 flush_after_tools，就说明搬家被回退了。"""
        src = AGENT_PY.read_text(encoding="utf-8")
        assert "import mid_turn" not in src
        assert "flush_after_tools" not in src
        assert src.count("_run_after_tools_hooks(conversation, session_id, log)") == 1
        assert "def register_after_tools_hook" in src

    def test_bridge_registers_the_hook(self):
        """注入点由 bridge 注册 —— 漏了这一步，主人的交代永远进不了对话，
        而界面一切正常（不报错、不告警），是最难发现的一类失效。"""
        src = BRIDGE_PY.read_text(encoding="utf-8")
        assert "register_after_tools_hook(mid_turn.flush_after_tools)" in src

    def test_frontend_events_registered(self):
        """api.ts 的 EVENTS 白名单漏了 mid_turn，EventSource 就永远收不到这类事件
        （reducer 写得再对也没用）—— 这条曾经是 clarify 卡片的同类坑。"""
        src = (ROOT / "agent_webui" / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        assert "'mid_turn'" in src


def _ts_literal(value: str) -> str:
    return "'%s'" % value.replace("\\", "\\\\").replace("'", "\\'")


if __name__ == "__main__":     # tests/README 的维护约定：每个文件都能独立直跑
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
