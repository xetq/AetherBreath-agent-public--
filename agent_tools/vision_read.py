# agent_tools/vision_read.py
# -*- coding: utf-8 -*-
"""vision_read —— 独立的「看图」通道（代调当前配置的 LLM 视觉通道）。

与「附件图片块」的分工（2026-09-22 起明确）
-------------------------------------------
· **附件通道（首选）**：图片经 attachments → 咽喉 `materialize_attachments` →
  content 数组里的 `image_url` 块，模型**原生看见**。这是主路径。
· **本工具（兜底）**：拿不到附件通道时（纯文本模型 `vision=false`、或只想单独
  问一张图），由工具代调视觉通道，把**文字描述**带回上下文。

⚠️ 本工具**不再带 OCR**（2026-09-22 主人明确「完全不需要 OCR 了」）：
   OCR 依赖与全部残留已移除。需要精确文字时走主路径 —— 让模型直接看原图，
   不再经过「OCR 转写」这层中介。

⚠️ 判据纪律（实测得来，别丢）：视觉通道对**小字与数字**不可靠 ——
   同一张力扣截图，模型曾把「简单 6/1055」读成「67/855」，口气一样自信。
   精确数字请以原始画面或可核对的字面为准。

返回统一形状（与其它工具一致）：
    {"success": bool, "result": str|None, "error": str|None}
"""

import base64
import io
import os
import sys
from typing import Any, Dict, Optional, Tuple

# ========== 可调参数 ==========
_MAX_SIDE = 1400            # 送模型前最长边上限（控 token）
_JPEG_QUALITY = 88
_MAX_TOKENS = 1200          # 模型回复上限
_VISION_TIMEOUT = 90        # LLM 请求超时（秒）
_DEFAULT_QUESTION = (
    "请详细描述这张图片的内容：这是什么界面或场景？"
    "有哪些关键的文字、数字和图形元素？"
)

_AB_CACHE = None            # AB 本体模块缓存


def _slog(logger, msg: str = "", level: str = "info") -> None:
    """安全日志：日志绝不能让工具崩掉。"""
    try:
        if logger:
            getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


def _find_ab_module():
    """定位 AB 本体模块（agent/agent.py）。

    bridge 用 `import agent as ab_agent` 装载它，所以 sys.modules 里的键是
    "agent"；也兼容 "agent.agent" 形态。判据用 compose_chat_kwargs —— 那是
    agent.py 独有的函数，比只看 client 更准。
    **只读 sys.modules，不 import、不读 .env、不新建凭据。**
    """
    global _AB_CACHE
    if _AB_CACHE is not None:
        return _AB_CACHE
    for key in ("agent", "agent.agent"):
        mod = sys.modules.get(key)
        if mod is not None and hasattr(mod, "client") and hasattr(mod, "compose_chat_kwargs"):
            _AB_CACHE = mod
            return mod
    for _, mod in list(sys.modules.items()):        # 兜底：全局找
        try:
            if hasattr(mod, "compose_chat_kwargs") and hasattr(mod, "client"):
                _AB_CACHE = mod
                return mod
        except Exception:
            continue
    return None


def _resolve_path(path: str, ab) -> str:
    """相对路径按项目根解析（AB 的既有约定）。"""
    p = str(path or "").strip().strip('"').strip("'")
    if not p:
        raise ValueError("path 为空")
    if os.path.isabs(p):
        return os.path.normpath(p)
    base = getattr(ab, "PROJECT_ROOT", None) if ab is not None else None
    base = str(base) if base else os.getcwd()
    return os.path.normpath(os.path.join(base, p))


def _prepare_image(path: str) -> Tuple[bytes, Tuple[int, int], Tuple[int, int]]:
    """读图 -> RGB -> 按上限缩放 -> JPEG。返回 (jpeg_bytes, 原始尺寸, 送模型尺寸)。"""
    from PIL import Image
    im = Image.open(path)
    origin = im.size
    if im.mode != "RGB":
        im = im.convert("RGB")
    scale = min(1.0, float(_MAX_SIDE) / float(max(im.size) or 1))
    if scale < 1.0:
        im = im.resize((max(1, int(im.size[0] * scale)),
                        max(1, int(im.size[1] * scale))))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return buf.getvalue(), origin, im.size


def _att_root_rel(ab) -> str:
    """附件落盘根（相对项目根）。与网关侧 attachments.py 同源、同优先级：
    paths.attachments > attachments.root > 默认 agent_workspace/attachments。

    为什么必须读配置而不是写死：网关（前端上传）与本工具落到**同一个**根目录
    下 —— 写死会让改配置后两边各写各的，会话里的 path 还在、文件却找不到。
    """
    default = "agent_workspace/attachments"
    try:
        cfg = getattr(ab, "CONFIG", None) or {}
        att = cfg.get("attachments") or {}
        paths = cfg.get("paths") or {}
        p = str(paths.get("attachments") or "").strip()
        r = str(att.get("root") or "").strip()
        return p or r or default
    except Exception:
        return default



def _dump_image(jpeg: bytes, ab) -> Optional[str]:
    """把规范化后的 JPEG 落到项目内附件目录，返回相对项目根的 posix 路径。

    为什么不把 base64 直接塞进返回值：会话文件存的是**消息原文**。
    若 base64 进了 tool 消息的 content，它会永久留在会话 JSON 里，
    且每轮历史重发都驮着它。落盘后会话里只留一个短路径。
    """
    try:
        import time
        import uuid
        from pathlib import Path
        root_p = Path(str(getattr(ab, "PROJECT_ROOT", "."))).resolve()
        dest_dir = root_p / _att_root_rel(ab) / "_tool_vision"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / ("%s_%s.jpg" % (time.strftime("%Y%m%d"), uuid.uuid4().hex[:10]))
        dest.write_bytes(jpeg)
        return dest.resolve().relative_to(root_p).as_posix()
    except Exception:
        return None


def vision_read(path: str = "", question: str = "", mode: str = "attach", logger=None) -> dict:
    """看一张图。

    mode="attach"（默认）—— 把图规范化后附加到**本工具结果**里，由咽喉
        （compose_chat_kwargs -> materialize_attachments）物化成 image_url 块。
        图片就落在 tool 消息内，**不伪装成 user 消息**；主模型原生看见原图，
        后续轮次可回看（受 attachments.keep_recent_images 折叠控量）。
    mode="describe" —— 旧行为：旁路调一次视觉通道，只把**文字描述**带回来。
        模型未启用视觉、或只想收敛 token 时用。
    """
    _slog(logger, "vision_read 开始: path=%s mode=%s" % (path, mode))
    try:
        ab = _find_ab_module()
        if ab is None:
            raise RuntimeError(
                "拿不到 AB 本体模块（agent 未在运行）。本工具必须在 agent 进程内被调用。")
        img_path = _resolve_path(path, ab)
        if not os.path.isfile(img_path):
            raise FileNotFoundError("图片不存在: %s" % img_path)

        m = str(mode or "attach").strip().lower()
        if m in ("attach", "image", "send"):
            jpeg, origin, sent = _prepare_image(img_path)
            rel = _dump_image(jpeg, ab)
            if not rel:
                raise RuntimeError("图片落盘失败（附件目录不可写？）")
            base_n = os.path.basename(img_path)
            result = """【附件】name=%s | kind=image | path=%s
（图片已附加在本工具结果中：%dx%d -> 送模型 %dx%d，JPEG %d bytes。主模型可直接查看原图。）""" % (
                base_n, rel, origin[0], origin[1], sent[0], sent[1], len(jpeg))
            _slog(logger, "vision_read attach 完成: %s -> %s" % (base_n, rel))
            return {"success": True, "result": result, "error": None}

        # ---- describe：旧行为（独立旁路请求，只把文字描述带回来）----
        jpeg, origin, sent = _prepare_image(img_path)
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        q = (question or "").strip() or _DEFAULT_QUESTION
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": q},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        resp = ab.client.chat.completions.create(
            model=ab.MODEL_NAME,
            messages=messages,
            stream=False,
            max_tokens=_MAX_TOKENS,
            timeout=_VISION_TIMEOUT,
        )
        try:
            desc = resp.choices[0].message.content or ""
        except Exception:
            desc = "(模型未返回可读文本)"
        img_tokens = None
        try:
            ptd = getattr(getattr(resp, "usage", None), "prompt_tokens_details", None)
            img_tokens = getattr(ptd, "image_tokens", None) if ptd is not None else None
        except Exception:
            img_tokens = None
        result = """===== vision_read (describe) =====
图片: %s
尺寸: %dx%d -> 送模型 %dx%d | JPEG %d bytes
视觉通道: model=%s | image_tokens=%s

【视觉描述】（数字与小字可能不准；要精确字面请用 mode=attach 让模型直接看原图）
%s""" % (img_path, origin[0], origin[1], sent[0], sent[1], len(jpeg),
         getattr(ab, "MODEL_NAME", "?"), img_tokens, (desc.strip() or "(空)"))
        _slog(logger, "vision_read describe 完成: %d 字符" % len(result))
        return {"success": True, "result": result, "error": None}

    except Exception as e:
        error_msg = "%s: %s" % (type(e).__name__, str(e))
        _slog(logger, "vision_read 失败: %s" % error_msg, level="error")
        return {"success": False, "result": None, "error": error_msg}


vision_read_schema: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vision_read",
        "description": (
            "看一张本地图片。默认（mode=attach）把图片**直接附加到本工具结果**里，"
            "主模型原生看见原图，且后续轮次仍可回看 —— 这是推荐用法。"
            "mode=describe 时改为只返回一段文字描述（模型未启用视觉、或想收敛 token 时用）。"
            "注意：视觉通道对小字与数字不可靠，精确字面请以原图为准。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "图片路径（绝对路径，或相对项目根的路径）",
                },
                "question": {
                    "type": "string",
                    "description": "仅 mode=describe 时有效：想问什么；attach 模式忽略此参数",
                },
                "mode": {
                    "type": "string",
                    "enum": ["attach", "describe"],
                    "description": "attach=把图片附加进上下文让主模型直接看（默认）；describe=只返回文字描述",
                },
            },
            "required": ["path"],
        },
    },
}

__all__ = ["vision_read", "vision_read_schema"]
