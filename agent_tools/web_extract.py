# -*- coding: utf-8 -*-
"""
工具名称: web_extract
功能: 批量抓取网页, 提取干净的 Markdown 正文 —— 语义与 Hermes 的 web_extract 工具对齐
来源: Hermes tools/web_tools.py 的 web_extract_tool() 可移植重实现
      (原版把抓取交给第三方 provider: firecrawl/tavily/exa/parallel, 需要 API key;
       本版本是纯本地实现: requests + BeautifulSoup + 自写 HTML→Markdown, 零 key 也能跑)

能力对齐 Hermes:
  - 接受 URL 字符串列表(也兼容单个 str, 或含 "url"/"href" 字段的 dict)
  - 返回 {"results": [{url, title, content, error}, ...]}, 保持输入顺序
  - content 是去掉导航/广告后的 Markdown 正文(不经过任何 LLM)
  - 单页超过 char_limit(默认 15000)时: 返回 75% 头 + 25% 尾, 完整全文落盘到 cache/web/,
    footer 写明截断信息和全文路径 —— 与 Hermes 的 _truncate_with_footer() 同款语义

安全(轻量版, 对齐 Hermes 的两道闸):
  1. 拒绝带疑似密钥/令牌的 URL(防止把 secret 发给第三方读取器)
  2. 拒绝明显指向内网/本机的 URL(SSRF 防护; 简版只挡显式内网地址, 不做 DNS 解析校验)
"""
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests
from bs4 import BeautifulSoup, Tag

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DEFAULT_CHAR_LIMIT = 15000
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "web")

# 安全校验：规则与 fetch_url **共用一处**（agent_tools/url_safety.py）。
# 这两道闸曾经只长在 web_extract 上，而 fetch_url 一道都没有 ——
# 工具的 schema 又劝模型优先用前者，于是「换个工具名 = 闸门整体消失」。
try:                                        # 包内导入（正常路径）
    from .url_safety import (check_url, url_has_secret, url_is_private,
                             safe_get, UnsafeURLError,
                             _PREFIX_SECRET_RE, _PRIVATE_HOST_RE,
                             _SENSITIVE_PARAM_NAMES)
except ImportError:                         # 直接以脚本方式运行时的兜底
    from url_safety import (check_url, url_has_secret, url_is_private,
                            safe_get, UnsafeURLError,
                            _PREFIX_SECRET_RE, _PRIVATE_HOST_RE,
                            _SENSITIVE_PARAM_NAMES)

# 旧名保留：本模块早期以 _url_* 命名，测试与外部脚本可能仍在引用
_url_has_secret = url_has_secret
_url_is_private = url_is_private


# ── HTML → Markdown ──────────────────────────────────────────────────────────
_INLINE_MAIN = ("b", "strong", "em", "i", "code", "a", "img", "br", "span", "small",
                "sub", "sup", "mark", "kbd", "s", "del", "u", "abbr", "time", "wbr")

def _inline_to_md(node: Tag, base_url: Optional[str]) -> str:
    """把行内节点/文本转成 markdown 片段。"""
    if isinstance(node, str):
        return node
    name = node.name.lower() if node.name else ""
    if name in ("b", "strong"):
        return f"**{_inline_to_md_text(node, base_url)}**"
    if name in ("em", "i"):
        return f"*{_inline_to_md_text(node, base_url)}*"
    if name == "code":
        return f"`{node.get_text()}`"
    if name == "a":
        text = _inline_to_md_text(node, base_url)
        href = (node.get("href") or "").strip()
        if not href:
            return text
        if base_url and not href.startswith(("http://", "https://", "#", "mailto:")):
            href = urllib.parse.urljoin(base_url, href)
        if href.startswith("#"):
            return text  # 页内锚点没意义, 保留文字
        return f"[{text}]({href})"
    if name == "img":
        alt = (node.get("alt") or "").strip()
        src = (node.get("src") or "").strip()
        if src.startswith("data:"):
            return f"[IMAGE: {alt}]" if alt else "[IMAGE]"
        if base_url and src.startswith(("/", "./", "../")) and not src.startswith("//"):
            src = urllib.parse.urljoin(base_url, src)
        return f"![{alt}]({src})" if src else (f"[IMAGE: {alt}]" if alt else "")
    if name == "br":
        return "\n"
    if name == "span":
        return _inline_to_md_text(node, base_url)
    # 其余行内标签: 取内部文本, 递归处理嵌套
    return _inline_to_md_text(node, base_url)


def _inline_to_md_text(node: Tag, base_url: Optional[str]) -> str:
    """递归拼接行内内容。"""
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif isinstance(child, Tag):
            parts.append(_inline_to_md(child, base_url))
    return "".join(parts).strip()


_BLOCK_ONLY = ("script", "style", "noscript", "nav", "footer", "header", "aside",
               "form", "iframe", "button", "select", "input", "textarea", "svg",
               "canvas", "video", "audio", "template", "dialog")


def _block_to_md(node: Tag, base_url: Optional[str]) -> Optional[str]:
    """把一个块级元素转成 markdown 块; 返回 None 表示该元素应被丢弃。"""
    if isinstance(node, str):
        text = node.strip()
        return text if text else None
    name = node.name.lower() if node.name else ""
    if name in _BLOCK_ONLY:
        return None

    # 标题
    m = re.fullmatch(r"h([1-6])", name)
    if m:
        level = int(m.group(1))
        text = _inline_to_md_text(node, base_url)
        return f"{'#' * level} {text}" if text else None

    if name in ("p", "div", "section", "article", "main", "li"):
        text = _inline_to_md_text(node, base_url)
        return text if text else None

    if name == "pre":
        code = node.get_text()
        lang = ""
        code_tag = node.find("code")
        if code_tag and code_tag.get("class"):
            cls = " ".join(code_tag.get("class"))
            m2 = re.search(r"(?:language-|lang-)?([\w+-]+)", cls)
            lang = m2.group(1) if m2 else ""
        return f"```{lang}\n{code.strip()}\n```"

    if name == "code":  # 独立 code 块(不在 pre 里)
        return f"`{node.get_text().strip()}`"

    if name in ("ul", "ol"):
        lines = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            inner = _inline_to_md_text(li, base_url)
            if name == "ol":
                lines.append(f"{i}. {inner}")
            else:
                lines.append(f"- {inner}")
            # 嵌套列表
            for sub in li.find_all(["ul", "ol"], recursive=False):
                sub_lines = _block_to_md(sub, base_url)
                if sub_lines:
                    for sl in sub_lines.split("\n"):
                        lines.append(f"  {sl}")
        return "\n".join(lines) if lines else None

    if name == "blockquote":
        inner = _inline_to_md_text(node, base_url)
        if not inner:
            return None
        return "\n".join(f"> {line}" for line in inner.split("\n"))

    if name == "table":
        rows = []
        for tr in node.find_all("tr"):
            cells = []
            for td in tr.find_all(["td", "th"]):
                cells.append(_inline_to_md_text(td, base_url).replace("|", "\\|"))
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows) if rows else None

    if name == "hr":
        return "---"

    if name in _INLINE_MAIN:
        return _inline_to_md(node, base_url)

    # 未知块级容器: 递归处理子节点, 拼成多个块
    blocks = []
    for child in node.children:
        if isinstance(child, str):
            text = child.strip()
            if text:
                blocks.append(text)
        elif isinstance(child, Tag):
            part = _block_to_md(child, base_url)
            if part:
                blocks.append(part)
    return "\n\n".join(b for b in blocks if b) if blocks else None


def _html_to_markdown(html: str, url: str) -> tuple:
    """整个页面 → (页面标题, markdown 正文)。"""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(strip=True)
    # 去掉无用标签
    for tag in soup(_BLOCK_ONLY):
        tag.decompose()

    body = soup.body or soup
    chunks = []
    for child in body.children:
        part = _block_to_md(child, url) if isinstance(child, Tag) else (child.strip() or None)
        if part:
            chunks.append(part)

    # 压缩 3+ 个连续空行
    text = "\n\n".join(chunks)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()


# ── 截断 + 落盘(对齐 Hermes _truncate_with_footer)────────────────────────────
# 落盘目录的容量上限（审计 T7：cache/web/ 原来只增不减，长会话高频抓取会无界增长）。
CACHE_MAX_FILES = 200


def _prune_cache() -> None:
    """只保留最近 CACHE_MAX_FILES 个全文缓存，多余的按修改时间从旧到新删。

    为什么不是 TTL：缓存的价值在于"同一页不用抓第二遍"，而上次访问时间不可靠；
    按条数封顶更简单、也更能防住"无界增长"这个真正的风险。
    文件数没超上限时这里只做一次 glob，开销可忽略。
    """
    try:
        files = [p for p in Path(CACHE_DIR).glob("*.md")]
        if len(files) <= CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[CACHE_MAX_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


def _store_full_text(url: str, content: str) -> Optional[str]:
    """全文落盘, 返回文件路径; 失败返回 None。"""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        digest = __import__("hashlib").sha256(url.encode("utf-8")).hexdigest()[:16]
        path = os.path.join(CACHE_DIR, f"{digest}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        _prune_cache()
        return path
    except Exception:
        return None


def _truncate_with_footer(content: str, url: str, char_limit: int) -> tuple:
    """返回 (model_text, was_truncated)。75% 头 + 25% 尾, 按换行切, footer 指明全文路径。"""
    if len(content) <= char_limit:
        return content, False

    head_budget = int(char_limit * 0.75)
    tail_budget = char_limit - head_budget
    head = content[:head_budget]
    tail = content[-tail_budget:]
    nl = head.rfind("\n")
    if nl > head_budget * 0.5:
        head = head[:nl]
    nl = tail.find("\n")
    if 0 <= nl < tail_budget * 0.5:
        tail = tail[nl + 1:]

    total = len(content)
    stored_path = _store_full_text(url, content)

    footer_lines = [
        "",
        "─" * 8 + " [TRUNCATED] " + "─" * 8,
        f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
        f"of {total:,} total clean characters.",
    ]
    if stored_path:
        middle_start_line = head.count("\n") + 2
        footer_lines.append(f"Full text saved to: {stored_path}")
        footer_lines.append(
            f'To read the omitted middle: read_file path="{stored_path}" '
            f"offset={middle_start_line} limit=200"
        )
    else:
        footer_lines.append("Full text could not be stored; re-run web_extract on a more specific URL.")
    footer_lines.append("─" * 29)

    model_text = head + "\n\n[... middle omitted — see footer ...]\n\n" + tail
    model_text += "\n" + "\n".join(footer_lines)
    return model_text, True


# ── 抓取单页 ─────────────────────────────────────────────────────────────────
def _fetch_one(url: str, char_limit: int) -> Dict[str, str]:
    try:
        # safe_get：手动逐跳跟随重定向，每一跳的落点都先过 SSRF 闸门（审计 B9）。
        # 旧实现用 requests.get()，默认 allow_redirects=True —— 公网 URL 302 到
        # http://127.0.0.1/ 时，请求已经打到内网服务上了才轮到我们知道。
        resp = safe_get(
            url,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            timeout=15,
        )
        resp.raise_for_status()
        # 优先用响应头 charset, 否则探测
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text
    except UnsafeURLError as e:
        return {"url": url, "title": "", "content": "",
                "error": "已拦截: %s" % e}
    except requests.exceptions.Timeout:
        return {"url": url, "title": "", "content": "", "error": "请求超时, 网页响应过慢"}
    except requests.exceptions.ConnectionError:
        return {"url": url, "title": "", "content": "", "error": "无法连接到该网址, 请检查网络或 URL 是否正确"}
    except requests.exceptions.HTTPError as e:
        return {"url": url, "title": "", "content": "",
                "error": f"HTTP 状态码 {e.response.status_code}, 可能页面不存在或禁止访问"}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "title": "", "content": "", "error": f"读取网页异常: {e}"}

    title, content = _html_to_markdown(html, url)
    if not content:
        return {"url": url, "title": title, "content": "",
                "error": "网页内容为空, 可能页面需要 JavaScript 渲染"}
    model_text, _truncated = _truncate_with_footer(content, url, char_limit)
    return {"url": url, "title": title, "content": model_text, "error": None}


def _normalize_url_item(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item.strip() if item.strip() else None
    if isinstance(item, dict):
        for key in ("url", "href"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def web_extract(urls: Union[str, List[Any]], char_limit: int = DEFAULT_CHAR_LIMIT) -> Dict:
    """
    批量抓取网页并提取干净 Markdown 正文(不经 LLM)。

    参数:
        urls: URL 字符串、URL 列表, 或含 "url"/"href" 字段的对象列表
        char_limit: 每页返回给模型的字符预算, 默认 15000; 超过则返回头尾窗口,
                    完整全文自动存到 cache/web/ 并在 footer 给出 read_file 读取指引

    返回:
        {"results": [{url, title, content, error}, ...]} —— 保持输入顺序,
        content 为 Markdown 正文(可能已截断), error 为 None 或失败原因
    """
    if isinstance(urls, (str, dict)):
        urls = [urls]

    try:
        char_limit = max(2000, min(int(char_limit), 500_000))
    except (TypeError, ValueError):
        char_limit = DEFAULT_CHAR_LIMIT

    results = []
    for index, item in enumerate(urls):
        raw_url = _normalize_url_item(item)
        if raw_url is None:
            results.append({"url": "", "title": "", "content": "",
                            "error": f"Invalid URL item at index {index}: expected a URL string or an object with 'url'/'href'"})
            continue
        if not raw_url.startswith(("http://", "https://")):
            results.append({"url": raw_url, "title": "", "content": "",
                            "error": "URL 需以 http:// 或 https:// 开头"})
            continue
        if _url_has_secret(raw_url):
            results.append({"url": raw_url, "title": "", "content": "",
                            "error": "已拦截: URL 疑似包含 API key/令牌, 禁止发送"})
            continue
        if _url_is_private(raw_url):
            results.append({"url": raw_url, "title": "", "content": "",
                            "error": "已拦截: URL 指向内网/本机地址(SSRF 防护)"})
            continue
        results.append(_fetch_one(raw_url, char_limit))

    # ok：本批至少有一页真的拿到了正文。历史返回只有 results，**全失败时也没有失败位**，
    # 于是"整批抓取失败"被判成成功（审计 L2-T4 的虚报来源之一）。加上显式失败位后，
    # 编排器/界面用的统一判据（success/ok == False）就能认出来。
    return {"results": results, "ok": any(r.get("content") for r in results)}


# 工具的 JSON Schema(说明书)
web_extract_schema = {
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": (
            "抓取指定 URL 的网页并提取干净 Markdown 正文(自动去除导航/广告/脚本), "
            "不经任何 LLM 处理。支持批量(一次传多个 URL)。"
            "适合在 web_search 找到相关链接后查看页面完整内容。"
            "单页超过 char_limit 时自动返回开头+结尾窗口, 完整全文存到本地 cache/web/ 并给出读取指引。"
            "注意: 需要 JavaScript 渲染的页面可能提取为空。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读取的网页 URL 列表(也接受单个 URL 字符串或含 url/href 的对象)"
                },
                "char_limit": {
                    "type": "integer",
                    "description": "每页字符预算, 默认 15000; 超长自动截断并全文落盘",
                    "default": 15000
                }
            },
            "required": ["urls"]
        }
    }
}


if __name__ == "__main__":
    import sys
    targets = sys.argv[1:] or ["https://example.com"]
    print(json.dumps(web_extract(targets), ensure_ascii=False, indent=2))
