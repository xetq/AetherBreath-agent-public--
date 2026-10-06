# -*- coding: utf-8 -*-
"""P12：交付的**呈现方式**与 `task_list` 的**口径**（主人 2026-09-30 的两条要求）。

要求 1：后台作业交付要和「中期交互」一样**附加在用户的输入卡片内**，但样式必须区分开。
        —— 前端靠"前缀识别 + 标记 + 样式"，三样缺一都会静默降级成"主人自己的发言"。

要求 2：`task_list` 只列**编排器当前状况**（在跑 / 待交付），不再当一张越用越长的历史表；
        交付完就出册（内容已经写进会话历史）—— 所以登记册不会随"跑过的作业越来越多"而长大。
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent"), str(ROOT / "agent_tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import task_orchestrator as T          # noqa: E402
from agent_tools import task_jobs as J  # noqa: E402

SRC = ROOT / "agent_webui" / "frontend" / "src"
JOB_TS = SRC / "lib" / "jobDelivery.ts"
TURNS_TS = SRC / "lib" / "turns.ts"
LOAD_TS = SRC / "lib" / "useLoadSession.ts"
LIST_TSX = SRC / "components" / "MessageList.tsx"
STYLES = SRC / "styles.css"
STORE = SRC / "store" / "appStore.tsx"
CHAT = SRC / "components" / "ChatView.tsx"
BRIDGE = ROOT / "agent_webui" / "backend" / "bridge.py"


class _FakeOrch:
    def __init__(self, reg):
        self.jobs = reg


@pytest.fixture
def reg_only():
    reg = T.JobRegistry()
    T.set_current_orchestrator(_FakeOrch(reg))
    try:
        yield reg
    finally:
        T.set_current_orchestrator(None)


def _ts_literal(s: str) -> str:
    return "'%s'" % s.replace("\\", "\\\\").replace("'", "\\'")


# ============================================================
# 1. 前缀契约：前端必须与 agent 侧**逐字一致**（否则历史里那条交付认不出来）
# ============================================================

def test_frontend_delivery_prefix_matches_agent():
    src = JOB_TS.read_text(encoding="utf-8")
    assert "export const JOB_DELIVERY_PREFIX = %s" % _ts_literal(T.JOB_DELIVERY_PREFIX) in src, \
        "前端 JOB_DELIVERY_PREFIX 与 agent 侧不一致 —— 交付会被当成主人自己的发言渲染"
    assert "export const JOB_DELIVERY_MARK" in src
    assert "parseJobDelivery" in src


def test_delivery_parser_keeps_body_intact():
    """解析器必须**原样保留正文**（那是作业的输出全文），只把标题与说明行分开。"""
    text = (T.JOB_DELIVERY_PREFIX + "\n作业 j9：tool()（已完成，已跑 1.0s）\n"
            "输出（全文 5 字符）：\nHELLO\n· 这是自动交付，已写入本会话历史。")
    conv = {"JOB_DELIVERY_PREFIX": T.JOB_DELIVERY_PREFIX}
    # 用同一套规则在 Python 里跑一遍，确保"剥标题/收说明行"不会吃掉正文
    lines = text[len(conv["JOB_DELIVERY_PREFIX"]):].lstrip("\n").split("\n")
    title = lines.pop(0).strip()
    notes = []
    while lines and lines[-1].strip().startswith("·"):
        notes.insert(0, lines.pop().strip())
    body = "\n".join(lines).strip()
    assert title.startswith("作业 j9")
    assert body == "输出（全文 5 字符）：\nHELLO", body
    assert notes and notes[0].startswith("· 这是自动交付")
    # 前端那份必须与这套规则一致（标题/说明行的判据写死在源码里，防止哪天被改成"贪婪剥离"）
    src = JOB_TS.read_text(encoding="utf-8")
    assert "startsWith('·')" in src, "说明行判据没了 —— 正文尾巴会被吃掉"
    assert "lines.shift()" in src, "标题行判据没了"


# ============================================================
# 2. 位置与样式：挂在用户卡里，且与「用户交代」区分开
# ============================================================

def test_delivery_is_rendered_inside_user_card():
    src = LIST_TSX.read_text(encoding="utf-8")
    card = src[src.index("function UserCard"):src.index("export default memo")]
    assert "t.jobs.map(" in card, "交付没挂在用户卡里（要求 1：附加在用户输入卡片内）"
    assert "t.mids.map(" in card, "别把「用户交代」挤掉了"
    # 卡片渲染的那条块必须自带"区分标记"（类名 + 角标），否则又跟交代混成一片
    assert "JobDeliveryBlock" in card and "function JobDeliveryBlock" in src
    assert "job-inline" in src and "JOB_DELIVERY_MARK" in src and "JOB_DELIVERY_BADGE" in src


def test_history_load_marks_delivery():
    src = LOAD_TS.read_text(encoding="utf-8")
    assert "parseJobDelivery" in src and "job: true" in src, \
        "装载历史时没把交付标出来 —— 它会当普通用户消息渲染"
    assert "mid: 'delivered'" in src, "别把「用户交代」的标记弄丢了"


def test_turns_attaches_delivery_to_its_turn():
    src = TURNS_TS.read_text(encoding="utf-8")
    assert "jobs: HistoryMessage[]" in src, "TurnView 少了 jobs 段"
    assert "m.job" in src and "jobs.push(m)" in src, "交付没被挂进所属回合"
    assert "jobs: seg.jobs" in src, "重建回合时没带上 jobs"


def test_styles_distinguish_delivery_from_mid():
    css = STYLES.read_text(encoding="utf-8")
    assert ".job-inline" in css and ".job-who" in css and ".job-badge" in css
    assert ".mid-inline" in css and ".mid-badge" in css, "「用户交代」的样式不许被这次的改动顶掉"
    # 两者必须**看得出来**不一样：交付用编排器那盏橙灯的同族色，交代仍用紫
    job_block = css[css.index(".job-inline"):css.index(".job-notes")]
    assert "#f0883e" in job_block, "交付没用橙色系 —— 与「用户交代」的紫区分不开"
    assert "#45316e" not in job_block, "交付串用了交代的紫色"


# ============================================================
# 3. task_list 只列"编排器当前状况"，且登记册不会越用越长
# ============================================================

def _settle(reg, sid="s1", out="产出"):
    j = reg.register(tool="t", label="t()", sid=sid)
    j.finish(result=out)
    return j


def test_task_list_prompt_says_it_is_not_a_history_table():
    """口径不许漂移（2026-10-02 真机：AB 自己把这句删了，被契约测试抓住）。"""
    from agent_tools import task_jobs as J
    desc = J.task_list_schema["function"]["description"]
    assert "不是历史表" in desc, "task_list 的说明丢了「这不是历史表」—— 模型会把它当累积台账"
    assert "出册" in desc, "没说清「交付完就出册」，模型会以为作业凭空消失"


def test_task_list_has_no_history_table_param():
    import inspect
    sig = inspect.signature(J.task_list)
    assert "include_finished" not in sig.parameters, \
        "include_finished 是「历史表」时代的参数 —— 现在只列当前状况，留着只会误导模型"
    assert "include_finished" not in str(J.task_list_schema), "schema 里也不许留"


def test_task_list_lists_running_and_waiting_delivery_only(reg_only):
    reg = reg_only
    running = reg.register(tool="t", label="run()", sid="s1")
    waiting = _settle(reg, out="X")
    out = J.task_list()
    assert running.job_id in out and "运行中" in out
    assert waiting.job_id in out and "待交付" in out, "结算未交付的必须还在列表里（它马上要被交付）"
    assert "编排器当前 2 个" in out, out[:60]


def test_delivered_job_disappears_and_registry_stops_growing(reg_only):
    """**主人给的理由就是这条**：运行久了表会一直占内存 —— 交付完必须出册。"""
    reg = reg_only
    for i in range(30):
        j = _settle(reg, out="X" * 100)
        reg.mark_delivered(j.job_id)
    assert reg.live_summary() == [], "交付过的还留在册里 —— 表会越用越长"
    out = J.task_list()
    assert "编排器当前空闲" in out
    assert "会话历史" in out, "空列表也要说清「交付过的东西去哪了」，不然像丢数据"


def test_task_output_for_delivered_job_says_where_it_went(reg_only):
    reg = reg_only
    j = _settle(reg, out="机密")
    reg.mark_delivered(j.job_id)
    out = J.task_output(job_id=j.job_id, wait_seconds=0)
    assert "没有这个后台作业" in out
    assert "交付进会话历史" in out, "必须点明「可能已交付」，否则模型会以为结果丢了"
    assert "task_list" in out, "要告诉它去哪儿看现在在跑的"


def test_task_output_settled_before_delivery_says_pending(reg_only):
    reg = reg_only
    j = _settle(reg, out="机密产出")
    out = J.task_output(job_id=j.job_id, wait_seconds=0)
    assert "机密产出" not in out, "还没交付也不许返回内容（内容走自动交付）"
    assert "自动交付" in out and "字符" in out


# ============================================================
# 4. 交付要**当场可见**：bridge 把回执放进 done，界面据此重放历史
# ============================================================

def test_bridge_carries_delivery_receipt_into_done():
    src = BRIDGE.read_text(encoding="utf-8")
    assert "take_delivered_notices" in src, "bridge 没去取交付回执"
    assert 'payload["delivered_jobs"] = delivered_jobs' in src, "done 事件没带上交付回执"


def test_frontend_replays_history_on_delivery():
    store = STORE.read_text(encoding="utf-8")
    assert "jobDeliveredAt" in store and "delivered_jobs" in store, \
        "store 没接住交付回执 —— 那条交付要等切会话/刷新才看得见"
    chat = CHAT.read_text(encoding="utf-8")
    seg = chat[chat.index("jobDeliveredAt"):]
    assert "void load(" in seg[:600], "收到回执后没有以磁盘为准重放历史"


def test_agent_take_notices_is_drain_once():
    import agent as ab
    ab.take_delivered_notices()
    ab._DELIVERED_NOTICES.append("j7")
    assert ab.take_delivered_notices() == ["j7"]
    assert ab.take_delivered_notices() == [], "取走即清空（同一批不许报两次）"
