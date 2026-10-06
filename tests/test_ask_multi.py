# -*- coding: utf-8 -*-
"""ask 多卡：共享活跃窗口 + 两侧接线契约
===========================================
背景：`ask_user` 支持同批并行提问（一个回合里同时挂 N 张卡）。本文件守住两件事：

1. `backend/ask_batch.WindowPool` 的窗口语义 —— 一批并行卡共享 deadline，
   有人在答就往后续、连续窗口秒没人答才算没人应答，且有硬上限防挂死。
   全部用传入的 `now` 做时间旅行，不真等。
2. **契约锁**：前端 store 必须是多卡结构（`clarifies` 数组），不许退回单槽；
   编排器的强制串行名单必须认 `ask_user` 这个名字（历史写成 `clarify`，是死条目）。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_ask_multi.py -q
"""

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent_webui" / "backend"))

from ask_batch import WindowPool, batch_timeout  # noqa: E402

STORE_TS = ROOT / "agent_webui" / "frontend" / "src" / "store" / "appStore.tsx"
ORCH_PY = ROOT / "agent" / "task_orchestrator.py"
BRIDGE_PY = ROOT / "agent_webui" / "backend" / "bridge.py"


# ---------------------------------------------------------------- 批次窗口公式
class TestBatchTimeout:
    def test_single_card_matches_history(self):
        """单卡必须与历史行为一致（120 + 10）：不为自己没提的需求多等一秒。"""
        assert batch_timeout(1, 120) == 130

    def test_scales_with_card_count(self):
        assert batch_timeout(3, 120) == 370
        assert batch_timeout(2, 120) == 250

    def test_never_zero(self):
        assert batch_timeout(0, 120) == 130      # 没有卡时也回落单卡语义


# ---------------------------------------------------------------- 活跃窗口
class TestWindowPool:
    def test_first_join_opens_a_batch(self):
        p = WindowPool()
        s = p.join(120, now=1000)
        assert s["batch_id"] == 1 and s["size"] == 1 and s["live"] == 1
        assert abs(s["remaining"] - 120) < 1e-6

    def test_parallel_cards_share_one_deadline(self):
        """三张卡并行挂上：同一个批次、同一条 deadline。"""
        p = WindowPool()
        a = p.join(120, now=1000)
        b = p.join(120, now=1000.2)
        c = p.join(120, now=1000.4)
        assert a["batch_id"] == b["batch_id"] == c["batch_id"]
        assert (a["size"], b["size"], c["size"]) == (1, 2, 3)
        assert c["live"] == 3
        assert p.live() == 3

    def test_later_card_never_shortens_deadline(self):
        """后来的卡只能把共同窗口往后推，绝不能提前关掉别人的等待。"""
        p = WindowPool()
        p.join(30, now=1000)                    # deadline = 1030
        s = p.join(120, now=1000)               # 更长 → 推到 1120
        assert abs(s["remaining"] - 120) < 1e-6
        s2 = p.join(10, now=1001)               # 更短 → 不缩短
        assert abs(s2["remaining"] - 119) < 1e-6

    def test_answer_extends_the_window_for_the_rest(self):
        """核心体验：答完一张，剩下几张的窗口往后续 —— 人就在跟前，不该被判超时。"""
        p = WindowPool()
        p.join(120, now=1000)
        p.join(120, now=1000)
        p.join(120, now=1000)
        assert abs(p.remaining(now=1100) - 20) < 1e-6      # 快没时间了
        p.touch(now=1100)                                   # 有卡被回答
        assert abs(p.remaining(now=1100) - 120) < 1e-6     # 续满窗
        assert abs(p.remaining(now=1190) - 30) < 1e-6

    def test_extension_is_capped(self):
        """硬上限：window*n + grace 之外不再续，防止回合被无限挂住。"""
        p = WindowPool(grace=60)
        for _ in range(3):
            p.join(120, now=1000)               # born=1000, n=3 → 上限 1000+360+60=1420
        for t in range(1100, 1400, 10):
            p.touch(now=t)
        cap = 1000 + 120 * 3 + 60
        assert abs(p.snapshot()["deadline"] - cap) < 1e-6
        p.touch(now=cap - 1)                    # 再怎么答也不越过上限
        assert abs(p.snapshot()["deadline"] - cap) < 1e-6

    def test_window_closes_when_last_card_leaves(self):
        p = WindowPool()
        p.join(120, now=1000)
        p.join(120, now=1000)
        p.leave()
        assert p.live() == 1 and p.remaining(now=1010) > 0
        p.leave()
        assert p.live() == 0 and p.remaining(now=1010) == 0.0

    def test_next_batch_starts_fresh(self):
        """上一批空了之后，新卡属于新批次（窗口、计数都重来）。"""
        p = WindowPool()
        p.join(120, now=1000)
        p.leave()
        s = p.join(120, now=2000)
        assert s["batch_id"] == 2 and s["size"] == 1 and s["live"] == 1
        assert abs(s["remaining"] - 120) < 1e-6

    def test_expired_flag(self):
        p = WindowPool()
        p.join(120, now=1000)
        assert not p.expired(now=1119)
        assert p.expired(now=1120)
        p.leave()
        assert p.expired(now=1000)              # 没卡了就是关的

    def test_reset_kills_the_batch(self):
        p = WindowPool()
        p.join(120, now=1000)
        p.join(120, now=1000)
        p.reset()
        assert p.live() == 0 and p.remaining(now=1010) == 0.0
        assert p.join(120, now=1010)["batch_id"] == 2

    def test_concurrent_join_touch_leave_keeps_counts_honest(self):
        """三阶段并发：批次内计数不能因为线程交错而丢。
        （注意 answers 是**批次内**计数：批次一旦空了就重置 —— 所以三个阶段要
        用栅栏隔开，让它们确实发生在同一批里。）"""
        p = WindowPool()
        n = 40
        b_join, b_touch, b_leave = (threading.Barrier(n) for _ in range(3))

        def joiner():
            b_join.wait()
            p.join(120)
            b_touch.wait()          # 等所有人都挂上，保证批次不被提前关掉
            p.touch()
            b_leave.wait()
            p.leave()

        ts = [threading.Thread(target=joiner) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert p.live() == 0
        assert p.snapshot()["size"] == n          # 40 张卡确实都进了同一批
        assert p.snapshot()["answers"] == n       # 40 次作答一次没丢


# ---------------------------------------------------------------- 契约锁
class TestContracts:
    def test_frontend_store_is_multi_card(self):
        """前端必须是多卡结构。退回单槽 = 又会出现"三张卡只看得见一张"。"""
        src = STORE_TS.read_text(encoding="utf-8")
        assert "clarifies: AskRequest[]" in src
        assert "ask: AskRequest | null" not in src
        assert "case 'resolve_ask'" in src    # 按 ask_id 摘卡，而不是清空全部

    def test_orchestrator_serial_list_names_the_real_tool(self):
        """强制串行名单必须写工具的真名。历史那条 `clarify` 是死条目：
        工具实际叫 `ask_user`，于是"交互工具要串行"的设计意图从未生效过。"""
        import re
        src = ORCH_PY.read_text(encoding="utf-8")
        m = re.search(r"_NEVER_PARALLEL_TOOLS\s*=\s*\{(.*?)\}", src, re.S)
        assert m, "没找到 _NEVER_PARALLEL_TOOLS 定义"
        block = m.group(1)
        assert '"ask_user"' in block
        assert '"clarify"' not in block          # 只查名单本体（注释里可提历史）

    def test_bridge_uses_the_shared_window(self):
        """bridge 必须真的接上共享窗口 —— 而不是各卡各算各的超时。"""
        src = BRIDGE_PY.read_text(encoding="utf-8")
        assert "ask_batch" in src
        assert "ASK_WINDOW.join(" in src
        assert "ASK_WINDOW.touch(" in src


if __name__ == "__main__":     # tests/README 的维护约定：每个文件都能独立直跑
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q"]))
