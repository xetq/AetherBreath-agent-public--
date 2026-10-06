# -*- coding: utf-8 -*-
"""P14：并行槽位的**颜色污染**（2026-10-02 真机）。

主人原话：

> 若当前管道运行过"后台任务"，那么它就会被"后台任务"所对应的橙色指示灯污染，
> 以后再在这个管道运行并行任务时，就无法亮起并行任务所对应的绿色指示灯，而是橙色。

**根因**：颜色是从一个**粘性槽位标记**算出来的 ——

```ts
const place = (v) => {
  if (v.background) s.job = true      // ★ 只要这一格**历史上**出现过 background 条目就置位
  ...
}
// 渲染：const cls = on ? (s.job ? 'job' : ...) : 'idle'
```

线程会被复用（同一格先跑后台作业、后跑并行任务是常态），store 又把已结束的后台条目
留在了 `pipes` 里（落回 idle，但**保留 background 标记**）——
于是"橙色"变成了这一格的**永久属性**。

修法一句话：**颜色只由"当前在跑的那条"决定；非忙条目绝不参与颜色判定**；
并且在条目结束/落回空闲时**摘掉 background 标记**（那标记描述"此刻"，不是历史）。

本文件分两层判据：
  · 源码契约（防回归：粘性写法不许再出现、结束时要摘标记）；
  · **运行时真跑**（调 node 跑离线探针 probe_orch_slots.mjs 的 11 条，它构造的就是真机那一屏）。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "agent_webui" / "frontend" / "src"
ORCH = SRC / "components" / "OrchStrip.tsx"
STORE = SRC / "store" / "appStore.tsx"
PROBE = ROOT / "self_maintenance" / "packs" / "orch-orange-probe" / "probe_orch_slots.mjs"


def _code_only(src: str) -> str:
    """去掉行注释再判据 —— 注释里会引用旧写法（讲清"为什么改"），别把说明当成代码。"""
    keep = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        keep.append(ln.split("//")[0] if "//" in ln else ln)
    return "\n".join(keep)


def test_color_comes_from_the_current_entry_not_from_history():
    """**核心契约**：颜色必须是"当前在跑那条"的属性，不是槽位的历史。"""
    code = _code_only(ORCH.read_text(encoding="utf-8"))
    assert "s.job = !!v.background" in code, "颜色不再由当前条目赋值"
    assert "if (v.background) s.job = true" not in code, \
        "粘性写法回来了 —— 这一格跑过后台作业就会永久变橙"


def test_non_busy_entries_never_touch_color():
    """非忙条目（已结束/空闲/陈旧）只能贡献「上次是谁」与失败数，不许碰颜色。"""
    src = ORCH.read_text(encoding="utf-8")
    place = src[src.index("const place = (v: PipeView)"):src.index("if (real) {")]
    body = place[place.index("} else {"):]
    body = body[:body.index("}")]
    assert "s.job" not in body, "非忙条目还在改颜色（历史条目又会影响当前显示）"


def test_store_clears_background_flag_when_entry_settles():
    """纵深防御：条目结束/落回空闲时，`background` 标记必须被摘掉。"""
    src = STORE.read_text(encoding="utf-8")
    idle = src[src.index("function idleAll"):src.index("function onEvent")]
    assert "background: false" in idle, "idleAll 没摘掉 background 标记（历史条目带着橙色属性）"
    merge = src[src.index("case 'merge_orch'"):]
    merge = merge[:merge.index("case '", 10)]
    assert merge.count("background: false") >= 2, \
        "merge_orch 对「服务端说它不在跑了」的条目没摘掉 background（落回空闲却留着橙色标记）"


def test_orch_strip_runtime_probe_passes():
    """**运行时真跑**：node 跑离线探针（含颜色污染那三条）。

    探针构造的就是真机那一屏：某一格跑过后台作业（橙色条目还落在视图里），
    同一格现在跑前台并行任务 —— 必须回到绿色；而当前真的在跑后台作业时仍必须橙色。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，跳过运行时探针（源码契约仍由上面两条守着）")
    r = subprocess.run([node, str(PROBE)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180,
                       cwd=str(ROOT))
    tail = (r.stdout or "")[-800:]
    assert r.returncode == 0, "探针未通过：\n%s" % tail
    assert "通过 (11/11)" in r.stdout, "探针结论行不见了（是不是改了用例数？）\n%s" % tail
