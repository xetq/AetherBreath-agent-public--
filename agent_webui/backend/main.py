# -*- coding: utf-8 -*-
"""AetherBreath WebUI 网关入口。

两种运行模式：
  开发模式：本进程(8900) + frontend `npm run dev`(5173, proxy /api)
  日常模式：先 npm run build，再由本进程托管 frontend/dist（单命令）
启动：
  venv-gateway\Scripts\python -m uvicorn backend.main:app --port 8900
  或  venv-gateway\Scripts\python backend\main.py
"""
from __future__ import annotations

import atexit
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Windows GBK 控制台下中文日志可能直接 UnicodeEncodeError，先统一 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 允许 `python backend/main.py` 直跑（把 backend/ 加入 sys.path）
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import config  # noqa: E402
from agent_proc import cleanup_orphan_bridges, get_manager  # noqa: E402
from api import router  # noqa: E402

_PLACEHOLDER = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>AetherBreath WebUI</title>
<style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{max-width:640px;padding:36px;border:1px solid #30363d;border-radius:12px;background:#161b22}
code{background:#21262d;padding:2px 7px;border-radius:5px;color:#79c0ff;font-size:13px}
h1{font-size:20px;margin:0 0 16px}</style></head><body><div class="card">
<h1>☲ AetherBreath WebUI 网关已运行</h1>
<p>未找到前端构建产物 <code>{dist}</code>。二选一：</p>
<p><b>日常模式</b>（本端口直接访问界面）：<br>
<code>cd frontend && npm install && npm run build</code></p>
<p><b>开发模式</b>（热更新，另开终端）：<br>
<code>cd frontend && npm run dev</code> → 浏览器打开 <code>http://127.0.0.1:5173</code></p>
<p>API 文档：<a style="color:#79c0ff" href="/docs">/docs</a> ·
状态接口：<a style="color:#79c0ff" href="/api/health">/api/health</a></p>
</div></body></html>"""


def _cors_origins() -> list:
    raw = os.environ.get("AETHER_CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # 默认只放开本地 dev server（生产同源，无需 CORS）
    return ["http://127.0.0.1:5173", "http://localhost:5173"]


def _silence_pipe_noise() -> None:
    """浏览器硬断 SSE 时，Windows proactor 会在 connection_lost 回调里抛
    ConnectionResetError 刷屏 —— 那是常态而非故障，只压掉这一类，其余照旧上报。"""
    try:
        import asyncio

        loop = asyncio.get_running_loop()
        orig = loop.get_exception_handler() or loop.default_exception_handler

        def _h(lo, ctx):
            exc = ctx.get("exception")
            msg = str(ctx.get("message") or "")
            if isinstance(exc, (ConnectionResetError, BrokenPipeError)) and "connection_lost" in msg:
                return
            orig(lo, ctx)

        loop.set_exception_handler(_h)
    except Exception:
        pass


class _Tee:
    """stdout/stderr 同时抄一份进文件；终端照常可见，取不到就退回纯终端。"""

    def __init__(self, console, fh):
        self.console, self.fh = console, fh

    def write(self, data):
        for w in (getattr(self, "console", None), getattr(self, "fh", None)):
            try:
                if w is not None:
                    w.write(data)
                    if w is self.fh:
                        w.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for x in (self.console, self.fh):
            try:
                if x is not None:
                    x.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self.console and self.console.isatty())
        except Exception:
            return False

    def __getattr__(self, k):
        return getattr(self.console, k) if self.console else lambda *a, **kw: None


def _open_tee_log(keep: int = 5) -> None:
    """网关自己的文件日志：按启动时间命名 + 追加 + 只留最近 keep 份。
    此前完全依赖外部重定向，而重定向每次启动都覆盖上一份 —— 排查进程事故等于没日志。"""
    try:
        config.WEBUI_LOG_DIR.mkdir(parents=True, exist_ok=True)
        old = sorted(config.WEBUI_LOG_DIR.glob("gateway_*.log"),
                     key=lambda q: q.stat().st_mtime, reverse=True)[keep:]
        for q in old:
            try:
                q.unlink()
            except OSError:
                pass
        path = config.WEBUI_LOG_DIR / ("gateway_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
        fh = open(path, "a", encoding="utf-8", errors="replace")
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
        print(f"   日志（追加，留最近 {keep} 份）: {path.name}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"   文件日志不可用，仅终端输出：{type(e).__name__}: {e}", file=sys.stderr, flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mgr = get_manager()
    atexit.register(mgr.shutdown_if_running)
    _silence_pipe_noise()
    res = cleanup_orphan_bridges()
    if res.get("killed"):
        print(f"🧹 已回收 {res['killed']} 个遗留 bridge（候选 {res.get('candidates')}）",
              file=sys.stderr, flush=True)
    if res.get("survivors"):
        print(f"⚠️ 有 {len(res['survivors'])} 个 bridge 回收失败，需手动处理：{res['survivors']}"
              f"（{res.get('notes')}）", file=sys.stderr, flush=True)
    if res.get("kept"):
        print(f"🛡️ 已保护 {len(res['kept'])} 个正被活网关使用的 bridge：{res['kept']}",
              file=sys.stderr, flush=True)
    if res.get("skipped"):
        print(f"ℹ️ 本次未做孤儿回收：{res['skipped']}", file=sys.stderr, flush=True)
    print(f"☲ WebUI 网关启动: http://{config.GATEWAY_HOST}:{config.GATEWAY_PORT}",
          file=sys.stderr, flush=True)
    print(f"   AB 本体解释器: {config.AGENT_PYTHON}", file=sys.stderr, flush=True)
    print(f"   前端产物: {config.FRONTEND_DIST} (存在={config.FRONTEND_DIST.exists()})",
          file=sys.stderr, flush=True)
    print("   AB 进程需手动开机：点界面「🟢 开机」或 POST /api/agent/start",
          file=sys.stderr, flush=True)
    yield
    mgr.shutdown_if_running()


app = FastAPI(
    title="AetherBreath WebUI Gateway",
    version="1.0.0",
    description="AB 本体的前后端分离 WebUI 网关：会话/回合/工具时间线/工作区/电源控制。",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    ico = config.FRONTEND_DIST / "favicon.svg"
    if ico.exists():
        return FileResponse(ico, media_type="image/svg+xml")
    raise HTTPException(404)


_dist = config.FRONTEND_DIST
if _dist.exists() and (_dist / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str):
    """SPA 托管 + fallback：刷新任意前端路由都不 404。"""
    if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
        raise HTTPException(404)
    dist = config.FRONTEND_DIST
    index = dist / "index.html"
    if not index.exists():
        return HTMLResponse(_PLACEHOLDER.replace("{dist}", str(dist)), status_code=200)
    candidate: Optional[Path] = None
    if full_path:
        candidate = (dist / full_path).resolve()
        inside = dist.resolve() in candidate.parents or candidate == dist.resolve()
        if inside and candidate.is_file():
            return FileResponse(candidate)
    # index.html 绝不缓存：它引用带哈希的 assets，缓存它会让人停在旧界面（实测踩过）
    return FileResponse(index, headers={"Cache-Control": "no-store"})


@app.exception_handler(Exception)
async def unhandled(request, exc):  # noqa: ARG001
    return JSONResponse(status_code=500, content={"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _assert_port_free() -> None:
    """绑定预检：uvicorn 的 lifespan 会先回收孤儿 bridge 再报 bind 失败，
    顺序反了就会误杀"另一个网关正在用的" AB 进程。故先探端口，不通就直接退出。
    """
    import os
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":  # Windows 上设 SO_REUSEADDR 反而允许抢占，失去探测意义
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((config.GATEWAY_HOST, config.GATEWAY_PORT))
    except OSError as e:
        raise SystemExit(
            f"[gateway] 端口 {config.GATEWAY_HOST}:{config.GATEWAY_PORT} 已被占用（{e.__class__.__name__}: {e}）。"
            f" 同一时间只能有一个网关；先在旧终端 Ctrl+C，或用 AETHER_WEBUI_PORT 换端口。"
            f" 本次启动已中止，未回收任何 bridge 进程。"
        )
    finally:
        probe.close()


def run() -> None:
    import uvicorn

    _assert_port_free()
    _open_tee_log()
    uvicorn.run(app, host=config.GATEWAY_HOST, port=config.GATEWAY_PORT, log_level="info")


if __name__ == "__main__":
    run()
