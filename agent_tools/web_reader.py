"""
工具名称: fetch_url
功能: 获取指定 URL 的网页内容并提取正文文本

安全（与 web_extract **共用同一套闸**，见 agent_tools/url_safety.py）:
  1. 拒绝带疑似密钥/令牌的 URL（防止 secret 随 URL 发往外部）
  2. 拒绝显式指向内网/本机的 URL（SSRF 防护）
这两道闸原先只长在 web_extract 上，本工具一道都没有 —— 而本工具的 schema
还写着「有更加优秀的 web_extract 工具, 优先用它」。工具可选 = 闸门可选：
模型换个工具名，安全措施整体消失。故抽成共用模块，两处同源。

审计修复（2026-09-19）：
  · B9 重定向绕过：改走 url_safety.safe_get() —— **手动逐跳**校验，
    公网 URL 302 到 http://127.0.0.1/ 时，落点在请求发出前就被拦。
    旧实现用 requests.get()，默认 allow_redirects=True，只校验了初始 URL。
  · B10 max_chars 参数此前没写进 schema，模型看不到、永远只能用 3000 —— 现已暴露。
"""

import requests
from bs4 import BeautifulSoup
import logging

try:                                    # 包内导入（正常路径）
    from .url_safety import check_url, safe_get, UnsafeURLError
except ImportError:                     # 直接以脚本方式运行时的兜底
    from url_safety import check_url, safe_get, UnsafeURLError

logger = logging.getLogger(__name__)

def fetch_url(url: str, max_chars: int = 3000) -> str:
    """
    获取网页内容，提取纯文本。

    参数:
        url: 要读取的网页地址
        max_chars: 最大返回字符数（防止上下文爆炸）

    返回:
        网页正文文本，或错误信息
    """
    if not url or not url.startswith(('http://', 'https://')):
        return "❌ 请提供有效的 URL（需以 http:// 或 https:// 开头）"

    blocked = check_url(url)
    if blocked:
        return f"❌ 已拦截: {blocked}"

    try:
        # 模拟浏览器访问，降低被屏蔽概率
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        # safe_get：逐跳校验重定向落点（B9）
        response = safe_get(url, headers=headers, timeout=10)
        response.raise_for_status()

        # 用 BeautifulSoup 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')

        # 移除 script、style 等非正文标签
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()

        # 提取纯文本
        text = soup.get_text(separator='\n', strip=True)

        # 清理多余空行
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned_text = '\n'.join(lines)

        # 限制长度
        if len(cleaned_text) > max_chars:
            cleaned_text = cleaned_text[:max_chars] + "\n... (内容过长，已截断)"

        return cleaned_text if cleaned_text else "网页内容为空，可能页面需要 JavaScript 渲染。"

    except UnsafeURLError as e:
        return f"❌ 已拦截: {e}"
    except requests.exceptions.Timeout:
        return "❌ 请求超时，网页响应过慢。"
    except requests.exceptions.ConnectionError:
        return "❌ 无法连接到该网址，请检查网络或 URL 是否正确。"
    except requests.exceptions.HTTPError as e:
        return f"❌ HTTP 状态码 {e.response.status_code}，可能页面不存在或禁止访问。"
    except Exception as e:
        logger.error(f"读取网页失败: {e}", exc_info=True)
        return f"❌ 读取网页时发生异常 - {str(e)}"


# 工具的 JSON Schema
fetch_url_schema = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "读取指定 URL 的网页正文内容，去除导航、广告等无关元素，返回纯文本。"
            "适合在搜索到相关链接后，进一步查看页面详情。"
            "注意：部分需要 JavaScript 渲染的页面可能无法正常获取。"
            "有更加优秀的web_extract工具,优先用它"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读取的网页 URL，例如 'https://www.weather.com.cn/weather/101010100.shtml'"
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最大返回字符数（防止上下文爆炸），默认 3000。"
                }
            },
            "required": ["url"]
        }
    }
}
