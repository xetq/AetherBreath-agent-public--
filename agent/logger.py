# -*- coding: utf-8 -*-
"""
生产级日志系统 - AetherBreath Logger
=======================================
特性：
- 异步写入（队列 + 独立线程）
- 结构化 JSON Lines 格式（.jsonl）
- 全链路追踪（session_id / trace_id / span_id）
- 动态日志级别（从 .env 读取）
- 采样与背压（防 IO 抖动）
- 安全脱敏（Secrets Redaction）
- 按会话分文件（agent_logs/{session_id}_{date}.jsonl）
- GitHub 友好：零硬编码路径，Clone 即用
- 终端零输出：所有日志只写文件，不污染 stdout/stderr

用法：
    from agent.logger import SessionLogger
    log = SessionLogger(session_id="my_session")
    log.info("工具调用开始", tool="search", query="conda")
    log.debug("LLM 原始响应", raw=response_json)
"""
import os
import re
import json
import queue
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, Optional

# 明确从项目根目录加载 .env（保证 GitHub 友好）
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
else:
    from dotenv import load_dotenv
    load_dotenv()


# ==================== 工具函数：清洗环境变量（去除注释） ====================

def _clean_env_value(raw: Optional[str]) -> str:
    """
    去除环境变量值中的注释（# 及之后的内容）和首尾空格。
    如果 raw 为 None，返回空字符串。
    """
    if raw is None:
        return ""
    return raw.split('#')[0].strip()


# ==================== 配置 ====================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_SAMPLING_RATE = float(_clean_env_value(os.getenv("LOG_SAMPLING_RATE", "0.1")))
LOG_QUEUE_SIZE = int(_clean_env_value(os.getenv("LOG_QUEUE_SIZE", "1000")))
LOG_BACKPRESSURE_THRESHOLD = int(_clean_env_value(os.getenv("LOG_BACKPRESSURE_THRESHOLD", "500")))
LOG_JSON_INDENT = os.getenv("LOG_JSON_INDENT")
if LOG_JSON_INDENT is not None:
    clean_indent = _clean_env_value(LOG_JSON_INDENT)
    if clean_indent:
        try:
            LOG_JSON_INDENT = int(clean_indent)
        except ValueError:
            LOG_JSON_INDENT = None
    else:
        LOG_JSON_INDENT = None
LOG_SECRETS_REDACT = os.getenv("LOG_SECRETS_REDACT", "true").lower() == "true"

# 日志级别映射
LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
CURRENT_LEVEL = LEVELS.get(LOG_LEVEL, logging.INFO)

# 敏感字段正则（用于脱敏）
SECRET_PATTERNS = [
    (re.compile(r'(api_key|apikey|api-key)[\s:=]+["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'(password|passwd|pwd)[\s:=]+["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'(authorization|auth)[\s:=]+["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'(secret|token)[\s:=]+["\']?([^"\'\s,}]+)', re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'(bearer)[\s]+([^\s,}]+)', re.IGNORECASE), r'bearer ***REDACTED***'),
]


# ==================== 脱敏过滤器 ====================

def redact_secrets(text: str) -> str:
    """脱敏文本中的敏感字段"""
    if not LOG_SECRETS_REDACT or not text:
        return text
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_dict(obj: Any) -> Any:
    """递归脱敏字典/列表中的敏感字段"""
    if not LOG_SECRETS_REDACT:
        return obj
    if isinstance(obj, dict):
        return {redact_dict(k): redact_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_dict(item) for item in obj]
    elif isinstance(obj, str):
        return redact_secrets(obj)
    else:
        return obj


# ==================== 日志条目 ====================

class LogEntry:
    """单条日志条目（结构化）"""

    def __init__(
        self,
        level: str,
        message: str,
        session_id: str,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        self.timestamp = datetime.now().isoformat(timespec="milliseconds")
        self.level = level
        self.message = message
        self.session_id = session_id
        self.trace_id = trace_id or session_id
        self.span_id = span_id
        self.extra = extra or {}

    def to_dict(self) -> Dict[str, Any]:
        """转为字典（已脱敏）"""
        data = {
            "ts": self.timestamp,
            "lvl": self.level,
            "sid": self.session_id,
            "tid": self.trace_id,
            "msg": self.message,
        }
        if self.span_id:
            data["span"] = self.span_id
        if self.extra:
            data.update(redact_dict(self.extra))
        return data

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


# ==================== 日志写入器（异步 + 背压，终端零输出） ====================

class AsyncLogWriter:
    """异步日志写入器（单例模式，每个会话一个实例）"""

    _instances: Dict[str, "AsyncLogWriter"] = {}

    def __new__(cls, session_id: str, log_dir: Path):
        if session_id in cls._instances:
            return cls._instances[session_id]
        instance = super().__new__(cls)
        instance._init(session_id, log_dir)
        cls._instances[session_id] = instance
        return instance

    def _init(self, session_id: str, log_dir: Path):
        self.session_id = session_id
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y%m%d")
        self._session_id = session_id
        self._log_date = date_str
        self.log_file = self.log_dir / f"{session_id}_{date_str}.jsonl"

        self._queue: queue.Queue = queue.Queue(maxsize=LOG_QUEUE_SIZE)
        self._running = True
        self._worker = threading.Thread(target=self._write_loop, daemon=True)
        self._worker.start()

        self._backpressure_active = False
        self._debug_counter = 0
        self._debug_skip_count = 0

    def _write_loop(self):
        """后台写入线程"""
        while self._running:
            try:
                entry = self._queue.get(timeout=0.5)
                if entry is None:
                    break
                self._write_entry(entry)
            except queue.Empty:
                continue
            except Exception:
                pass

    def _current_log_file(self):
        """当前该写进哪个文件。

        审计 B11：文件名在 init 时按当天定死，会话跨过午夜后仍往**昨天**那个文件里写
        （表现为"昨天的日志里出现今天的事"，排查时序时会误导）。这里按写入时刻核对
        日期，跨天即切到当天的新文件。只有写线程调用它，不需要加锁。
        """
        today = datetime.now().strftime("%Y%m%d")
        if today != self._log_date:
            self._log_date = today
            self.log_file = self.log_dir / f"{self._session_id}_{today}.jsonl"
        return self.log_file

    def _write_entry(self, entry: LogEntry):
        """实际写入文件"""
        try:
            with open(self._current_log_file(), "a", encoding="utf-8") as f:
                f.write(entry.to_json(indent=LOG_JSON_INDENT) + "\n")
                f.flush()
        except Exception:
            pass

    def _should_sample(self, level: str) -> bool:
        """采样判断：DEBUG 日志按采样率记录"""
        if level != "DEBUG":
            return True
        if LOG_SAMPLING_RATE <= 0:
            return False
        if LOG_SAMPLING_RATE >= 1.0:
            return True
        self._debug_counter += 1
        if self._debug_counter % int(1 / LOG_SAMPLING_RATE) == 0:
            return True
        self._debug_skip_count += 1
        return False

    def _check_backpressure(self):
        """检查背压：队列积压超过阈值则触发降级（仅写日志到文件，不 print）"""
        qsize = self._queue.qsize()
        if qsize > LOG_BACKPRESSURE_THRESHOLD and not self._backpressure_active:
            self._backpressure_active = True
            # 背压激活日志直接入队（不 print）
            self.log(
                "WARNING",
                f"日志队列积压 {qsize} 条，超过阈值 {LOG_BACKPRESSURE_THRESHOLD}，触发背压降级",
                extra={"qsize": qsize},
            )
        elif qsize < LOG_BACKPRESSURE_THRESHOLD * 0.5 and self._backpressure_active:
            self._backpressure_active = False

    def log(
        self,
        level: str,
        message: str,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """写入一条日志（主入口）"""
        level_upper = level.upper()
        level_value = LEVELS.get(level_upper, logging.INFO)

        if level_value < CURRENT_LEVEL:
            return

        if not self._should_sample(level_upper):
            return

        self._check_backpressure()

        if self._backpressure_active and level_value <= logging.INFO:
            return

        entry = LogEntry(
            level=level_upper,
            message=message,
            session_id=self.session_id,
            trace_id=trace_id or self.session_id,
            span_id=span_id,
            extra=extra,
        )

        try:
            self._queue.put(entry, block=False)
        except queue.Full:
            # 队列满时静默丢弃（绝不 print 到终端）
            pass

    def debug(self, message: str, **extra):
        self.log("DEBUG", message, extra=extra)

    def info(self, message: str, **extra):
        self.log("INFO", message, extra=extra)

    def warning(self, message: str, **extra):
        self.log("WARNING", message, extra=extra)

    def error(self, message: str, **extra):
        self.log("ERROR", message, extra=extra)

    def close(self):
        """关闭写入器，排空队列后退出。

        修复（2026-09-03）：原实现先置 _running=False 再入队哨兵，worker 写完
        当前一条后检查 while 条件发现 False 立即退出，丢弃队列中所有剩余日志
        （快节奏执行后 close 会丢日志——表现为文件只有第一条）。现改为哨兵排空：
        worker 先消费完此前全部日志，遇 None 哨兵才退出。

        采样汇总（2026-09-17，审计 B6）：`_debug_skip_count` 此前只累加、无人读，
        于是无法回答"这次排查里有多少 DEBUG 被采样掉了"，容易把"日志里没有"
        误读成"事情没发生"。收尾时落一条 WARNING 把数字交出去。
        """
        if self._debug_skip_count:
            self.log(
                "WARNING",
                f"DEBUG 采样摘要：本会话共丢弃 {self._debug_skip_count} 条 "
                f"（采样率 {LOG_SAMPLING_RATE}）—— 排查时请把这段计入'没看到的日志'",
                extra={"dropped_debug": self._debug_skip_count,
                       "log_sampling_rate": LOG_SAMPLING_RATE},
            )
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            # 队列满无法入哨兵：交给背压降级（worker 退出时丢弃剩余）
            pass
        if self._worker.is_alive():
            self._worker.join(timeout=3.0)

    @classmethod
    def get(cls, session_id: str) -> Optional["AsyncLogWriter"]:
        return cls._instances.get(session_id)


# ==================== SessionLogger（统一入口） ====================

class SessionLogger:
    """会话级日志入口（对外统一接口）"""

    def __init__(
        self,
        session_id: str,
        log_dir: Optional[Path] = None,
        log_level: Optional[str] = None,
    ):
        self.session_id = session_id

        if log_dir is None:
            project_root = Path(__file__).parent.parent.absolute()
            log_dir = project_root / "agent_logs"
        self.log_dir = log_dir

        self._writer = AsyncLogWriter(session_id, log_dir)

        global CURRENT_LEVEL
        if log_level:
            new_level = LEVELS.get(log_level.upper(), logging.INFO)
            if new_level != CURRENT_LEVEL:
                CURRENT_LEVEL = new_level

    def log(self, level: str, message: str, **extra):
        self._writer.log(level, message, extra=extra)

    def debug(self, message: str, **extra):
        self._writer.debug(message, **extra)

    def info(self, message: str, **extra):
        self._writer.info(message, **extra)

    def warning(self, message: str, **extra):
        self._writer.warning(message, **extra)

    def error(self, message: str, **extra):
        self._writer.error(message, **extra)

    def with_span(self, span_id: str):
        """返回一个绑定 span_id 的子日志器（用于工具调用链路）"""
        return _BoundLogger(self, span_id)

    def close(self):
        self._writer.close()

    @classmethod
    def get(cls, session_id: str) -> Optional["SessionLogger"]:
        writer = AsyncLogWriter.get(session_id)
        if writer:
            return cls(session_id, writer.log_dir)
        return None


class _BoundLogger:
    """绑定 span_id 的日志器"""

    def __init__(self, parent: SessionLogger, span_id: str):
        self._parent = parent
        self._span_id = span_id

    def log(self, level: str, message: str, **extra):
        self._parent._writer.log(level, message, span_id=self._span_id, extra=extra)

    def debug(self, message: str, **extra):
        self.log("DEBUG", message, **extra)

    def info(self, message: str, **extra):
        self.log("INFO", message, **extra)

    def warning(self, message: str, **extra):
        self.log("WARNING", message, **extra)

    def error(self, message: str, **extra):
        self.log("ERROR", message, **extra)