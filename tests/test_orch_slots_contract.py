# -*- coding: utf-8 -*-
"""点阵视觉契约：**格子数恒定，亮点只能把暗点点亮**（主人 2026-09-30）。

现象（主人原话）：
> 任务编排器亮起点，应该是在**暗点的基础上**点亮，而非**多加橙色点**。

代码里恰好有两处会新增格子（都在 `OrchStrip.tsx`）：
  ① `put()`：线程号超出上报容量时"补一个槽位"；
  ② P5 加的 loose 分支：认不出槽位的跨回合作业单独补一格。
于是"1 串 + 16 并"的底grid 之外会冒出额外橙点。

本文件从**源码层**钉住这两处不许回来；运行时行为由
`self_maintenance/packs/orch-orange-probe/probe_orch_slots.mjs` 真跑 `buildSlots()` 判。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "agent_webui" / "frontend" / "src"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_orchstrip_exports_pure_slot_builder():
    """点阵的"哪一格亮"必须是**可离线判的纯函数** —— 否则视觉契约只能靠肉眼。"""
    strip = _read(SRC / "components" / "OrchStrip.tsx")
    assert "export function buildSlots" in strip, (
        "没有导出 buildSlots：视觉契约无法被探针真跑（只能靠源码里有没有某个词）")


def test_orchstrip_never_grows_the_grid():
    strip = _read(SRC / "components" / "OrchStrip.tsx")
    assert "补一个槽位" not in strip and "补一个，绝不丢信息" not in strip, (
        "`put()` 还在「超出容量就补一格」—— 那就是主人看到的多余橙点")
    # 认不出槽位的作业只能**吸收进空闲格**或计入未映射，不许自己 map.set 新格子
    assert "job#" not in strip, "还在给未映射的作业新建槽位键（job#…）"
    assert "loose = true" in strip or "loose: true" in strip, \
        "未映射的作业必须仍被显示（占一个空闲格并标 loose），不能变成看不见的黑箱"


def test_orchstrip_reports_unmapped_in_header():
    strip = _read(SRC / "components" / "OrchStrip.tsx")
    assert "unmapped" in strip, "溢出的作业没有计数 —— 池满时界面会瞒报"
    assert "未映射" in strip, "头部没有把「未映射」如实说出来"
