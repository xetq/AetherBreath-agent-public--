#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mcp_stub_server —— 测试用的假 MCP server（纯 stdlib，换行分隔 JSON-RPC）。

它不是产品代码，是**夹具**：让 L1 单测能覆盖手写 MCP 客户端最容易错的地方 ——
分帧、对号入座、超时 kill、stderr 背压、错误映射 —— 而完全不依赖 npx / 网络 / 真 server。

行为由 `--behavior` 指定（默认 normal）：

  normal        正常：initialize / tools/list / tools/call 都按协议回
  error         正常，但 tools/call 回 isError: true
  slow          tools/call 先睡 N 秒再回（配合客户端 timeout 测真超时）
  hang          tools/call 永不回（测超时后 kill）
  stderr_flood  tools/call 前往 stderr 灌大量内容（测 stderr 有没有被排空）
  stray         tools/call 前先发一个**别人的 id** + 一条通知 + 一行畸形内容（测对号入座）
  crash         initialize 之后立刻退出（测进程死亡）
  garbage       initialize 收到非 JSON 的一行（测协议错误）
  bad_version   initialize 回一个不同的 protocolVersion（测宽容协商）
  paginate      tools/list 分两页返回（测翻页）

工具集：echo(text) / picture() / resource() / fail() / note()

`--log <path>` 会把**收到的每一条**消息按行写成 JSON（测试据此断言客户端确实发了
`notifications/initialized`、请求顺序对不对）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

# 1x1 的透明 PNG（32 字节），用来测图片落盘
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/"
           "q842iQAAAABJRU5ErkJggg==")

TOOLS = [
    {"name": "echo", "description": "回显文本",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                     "required": ["text"]}},
    {"name": "picture", "description": "返回一张 1x1 的图片",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "resource", "description": "返回一个资源引用",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "总是报错（isError: true）",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "note", "description": "返回一段长文本",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Stub:
    def __init__(self, behavior: str, log_path: str = "", delay: float = 3.0):
        self.behavior = behavior
        self.delay = delay
        self.log_path = log_path

    # ---------- 基础设施 ----------

    def send(self, payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def send_raw(self, text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def log_in(self, msg) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def reply(self, mid, result) -> None:
        self.send({"jsonrpc": "2.0", "id": mid, "result": result})

    # ---------- 方法 ----------

    def on_initialize(self, mid, params) -> None:
        if self.behavior == "garbage":
            self.send_raw("这不是 JSON {{{")
            return
        version = "1900-01-01" if self.behavior == "bad_version" else str(
            (params or {}).get("protocolVersion") or "2025-06-18")
        self.reply(mid, {"protocolVersion": version,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "mcp-stub", "version": "0.1.0"}})

    def on_tools_list(self, mid, params) -> None:
        cursor = (params or {}).get("cursor")
        if self.behavior == "paginate":
            if not cursor:
                self.reply(mid, {"tools": TOOLS[:2], "nextCursor": "page2"})
            else:
                self.reply(mid, {"tools": TOOLS[2:]})
            return
        self.reply(mid, {"tools": TOOLS})

    def _content_for(self, name: str, args: dict):
        if name == "echo":
            return [{"type": "text", "text": "echo: %s" % (args or {}).get("text", "")}]
        if name == "picture":
            return [{"type": "image", "mimeType": "image/png", "data": PNG_B64}]
        if name == "resource":
            return [{"type": "resource",
                     "resource": {"uri": "stub://doc/1", "mimeType": "text/plain",
                                  "text": "资源正文"}}]
        if name == "note":
            return [{"type": "text", "text": "x" * 500}]
        return [{"type": "text", "text": "未知工具 %s" % name}], True

    def on_tools_call(self, mid, params) -> None:
        name = str((params or {}).get("name") or "")
        args = (params or {}).get("arguments") or {}

        if self.behavior == "hang":
            return                                     # 永不回：让客户端超时并 kill
        if self.behavior == "stderr_flood":
            for i in range(4000):                      # 灌满 stderr 管道（没人读就会卡死）
                sys.stderr.write("stderr line %d %s\n" % (i, "y" * 200))
            sys.stderr.flush()
        if self.behavior == "slow":
            time.sleep(self.delay)
        if self.behavior == "stray":
            # 先发一个别人 id 的响应 + 一条通知 + 一行畸形内容，再发真正的响应
            self.send({"jsonrpc": "2.0", "id": 999999, "result": {"tools": []}})
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progress": 1}})
            self.send_raw("{oops")

        content = self._content_for(name, args)
        is_err = False
        if isinstance(content, tuple):
            content, is_err = content
        if self.behavior == "error" or name == "fail":
            is_err = True
            content = [{"type": "text", "text": "这个工具故意失败了"}]
        self.reply(mid, {"content": content, "isError": is_err})

    # ---------- 主循环 ----------

    def serve(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self.log_in({"__unparsable__": line[:200]})
                continue
            self.log_in(msg)
            method = str(msg.get("method") or "")
            mid = msg.get("id")
            if method == "initialize":
                self.on_initialize(mid, msg.get("params") or {})
                if self.behavior == "crash":
                    return 0
            elif method == "notifications/initialized":
                pass                                   # 通知，无需回复
            elif method == "tools/list":
                self.on_tools_list(mid, msg.get("params") or {})
            elif method == "tools/call":
                self.on_tools_call(mid, msg.get("params") or {})
            elif mid is not None:
                self.send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32601, "message": "method not found: %s" % method}})
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="测试用假 MCP server")
    ap.add_argument("--behavior", default="normal")
    ap.add_argument("--log", default="")
    ap.add_argument("--delay", type=float, default=3.0)
    args = ap.parse_args(argv)
    return Stub(args.behavior, args.log, args.delay).serve()


if __name__ == "__main__":
    sys.exit(main())
