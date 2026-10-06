#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自维护快照 —— 文件级前像 + 指纹变更报告（纯标准库，零依赖）

为什么不直接用 git？
    AB 要能交给"可能连 git 都没装"的用户运行。git 是更好的网，但它是前提而不是
    保障。所以这里自备一套最小快照：**维护前把「要动的文件」复制一份前像**，
    再对全项目做一次轻量指纹扫描，维护后对比出「实际改了哪些文件」——包括没
    声明却动了的（意外的改动会被点名）。

    真话：它比 git 弱。没有内容寻址、没有历史链、没有三方合并。它只保证一件事：
    维护搞砸时，被改的文件能整份还原。README 里仍建议装 git 做第二道网。

用法（项目根或其下任意位置执行皆可）：
    python self_maintenance/tools/snapshot.py begin --name "修复MCP超时" \
        --paths agent/mcp_client.py agent/approvals
    python self_maintenance/tools/snapshot.py end
    python self_maintenance/tools/snapshot.py status
    python self_maintenance/tools/snapshot.py list
    python self_maintenance/tools/snapshot.py rollback 20260917-205012-修复MCP超时        # 只打印计划
    python self_maintenance/tools/snapshot.py rollback 20260917-205012-修复MCP超时 --yes  # 真回滚

约定：回滚永不自动执行，必须人工带 --yes。维护收尾要看 end 的输出——它会告诉你
有没有「范围外的意外改动」，那种改动没有前像，回滚救不回来。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:                                            # Windows 控制台默认 GBK，中文会炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SELF_DIR = PROJECT_ROOT / "self_maintenance"
SNAP_DIR = SELF_DIR / "snapshots"
LOG_FILE = SELF_DIR / "logs" / "maintenance.log"
CURRENT_PTR = SNAP_DIR / ".current"

# ---------------- 扫描排除清单 ----------------
# 两类东西不进指纹扫描：
#   1) 次生物 / 运行期天天变的（日志、会话、缓存）—— 它们每扫描一次都会报"被改过"，
#      噪音会淹掉真信号。主人的原话："那些东西算是次生物，终究会删掉的"。
#   2) 可重建的巨大目录（venv、第三方解压源码）—— 扫它们只是浪费几秒。
# ⚠ 别用裸目录名做匹配！`src`/`logs`/`cache` 这类名字在真项目里到处都是
#   （agent_webui/frontend/src 就是前端真代码）—— 曾差点把前端源码整个排除。
#   段名只列全局唯一的运行时目录；一次性/带歧义的路径写进下面的精确前缀表。
EXCLUDE_SEGMENTS = {
    ".git", "__pycache__", ".pytest_cache", "node_modules",
    "venv", "venv-gateway", ".venv", "envs",
    "hf_cache", "agent_logs", ".trash", ".state",
}
# agent_memory 下两处是次生物（会话原文与压缩视图）；自维护系统自身的运行区同理
EXCLUDE_REL_PREFIXES = (
    "agent_memory/working_memory",
    "agent_memory/.condensed_sessions",
    "self_maintenance/snapshots",
    "self_maintenance/workbench",
    "self_maintenance/logs",
    "agent_tools/cache",                       # 工具运行缓存
    "agent_webui/logs",                        # 网关运行日志
    "agent_webui/frontend/dist",               # 前端构建产物
    "agent_workspace/nlp",                     # 758MB 课程解释器
    "agent_workspace/headroom集成评估/src",     # 解压源码（同目录 tar.gz 可重建）
    "agent_workspace/github_mcp_probe/src",    # 第三方探针源码
    "agent_workspace/扩展包",                   # 第三方源码包（JMComic 等），非本项目代码
    "agent_workspace/.tmp", "agent_workspace/.tmp_exec",
)
# 这类目录在项目里**不止一处**（subagents/多agent系统/*/agent_memory/working_memory 也有），
# 所以用子串匹配而不是前缀匹配 —— 曾因为只写前缀，让子代理的会话文件混进指纹（3 万条里藏次生物）。
EXCLUDE_SUBSTRINGS = (
    "agent_memory/working_memory",
    "agent_memory/.condensed_sessions",
)
# 密钥类：**绝不复制进快照**（否则明文密钥落进 snapshots/，一旦被提交或分享就是事故）。
# 注意：指纹里仍会记 .env 的路径/size —— 那只是元数据（无内容），留着有用：维护若动了
# 密钥文件，end 会点名它（虽然没前像，至少你知道自己被碰了）。
SECRET_SUFFIXES = (".env", ".pem", ".key", ".pfx", ".p12")
# 大文件不入指纹（二进制资产，维护不会去改它们；要改自己手动备份）
MAX_FINGERPRINT_BYTES = 4 * 1024 * 1024


def _excluded(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    low = rel.lower()
    for sub in EXCLUDE_SUBSTRINGS:
        if sub in low:
            return True
    for pre in EXCLUDE_REL_PREFIXES:
        if low == pre or low.startswith(pre + "/"):
            return True
    for seg in low.split("/"):
        # 虚拟环境：名字花样太多（venv / venvs / venv-gateway / .venv / envs）。
        # 实测只写死 "venv" 时，agent_workspace/venvs/ 里的 2.4 万个 py/pyi 会把
        # 指纹从 3 千条抬到 3.1 万条（manifest 每次白写 1.8MB）。
        if seg in EXCLUDE_SEGMENTS or "venv" in seg or seg.endswith(".egg-info"):
            return True
    if low.endswith((".pyc", ".pyo")):
        return True
    return False


def is_secret(rel: str) -> bool:
    """密钥类文件——快照拒收，回滚也拒收。

    例外：`.env.example` 是模板（明确不含真实密钥，仓库里本来就有），它应当能进快照 ——
    否则维护"补一个环境变量模板"这个常见动作就没法回滚。
    """
    name = os.path.basename(rel.replace("\\", "/")).lower()
    if name.endswith(".example"):
        return False
    return name.endswith(SECRET_SUFFIXES) or name.startswith(".env")


def fingerprint_tree(root: Path) -> Dict[str, Tuple[int, int]]:
    """全项目轻量指纹：相对路径 -> (字节数, mtime_ns)。

    只读元数据、不读内容 —— 几千文件毫秒级。够用来回答"这次维护动了哪些文件"。
    """
    out: Dict[str, Tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames
                       if not _excluded((rel_dir + "/" + d) if rel_dir else d)]
        for fn in filenames:
            rel = (rel_dir + "/" + fn) if rel_dir else fn
            if _excluded(rel):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size > MAX_FINGERPRINT_BYTES:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns)
    return out


def diff_fingerprint(before: Dict, after: Dict) -> Dict[str, List[str]]:
    """两次指纹的差集：谁被改了、谁是新加的、谁消失了。

    ⚠ 必须先把值归一成 tuple：before 是从 manifest.json 读回来的，JSON 没有元组，
    (size, mtime) 会被存成 **list**；而 after 是新扫的 **tuple**。
    直接比 `[a,b] != (a,b)` 恒为真 —— 曾因此把全项目 6000 个文件报成「已修改」
    （实测：end 输出 6MB）。这不是理论风险，是踩过的坑。
    """
    b = {k: tuple(v) for k, v in before.items()}
    a = {k: tuple(v) for k, v in after.items()}
    bk, ak = set(b), set(a)
    return {
        "added": sorted(ak - bk),
        "deleted": sorted(bk - ak),
        "modified": sorted(k for k in (ak & bk) if b[k] != a[k]),
    }


def copy_into(snapshot_dir: Path, rel: str) -> int:
    """把一个文件/目录的前像复制进快照的 before/，返回复制文件数。"""
    src = PROJECT_ROOT / rel
    n = 0
    if src.is_file():
        if is_secret(rel):
            raise SystemExit(f"❌ 拒绝快照密钥类文件：{rel}（快照不存密钥，请手动备份）")
        dst = snapshot_dir / "before" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1
    if not src.exists():
        raise SystemExit(f"❌ 路径不存在：{rel}")
    for dirpath, dirnames, filenames in os.walk(src):
        for fn in filenames:
            p = Path(dirpath) / fn
            r = p.relative_to(PROJECT_ROOT).as_posix()
            if _excluded(r) or is_secret(r) or p.stat().st_size > MAX_FINGERPRINT_BYTES:
                continue
            dst = snapshot_dir / "before" / r
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            n += 1
    return n


def log_line(kind: str, text: str) -> None:
    """给用户看的自维护日志（人类可读、追加、不脱敏也无密钥）。"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {kind:<8} {text}\n")


def load_current() -> Optional[Path]:
    if not CURRENT_PTR.exists():
        return None
    name = CURRENT_PTR.read_text(encoding="utf-8").strip()
    d = SNAP_DIR / name
    return d if d.is_dir() else None


def read_manifest(d: Path) -> Dict:
    with open(d / "manifest.json", "r", encoding="utf-8") as f:
        return json.load(f)


def write_manifest(d: Path, m: Dict) -> None:
    tmp = d / "manifest.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, d / "manifest.json")


def in_scope(rel: str, paths: List[str]) -> bool:
    rel = rel.replace("\\", "/")
    for p in paths:
        p = p.replace("\\", "/").rstrip("/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


# ==================== 子命令 ====================

def cmd_begin(args) -> int:
    if load_current() is not None:
        print("❌ 已有一个进行中的快照。先 `end` 收尾，或删掉 "
              f"{CURRENT_PTR.relative_to(PROJECT_ROOT)} 后再开新的。")
        return 1

    name = args.name.strip()
    paths = [p.replace("\\", "/").rstrip("/") for p in (args.paths or [])]
    if not name:
        print("❌ --name 不能为空（它是回滚时认人的名字）")
        return 1
    if not paths:
        print("❌ --paths 至少要给一项：这次维护**打算**动什么（文件或目录）。")
        print("   例：--paths agent/mcp_client.py agent/approvals")
        return 1

    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:40]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap = SNAP_DIR / f"{stamp}-{safe}"
    snap.mkdir(parents=True, exist_ok=True)

    print(f"■ 开始快照：{snap.name}")
    print(f"  维护目标：{name}")
    print(f"  声明范围：{paths}")
    n_copied = 0
    for rel in paths:
        n_copied += copy_into(snap, rel)
    if n_copied == 0:
        print("  ⚠ 范围里一个文件都没复制到（空目录？）—— 回滚将无可恢复的，请复核。")

    manifest = {
        "name": name,
        "snapshot": snap.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "declared_paths": paths,
        "before_copied": n_copied,
        "tree_before": fingerprint_tree(PROJECT_ROOT),
        "changes": None,
        "out_of_scope": None,
    }
    write_manifest(snap, manifest)
    CURRENT_PTR.write_text(snap.name, encoding="utf-8")
    log_line("BEGIN", f"{snap.name} 目标={name} 声明范围={len(paths)} 项 "
                      f"已存前像={n_copied} 个文件")
    print(f"  ✓ 前像已存 {n_copied} 个文件，指纹已记录"
          f"（{len(manifest['tree_before'])} 个文件）")
    print("  现在可以放心动手。改完务必跑 `end` —— 它会点出范围外的意外改动。")
    return 0


def cmd_end(args) -> int:
    snap = load_current()
    if snap is None:
        print("❌ 没有进行中的快照。维护前要先 `begin`。")
        return 1
    m = read_manifest(snap)
    after = fingerprint_tree(PROJECT_ROOT)
    d = diff_fingerprint(m["tree_before"], after)
    declared = m["declared_paths"]
    changes = d["modified"] + d["added"] + d["deleted"]
    out = [r for r in changes if not in_scope(r, declared)]

    m["finished_at"] = datetime.now().isoformat(timespec="seconds")
    m["changes"] = d
    m["out_of_scope"] = out
    write_manifest(snap, m)
    CURRENT_PTR.unlink(missing_ok=True)

    print(f"■ 收尾快照：{snap.name}")
    print(f"  改动 {len(changes)} 个文件（改 {len(d['modified'])} / 新增 "
          f"{len(d['added'])} / 删 {len(d['deleted'])}）")
    for kind, items in (("改", d["modified"]), ("新增", d["added"]), ("删除", d["deleted"])):
        for r in items[:20]:
            print(f"    {kind}  {r}")
        if len(items) > 20:
            print(f"    {kind}  …另 {len(items) - 20} 个（见 manifest.json）")

    if out:
        print(f"\n  ⚠ 范围外改动 {len(out)} 个 —— **这些没有前像，回滚救不回来**：")
        for r in out[:20]:
            print(f"       {r}")
        print("     说明：声明范围只覆盖你打算动的东西。范围外常见且合法的情况是"
              "\n     agent_memory/long_memory/MEMORY.md 之类的记忆更新；如果这里出现"
              "你\n     没预料到的代码文件，务必在报告里点名说清。")
    else:
        print("\n  ✓ 未发现范围外改动 —— 与声明一致")

    log_line("END", f"{snap.name} 改动={len(changes)} 文件"
                    f"（范围外意外 {len(out)} 个）")
    print("\n下一步：跑验收 `python self_maintenance/tools/verify.py`，再写维护笔记。")
    return 0


def cmd_status(args) -> int:
    snap = load_current()
    if snap is None:
        print("没有进行中的快照（安全状态）。")
        return 0
    m = read_manifest(snap)
    print(f"进行中：{snap.name}")
    print(f"  目标：{m['name']}")
    print(f"  开始：{m['started_at']}")
    print(f"  声明范围：{m['declared_paths']}")
    print(f"  已存前像：{m['before_copied']} 个文件")
    return 0


def cmd_list(args) -> int:
    if not SNAP_DIR.exists():
        print("（还没有任何快照）")
        return 0
    rows = sorted([d for d in SNAP_DIR.iterdir() if d.is_dir() and (d / "manifest.json").exists()])
    if not rows:
        print("（还没有任何快照）")
        return 0
    for d in rows:
        m = read_manifest(d)
        ch = m.get("changes")
        n = "-" if ch is None else str(len(ch["modified"]) + len(ch["added"]) + len(ch["deleted"]))
        fin = m.get("finished_at") or "进行中"
        print(f"{d.name:<48} 改动 {n:>4}  前像 {m['before_copied']:>3}  {fin}")
    print(f"\n共 {len(rows)} 个快照。目录：{SNAP_DIR.relative_to(PROJECT_ROOT)}")
    return 0


def cmd_rollback(args) -> int:
    target = (SNAP_DIR / args.snapshot).resolve()
    if SNAP_DIR.resolve() not in target.parents and target != SNAP_DIR.resolve():
        print("❌ 只允许回滚 self_maintenance/snapshots/ 下的快照目录")
        return 1
    if not (target / "manifest.json").exists():
        print(f"❌ 找不到快照：{args.snapshot}")
        return 1
    m = read_manifest(target)
    before_root = target / "before"
    restore = []
    if before_root.exists():
        for dirpath, _dirnames, filenames in os.walk(before_root):
            for fn in filenames:
                p = Path(dirpath) / fn
                restore.append(p.relative_to(before_root).as_posix())
    ch = m.get("changes") or {}
    to_delete = list(ch.get("added") or [])

    print(f"■ 回滚计划：{target.name}")
    print(f"  维护目标：{m['name']}")
    print(f"  将还原 {len(restore)} 个文件到维护前内容")
    print(f"  将删除 {len(to_delete)} 个「维护时新增」的文件")
    if not args.yes:
        for r in restore[:30]:
            print(f"    还原  {r}")
        if len(restore) > 30:
            print(f"    还原  …另 {len(restore) - 30} 个")
        for r in to_delete[:30]:
            print(f"    删除  {r}")
        print("\n⚠ 这是**计划**，什么都没做。确认要执行就加 --yes。")
        print("  建议：先手动把你现在关心的文件另存一份 —— 手动备份永远最靠谱。")
        return 0

    n_ok = 0
    for rel in restore:
        src = before_root / rel
        dst = PROJECT_ROOT / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n_ok += 1
        except OSError as e:
            print(f"  ⚠ 还原失败 {rel}: {e}")
    n_del = 0
    for rel in to_delete:
        p = PROJECT_ROOT / rel
        try:
            if p.is_file():
                p.unlink()
                n_del += 1
        except OSError as e:
            print(f"  ⚠ 删除失败 {rel}: {e}")

    log_line("ROLLBACK", f"{target.name} 还原={n_ok} 删除={n_del}")
    print(f"\n✓ 回滚完成：还原 {n_ok} 个，删除 {n_del} 个")
    print("  注意：范围外的改动不在快照里，若 end 曾报过「范围外改动」，那些没被还原。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="自维护快照：文件级前像 + 变更报告")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("begin", help="维护前：存前像 + 记录指纹")
    p.add_argument("--name", required=True, help="这次维护叫什么（回滚时认人）")
    p.add_argument("--paths", nargs="+", required=True,
                   help="打算动的文件/目录（相对项目根）")
    p.set_defaults(func=cmd_begin)

    p = sub.add_parser("end", help="维护后：算出改了哪些文件")
    p.set_defaults(func=cmd_end)

    p = sub.add_parser("status", help="看有没有进行中的快照")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="列出全部快照")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("rollback", help="回滚到某个快照（默认只打印计划）")
    p.add_argument("snapshot", help="快照目录名（见 list）")
    p.add_argument("--yes", action="store_true", help="确认执行")
    p.set_defaults(func=cmd_rollback)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
