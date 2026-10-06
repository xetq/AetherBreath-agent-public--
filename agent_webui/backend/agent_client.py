# -*- coding: utf-8 -*-
"""bridge 本地 HTTP 客户端（仅用标准库 urllib，不给网关加依赖）。

SSE 消费走独立线程 + 回调，断线自动重连（指数退避，上限 5s）。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

TOKEN_HEADER = "X-Bridge-Token"


class BridgeError(Exception):
    pass


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------- 基础 ----------
    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: Optional[float] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {TOKEN_HEADER: self.token, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except Exception:
                raise BridgeError(f"bridge {path} 返回 {e.code}: {raw[:300]}") from e
            parsed["_http_status"] = e.code
            return parsed
        except Exception as e:  # URLError / timeout / 连接被拒
            raise BridgeError(f"bridge {path} 请求失败: {type(e).__name__}: {e}") from e
        try:
            return json.loads(raw)
        except Exception as e:
            raise BridgeError(f"bridge {path} 返回非 JSON: {raw[:300]}") from e

    # ---------- 端点 ----------
    def health(self, timeout: float = 8) -> Dict[str, Any]:
        # timeout 可短化：网关 status 的忙碌态自愈核对用它（默认值不变，其它调用方零改动）。
        return self._request("GET", "/health", timeout=timeout)

    def tools(self) -> Dict[str, Any]:
        return self._request("GET", "/tools", timeout=8)

    def chat(self, session_id: str, message: str, attachments=None) -> Dict[str, Any]:
        """发起回合。attachments = 附件相对路径列表（可为空 → 请求体与旧版完全一致）。"""
        body: Dict[str, Any] = {"session_id": session_id, "message": message}
        if attachments:
            body["attachments"] = list(attachments)
        return self._request("POST", "/chat", body, timeout=20)

    def stop(self, run_id: Optional[str] = None,
             timeout: Optional[float] = 15.0) -> Dict[str, Any]:
        return self._request("POST", "/stop", {"run_id": run_id}, timeout=timeout)

    def mid_turn(self, session_id: str, text: str,
                 run_id: Optional[str] = None, attachments=None) -> Dict[str, Any]:
        """回合运行中追加「用户交代」（可带附件）：投给当前活动回合，随下一批工具返回注入模型。"""
        body: Dict[str, Any] = {"session_id": session_id, "text": text, "run_id": run_id}
        if attachments:
            body["attachments"] = list(attachments)
        return self._request("POST", "/mid_turn", body, timeout=10)

    def ask_answer(self, ask_id: str, answer: str) -> Dict[str, Any]:
        return self._request("POST", "/ask/answer",
                             {"ask_id": ask_id, "answer": answer}, timeout=10)

    def approval_answer(self, ask_id: str, choice: str = "",
                        payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把人类裁决送回 bridge；合并卡走 payload.approved。"""
        body: Dict[str, Any] = {"ask_id": ask_id}
        if choice:
            body["choice"] = choice
        # 整包透传，不再逐字段挑白名单：审批字段该由通道与引擎自己解释。
        # 网关每维护一份白名单，就多一个「新字段静默消失」的坑 —— note 就是这么丢的。
        for k, val in (payload or {}).items():
            if val is not None:
                body[k] = val
        return self._request("POST", "/approval/answer", body, timeout=10)

    def approval_pending(self) -> Dict[str, Any]:
        """当前挂起的审批卡片。前端刷新/切会话/重开页面后靠它恢复。"""
        return self._request("GET", "/approval/pending", timeout=8)

    def ask_pending(self) -> Dict[str, Any]:
        return self._request("GET", "/ask/pending", timeout=8)

    def orch(self) -> Dict[str, Any]:
        """编排器占用视图（跨回合作业占着哪些槽位）—— 前端刷新/重连后对表用。"""
        return self._request("GET", "/orch", timeout=5)

    def approval_rules(self, session_id: str = "") -> Dict[str, Any]:
        return self._request("POST", "/approval/rules",
                             {"session_id": session_id}, timeout=10)

    def approval_revoke(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # 整包透传，不列白名单：新字段不会再被静默吃掉（note 就是这么丢的）
        return self._request("POST", "/approval/rules/revoke",
                             dict(payload or {}), timeout=10)

    def permission_mode(self, session_id: str,
                        mode: Optional[str] = None) -> Dict[str, Any]:
        """会话级权限模式：带 mode = 切换，不带 = 查询（2026-10）。

        切换必须走 bridge 而不是直接改会话文件：引擎就在 bridge 进程里，模式要
        **当场**写进它的缓存，下一次工具调用才按新模式判。文件那一份由 bridge 负责落。
        """
        body: Dict[str, Any] = {"session_id": session_id}
        if mode is not None:
            body["mode"] = mode
        return self._request("POST", "/permission/mode", body, timeout=10)

    def exit_gracefully(self, wait_sec: float) -> Dict[str, Any]:
        return self._request("POST", "/exit", {"timeout": wait_sec}, timeout=10)

    # ---------- SSE ----------
    def stream_events(self, on_event: Callable[[Dict[str, Any]], None],
                      stop_flag: threading.Event,
                      on_reconnect_fail: Optional[Callable[[str], None]] = None,
                      max_failures: int = 3) -> None:
        """长连接消费 bridge 事件；在独立线程里调用。"""
        url = f"{self.base_url}/events?{TOKEN_HEADER}={self.token}"
        failures = 0
        backoff = 0.5
        while not stop_flag.is_set() and failures < max_failures:
            try:
                req = urllib.request.Request(
                    url, headers={TOKEN_HEADER: self.token, "Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=None) as resp:
                    failures = 0
                    backoff = 0.5
                    self._pump(resp, on_event, stop_flag)
                break
            except Exception as e:  # 连接失败/中断 -> 退避重连
                failures += 1
                msg = f"{type(e).__name__}: {e}"
                if on_reconnect_fail:
                    on_reconnect_fail(msg)
                if failures >= max_failures or stop_flag.is_set():
                    break
                stop_flag.wait(backoff)
                backoff = min(backoff * 2, 5.0)

    @staticmethod
    def _pump(resp, on_event: Callable[[Dict[str, Any]], None],
              stop_flag: threading.Event) -> None:
        data_lines: list = []
        while not stop_flag.is_set():
            chunk = resp.readline()
            if not chunk:
                return  # 服务端关闭
            try:
                line = chunk.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
                continue
            if line == "":
                if not data_lines:
                    continue
                blob = "\n".join(data_lines)
                data_lines.clear()
                try:
                    evt = json.loads(blob)
                except Exception:
                    continue
                try:
                    on_event(evt)
                except Exception:
                    pass
