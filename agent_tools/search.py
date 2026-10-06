#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search —— 多引擎网页搜索（带相关性过滤）

SPDX-License-Identifier: MIT
Copyright (c) 2026 AetherBreath

⚠️ 铁律：本模块**不得**在 import 期触碰 sys.stdout（旧实现曾无条件
   sys.stdout.detach()，摧毁 pytest 的 capture，导致任何 import agent_tools 的测试全崩）。

设计要点（2026-09-16 重写相关性层）：
  旧版把整批结果拼成一个 blob 算一个分，只要有一条沾到查询就把整批放行，
  导致 Bing 中文分词退化时（"使命召唤 最新消息" → 2005 年电视剧《使命》）
  垃圾直通到我脸上。现在改成**逐条**判定 + 低相关直接丢弃。
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

# ---- 可配置项：全部走环境变量（AETHER_* 前缀），无密钥也能跑 ------------------
_TIMEOUT = float(os.environ.get("AETHER_SEARCH_TIMEOUT") or os.environ.get("SEARCH_TIMEOUT") or "8")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_CAPTION_SEP = "\u00b7"          # ·  Bing 摘要里时间前缀的分隔符
# 这些主机的结果对搜索无意义（导航位/自家域），直接丢
_SKIP_HOSTS = ("bing.com", "microsoft.com", "msn.com", "microsoftonline.com")

# ---- 相关性阈值（调参留痕，别乱动）------------------------------------------
_COV_STRONG = 0.5      # 强信号命中率：达到即通过
_COV_TWO_HIT = 0.34    # 命中 2 个及以上强信号时放低的门槛
_WEAK_MIN = 3          # 二字组兜底：至少命中这么多个不同二字组
# 强信号最少几个才有"覆盖率"可言：单个词查询（如"使命召唤"）无从判断，直接放行
_MIN_STRONG_FOR_GATE = 2
# DDG 单独给更短的超时：它一旦被限流就是纯白等，实测能吃掉 10 秒以上
_DDG_TIMEOUT = float(os.environ.get("AETHER_SEARCH_DDG_TIMEOUT") or "4")


# ---- 文本与 URL 清洗（只用标准库，不碰 stdout） ------------------------------
def _clean(text: str) -> str:
    """折叠所有 Unicode 空白（Bing 摘要里混有全角空格）。"""
    return " ".join((text or "").split())


def _strip_time_prefix(text: str) -> str:
    """剥掉 Bing 摘要的时间戳前缀（"6 天之前 ·" / "11 hours ago ·"）。"""
    if _CAPTION_SEP not in text:
        return text
    head, _, tail = text.partition(_CAPTION_SEP)
    head = head.strip()
    has_digit = any(c.isdigit() for c in head)
    if has_digit and any(k in head for k in ("之前", "前", "ago", "before")):
        return _clean(tail)
    return text


def _decode_bing_redirect(href: str) -> str:
    """Bing 有时把真实地址藏在 /ck/a?...&u=a1<base64url> 里，解出来。失败则原样返回。"""
    if "/ck/a" not in href:
        return href
    try:
        token = (parse_qs(urlsplit(href).query).get("u") or [""])[0]
        if token.startswith("a1"):
            token = token[2:]
        if not token:
            return href
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8", "ignore")
        start = raw.find("http")
        return raw[start:] if start >= 0 else href
    except Exception:
        return href


_TRACKING_KEYS = ("utm_", "FORM", "frm", "qs", "cvid", "toWww", "pc", "sh", "sk", "selm")


def _strip_tracking(url: str) -> str:
    """删掉追踪参数，链接更干净、去重更准。"""
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        kept = []
        for kv in parts.query.split("&"):
            key = kv.split("=", 1)[0]
            if not any(key.startswith(t) or key == t for t in _TRACKING_KEYS):
                kept.append(kv)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(kept), ""))
    except Exception:
        return url


def _normalize(url: str) -> str:
    u = _decode_bing_redirect((url or "").strip())
    if u.startswith("/"):
        u = "https://www.bing.com" + u
    u = _strip_tracking(u)
    return unquote(u) if "%" in u and "://" in u else u


def _host(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower()
    except Exception:
        return ""


def _useful(url: str) -> bool:
    h = _host(url)
    if not url.startswith("http") or not h:
        return False
    return not any(h == s or h.endswith("." + s) for s in _SKIP_HOSTS)


def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        key = it["href"].lower().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(it)
    return out


# ---- 相关性判定：按词组切分，逐条打分 ----------------------------------------
def _query_grams(query: str) -> Tuple[List[str], List[str]]:
    """把查询切成强/弱信号 gram。

    切段规则：ASCII 字母数字为一段，非 ASCII 非空白为一段（空格/标点天然是分隔符）。
      - ASCII 段：长度 >= 4 才算强信号（python / asyncio / protocol），小写化比对
      - 中文段：长度 >= 3 时产出 3 字与 4 字滑窗作强信号；
                所有 2 字滑窗作弱信号
    为什么不用单字：单字（使/命/召）在中文里是超高频字，电视剧《使命》的页面能
    命中一大片，旧版 _relevance 就是这么给垃圾页抬过分数的。
    """
    segs, cur, cur_ascii = [], [], None
    for ch in (query or "") + " ":
        if ch.isspace() or ch in "、。，,:：;；/|()-_":
            if cur:
                segs.append((cur_ascii, "".join(cur)))
                cur, cur_ascii = [], None
            continue
        a = ch.isascii() and ch.isalnum()
        if cur_ascii is None:
            cur_ascii = a
        elif a != cur_ascii:
            segs.append((cur_ascii, "".join(cur)))
            cur = []
            cur_ascii = a
        cur.append(ch)
    if cur:
        segs.append((cur_ascii, "".join(cur)))

    strong, weak = [], []
    for is_ascii, seg in segs:
        if is_ascii:
            if len(seg) >= 4:
                strong.append(seg.lower())
            continue
        n = len(seg)
        if n >= 3:
            for w in (3, 4):
                for i in range(n - w + 1):
                    strong.append(seg[i:i + w])
        for i in range(n - 1):
            weak.append(seg[i:i + 2])

    def _uniq(seq):
        seen, out = set(), []
        for g in seq:
            if g not in seen:
                seen.add(g)
                out.append(g)
        return out

    return _uniq(strong), _uniq(weak)


def _item_hit(strong: List[str], weak: List[str], text: str) -> Tuple[int, int, int]:
    """返回 (强命中数, 弱命中数, 最长强命中长度)。"""
    sh = [g for g in strong if g in text]
    wh = sum(1 for g in weak if g in text)
    longest = max((len(g) for g in sh), default=0)
    return len(sh), wh, longest


def _item_passes(ns: int, nw: int, cov: float) -> bool:
    """单条结果是否算相关。三条通路任一成立即可（见常量注释）。"""
    if cov >= _COV_STRONG:
        return True
    if ns >= 2 and cov >= _COV_TWO_HIT:
        return True
    if nw >= _WEAK_MIN:
        return True
    return False


def _score_batch(query: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每条结果打上内部评分字段（下划线开头，返回前由 _strip_scores 清掉）。"""
    strong, weak = _query_grams(query)
    total = max(1, len(strong))
    gated = len(strong) >= _MIN_STRONG_FOR_GATE
    for it in items:
        text = (it.get("title", "") + " " + it.get("body", "") + " " + it.get("href", "")).lower()
        ns, nw, longest = _item_hit(strong, weak, text)
        it["_ns"], it["_nw"], it["_long"] = ns, nw, longest
        it["_cov"] = ns / total
        # 强信号不足 2 个（单字/单词查询）时无从谈覆盖率，放行 —— 宁滥勿缺
        it["_ok"] = (not gated) or _item_passes(ns, nw, it["_cov"])
    return items


def _strip_scores(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keys = ("_ns", "_nw", "_long", "_cov", "_ok")
    out = []
    for it in items:
        out.append({k: v for k, v in it.items() if k not in keys})
    return out


def _filter_by_relevance(query: str, items: List[Dict[str, Any]]
                         ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """逐条筛相关。返回 (通过的原始条, 统计信息)。

    统计信息用于"全被筛光"时生成诊断项 —— 让我知道是"抓到了但不相关"，
    而不是"网络挂了"或"引擎没返回"。
    """
    scored = _score_batch(query, items)
    kept = [it for it in scored if it["_ok"]]
    stats = {
        "raw": len(scored),
        "kept": len(kept),
        "strong": len(_query_grams(query)[0]),
        "best_cov": max((it["_cov"] for it in scored), default=0.0),
        "best_long": max((it["_long"] for it in scored), default=0),
        "samples": [(it["title"][:40], round(it["_cov"], 2), it["_nw"], it["_long"])
                    for it in scored[:4]],
    }
    return _strip_scores(kept), stats


def _low_relevance_note(query: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    """抓到东西但全被判不相关时的显式说明。

    刻意放进返回列表里（source=filter），但标题以警示符开头，
    绝不是搜索结果 —— 引用前必须看清 source 字段。
    """
    tips = []
    has_cjk = any((not ch.isascii()) and ch.isalnum() for ch in query)
    if " " in query:
        if has_cjk:
            tips.append("中文查询带空格易被 Bing 拆坏，改成连续短语重试：「%s」"
                        % "".join(query.split()))
        else:
            tips.append("Bing 中文站对多词英文查询分词极差（实测只认头一个词），"
                        "建议减到 2-3 个核心词，或直接 web_extract 抓官方文档/站点")
    tips.append("也可换更具体的专有词组（作品名/版本号/人名/站点名）")
    return {
        "title": "⚠️ 无相关结果（抓到了但不相关，已按相关性过滤，未硬塞低质结果）",
        "href": "",
        "body": ("查询「%s」的 %d 条原始结果全部低于相关阈值：强信号 %d 个，"
                 "最佳命中率 %.2f，最佳强命中长度 %d 字。建议：%s。"
                 % (query, stats["raw"], stats["strong"], stats["best_cov"],
                    stats["best_long"], "；".join(tips))),
        "source": "filter",
    }


# ---- 引擎实现（每个都返回统一结构，失败一律返回 []，绝不抛给调用方） --------
def _parse_count(n: int) -> int:
    """抓得比要的多一点，给逐条过滤留余量（筛完可能不够 n 条）。"""
    return max(n * 3, 12)


def _via_bing(query: str, n: int, ensearch: bool = False) -> List[Dict[str, Any]]:
    """Bing HTML 解析（本机主力通道），返回**未过滤**的原始结果。

    ensearch=True 走 Bing 国际检索路径。实测它能修好空格分隔的多词中文查询
    （否则被退化成首字面匹配），但会弄坏中英混排 —— 故只在相关度不足时才补探。
    """
    try:
        from bs4 import BeautifulSoup                      # noqa: PLC0415
        params = {"q": query, "count": min(50, _parse_count(n))}
        if ensearch:
            params["ensearch"] = "1"
        resp = requests.get("https://www.bing.com/search", params=params,
                            headers={"User-Agent": _UA,
                                     "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                            timeout=_TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            logger.debug("bing http %s", resp.status_code)
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        out = []
        for node in soup.select("li.b_algo"):              # 先全收，过滤后再截断
            link = node.select_one("h2 a") or node.select_one("a")
            if not link:
                continue
            href = _normalize(link.get("href") or "")
            if not _useful(href):
                continue
            cap = node.select_one(".b_caption") or node.select_one("p")
            body = _strip_time_prefix(_clean(cap.get_text(" ", strip=True) if cap else ""))
            title = _clean(link.get_text(strip=True))
            if title:
                out.append({"title": title, "href": href, "body": body, "source": "bing"})
        return out
    except Exception as e:                                # 网络/解析异常一律降级
        logger.debug("bing failed: %s: %s", type(e).__name__, str(e)[:120])
        return []


_DDG_STATE = {"dead": False, "why": ""}   # 进程级死态缓存


def _via_ddg(query: str, n: int) -> List[Dict[str, Any]]:
    """DuckDuckGo：走 MIT 许可的 ddgs（回退旧包名 duckduckgo_search）。本机常不可达。

    不可用时打进程级死态标记，后续查询直接跳过 —— 这个引擎挂一次会白等好几秒，
    每次搜索都重试一遍等于把工具拖进超时（30s 硬上限）。
    """
    if _DDG_STATE["dead"]:
        return []
    try:
        try:
            from ddgs import DDGS                          # noqa: PLC0415
        except ImportError:
            from duckduckgo_search import DDGS             # noqa: PLC0415
    except Exception as e:
        _DDG_STATE.update(dead=True, why="import:" + type(e).__name__)
        logger.debug("ddg unavailable: %s", _DDG_STATE["why"])
        return []
    out = []
    try:
        try:
            ctx = DDGS(timeout=int(_DDG_TIMEOUT))
        except TypeError:                             # 旧版不接受 timeout 参数
            ctx = DDGS()
        with ctx as ddgs:
            for r in ddgs.text(query, max_results=_parse_count(n)):
                href = _normalize(r.get("href") or r.get("url") or "")
                if not _useful(href):
                    continue
                out.append({"title": _clean(r.get("title") or ""),
                            "href": href,
                            "body": _clean(r.get("body") or r.get("abstract") or ""),
                            "source": "duckduckgo"})
        return out
    except Exception as e:
        _DDG_STATE.update(dead=True, why="call:" + type(e).__name__)
        logger.debug("ddg failed: %s: %s", type(e).__name__, str(e)[:120])
        return []


def _gate(raw: List[Dict[str, Any]], query: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """过滤 + 判空语义：区分「抓到但不相关」与「什么都没抓到」。

    返回的 kept 为空且 raw 非空时，调用方应给出显式说明，而不是静默交付空列表 ——
    否则我分不清是引擎挂了还是查询词有问题，只能瞎猜。
    """
    kept, stats = _filter_by_relevance(query, raw)
    return kept, stats


def _via_bing_smart(query: str, n: int) -> List[Dict[str, Any]]:
    """Bing 双探 + 逐条相关性过滤。

    与旧版的关键差别：旧版是「两份取分高的那份」，两份都烂也照样交付；
    现在是「两份都过不了阈值就明确告诉你过不了」。
    """
    raw1 = _via_bing(query, n)
    kept, st1 = _gate(raw1, query)
    # 有货就走，不再为凑满 n 条去补探 ensearch：实测多词中文查询常是
    # 「10 条里 3 条相关」，硬凑会把单次请求翻倍，且 ensearch 那次往往抓回 0 条
    if kept:
        return kept[:n]
    raw2 = _via_bing(query, n, ensearch=True)
    kept2, st2 = _gate(raw2, query)
    if len(kept2) > len(kept):
        kept, st1 = kept2, st2
    if kept:
        return kept[:n]
    if st1["raw"] > 0:                       # 抓到了但全被判不相关
        return [_low_relevance_note(query, st1)]
    return []                                # 真的什么都没抓到，交给下一级引擎


def _via_ddg_gated(query: str, n: int) -> List[Dict[str, Any]]:
    """DuckDuckGo 同套过滤（保持两个引擎行为一致）。"""
    raw = _via_ddg(query, n)
    kept, st = _gate(raw, query)
    if kept:
        return kept[:n]
    if st["raw"] > 0:
        return [_low_relevance_note(query, st)]
    return []


# ---- 对外入口 ---------------------------------------------------------------
def search(query: str, max_results: int = 5, prefer_quality: bool = False) -> list:
    """多引擎网页搜索（结果已按查询词组做逐条相关性过滤）。

    参数:
        query: 搜索关键词。**中文尽量写成连续短语，别用空格分隔** ——
               Bing 中文常把空格多词查询拆坏（实测「使命召唤 最新消息」会返回
               电视剧《使命》的页面），过滤层虽会拦下垃圾，但空手而归不如一次命中。
        max_results: 期望条数（默认 5）
        prefer_quality: True=优先 DuckDuckGo（需本机装有 ddgs），False=优先 Bing HTML
    返回:
        [{"title", "href", "body", "source"}, ...]
        source 取值: bing / duckduckgo / **filter**
        ⚠️ source="filter" 且 href="" 的条目**不是搜索结果**，是"抓到了但全部低于
        相关阈值"的说明（含各条命中率与建议）。看到它就该换查询写法或改用
        web_extract 直接抓权威页面，切勿把它当作信息引用。
        全部引擎都无结果时返回空列表 []。
    """
    query = _clean(query)
    if not query:
        return []
    try:
        n = int(max_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 50))
    plan = ([_via_ddg_gated, _via_bing_smart] if prefer_quality
            else [_via_bing_smart, _via_ddg_gated])

    engines = list(plan)
    pooled: List[Dict[str, Any]] = []
    notes: List[Dict[str, Any]] = []
    while engines:
        engine = engines.pop(0)
        out = engine(query, n)
        for it in out:
            (notes if it.get("source") == "filter" else pooled).append(it)
        if pooled:                  # 有真结果就交，不为凑满 n 条去叫死引擎
            return _dedup(pooled)[:n]
        if notes and engine is _via_bing_smart and not prefer_quality:
            # Bing 明确答过了、只是不相关 —— 这属查询形态问题，不是引擎故障。
            # 再去叫下一级不值得：本机 ddgs 底层打 google，实测 16s 必超时。
            break
        # out 为空 = 该引擎不可达/零返回，这种情况才需要降级
    if notes:                       # 一个相关的都没有 —— 把原因交出去
        note = notes[0]
        if _DDG_STATE["dead"]:      # 顺带告知备份引擎的真实状态，省得我下轮白等
            note["body"] += "（备份引擎 DuckDuckGo 本机不可达：%s，底层走 google）" % _DDG_STATE["why"]
        return [note]
    return []                       # 引擎全部不可达/无返回


def get_status() -> Dict[str, Any]:
    """报告各引擎的配置态。刻意不做网络探测 —— 避免为"看一眼状态"白等一轮超时。"""
    ddg_ready = True
    ddg_err = ""
    try:
        import ddgs                                   # noqa: F401, PLC0415
    except Exception as e:
        try:
            import duckduckgo_search                  # noqa: F401, PLC0415
        except Exception as e2:
            ddg_ready = False
            ddg_err = type(e2).__name__
    return {
        "engines": ["bing", "duckduckgo"],
        "default_order": ["bing", "duckduckgo"],
        "prefer_quality_order": ["duckduckgo", "bing"],
        "ddg_library_installed": ddg_ready,
        "ddg_import_error": ddg_err,
        "timeout_sec": _TIMEOUT,
        "relevance": {"cov_strong": _COV_STRONG, "cov_two_hit": _COV_TWO_HIT,
                      "weak_min": _WEAK_MIN, "min_strong_for_gate": _MIN_STRONG_FOR_GATE},
        "note": "Tavily 通道已移除（2026-09-16）；过滤层会丢弃低相关结果并给出说明项",
    }


# ---- 工具的 JSON Schema（说明书） -------------------------------------------
search_schema = {
    "type": "function",
    "function": {
        "name": "search",
        "description": (
            "多引擎网页搜索（自研实现，带逐条相关性过滤）。顺序 Bing HTML → DuckDuckGo，"
            "任一引擎失败自动降级；返回标题、链接与摘要。低相关结果会被丢弃而非硬塞，"
            "此时返回的条目 source=filter（那不是搜索结果，是过滤说明）。"
            "中文查询请写成连续短语、不要用空格分隔（Bing 中文会把空格多词拆坏）。"
            "若发现疑似相关链接，请用 fetch_url 或 web_extract 获取页面完整内容再判断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（中文建议连续短语），例如：'使命召唤最新消息'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最多返回多少条结果，默认为 5",
                    "default": 5,
                },
                "prefer_quality": {
                    "type": "boolean",
                    "description": "True=优先 DuckDuckGo（需装有 ddgs 包）；False=优先 Bing HTML（默认）",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
}
