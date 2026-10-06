#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：验证 verify.check_compile 真的会"失败"（双向对照）

怎么跑（项目根）：
    venv/Scripts/python.exe self_maintenance/packs/verify-probe/probe_compile.py

副作用：在 self_maintenance/workbench/ 下临时造两个探针文件，跑完自动删除。
        不碰生产文件、不联网、不改配置。

为什么需要它：检查类代码最隐蔽的坏法是"永远返回通过"。
2026-09-17 踩过：py_compile(..., quiet=2) 在 Python 3.11.15 上吞掉语法错，
语法检查一路报合格。单向测试救不了这个，必须双向对照。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[3]      # <root>/self_maintenance/packs/verify-probe/x.py
TOOLS = ROOT / "self_maintenance" / "tools"
WB = ROOT / "self_maintenance" / "workbench"
sys.path.insert(0, str(TOOLS))
import verify  # noqa: E402


def main() -> int:
    WB.mkdir(parents=True, exist_ok=True)
    bad = WB / "_probe_bad_syntax.py"
    good = WB / "_probe_good_syntax.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8", newline="\n")
    good.write_text("def f():\n    return 1\n", encoding="utf-8", newline="\n")
    try:
        ok_bad, detail_bad = verify.check_compile([bad])
        ok_good, detail_good = verify.check_compile([good])
        print("bad  file ->", ok_bad, "|", str(detail_bad).replace("\n", " "))
        print("good file ->", ok_good, "|", str(detail_good).replace("\n", " "))
        if ok_bad is not False:
            print("FAIL: checker missed a broken file (silent-failure relapse)")
            return 1
        if ok_good is not True:
            print("FAIL: checker rejected a valid file (reverse failure)")
            return 1
        print("PASS: both directions hold")
        return 0
    finally:
        for p in (bad, good):
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
