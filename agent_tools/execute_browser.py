# agent_tools/execute_browser.py
"""browser-use 集成工具：让 AI 通过真实浏览器执行自动化任务。

基于 browser-use 0.13.x（MIT License，github.com/browser-use/browser-use）。
本工具自动适配各家 OpenAI 兼容端点：DeepSeek 端点内置适配层（browser-use
原版 ChatDeepSeek 在结构化输出时强制指定 tool_choice，而 DeepSeek thinking
模型只接受 tool_choice="auto"，原版会报 "Thinking mode does not support this
tool_choice"，此处用子类覆盖）；其余厂商走通用 ChatOpenAI。

依赖：
    pip install browser-use playwright
    系统需安装 Edge/Chrome（或用 playwright install chromium 下载内核）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import logging
import threading
from contextlib import contextmanager
from typing import Any
from io import StringIO

# ========== 抑制第三方库的终端日志输出 ==========
# 在导入 browser-use 之前设置日志级别，防止它输出到终端
logging.getLogger("browser_use").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ========== 路径推导（GitHub 友好：不硬编码绝对路径） ==========
_TOOL_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_env_path() -> str | None:
    """向上回溯查找 .env（项目根 .env 或 agent/.env 均可；工具可能部署在临时目录）。"""
    cur = _TOOL_DIR
    for _ in range(6):
        for cand in (os.path.join(cur, ".env"), os.path.join(cur, "agent", ".env")):
            if os.path.isfile(cand):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _load_env() -> dict[str, str]:
    """从项目 .env 读取 LLM 配置（key 不出现在返回值外）。"""
    env_path = _find_env_path()
    env: dict[str, str] = {}
    if not env_path:
        return env
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


class _ChatDeepSeekAuto:
    """ChatDeepSeek 适配子类：output_format 时 tool_choice 使用 'auto'。

    browser-use 原版 ChatDeepSeek.ainvoke 在结构化输出路径会发送
    tool_choice={'type':'function','function':{...}}，DeepSeek thinking 模式
    返回 400 "Thinking mode does not support this tool_choice"。本类覆盖该路径。
    """

    def __new__(cls, *args, **kwargs):
        from browser_use.llm.deepseek.chat import ChatDeepSeek

        class _Impl(ChatDeepSeek):
            async def ainvoke(self, messages, output_format=None, tools=None, stop=None, **kw):
                if output_format is not None and hasattr(output_format, "model_json_schema"):
                    schema = output_format.model_json_schema()
                    schema.pop("title", None)
                    tool_name = output_format.__name__
                    call_tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": f"Return a JSON object of type {tool_name}",
                                "parameters": schema,
                            },
                        }
                    ]
                    client = self._client()
                    from browser_use.llm.deepseek.serializer import DeepSeekMessageSerializer
                    from browser_use.llm.views import ChatInvokeCompletion

                    ds_messages = DeepSeekMessageSerializer.serialize_messages(messages)
                    common: dict[str, Any] = {}
                    if self.temperature is not None:
                        common["temperature"] = self.temperature
                    if self.max_tokens is not None:
                        common["max_tokens"] = self.max_tokens
                    if self.top_p is not None:
                        common["top_p"] = self.top_p
                    if self.seed is not None:
                        common["seed"] = self.seed
                    resp = await client.chat.completions.create(
                        model=self.model,
                        messages=ds_messages,
                        tools=call_tools,
                        tool_choice="auto",
                        **common,
                    )
                    msg = resp.choices[0].message
                    if not msg.tool_calls:
                        raise ValueError("Expected tool_calls in response but got none (auto tool_choice)")
                    raw = msg.tool_calls[0].function.arguments
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                    return ChatInvokeCompletion(completion=output_format.model_validate(parsed), usage=None)
                return await super().ainvoke(messages, output_format, tools, stop, **kw)

        return _Impl(*args, **kwargs)


# 审计 B17：默认**不**全局替换 stdout/stderr。
# 旧实现无条件把进程的 sys.stdout/stderr 换成 StringIO，一换就是整个浏览器任务
# 期间（可能几分钟）：这期间**其它线程**的任何 print 都被吞（WebUI 的事件泵、
# 日志线程都在跑），而且两次调用并发时还会互相把对方复原成展品。
# browser-use 的终端噪音已由模块顶部的 setLevel(WARNING) 压住，够用了；
# 真要静音再显式设 AETHER_BROWSER_SUPPRESS_STDOUT=1（启用时全程持锁）。
_SUPPRESS_STDOUT = os.environ.get("AETHER_BROWSER_SUPPRESS_STDOUT", "0") == "1"
_SUPPRESS_LOCK = threading.Lock()


@contextmanager
def _suppress_stdout():
    """抑制 stdout/stderr（默认关闭；开启时持锁，且只影响本线程期间的输出）。"""
    if not _SUPPRESS_STDOUT:
        yield
        return
    with _SUPPRESS_LOCK:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()
        try:
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


def _run_async(coro, suppress_output: bool = True):
    """在同步上下文运行 asyncio 协程（工具函数为同步入口）。"""
    if suppress_output:
        with _suppress_stdout():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()
    else:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()


import re as _re


def _extract_urls(text: str) -> list:
    """从自然语言文本中提取 http(s) URL。"""
    return _re.findall(r"https?://[^\s<>\"]+", text)


def _lightweight_fetch(url: str, max_chars: int = 8000) -> str | None:
    """轻量级 HTTP GET（不走 LLM，不启浏览器），返回正文或 None。

    审计 B9：这条降级路径以前**完全没走安全闸** —— execute_browser.py 里没有
    任何 url_safety 引用，而 URL 是从任务文本里正则抠出来的，等于给 SSRF 开了
    另一扇门（两个抓取工具都拦 `http://127.0.0.1/`，这里却直通）。
    现在与 fetch_url / web_extract 共用同一套闸门（含重定向逐跳校验）。
    """
    try:
        try:
            from .url_safety import safe_get, UnsafeURLError
        except ImportError:
            from url_safety import safe_get, UnsafeURLError
    except Exception:
        return None
    try:
        resp = safe_get(url, headers={"User-Agent": "Mozilla/5.0 AetherBreath/1.0"},
                        timeout=15)
        raw = resp.content
        text = raw.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... (truncated, {} bytes total)".format(len(raw))
        return text
    except UnsafeURLError:
        return None
    except Exception:
        return None


def execute_browser(
    task: str,
    headless: bool = True,
    model: str | None = None,
    channel: str = "msedge",
    max_steps: int | None = None,
    timeout: int = 180,
    logger=None,  # 日志实例（由编排器或agent.py传入）
) -> dict:
    """使用 browser-use Agent 在真实浏览器中执行任务。

    Args:
        task: 自然语言任务描述，例如 "打开 example.com 并把页面标题告诉我"。
        headless: 是否无头模式。True 不弹窗（默认）；False 可见浏览器窗口。
        model: LLM 模型名，默认读取 .env 的 LLM_MODEL（旧键 MODEL_NAME 兼容）。
        channel: 浏览器通道，默认 msedge（系统 Edge）。也可用 chrome / chromium。
        max_steps: 最大 Agent 步数限制（默认不限制）。
        timeout: 单步超时秒数。
        logger: 可选的日志实例（SessionLogger），用于记录进度到文件。

    Returns:
        dict: {"success": bool, "result": str|None, "error": str|None, "steps": int}
    """
    env = _load_env()
    # 通用 OpenAI 兼容配置（LLM_* 优先，旧键名 DEEPSEEK_*/MODEL_NAME 兼容）
    api_key = env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY", "")
    base_url = env.get("LLM_BASE_URL") or env.get("DEEPSEEK_BASE_URL", "")
    model = model or env.get("LLM_MODEL") or env.get("MODEL_NAME", "") or "deepseek-chat"
    if not api_key:
        return {"success": False, "result": None, "error": "缺少 LLM_API_KEY（请在项目根或 agent/.env 中配置，参考 .env.example）", "steps": 0}

    # ===== 优先使用传入的 logger，如果没有则尝试从全局上下文获取 =====
    if logger is None:
        try:
            from logger import get_current_logger
            logger = get_current_logger()
        except ImportError:
            pass

    # 记录开始（如果有 logger）
    if logger:
        logger.info(f"浏览器任务开始: {task[:100]}")

    async def _main():
        from browser_use import Agent, BrowserProfile

        profile = BrowserProfile(
            channel=channel,
            headless=headless,
            enable_default_extensions=False,
        )
        # 自动选型：DeepSeek 端点走专用适配（thinking 模型只接受 tool_choice=auto），
        # 其余 OpenAI 兼容端点（智谱/硅基流动/Moonshot/Ollama...）用通用 ChatOpenAI。
        is_deepseek = ("deepseek" in str(base_url).lower()) or ("deepseek" in model.lower())
        if is_deepseek:
            llm = _ChatDeepSeekAuto(model=model, api_key=api_key, base_url=base_url)
        else:
            from browser_use.llm.openai.chat import ChatOpenAI
            # reasoning_effort 默认 'low' 会被发给所有端点，多数厂商不认 → 置 None 不发送
            llm = ChatOpenAI(
                model=model, api_key=api_key, base_url=base_url,
                reasoning_effort=None,
            )
        # 审计 B16：`max_steps` / `timeout` 两个 schema 参数以前是**死参数**
        # （Agent 构造里根本没传，模型写了也无效）。实测 browser-use 的签名：
        #   · max_steps 不在 Agent 上，而在 `agent.run(max_steps=...)`；
        #   · 单步超时是 Agent 的 `step_timeout`（默认 180）。
        agent_kwargs = {}
        if timeout:
            try:
                agent_kwargs["step_timeout"] = int(timeout)
            except (TypeError, ValueError):
                pass
        agent = Agent(
            task=task,
            llm=llm,
            browser_profile=profile,
            use_thinking=False,
            max_failures=3,
            **agent_kwargs,
        )
        run_method = getattr(agent, "run")
        run_kwargs = {}
        if max_steps:
            try:
                run_kwargs["max_steps"] = int(max_steps)
            except (TypeError, ValueError):
                pass
        history = await run_method(**run_kwargs)
        final = history.final_result() if hasattr(history, "final_result") else str(history)[:2000]
        n_steps = len(history) if hasattr(history, "__len__") else 0
        return final, n_steps

    try:
        # 执行时抑制终端输出
        final, n_steps = _run_async(_main(), suppress_output=True)

        # ===== result=null 防御：browser-use 跑完但没产出文本时，尝试轻量降级 =====
        fallback_note = ""
        if not final or not final.strip():
            urls = _extract_urls(task)
            if urls:
                fetched = _lightweight_fetch(urls[0])
                if fetched:
                    final = f"[轻量降级: browser-use 未产出结果，已用 HTTP GET 直接获取]\nURL: {urls[0]}\n\n{fetched}"
                    fallback_note = " (fallback)"
            # 依然无结果 -> 判定失败
            if not final or not final.strip():
                if logger:
                    logger.warning(f"浏览器任务无结果: {n_steps} 步完成但 final_result() 为空")
                return {
                    "success": False,
                    "result": None,
                    "error": f"browser-use 执行了 {n_steps} 步但未产出有效结果（final_result() 为空）。任务: {task[:200]}",
                    "steps": n_steps,
                }

        if logger:
            logger.info(f"浏览器任务完成{fallback_note}: {n_steps} 步")

        return {"success": True, "result": final, "error": None, "steps": n_steps}

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        if logger:
            logger.error(f"浏览器任务失败: {error_msg}")
        return {"success": False, "result": None, "error": error_msg, "steps": 0}


execute_browser_schema = {
    "type": "function",
    "function": {
        "name": "execute_browser",
        "description": (
            "在真实浏览器中执行 AI 自动化任务。"
            "可打开网页、点击、填表、提取数据、提交表单等。返回任务最终结果文本。"
            "注意：纯信息读取任务请优先使用 fetch_url（更轻量快速）；"
            "需要浏览器交互（点击/填表/登录/JS渲染）时建议使用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "自然语言任务描述，如：打开 example.com 并返回页面标题"
                },
                "headless": {
                    "type": "boolean",
                    "description": "无头模式（默认 True，不弹浏览器窗口）",
                    "default": True
                },
                "model": {
                    "type": "string",
                    "description": "LLM 模型名（默认读 .env 的 LLM_MODEL）"
                },
                "channel": {
                    "type": "string",
                    "description": "浏览器通道：msedge（默认）/ chrome / chromium"
                },
                "max_steps": {
                    "type": "integer",
                    "description": "最大 Agent 步数限制（可选）"
                },
                "timeout": {
                    "type": "integer",
                    "description": "单步超时秒数（默认 180）",
                    "default": 180
                }
            },
            "required": ["task"]
        }
    }
}

__all__ = ["execute_browser", "execute_browser_schema"]