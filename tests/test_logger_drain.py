"""
AsyncLogWriter 关闭排空回归测试

背景（2026-09-03 修复）：原 close() 先置 _running=False 再入队 None 哨兵，
worker 写完当前条目后检查 while 条件发现 False 立即退出，丢弃队列中所有
剩余日志——快节奏执行（如编排器多工具批）后立即关闭会丢日志，文件只剩
第一条。修复：close() 不再提前置 False，哨兵排在队尾，worker 先消费完
此前全部日志再遇 None 退出。
"""
import json
import sys
import uuid
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT_DIR))

from logger import SessionLogger  # noqa: E402


@pytest.fixture()
def logger(tmp_path):
    """每测试一个独立 session_id + 临时日志目录"""
    sid = f"test_logger_{uuid.uuid4().hex[:8]}"
    log = SessionLogger(session_id=sid, log_dir=tmp_path)
    yield log, tmp_path, sid
    log.close()


def _read_msgs(log_dir: Path, sid: str):
    files = list(log_dir.glob(f"{sid}_*.jsonl"))
    if not files:
        return []
    msgs = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line).get("msg", ""))
        except json.JSONDecodeError:
            continue
    return msgs


def test_close_after_burst_drains_all_entries(logger):
    """连发 20 条后立即 close：队列积压全部落盘，不能只剩第一条"""
    log, log_dir, sid = logger
    for i in range(20):
        log.info(f"突发日志第 {i} 条")
    # 不 sleep，立即 close（复现原竞态窗口）
    log.close()

    msgs = _read_msgs(log_dir, sid)
    assert len(msgs) == 20, f"期望 20 条全落盘，实得 {len(msgs)}: {msgs}"
    assert msgs[0] == "突发日志第 0 条"
    assert msgs[-1] == "突发日志第 19 条"


def test_close_flushes_warning_and_error_levels(logger):
    """close 前排空适用于全部级别（INFO/WARNING/ERROR）"""
    log, log_dir, sid = logger
    log.info("普通信息")
    log.warning("告警")
    log.error("错误")
    log.close()

    msgs = _read_msgs(log_dir, sid)
    assert len(msgs) == 3
    assert msgs == ["普通信息", "告警", "错误"]


def test_multiple_close_calls_are_safe(logger):
    """重复 close 不应抛异常"""
    log, log_dir, sid = logger
    log.info("第一条")
    log.close()
    log.close()  # 幂等
    msgs = _read_msgs(log_dir, sid)
    assert msgs == ["第一条"]
