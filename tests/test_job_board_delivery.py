# -*- coding: utf-8 -*-
"""后台作业**自动交付**契约：全文一次性交付 → 释放内存 → 列表留"已交付"墓碑。

主人 2026-09-30 定的口径（覆盖 P6 的看板语义）：

1. `task_list` **只是列表**：砍掉「未取回」这种取回态标识，已交付的标「已交付」。
2. 跨回合作业完成后**自动交付全文**（不是 4KB 截断版），交付后**释放该作业的内存**，
   列表里留一个「已交付」墓碑。
3. `task_output` **砍掉读内容**：只报"运行情况"——运行中给状态 + 最近日志；
   已结算的只说"已交付/待交付 + 输出大小"，不再返回内容（内容已在会话历史里）。

关键设计约束（写测试时就钉住，免得实现走偏）：
  · 交付必须**持久化**（否则"释放内存"= 内容永久丢失，"自动交付"就是假的）；
  · 但**不能用 system 角色**：`save_session`/`load_session` 会把 system 消息**整条丢掉**，
    而 `assemble_request` 会把它们**提到最前**当语境快照 —— 两条路都会毁掉这条交付；
  · 所以走 `user` 角色 + 响亮前缀（与「用户交代」同款），并让 bridge 的 `_user_seq`
    把它排除在"真实用户输入"之外（否则 WebUI 的轮次对不上）。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent"), str(ROOT / "agent_webui" / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent as ab                                # noqa: E402
import task_orchestrator as T                     # noqa: E402
from agent_tools import task_jobs as J            # noqa: E402


class _Log:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass


BIG = "X" * 20000          # 远超旧的 JOB_INLINE_LIMIT(4096)：用来证"交付的是全文"


def _settled(reg, sid="s1", label="t()", out="产出内容"):
    j = reg.register(tool="t", label=label, sid=sid)
    j.finish(result=out)
    return j


# ============================================================
# 1. 交付文本 = 全文（不是截断版）
# ============================================================

def test_delivery_text_is_full_output_not_truncated():
    reg = T.JobRegistry()
    j = _settled(reg, out=BIG)
    txt = j.delivery_text()
    assert j.job_id in txt and j.label in txt, "交付文本要能认出是哪个作业"
    assert BIG in txt, "交付的是截断版（旧的 4KB 内联上限还在）—— 主人要的是全文"
    assert T.JOB_DELIVERY_PREFIX in txt, "交付文本缺前缀：模型/界面认不出这是自动交付"


def test_delivery_text_carries_error_or_logs_when_no_output():
    reg = T.JobRegistry()
    j = reg.register(tool="t", label="t()", sid="s1")
    j.append_log("第一行")
    j.append_log("第二行")
    j.finish(error="炸了")
    assert "炸了" in j.delivery_text()
    reg2 = T.JobRegistry()
    k = reg2.register(tool="t", label="t()", sid="s1")
    k.append_log("只有日志")
    k.finish(result=None)
    assert "只有日志" in k.delivery_text(), "没有输出时至少要把日志尾巴交付出去"


# ============================================================
# 2. 交付后释放内存，列表留墓碑
# ============================================================

def test_release_frees_result_and_logs_but_keeps_identity():
    reg = T.JobRegistry()
    j = _settled(reg, out=BIG)
    j.append_log("日志" * 100)
    assert j.output_chars >= len(BIG), "finish 时必须先记下输出大小（释放后还要能如实报）"
    j.release()
    assert j.result is None, "释放后结果不许还占着内存"
    assert j.log_lines == [], "日志环形缓冲也要释放"
    assert j.job_id and j.label and j.status == "done", "身份/状态要留着（列表还要列它）"
    assert j.output_chars >= len(BIG), "输出大小要留着（不然只能报「不知道多大」）"


def test_mark_delivered_releases_and_unregisters():
    """交付完成 = **释放内存 + 从登记册出册**（P12：交付过的不用再留着）。"""
    reg = T.JobRegistry()
    j = _settled(reg, out=BIG)
    assert reg.pending_delivery("s1") == [j]
    reg.mark_delivered(j.job_id)
    assert j.result is None, "标记已交付时必须一并释放内存"
    assert reg.get(j.job_id) is None, "交付完就该出册 —— 留着墓碑 = 表越用越长（主人 P12 否掉）"
    assert reg.pending_delivery("s1") == [], "已交付的不该再进待交付队列"


def test_registry_has_no_delivered_flag_anymore():
    """`delivered` 是 P8 加的标记，P12 起**由「还在不在册里」表达** —— 不许留死字段（P6 教训）。"""
    reg = T.JobRegistry()
    j = _settled(reg)
    assert not hasattr(j, "delivered"), "delivered 还活着 —— 它的语义已经由出册表达了"


def test_pending_delivery_only_settled_and_owning_session():
    reg = T.JobRegistry()
    running = reg.register(tool="t", label="r()", sid="s1")          # 还在跑
    mine = _settled(reg, sid="s1")
    other = _settled(reg, sid="s2")
    pend = reg.pending_delivery("s1")
    assert mine in pend and running not in pend and other not in pend
    assert reg.pending_delivery("s2") == [other]


# ============================================================
# 3. task_list 只是列表：砍掉取回态
# ============================================================

def test_one_line_marks_waiting_delivery_only():
    """列表行只区分「运行中」与「待交付」；「已交付」这种行不会再出现（交付即出册）。"""
    reg = T.JobRegistry()
    j = _settled(reg, sid="s1")
    assert "待交付" in j.one_line()
    assert "已交付" not in j.one_line(), "已交付不该再出现在列表里（它已经出册了）"
    assert "未取回" not in j.one_line(), "取回态标识该砍掉了"
    assert j.label in j.one_line() and "已完成" in j.one_line(), "列表仍要给出身份与状态"
    run = reg.register(tool="t", label="r()", sid="s1")
    assert "待交付" not in run.one_line(), "在跑的作业不该挂交付尾巴"


def test_job_has_no_fetched_field_anymore():
    """旧字段 `fetched` 是"写了没人读"的死字段（P6 的教训），这次连根删掉。"""
    reg = T.JobRegistry()
    j = _settled(reg)
    assert not hasattr(j, "fetched"), "fetched 还活着 —— 取回语义已经不存在了"


# ============================================================
# 4. task_output 只报运行情况，不返回内容
# ============================================================

def test_task_output_running_gives_progress_logs():
    o = T.TaskOrchestrator(max_workers=2, default_timeout=5,
                           tools_map={"probe": lambda **kw: "ok"},
                           tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    o._register_signal_handlers = lambda: None
    try:
        T.set_current_orchestrator(o)
        job = o.jobs.register(tool="probe", label="probe()", sid="s1")
        job.append_log("进度一行")
        out = J.task_output(job_id=job.job_id, wait_seconds=0)
        assert "运行中" in out and "进度一行" in out, "运行中就该给状态 + 最近日志"
    finally:
        T.set_current_orchestrator(None)
        o.shutdown()


def test_task_output_settled_returns_no_content():
    o = T.TaskOrchestrator(max_workers=2, default_timeout=5,
                           tools_map={"probe": lambda **kw: "ok"},
                           tool_timeouts={}, side_effect_tools=set(), log_enabled=False)
    o._register_signal_handlers = lambda: None
    try:
        T.set_current_orchestrator(o)
        job = o.jobs.register(tool="probe", label="probe()", sid="s1")
        job.finish(result="机密产出内容")
        out = J.task_output(job_id=job.job_id, wait_seconds=0)
        assert "机密产出内容" not in out, "已结算的还返回内容 —— 主人要的是砍掉读内容"
        assert "交付" in out, "至少要说清「内容去哪了」（自动交付）"
        assert "字符" in out, "至少要如实报输出大小"
        # 交付之后：它**出册**了，所以只能如实说"没有这个作业"并点明原因（内容在会话历史里）
        o.jobs.mark_delivered(job.job_id)
        out2 = J.task_output(job_id=job.job_id, wait_seconds=0)
        assert "没有这个后台作业" in out2, out2
        assert "交付进会话历史" in out2, "必须点明「它可能已经交付了」，别让人以为结果丢了"
        assert "机密产出内容" not in out2
    finally:
        T.set_current_orchestrator(None)
        o.shutdown()


# ============================================================
# 5. agent 侧装配：交付**落进会话**（持久化），再释放内存
# ============================================================

class _FakeOrch:
    def __init__(self, reg):
        self.jobs = reg

    def pool_pressure_note(self):
        return ""


@pytest.fixture
def wired(monkeypatch):
    reg = T.JobRegistry()
    monkeypatch.setattr(ab, "_get_orchestrator", lambda: _FakeOrch(reg))
    saved = []
    monkeypatch.setattr(ab, "save_session",
                        lambda sid, msgs, log, status="active": saved.append(
                            (sid, json.dumps(msgs, ensure_ascii=False))))
    return reg, saved


def test_delivery_appends_durable_user_message(wired):
    reg, saved = wired
    _settled(reg, sid="s1", out=BIG)
    conv = [{"role": "user", "content": "你好"}]
    n = ab._deliver_finished_jobs(conv, "s1", _Log())
    assert n == 1
    assert len(conv) == 2, "交付要 appen 进 conversation（不是只拼进这一次请求）"
    msg = conv[-1]
    assert msg["role"] == "user", (
        "必须是 user 角色：system 会被 save_session/load_session 丢掉、"
        "又会被 assemble_request 提到最前当语境快照")
    assert BIG in msg["content"] and T.JOB_DELIVERY_PREFIX in msg["content"]
    assert saved and saved[-1][0] == "s1", "交付要先落盘（durable）再释放内存"


def test_delivery_is_once_and_releases_memory(wired):
    reg, saved = wired
    j = _settled(reg, sid="s1", out=BIG)
    conv = [{"role": "user", "content": "你好"}]
    assert ab._deliver_finished_jobs(conv, "s1", _Log()) == 1
    assert j.result is None, "交付后要释放内存"
    assert reg.get(j.job_id) is None, "交付后要出册"
    assert ab._deliver_finished_jobs(conv, "s1", _Log()) == 0, "第二次又交付了一遍"
    assert len(conv) == 2, "会话里不该出现第二条重复交付"


def test_delivery_records_notice_for_the_done_event(wired):
    """交付是**落盘消息**、实时通道里没有它 —— 所以要留一条"刚交付了谁"给 bridge 放进 done，
    界面据此当场重放历史（否则那条交付要等切会话/刷新才看得见）。"""
    reg, saved = wired
    ab.take_delivered_notices()                      # 清干净，别受别的用例影响
    _settled(reg, sid="s1", out="X")
    ab._deliver_finished_jobs([{"role": "user", "content": "hi"}], "s1", _Log())
    got = ab.take_delivered_notices()
    assert got == ["j1"], "交付后必须留下回执给 done 事件，实际=%r" % got
    assert ab.take_delivered_notices() == [], "取走即清空（同一批不许报两次）"


def test_delivery_never_crosses_sessions(wired):
    reg, saved = wired
    _settled(reg, sid="s1", out="A 会话的产出")
    conv = [{"role": "user", "content": "你好"}]
    assert ab._deliver_finished_jobs(conv, "s2", _Log()) == 0
    assert conv == [{"role": "user", "content": "你好"}], "串台了"
    assert ab._deliver_finished_jobs(conv, "s1", _Log()) == 1


def test_delivery_skips_running_jobs(wired):
    reg, saved = wired
    reg.register(tool="t", label="r()", sid="s1")       # 还在跑
    conv = [{"role": "user", "content": "你好"}]
    assert ab._deliver_finished_jobs(conv, "s1", _Log()) == 0
    assert len(conv) == 1


def test_pool_note_is_still_request_temporary(wired, monkeypatch):
    """池压力提示与交付是两回事：它只拼进**这一次请求**，不落盘。"""
    reg, saved = wired
    monkeypatch.setattr(_FakeOrch, "pool_pressure_note", lambda self: "⚠️ 池压力测试")
    orch = ab._get_orchestrator()
    msgs = [{"role": "user", "content": "hi"}]
    out = ab._with_pool_note(msgs, _Log())
    assert len(out) == 2 and out[-1]["role"] == "system"
    assert "池压力测试" in out[-1]["content"]
    assert msgs == [{"role": "user", "content": "hi"}], "不许就地改 conversation"


# ============================================================
# 6. 跨层契约：交付前缀必须被"真实用户输入"计数排除
# ============================================================

def test_bridge_excludes_delivery_from_user_seq():
    """交付是 user 角色；bridge 数"第几次真实用户输入"时必须把它排除，
    否则 WebUI 的轮次↔中期过程会整体错位。"""
    src = (ROOT / "agent_webui" / "backend" / "bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("def _user_seq"):src.index("def _user_seq") + 700]
    assert "JOB_DELIVERY_PREFIX" in seg, \
        "bridge._user_seq 没排除后台作业交付 —— 每交付一次，界面的轮次就错一次"


def test_delivery_prefix_is_shared_constant():
    """前缀只能有一处定义（agent 侧），bridge 从它 import —— 两边各写一份必然漂移。"""
    src = (ROOT / "agent_webui" / "backend" / "bridge.py").read_text(encoding="utf-8")
    assert "from task_orchestrator import" in src and "JOB_DELIVERY_PREFIX" in src, \
        "bridge 没从 task_orchestrator 取那个前缀常量"
    assert T.JOB_DELIVERY_PREFIX in ("【后台作业交付 · 跨回合任务（不是你本回合发起的动作）】",
                                     T.JOB_DELIVERY_PREFIX)
