"""
前后端「名字对齐」契约测试（守护 2026-09-17 那次改名的两类回归）

背景（两次真实事故，都由本次 clarify→ask 改名暴露）：
  1. **前端构建产物没重建** → 后端发 `ask_request`、旧包还在监听 `clarify_request`，
     ask 卡永远不出现。
  2. **样式表不在改名的文件类型白名单里** → 组件 className 改成了 `ask-*`，
     而 styles.css 里还是 `.clarify-*`，选择器全部失配，卡片退化成裸样式。

根因是同一条：**跨层/跨文件的名字靠手工同步，没有任何断言守着**。这个文件把
"必须两边一致"的几组名字钉住 —— 任何一侧改名而另一侧没跟上，这里就红。

（注意：前端 store 的 `state.clarifies` 是 `test_ask_multi.py` 契约锁定的**内部字段名**，
 刻意不参与本次改名，所以下面的检查不碰它。）
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "agent_webui" / "frontend"
SRC = FRONTEND / "src"
BACKEND = ROOT / "agent_webui" / "backend"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_pipeline_background_pierces_every_layer():
    """跨层字段穿透：pipeline 事件的 background 必须在四层都活下来。

    事故背景（2026-09-29 真机）：bridge 发了它、OrchStrip 也读它、types.ts 也声明了，
    但 store 的事件处理是**显式挑字段**，漏了它 -> 橙色槽位永远不亮。
    判据取各层的真实代码形态，不是"文件名对上了"就算过。
    """
    bridge = _read(BACKEND / "bridge.py")
    store = _read(SRC / "store" / "appStore.tsx")
    orch = _read(SRC / "components" / "OrchStrip.tsx")
    types_ = _read(SRC / "types.ts")
    assert "background" in bridge, "bridge 必须把它发进 pipeline 事件"
    assert "background: evt.background" in store, \
        "store 必须接住它 —— 显式挑字段时漏掉 = 整条链路白干"
    assert "v.background" in orch, "OrchStrip 必须用它决定橙色"
    assert "background?: boolean" in types_, "types.ts 必须有类型声明"




# ============ 1. 组件 class 必须都在样式表里有定义 ============

def _classes_used_by(path: Path):
    """从 tsx 源码的 className 里抽出类名（去掉模板表达式）。"""
    text = _read(path)
    out = set()
    for m in re.finditer(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', text):
        raw = m.group(1) or m.group(2) or ""
        raw = re.sub(r"\$\{[^}]*\}", " ", raw)          # 模板表达式不是类名
        for tok in raw.split():
            tok = tok.strip("${}?.:'\"")
            if tok and re.fullmatch(r"[a-zA-Z][\w-]*", tok):
                out.add(tok)
    return out


def test_ask_card_classes_are_defined_in_css():
    """AskCard 用到的 ask-* 类名，每个都必须在 styles.css 里有定义。

    这条直接对应那次「太劣质了」：改了 className 没改选择器 → 样式全失配。
    """
    css = _read(SRC / "styles.css")
    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    used = {c for c in _classes_used_by(SRC / "components" / "AskCard.tsx") if c.startswith("ask")}
    assert used, "AskCard 里没抓到 ask-* 类名？检查提取逻辑"
    missing = sorted(used - defined)
    assert not missing, "这些类名在 styles.css 里没有定义（样式会丢）：%s" % missing


def test_no_stale_clarify_selectors_in_css():
    """样式表里不许再有 clarify 选择器（组件已经不用了）。"""
    css = _read(SRC / "styles.css")
    stale = re.findall(r"\.clarify[\w-]*", css)
    assert not stale, "styles.css 里还有失效选择器：%s" % sorted(set(stale))


def test_no_clarify_classname_left_in_frontend_sources():
    """组件源码里不许再出现 clarify 类名（除 store 那个契约字段 clarifies）。"""
    offenders = []
    for f in list(SRC.rglob("*.tsx")) + list(SRC.rglob("*.ts")):
        for i, line in enumerate(_read(f).splitlines(), 1):
            if re.search(r"\.clarify[\w-]*|className=[^>]*clarify", line):
                offenders.append("%s:%d" % (f.relative_to(ROOT).as_posix(), i))
    assert not offenders, "前端源码里还有 clarify 类名：%s" % offenders


# ============ 2. 后端发的事件名，前端必须认识 ============

def _backend_event_types():
    """解析 bridge.py 里 _EVENT_TYPES 字面量（静态解析，不导入模块）。"""
    text = _read(BACKEND / "bridge.py")
    m = re.search(r"_EVENT_TYPES\s*=\s*\{(.*?)\}", text, re.S)
    assert m, "没找到 _EVENT_TYPES 定义"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def _frontend_event_types():
    """解析 types.ts 里 WsEventType 联合的字符串字面量。"""
    text = _read(SRC / "types.ts")
    m = re.search(r"WsEventType\s*=(.*?)(?:\n\nexport|\Z)", text, re.S)
    assert m, "没找到 WsEventType 定义"
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_backend_events_are_known_to_frontend():
    """后端会发出的事件类型，前端白名单里必须都有 —— 否则事件被静默丢掉。"""
    backend = _backend_event_types()
    frontend = _frontend_event_types()
    assert backend, "后端事件表解析为空"
    unknown = sorted(backend - frontend)
    assert not unknown, ("后端会发这些事件、前端不认识（界面上什么都不会发生）：%s\n"
                         "  后端表：%s\n  前端表：%s" % (unknown, sorted(backend), sorted(frontend)))


def test_api_ts_allowlist_matches_types_ts():
    """api.ts 的运行时白名单必须与 types.ts 的类型联合一致（两处手工维护）。"""
    api = set(re.findall(r"'([a-z_]+)'", _read(SRC / "api.ts")))
    types_ = _frontend_event_types()
    missing = sorted(types_ - api)
    assert not missing, "types.ts 声明了但 api.ts 白名单里没有（运行时会丢）：%s" % missing


# ============ 3. 路由/选择器：改名后不许留旧名 ============

def test_ask_routes_registered_and_no_clarify_routes():
    api_py = _read(BACKEND / "api.py")
    assert "/ask/answer" in api_py and "/ask/pending" in api_py
    assert "/clarify/answer" not in api_py and "/clarify/pending" not in api_py


def test_verify_scripts_use_current_dom_selectors():
    """.mjs 验收脚本里的 DOM 选择器必须跟当前组件类名一致（它们曾整批失效）。"""
    offender = []
    for f in (ROOT / "agent_webui" / "scripts").glob("*.mjs"):
        text = _read(f)
        if re.search(r"querySelector(?:All)?\(\s*['\"]\.clarify", text):
            offender.append(f.name)
    assert not offender, "这些验收脚本还在用 .clarify 选择器（会永远查不到卡）：%s" % offender


def test_verify_scripts_use_current_event_names():
    offender = []
    for f in (ROOT / "agent_webui" / "scripts").glob("*.mjs"):
        text = _read(f)
        if "'clarify_request'" in text or '"clarify_request"' in text:
            offender.append(f.name)
    assert not offender, "验收脚本还在用旧事件名 clarify_request：%s" % offender
