# -*- coding: utf-8 -*-
"""search 工具降级语义回归集（纯打桩，零网络）。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_search_fallback.py -q

存在理由：2026-09-16 的相关性过滤改完后，旧降级条件 `if len(pooled) >= n`
会让「10 条里筛剩 3 条」继续去叫下一级引擎 —— 而本机 ddgs 底层打 google，
实测 16s 必超时，于是整次搜索被推到 30s 工具硬上限强杀。
这几条把「有货即交 / 不相关不叫备胎 / 真死了才降级」钉成契约。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent_tools            # noqa: F401
import agent_tools.search     # noqa: F401
S = sys.modules["agent_tools.search"]   # 子模块名被包内 from-import 遮蔽，只能这么取

NOTE = {"title": "⚠️ 说明项", "href": "", "body": "原始结果全部低相关", "source": "filter"}


def _good(i):
    return {"title": "使命召唤%d 官网" % i, "href": "https://a%d.com/" % i,
            "body": "使命召唤", "source": "bing"}


def _stub(monkeypatch, bing, ddg):
    calls = {"ddg": 0}
    monkeypatch.setattr(S, "_via_bing_smart", lambda q, n: list(bing))

    def _d(query, n):
        calls["ddg"] += 1
        return list(ddg)
    monkeypatch.setattr(S, "_via_ddg_gated", _d)
    return calls


def test_有货即交_哪怕没凑满条数(monkeypatch):
    calls = _stub(monkeypatch, bing=[_good(1), _good(2)], ddg=[_good(3)])
    r = S.search("使命召唤", 5)          # n=5 而 bing 只给 2 条
    assert len(r) == 2, "筛剩几条就交几条，不硬凑"
    assert calls["ddg"] == 0, "有真结果就不该再叫下一级"


def test_抓到但不相关_不去叫不可达的备胎(monkeypatch):
    calls = _stub(monkeypatch, bing=[NOTE], ddg=[_good(9)])
    r = S.search("使命召唤 最新消息", 3)
    assert r[0]["source"] == "filter"
    assert calls["ddg"] == 0, "Bing 明确答过（只是不相关）属查询形态问题，白等 16s 无意义"


def test_引擎真死_必须降级到下一级(monkeypatch):
    calls = _stub(monkeypatch, bing=[], ddg=[_good(1)])
    r = S.search("使命召唤", 3)
    assert calls["ddg"] == 1, "零返回才是引擎故障，该降级"
    assert [x["title"] for x in r] == ["使命召唤1 官网"]


def test_两级全空时返回空列表而非硬塞(monkeypatch):
    calls = _stub(monkeypatch, bing=[], ddg=[])
    assert S.search("使命召唤", 3) == [], "什么都没抓到就该给空，不许编条目"
    assert calls["ddg"] == 1


def test_说明项会被标注备胎引擎真实状态(monkeypatch):
    monkeypatch.setitem(S._DDG_STATE, "dead", True)
    monkeypatch.setitem(S._DDG_STATE, "why", "call:TimeoutException")
    monkeypatch.setattr(S, "_via_bing_smart", lambda q, n: [dict(NOTE)])
    monkeypatch.setattr(S, "_via_ddg_gated", lambda q, n: [])
    r = S.search("使命召唤 最新消息", 3)
    assert "DuckDuckGo" in r[0]["body"] and "TimeoutException" in r[0]["body"]


def test_去重不误伤正常结果_且说明项不混入结果堆(monkeypatch):
    _stub(monkeypatch, bing=[_good(1), dict(_good(1))], ddg=[])
    r = S.search("使命召唤", 5)
    assert len(r) == 1, "同 href 必须去重"


def test_prefer_quality_优先叫_ddg(monkeypatch):
    calls = _stub(monkeypatch, bing=[_good(1)], ddg=[])
    S.search("使命召唤", 3, prefer_quality=True)
    assert calls["ddg"] == 1, "显式要求优先高质量引擎时，DDG 得先上"
