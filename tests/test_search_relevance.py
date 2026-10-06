# -*- coding: utf-8 -*-
"""search 工具相关性过滤回归集（纯离线，不联网）。

跑法（项目根）：
    venv/Scripts/python -m pytest tests/test_search_relevance.py -q

存在理由：2026-09-16 实测 Bing 中文把空格分隔的多词查询拆坏 ——
「使命召唤 最新消息」返回 2005 年刑侦剧《使命》与百度百科"使命"词条，
而旧版把整批结果拼成一个 blob 算一个分，只要有一条沾到查询就整批放行。
这里的靶子是当天真实抓到的页面文本，钉住「垃圾必须被逐条拒掉」。

零绝对路径：项目根运行期取，clone 到任何机器都能跑。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent_tools            # noqa: F401  先加载包
import agent_tools.search     # noqa: F401  注册进 sys.modules
# 坑：agent_tools/__init__.py 的 from .search import search 把子模块名遮蔽成同名函数，
#     `import agent_tools.search as S` 拿到的是函数，必须从 sys.modules 取真模块
S = sys.modules["agent_tools.search"]


def _it(t, b="", h="https://example.com/"):
    return {"title": t, "body": b, "href": h, "source": "bing"}


# (查询, [(结果, 期望是否通过)]) —— 2026-09-16 真实抓到的页面
CASES = [
 ("使命召唤 最新消息", [
   (_it("使命召唤手游官方网站-腾讯游戏", "游戏将以最高质量的视觉效果呈现，高度还原使命召唤系列的经典玩法地图角色", "https://codm.qq.com/main.shtml"), True),
   (_it("《使命》全集-电视剧-免费在线观看 - Sogou", "电视剧《使命》高清免费在线播放，更新第18集，由何群导演，张嘉译主演", "https://movie.sogou.com/x.html"), False),
   (_it("使命_01_电视剧_高清完整版视频在线观看_腾讯视频", "《使命》高清在线观看，领衔主演:张嘉益，该剧讲述了清水市公安局长", "https://v.qq.com/x.html"), False),
   (_it("使命（汉语词语）_百度百科", "曹禺等《胆剑篇》：我奉大王使命，和范大夫办理此事。应尽任务，应尽责任", "https://baike.baidu.com/item/使命/14529"), False),
   (_it("使命剧情介绍（1-21全集）大结局_电视剧_电视猫", "电视剧《使命》是一部刑侦题材的电视剧，主要讲述了清水市近来的治安混乱", "https://www.tvmao.com/drama"), False),
 ]),
 ("北京 今日天气", [
   (_it("【北京今天天气预报】北京天气预报24小时详情_北京天气网", "北京天气网为您提供北京天气预报24小时详情，包括今日实时温度、降水概率、湿度", "https://www.tianqi.com/beijing/today/"), True),
   (_it("北京天气预报,北京7天天气预报,北京15天天气预报", "北京天气预报，及时准确发布中央气象台天气信息，便捷查询北京今日天气", "https://www.weather.com.cn/weather/101010100.shtml"), True),
 ]),
 ("python asyncio TaskGroup best practices", [
   (_it("Welcome to Python.org", "The mission of the Python Software Foundation is to promote, protect, and advance the Python programming language", "https://www.python.org/"), False),
   (_it("TaskGroup — asyncio documentation", "asyncio.TaskGroup created at the top level of this function, this creates a new task group", "https://docs.python.org/3/library/asyncio-task.html"), True),
 ]),
 ("latest MCP protocol specification changes 2026", [
   (_it("latest是什么意思_latest的翻译_音标_读音_用法_例句", "爱词霸权威在线词典,为您提供latest的中文意思,latest的用法讲解,latest的读音", "https://www.iciba.com/word?w=latest"), False),
   (_it("LATEST中文(简体)翻译：剑桥词典", "A decision is expected by the end of May at the latest", "https://dictionary.cambridge.org/zhs/latest"), False),
   (_it("Model Context Protocol specification revision 2026 changelog", "The MCP protocol transport changed in this specification revision", "https://modelcontextprotocol.io/changelog"), True),
 ]),
 ("AetherBreath agent learning hub", [
   (_it("Walmart Supercenter in Chicago, IL | Grocery, Electronics, Toys", "Get Walmart hours, driving directions and check out weekly specials", "https://www.walmart.com/store/5402-chicago-il"), False),
 ]),
 ("使命召唤：黑色行动7", [
   (_it("使命召唤：黑色行动7_百度百科", "该作为《使命召唤》系列第22部主要作品，也是《黑色行动》子系列的第七部作品", "https://baike.baidu.com/item/x"), True),
   (_it("《使命召唤®：黑色行动 7》|官网", "在《使命召唤：黑色行动 7》中，Treyarch与Raven Software将为玩家呈现有史以来最大规模的《黑色行动》", "https://www.callofduty.com/cn/zh/blackops7"), True),
   (_it("使命召唤7黑色行动_使命召唤7黑色行动下载", "《使命召唤7：黑色行动》是系列第七部作品，由Treyarch开发，于2010年11月9日发行", "https://www.ali213.net/zt/cod7/"), True),
 ]),
 # 四字查询也能产出 3 个强 gram，门槛照常生效：讲汉语词"使命"的词条必须拒掉
 ("使命召唤", [
   (_it("使命召唤手游官方网站-腾讯游戏", "高度还原使命召唤系列的经典玩法", "https://codm.qq.com/"), True),
   (_it("使命（汉语词语）_百度百科", "应尽任务，应尽责任", "https://baike.baidu.com/item/使命/14529"), False),
 ]),
]


def _ids():
    out = []
    for q, rows in CASES:
        for i, (_r, want) in enumerate(rows):
            out.append("%s#%d(want=%s)" % (q[:14], i, want))
    return out


import pytest

@pytest.mark.parametrize("case", range(sum(len(r) for _, r in CASES)), ids=_ids())
def test_逐条相关性判定(case):
    idx, pos = case, 0
    for q, rows in CASES:
        if idx < len(rows):
            item, want = rows[idx]
            break
        idx -= len(rows)
    got = S._score_batch(q, [dict(item)])[0]["_ok"]
    assert got == want, "%r 判定应为 %s，实际 %s" % (item["title"], want, got)


def test_过滤后绝不硬塞低质结果():
    junk = [_it("使命（汉语词语）_百度百科", "应尽任务，应尽责任"),
            _it("《使命》全集-电视剧", "电视剧《使命》高清在线")]
    kept, stats = S._filter_by_relevance("使命召唤 最新消息", junk)
    assert kept == [], "全是垃圾时必须一条不剩"
    assert stats["raw"] == 2 and stats["kept"] == 0
    note = S._low_relevance_note("使命召唤 最新消息", stats)
    assert note["source"] == "filter" and note["href"] == ""
    assert "⚠️" in note["title"], "说明项必须一眼可辨，不能被当成搜索结果"


def test_中文查询的建议不带英文话术_反之亦然():
    st = {"raw": 10, "kept": 0, "strong": 5, "best_cov": 0.2, "best_long": 6, "samples": []}
    zh = S._low_relevance_note("使命召唤 最新消息", st)["body"]
    en = S._low_relevance_note("latest mcp protocol spec", st)["body"]
    assert "连续短语" in zh and "改成连续短语" not in en, "对英文查询讲中文分词建议是胡话"
    assert "多词英文查询" in en and "多词英文查询" not in zh


def test_内部评分字段不外泄():
    kept, _ = S._filter_by_relevance("使命召唤", [_it("使命召唤手游官方网站", "使命召唤")])
    assert kept and not [k for k in kept[0] if k.startswith("_")]


def test_单字信号已彻底弃用():
    strong, weak = S._query_grams("使命召唤 最新消息")
    assert all(len(g) >= 2 for g in strong + weak), "不能再有单字信号"
    assert "使命召唤" in strong and "使命" in weak
