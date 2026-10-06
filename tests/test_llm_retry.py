"""
LLM 调用重试测试

审计条件二的原话："框架自身**无重试代码**：agent.py:515 API 异常即 return {"error": ...}
整回合终止。仅靠 OpenAI SDK 隐式默认（实测 client.max_retries=2，超时 connect=5s/read=600s）
→ 有重试但不可配、不可见、未落日志。"

本文件锁死显式化之后的行为：
  1. 可恢复错误（429 限流 / 5xx / 超时 / 连接断）→ 有限次指数退避重试
  2. 不可恢复错误（400 参数错 / 401 鉴权 / 404 模型不存在）→ 立刻抛，不浪费时间
  3. 每次重试都落日志（重试过程可见，不再"静默重试两次"）
  4. 耗尽后把最后一次异常抛出（由调用方终止回合）
  5. KeyboardInterrupt / SystemExit 永远原样抛（停止按钮依赖它）

注：判据看的是 `type(e).__name__`，所以假异常必须是**各自独立的类**
（曾用改类名的写法，导致所有用例共用一个类名、判据串味）。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent as ab                                  # noqa: E402


def _mk_err(name, status=None):
    """造一个**真正独立类型**的异常（类名参与判据，不能多个用例共用一个类）。"""
    cls = type(name, (Exception,), {})
    e = cls("boom")
    if status is not None:
        e.status_code = status
    return e


class _FakeCompletions:
    """按脚本依次抛异常 / 返回结果的假 client。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, script):
        self.chat = type("C", (), {"completions": _FakeCompletions(script)})()


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        pass


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    """把退避压成 0，测试不等真实秒数。"""
    monkeypatch.setattr(ab, "LLM_RETRY_BASE_DELAY", 0.0)


# ============ 1. 可恢复错误 → 重试 ============

RETRYABLE = [
    ("rate_limit_429", _mk_err("_E429", 429)),
    ("server_error_500", _mk_err("_E500", 500)),
    ("service_unavailable_503", _mk_err("ServiceUnavailableError", 503)),
    ("api_timeout_no_status", _mk_err("APITimeoutError")),
    ("api_connection_no_status", _mk_err("APIConnectionError")),
    ("request_timeout_408", _mk_err("_E408", 408)),
]


@pytest.mark.parametrize("label,err", RETRYABLE, ids=[c[0] for c in RETRYABLE])
def test_retryable_errors_are_retried(label, err, monkeypatch):
    monkeypatch.setattr(ab, "LLM_MAX_ATTEMPTS", 3)
    client = _FakeClient([err, err, "resp"])
    log = _Log()
    assert ab.chat_with_retry(client, log, model="m") == "resp"
    assert client.chat.completions.calls == 3, "两次失败后第三次应成功返回"
    assert len(log.warnings) == 2, "每次重试都要落日志（可见性）"


# ============ 2. 不可恢复错误 → 立刻抛 ============

NON_RETRYABLE = [
    ("bad_request_400", _mk_err("_E400", 400)),
    ("auth_401", _mk_err("_E401", 401)),
    ("not_found_404", _mk_err("_E404", 404)),
    ("plain_value_error", _mk_err("BoomValueError")),
]


@pytest.mark.parametrize("label,err", NON_RETRYABLE, ids=[c[0] for c in NON_RETRYABLE])
def test_non_retryable_errors_raise_immediately(label, err, monkeypatch):
    monkeypatch.setattr(ab, "LLM_MAX_ATTEMPTS", 4)
    client = _FakeClient([err, "resp"])
    with pytest.raises(type(err)):
        ab.chat_with_retry(client, _Log(), model="m")
    assert client.chat.completions.calls == 1, "不可重试的错误不该浪费时间再来一次"


# ============ 3. 重试耗尽 → 抛出最后一次异常 ============

def test_exhausted_retries_raise(monkeypatch):
    monkeypatch.setattr(ab, "LLM_MAX_ATTEMPTS", 2)
    err = _mk_err("_E500x", 500)
    client = _FakeClient([err, err])
    log = _Log()
    with pytest.raises(type(err)):
        ab.chat_with_retry(client, log, model="m")
    assert client.chat.completions.calls == 2
    assert len(log.warnings) == 1, "最后一次不再重试，所以只有一次重试日志"


def test_attempts_capped_by_config(monkeypatch):
    monkeypatch.setattr(ab, "LLM_MAX_ATTEMPTS", 5)
    err = _mk_err("_E429x", 429)
    client = _FakeClient([err] * 10)
    with pytest.raises(type(err)):
        ab.chat_with_retry(client, _Log(), model="m")
    assert client.chat.completions.calls == 5, "次数受 LLM_MAX_ATTEMPTS 约束"


# ============ 4. 中断信号绝不被吞掉 ============

@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(1)])
def test_interrupt_is_never_swallowed(exc):
    """停止按钮靠 KeyboardInterrupt 生效 —— 重试逻辑绝不能把它当"可重试错误"。"""
    client = _FakeClient([exc, "resp"])
    with pytest.raises(type(exc)):
        ab.chat_with_retry(client, _Log(), model="m")
    assert client.chat.completions.calls == 1


# ============ 5. 成功路径零开销 ============

def test_success_takes_one_call():
    client = _FakeClient(["resp"])
    log = _Log()
    assert ab.chat_with_retry(client, log, model="m") == "resp"
    assert client.chat.completions.calls == 1
    assert log.warnings == []


def test_config_defaults_are_sane():
    """默认 3 次尝试、退避基数 1.5s，且尝试次数有硬上限（别被 .env 写成 999）。"""
    assert 1 <= ab.LLM_MAX_ATTEMPTS <= 6
    assert ab.LLM_RETRY_BASE_DELAY >= 0.0
