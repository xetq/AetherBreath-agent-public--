# -*- coding: utf-8 -*-
"""URL 安全校验：疑似凭据 / 内网地址（SSRF）。

**规则只有一处。** `web_extract` 与 `fetch_url` 都 import 本模块。

为什么抽出来：本项目已经出过这个事故 —— 同一类工具里 `web_extract` 有
「凭据 + SSRF」两道闸，`fetch_url` 一道都没有，而它的 schema 还劝模型
「有更优秀的 web_extract, 优先用它」。**选择哪个工具不该决定安全性**：
模型换个工具名，闸门就整体消失。

取向（对齐 Hermes `tools/url_safety.py` 的思路，简版）：
  · 只挡**显式**内网地址，不做 DNS 解析 —— 解析到内网的域名挡不住
    （要更严需解析后再判，见 approvals/README.md 已知限制）；
  · 命中即拒绝，不给「仍要访问」的授权入口 —— 换个写法就能绕过的东西，
    留个按钮只会在卡片上制造噪音；
  · 拦截措辞由本模块统一给出，两个工具回报同一句话，便于测试断言。
  · **重定向也要过闸**（`safe_get`）：旧版只校验初始 URL，而 requests 默认
    `allow_redirects=True` —— 公网 URL 302 到 `http://127.0.0.1/` 时，请求已经
    打到内网服务上了，闸门事后才知道。现在改为手动逐跳、每跳先校验。

已修的两个真 BUG（2026-09-19 工具集审计）：
  B2  IPv6 内网地址全线漏网：`url_is_private` 先 `host.strip("[]")` 去掉方括号，
      正则却要求字面 `[::1]`/`[fc` → 永不匹配。实测量过：
      `http://[::1]/`、`http://[::]/`、`http://[fc00::1]/`、`http://[fd12:3456::1]/`
      全部放行。现按「先剥壳、再按形态判」重写（IPv6 走独立规则）。
  B11 docstring 说「解析不出主机名时按『是』处理（保守）」，实现却返回 False
      （`file:///etc/passwd`、`http://` 实测都放行）。现按文档语义实现。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, Optional

# 疑似密钥模式（对齐 Hermes tools/url_safety.py + agent/redact.py 的思路）：
#   A. 前缀式凭据，全串搜索（sk- / ghp_ / github_pat_ / xox / AKIA / rk-live-）
#   B. 敏感 query 参数名（对齐 Hermes _SENSITIVE_QUERY_PARAM_NAMES 的子集）
_PREFIX_SECRET_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9]{12,}|ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"xox[baprs]-[a-z0-9-]{10,}|AKIA[0-9a-z]{16}|rk-live-[a-z0-9]{16,})"
)
_SENSITIVE_PARAM_NAMES = frozenset({
    "access_token", "api_key", "apikey", "auth_token", "authorization",
    "awsaccesskeyid", "client_secret", "credential", "credentials", "jwt",
    "password", "passwd", "secret", "session_id", "signature", "token",
    "x_amz_security_token", "x_amz_signature", "x_api_key", "x_auth_token",
})

# ---------------------------------------------------------------
# 显式内网/本机地址（IPv4）
# ---------------------------------------------------------------
# 注意：本正则匹配的是**已剥掉方括号**的主机名，所以这里只写 IPv4 形态。
# 旧版把 `\[::1\]` / `\[fc` 也塞进这条正则，而调用方先做了 strip("[]")，
# 于是 IPv6 分支永远不可能命中（B2）。
_PRIVATE_HOST_RE = re.compile(
    r"(?i)^(localhost|0\.0\.0\.0|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
    r"169\.254\.\d{1,3}\.\d{1,3})$"
)

# IPv6 显式内网形态（主机名里含冒号时走这条）：
#   ::1 / ::              回环、未指定
#   fc00::/7              ULA（唯一本地地址）
#   fe80::/10             链路本地
#   ::ffff:<内网 IPv4>    IPv4-mapped
_PRIVATE_IPV6_RE = re.compile(
    r"(?i)^(::1|::|"
    r"f[cd][0-9a-f]{2}:.*|"
    r"fe[89ab][0-9a-f]:.*|"
    r"::ffff:(?:127\.|10\.|192\.168\.|169\.254\.|172\.(?:1[6-9]|2\d|3[01])\.))"
)


class UnsafeURLError(Exception):
    """URL 未通过安全闸（疑似凭据 / 内网地址 / 重定向越界）。"""


def url_has_secret(url: str) -> bool:
    """URL 里是否疑似带着凭据（明文或百分号编码）。"""
    decoded = urllib.parse.unquote(url)
    if _PREFIX_SECRET_RE.search(url) or _PREFIX_SECRET_RE.search(decoded):
        return True
    try:
        parsed = urllib.parse.urlsplit(decoded)
    except ValueError:
        return False
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if value and urllib.parse.unquote(key).lower() in _SENSITIVE_PARAM_NAMES:
            return True
    return False


def url_is_private(url: str) -> bool:
    """URL 是否显式指向本机/内网地址。

    保守取向（与模块 docstring 一致）：
      · 解析失败          → True
      · **拿不到主机名**  → True（`file:///etc/passwd`、`http://` 这类；旧实现
        返回 False，与 docstring 说的「保守」相反 —— B11）
      · 主机名含冒号      → 按 IPv6 字面量规则判（B2）
    """
    try:
        host = urllib.parse.urlparse(url).hostname
    except ValueError:
        return True
    if not host:
        return True
    host = host.strip().strip("[]").lower()
    if not host:
        return True
    if _PRIVATE_HOST_RE.match(host):
        return True
    if ":" in host:                      # 域名不含冒号，含冒号即 IPv6 字面量
        return bool(_PRIVATE_IPV6_RE.match(host))
    return False


def check_url(url: str) -> Optional[str]:
    """返回拦截原因（给模型看的短语）；可放行时返回 None。

    只回答「这个 URL 能不能发出去」，不关心能不能取回内容 ——
    scheme 校验、可用性错误仍由各自工具负责。
    """
    if url_has_secret(url):
        return "URL 疑似包含 API key/令牌, 禁止发送"
    if url_is_private(url):
        return "URL 指向内网/本机地址(SSRF 防护)"
    return None


# ---------------------------------------------------------------
# 带闸门的 GET（两个抓取工具共用；**重定向逐跳校验**）
# ---------------------------------------------------------------
_REDIRECT_CODES = (301, 302, 303, 307, 308)


def safe_get(url: str, *, timeout: Any = 15,
             headers: Optional[Dict[str, str]] = None,
             max_redirects: int = 5, **kwargs):
    """带 SSRF 校验的 GET：**手动逐跳**跟随重定向，每一跳的落点都先过闸。

    为什么要手动：`requests.get` 默认 `allow_redirects=True`，会在不通知我们的
    情况下继续请求 302 指向的地址。初始 URL 是公网、第二跳是 `http://127.0.0.1/`
    时，只校验初始 URL 等于没校验 —— 请求已经打到内网服务上了（B9）。

    行为：
      · 每一跳都先 `check_url()`，命中即抛 `UnsafeURLError`（请求**尚未发出**）；
      · 检测重定向成环 / 超过 max_redirects，抛 `UnsafeURLError`；
      · 返回**未自动消费**的响应对象，`raise_for_status()` / 读 body 由调用方负责。

    延迟导入 requests：本模块的规则部分保持零第三方依赖，测试可脱离网络。
    """
    import requests                                     # noqa: PLC0415

    seen = []
    current = url
    for _ in range(max_redirects + 1):
        reason = check_url(current)
        if reason:
            raise UnsafeURLError("%s（落点：%s）" % (reason, current))
        resp = requests.get(current, headers=headers, timeout=timeout,
                            allow_redirects=False, **kwargs)
        location = resp.headers.get("Location") if resp.status_code in _REDIRECT_CODES else None
        if not location:
            return resp
        nxt = urllib.parse.urljoin(current, location)
        if nxt in seen:
            raise UnsafeURLError("重定向成环，拒绝跟随：%s" % nxt)
        seen.append(current)
        current = nxt
    raise UnsafeURLError("重定向超过 %d 次，拒绝继续跟随" % max_redirects)
