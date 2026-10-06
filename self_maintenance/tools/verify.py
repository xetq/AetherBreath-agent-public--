#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自维护验收 —— 维护做完后，判断"这次手术有没有把自己弄坏"（纯标准库）

三层，全部不调 LLM、不启动对话：
    1) 语法编译：改动过的 .py 能不能编译（比 import 更早发现问题，且无副作用）
    2) 回归测试：pytest tests -q（项目的判据是「一条命令、无需 LLM、不写真实文件」）
    3) 冷导入自检：核心模块能不能导入、工具表与 schema 是否对齐、审批规范有没有加载失败

第四项「AB 能启动并完成一次简单任务」**刻意不自动化**：那要用刚被改过的系统去验证
它自己，改坏启动路径时只会给出假信心。它由人在 WebUI 里按 MANUAL.md 确认。

用法：
    python self_maintenance/tools/verify.py                    # 全量：核心目录全查
    python self_maintenance/tools/verify.py --snapshot 20260917-205012-修复MCP
                                                               # 只查那次维护动过的文件
    python self_maintenance/tools/verify.py --fast             # 跳过 pytest（只语法+导入）
"""
from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SELF_DIR = PROJECT_ROOT / "self_maintenance"
LOG_FILE = SELF_DIR / "logs" / "maintenance.log"
# 基线已知失败清单：让验收能区分「本次维护弄坏了」与「本来就红」
BASELINE_FILE = SELF_DIR / "baseline_failures.json"

# 全量模式下要检查的代码目录（项目主体的真实代码；工作区实验物不查）
CODE_DIRS = ("agent", "agent_tools", "agent_MCP", "agent_webui/backend",
             "tests", "self_maintenance/tools")
SKIP_SEGMENTS = {"venv", "node_modules", "__pycache__", ".git"}

# 冷导入自检：跑在**子进程**里（导入失败不能污染本进程），也绝不 import agent.py 主循环
# 注意 approval.self_check() 的真实结构：specs 是 {"loaded": [...], "failed": [...]}
# —— 曾把它当 list 用，len() 得到的是 dict 的**键数 2**，于是"8 类规范"被报成"2 类"。
IMPORT_PROBE = r'''
import sys, json
sys.path.insert(0, "agent")
out = {"ok": True, "errors": [], "tools": 0, "schemas": 0,
       "specs": None, "spec_errors": [], "checks_failed": [], "problems": []}
try:
    import agent_tools
    out["tools"] = len(agent_tools.AVAILABLE_TOOLS)
    out["schemas"] = len(agent_tools.TOOLS_SCHEMA)
except Exception as e:
    out["ok"] = False
    out["errors"].append("agent_tools 导入失败: %s: %s" % (type(e).__name__, e))
try:
    import approval
    sc = approval.self_check()
    sp = sc.get("specs") or {}
    out["specs"] = len(sp.get("loaded") or [])
    out["spec_errors"] = list(sp.get("failed") or [])
    out["problems"] = list(sc.get("problems") or [])
    # checks 每条是 [是否通过, 结果]；任何一条 false 都说明审批判定行为变了
    out["checks_failed"] = [k for k, v in (sc.get("checks") or {}).items()
                            if not (isinstance(v, (list, tuple)) and bool(v[0]))]
    if not sc.get("ok") or out["spec_errors"] or out["checks_failed"] or out["problems"]:
        out["ok"] = False
except Exception as e:
    out["ok"] = False
    out["errors"].append("approval 导入/自检失败: %s: %s" % (type(e).__name__, e))
print(json.dumps(out, ensure_ascii=False))
'''


def py_files_in(dirs) -> List[Path]:
    out: List[Path] = []
    for d in dirs:
        p = PROJECT_ROOT / d
        if p.is_file() and p.suffix == ".py":
            out.append(p)
            continue
        if not p.is_dir():
            continue
        for f in p.rglob("*.py"):
            if any(seg in f.parts for seg in SKIP_SEGMENTS):
                continue
            out.append(f)
    return sorted(set(out))


def changed_py_from_snapshot(name: str) -> Tuple[List[Path], Optional[str]]:
    """从快照 manifest 取"本次维护动过的文件"，只留 .py。"""
    snap = SELF_DIR / "snapshots" / name
    mf = snap / "manifest.json"
    if not mf.exists():
        return [], f"找不到快照 {name}（用 snapshot.py list 看有哪些）"
    data = json.loads(mf.read_text(encoding="utf-8"))
    ch = data.get("changes") or {}
    rels = list(ch.get("modified", [])) + list(ch.get("added", []))
    files = [PROJECT_ROOT / r for r in rels
             if r.endswith(".py") and (PROJECT_ROOT / r).is_file()]
    return sorted(set(files)), None


def check_compile(files: List[Path]) -> Tuple[bool, str]:
    if not files:
        return True, "没有 .py 改动（跳过）"
    bad: List[str] = []
    with tempfile.TemporaryDirectory() as td:
        for f in files:
            try:
                # ⚠ 绝对不要传 quiet=2！实测 Python 3.11.15 上
                # `py_compile.compile(..., doraise=True, quiet=2)` 会**静默吞掉语法错误**
                # （返回 None、不抛异常）→ 语法检查永远报"合格"，是本工具最危险的一种失效。
                # 被 tests/test_self_maintenance.py::test_verify_compile_check_reports_syntax_error 钉死。
                py_compile.compile(str(f), cfile=str(Path(td) / "probe.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                err = str(e).strip().splitlines()
                bad.append(f"{f.relative_to(PROJECT_ROOT).as_posix()}: {err[-1][:160] if err else e}")
    if bad:
        return False, f"{len(bad)}/{len(files)} 个文件编译失败：\n      " + "\n      ".join(bad[:8])
    return True, f"{len(files)} 个 .py 全部可编译"


def check_pytest(timeout: int = 420) -> Tuple[bool, str, List[str]]:
    """跑回归测试。返回 (是否全绿, 摘要, 失败用例 ID 列表)。

    ⚠ 为什么必须把失败 ID 单独取出来：本项目的测试集**在基线时就有已知红**
    （2026-09-17 实测 tests/test_shell_path_env.py 4 条，属"shell PATH 修复"工作线的
    未完成部分）。不看 ID 只看"有没有 failed"，验收工具就分不清
    「本次维护改坏了」和「本来就坏」—— 那不是验收，那是每次维护都报红。
    """
    py = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    exe = str(py) if py.exists() else sys.executable
    try:
        p = subprocess.run([exe, "-m", "pytest", "tests", "-q"], cwd=str(PROJECT_ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"pytest 超过 {timeout}s 未结束（可能卡死）", []
    tail = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    summary = tail[-1] if tail else "(无输出)"
    failed = []
    for ln in tail:
        if ln.startswith("FAILED "):
            failed.append(ln[len("FAILED "):].split(" - ")[0].strip())
    return (p.returncode == 0), summary[:200], failed


def load_baseline() -> Dict[str, object]:
    if BASELINE_FILE.exists():
        try:
            return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_baseline(failed: List[str], summary: str) -> None:
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps({
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "failed": sorted(failed),
        "why": "这些是**基线时就已经红**的用例。验收时只有'新增失败'才算本次维护弄坏了；"
               "修好其中某条后重跑 --save-baseline 即可刷新此清单。",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def check_import() -> Tuple[bool, str]:
    py = PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
    exe = str(py) if py.exists() else sys.executable
    try:
        p = subprocess.run([exe, "-X", "utf8", "-c", IMPORT_PROBE], cwd=str(PROJECT_ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return False, "冷导入超过 120s（可能卡在模型加载/网络）"
    line = (p.stdout or "").strip().splitlines()
    if not line:
        return False, f"自检无输出；stderr: {(p.stderr or '')[-300:]}"
    try:
        d = json.loads(line[-1])
    except json.JSONDecodeError:
        return False, f"自检输出无法解析：{line[-1][:200]}"
    detail = (f"工具表 {d['tools']} 个 / schema {d['schemas']} 个"
              + (f"；审批规范 {d['specs']} 类已加载" if d.get("specs") else ""))
    if d["tools"] != d["schemas"]:
        return False, detail + f"\n      ⚠ 工具数与 schema 数不一致（{d['tools']} vs {d['schemas']}）"
    if not d["ok"]:
        msgs = (d.get("errors") or []) + (d.get("spec_errors") or [])
        msgs += [f"审批内置自检未通过：{k}" for k in (d.get("checks_failed") or [])]
        msgs += [f"自检问题：{p}" for p in (d.get("problems") or [])]
        return False, detail + "\n      " + "\n      ".join(str(m)[:200] for m in msgs[:6])
    return True, detail + "；内置自检全过"


def log_line(kind: str, text: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {kind:<8} {text}\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="自维护验收：语法 + 回归测试 + 冷导入自检")
    ap.add_argument("--snapshot", help="只验收某次快照改动过的文件")
    ap.add_argument("--fast", action="store_true", help="跳过 pytest（只跑语法与导入）")
    ap.add_argument("--save-baseline", action="store_true",
                    help="把当前 pytest 失败登记为「基线已知红」（维护前跑一次）")
    args = ap.parse_args(argv)

    if args.save_baseline:
        ok, summary, failed = check_pytest()
        save_baseline(failed, summary)
        print(f"■ 基线已登记：{len(failed)} 条已知失败 → "
              f"{BASELINE_FILE.relative_to(PROJECT_ROOT).as_posix()}")
        for f in failed:
            print(f"    · {f}")
        print(f"  pytest 摘要：{summary}")
        print("  之后 verify 只会把「新增失败」判为不合格。")
        log_line("BASELINE", f"登记已知失败 {len(failed)} 条")
        return 0

    print("■ 自维护验收")

    files: List[Path] = []
    if args.snapshot:
        files, err = changed_py_from_snapshot(args.snapshot)
        if err:
            print(f"  ❌ {err}")
            return 2
        print(f"  范围：快照 {args.snapshot} 改动过的 {len(files)} 个 .py")
    else:
        files = py_files_in(CODE_DIRS)
        print(f"  范围：核心代码目录全量（{len(files)} 个 .py）")

    results: List[Tuple[str, bool, str]] = []
    ok, detail = check_compile(files)
    results.append(("语法编译", ok, detail))
    print(f"  [1/3] 语法编译 … {'✅ 合格' if ok else '❌ 不合格'}\n      {detail}")

    if args.fast:
        print("  [2/3] 回归测试 … ⏭ 已跳过（--fast）")
    else:
        ok, detail, failed = check_pytest()
        bl = load_baseline()
        known = set(bl.get("failed") or [])
        if not ok and known:
            new_fails = [f for f in failed if f not in known]
            fixed = [f for f in known if f not in failed]
            if not new_fails:
                # 只有"本来就红"的那些 —— 不算本次回归，但要让人看见它们还在
                ok = True
                detail += f"\n      ⚠ 这 {len(failed)} 条是**基线已知红**，不算本次回归："
                detail += "".join(f"\n        · {f}" for f in failed[:6])
            else:
                detail += "\n      ❌ 新增失败（= 本次维护弄坏的）："
                detail += "".join(f"\n        · {f}" for f in new_fails[:6])
            if fixed:
                detail += (f"\n      ✓ 另有 {len(fixed)} 条基线红已修好 —— "
                           f"修完记得重跑 --save-baseline 刷新清单")
        elif ok and known:
            detail += (f"\n      ✓ 基线里的 {len(known)} 条已知红现在全绿了 —— "
                       f"重跑 --save-baseline 刷新清单")
        results.append(("回归测试", ok, detail))
        mark = "✅ 合格" if ok else "❌ 不合格"
        print(f"  [2/3] 回归测试 … {mark}\n      {detail}")

    ok, detail = check_import()
    results.append(("冷导入自检", ok, detail))
    print(f"  [3/3] 冷导入自检 … {'✅ 合格' if ok else '❌ 不合格'}\n      {detail}")

    bad = [r[0] for r in results if not r[1]]
    print()
    if bad:
        print(f"  ── 结论：未通过（{('、'.join(bad))}）")
        print("     先别继续。若你已在维护前 `snapshot.py begin`，可用 rollback 整份还原：")
        print("       python self_maintenance/tools/snapshot.py list")
        print("       python self_maintenance/tools/snapshot.py rollback <快照名>        # 先看计划")
        print("       python self_maintenance/tools/snapshot.py rollback <快照名> --yes  # 再执行")
        print("     回滚前建议手动把关键文件另存一份 —— 手动备份永远最靠谱。")
        log_line("VERIFY", f"❌ 未通过：{'、'.join(bad)}")
        return 1

    print("  ── 结论：三项全过")
    print("     还差最后一步（不可自动化）：**人工**在 WebUI 里开新对话，让它完成一件小事，")
    print("     确认 AB 真的还能跑起来。这一条必须由人确认，见 MANUAL.md「维护后必做」。")
    log_line("VERIFY", "✅ 三层全过（语法 / 回归测试 / 冷导入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
