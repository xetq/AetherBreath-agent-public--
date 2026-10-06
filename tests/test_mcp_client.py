# -*- coding: utf-8 -*-
"""MCP 客户端 L1 回归 —— agent/mcp_client.py + approvals/mcp_spawn.py

判据（照 tests/README.md）：**一条命令、无需 LLM、无需网关、不写真实文件**。
本文件的"真进程"只有一个：`tests/mcp_stub_server.py`（纯 stdlib 的假 MCP server，
由本机 python 拉起，不联网、不装包）。这不是"执行真命令"，而是本模块唯一能验证
**换行分帧 / 对号入座 / 超时 kill / stderr 背压**的方式 —— 这四处正是手写实现最
容易错的地方，桩函数测不到。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_mcp_client.py -q
    venv/Scripts/python tests/test_mcp_client.py          # 独立直跑，自带汇总
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = PROJECT_ROOT / "agent"
SPEC_PATH = AGENT_DIR / "approvals" / "mcp_spawn.py"
STUB_PATH = Path(__file__).resolve().parent / "mcp_stub_server.py"

# 与运行时同一套扁平导入：agent/ 进 sys.path（agent.py 就是这么导入兄弟模块的）
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import mcp_client as mc                                  # noqa: E402
import mcp_station as ms                                 # noqa: E402


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp_spawn = _load_by_path("mcp_spawn", SPEC_PATH)


# ============================================================
# 夹具：临时 station 目录（一个 server = 一个文件夹）+ 假 server
# ============================================================

class Fixture:
    """临时 station 目录 + 日志。退出时全部关掉。

    每个"server"就是一个 `<tmp>/<name>/`：`STATION.md`（手写风 frontmatter）+ `tools.yaml`
    （用 `mcp_station.write_tools_file` 写，与机器集成时同一套格式）。
    环境变量 `AETHER_MCP_STATIONS` 指向这个临时目录 —— 于是传输层用例全部跑在
    station 模式（v2 的唯一路径）上。
    """

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ab_mcp_"))
        self.log_dir = self.dir / "logs"           # 不是 station（没有 STATION.md）
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.images = self.dir / "images"
        self._old_env = {k: os.environ.get(k) for k in
                         ("AETHER_MCP_STATIONS", "AETHER_MCP_IMAGE_TMP", "AB_TEST_TOKEN")}
        os.environ["AETHER_MCP_STATIONS"] = str(self.dir)
        os.environ["AETHER_MCP_IMAGE_TMP"] = str(self.images)
        os.environ["AB_TEST_TOKEN"] = "tok-123"
        self.write_stations()

    # ---- 写 station 文件夹 ----

    def station(self, name, behavior, timeout=10, enabled=True, never_parallel=False,
                env=None, delay=3.0):
        """造一个 station：`STATION.md`（启动参数）+ `tools.yaml`（工具清单）。"""
        meta = {
            "name": name,
            "description": "传输层用例的假 server（--behavior %s）" % behavior,
            "command": sys.executable,
            "args": [str(STUB_PATH).replace("\\", "/"), "--behavior", behavior,
                     "--log", str(self.log_dir / ("%s.jsonl" % name)).replace("\\", "/"),
                     "--delay", str(delay)],
            "enabled": enabled,
            "timeout": timeout,
            "never_parallel": never_parallel,
            "origin": "hand",
        }
        if env:
            meta["env"] = env
        folder = self.dir / name
        folder.mkdir(parents=True, exist_ok=True)
        text = ("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n"
                "# %s\n\n假 station，只给传输层用例用。\n" % name)
        (folder / "STATION.md").write_text(text, encoding="utf-8")
        ms.write_tools_file(name, _tools_payload(), {"protocol_version": "test"},
                            stations=self.dir)
        return folder

    def write_stations(self):
        self.station("stub_normal", "normal")
        self.station("stub_error", "error")
        self.station("stub_slow", "slow", timeout=1, delay=30)
        self.station("stub_hang", "hang", timeout=1)
        self.station("stub_flood", "stderr_flood", timeout=20)
        self.station("stub_stray", "stray")
        self.station("stub_crash", "crash")
        # 握手收到非 JSON 的 server：它只能说"没有合法响应"，客户端只能等到超时，
        # 所以这里把 timeout 调小，免得整个回归套件多等 10 秒
        self.station("stub_garbage", "garbage", timeout=3)
        self.station("stub_badver", "bad_version")
        self.station("stub_pages", "paginate")
        self.station("stub_off", "normal", enabled=False)
        self.station("stub_np", "normal", never_parallel=True)
        self.station("stub_env", "normal", env={"STUB_TOKEN": "${AB_TEST_TOKEN}",
                                               "STUB_MISSING": "${AB_MISSING_VAR}"})

    def log_lines(self, server):
        p = self.log_dir / ("%s.jsonl" % server)
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def close(self):
        try:
            mc.shutdown_all()
        except Exception:
            pass
        with mc._CONNS_LOCK:
            mc._CONNS.clear()
        mc.reset_approvals()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


def _tools_payload():
    """该 station 声明的工具清单（写进 `<station>/tools.yaml`）。"""
    return [
        {"name": "echo", "description": "回显文本",
         "inputSchema": {"type": "object",
                         "properties": {"text": {"type": "string", "description": "文本"}},
                         "required": ["text"]}},
        {"name": "picture", "description": "返回图片", "inputSchema": {"type": "object"}},
        {"name": "fail", "description": "总是报错", "inputSchema": {"type": "object"}},
    ]


_FIX = {"value": None}


def _fx() -> Fixture:
    if _FIX["value"] is None:
        _FIX["value"] = Fixture()
    return _FIX["value"]


def _fresh_env():
    """每个用例开头清干净：连接池、审批状态。"""
    try:
        mc.shutdown_all()
    except Exception:
        pass
    with mc._CONNS_LOCK:
        mc._CONNS.clear()
    mc.reset_approvals()


# ============================================================
# 1. 注册表（station 目录）
# ============================================================

def test_registry_parses_stations_and_env_placeholders():
    fx = _fx()
    reg = mc.load_registry()
    assert reg.servers, "注册表应当解析出 server（夹具写了 13 个 station）"
    assert reg.path == fx.dir, "station 模式下 Registry.path 就是 station 目录"
    s = reg.by_name("stub_env")
    assert s is not None, "应当按名字取到 server"
    assert s.origin == "hand", "origin 来自 STATION.md 的 frontmatter"
    assert s.tools and s.tools_path == fx.dir / "stub_env" / "tools.yaml", \
        "工具 schema 从 station 自己的 tools.yaml 读"
    missing = []
    env = s.resolved_env(missing)
    assert env["STUB_TOKEN"] == "tok-123", "${VAR} 应当从环境变量展开（决策 7）"
    assert env["STUB_MISSING"] == "", "缺失的变量展开为空串"
    assert "AB_MISSING_VAR" in missing, "缺失的变量名必须被记下来（不许静默）"


def test_missing_station_dir_is_not_fatal():
    missing = Path(tempfile.gettempdir()) / ("definitely_not_here_%d" % time.time())
    reg = mc.load_registry(missing)
    assert reg.servers == [], "station 目录不存在时应当是空注册表而不是抛异常"
    assert reg.problems, "目录不存在必须记进 problems（不静默）"


# ============================================================
# 2. 命令行
# ============================================================

def test_windows_cmd_is_wrapped_with_comspec():
    argv = mc._resolve_argv("C:/somewhere/npx.cmd", ["-y", "pkg"])
    if os.name == "nt":
        assert argv[1:3] == ["/c", argv[2]] or argv[1] == "/c", \
            "Windows 上 .cmd/.bat 必须经 cmd /c（CreateProcess 跑不了 .cmd）"
        assert argv[-2:] == ["-y", "pkg"], "参数要原样带上"
    else:
        assert argv[-2:] == ["-y", "pkg"]


# ============================================================
# 3. 审批入口：只有 mcp_call（v2 的唯一形态）
# ============================================================


def test_gateway_entry_is_the_only_mcp_spawn_entry():
    """MCP 只有一个入口：`mcp_call`。闸门必须认领它；只读的 `mcp_search` 不被认领。"""
    assert mcp_spawn.applies({"tool": "mcp_call", "kwargs": {}}) is True, \
        "MCP 的唯一入口 mcp_call 必须被 mcp.spawn 认领（否则启动子进程无人看守）"
    assert mcp_spawn.applies({"tool": "mcp_search", "kwargs": {}}) is False, \
        "mcp_search 只读 station 文件夹、不起进程，不该弹卡"


# ============================================================
# 4. 真进程：握手、调用、分帧、对号入座
# ============================================================

def test_handshake_and_call_roundtrip():
    _fresh_env()
    out = mc.call_tool("stub_normal", "echo", {"text": "你好"})
    assert out == "echo: 你好", "正常往返应当原样返回文本（MCP 的 text 内容直连）"
    info = mc.peek_server_info("stub_normal")
    assert info.get("alive") is True, "调用后 server 进程应当活着（懒启动 + 常驻复用）"
    assert mc.is_approved("stub_normal") is True, \
        "握手成功 = 人已批准过 → 必须标记为已批准（审批规范据此不再问）"
    assert mc.call_tool("stub_normal", "echo", {"text": "again"}) == "echo: again", \
        "第二次调用必须复用同一个进程"


def test_initialized_notification_is_sent():
    fx = _fx()
    _fresh_env()
    mc.call_tool("stub_normal", "echo", {"text": "x"})
    msgs = fx.log_lines("stub_normal")
    methods = [m.get("method") for m in msgs if isinstance(m, dict)]
    assert "initialize" in methods, "必须先 initialize"
    assert "notifications/initialized" in methods, \
        "MCP 规格要求 initialize 响应后客户端发 initialized 通知（漏了会被部分 server 拒绝）"
    assert methods.index("initialize") < methods.index("notifications/initialized"), \
        "initialized 通知必须在 initialize 之后"


def test_tools_list_pagination_is_merged():
    _fresh_env()
    tools = mc.list_tools("stub_pages")
    assert len(tools) == 5, "分页返回的 tools 必须翻页合并（夹具 stub 一共 5 个工具）"


def test_iserror_becomes_biz_fail_mark():
    _fresh_env()
    out = mc.call_tool("stub_error", "echo", {"text": "x"})
    assert out.startswith("❌"), \
        "isError: true 必须映射成 ❌ 前缀 —— 编排器靠它识别业务失败，否则界面会虚报成功"
    out2 = mc.call_tool("stub_error", "fail", {})
    assert out2.startswith("❌")


def test_timeout_kills_the_server_process():
    _fresh_env()
    conn = mc.get_connection("stub_hang")
    t0 = time.monotonic()
    out = mc.call_tool("stub_hang", "echo", {"text": "x"})
    dt = time.monotonic() - t0
    assert out.startswith("❌") and "超时" in out, "挂死的 server 必须返回 ❌ 超时"
    assert dt < 8, "超时必须真的生效（YAML timeout=1），不能干等到天荒地老"
    assert conn.alive() is False, "超时后必须 kill 进程（决策 9：真回收资源）"


def test_stray_response_ids_are_ignored():
    _fresh_env()
    out = mc.call_tool("stub_stray", "echo", {"text": "对号"})
    assert out == "echo: 对号", \
        "对端先发了别人的 id + 通知 + 畸形行，客户端必须忽略并继续等自己的响应（对号入座）"
    conn = mc.get_connection("stub_stray")
    assert conn._stray_ids, "对不上号的响应 id 必须被记下来（串包证据）"


def test_stderr_flood_does_not_block_the_call():
    _fresh_env()
    t0 = time.monotonic()
    out = mc.call_tool("stub_flood", "echo", {"text": "洪水"})
    dt = time.monotonic() - t0
    assert out == "echo: 洪水", "server 往 stderr 灌大量内容时调用仍须成功"
    assert dt < 15, "stderr 必须被持续排空，否则子进程会卡死在写 stderr 上"


def test_bad_handshake_fails_closed():
    _fresh_env()
    out = mc.call_tool("stub_garbage", "echo", {"text": "x"})
    assert out.startswith("❌"), "握手收到非 JSON 必须 fail-closed 返回 ❌（不许抛异常穿透）"
    conn = mc.get_connection("stub_garbage")
    assert conn._unparsable, \
        "收到的畸形行必须被记下来（排障时要知道'对端到底回了什么'，而不是只说一句超时）"
    assert conn.alive() is False, "超时后进程必须被 kill（不许留半死进程）"


def test_crash_during_call_fails_closed():
    _fresh_env()
    out = mc.call_tool("stub_crash", "echo", {"text": "x"})
    assert out.startswith("❌"), "进程在握手后立刻退出，必须返回 ❌ 而不是挂住"
    assert ("进程" in out) or ("退出" in out) or ("输出" in out), \
        "错误信息要说明是进程/输出层的问题（方便排障）"
    conn = mc.get_connection("stub_crash")
    assert conn.alive() is False, (
        "对端 stdout 读到 EOF 后必须**判死**：只看 poll() 会把刚退出（尚未回收）的进程"
        "判成'运行中'，于是既不重启也不回收 —— 曾经就是这样堆出半死子进程的")


class _FakeLogger:
    """假的 SessionLogger：记录 span 与事件，用来验证"日志路径不许影响调用"。"""

    def __init__(self, records=None, span=""):
        self.records = records if records is not None else []
        self.span = span

    def with_span(self, span):
        return _FakeLogger(self.records, span)

    def _rec(self, level, msg, **extra):
        self.records.append({"span": self.span, "level": level, "msg": msg, "extra": extra})

    def info(self, msg, **extra):
        self._rec("info", msg, **extra)

    def warning(self, msg, **extra):
        self._rec("warning", msg, **extra)

    def error(self, msg, **extra):
        self._rec("error", msg, **extra)

    def debug(self, msg, **extra):
        self._rec("debug", msg, **extra)


def test_logger_injection_records_events_without_breaking_calls():
    """决策 10 的具化：函数签名有 logger，事件写进会话日志（span=mcp:<server>），
    且**日志写入路径本身不许把调用搞崩**（extra 键与形参重名的坑真实发生过）。"""
    _fresh_env()
    lg = _FakeLogger()
    out = mc.call_tool("stub_normal", "echo", {"text": "日志"}, logger=lg)
    assert out == "echo: 日志", "带 logger 调用必须与不带时结果一致"
    assert any(r["span"] == "mcp:stub_normal" for r in lg.records), \
        "事件必须带 span=mcp:<server>（否则排障时找不到是哪条链路）"
    assert any("调用完成" in r["msg"] for r in lg.records), "调用完成要有日志"
    assert any(isinstance(r["extra"].get("elapsed"), float) for r in lg.records), \
        "耗时必须以数值形式落进日志（供排障统计）"


def test_bad_protocol_version_is_tolerated():
    _fresh_env()
    out = mc.call_tool("stub_badver", "echo", {"text": "宽松"})
    assert out == "echo: 宽松", \
        "对端回的 protocolVersion 与本地偏好不同时不许失败（宽容协商，只记日志）"


def test_disabled_and_unknown_server_are_refused():
    _fresh_env()
    off = mc.call_tool("stub_off", "echo", {"text": "x"})
    assert off.startswith("❌") and "未启用" in off, "enabled: false 的 server 必须拒绝调用（fail-closed）"
    unknown = mc.call_tool("no_such_server", "echo", {})
    assert unknown.startswith("❌"), "注册表里没有的 server 必须返回 ❌"


# ============================================================
# 5. 结果降维
# ============================================================

def test_image_is_saved_to_configured_dir():
    fx = _fx()
    _fresh_env()
    out = mc.call_tool("stub_normal", "picture", {})
    assert "图片已保存" in out, "MCP 的 image 内容必须落盘并给出路径（模型读不了 base64）"
    path = Path(out.split("：", 1)[-1].rstrip("]"))
    assert path.exists() and path.suffix == ".png", "落盘文件必须真的存在且扩展名跟随 mimeType"
    assert str(fx.images) in str(path) or os.environ.get("AETHER_MCP_IMAGE_TMP") in str(path), \
        "落盘目录必须尊重 AETHER_MCP_IMAGE_TMP（测试才能不写真实工作区）"


def test_resource_and_unknown_content_types():
    text = mc.render_result({"content": [
        {"type": "resource", "resource": {"uri": "s://x/1", "text": "正文"}},
        {"type": "wat", "data": 1},
    ]})
    assert "s://x/1" in text and "正文" in text, "resource 要给出 uri 与内嵌文本"
    assert "wat" in text, "认不出的内容类型要原样附上（不丢信息、不脑补）"


def test_long_result_is_truncated_with_marker():
    text = mc.render_result({"content": [{"type": "text", "text": "z" * (mc.MAX_RESULT_CHARS + 500)}]})
    assert "已截断" in text, "超长结果必须截断并标记（否则会把上下文挤爆）"
    assert len(text) < mc.MAX_RESULT_CHARS + 200


# ============================================================
# 6. 审批规范
# ============================================================

def test_approval_spec_has_five_required_members():
    for member in ("KIND", "TITLE", "RISK", "applies", "finding"):
        assert hasattr(mcp_spawn, member), \
            "审批规范必须提供五个成员（approvals/README.md 的契约），缺了会记进 SPECS_LOAD_ERRORS"
    assert mcp_spawn.KIND == "mcp.spawn" and isinstance(mcp_spawn.RISK, int)


def test_approval_applies_only_to_mcp_tools():
    assert mcp_spawn.applies({"tool": "mcp_call", "kwargs": {}}) is True, \
        "MCP 的唯一入口 mcp_call 必须被认领（否则起子进程时无人看守）"
    assert mcp_spawn.applies({"tool": "read_file"}) is False, \
        "普通工具不该被这类规范碰到（否则会误弹卡）"
    assert mcp_spawn.finding({"tool": "read_file", "kwargs": {}}) is None


def test_first_call_asks_with_real_command_line():
    _fresh_env()
    f = mcp_spawn.finding({"tool": "mcp_call",
                           "kwargs": {"station": "stub_normal", "tool": "echo",
                                      "arguments": {"text": "hi"}}})
    assert f is not None and f.get("quiet") is False, "station 首次被调用时必须申请人工确认"
    assert f.get("targets") == [], "MCP 没有文件目标，不许编一个（README 明写）"
    joined = " ".join(f.get("notes") or [])
    assert "mcp_stub_server" in joined or "命令行" in joined, \
        "审批卡片必须显示将要执行的命令行（主人要看清才批）"
    assert "stub_normal" in f.get("intent", ""), "意图里要写明是哪个 server"


def test_after_spawn_it_is_silent_and_does_not_shadow_other_specs():
    _fresh_env()
    mc.approve("stub_normal")
    ctx = {"tool": "mcp_call", "kwargs": {"station": "stub_normal", "tool": "echo",
                                          "arguments": {}}}
    f = mcp_spawn.finding(ctx)
    assert f is None, (
        "已批准的 station 必须返回 None（不是 quiet）：引擎在第一个命中的规范处 break，"
        "quiet 会遮蔽 outzone.write / secrets_read 的 ask —— 那等于给'用 MCP 写系统盘'开后门")
    # 不遮蔽的正面证据：同一个工具名（mcp_call）带着系统盘路径时，路径提取照样看得见 ——
    # 后面那些路径类规范仍有机会命中；mcp.spawn 是"没意见"，不是"抢先放行"。
    import approval
    deep = {"tool": "mcp_call", "kwargs": {"station": "stub_normal", "tool": "write",
                                           "arguments": {"path":
                                                         "C:/Windows/System32/drivers/etc/hosts"}}}
    assert mcp_spawn.finding(deep) is None, "已批准的 station 不该对路径类规范抢先表态"
    assert approval.extract_paths(deep["kwargs"]), \
        "路径类规范必须仍能从 mcp_call 的参数里扫到路径（否则就是被遮蔽了）"


def test_missing_client_state_fails_closed():
    """拿不到客户端状态（客户端模块不可用）时必须按未批准处理 —— fail-closed。"""
    saved = mcp_spawn._client
    try:
        mcp_spawn._client = lambda: None
        _fresh_env()
        f = mcp_spawn.finding({"tool": "mcp_call",
                               "kwargs": {"station": "stub_normal", "tool": "echo",
                                          "arguments": {}}})
        assert f is not None and f.get("quiet") is False, "状态拿不到时不许放行，要问"
        assert any("取到" in n or "确认" in n for n in (f.get("notes") or [])), \
            "拿不到命令行时要如实说明（不许假装知道）"
    finally:
        mcp_spawn._client = saved


# ============================================================
# 6b. v2：审批闸门认 mcp_call（+ 参数里的路径对路径类规范可见）
# ============================================================

def test_spawn_card_for_v2_mcp_call_shows_command_and_inner_tool():
    """v2 的唯一入口是 `mcp_call`：卡上必须写清"要启动哪个 station、调哪个工具、真实命令行"。"""
    _fresh_env()
    f = mcp_spawn.finding({"tool": "mcp_call",
                           "kwargs": {"station": "stub_normal", "tool": "echo",
                                      "arguments": {"text": "x"}}})
    assert f is not None and f.get("quiet") is False, "首次调用必须弹卡"
    assert "stub_normal" in f["intent"] and "echo" in f["intent"], \
        "意图里要说清 station 与内部工具：%s" % f["intent"]
    assert any("命令行" in n for n in f["notes"]), "卡上要显示那条真实命令行"


def test_spawn_card_silent_after_station_approved():
    _fresh_env()
    mc.approve("stub_normal")
    assert mcp_spawn.finding({"tool": "mcp_call",
                              "kwargs": {"station": "stub_normal", "tool": "echo"}}) is None, \
        "同一 station 批过一次就不再打扰（但返回 None，不遮蔽其它规范）"


def test_mcp_call_arguments_paths_are_visible_to_path_specs():
    """安全增益：MCP 调用的参数（`arguments` 里的路径）必须能被路径提取扫到。

    v2 把 MCP 调用收窄到一个工具名（`mcp_call`），所以"用 MCP 去写系统盘"这类事
    必须仍然被 `outzone.*` / `secrets_read` 那类规范拦住 —— 靠的就是这一层提取。
    """
    import approval

    paths = approval.extract_paths({
        "station": "fs", "tool": "write",
        "arguments": {"path": "C:/Windows/System32/drivers/etc/hosts", "content": "x"},
    })
    assert paths, "arguments 里的路径必须被扫出来（否则 MCP 就是审批引擎的盲区）"
    assert any("hosts" in p or "System32" in p for p in paths), \
        "扫出来的应当是那条路径本身：%s" % paths


def test_mcp_search_needs_no_approval():
    assert mcp_spawn.finding({"tool": "mcp_search", "kwargs": {"station": "stub_normal"}}) is None, \
        "mcp_search 只读 station 文件夹、不起进程，不该弹卡"


# ============================================================
# 7. 与真实运行环境的契约（子进程验证，避免污染 pytest 的 stdout）
# ============================================================

def test_agent_tools_import_registers_mcp_tools():
    """真环境契约（v2）：`import agent_tools` 之后 —— **没有** MCP 工具进常驻工具表，
    只有两个元工具（`mcp_search` / `mcp_call`）+ 运维工具 `mcp_manage`。

    这是 v2 的核心承诺（docs/MCP设计.md v2 §五）：再大的 station（这里是 112 个工具）
    也不往工具表里塞东西 —— 那些工具的 schema 只在模型调 `mcp_search` 时才出现。
    放在子进程里做：验的就是"在一个干净解释器里 import 一遍"的结果。
    """
    import tempfile
    # 注意：`_fx()` 不只是"造夹具"——它还把 AETHER_MCP_STATIONS 指向共享夹具（Fixture.__init__
    # 里设的），本文件相当多用例靠它。直跑 harness 按**名字排序**，本用例排在前面，
    # 少了这一句后面那些用例就读不到 station、报"没有叫 stub_normal 的 station"。
    _fx()
    tmp = Path(tempfile.mkdtemp(prefix="mcp_v2_"))
    folder = tmp / "big"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "STATION.md").write_text(
        "---\nname: big\ndescription: 112 个工具的假 station\ncommand: python\n"
        "args: ['-c', 'pass']\nenabled: true\norigin: hand\n---\n\n# big\n\n契约用例用。\n",
        encoding="utf-8")
    tools = [{"name": "t%03d" % i, "description": "工具 %d" % i,
              "inputSchema": {"type": "object",
                              "properties": {"p": {"type": "string", "description": "x" * 200}},
                              "required": ["p"]}}
             for i in range(112)]
    (folder / "tools.yaml").write_text(
        yaml.safe_dump({"station": "big", "tools": tools}, allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    code = (
        "import sys, json;"
        "sys.path.insert(0, r'%s');"
        "import agent_tools as A;"
        "print(json.dumps({'mcp': sorted(n for n in A.AVAILABLE_TOOLS if n.startswith('mcp')),"
        " 'reg': {k: v for k, v in A.MCP_REGISTRATION.items() if k != 'problems'},"
        " 'big_in_table': [n for n in A.AVAILABLE_TOOLS if n.startswith('t0')]}))"
    ) % str(PROJECT_ROOT)
    env = dict(os.environ)
    env["AETHER_MCP_STATIONS"] = str(tmp)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(PROJECT_ROOT), env=env, timeout=180,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "import agent_tools 必须成功（MCP 自检不许弄崩 import）：%s" % proc.stderr[-800:]
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert set(data["mcp"]) == {"mcp_search", "mcp_call", "mcp_manage"}, \
        ("工具表里 MCP 相关只该有这三个（两个元工具 + 运维）—— 任何 server 的工具"
         "一个都不许进常驻工具表：%s" % data["mcp"])
    assert data["reg"].get("mode") == "station" and data["reg"].get("servers") == 1, \
        "自检报告要如实说清 station 模式与数量：%s" % data["reg"]
    assert data["big_in_table"] == [], \
        "112 个工具必须有**零个**进工具表（否则每轮请求白吃几万 token）"


def test_end_to_end_through_task_orchestrator():
    """真端到端（无 LLM）：真 `AVAILABLE_TOOLS` + 真 `TaskOrchestrator` + 假 server。

    子进程跑 `tests/mcp_e2e_probe.py`（它会打印 JSON 摘要），断言四件事：
    注册时机、执行链、**logger 自动注入**、`isError` → ❌ → 编排器计为失败。
    """
    import json as _json
    import subprocess as _sp
    probe = Path(__file__).resolve().parent / "mcp_e2e_probe.py"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = _sp.run([sys.executable, "-X", "utf8", str(probe)], capture_output=True,
                   text=True, cwd=str(PROJECT_ROOT), env=env, timeout=300,
                   encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "端到端探针必须全过：%s\n%s" % (proc.stdout[-1500:], proc.stderr[-800:])
    data = _json.loads(proc.stdout.strip())
    assert data["ok"] is True, "探针自报失败：%s" % data.get("problems")
    steps = {s["step"]: s for s in data["steps"]}
    assert steps["v2：MCP 工具不再进常驻工具表"]["ok"], \
        "v2 核心承诺：MCP 工具不许再进常驻工具表（上百个工具全量注入 = 每轮白烧 token）"
    assert steps["mcp_search 现场取到工具与 schema"]["ok"], \
        "mcp_search 必须能实时从 station 文件夹取到工具与完整 inputSchema"
    assert steps["编排器按签名注入 logger（span=mcp:*）"]["ok"], \
        "编排器必须按签名的 logger 形参注入会话日志（决策 10）"
    assert steps["isError 走 ❌ 前缀（biz_fail）"]["ok"], \
        "MCP 错误必须映射成 ❌ 前缀，否则编排器会把失败记成成功（界面虚报）"
    assert steps["缺必填参数在本地就被拒（不白起进程）"]["ok"], \
        "缺必填参数要在本地拒（省一趟进程启动），且提示里带 schema"


# ============================================================
# 独立直跑汇总（不依赖 pytest）
# ============================================================

def _run_all() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print("PASS  %s" % name)
        except Exception as e:
            failed.append((name, e))
            print("FAIL  %s → %s: %s" % (name, type(e).__name__, e))
    print("-" * 60)
    print("通过 %d / %d" % (passed, len(tests)))
    for name, e in failed:
        print("  ❌ %s → %s" % (name, e))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(_run_all())
    finally:
        if _FIX["value"] is not None:
            _FIX["value"].close()
