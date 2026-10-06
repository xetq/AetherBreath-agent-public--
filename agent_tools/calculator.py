import ast
import operator
import logging
import math
from typing import Union

logger = logging.getLogger(__name__)

# 幂运算的规模上限（审计 B15）
# 旧实现完全不管指数：`2**2**16` 会被**真算完**，然后死在
# `ValueError: Exceeds the limit (4300 digits) for integer string conversion`
# 上（CPython 3.11+ 的 int→str 保护）—— 等于先烧完 CPU/内存才失败。
# 更狠的 `9**9**9` 会直接把工具拖进超时（几十秒 CPU + 数百 MB 内存）。
# 这里在**算之前**用对数估位数，超限直接拒绝。
_MAX_RESULT_DIGITS = 4000          # 留出余量给 4300 位的 str 转换限制
_MAX_EXPONENT = 4096               # 指数本身的硬上限（防 `2**2**2**2**...` 这类嵌套）


class SafeCalculator:
    """使用 AST 安全计算"""

    # 允许的运算符映射
    _operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,      # 一元负号
        ast.Mod: operator.mod,
        ast.FloorDiv: operator.floordiv,
    }

    @classmethod
    def _check_pow_scale(cls, base, exponent) -> None:
        """幂运算的规模闸门：位数超限就拒绝，别等算完再炸（B15）。"""
        if isinstance(exponent, int) and exponent > _MAX_EXPONENT:
            raise TypeError(
                f"指数过大（{exponent} > 上限 {_MAX_EXPONENT}），拒绝计算"
            )
        if isinstance(base, int) and isinstance(exponent, int) and exponent > 0:
            try:
                digits = int(exponent * math.log10(abs(base))) + 1
            except (ValueError, OverflowError):
                return
            if digits > _MAX_RESULT_DIGITS:
                raise TypeError(
                    f"结果约有 {digits} 位，超过上限 {_MAX_RESULT_DIGITS} 位，拒绝计算"
                )

    @classmethod
    def _safe_eval(cls, node: ast.AST) -> Union[int, float]:
        """递归安全评估 AST 节点，只允许数字和基本运算"""
        if isinstance(node, ast.Constant):
            # 只允许数字
            if isinstance(node.value, (int, float)):
                return node.value
            raise TypeError(f"非法常量: {node.value}")

        elif isinstance(node, ast.BinOp):
            # 二元运算 (加减乘除)
            left = cls._safe_eval(node.left)
            right = cls._safe_eval(node.right)
            if type(node.op) in cls._operators:
                if isinstance(node.op, ast.Pow):
                    cls._check_pow_scale(left, right)
                return cls._operators[type(node.op)](left, right)
            raise TypeError(f"不支持的运算符: {type(node.op).__name__}")

        elif isinstance(node, ast.UnaryOp):
            # 一元运算 (负数)
            operand = cls._safe_eval(node.operand)
            if type(node.op) in cls._operators:
                return cls._operators[type(node.op)](operand)
            raise TypeError(f"不支持的一元运算符: {type(node.op).__name__}")

        else:
            raise TypeError(f"表达式包含非法结构: {type(node).__name__}")

    @classmethod
    def calculate(cls, expression: str) -> str:
        """生产级入口：解析并计算表达式"""
        try:
            # 1. 清理输入
            expr_clean = expression.strip()
            if not expr_clean:
                return "❌ 表达式为空"

            # 2. 字符级白名单（只允许数字、运算符、括号、空格、小数点）
            allowed_chars = set("0123456789+-*/().%^ ")
            if any(c not in allowed_chars for c in expr_clean):
                return f"❌ 表达式包含非法字符。仅支持 数字 和 + - * / ( ) . % ^"

            # 3. 将 ^ 转换为 **（AST 解析时自动处理，但我们需要替换文本）
            # 注意：为了安全，我们不直接替换，而是解析后处理幂运算
            # 但 AST 不识别 ^，所以文本替换必须在解析前：
            safe_expr = expr_clean.replace('^', '**')

            # 4. 解析为 AST
            tree = ast.parse(safe_expr, mode='eval')

            # 5. 安全计算
            result = cls._safe_eval(tree.body)

            # 6. 格式化输出
            if isinstance(result, float):
                # 如果结果是无限大或 NaN
                if math.isinf(result):
                    return "❌ 结果溢出（无穷大）"
                if math.isnan(result):
                    return "❌ 结果非数字（NaN）"
                # 如果是整数，去掉 .0
                if result.is_integer():
                    result = int(result)
                else:
                    result = round(result, 6)  # 保留6位小数

            return f"✅ 计算结果: {result}"

        except SyntaxError as e:
            logger.warning(f"表达式语法错误: {expression}, 错误: {e}")
            return f"❌ 表达式语法错误，请检查括号或运算符。"
        except TypeError as e:
            logger.warning(f"表达式类型错误: {expression}, 错误: {e}")
            return f"❌ {str(e)}"
        except ZeroDivisionError:
            return "❌ 除以零！"
        except Exception as e:
            logger.error(f"计算异常: {expression}, 异常: {e}", exc_info=True)
            return f"❌ 内部计算异常，已记录日志。"

# 暴露给外部的函数接口（保持与老版本兼容）
def calculator(expression: str) -> str:
    return SafeCalculator.calculate(expression)

# 工具 Schema（保持不变，但生产环境下建议用 Pydantic 生成，这里暂用字典）
calculator_schema = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "基础数学运算。仅支持 + - * / ( ) . % ^。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如 '3 + 5 * 2' 或 '10 / 3'"
                }
            },
            "required": ["expression"]
        }
    }
}
