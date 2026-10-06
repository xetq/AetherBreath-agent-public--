# agent_tools/create_tool.py
"""
元工具：让 Agent 动态创建并注册新工具。
遵循 AetherBreath 日志标准（自动注入 logger）。
"""
import re
import json
from pathlib import Path
from typing import Dict, Any

# ========== 路径推导（GitHub 友好） ==========
_TOOL_DIR = Path(__file__).parent.absolute()
PROJECT_ROOT = _TOOL_DIR.parent


def _slog(logger, msg: str = "", level: str = "info") -> None:
    """安全日志：日志绝不能把工具本身搞崩（审计 B6）。"""
    try:
        if logger:
            getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


def _validate_name(name: str) -> bool:
    """工具名只能包含字母、数字、下划线，且不能以数字开头"""
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def _update_init_py(tool_name: str) -> str:
    """
    自动将新工具注册到 agent_tools/__init__.py
    返回修改说明
    """
    init_path = _TOOL_DIR / "__init__.py"
    if not init_path.exists():
        return "⚠️ __init__.py 不存在，请手动注册"

    with open(init_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # 1. 检查是否已存在
    import_statement = f"from .{tool_name} import {tool_name}, {tool_name}_schema"
    for line in lines:
        if f"from .{tool_name}" in line:
            return f"⏭️ 工具 '{tool_name}' 已注册，跳过修改"

    # 2. 找 import 区域末尾（在 AVAILABLE_TOOLS 之前插入）
    insert_idx = -1
    for i, line in enumerate(lines):
        if line.strip().startswith("AVAILABLE_TOOLS = {"):
            insert_idx = i
            break

    if insert_idx == -1:
        return "⚠️ 未找到 AVAILABLE_TOOLS 定义，请手动添加 import"

    # 在 AVAILABLE_TOOLS 之前插入 import
    lines.insert(insert_idx, import_statement + "\n")

    # 3. 更新 AVAILABLE_TOOLS 字典
    tool_entry = f'    "{tool_name}": {tool_name},\n'
    for i, line in enumerate(lines):
        if line.strip().startswith('"') and ":" in line:
            continue
        if line.strip().startswith("}"):
            lines.insert(i, tool_entry)
            break

    # 4. 更新 __all__ 列表
    for i, line in enumerate(lines):
        if line.strip().startswith("__all__ = ["):
            # 找到列表末尾
            j = i
            while j < len(lines) and "]" not in lines[j]:
                j += 1
            if j < len(lines):
                # 在 ] 前插入
                lines.insert(j, f'    "{tool_name}",\n')
            break

    # 写入
    # 3.6 update TOOLS_SCHEMA list so the new tool schema is exposed to the LLM
    for i, line in enumerate(lines):
        if line.strip().startswith("TOOLS_SCHEMA = ["):
            j = i
            while j < len(lines) and not lines[j].strip().startswith("]"):
                j += 1
            if j < len(lines):
                lines.insert(j, "    " + tool_name + "_schema," + chr(10))
            break


    with open(init_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    return f"✅ 已自动注册到 __init__.py"


def create_tool(
    name: str,
    description: str,
    parameters: Dict[str, Any],
    implementation_code: str,
    logger=None,
) -> dict:
    """
    创建一个新的 Agent 工具。

    Args:
        name: 工具名称（仅字母、数字、下划线，如 "get_weather"）
        description: 工具功能描述（会写入 docstring 和 schema）
        parameters: JSON Schema 格式的参数定义
                   如 {"city": {"type": "string", "description": "城市名"}}
        implementation_code: 核心逻辑的 Python 代码（字符串）
                             代码中可使用 kwargs 获取参数，最后赋值 result 变量
        logger: 日志实例（由编排器自动注入）

    Returns:
        dict: {"success": bool, "file_path": str, "message": str}
    """
    # ===== 记录开始 =====
    _slog(logger, f"开始创建工具: {name}")

    # ===== 1. 验证名称 =====
    if not _validate_name(name):
        error = f"工具名 '{name}' 不合法，仅支持字母、数字、下划线，且不以数字开头"
        _slog(logger, error, level="error")
        return {"success": False, "file_path": None, "message": error}

    # ===== 2. 检查文件是否已存在 =====
    target_file = _TOOL_DIR / f"{name}.py"
    if target_file.exists():
        error = f"文件 {target_file} 已存在，请先删除或使用其他名称"
        _slog(logger, error, level="error")
        return {"success": False, "file_path": str(target_file), "message": error}

    # ===== 3. 提取参数名称列表 =====
    param_names = list(parameters.keys())
    param_defs = []
    param_doc = []
    for pname, pdef in parameters.items():
        ptype = pdef.get("type", "str")
        pdesc = pdef.get("description", "")
        default = pdef.get("default")
        if default is not None:
            param_defs.append(f"{pname}={repr(default)}")
        else:
            param_defs.append(pname)
        param_doc.append(f"        {pname}: {ptype} - {pdesc}")

    # ===== 4. 生成 Python 文件内容 =====
    # 安全缩进处理
    code_lines = implementation_code.strip().split('\n')
    indented_code = '\n'.join(['        ' + line for line in code_lines])

    # ---- 安全日志助手（审计 B6）----
    # 旧模板生成的是 `logger.info(f"xxx 开始", city={ city })` —— 那个 `{ city }`
    # 是**独立的 set 字面量参数**，不是 f-string 插值。实测后果：
    #   · 传标准 logging.Logger → `TypeError: Logger._log() got an unexpected
    #     keyword argument 'location'`，而且该调用在 try 之外，异常直穿函数；
    #   · 传本项目注入的 SessionLogger（签名能吃 **extra）→ 不崩，但 set 过不了
    #     json.dumps，日志写入被静默吞掉（每次调用丢一条日志）。
    # 现改为：消息**整条拼好**再传，且所有日志走 _slog() —— 日志永远不能让工具崩。
    _log_args = ", ".join("%s={%s}" % (p, p) for p in param_names)
    # ⚠️ 这三个片段**不带缩进前缀** —— 模板里已经写好缩进（`    {_log_begin}` 等），
    #    两边都带会叠加成 IndentationError（首次交付时真的踩到了：生成的文件里
    #    第一处 _slog 变成 8 空格，第 33 行 unexpected indent）。
    _log_begin = ('_slog(logger, f"%s 开始: %s")' % (name, _log_args)
                  if _log_args else '_slog(logger, f"%s 开始")' % name)
    _log_done = '_slog(logger, f"%s 完成: " + str(result)[:200])' % name
    _log_fail = '_slog(logger, f"%s 失败: {error_msg}", level="error")' % name

    file_content = f'''# agent_tools/{name}.py
"""
{description}
自动生成于 AetherBreath create_tool。
"""

from typing import Any, Dict


def _slog(logger, msg: str = "", level: str = "info") -> None:
    """安全日志：logger 的实现各式各样，日志绝不能让工具崩掉。"""
    try:
        if logger:
            getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


def {name}(
    {', '.join(param_defs)},
    logger=None,
) -> dict:
    """
    {description}

    Args:
{chr(10).join(param_doc)}
        logger: 日志实例（由编排器自动注入）

    Returns:
        dict: {{"success": bool, "result": Any, "error": str|None}}
    """
    {_log_begin}

    try:
        # ===== 用户实现代码 =====
{indented_code}
        # ===== 结束 =====
        {_log_done}
        return {{"success": True, "result": result, "error": None}}
    except Exception as e:
        error_msg = f"{{type(e).__name__}}: {{str(e)}}"
        {_log_fail}
        return {{"success": False, "result": None, "error": error_msg}}


{name}_schema = {{
    "type": "function",
    "function": {{
        "name": "{name}",
        "description": "{description}",
        "parameters": {{
            "type": "object",
            "properties": {json.dumps(parameters, ensure_ascii=False, indent=4)},
            "required": {list(parameters.keys())}
        }}
    }}
}}

__all__ = ["{name}", "{name}_schema"]
'''

    # ===== 5. 写入文件 =====
    try:
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(file_content)
        _slog(logger, f"工具文件已创建: {target_file}")
    except Exception as e:
        error = f"写入文件失败: {e}"
        _slog(logger, error, level="error")
        return {"success": False, "file_path": str(target_file), "message": error}

    # ===== 6. 自动注册到 __init__.py =====
    register_msg = _update_init_py(name)
    _slog(logger, register_msg)

    # ===== 7. 返回结果 =====
    message = (
        f"✅ 工具 '{name}' 创建成功！\n"
        f"📁 文件: {target_file}\n"
        f"{register_msg}\n"
        f"🔄 请重启 Agent 进程以加载新工具。"
    )

    return {
        "success": True,
        "file_path": str(target_file),
        "message": message,
        "register_status": register_msg,
    }


create_tool_schema = {
    "type": "function",
    "function": {
        "name": "create_tool",
        "description": (
            "动态创建并注册一个新的 Agent 工具。"
            "生成符合 AetherBreath 日志标准的 Python 工具文件，并自动注册到 __init__.py。"
            "创建后需要重启 Agent 进程才能使用新工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "工具名称（仅字母、数字、下划线），如 'get_weather'"
                },
                "description": {
                    "type": "string",
                    "description": "工具功能描述，会写入 docstring 和 schema"
                },
                "parameters": {
                    "type": "object",
                    "description": "JSON Schema 格式的参数定义",
                    "properties": {
                        "city": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["string"]},
                                "description": {"type": "string"}
                            }
                        }
                    }
                },
                "implementation_code": {
                    "type": "string",
                    "description": (
                        "核心逻辑的 Python 代码。"
                        "代码中可直接使用参数名访问值，最后将结果赋值给 result 变量。"
                        "示例: result = f'城市 {city} 的天气是晴朗的'"
                    )
                }
            },
            "required": ["name", "description", "parameters", "implementation_code"]
        }
    }
}

__all__ = ["create_tool", "create_tool_schema"]