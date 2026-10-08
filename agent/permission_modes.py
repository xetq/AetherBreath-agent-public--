# -*- coding: utf-8 -*-
"""会话级权限模式：仅读 / 工作区 / 普通 / 完全。

本模块只放**策略与状态**，不放判定逻辑；判定接在 `agent/approval.py` 的
`_apply_permission_mode()` 上（它需要词法层的动作点，那是引擎的领域知识）。

四档是什么：

  · `readonly` 仅读 —— 只能「知晓」不能「操作」。读文件/读网页/跑只读命令放行；
    任何写/改/移/删、带载荷外发、编码执行一律**直接拒**（不弹卡）。
  · `workspace` 工作区 —— `agent_workspace/` 内自由读写、免审批；区外只读：
    区外的写/改/移/删直接拒（外面只允许**拷贝进来**），带载荷外发、目标不可
    判定的执行也直接拒。其余规范（如 mcp.spawn）照旧弹卡。
  · `normal` 普通 —— 走完整的初始审批（优化前的老行为）。
  · `full` 完全 —— 初始审批全部自动同意；**绝对禁区（DECIDE_BLOCK）仍然硬拦**。

为什么 BLOCK 在完全模式下也拦：绝对禁区挡的是「手滑一次就没有终端了」那类操作，
把它外包给系统提示词等于把最后一道机械防线换成一句劝告。

模式是**会话级**的，存在会话 json 的 `permission_mode` 字段里；本模块的内存表
只是引擎侧缓存（引擎按 session_id 服务多个会话，与 `_GRANTS` 同一套纪律）。

`AETHER_AUDIT_MODE` 不再从 .env 读 —— 它现在由模式**派生**（见 AUDIT_MODE）。
那条 `AETHER_AUDIT_MODE=off` 一行关闸的后门随之消失。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Tuple

# ============================================================
# 1. 四档定义
# ============================================================
MODE_READONLY = "readonly"
MODE_WORKSPACE = "workspace"
MODE_NORMAL = "normal"
MODE_FULL = "full"

MODES: Tuple[str, ...] = (MODE_READONLY, MODE_WORKSPACE, MODE_NORMAL, MODE_FULL)
DEFAULT_MODE = MODE_NORMAL

LABELS: Dict[str, str] = {
    MODE_READONLY: "仅读",
    MODE_WORKSPACE: "工作区",
    MODE_NORMAL: "普通",
    MODE_FULL: "完全",
}

ICONS: Dict[str, str] = {
    MODE_READONLY: "🔒",
    MODE_WORKSPACE: "📁",
    MODE_NORMAL: "🛡️",
    MODE_FULL: "🔓",
}

# 一句话状态：**界面那块与模型那行共用同一份文案**（两处各写一份必然漂移）。
# 措辞要求：说清"因此什么被禁 / 什么被允许"，且足够短（它每轮请求都会带上）。
STATUS: Dict[str, str] = {
    MODE_READONLY: "只能读取；写入/删除/外发一律直接拒绝",
    MODE_WORKSPACE: "agent_workspace/ 内自由；区外只读，越界直接拒绝",
    MODE_NORMAL: "按初始审批规则弹卡确认",
    MODE_FULL: "初始审批自动同意；绝对禁区仍然硬拦",
}


def status_of(mode: str) -> str:
    return STATUS.get(normalize(mode), STATUS[DEFAULT_MODE])


# 「谁在说话」的标注（2026-10）：这条通知走 **user 通道**，但它**不是主人说的话** ——
# 是审批系统按权限模式自动注入的（主人原话：“走的是 user 通道，你会误以为是我发的”）。
# 注入侧 `agent._with_mode_notice` 在正文前加上它，模型才知道该把“权限被切成了 X”
# 读成系统事件、而不是主人的原话。
# 只加在**注入侧**、不改 `notice_text` 本体：那个函数管“文案生成”（格式被测试与
# 文档引用），而前缀表达的是“这条消息从哪来”，属于通道的事。
INJECT_PREFIX = "[审批系统自动注入]: "


def notice_text(previous: str, current: str) -> str:
    """切换时要发给模型的那一行（主人 2026-10 定的口径）：

        `旧档权限->新档权限：一句话内容`

    三条约束都来自实测反馈：
      · **只在切换后发一次**（不是每条消息都发）—— 每轮都带会让每次对话都多一行；
      · 短 —— 只有一句状态，不写"谁切的、什么时候切的"；
      · 说清"现在什么被禁 / 什么被允许"，模型才知道自己能动什么。
    """
    old = LABELS.get(previous, LABELS[DEFAULT_MODE]) if previous else LABELS[DEFAULT_MODE]
    return "%s权限->%s权限：%s" % (old, label(current), status_of(current))


# 一句话简化描述（下拉菜单里展开的那段，比 STATUS 长）
DESCRIPTIONS: Dict[str, str] = {
    MODE_READONLY:
        "权限模式：仅读。只能读取与了解信息（读文件、读网页、跑只读命令）。",
    MODE_WORKSPACE:
        "权限模式：工作区。agent_workspace/ 内（以及 agent_memory/long_memory/MEMORY.md）"
        "可自由读写、无需审批；工作区之外只读 —— 区外的写/改/移/删、带载荷外发数据、"
        "以及目标落在工作区之外的执行都会被直接拒绝（外部文件只允许**拷贝进**工作区"
        "再操作），不会弹审批卡。",
    MODE_NORMAL:
        "权限模式：普通。工作区内外都能操作，按既有的初始审批规则弹卡确认。",
    MODE_FULL:
        "权限模式：完全。初始审批全部自动同意（不再弹卡），但绝对禁区仍然硬拦。",
}

# 模式 -> 旧的审计档（AETHER_AUDIT_MODE）
#   仅读  → strict：词法层退回全文扫，宁误拦不漏拦（最严的一档就该这样）
#   工作区/普通 → smart：默认
#   完全  → off：引擎不再逐项申请审批（但 BLOCK 在完全模式下**仍然**拦）
AUDIT_MODE: Dict[str, str] = {
    MODE_READONLY: "strict",
    MODE_WORKSPACE: "smart",
    MODE_NORMAL: "smart",
    MODE_FULL: "off",
}

# 模式拒绝时回给模型的措辞前缀（与 WHY_* 同一族，便于分辨「被模式拒」与「被人拒」）
MODE_DENY_HINT = "这是权限模式的硬限制，不是主人拒绝了你。不要绕路重试：请停下来向主人说明你需要什么权限。"


def normalize(value: Any) -> str:
    """把外部传入（前端/会话文件/环境）的模式名收敛到四档之一；不认识就是默认档。"""
    m = str(value or "").strip().lower()
    return m if m in MODES else DEFAULT_MODE


def label(mode: str) -> str:
    return LABELS.get(normalize(mode), LABELS[DEFAULT_MODE])


def icon(mode: str) -> str:
    return ICONS.get(normalize(mode), ICONS[DEFAULT_MODE])


def describe(mode: str) -> str:
    return DESCRIPTIONS.get(normalize(mode), DESCRIPTIONS[DEFAULT_MODE])


def audit_mode(mode: str) -> str:
    return AUDIT_MODE.get(normalize(mode), "smart")


def catalog() -> List[Dict[str, Any]]:
    """给界面的可选清单（顺序即下拉顺序：从最紧到最松）。

    每项带 `status`（一句话状态）—— 界面那块状态块直接用它，与模型那行同源。
    """
    return [{"mode": m, "label": LABELS[m], "icon": ICONS[m],
             "status": STATUS[m], "description": DESCRIPTIONS[m]} for m in MODES]


# ============================================================
# 2. 会话级状态（引擎侧缓存）
# ============================================================
_lock = threading.RLock()
_current: Dict[str, str] = {}
# 待告知模型的换档通知（每会话最多一条，取走即清）。
# 口径（主人 2026-10）：**只有真的换档**（切到不同的档）才记一条，
# 由 `agent._with_mode_notice` 在**下一次请求**里以 **user 通道**追加一行
# `旧权限->新权限：一句话`，取走之后就自然消失。
# 为什么不是"每轮都带"：那会让每次发送消息都多一行、白烧 token（他实测反馈过）；
# 为什么不是"写进历史"：切几次就堆几条（更早一版的毛病）。
_notice: Dict[str, str] = {}


def get(session_id: str = "") -> str:
    """取某会话当前模式；没有记录就是默认档（普通）。"""
    with _lock:
        return _current.get(session_id or "") or DEFAULT_MODE


def take_notice(session_id: str = "") -> str:
    """取走"待告知模型"的切换通知，取一次即清；没有就返回空串。"""
    with _lock:
        return _notice.pop(session_id or "", "")


def tracked(session_id: str = "") -> bool:
    """本进程里**已经有**这个会话的模式记录吗？

    `load_session` 的"读回"要用它来决定该不该写：文件是**跨进程/重启**的真源，
    但本进程一旦处理过该会话（切过档、跑过回合），内存里那一份就是更新的 ——
    无条件读回会把刚设好的新档盖成文件里的旧档（2026-10 实测踩过：切档卡死在"仅读"）。
    """
    with _lock:
        return (session_id or "") in _current


def set(session_id: str, mode: str) -> str:
    """写某会话的模式，返回生效值（已收敛）。不负责持久化与落盘 —— 那是调用方的事。

    **只有真的换了档才记下"待告知模型"的那一行**（切到同一个档不算切换）：由
    `agent._with_mode_notice` 在**下一次请求**里取走、以 user 通道追加一行
    `旧权限->新权限：一句话`，之后不再出现（不每轮带、也不写进历史）。
    """
    m = normalize(mode)
    sid = session_id or ""
    with _lock:
        prev = _current.get(sid)
        _current[sid] = m
        if prev is None and m == DEFAULT_MODE:
            pass                      # 首次落地且就是默认档：没有"变化"可报
        elif prev != m:
            _notice[sid] = notice_text(prev or DEFAULT_MODE, m)
    return m


def forget(session_id: str) -> None:
    with _lock:
        _current.pop(session_id or "", None)
        _notice.pop(session_id or "", None)


def reset() -> None:
    """测试用：清空全部会话记录。"""
    with _lock:
        _current.clear()
        _notice.clear()


# ============================================================
# 3. 「工作区」的边界
# ============================================================
def _project_root() -> str:
    # 本文件在 agent/ 下，项目根 = 它上一层的上一层
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(p: str) -> str:
    """归一：小写、正斜杠、去尾斜杠（与 approval.unify 的口径一致）。"""
    s = str(p or "").strip().replace("\\", "/").lower()
    while len(s) > 1 and s.endswith("/"):
        s = s[:-1]
    return s


def workspace_dirs() -> List[str]:
    """工作区内**可以自由读写**的目录（运行时算，不缓存 —— 便于测试改根）。"""
    return [_norm(os.path.join(_project_root(), "agent_workspace"))]


def workspace_files() -> List[str]:
    """工作区内额外放行的**单个文件**：AB 自己记的长记忆。

    只加这一个：其余 `agent_memory/` 内容由系统自动维护，不该让 agent 直接改。
    """
    return [_norm(os.path.join(_project_root(), "agent_memory", "long_memory", "MEMORY.md"))]


def in_workspace(path: str) -> bool:
    """路径（已归一或未归一都行）是否落在工作区内。

    注意两件刻意的取舍：
      · **工作区文件夹自身不算「内」** —— 删/改名整个 agent_workspace 是「掀桌子」，
        与 `rm -rf agent_workspace/子目录` 不是一回事，由调用方用 `allow_root=False` 区分；
      · 用 `==` 或 `前缀 + "/"` 两种匹配，避免 `agent_workspace2/` 这种同前缀误判。
    """
    p = _norm(path)
    if not p:
        return False
    for f in workspace_files():
        if p == f:
            return True
    for d in workspace_dirs():
        if p.startswith(d + "/"):
            return True
    return False


def is_workspace_root(path: str) -> bool:
    """是不是工作区**文件夹本身**（删/改名它 = 掀桌子）。"""
    p = _norm(path)
    return bool(p) and p in workspace_dirs()


# ============================================================
# 4. 切换通知（**已废弃为历史遗留**，只用于识别旧会话里的旧消息）
# ============================================================
# 2026-10 主人两次改口径，现在的做法是：
#   ① 不再往会话历史里追加切换通知（切几次堆几条、白占上下文）；
#   ② 也**不是**每轮请求都带一行"当前状态"（那是中间版本，会让每次发送都多一行）；
#   ③ 只在**真的换了档**那一次，由 `agent._with_mode_notice` 在**下一次请求**里
#      以 **user 通道**追加一行 `旧权限->新权限：一句话`（见 `notice_text`），取走即清。
# 所以下面这两个符号的唯一用途是**认得旧会话里已经落盘的那些通知**
# （标题过滤、轮次计数、前端渲染都要认出来）。新代码不要再用它写消息。
NOTICE_PREFIX = "🔐 [权限模式] "


def switch_notice(previous: str, current: str) -> str:
    """【遗留】旧版切换通知的正文格式；只为解析/测试旧数据保留。"""
    return (NOTICE_PREFIX + "主人把本会话的权限模式从「%s」切成了「%s」。%s"
            % (label(previous), label(current), describe(current)))
