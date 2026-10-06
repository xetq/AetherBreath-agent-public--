"""
AetherBreath Agent - 带对话连续性的生产级入口
支持多轮工具调用、错误恢复、日志记录、会话持久化
所有配置均从 config.yaml 和 .env 加载
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional, Callable
from pathlib import Path
import yaml
import hashlib

# ===== 控制台编码兜底 =====
# Windows 控制台默认是 GBK；启动路径里的 emoji（ℹ️ / 📄 / 🤖 …）在 GBK 下会抛
# UnicodeEncodeError，表现成「什么都没发生就退出」—— 新用户 clone 后还没建 .env 时
# 必然踩到（那条提示带 ℹ️）。这里只放宽错误处理、不改编码：编不出的字符降级成 '?'，
# 既不崩，也不会把 UTF-8 塞进 GBK 控制台变成乱码。
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# ===== 把项目根目录加入 sys.path，以便导入根目录下的模块 =====
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ===== 导入依赖 =====
from openai import OpenAI
from dotenv import load_dotenv
from task_orchestrator import ToolCall, TaskOrchestrator
import task_orchestrator          # 同名模块：注册"当前常驻编排器"给工具侧要用到它（见 _get_orchestrator）

# 日志模块（同目录直接导入）
from logger import SessionLogger

# 技能系统（同目录直接导入：技能库扫描 / 注册表同步 / 上下文注入块）
from skill_system import sync_registry, build_injection_block

# MCP 服务站（同目录直接导入：注册表同步 / 注入块 / 总闸）
# v2：MCP 只注入**注册表**（每 station 一行），工具 schema 由模型的 mcp_search 按需取
# （见 docs/MCP设计.md v2 §四）。与技能注册表同一处、同一套"会话内冻结"语义。
import mcp_station
import integration_pack

# 上下文管理器（同目录直接导入：视图生成 / 落盘 / 命中 / 回捞）
# 原文永不动：本模块只生产「发给模型的视图」，working_memory 与日志一律不改
from context_manager import ContextManager, load_params as load_cm_params

# 工具网关
from agent_tools import (
    AVAILABLE_TOOLS,
    NON_IDEMPOTENT_TOOLS,
    TOOL_TIMEOUTS,
    TOOLS_SCHEMA,
)


# ========== 宿主扩展点：每批工具返回之后 ==========
# 宿主（WebUI 网关 bridge）用 register_after_tools_hook() 把**自己的**回调挂进来，
# 例如「中期交互信箱」（agent_webui/backend/mid_turn.py）要在每批工具结果写回
# conversation 之后，把主人趁跑动时追加的交代作为独立 user 消息一并带回模型。
# 本模块只提供挂载点，**不认识任何具体宿主功能** —— CLI 下没有注册者，主循环遍历
# 空表，零开销、零行为差异。（与 approval.set_port / agent_tools.AVAILABLE_TOOLS 同构：
# 宿主注入实现，agent 本体只管路由。）
_AFTER_TOOLS_HOOKS: List[Callable[[List[Dict[str, Any]], str, Any], Any]] = []


def register_after_tools_hook(fn: Callable[[List[Dict[str, Any]], str, Any], Any]) -> None:
    """注册「一批工具结果写回 conversation 之后」的回调（宿主专用，可注册多个）。

    回调签名 fn(conversation, session_id, log) -> Any（返回值忽略）。
    钩子是宿主的东西，**绝不允许把回合带崩**：单个钩子抛异常只记一条 warning。
    重复注册同一函数会被忽略（幂等）。
    """
    if callable(fn) and fn not in _AFTER_TOOLS_HOOKS:
        _AFTER_TOOLS_HOOKS.append(fn)


def _run_after_tools_hooks(conversation: List[Dict[str, Any]], session_id: str,
                           log: Any) -> None:
    """主循环的调用点：依次跑已注册的钩子（没有注册者时什么都不做）。"""
    for fn in list(_AFTER_TOOLS_HOOKS):
        try:
            fn(conversation, session_id, log)
        except Exception as e:
            try:
                log.warning(f"after-tools 钩子失败（不阻断回合）: {type(e).__name__}: {e}")
            except Exception:
                pass


# ========== 宿主扩展点：LLM 流式增量 ==========
# 与 _AFTER_TOOLS_HOOKS 同构：宿主（WebUI bridge）把回调挂进来，把**生成中的**
# 思考/正文增量推给界面；本模块只提供挂载点与"逐块喂给它"的调用，不认识任何
# 具体宿主功能。CLI 下没有注册者 → 零开销、行为与从前逐字一致。
#
# 为什么需要它（2026-10-02 主人裁定的"流式"）：以前 stream=False，模型在思考的
# 整段时间里 agent 进程一个字都吐不出来，界面只能干等（实测单次生成最长 550s）。
# 开了流式之后，这里每收到一块就回调一次 —— 界面才是"边想边看"。
#
# 回调签名 fn(evt: dict) -> Any，evt 形如：
#   {"session_id": str, "kind": "reasoning"|"content", "text": str}
# 钩子是宿主的东西：**绝不允许把回合带崩** —— 单个钩子抛异常只记一条 warning 并跳过。
_DELTA_HOOKS: List[Callable[[Dict[str, Any]], Any]] = []


def register_delta_hook(fn: Callable[[Dict[str, Any]], Any]) -> None:
    """注册「流式增量」回调（宿主专用，可注册多个；重复注册同一函数被忽略=幂等）。"""
    if callable(fn) and fn not in _DELTA_HOOKS:
        _DELTA_HOOKS.append(fn)


def _emit_delta(session_id: str, kind: str, text: str, log=None) -> None:
    """把一块增量喂给所有钩子（没有注册者时直接返回）。"""
    if not _DELTA_HOOKS or not text:
        return
    evt = {"session_id": session_id, "kind": kind, "text": text}
    for fn in list(_DELTA_HOOKS):
        try:
            fn(evt)
        except Exception as e:  # noqa: BLE001
            try:
                if log is not None:
                    log.warning(f"delta 钩子失败（已忽略）: {type(e).__name__}: {e}")
            except Exception:
                pass


# ========== 1. 加载配置 ==========

# 1.1 显式加载 .env（不依赖 cwd）：项目根 .env 优先，兼容历史 agent/.env 位置
def _load_env_files() -> None:
    candidates = [
        PROJECT_ROOT / ".env",
        Path(__file__).parent / ".env",
    ]
    loaded = [str(p) for p in candidates if p.exists()]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
    if loaded:
        print(f"📄 已加载 .env: {', '.join(loaded)}")
    else:
        print("ℹ️ 未找到 .env 文件（项目根或 agent/ 目录），将读取系统环境变量与 config.yaml 默认值")

# 1.2 计算项目根目录（已定义）

# 1.3 加载 config.yaml
def load_config():
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"❌ 配置文件不存在: {config_path}\n"
            "请确保在项目根目录创建 config.yaml（参考文档）"
        )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# 1.35 加载 .env（须在读取 LLM 环境变量之前执行）
_load_env_files()

# 通用环境变量读取：按顺序取第一个非空值（新键名优先，兼容旧键名）
def _get_env(*names, default=None):
    """按顺序返回第一个非空环境变量；全部缺失则返回 default。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

# 1.4 解析路径
def resolve_path(relative_path: str) -> Path:
    return PROJECT_ROOT / relative_path

WORKING_MEMORY_DIR = resolve_path(CONFIG["paths"]["working_memory"])
LONG_MEMORY_DIR = resolve_path(CONFIG["paths"]["long_memory"])
SOUL_PATH = resolve_path(CONFIG["paths"]["soul_file"])
AGENTS_PATH = resolve_path(CONFIG["paths"]["agents_file"])
MEMORY_PATH = resolve_path(CONFIG["paths"]["memory_file"])
# USER.md（关于主人）：与 SOUL/AGENTS/MEMORY 同源注入；config 缺该键时按默认路径兜底
USER_PATH = resolve_path(CONFIG["paths"].get("user_file", "agent_memory/long_memory/USER.md"))
WORKSPACE_ROOT = resolve_path(CONFIG["paths"]["workspace"])
KNOWLEDGE_BASE_DIR = resolve_path(CONFIG["paths"]["knowledge_base"])
CHROMA_DB_DIR = resolve_path(CONFIG["paths"]["chroma_db"])
# 技能系统：技能库目录 + 注册表文件（缺省 agent_skills/，注册表每会话启动同步一次）
SKILLS_DIR = resolve_path(CONFIG["paths"].get("skills_dir", "agent_skills"))
SKILL_REGISTRY_PATH = resolve_path(
    CONFIG["paths"].get("skill_registry", "agent_skills/SKILL_REGISTRY.md")
)

# 确保必要的目录存在
WORKING_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
LONG_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_BASE_DIR.mkdir(parents=True, exist_ok=True)

# 1.45 上下文管理器：参数 + 常驻实例（会话日志在 main 里注入）
#      设计见 agent_workspace/上下文管理器/DESIGN.md，开关在 config.yaml 的
#      context_manager.enabled（false = 完全回退到原文直发）
CM_PARAMS = load_cm_params(CONFIG.get("context_manager"))
_CONTEXT_MGR: Optional[ContextManager] = None
_LAST_PROMPT_TOKENS: Dict[str, int] = {}      # sid → 上一轮 API 返回的真实 prompt_tokens


def get_context_manager(log=None) -> ContextManager:
    """懒创建常驻上下文管理器；会话切换时刷新其日志句柄。"""
    global _CONTEXT_MGR
    if _CONTEXT_MGR is None:
        _CONTEXT_MGR = ContextManager(PROJECT_ROOT, CM_PARAMS, log=log)
    elif log is not None:
        _CONTEXT_MGR.log = log
    return _CONTEXT_MGR

# 1.5 读取 LLM 配置（OpenAI 兼容通用方案，支持任意厂商）
#     .env 填三个核心变量即可切换厂商：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
#     旧键名 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / MODEL_NAME 仍兼容（自动回退）
#     REASONING_EFFORT / THINKING_ENABLED 是厂商专属参数，默认关闭（最兼容），
#     目标模型支持时再显式开启（详见 .env.example）。
LLM_API_KEY = _get_env("LLM_API_KEY", "DEEPSEEK_API_KEY")
MODEL_NAME = _get_env("LLM_MODEL", "MODEL_NAME", default=CONFIG["defaults"].get("model", "deepseek-chat"))
MAX_ITERATIONS = int(_get_env("MAX_ITERATIONS", default=CONFIG["defaults"].get("max_iterations", 100)))

# 新配置模式：只要用了 LLM_* 三件套（任意一个），行为参数只认 LLM_ 新键且默认关闭；
# 否则（纯旧 DEEPSEEK_* 配置）才兼容旧的 REASONING_EFFORT / THINKING_ENABLED 键。
_USING_LLM_KEYS = any(os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"))
if _USING_LLM_KEYS:
    REASONING_EFFORT = _get_env("LLM_REASONING_EFFORT", default="off")
    THINKING_ENABLED = _get_env("LLM_THINKING_ENABLED", default="false").lower() == 'true'
    LLM_BASE_URL = _get_env("LLM_BASE_URL")
    if not LLM_BASE_URL:
        raise ValueError(
            "❌ 使用 LLM_* 配置时请同时设置 LLM_BASE_URL\n"
            "（参考 .env.example 中各家厂商的端点示例）。"
        )
else:
    REASONING_EFFORT = _get_env("REASONING_EFFORT", default="off")
    THINKING_ENABLED = _get_env("THINKING_ENABLED", default="false").lower() == 'true'
    LLM_BASE_URL = _get_env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com")

# 流式开关（2026-10-02）：默认开。关掉它就完全回退到"整包返回"的老行为。
# 为什么要开：stream=False 时模型思考的全过程在界面上是完全不可见的 ——
# 实测单次生成最长 550s 一个字都不出，主人只能靠"掐掉重来"，白烧几千 token。
# 为什么敢开：usage 用 stream_options.include_usage 在最后一个 chunk 带回，
# 计量账本不会因此停摆；万一端点不认这个参数，下面的 fallback 会自动退回非流式。
STREAM_ENABLED = _get_env("LLM_STREAM", default="true").lower() not in ("0", "false", "off", "no")

if not LLM_API_KEY:
    raise ValueError(
        "❌ 环境变量 LLM_API_KEY 未设置（或旧名 DEEPSEEK_API_KEY）。\n"
        "请在项目根目录或 agent/ 目录的 .env 中填写（参考 .env.example），\n"
        "或先设置系统环境变量 LLM_API_KEY。"
    )


# ========== 2. 初始化 OpenAI 客户端 ==========
client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
)


# ========== 2.1 API 请求组装（厂商无关） ==========

# ========== 2.2 附件物化（内容块投影）==========
# 设计要点：conversation 里附件**只是一个文本标记**（`【附件】path=…`），
# 真正的"读字节 → 图片块 / 文本块"只在本节发生一次 —— 就在咽喉里。
# 这样带来的好处是决定性的：
#   · ContextManager / 会话文件 / 前端渲染 / mid_turn 注入 **全部零改动**
#     （它们看到的 content 永远是字符串）
#   · 不含附件的消息**逐字节等同改造前** → 任何 OpenAI 兼容模型（含纯文本）
#     接入本框架都零影响；只有真带了附件、且视觉开着，才会出现 content 数组。
ATT_MARK = "【附件】"
_ATT_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_ATT_ROLES = ("user", "tool")   # 附件标记可出现于哪些角色：user=用户上传 / tool=工具附加的图片
_ATT_DOC_EXT = {".docx", ".pdf", ".xlsx"}
_ATT_MAX_SIDE = 1400        # 送模型前最长边上限（控 image token）
_ATT_JPEG_Q = 88


def _att_cfg(key: str, default):
    try:
        return (CONFIG.get("attachments") or {}).get(key, default)
    except Exception:
        return default


def vision_enabled() -> bool:
    """视觉能力开关（config.yaml 的 llm.vision）。

    auto 的初判**刻意保守**：只认名字里明确属于视觉系/本机实测支持的模型，
    其余一律按"不支持"处理 —— 宁可少给能力，也不让纯文本模型收到数组后 400。
    要强制开启/关闭就写 true / false。
    """
    try:
        v = str((CONFIG.get("llm") or {}).get("vision", "auto")).strip().lower()
    except Exception:
        v = "auto"
    if v in ("false", "0", "no", "off", "disabled"):
        return False
    if v in ("true", "1", "yes", "on", "enabled"):
        return True
    name = str(MODEL_NAME or "").lower()
    if "deepseek-flash" in name or "deepseek-v4" in name:
        return True                      # 本机实测：图片输入可用（image_tokens>0）
    return any(k in name for k in ("vision", "-vl", "multimodal", "omni", "gpt-4o", "gpt-5"))


def _att_kind(ext: str) -> str:
    if ext in _ATT_IMAGE_EXT:
        return "image"
    if ext in _ATT_DOC_EXT:
        return "doc"
    return "text"


def _att_abs(rel: str):
    """相对路径 → 绝对路径；必须落在项目根内（防穿越）。"""
    try:
        p = (PROJECT_ROOT / str(rel).replace("\\", "/")).resolve()
        if p != PROJECT_ROOT and PROJECT_ROOT not in p.parents:
            return None
        return p
    except Exception:
        return None


def _att_parse(line: str) -> Dict[str, str]:
    """解析 `【附件】name=x | kind=y | path=z` → dict。"""
    s = str(line or "").strip()
    if s.startswith(ATT_MARK):
        s = s[len(ATT_MARK):]
    out: Dict[str, str] = {}
    for seg in s.split("|"):
        seg = seg.strip()
        if "=" in seg:
            k, _, v = seg.partition("=")
            out[k.strip().lower()] = v.strip()
    return out


def _att_image_data_url(path) -> Optional[str]:
    """图片 → JPEG data URL（缩到上限内，控 token）。"""
    try:
        import base64 as _b64
        import io as _io
        from PIL import Image
        im = Image.open(str(path))
        if im.mode != "RGB":
            im = im.convert("RGB")
        sc = min(1.0, float(_ATT_MAX_SIDE) / float(max(im.size) or 1))
        if sc < 1.0:
            im = im.resize((max(1, int(im.size[0] * sc)), max(1, int(im.size[1] * sc))))
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=_ATT_JPEG_Q)
        return "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _att_read_text(path, ext: str) -> str:
    """文本/文档 → 纯文本（docx/pdf/xlsx 尽力提取，失败如实说明）。"""
    lim = int(_att_cfg("max_text_chars", 200000) or 200000)
    try:
        if ext == ".docx":
            try:
                import docx
                t = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
            except Exception as e:
                return "(docx 提取失败：%s)" % type(e).__name__
        elif ext == ".pdf":
            try:
                from pypdf import PdfReader
                t = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
            except Exception as e:
                return "(pdf 提取失败：%s)" % type(e).__name__
        elif ext == ".xlsx":
            try:
                import openpyxl
                wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
                rows = []
                for ws in wb.worksheets:
                    rows.append("[sheet] %s" % ws.title)
                    for i, row in enumerate(ws.iter_rows(values_only=True)):
                        if i > 500:
                            rows.append("…（本表超过 500 行，已截断）")
                            break
                        rows.append("\t".join("" if c is None else str(c) for c in row))
                t = "\n".join(rows)
            except Exception as e:
                return "(xlsx 提取失败：%s)" % type(e).__name__
        else:
            with open(str(path), "rb") as f:
                t = f.read().decode("utf-8", errors="replace")
    except Exception as e:
        return "(读取失败：%s)" % type(e).__name__
    if len(t) > lim:
        t = t[:lim] + "\n…（已截断：原文 %d 字符，上限 %d）" % (len(t), lim)
    return t


def _att_build_parts(text: str, vision: bool, allow_images: bool = True):
    """把「正文 + 附件标记」的字符串拆成 content 块数组；无附件则返回 None。"""
    body_lines: List[str] = []
    atts: List[Dict[str, str]] = []
    for ln in str(text or "").split("\n"):
        if ln.strip().startswith(ATT_MARK):
            meta = _att_parse(ln)
            if meta.get("path"):
                atts.append(meta)
            continue
        body_lines.append(ln)
    if not atts:
        return None

    import os as _os
    parts: List[Dict[str, Any]] = []
    body = "\n".join(body_lines).strip()
    if body:
        parts.append({"type": "text", "text": body})

    for a in atts:
        rel = a["path"]
        name = a.get("name") or _os.path.basename(rel.replace("\\", "/"))
        ext = _os.path.splitext(name)[1].lower()
        kind = a.get("kind") or _att_kind(ext)
        p = _att_abs(rel)

        if p is None or not p.is_file():
            parts.append({"type": "text", "text": "【附件】%s —— 文件不存在或路径越界，已跳过" % name})
            continue

        if kind == "image":
            if not allow_images:
                # 老图折叠：这张图不在"最近 N 条"窗口里，不再重复上传 base64。
                # 只留一行可审计的痕迹，模型知道"曾经有过这张图"。
                parts.append({"type": "text",
                              "text": "【附件·图片】%s（历史图片已折叠，不再重复上传）" % name})
                continue
            if not vision:
                parts.append({"type": "text",
                              "text": "【附件·图片】%s（当前模型未启用视觉，只报文件名；"
                                      "如需描述可调用 vision_read 工具）" % name})
                continue
            url = _att_image_data_url(p)
            if url:
                parts.append({"type": "text", "text": "【附件·图片】%s" % name})
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append({"type": "text", "text": "【附件·图片】%s —— 读取失败" % name})
        else:
            parts.append({"type": "text",
                          "text": "【附件·文件】%s\n---\n%s\n---" % (name, _att_read_text(p, ext))})
    return parts


def _att_unwrap(text: str) -> str:
    """从 tool 消息的 content 里取出可能被 dict 包装的正文。

    工具的返回值由编排器 str() 后作为 tool 消息的 content，形如
    "{'success': True, 'result': '...', 'error': None}" —— 附件标记被裹在
    引号里、不在行首，按「行首扫描」会整个漏掉。这里只在「确实是 dict 字面量、
    且其中某字段含附件标记」时才解包，其余一律原样返回（对既有路径零影响）。
    """
    t = str(text or "")
    if ATT_MARK not in t:
        return t
    s = t.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return t
    try:
        import ast
        d = ast.literal_eval(s)
    except Exception:
        return t
    if isinstance(d, dict):
        for k in ("result", "content", "text", "message", "output"):
            v = d.get(k)
            if isinstance(v, str) and ATT_MARK in v:
                return v
    return t



def materialize_attachments(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """咽喉投影：含附件标记的 user / tool 消息 → content 块数组；其余原样。

    返回的 list 里，**没有改动的消息是同一个对象引用**（零拷贝），
    且当一条都没改时直接返回原 list —— 保证"没附件"这条路径与改造前一致。
    """
    if not messages:
        return messages
    vision = vision_enabled()

    # 老图折叠：只把**最近 N 条**含附件的消息展开成真图，更早的折叠为文本占位。
    # 为什么必须做：附件一旦进了历史，之后**每个回合**都会把整张图的 base64 再发一遍，
    # 传输量随轮次线性累积（实测一张 1400x787 的图 ≈ 397KB base64）。
    # 判据取"含附件的消息条数"而不是"轮数" —— 可预测、不受工具轮次影响。
    try:
        keep_n = int(_att_cfg("keep_recent_images", 2) or 0)
    except Exception:
        keep_n = 2
    att_idxs = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") in _ATT_ROLES
                and isinstance(m.get("content"), str) and ATT_MARK in m["content"]]
    keep_idx = set(att_idxs[-keep_n:]) if keep_n > 0 else set()

    out: List[Dict[str, Any]] = []
    changed = False
    for i, m in enumerate(messages):
        c = m.get("content")
        # 只处理 user / tool 角色的字符串 content（其它形态一律不碰）
        if (not isinstance(m, dict) or m.get("role") not in _ATT_ROLES
                or not isinstance(c, str) or ATT_MARK not in c):
            out.append(m)
            continue
        try:
            _body = _att_unwrap(c) if m.get("role") == "tool" else c
            parts = _att_build_parts(_body, vision, allow_images=(i in keep_idx))
        except Exception as e:
            parts = [{"type": "text", "text": "(附件物化失败：%s)\n%s" % (type(e).__name__, c)}]
        if not parts:
            out.append(m)
            continue
        nm = dict(m)
        nm["content"] = parts
        out.append(nm)
        changed = True
    return out if changed else messages


def compose_chat_kwargs(
    messages: List[Dict[str, Any]],
    model: str = MODEL_NAME,
    tools=None,
    reasoning_effort: str | None = None,
    thinking: bool = False,
    stream: bool | None = None,
) -> Dict[str, Any]:
    """组装 chat.completions.create 的请求参数。

    - reasoning_effort: 仅当显式给出且不是 off/none 时才附带（多数厂商不认，
      带了会 400，默认不传 = 最兼容）。
    - thinking: DeepSeek 系 / GLM 等 thinking 模型的专属字段，默认关闭。
    - stream: None = 跟随全局 STREAM_ENABLED（默认开）。开了就**必须**同时带上
      stream_options.include_usage —— 否则流式响应没有同步 usage，token 计量会静默停摆
      （bridge 侧把这条写成了显式告警，见 _install_usage_watch）。
      万一端点不认 stream_options，调用方会带着 fallback 重试一次非流式。
    """
    use_stream = STREAM_ENABLED if stream is None else bool(stream)
    kwargs: Dict[str, Any] = {
        "model": model,
        # 附件物化：含【附件】标记的消息在这里变成 content 块（图片块/文本块）；
        # 没有附件的消息原样透传 —— 对纯文本模型零影响。
        "messages": materialize_attachments(messages),
        "stream": use_stream,
        "tools": tools,
        "tool_choice": "auto",
    }
    if use_stream:
        kwargs["stream_options"] = {"include_usage": True}
    if reasoning_effort and str(reasoning_effort).lower() not in ("", "off", "none", "false"):
        kwargs["reasoning_effort"] = reasoning_effort
    if thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    return kwargs


def _mask_key(key: str) -> str:
    """打码密钥用于启动日志。"""
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}...{key[-4:]}"


# ========== 2.2 LLM 调用重试（显式、可配、落日志） ==========
# 审计条件二：框架自身原先**没有任何重试代码** —— API 一异常就 return {"error": ...}
# 整回合终止；实际重试全靠 OpenAI SDK 的隐式默认（max_retries=2），既不可配、不可见、
# 也不落日志。这里把重试显式化：有限次 + 指数退避 + 每次都落日志 + .env 可配。
LLM_MAX_ATTEMPTS = max(1, min(int(os.environ.get("LLM_MAX_ATTEMPTS", "3") or 3), 6))
LLM_RETRY_BASE_DELAY = max(0.0, float(os.environ.get("LLM_RETRY_BASE_DELAY", "1.5") or 1.5))

# 值得重试的错误：限流 / 超时 / 连接断了 / 服务端 5xx —— 等一会儿再来确实有救。
# 参数写错、鉴权失败、模型不存在这类 4xx，重试一万次也不会成功，立刻抛出去省时间。
_RETRYABLE_STATUS = (408, 409, 429)
_RETRYABLE_EXC_HINTS = ("Timeout", "Connection", "RateLimit", "ServiceUnavailable", "InternalServer")


def _is_retryable_llm_error(e: BaseException) -> bool:
    """这个异常重试有意义吗（有状态码就看状态码，没有就看异常类名）。"""
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS or 500 <= status < 600
    return any(h in type(e).__name__ for h in _RETRYABLE_EXC_HINTS)


class _AssembledMessage:
    """_consume_stream 的产物：形状对齐 SDK 的 ChatCompletionMessage。

    只带主循环真正读的三个字段：content / reasoning_content / tool_calls。
    tool_calls 里每项也是**对象**（有 .id/.type/.function.name/.function.arguments），
    因为主循环到处写的是 `tc.function.name` / `tc.function.arguments`。
    """

    def __init__(self, content, reasoning_content, tool_calls):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls

    def model_dump(self) -> Dict[str, Any]:
        # ⚠️ tool_calls 可能是 None（纯文本回合）——**不能**直接对它做列表推导，
        # 否则每个"只回答不调工具"的回合都会在这里 TypeError 崩掉（离线用例抓到过）。
        calls = None
        if self.tool_calls:
            calls = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments}}
                for tc in self.tool_calls
            ] or None
        return {
            "role": "assistant",
            "content": self.content,
            "reasoning_content": self.reasoning_content,
            "tool_calls": calls,
        }


class _FnSlice:
    def __init__(self):
        self.name = ""
        self.arguments = ""


class _ToolCallSlice:
    def __init__(self, index: int):
        self.index = index
        self.id = ""
        self.type = "function"
        self.function = _FnSlice()


class _AssembledChoice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class _AssembledResponse:
    """形状对齐 SDK 的 ChatCompletion —— 主循环只读 choices[0].message 与 usage。

    usage 用**最后一个 chunk** 带回来的那个对象（stream_options.include_usage 的产物），
    类型就是 SDK 的 CompletionUsage，所以 bridge 侧 _norm_usage 不需要任何改动。
    """

    def __init__(self, message, finish_reason, usage):
        self.choices = [_AssembledChoice(message, finish_reason)]
        self.usage = usage
        self.model = MODEL_NAME

    def model_dump(self) -> Dict[str, Any]:
        """对齐 SDK 的 ChatCompletion.model_dump()。

        ⚠️ 这个方法是**必需**的，不是锦上添花：主循环里有
        `log.debug("LLM 原始响应", raw=response.model_dump())`（agent.py 里那一行），
        日志写入器最后会 json.dumps 它。少了它，**每一轮流式响应都会在这里
        AttributeError 崩掉整个回合**（2026-10-02 端到端实测就是这么炸的）。
        """
        usage = self.usage
        if usage is not None and not isinstance(usage, dict):
            # 真机 usage 是 SDK 的 CompletionUsage（有 model_dump）；这里再兜两层，
            # 保证**任何**情况下这个 dump 都能过 json.dumps —— 它是打日志用的，
            # 绝不能因为一个序列化问题把整个回合崩掉（这正是 2026-10-02 那次的事故形态）。
            for _attr in ("model_dump", "to_dict", "dict"):
                _fn = getattr(usage, _attr, None)
                if callable(_fn):
                    try:
                        usage = _fn()
                        break
                    except Exception:
                        continue
            else:
                try:
                    usage = dict(vars(usage))
                except Exception:
                    usage = str(usage)
        return {
            "id": getattr(self, "id", "") or "",
            "object": "chat.completion",
            "created": int(getattr(self, "created", 0) or time.time()),
            "model": self.model,
            "choices": [
                {"index": i,
                 "finish_reason": c.finish_reason,
                 "message": c.message.model_dump()}
                for i, c in enumerate(self.choices)
            ],
            "usage": usage,
        }


def _consume_stream(stream, *, session_id: str = "", log=None) -> _AssembledResponse:
    """把一个流式响应收成**一个**与整包返回同形的对象，同时边收边把增量喂给宿主钩子。

    三件事必须同时成立，缺一不可：
      1. **拼装**：content / reasoning_content 拼接，tool_calls 按 index 分片拼装
         （SDK 的流式 tool_calls 是碎片：第一个片带 id+name，后续片只带 arguments 增量）。
      2. **计量**：usage 只在**最后一个 chunk** 上（stream_options.include_usage），
         必须抓下来挂到返回对象上，否则 token 计量静默停摆。
      3. **可见**：每块增量立刻回调 _emit_delta —— 这就是"流式"的全部意义。
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    slices: Dict[int, _ToolCallSlice] = {}
    usage = None
    finish_reason = None

    for chunk in stream:
        try:
            if getattr(chunk, "usage", None) is not None:
                usage = chunk.usage                 # include_usage 的最后一帧
        except Exception:
            pass
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        ch0 = choices[0]
        if getattr(ch0, "finish_reason", None):
            finish_reason = ch0.finish_reason
        delta = getattr(ch0, "delta", None)
        if delta is None:
            continue

        # ---- 思考增量（DeepSeek thinking 模式把它放在 reasoning_content）----
        rc = getattr(delta, "reasoning_content", None)
        if not rc:
            # 有的兼容端点把它塞进 model_extra / 别的键名，兜一层
            extra = getattr(delta, "model_extra", None) or {}
            rc = extra.get("reasoning_content")
        if rc:
            reasoning_parts.append(rc)
            _emit_delta(session_id, "reasoning", rc, log)

        # ---- 正文增量 ----
        c = getattr(delta, "content", None)
        if c:
            content_parts.append(c)
            _emit_delta(session_id, "content", c, log)

        # ---- 工具调用分片 ----
        for tc in (getattr(delta, "tool_calls", None) or []):
            try:
                idx = int(getattr(tc, "index", 0) or 0)
            except (TypeError, ValueError):
                idx = 0
            slot = slices.get(idx)
            if slot is None:
                slot = _ToolCallSlice(idx)
                slices[idx] = slot
            if getattr(tc, "id", None):
                slot.id = tc.id
            ttype = getattr(tc, "type", None)
            if ttype:
                slot.type = ttype
            fn = getattr(tc, "function", None)
            if fn is not None:
                fname = getattr(fn, "name", None)
                if fname:
                    slot.function.name = fname
                fargs = getattr(fn, "arguments", None)
                if fargs:
                    slot.function.arguments += fargs

    tool_calls: List[_ToolCallSlice] = [slices[k] for k in sorted(slices)]
    # id 缺失的补一个稳定 id：OpenAI 协议要求 tool_call 有 id，后面 role=tool 的
    # 响应靠它配对；缺了会话就落成"有 tool_call 无响应"的断头（下次加载直接 400）。
    for i, tc in enumerate(tool_calls):
        if not tc.id:
            tc.id = f"call_stream_{i}_{int(time.time() * 1000)}"

    msg = _AssembledMessage("".join(content_parts), "".join(reasoning_parts) or None,
                            tool_calls or None)
    # 把 usage 交给宿主：宿主（bridge）包的是 create() 的返回值，流式下它没有别的
    # 同步来源，计量就靠这一步。两种交付方式，都做，因为**时序**踩过坑：
    #   · `_ab_usage` 属性：宿主"排空后自己去读"的路子；
    #   · `_ab_usage_sink(usage)` 回调：宿主**显式被通知**的路子。
    # 为什么必须补回调：`for x in it` 的语义是"__next__ 抛 StopIteration 就结束"，
    # 所以宿主的结算**早于**循环体后面这几行 —— 光挂属性，宿主读到的永远是"缺失"
    # （2026-10-02 实测：对象对、时机错，账本一条不记）。
    # 挂不上/没实现都不算错（宿主会少记一次量，绝不反噬回合）。
    try:
        stream._ab_usage = usage
    except Exception:
        pass
    try:
        _sink = getattr(stream, "_ab_usage_sink", None)
        if callable(_sink):
            _sink(usage)
    except Exception:
        pass
    return _AssembledResponse(msg, finish_reason, usage)


def _looks_like_stream_unsupported(e: BaseException) -> bool:
    """这个异常是不是"端点不认流式/不认 stream_options"（→ 值得退回非流式再试一次）。

    只认 4xx（参数类错误）。超时/限流/5xx 是**另一回事**，交给原来的重试退避逻辑，
    不要在这里把流式悄悄关掉 —— 否则一次网络抖动就永久降级，而且没人知道。
    """
    status = getattr(e, "status_code", None)
    if not isinstance(status, int) or not (400 <= status < 500):
        return False
    text = f"{e}".lower()
    return any(k in text for k in ("stream_options", "include_usage", "stream"))


def chat_with_retry(client, log, **kwargs):
    """带指数退避的 LLM 调用 —— 主循环的**唯一**调用入口。

    退避序列 = base × 1, 2, 4, …（默认 1.5s / 3s / 6s），最多 LLM_MAX_ATTEMPTS 次。
    每次失败都落日志（"重试两次"从此看得见），耗尽才把最后一次异常抛给调用方
    —— 由调用方决定终止回合。KeyboardInterrupt / SystemExit 永远原样抛出（停止按钮
    依赖它）。

    流式（2026-10-02）：kwargs 里 stream=True 时，这里负责把流收成整包形状再返回，
    所以**调用方一行都不用改**。agent 层每次调用都带上 session_id（增量钩子要按会话
    归属），非流式路径多余的关键字会在下面剥掉。
    """
    session_id = str(kwargs.pop("_session_id", "") or "")
    last: BaseException | None = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            if kwargs.get("stream"):
                return _consume_stream(resp, session_id=session_id, log=log)
            return resp
        except BaseException as e:          # noqa: BLE001 - 先分类再决定重试/抛出
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            last = e
            # 端点不认流式参数 → 退回整包返回（一次性降级决策，**显式落日志**，
            # 不做静默降级：以后有人问"为什么没有增量"，日志里有答案）。
            if kwargs.get("stream") and _looks_like_stream_unsupported(e):
                kwargs = dict(kwargs)
                kwargs.pop("stream", None)
                kwargs.pop("stream_options", None)
                try:
                    log.warning("端点不认流式参数，本回合退回非流式（无增量输出）: "
                                f"{type(e).__name__}: {e}")
                except Exception:
                    pass
                continue
            if attempt >= LLM_MAX_ATTEMPTS or not _is_retryable_llm_error(e):
                raise
            delay = LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            try:
                log.warning(f"LLM 调用失败（第 {attempt}/{LLM_MAX_ATTEMPTS} 次），"
                            f"{delay:.1f}s 后重试: {type(e).__name__}: {e}")
            except Exception:
                pass
            time.sleep(delay)
    raise last if last is not None else RuntimeError("LLM 调用失败（无异常信息）")


print(
    f"🤖 LLM: model={MODEL_NAME} | base_url={LLM_BASE_URL} | "
    f"key={_mask_key(LLM_API_KEY)} | thinking={'ON' if THINKING_ENABLED else 'off'} | "
    f"reasoning_effort={REASONING_EFFORT}"
)


# ========== 3. 会话管理函数 ==========

def get_session_id() -> str:
    """获取或创建会话ID"""
    print("\n" + "=" * 60)
    print("🤖 AetherBreath Agent ")
    print("=" * 60)

    existing = list(WORKING_MEMORY_DIR.glob("*.json"))
    if existing:
        print("\n📂 已有会话:")
        for i, f in enumerate(existing, 1):
            try:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                    msg_count = len(data.get("messages", []))
                    created = data.get("created_at", "未知")
                    status = data.get("status", "complete")
                    status_mark = {"active": "🔄 进行中", "interrupted": "⚠️ 中断未完成", "complete": "✅ 正常"}.get(status, status)
                    print(f"  {i}. {f.stem} ({status_mark}, 消息数: {msg_count}, 创建: {created})")
            except:
                print(f"  {i}. {f.stem}")

    print("\n选项:")
    print("  - 输入已有会话ID 继续对话")
    print("  - 输入 'new' 创建新会话")
    print("  - 直接回车 自动创建新会话")

    choice = input("\n💬 请输入: ").strip()

    if choice and choice.lower() != 'new':
        session_id = choice
        session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
        if not session_file.exists():
            print(f"⚠️ 会话 '{session_id}' 不存在，将创建新会话")
            return session_id
        return session_id

    session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"✅ 新会话创建: {session_id}")
    return session_id


def load_session(session_id: str, log: SessionLogger) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    加载会话历史。

    Returns:
        (messages, saved_snapshot): messages 为纯对话历史（不含 system）；
        saved_snapshot 为上次保存的 system_prompt（语境快照），
        用于续聊时判断「源文件是否变化、能否沿用旧快照」。
    """
    session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
    if session_file.exists():
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                messages = data.get("messages", [])
                clean = [m for m in messages if m.get("role") != "system"]
                if len(clean) != len(messages):
                    log.warning(f"会话 {session_id} 清理了 {len(messages) - len(clean)} 条重复 system 消息")
                    messages = clean
                snapshot = data.get("system_prompt")
                if snapshot:
                    one_line = snapshot.replace("\n", " ").strip()
                    print(f"📜 上次语境快照: {one_line[:60]}...")
                # 权限模式从会话文件读回引擎（2026-10）：模式是会话级的，
                # 进程重启后必须回到退出前那一档，而会话文件是它唯一的家。
                # **只在内存里还没有该会话的记录时读**：本进程既然处理过这个会话，
                # 内存那一份就是更新的；无条件读回会把"刚切好的新档"盖成文件里的
                # 旧档 —— 实测症状是切档卡死在曾经用过的某一档上出不来。
                # 缺字段 = 旧会话，保持默认档（普通），不做任何推断。
                try:
                    import permission_modes as _PM      # noqa: PLC0415
                    if not _PM.tracked(session_id):
                        _PM.set(session_id, data.get("permission_mode"))
                except Exception as e:
                    log.warning(f"权限模式读回失败（按默认档继续）: {e}")
                status = data.get("status", "complete")
                if status == "interrupted":
                    print(f"♻️ 会话 '{session_id}' 上次被中断，已从断点恢复（{len(messages)} 条消息）")
                else:
                    print(f"📂 加载会话 '{session_id}'，共 {len(messages)} 条消息")
                return messages, snapshot
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"加载会话失败: {e}，从空会话开始")
            return [], None
    return [], None


def save_session(session_id: str, messages: List[Dict[str, str]], log: SessionLogger, status: str = "active"):
    """保存会话历史（原子写）"""
    session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
    tmp_file = session_file.with_suffix(".json.tmp")
    try:
        system_prompt = None
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m.get("content", "")
                break
        messages = [m for m in messages if m.get("role") != "system"]
        data = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "message_count": len(messages),
            "status": status,
            "messages": messages
        }
        # 权限模式随会话落盘（2026-10）。**必须由这里写**：本函数是全量覆盖写，
        # 网关侧另存的字段会被下一次保存整片抹掉 —— 所以模式不能放旁挂文件，
        # 得由会话文件的唯一写者负责。启动/恢复会话时由 load_session 读回。
        try:
            import permission_modes as _PM          # noqa: PLC0415
            data["permission_mode"] = _PM.get(session_id)
        except Exception:
            pass
        if system_prompt is not None:
            data["system_prompt"] = system_prompt
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, session_file)
    except Exception as e:
        log.error(f"保存会话失败: {e}")
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


# ========== 4. 核心 Agent 循环 ==========

# ===== 4.0 常驻编排器单例：agent.py 仅作路由器，不直接执行任何工具 =====
# 编排器实例化一次（双通道线程池：串行 1 worker + 并行 8 worker），
# 跨 LLM 轮次/会话复用；一切工具执行都交给它，避免反复建销线程池。



def close_dangling_tool_calls(conversation, tool_calls, log,
                              reason="❌ 本回合在该调用生效前被中断，操作未执行。"):
    """给每个已声明却没有响应的 tool_call 补一条 role=tool 消息。

    OpenAI 兼容接口要求 assistant.tool_calls 的每个 id 都有配对 tool 消息，缺一条
    就会让该会话下次加载被判 400（表现为这个对话再也发不出消息）；界面侧则显示成
    一个空的工具卡片。审批把等待拉到分钟级，使这种断头从罕见变成常见，故必须闭合。
    """
    try:
        have = set()
        for m in conversation:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                have.add(m.get("tool_call_id"))
        n = 0
        for tc in (tool_calls or []):
            tcid = getattr(tc, "id", None)
            if tcid and tcid not in have:
                conversation.append({"role": "tool", "tool_call_id": tcid, "content": reason})
                n = n + 1
        if n:
            log.warning("已为 " + str(n) + " 个未响应调用补闭合消息（防会话损坏）")
        return n
    except Exception:
        return 0        # 闭合逻辑自己绝不能再把回合搞崩


_ORCHESTRATOR: TaskOrchestrator | None = None

# ---- 幽灵执行的止损点（审计 L1-B1）----
# 被判「超时」的有副作用调用，其线程仍在后台跑（可能已经成功）。若模型原样重发，
# 就会把同一个副作用做两遍（写盘变重复、提交变两次、删除执行两遍）。
# 这里按会话记住这些调用的指纹，下次见到**原样**重发就拒绝，逼它先核对目标状态。
_TIMED_OUT_CALLS: Dict[str, set] = {}          # session_id -> {调用指纹}
# 单会话最多记这么多条：超时是有副作用的少数事件，量级有限，不做淘汰也够用。
_TIMED_OUT_LIMIT = 200


def _call_fingerprint(name: str, args_text: str) -> str:
    """工具调用的**规范化**指纹：同语义、不同字面 → 同指纹。

    修复前是 `md5(模型给的原始 JSON 字符串)`（审计 L1-B3）：只差一个空格、键序颠倒
    或换行就判成两次不同调用 —— 对模型每轮重新生成的 JSON 几乎不命中。这里解析成
    dict 后按键排序序列化，解析失败才退回原样（保住"至少不比以前差"）。
    """
    try:
        obj = json.loads(args_text)
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError, ValueError):
        canon = args_text or ""
    return hashlib.md5((name + "|" + canon).encode("utf-8")).hexdigest()


def _remember_timeout(session_id: str, fingerprint: Optional[str]) -> None:
    """记下一个「超时（结果未知）」的调用指纹（幽灵执行止损用）。"""
    if not fingerprint:
        return
    bucket = _TIMED_OUT_CALLS.setdefault(session_id, set())
    if len(bucket) < _TIMED_OUT_LIMIT:
        bucket.add(fingerprint)


# ---- 后台作业的止损线（P1）：已转后台的调用**不许原样重发** ----
# 它与"超时止损"是两条独立线：那条管"结果未知"，这条管"这件事已经交出去了"。
# 同一个副作用重发两遍后果一样严重，所以要拦；但**出路不同** —— 这里是"去取结果"，
# 不是"去核对是否做成"。判据不复制：只读工具一律放行（重发安全）。
_PENDING_JOBS: Dict[str, Dict[str, str]] = {}      # session_id -> {调用指纹: 作业号}


def _remember_background(session_id: str, fingerprint: Optional[str], job_id: str) -> None:
    """记下"这个调用已转成后台作业"（原样重发止损用）。有界，与超时台账同一上限。"""
    if not fingerprint or not job_id:
        return
    bucket = _PENDING_JOBS.setdefault(session_id, {})
    if len(bucket) < _TIMED_OUT_LIMIT:
        bucket[fingerprint] = job_id


def _background_refuse_text(session_id: str, func_name: str, fingerprint: str) -> Optional[str]:
    """这次调用是否与某个后台作业同源 -> 返回拒绝文案（否则 None）。"""
    if func_name not in NON_IDEMPOTENT_TOOLS:
        return None
    jid = _PENDING_JOBS.get(session_id, {}).get(fingerprint)
    if not jid:
        return None
    return ("❌ 拒绝原样重发：这次调用此前已经转成**后台作业 " + jid + "** —— "
            "它仍在跑（或已跑完），重发等于把同一个副作用做两遍。\n"
            "  · 看进度 / 取结果：task_output(job_id=\"" + jid + "\")\n"
            "  · 不要它了：task_kill(job_id=\"" + jid + "\")\n"
            "  · 确实要再跑一遍：改一处参数（那才算一次新的调用）。")


def _is_refused_retry(session_id: str, func_name: str, fingerprint: str) -> bool:
    """这次调用是否踩中「幽灵执行」的止损条件。

    条件 = 工具**有副作用**（重复执行会在外部留痕）**且**它此前被判过「超时（结果未知）」。
    超时后那个工具的线程仍在后台跑（可能已经成功），原样重发就等于把同一个副作用做两遍。
    """
    return (func_name in NON_IDEMPOTENT_TOOLS
            and fingerprint in _TIMED_OUT_CALLS.get(session_id, ()))


def _get_orchestrator() -> TaskOrchestrator:
    """懒创建常驻编排器；首次调用后整场会话复用同一实例。"""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        _ORCHESTRATOR = TaskOrchestrator(
            max_workers=16,
            default_timeout=30,
            tools_map=AVAILABLE_TOOLS,
            tool_timeouts=TOOL_TIMEOUTS,   # per-tool 超时声明（同一 dict 引用，运行期可追加）
            side_effect_tools=NON_IDEMPOTENT_TOOLS,   # 有副作用的工具（超时文案强度 + 拒绝原样重试）
            log_enabled=True,
        )
        # ⚠️ 关键装配线：把实例注册到 task_orchestrator 的模块级引用上 ——
        # task_list / task_output / task_kill 三个工具就是从这里拿编排器的。
        # 漏这一行的后果**真机实测过**（2026-09-29）：三个接口恒定返回"没有可用的任务编排器"，
        # 而看板照常推送 —— 因为看板走的是本模块内部的 _ORCHESTRATOR，不经过这个注册。
        task_orchestrator.set_current_orchestrator(_ORCHESTRATOR)
        print("🧭 常驻编排器已启动（串行管道 ×1 + 并行管道 ×16）")
    return _ORCHESTRATOR


def shutdown_orchestrator() -> None:
    """关闭常驻编排器，回收双通道线程池（会话结束/进程退出时调用）。"""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        _ORCHESTRATOR.shutdown()
        _ORCHESTRATOR = None
        task_orchestrator.set_current_orchestrator(None)   # 注销：别让工具拿到一个已死的编排器
        print("🧭 常驻编排器已关闭")


# ===== 4.05 请求组装：system 快照原样 + 历史走上下文视图 =====
# 只影响「发给模型的那一份」，conversation 与落盘原文一字不改 —— 原文永远是真相。

def assemble_request(conversation: List[Dict[str, Any]], session_id: str, log) -> List[Dict[str, Any]]:
    """组装请求体：system 消息（快照冻结）不动，历史部分优先用已落盘的上下文视图。"""
    if not CM_PARAMS.get("enabled"):
        return conversation
    try:
        sys_msgs = [m for m in conversation if m.get("role") == "system"]
        history = [m for m in conversation if m.get("role") != "system"]
        body, how = get_context_manager(log).view_for(session_id, history)
        if how == "stale_view":
            # 视图与原文不一致：本轮回退原文，下个轮次边界会重算视图。
            # 用量不必清 —— 本轮 API 返回的真实 prompt_tokens 会覆盖它。
            log.warning("上下文视图与原文不一致：本轮回退原文，下个轮次边界重算")
        return sys_msgs + list(body)
    except Exception as e:
        log.warning(f"上下文视图组装失败，本轮用原文: {e}")
        return conversation


def _deliver_finished_jobs(conversation: List[Dict[str, Any]], session_id: str, log) -> int:
    """【自动交付】把本会话已结算、尚未交付的后台作业**全文**写进会话历史，再释放它的内存。

    这是"跨回合任务完成"唯一的落地方式，所以有三条硬约束（每条都对应一个会毁掉交付的坑）：

    1. **落进 conversation（持久化），不是只拼进这一次请求。** 交付之后作业内存就释放了、
       `task_output` 也不再返回内容 —— 不落盘的话这份结果在下一轮就永久消失，
       "自动交付"就成了假话。
    2. **必须用 user 角色，不能用 system。** `save_session` / `load_session` 会把 `system`
       消息**整条丢掉**（那正是语境快照的处理方式），而 `assemble_request` 又会把所有
       `system` 消息**提到最前**当语境快照 —— 两条路都会毁掉这条交付。所以走 `user` +
       响亮前缀（与「用户交代」同款），并让 bridge 的 `_user_seq` 把它排除在
       "真实用户输入"之外，免得界面的轮次错位。
    3. **先落盘、再释放**（顺序不能反）：反过来一次写盘失败就等于结果永久消失。

    没有待交付作业时**零开销**（一次列表查询）。
    """
    try:
        orch = _get_orchestrator()
        jobs = orch.jobs.pending_delivery(session_id) if orch is not None else []
    except Exception as e:
        log.warning(f"后台作业交付失败（不影响本回合）: {type(e).__name__}: {e}")
        return 0
    if not jobs:
        return 0
    delivered: List[str] = []
    for job in jobs:
        try:
            text = job.delivery_text()
        except Exception as e:
            log.warning(f"作业 {job.job_id} 的交付文本渲染失败: {type(e).__name__}: {e}")
            continue
        conversation.append({"role": "user", "content": text})
        delivered.append(job.job_id)
    if not delivered:
        return 0
    try:
        save_session(session_id, conversation, log)     # 先落盘（durable）
    except Exception as e:
        del conversation[-len(delivered):]              # 回滚，别在内存里留重复的交付
        log.warning(f"交付落盘失败（作业保持待交付，下轮重试）: {type(e).__name__}: {e}")
        return 0
    for jid in delivered:
        try:
            # 再释放内存 + **从登记册出册**（P12：交付过的不用再留着 —— 内容已在会话历史里）
            orch.jobs.mark_delivered(jid)
        except Exception as e:
            log.warning(f"作业 {jid} 标记交付失败: {type(e).__name__}: {e}")
    try:
        # 记一笔"刚交付了谁"，供 bridge 放进 `done` 事件 —— 界面据此**当场**重放历史，
    # 那条交付才不用等你切会话/刷新才出现（交付本身是落盘消息，实时通道里没有它）。
        _DELIVERED_NOTICES.extend(delivered)
        del _DELIVERED_NOTICES[:-50]                    # 只留最近 50 条，别无限长
    except Exception:
        pass
    log.info("[后台作业交付] %d 个作业的全文已写入会话: %s"
             % (len(delivered), ", ".join(delivered)))
    return len(delivered)


# 最近自动交付过的作业号（agent 侧的事实，bridge 取走后清空）—— 见 take_delivered_notices
_DELIVERED_NOTICES: List[str] = []


def take_delivered_notices() -> List[str]:
    """取走并清空"刚交付过"的作业号（bridge 用来给 `done` 事件带上 `delivered_jobs`）。

    为什么不让 bridge 自己去看会话文件：交付这件事只有 agent 侧**知道确切时刻**
    （它就是在这里 append + 落盘的）。取走即清空 = 同一批交付不会被报两次。
    """
    out = list(_DELIVERED_NOTICES)
    _DELIVERED_NOTICES.clear()
    return out


def _with_pool_note(messages: List[Dict[str, Any]], log) -> List[Dict[str, Any]]:
    """把**池压力**提示临时拼进本次请求（不落盘：conversation 一字不改）。

    为什么它不像作业交付那样落盘：这是"**当下这一刻**的资源状况"（池子快满了），
    下一轮就未必成立 —— 落盘只会留下一串历史噪音。作业交付则是"已经发生的事"，必须持久。

    - 每次返回**新列表**：就地 append 会把提示写进真实历史。
    - 池子不紧张时原样返回：零开销、零行为变化。
    """
    try:
        orch = _get_orchestrator()
        note = orch.pool_pressure_note() if orch is not None else ""
    except Exception as e:
        log.warning(f"池压力提示渲染失败（不影响本回合）: {type(e).__name__}: {e}")
        return messages
    if not note:
        return messages
    return list(messages) + [{"role": "system", "content": note}]


def _with_mode_notice(messages: List[Dict[str, Any]], session_id: str) -> List[Dict[str, Any]]:
    """把**换档那一次**的权限通知拼进本次请求（取走即清，不落盘）。

    口径（主人 2026-10 亲口给的，逐条兑现）：
      · **只有真的换了档才有这一行** —— 切到同一个档不算切换；没换档的每一次发送
        都一条不加（每轮都带 = 每条消息都多一行、白烧 token，他实测反馈过）；
      · **走 user 通道**（不是 system）：他要的是"追加到 user 通道直达 LLM" ——
        夹在最后一条 user 消息之后、与它同通道；
      · 格式 `旧权限->新权限：当前权限的一句话`（`permission_modes.notice_text`，
        界面那块状态块与它同源，不写第二份）；
      · 也不写进历史：切几次就堆几条（更早一版的毛病）。
    所以：换档时 `permission_modes.set()` 记一条，这里在下一次请求取走，之后自然消失。
    """
    try:
        import permission_modes as _PM      # noqa: PLC0415
        note = _PM.take_notice(session_id)
    except Exception as e:
        # 取不到就**照常发请求**（不过是少一行告知，门还在），但要留痕
        try:
            print("[ab] 权限模式通知渲染失败（本轮不带）: %s: %s"
                  % (type(e).__name__, e), file=sys.__stderr__)
        except Exception:
            pass
        return messages
    if not note:
        return messages
    return list(messages) + [{"role": "user", "content": note}]


def _install_restore_provider(conversation: List[Dict[str, Any]], session_id: str) -> None:
    """把「当前会话原文」接到 restore_context 工具上。

    视图层下原文始终在内存（conversation）里，取回无需任何归档文件；
    conversation 是同一个 list 对象，本回合后续 append 的内容也能取到。
    """
    try:
        from agent_tools.restore_context import set_provider
        cm = get_context_manager()

        def _fn(round_no=None, tool_call_id=None, max_chars: int = 20000):
            # 轮号口径与视图一致：都不含 system（system 是快照，不属于任何一轮）
            hist = [m for m in conversation if m.get("role") != "system"]
            return cm.restore(session_id, hist, round_no=round_no,
                              tool_call_id=tool_call_id, max_chars=max_chars)

        set_provider(_fn)
    except Exception:
        pass          # 数据源装不上不影响主流程（工具会给明确错误）


def _maybe_compact_at_turn_start(conversation: List[Dict[str, Any]], session_id: str, log) -> None:
    """回合入口：判定并生成上下文视图（同步，实测 603 条约 50ms）。

    ⚠️ 必须放在这里、而不是 main() 的交互循环里：
      - CLI 走 main() → call_agent_with_tools
      - **WebUI 走 bridge.py:1065 → call_agent_with_tools（完全不经过 main()）**
    2026-09-13 实测事故：判定只写在 main() 里，WebUI 会话永远不触发压缩，
    视图目录一直是空的（主人发现"压缩后没看到视图"）。
    两条路径的公共入口只有这一个函数，故判定放这里。
    """
    if not CM_PARAMS.get("enabled"):
        return
    try:
        get_context_manager(log).maybe_compact(
            session_id,
            [m for m in conversation if m.get("role") != "system"],
            prompt_tokens=_LAST_PROMPT_TOKENS.get(session_id),
            model=MODEL_NAME,
        )
    except Exception as e:
        log.warning(f"上下文压缩判定失败（本轮继续用现有上下文）: {e}")


def call_agent_with_tools(
    messages: List[Dict[str, Any]],
    session_id: str,
    log: SessionLogger,
    max_iterations: int = MAX_ITERATIONS
) -> Dict[str, Any]:
    conversation = messages.copy()
    _install_restore_provider(conversation, session_id)   # restore_context 的数据源
    _maybe_compact_at_turn_start(conversation, session_id, log)   # 轮次边界：压缩判定

    def ensure_tool_responses(conv: List[Dict[str, Any]]) -> None:
        i = 0
        while i < len(conv):
            msg = conv[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                needed_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                existing_ids = set()
                j = i + 1
                while j < len(conv) and conv[j].get("role") == "tool":
                    tid = conv[j].get("tool_call_id")
                    if tid in needed_ids:
                        existing_ids.add(tid)
                    j += 1
                missing_ids = needed_ids - existing_ids
                if missing_ids:
                    log.warning(f"为 {len(missing_ids)} 个 tool_calls 补上 '编排器未响应'")
                    for tid in sorted(missing_ids):
                        conv.insert(i + 1, {
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": "❌ 编排器未响应",
                        })
                        i += 1
            i += 1

    ensure_tool_responses(conversation)
    log.info("Agent 循环开始", max_iterations=max_iterations, message_count=len(conversation))
    iteration = 0
    # 提前初始化（审计 B4）：中断分支要拿它去闭合"有 tool_call 无响应"的断头，
    # 而原来的写法靠 `except NameError` 兜"变量还没定义" —— 那会连被调函数内部的
    # 笔误型 NameError 一起吞掉（同型事故：一个笔误静默拆掉整道审批门）。
    tool_calls = []

    try:
        while iteration < max_iterations:
            iteration += 1
            log.debug(f"Agent 循环第 {iteration}/{max_iterations} 轮")
            ensure_tool_responses(conversation)

            # 跨回合作业的**自动交付**：全文落进 conversation（持久），再释放它的内存。
            # 必须在 assemble_request 之前做 —— 它改的是真实历史，不是"这一次请求"。
            _deliver_finished_jobs(conversation, session_id, log)

            try:
                response = chat_with_retry(client, log, **compose_chat_kwargs(
                    _with_mode_notice(
                        _with_pool_note(assemble_request(conversation, session_id, log), log),
                        session_id),
                    model=MODEL_NAME,
                    tools=TOOLS_SCHEMA,
                    reasoning_effort=REASONING_EFFORT,
                    thinking=THINKING_ENABLED,
                ), _session_id=session_id)
                # 注：_session_id 只给流式增量钩子做会话归属用，chat_with_retry 会
                # 在发请求前把它从 kwargs 里剥掉（它不是 API 参数，带上会被端点 400）。
            except Exception as e:
                log.error(f"API 调用失败（已重试到 {LLM_MAX_ATTEMPTS} 次上限）: {e}")
                return {"error": f"API 请求异常: {str(e)}"}

            log.debug("LLM 原始响应", raw=response.model_dump())

            # 上下文用量真值：本轮的 prompt_tokens 就是「当前上下文实际占用」，
            # 供下一个轮次边界判定是否压缩（本地估算只作兜底，见 DESIGN.md §6）
            try:
                _u = getattr(response, "usage", None)
                if _u is not None:
                    _LAST_PROMPT_TOKENS[session_id] = int(getattr(_u, "prompt_tokens", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                pass

            assistant_msg = response.choices[0].message
            conversation.append(assistant_msg.model_dump())
            save_session(session_id, conversation, log)

            if assistant_msg.tool_calls:
                reasoning_text = getattr(assistant_msg, "reasoning_content", None)
                progress_text = (assistant_msg.content or "").strip() or (reasoning_text or "").strip()
                if progress_text:
                    print(f"[中期进度]: {progress_text}")
                    log.info(f"[中期进度]: {progress_text}")
            if not assistant_msg.tool_calls:
                log.info(f"Agent 完成推理，共 {iteration} 轮")
                save_session(session_id, conversation, log, status="complete")
                return {
                    "content": assistant_msg.content or "",
                    "conversation": conversation,
                    "iterations": iteration,
                    "prompt_tokens": _LAST_PROMPT_TOKENS.get(session_id),
                }

            tool_calls = assistant_msg.tool_calls

            # ---- 统一工具执行：agent.py 仅作路由器，一切工具执行交给常驻编排器 ----
            # 单工具调用 → 编排器串行管道（1 worker）；多工具批 → 并行管道（依赖分层）
            orchestrator = _get_orchestrator()
            orchestrator.set_logger(log)
            orchestrator.set_session(session_id)   # 后台作业要标注归属会话
            log.info(f"工具调用开始，共 {len(tool_calls)} 个（统一编排）")

            parsed_calls = []
            audit_items = []      # (tc, name, args)：本批待审批项
            call_record = set()
            call_fp: Dict[str, str] = {}      # tool_call_id -> 规范化指纹（超时后要记档）

            for tc in tool_calls:
                func_name = tc.function.name
                fp = _call_fingerprint(func_name, tc.function.arguments)
                key = (func_name, fp)
                if key in call_record:
                    log.warning(f"检测到重复调用，已跳过: {func_name}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        # ❌ 而非 ⚠️：这是一次"没有执行"，按失败契约该计失败（审计 L3-W8：
                        # ⚠️ 三份判据全不认 → 被记成成功）。文案里点名"别原样重试"，
                        # 免得模型把它当普通失败又发一遍同样的调用。
                        "content": "❌ 检测到重复调用，已跳过本次（同一批里同工具+同参数只执行一次，改参数再试）。",
                    })
                    continue
                call_record.add(key)

                try:
                    func_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning(f"工具参数解析失败: {tc.function.arguments}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "❌ 参数解析失败: 请提供有效的 JSON 参数",
                    })
                    continue

                # ---- 幽灵执行止损（审计 L1-B1）：此前被判「超时（结果未知）」的**有副作用**
                # 调用，拒绝原样重发。超时后它的线程仍在后台跑（可能已经成功），原样重试
                # 就是让同一个副作用做两遍。改成核对目标状态 / 改参数后重发即放行。
                _bg_why = _background_refuse_text(session_id, func_name, fp)
                if _bg_why:
                    log.warning(f"拒绝原样重发（该调用已转后台作业）: {func_name}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _bg_why,
                    })
                    continue
                if _is_refused_retry(session_id, func_name, fp):
                    log.warning(f"拒绝原样重试（此前超时、结果未知）: {func_name}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": (
                            "❌ 拒绝原样重试：这次调用此前被判「超时（结果未知）」，"
                            "而它有副作用 —— 后台线程可能已经把它做完了。\n"
                            "请先核对目标状态：\n"
                            "  · 已完成（文件已写出 / 命令已生效 / 提交已送达）→ 不必重做，继续下一步；\n"
                            "  · 确实没做成 → 调整参数后重新发起（哪怕只改一处也算重新审视过），"
                            "原样重发仍会被拒。"),
                    })
                    continue

                call_fp[tc.id] = fp
                audit_items.append((tc, func_name, func_args))

            # ---- 审批闸门（agent/approval.py）：整批原子 ----
            # 本批只要有一条没拿到批准，全部工具都不提交编排器 ——
            # 模型把它们放在同一批，说明这是同一个意图的整体；只做一半可能留下
            # 比全不做更糟的中间态。通过的原样进编排器，依赖分层与串并行仍由
            # 编排器决定，审批只控制"请求能不能传过去"。
            if audit_items:
                _dec = {}
                try:
                    from approval import gate_batch as _gate_batch
                    # 只路由：本批工具 + 整个会话交给引擎，审批上下文由引擎自己取。
                    _dec, _grants = _gate_batch(
                        [(t[0].id, t[1], t[2]) for t in audit_items],
                        session_id=session_id, cwd=str(PROJECT_ROOT),
                        conversation=conversation)
                except BaseException as _be:
                    # 等裁决时被 Ctrl+C / 停止按钮打断：整批补闭合响应，否则会话里
                    # 留下"有 tool_call 无 tool 响应"的断头，下次加载直接 400。
                    for _tcx, _nmx, _kwx in audit_items:
                        conversation.append({
                            "role": "tool", "tool_call_id": _tcx.id,
                            "content": "❌ 审批未完成（等待裁决时回合被中断），本批全部未执行。",
                        })
                    if isinstance(_be, Exception):
                        log.warning("审批引擎异常，本批按未获批处理: " + str(_be))
                    else:
                        raise
                for _tc, _nm, _kw in audit_items:
                    _ok, _why = _dec.get(_tc.id, (True, ""))
                    if not _ok:
                        log.warning("审批未通过: " + _nm)
                        conversation.append({
                            "role": "tool", "tool_call_id": _tc.id,
                            "content": _why or "❌ 审批未通过。",
                        })
                        continue
                    parsed_calls.append(ToolCall(
                        id=_tc.id, name=_nm, arguments=_kw, depends_on=[],
                    ))

            if parsed_calls:
                try:
                    batch_result = orchestrator.execute(parsed_calls)
                except Exception as e:
                    log.error(f"编排器执行异常: {e}")
                    batch_result = None

                if batch_result is None:
                    for tc in parsed_calls:
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "❌ 编排器执行失败",
                        })
                else:
                    result_map = {res.tool_call_id: res for res in batch_result.results}
                    for tc in parsed_calls:
                        res = result_map.get(tc.id)
                        if res is None:
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "❌ 编排器未返回该工具结果",
                            })
                        else:
                            content = str(res.result) if res.success else f"❌ {res.error}"
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": res.tool_call_id,
                                "content": content,
                            })
                            # 超时的**有副作用**调用记档：下次原样重发会被拒（幽灵执行止损）
                            if (not res.success) and (res.error or "").startswith("⏰ 超时"):
                                _remember_timeout(session_id, call_fp.get(tc.id))
                            # 转后台的调用：活已经交出去了，重发 = 副作用做两遍
                            _bj = (res.metadata or {}).get("background_job")
                            if _bj:
                                _remember_background(session_id, call_fp.get(tc.id), _bj)
                    if batch_result.is_interrupted:
                        log.warning("编排器被中断，部分工具未完成")

            # ---- 宿主扩展点：本批工具结果已落进 conversation，先给宿主钩子一次机会
            # （WebUI 的中期交互「用户交代」在此注入；无注册者时空表遍历，零开销）。
            _run_after_tools_hooks(conversation, session_id, log)

            save_session(session_id, conversation, log)

    except KeyboardInterrupt:
        # 主人按了停止 = **一起停**（决策 7）：后台作业一并取消并清空 ——
        # 否则会出现"我以为停了、它还在后台写盘"这种最坏情况。
        # 口径：cancelled 只代表结果被丢弃；只有支持取消令牌的工具会被真正打断。
        try:
            _o = _ORCHESTRATOR
            if _o is not None:
                # 一起停会**有界等待**强杀落地（返回台账），文案照台账说 —— 不许把
                # "信号发出去了"说成"都停了"：纯线程动作杀不掉，那部分要如实报。
                _stopped = _o.stop_all_jobs("主人点停止")
                if isinstance(_stopped, dict):
                    _n = int(_stopped.get("total", 0) or 0)
                    if _n:
                        log.warning("已一起停：%d 个后台作业被取消并清空（%d 个确认已收手，%d 个没在时限内收手）"
                                    % (_n, int(_stopped.get("settled", 0) or 0),
                                       int(_stopped.get("stuck", 0) or 0)))
                elif _stopped:
                    log.warning(f"已一起停：{_stopped} 个后台作业被取消并清空")
        except Exception as e:
            log.warning(f"取消后台作业失败（不阻断中断保存）: {type(e).__name__}: {e}")
        try:                                  # 先闭合断头，再保存
            close_dangling_tool_calls(conversation, tool_calls, log)
        except Exception as e:
            # 只兜真异常：tool_calls 已在循环前初始化，不再需要 `except NameError`
            # （那种写法会把被调函数内部的笔误也一起吞掉，见 B4）。
            # 闭合失败不阻断中断保存 —— 但必须留痕，别静默。
            log.warning(f"闭合断头工具调用失败（不阻断中断保存）: {type(e).__name__}: {e}")
        log.warning("Agent 被用户中断，正在保存当前进度...")
        try:
            save_session(session_id, conversation, log, status="interrupted")
        except Exception as e:
            log.error(f"中断保存失败: {e}")
        return {
            "content": "(被中断，未完成)",
            "conversation": conversation,
            "iterations": iteration,
            "interrupted": True
        }

    log.warning(f"Agent 达到最大迭代次数 {max_iterations}，强制终止")
    try:
        save_session(session_id, conversation, log, status="interrupted")
    except Exception as e:
        log.error(f"保存失败: {e}")
    return {
        "error": f"Agent 循环超过最大迭代次数 {max_iterations}",
        "conversation": conversation,
        "iterations": iteration
    }


# ========== 5. 提示词注入 ==========

def load_system_prompt(log: SessionLogger) -> Tuple[str, List[str]]:
    """
    组装系统提示（SOUL/AGENTS/USER/MEMORY/技能注册表 + 项目信息）——【仅新对话调用】。

    返回 (prompt_text, injected_sources)，injected_sources 记录本次实际读入的
    源文件（"注入 xxx.md: path"）。快照冻结（Freeze on Start）语义：
    main() 只在「无已保存快照 = 新对话」时调用本函数并注入；
    续聊同一对话时无条件复用已持久化的快照，不调用本函数（不读源文件、
    不 sync），源 .md 变更需开新对话才生效。
    """
    parts: List[str] = []
    injected: List[str] = []
    if SOUL_PATH.exists():
        try:
            with open(SOUL_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 SOUL.md: {SOUL_PATH}")
        except Exception as e:
            log.warning(f"加载 SOUL.md 失败: {e}")
    else:
        log.debug(f"SOUL.md 不存在: {SOUL_PATH}，跳过")

    if AGENTS_PATH.exists():
        try:
            with open(AGENTS_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 AGENTS.md: {AGENTS_PATH}")
        except Exception as e:
            log.warning(f"加载 AGENTS.md 失败: {e}")
    else:
        log.debug(f"AGENTS.md 不存在: {AGENTS_PATH}，跳过")

    if USER_PATH.exists():
        try:
            with open(USER_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 USER.md: {USER_PATH}")
        except Exception as e:
            log.warning(f"加载 USER.md 失败: {e}")
    else:
        log.debug(f"USER.md 不存在: {USER_PATH}，跳过")
    if MEMORY_PATH.exists():
        try:
            with open(MEMORY_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 MEMORY.md: {MEMORY_PATH}")
        except Exception as e:
            log.warning(f"加载 MEMORY.md 失败: {e}")
    else:
        log.debug(f"MEMORY.md 不存在: {MEMORY_PATH}，跳过")

    # 技能注册表：每会话启动同步一次（多增少删），随后把注册表全文作为
    # 「技能目录快照」注入上下文（会话内冻结；需要执行技能时按路径读正文）
    try:
        sync_result = sync_registry(
            skills_dir=SKILLS_DIR, registry_path=SKILL_REGISTRY_PATH
        )
        if sync_result.changed:
            log.info(f"SKILL_REGISTRY.md 已同步: {sync_result.summary()}")
        else:
            log.debug("SKILL_REGISTRY.md 无变化")
        if sync_result.issues:
            for issue in sync_result.issues:
                log.warning(f"技能扫描提示: {issue}")
        registry_block = build_injection_block(
            skills_dir=SKILLS_DIR, registry_path=SKILL_REGISTRY_PATH
        )
        if registry_block:
            parts.append(registry_block)
            injected.append(f"注入 SKILL_REGISTRY.md: {SKILL_REGISTRY_PATH}")
    except Exception as e:
        log.warning(f"SKILL_REGISTRY.md 加载失败: {e}")

    # MCP 服务站注册表：同一处、同一套"会话内冻结"语义（v2，见 docs/MCP设计.md §四）。
    # 只注入**注册表**（每 station 一行：名字/用途/工具数/开关）——**不含任何工具 schema**：
    # 一个 station 可能上百个工具，全量注入是纯浪费；模型要用时先 mcp_search 取 schema。
    # 冻结 ≠ 不热：mcp_search/mcp_call 每次实时读 station 文件夹，新加的 station 同会话就能用。
    try:
        if not mcp_station.mcp_enabled():
            log.info("MCP 总闸关闭（config.yaml 的 mcp.enabled=false），跳过注册表注入")
        else:
            mcp_sync_result = mcp_station.sync_registry()
            if mcp_sync_result.changed:
                log.info(f"MCP_REGISTRY.md 已同步: {mcp_sync_result.summary()}")
            else:
                log.debug("MCP_REGISTRY.md 无变化")
            for issue in (mcp_sync_result.issues or []):
                log.warning(f"MCP station 提示: {issue}")
            mcp_block = mcp_station.build_injection_block()
            if mcp_block:
                parts.append(mcp_block)
                injected.append("注入 MCP_REGISTRY.md: %s" % mcp_station.registry_path())
            elif mcp_sync_result.stations:
                log.debug("MCP 注册表为空（没有可见的 station），不注入")
    except Exception as e:
        log.warning(f"MCP_REGISTRY.md 加载失败: {e}")

    # 集成包注册表：同一处、同一套"会话内冻结"语义（设计见 agent_integration_packs/README.md）。
    # 只注入**注册表**（每包一行：包名/用途/说明书路径）——**不含包内工具清单**：
    # 包里有什么、怎么用，全在该包的 PACK.md 里，模型要用时按需读（渐进式披露的下一层）。
    # 冻结 ≠ 不热：新建的包在盘上立刻存在（read_file 就能读），只是"注册表那一段"要等新对话刷新。
    try:
        pack_sync = integration_pack.sync_registry()
        if pack_sync.changed:
            log.info(f"PACK_REGISTRY.md 已同步: {pack_sync.summary()}")
        else:
            log.debug("PACK_REGISTRY.md 无变化")
        for issue in (pack_sync.issues or []):
            log.warning(f"集成包提示: {issue}")
        pack_block = integration_pack.build_injection_block()
        if pack_block:
            parts.append(pack_block)
            injected.append("注入 PACK_REGISTRY.md: %s" % integration_pack.registry_path())
    except Exception as e:
        log.warning(f"PACK_REGISTRY.md 加载失败: {e}")

    if not parts:
        parts.append("你是一个智能助手，可以调用工具来完成复杂任务。")
        injected.append("注入 内置默认系统提示（未找到任何源文件）")

    base_prompt = "\n\n---\n\n".join(parts)

    project_info = f"""

    ## 项目信息（由系统自动注入，无需用户说明）
    - 项目根目录：`{PROJECT_ROOT}`
    - 工作空间：`{WORKSPACE_ROOT}`
    - 所有文件操作都相对于项目根目录。除非用户明确指定绝对路径，否则不要访问项目根目录以外的文件。
    - 如果你需要读取或操作文件，请基于上述路径进行。
    - 知识库目录：`{KNOWLEDGE_BASE_DIR}`
    - 会话存储目录：`{WORKING_MEMORY_DIR}`
    """

    return base_prompt + project_info, injected


# ========== 6. 主程序入口 ==========

def main():
    session_id = get_session_id()

    log = SessionLogger(session_id)
    log.info("会话启动", session_id=session_id)

    history, saved_snapshot = load_session(session_id, log)

    # 上下文管理器：接上本会话日志 + 清理上次写盘中断留下的临时文件
    cm = get_context_manager(log)
    _n_orphan = cm.clean_orphans()
    if _n_orphan:
        log.warning(f"清理上下文视图孤儿临时文件 {_n_orphan} 个")

    # 快照冻结（Freeze on Start）：同一对话的 system prompt 在对话开始时
    # 构建一次并随会话持久化；此后每次续聊【无条件复用】保存的快照——
    # 不读源 .md、不 sync 注册表、不做任何比较。任务中 memory.md 被多次
    # 修改、或新增了技能，本对话都不会重新加载；要生效必须开新对话。
    if saved_snapshot is not None:
        system_prompt = saved_snapshot
        log.info("语境快照沿用（快照冻结：本对话不随 .md 变更重新加载）")
        print("🧊 语境快照沿用")
    else:
        # 新对话：读取全部源文件组装快照，注入后即冻结
        system_prompt, injected_sources = load_system_prompt(log)
        for src in injected_sources:
            log.info(src)
        print(f"🧊 新对话：语境快照已注入并冻结（{len(injected_sources)} 个源文件）")

    messages = [{"role": "system", "content": system_prompt}] + history

    print("\n" + "=" * 60)
    print(f"💬 会话: {session_id}")
    print(f"📝 历史消息: {len(history)} 条")
    print("输入 'exit' 或 'quit' 退出并保存")
    print("=" * 60 + "\n")

    exit_status = "complete"
    try:
        while True:
            user_input = input("👤 你: ").strip()
            if user_input.lower() in ['exit', 'quit', 'q']:
                break
            if not user_input:
                continue

            messages.append({"role": "user", "content": user_input})
            log.info(f"用户输入: {user_input[:100]}")

            result = call_agent_with_tools(messages, session_id, log)

            if "error" in result:
                print(f"\n❌ 错误: {result['error']}\n")
                log.error(f"Agent 执行错误: {result['error']}")
                messages.pop()
                continue

            messages = result["conversation"]
            print(f"\n🤖 Agent: {result['content']}\n")

    except KeyboardInterrupt:
        print("\n\n⚠️ 检测到 Ctrl+C，正在保存会话...")
        exit_status = "interrupted"
    finally:
        save_session(session_id, messages, log, status=exit_status)
        log.info(f"会话结束，状态: {exit_status}")
        shutdown_orchestrator()
        log.close()
        print(f"✅ 会话已保存: {session_id} (状态: {exit_status})")
        print("👋 下次启动时输入相同会话ID即可继续对话")


if __name__ == "__main__":
    main()