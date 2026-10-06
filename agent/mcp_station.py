# -*- coding: utf-8 -*-
"""mcp_station —— MCP **服务站（station）** 层：一个 server = 一个文件夹。

设计：`docs/MCP设计.md` v2 §三/§四。这一层是 MCP 系统的**发现层**，只做四件事：

  1. 扫 `agent_MCP/<station>/`（**文件夹存在即注册**，与技能系统同构的"渐进式披露"）。
  2. 读 `STATION.md` 的 frontmatter —— 手写的启动命令 / 参数 / env / 开关 / 超时。
  3. 读 `tools.yaml` —— 机器生成的该 station **全部工具的完整 schema**（由 `mcp_sync` 写）。
  4. 渲染 + 同步 `MCP_REGISTRY.md`（每个 station **一行**，供系统提示词**冻结注入**）。

它刻意**不做**的事（都有理由，别顺手加）：
  · **不启动任何进程** —— 那是 `mcp_client` 的事；本层只读盘（可在 import 期安全调用）。
  · **不碰 `skill_system.py`** —— 技能系统是已验证资产，本层只借鉴它的形状（决策 Q9-B）。
  · **不决定"给模型看多少 schema"** —— 那是 `mcp_gateway.mcp_search` 的事（口径 3 的分页在那里）。

关键设计点：**注册表只注入"有什么"（一行/station），工具 schema 按需现取** ——
于是注入体积只与 station 数线性、与工具数无关（本次重构的核心 KPI）。
冻结注入 ≠ 不能热：`load_station()` 每次读盘，所以同一会话内新建的 station 立刻能被找到。
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATIONS_DIR = "agent_MCP"
STATION_DOC = "STATION.md"          # 手写：frontmatter + 正文说明
TOOLS_FILE = "tools.yaml"           # 机器生成：该 station 全部工具的完整 schema
REGISTRY_NAME = "MCP_REGISTRY.md"   # 机器生成：每 station 一行，注入系统提示词
STATIONS_ENV = "AETHER_MCP_STATIONS"
STATE_DIR = ".state"                # 失败计数等运行时状态（不提交）
TRASH_DIR = ".trash"                # 移除的 station 挪这里（可恢复，不真删）

# 扫描时要跳过的名字（不是 station）：注册表自己、运维脚本、运行时痕迹
_SKIP_DIRS = {".state", ".trash", "__pycache__", "tools", ".git"}

_INJECT_GUIDE = (
    "下面是**已注册的 MCP 服务站**（一个 station = 一个 MCP server）。\n"
    "注册表**只说明有什么、不含任何工具的参数 schema** —— 这是刻意的：有的 server 有上百个工具，\n"
    "全量注入会白吃大量上下文。要用某个 station 的工具时，两步走：\n"
    "  1) `mcp_search(station=\"<station 名>\")` —— 取它的工具清单与完整参数 schema；\n"
    "  2) `mcp_call(station=\"<station 名>\", tool=\"<工具名>\", arguments={...})` —— 调用。\n"
    "**不要凭记忆猜参数名**：先 search 再 call。\n"
    "**状态列**：`on` = 开着能用；`off` = 关着（**得先开**，现在调不动）；`⚠ 不可用` = 开着但没配好。"
)


# ============================================================
# 1. 路径
# ============================================================

def _config_paths() -> Dict[str, Any]:
    cfg = PROJECT_ROOT / "config.yaml"
    if not cfg.exists():
        return {}
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def stations_dir(path: Optional[Path] = None) -> Path:
    """station 目录：参数 > 环境变量 AETHER_MCP_STATIONS > config.yaml paths.mcp_stations > 项目内默认。

    不硬编码盘符（GitHub 友好）：默认值由本文件位置推出。
    注册表（`MCP_REGISTRY.md`）永远由这个目录推出，见 `registry_path()` —— 它是**派生值**，
    config 里不再单独配（曾有 `paths.mcp_registry` 这个旧写法，已随 v1 一起删）。
    """
    if path is not None:
        return Path(path)
    env = (os.environ.get(STATIONS_ENV) or "").strip()
    if env:
        return Path(env)
    paths = (_config_paths().get("paths") or {})
    rel = str((paths.get("mcp_stations") or "")).strip()
    return (PROJECT_ROOT / rel) if rel else (PROJECT_ROOT / DEFAULT_STATIONS_DIR)


def registry_path(stations: Optional[Path] = None) -> Path:
    """注册表文件：**永远在 station 目录里**（`<stations>/MCP_REGISTRY.md`）。

    曾经它优先读 config 的 `paths.mcp_registry` —— 于是把 station 目录指到临时目录
    （测试、多套配置）时，注册表还是会写进**真实仓库**：跑一次测试就污染了工作区。
    现在只有一条规则：注册表跟着 station 目录走（config 只决定默认目录是哪个）。
    """
    return stations_dir(stations) / REGISTRY_NAME


def mcp_enabled() -> bool:
    """全局总闸（config.yaml 的 mcp.enabled）：false = 连元工具与注册表都不注入。"""
    val = (_config_paths().get("mcp") or {}).get("enabled", True)
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "0", "no", "off")
    return bool(val)


# ============================================================
# 2. 数据结构
# ============================================================

@dataclass
class StationInfo:
    """一个服务站（文件夹）的解析结果。"""
    name: str
    path: Path
    description: str = ""
    enabled: bool = True
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout: int = 30
    never_parallel: bool = False
    origin: str = "hand"                 # hand = 主人手写；auto = AB 自己集成
    body: str = ""                       # STATION.md 正文（给人/模型看的说明）
    tools: List[Dict[str, Any]] = field(default_factory=list)
    tools_path: Optional[Path] = None
    problems: List[str] = field(default_factory=list)

    @property
    def tools_count(self) -> int:
        return len(self.tools)

    @property
    def usable(self) -> bool:
        """能不能真的连（缺 command 或有问题就只能看不能用）。"""
        return bool(self.command) and not self.problems

@dataclass
class SyncResult:
    """注册表同步结果（照 skill_system.SyncResult 的形状）。"""
    changed: bool = False
    created: bool = False
    rows_added: List[str] = field(default_factory=list)
    rows_removed: List[str] = field(default_factory=list)
    rows_changed: List[str] = field(default_factory=list)
    stations: int = 0            # 注册表里的总行数（含关着的）
    on_count: int = 0            # 其中真正开着的
    issues: List[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "%d 个 station" % self.stations
        if self.on_count != self.stations:
            head += "（%d 个开着）" % self.on_count
        if self.created:
            return "新建注册表：%s" % head
        if not self.changed:
            return "注册表无变化：%s" % head
        bits = []
        if self.rows_added:
            bits.append("新增 %s" % ", ".join(self.rows_added))
        if self.rows_removed:
            bits.append("移除 %s" % ", ".join(self.rows_removed))
        if self.rows_changed:
            bits.append("变更 %s" % ", ".join(self.rows_changed))
        return "注册表已更新（%s）：%s" % (head, "；".join(bits))


# ============================================================
# 3. frontmatter 解析（借鉴 skill_system 的形状，但自成一体、不依赖它）
# ============================================================

def _split_frontmatter(text: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """把 markdown 切成 (frontmatter dict, 正文)；没有 frontmatter 返回 None。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return None
    raw = "\n".join(lines[1:end])
    try:
        meta = yaml.safe_load(raw) or {}
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    return meta, "\n".join(lines[end + 1:]).strip()


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


def _as_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_env(value: Any) -> Dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def read_tools(path: Path) -> List[Dict[str, Any]]:
    """读 station 的 tools.yaml（机器生成）。缺文件 = 空列表（不是错误）。

    容错口径：**凡是能取到名字的条目都留下**（坏 schema 用空对象兜底），只有完全没名字的丢掉。
    坏条目宁可带着空 schema 露出来，也不要静默消失 —— 否则"工具数对不上"这种问题查不到。
    """
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    tools = data.get("tools") if isinstance(data, dict) else data
    out: List[Dict[str, Any]] = []
    for t in (tools or []):
        if isinstance(t, dict) and not str(t.get("name") or "").strip():
            continue                      # 空字典/无名条目才算垃圾
        entry = _tool_entry(t)
        if str(entry.get("name") or "").strip():
            out.append(entry)
    return out


TOOLS_HEADER = """# 本文件由 agent_MCP/mcp_sync.py 生成 —— **机器所有，不要手改**（下次同步会覆盖）。
# station 怎么启动、开不开、超时多少：见同目录的 STATION.md（那是手写的）。
# 重新生成：venv/Scripts/python agent_MCP/mcp_sync.py --sync %s
"""


def _tool_entry(t: Any) -> Dict[str, Any]:
    """规范化一个工具条目（原始 inputSchema 原样留，转换交给 mcp_search）。"""
    if not isinstance(t, dict):
        return {"name": str(t), "description": "", "inputSchema": {"type": "object", "properties": {}}}
    schema = t.get("inputSchema") or t.get("input_schema")
    return {
        "name": str(t.get("name") or ""),
        "description": str(t.get("description") or ""),
        "inputSchema": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
    }


def write_tools_file(name: str, tools: List[Dict[str, Any]],
                     meta: Optional[Dict[str, Any]] = None,
                     stations: Optional[Path] = None) -> Path:
    """把一个 server 的**全部工具 schema** 写进它自己的文件夹（`<station>/tools.yaml`）。

    为什么一个 station 一个文件（而不是每个工具一个）：读一个 station 只需读它自己这一份，
    已经满足"按需索取"；拆成上百个小文件只换来目录噪音（口径 1）。
    """
    folder = stations_dir(stations) / str(name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / TOOLS_FILE
    payload: Dict[str, Any] = {
        "station": str(name),
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "protocol_version": str((meta or {}).get("protocol_version") or ""),
        "tools": [_tool_entry(t) for t in (tools or [])],
    }
    info = (meta or {}).get("server_info")
    if info:
        payload["server_info"] = info
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100)
    path.write_text((TOOLS_HEADER % name) + body, encoding="utf-8")
    return path


# ============================================================
# 4. 扫描
# ============================================================

def _parse_station(folder: Path) -> Tuple[Optional[StationInfo], List[str]]:
    """解析一个 station 文件夹 → (StationInfo | None, issues)。"""
    issues: List[str] = []
    doc = folder / STATION_DOC
    if not doc.exists():
        # 有 tools.yaml 却没说明文档 = 半成品，要报出来（不静默忽略）
        if (folder / TOOLS_FILE).exists():
            issues.append("%s/ 有 %s 但没有 %s —— 文件夹不算注册（补上说明文档）"
                          % (folder.name, TOOLS_FILE, STATION_DOC))
        return None, issues

    try:
        text = doc.read_text(encoding="utf-8")
    except Exception as e:
        issues.append("%s/%s 读不了：%s: %s" % (folder.name, STATION_DOC, type(e).__name__, e))
        return None, issues

    parsed = _split_frontmatter(text)
    if parsed is None:
        issues.append("%s/%s 缺 frontmatter（开头要有 --- 包起来的元数据）" % (folder.name, STATION_DOC))
        return None, issues
    meta, body = parsed

    name = str((meta.get("name") or "").strip() or folder.name)
    if str(meta.get("name") or "").strip() and str(meta.get("name")).strip() != folder.name:
        issues.append("station `%s`：frontmatter 的 name 与文件夹名 `%s` 不一致 —— "
                      "以 frontmatter 为准（要改名请两处一起改，否则改名静默无效）"
                      % (str(meta.get("name")).strip(), folder.name))
    if not str(meta.get("name") or "").strip():
        issues.append("%s：frontmatter 缺 name，已按文件夹名 %s 处理" % (folder.name, folder.name))

    tools_path = folder / TOOLS_FILE
    st = StationInfo(
        name=name,
        path=folder,
        description=str(meta.get("description") or "").strip(),
        enabled=_as_bool(meta.get("enabled", True), True),
        command=str(meta.get("command") or "").strip(),
        args=_as_list(meta.get("args")),
        env=_as_env(meta.get("env")),
        timeout=_as_int(meta.get("timeout"), 30),
        never_parallel=_as_bool(meta.get("never_parallel", False), False),
        origin=str(meta.get("origin") or "hand").strip().lower(),
        body=body,
        tools=read_tools(tools_path) if tools_path.exists() else [],
        tools_path=tools_path if tools_path.exists() else None,
    )
    if not st.command:
        st.problems.append("frontmatter 缺 command（这个 station 只能看、不能连）")
    if not st.tools:
        st.problems.append("没有 %s（用 `mcp_sync --sync %s` 生成工具清单）" % (TOOLS_FILE, st.name))
    if st.origin not in ("hand", "auto"):
        issues.append("%s：origin 只能是 hand/auto（拿到 %r），按 hand 处理" % (name, st.origin))
        st.origin = "hand"
    return st, issues


def scan_stations(stations: Optional[Path] = None) -> Tuple[List[StationInfo], List[str]]:
    """扫 station 目录 → (station 列表（按名字排序）, issues)。

    **文件夹存在即注册**；不合规的文件夹把问题记进 issues（不静默丢）。
    重名（frontmatter name 撞了）保留第一个并记 issue。
    """
    root = stations_dir(stations)
    issues: List[str] = []
    if not root.is_dir():
        return [], ["station 目录不存在：%s" % root]

    found: List[StationInfo] = []
    seen: Dict[str, str] = {}
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name in _SKIP_DIRS or child.name.startswith("."):
            continue
        st, sub_issues = _parse_station(child)
        issues += sub_issues
        if st is None:
            continue
        if st.name in seen:
            issues.append("station 名重复：%s（%s 与 %s，后者已跳过）"
                          % (st.name, seen[st.name], child.name))
            continue
        seen[st.name] = child.name
        if st.problems:
            for p in st.problems:
                issues.append("%s：%s" % (st.name, p))
        found.append(st)
    return found, issues


def load_station(name: str, stations: Optional[Path] = None) -> Optional[StationInfo]:
    """按名字实时读一个 station（**每次读盘** → 同一会话内新建的 station 立刻可见）。"""
    for st in scan_stations(stations)[0]:
        if st.name == str(name):
            return st
    return None


def visible_stations(stations: Optional[Path] = None) -> List[StationInfo]:
    """能进注册表 / 能被调用的 station：enabled 且没问题（fail-closed）。"""
    return [st for st in scan_stations(stations)[0] if st.enabled and st.usable]


# ============================================================
# 5. 注册表（渲染 + 同步 + 注入块）
# ============================================================

def _status_of(st: "StationInfo") -> str:
    """注册表「状态」列的三档取值。

    `on`   = 开着且能用；
    `off`  = 关着（站点的开关，在它自己文件夹的 STATION.md 里）；
    `⚠ 不可用` = 开着但没配好（缺 command / frontmatter 解析有问题）——
    标 `on` 是骗人（真调不通），标 `off` 也不对（开关确实是开着的）。
    """
    if not st.enabled:
        return "off"
    if not st.usable:
        return "⚠ 不可用"
    return "on"


def render_rows(stations: List[StationInfo]) -> str:
    """只渲染数据行（不含标题/表头），用于内容比较。**每 station 恰好一行**。

    `stations` 传**全部** station（含关着的）：关掉的留在表里标 `off`，
    "装着但关着"是可见信息（模型能主动问"要开吗"），而不是整行消失。
    """
    lines = []
    for st in stations:
        usage = (st.description or "（未写用途）").replace("|", "／").replace("\n", " ")[:60]
        lines.append("| %s | %s | %d | %s |" % (st.name, usage, st.tools_count,
                                                _status_of(st)))
    return "\n".join(lines)


def render_registry(stations: List[StationInfo], timestamp: Optional[str] = None) -> str:
    """渲染注册表全文。**体积只与 station 数线性、与工具数无关**（核心 KPI）。

    刻意**不写时间戳**：机器生成的文件一旦带时间，每次同步内容都不同 → git 脏 diff、
    "变了没变"判断全废。快照时间放在注入块那边（那是每次会话临时拼的，不进盘）。
    """
    head = [
        "# MCP_REGISTRY.md（MCP 服务站注册表）",
        "",
        "> 本文件由 `agent/mcp_station.py` **自动生成**（机器所有，别手改；改 station 文件夹即可）。",
        "> 一个 station = 一个 MCP server = `agent_MCP/<station>/` 一个文件夹，**文件夹存在即注册**。",
        "> **开关就在该文件夹里**：`STATION.md` 的 `enabled:`（true/false）。关掉的站点仍留在下表，状态标 `off`。",
        "> 注册表只说明「有什么」，**不含工具参数 schema** —— 要用时先 `mcp_search` 取，再 `mcp_call` 调。",
        "",
        "| station | 用途 | 工具数 | 状态 |",
        "|---|---|---|---|",
    ]
    rows = render_rows(stations)
    tail = ["", "（共 %d 个 station）" % len(stations)]
    return "\n".join(head + ([rows] if rows else ["| （暂无） | | | |"]) + tail) + "\n"


def _extract_body(text: str) -> str:
    """从已有注册表文本里取出**数据行**（用于比较是否变化）。

    表头不能靠"含 station 这个词"来认 —— station 的用途里出现 "station" 太正常了
    （曾经因此把合法数据行当表头丢掉，于是每次同步都误报"有变化"、每次 --check 都红）。
    改成**按结构**认：剥掉分隔行后，如果第一格的文本正好是 `station`，那行才是表头。
    """
    lines = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s.startswith("|") or s.startswith("|---"):
            continue
        lines.append(s)
    if lines:
        first = [c.strip() for c in lines[0].strip("|").split("|")]
        if first and first[0] == "station":
            lines = lines[1:]
    return "\n".join(lines)


def _rows_equal(old_text: str, new_rows: str) -> bool:
    """注册表正文是否等价（空表占位行 `| （暂无） | | | |` 与"没有 station"等价）。"""
    old = _extract_body(old_text).strip()
    if old.startswith("| （暂无）"):
        old = ""
    return old == (new_rows or "").strip()


def sync_registry(stations: Optional[Path] = None,
                  path: Optional[Path] = None) -> SyncResult:
    """把 station 目录现状写进注册表（内容没变就不动文件，省得脏 diff）。"""
    found, issues = scan_stations(stations)
    # 注册表 = **所有 station 文件夹**（开着 + 关着）：关着的标 `off` 留在表里。
    # 「能不能调」是另一回事，由 visible_stations() / mcp_call 把关（关着的拒绝调用）。
    visible = [st for st in found if st.enabled and st.usable]
    res = SyncResult(stations=len(found), on_count=len(visible), issues=list(issues))
    target = Path(path) if path else registry_path(stations)
    new_rows = render_rows(found)
    if not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_registry(found), encoding="utf-8")
            res.created = True
            res.changed = True
        except Exception as e:
            res.issues.append("注册表写入失败：%s: %s" % (type(e).__name__, e))
        return res

    old_text = target.read_text(encoding="utf-8")
    if _rows_equal(old_text, new_rows):
        return res
    old_rows = _extract_body(old_text)
    old_map = {l.split("|")[1].strip(): l for l in old_rows.split("\n") if l.count("|") >= 4}
    new_map = {l.split("|")[1].strip(): l for l in new_rows.split("\n") if l.count("|") >= 4}
    res.rows_added = sorted(set(new_map) - set(old_map))
    res.rows_removed = sorted(set(old_map) - set(new_map))
    res.rows_changed = sorted(k for k in set(old_map) & set(new_map) if old_map[k] != new_map[k])
    try:
        target.write_text(render_registry(found), encoding="utf-8")
        res.changed = True
    except Exception as e:
        res.issues.append("注册表写入失败：%s: %s" % (type(e).__name__, e))
    return res


def build_injection_block(stations: Optional[Path] = None,
                          path: Optional[Path] = None) -> str:
    """构建注入系统提示词的块：引导说明 + 注册表全文（会话快照）。

    **没有可见 station 就返回空串**：目录不存在、或全都关着 —— 这时候给模型讲
    "怎么用 mcp_search/mcp_call" 是纯噪音（而且那两个工具此时也没意义）。
    注册表文件不存在时先同步生成一次；同步失败也返回空串（宁可少注入，不注入半截）。
    """
    target = Path(path) if path else registry_path(stations)
    # 只要有 station 文件夹就注入（哪怕**全关着**）：关掉的站点也留在注册表里标 `off`，
    # "装着但关着"是有用信息（模型可以主动问要不要开），不该凭空消失。
    if not scan_stations(stations)[0]:
        return ""
    if not target.is_file():
        sync_registry(stations, target)
    if not target.is_file():
        return ""
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    return ("## MCP_REGISTRY.md（MCP 服务站注册表）（会话快照 · %s · 自动注入）\n\n"
            % time.strftime("%Y-%m-%d %H:%M")
            + _INJECT_GUIDE + "\n\n" + text)


# ============================================================
# 6. 给 mcp_client 的运行期 spec（形状与 v1 一致，消费方无需改动）
# ============================================================

def _to_server_spec(st: StationInfo):
    """StationInfo → mcp_client.ServerSpec（延迟导入 mcp_client，避免环形依赖）。"""
    import mcp_client as mc                 # noqa: PLC0415
    return mc.ServerSpec(
        name=st.name,
        command=st.command,
        args=list(st.args),
        env=dict(st.env),
        timeout=st.timeout,
        never_parallel=st.never_parallel,
        enabled=st.enabled,
        description=st.description,
        tools=[mc.ToolSpec(name=str(t.get("name") or ""),
                           description=str(t.get("description") or ""),
                           input_schema=(t.get("inputSchema") or t.get("input_schema") or {}))
               if isinstance(t, dict) else mc.ToolSpec(name=str(t))
               for t in st.tools],
        tools_path=st.tools_path,
        problems=list(st.problems),
        origin=st.origin,
    )


def load_registry(stations: Optional[Path] = None, with_tools: bool = True):
    """给 `mcp_client.load_registry()` 用的 station 模式加载器 → mcp_client.Registry。

    与 v1 的 Registry 同形（含 by_name / origin_of / enabled），所以下游（审批规范、
    工具映射、自维护工具）不用改。origin 来自 STATION.md 的 frontmatter（hand/auto）。
    """
    import mcp_client as mc                 # noqa: PLC0415
    root = stations_dir(stations)
    found, issues = scan_stations(root)
    reg = mc.Registry(path=root)
    reg.problems = list(issues)
    for st in found:
        spec = _to_server_spec(st)
        if not with_tools:
            spec.tools = []
        reg.servers.append(spec)
    return reg


# ============================================================
# 7. 机器写入（只动 `origin: auto` 的 station；手写文件一个字不改）
# ============================================================
# 纪律（v2 §三）：
#   · 手写文件（主人建的 station）**机器永不改写** —— PyYAML 会抹掉注释与排版；
#   · 机器只写自己建的（`origin: auto`）：建文件夹、写 STATION.md、开关、移除；
#   · 移除走 `.trash/`（可恢复胜过彻底消失，见工作区红线）；
#   · 开关只改 `enabled:` **那一行**（不整文件重写，保住别的行）。

def station_doc(name: str, stations: Optional[Path] = None) -> Path:
    """station 的 `STATION.md` 路径（不管存不存在）。"""
    return stations_dir(stations) / str(name) / STATION_DOC


def trash_dir(stations: Optional[Path] = None) -> Path:
    """回收站：`agent_MCP/.trash/`（跟随 station 目录，不跟随别处的注册表）。"""
    return stations_dir(stations) / TRASH_DIR


def state_dir(stations: Optional[Path] = None) -> Path:
    """运行时状态目录：`agent_MCP/.state/`（失败计数等；已 gitignore）。"""
    return stations_dir(stations) / STATE_DIR


def backup_file(path: Path, tag: str = "bak", stations: Optional[Path] = None) -> Optional[Path]:
    """把文件复制进回收站（带时间戳）。失败返回 None（调用方要当成"没有备份"）。"""
    try:
        src = Path(path)
        if not src.exists():
            return None
        dest_dir = trash_dir(stations)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ("%s.%s.%s" % (src.name, time.strftime("%Y%m%d-%H%M%S"), tag))
        shutil.copy2(str(src), str(dest))
        return dest
    except Exception:
        return None


def station_exists(name: str, stations: Optional[Path] = None) -> bool:
    """文件夹里有没有 STATION.md（**文件夹存在即注册**的判据）。"""
    return station_doc(name, stations).exists()


def write_station(name: str, meta: Dict[str, Any], body: str = "",
                  stations: Optional[Path] = None) -> Path:
    """写一个 station 的 `STATION.md`（**给机器集成用**：frontmatter + 正文）。

    调用方必须先确认这不是主人的手写 station（撞名检查）——这里不做保护，
    因为"写到一半的半成品"正是集成失败回滚要的东西。
    """
    folder = stations_dir(stations) / str(name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / STATION_DOC
    fm = {"name": str(name)}
    for key in ("description", "command", "args", "env", "enabled",
                "timeout", "never_parallel", "origin", "source"):
        if key in meta and meta[key] is not None:
            fm[key] = meta[key]
    fm.setdefault("enabled", True)
    fm.setdefault("origin", "auto")
    text = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=100)
    text += "---\n\n" + (body or ("# %s\n\n这个 station 由我（AB）集成，`origin: auto`。\n"
                                  "要改参数：先 `mcp_manage(action=\"remove\")` 再 add。\n" % name))
    path.write_text(text, encoding="utf-8")
    return path


def set_station_enabled(name: str, enabled: bool,
                        stations: Optional[Path] = None) -> Tuple[bool, str]:
    """改 station 的开关（**只改 `enabled:` 那一行**）。返回 (做到没有, 说明)。"""
    st = load_station(name, stations)
    if st is None:
        return False, "没有叫 %s 的 station" % name
    if st.origin != "auto":
        return False, ("station `%s` 是主人手写的（`origin: hand`）—— 我不改写手写文件。"
                       "请直接改 `%s` 里的 `enabled:`，然后跑 `python agent/mcp_station.py`。"
                       % (name, st.path / STATION_DOC))
    path = st.path / STATION_DOC
    try:
        text = path.read_text(encoding="utf-8")
        new_text, n = re.subn(r"^enabled:[ \t]*.*$",
                              "enabled: %s" % ("true" if enabled else "false"),
                              text, count=1, flags=re.M)
        if n == 0:                                  # frontmatter 里没有这一行 → 补一行
            if not text.startswith("---"):
                return False, "STATION.md 没有 frontmatter，我不猜着写（请先手工修好）"
            new_text = text.replace("---", "---\nenabled: %s" % ("true" if enabled else "false"), 1)
        path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return False, "写 %s 失败：%s: %s" % (path, type(e).__name__, e)
    return True, str(path)


def remove_station(name: str, stations: Optional[Path] = None) -> Tuple[bool, str]:
    """移除 station：整个文件夹移到 `<stations>/.trash/<时间戳>-<name>/`（可恢复）。"""
    st = load_station(name, stations)
    if st is None:
        return False, "没有叫 %s 的 station" % name
    if st.origin != "auto":
        return False, ("station `%s` 是主人手写的（`origin: hand`）—— 我不删主人手写的东西。"
                       "要删请自己动手（文件夹：%s）。" % (name, st.path))
    root = stations_dir(stations)
    dest = root / TRASH_DIR / ("%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), name))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(st.path), str(dest))
    except Exception as e:
        return False, "移动失败：%s: %s" % (type(e).__name__, e)
    return True, str(dest)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI：`python agent/mcp_station.py [--check]` —— 同步/查看注册表。"""
    import argparse
    ap = argparse.ArgumentParser(description="MCP 注册表同步（station 模式）")
    ap.add_argument("--check", action="store_true", help="只报告差异，不写文件")
    a = ap.parse_args(argv)

    root = stations_dir()
    found, issues = scan_stations(root)
    print("station 目录：%s" % root)
    print("注册表：%s" % registry_path(root))
    for st in found:
        print("  · %-14s %-4s 工具 %-4d %s" % (st.name, "on" if st.enabled else "off",
                                              st.tools_count, st.description[:40]))
    for i in issues:
        print("  ⚠️  %s" % i)
    if a.check:
        rows_now = render_rows([s for s in found if s.enabled and s.usable])
        target = registry_path(root)
        old_text = target.read_text(encoding="utf-8") if target.exists() else ""
        if _rows_equal(old_text, rows_now):
            print("✅ 注册表与目录一致")
            return 0
        print("⚠️ 注册表与目录不一致（跑一次不带 --check 即可同步）")
        return 1
    res = sync_registry(root)
    print(res.summary())
    return 0 if not res.issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
