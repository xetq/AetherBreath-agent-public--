# agent_tools/skillhub_download.py
"""
AetherBreath 技能下载器（Skillhub Downloader）
============================================
让 agent 从 Hermes skillhub 生态（skills.sh / GitHub 技能仓库）直接下载技能，
落到自己的技能库 agent_skills/，复制粘贴即注册、开新对话生效。

定位符格式（兼容 hermes skills install 的书写习惯，GitHub 友好）：
    skills-sh/addyosmani/agent-skills/planning-and-task-breakdown   # skills.sh 前缀
    github/openai/skills/skills/.curated/web-search                 # github 前缀
    addyosmani/agent-skills/planning-and-task-breakdown             # 裸 owner/repo/path（等价 github）
                         └── repo ──┘ └──── skill 目录路径 ────┘

下载机制（批量友好）：
    - 同一 repo 的多个技能共享 1 次 repo meta + 1 次 git trees API（省限流配额）；
    - 技能目录内文件用 raw.githubusercontent.com 多线程并发拉取
      （raw 通道不受 GitHub API 限流约束，批量下载的吞吐瓶颈在这里）；
    - 网络适配：GitHub 直连不稳定时按序自动回退镜像前缀
      （ghfast.top / gh-proxy.com），全链失败才报错。

落地动作：
    - 写入 agent_skills/<技能目录名>/（保留技能目录内相对子路径）；
    - 在 SKILL.md frontmatter 记录 source 溯源字段（若未自带）；
    - 检测 SKILL.md 中指向技能目录外的相对引用（../../refs 等），列出警告
      （这类技能需要适配后才能完整自包含，本工具不自动重写引用）；
    - 下载完成后自动同步 SKILL_REGISTRY.md（调用 agent/skill_system.py）。

⚠️ 安全边界：本工具只做「下载 + 落盘」，绝不执行下载内容。技能正文/脚本
   进入技能库后按 AetherBreath 既定安全模型使用（正文由模型读取，脚本如需
   执行须走 execute_shell/execute_python 护栏通道）。来源为第三方仓库，请
   对技能内容保持与「复制粘贴即注册」同级的审视。

命令行（工程师手动批量下载）：
    venv/Scripts/python agent_tools/skillhub_download.py \\
        skills-sh/addyosmani/agent-skills/code-review-and-quality \\
        skills-sh/addyosmani/agent-skills/systematic-debugging \\
        --list   # 仅解析并列出将下载的文件，不写盘（演练模式）
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ========== 路径推导（GitHub 友好：全部相对项目根，不写死绝对路径） ==========
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET_DIR = PROJECT_ROOT / "agent_skills"

# 让模块能复用技能系统的 sync_registry（位于 agent/skill_system.py）
_AGENT_DIR = PROJECT_ROOT / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# ========== 网络配置 ==========
TIMEOUT = 15                 # 单请求超时（秒）
MAX_FILE_BYTES = 10 * 1024 * 1024   # 单文件大小上限（防呆，10MB）
MAX_WORKERS = 8              # raw 并发下载线程数
USER_AGENT = "AetherBreath-skillhub-downloader/1.0"

# 镜像回退链：先直连，失败后依次尝试镜像前缀（前缀 + 原始完整 URL）
MIRROR_PREFIXES: List[str] = [
    "https://ghfast.top/",
    "https://gh-proxy.com/",
]

# 技能目录内忽略的杂物路径片段
_IGNORE_PATH_PARTS = (
    ".git", "__pycache__", "node_modules", ".DS_Store", ".idea", ".vscode",
    "*.pyc", "Thumbs.db",
)


# ========== 定位符解析 ==========
# 支持前缀: skills-sh/ 、 skills.sh/ 、 github/ ；其余按裸 owner/repo/path 处理
_SOURCE_PREFIX_RE = re.compile(r"^(?P<src>skills-sh|skills\.sh|github)/", re.IGNORECASE)


def parse_identifier(identifier: str) -> Dict[str, str]:
    """
    解析技能定位符 → {source, owner, repo, skill_path, repo_full}。

    示例:
      skills-sh/addyosmani/agent-skills/planning-and-task-breakdown
      → source=skills-sh, repo=addyosmani/agent-skills,
        skill_path=planning-and-task-breakdown
    """
    ident = identifier.strip().strip("/")
    source = "github"
    m = _SOURCE_PREFIX_RE.match(ident)
    if m:
        source = m.group("src").lower()
        if source == "skills.sh":
            source = "skills-sh"
        ident = ident[m.end():]

    parts = ident.split("/")
    if len(parts) < 3:
        raise ValueError(
            f"定位符格式错误: '{identifier}' —— 需要 owner/repo/技能目录路径"
            "（可带 skills-sh/ 或 github/ 前缀），例如 "
            "skills-sh/addyosmani/agent-skills/planning-and-task-breakdown"
        )
    return {
        "source": source,
        "owner": parts[0],
        "repo": parts[1],
        "skill_path": "/".join(parts[2:]),
        "repo_full": f"{parts[0]}/{parts[1]}",
    }


def canonical_label(parsed: Dict[str, str]) -> str:
    """溯源标签：保留用户输入的前缀风格（与技能库现有 source: clawhub/... 一致）。"""
    return f"{parsed['source']}/{parsed['owner']}/{parsed['repo']}/{parsed['skill_path']}"


# ========== HTTP（直连 + 镜像回退链） ==========

def _request(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    stream: bool = False,
    api: bool = False,
) -> requests.Response:
    """
    带通道策略的 GET 请求。

    - api=True（api.github.com 等）：只走直连——镜像普遍不支持 GitHub API
      （ghfast.top/gh-proxy.com 对 api.github.com 返回 403/000），试了白等。
    - raw 文件：直连（连接超时 5s）失败后按 MIRROR_PREFIXES 依次走镜像。
    - 非 2xx 不立即放弃：403 常见于「镜像拒绝该域名」，换通道可能成功；
      全部通道失败后抛出汇总错误（保留最后 3 个）。
    """
    if api:
        candidates = [url]
    else:
        candidates = [url] + [p + url for p in MIRROR_PREFIXES]

    errors: List[str] = []
    for cand in candidates:
        try:
            resp = requests.get(
                cand,
                params=params,
                timeout=(5, 25),  # 连接 5s / 读取 25s：raw 直连不通时快速切镜像
                stream=stream,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            errors.append(f"{cand}: {type(e).__name__}")
            continue
        if resp.status_code < 400:
            return resp
        errors.append(f"{cand}: HTTP {resp.status_code}")

    raise requests.RequestException(
        "所有通道失败: " + " | ".join(errors[-3:])
    )


def _api_json(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = _request(url, params=params, api=True)
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise requests.RequestException(f"非 JSON 响应 {url}: {e}") from e


def _raw_get_bytes(url: str) -> bytes:
    resp = _request(url, stream=True)
    resp.raw.decode_content = True
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise requests.RequestException(
                f"文件超过大小上限 {MAX_FILE_BYTES // 1024 // 1024}MB: {url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ========== GitHub 仓库信息 ==========

def _repo_default_branch(repo_full: str) -> str:
    data = _api_json(f"https://api.github.com/repos/{repo_full}")
    branch = data.get("default_branch")
    if not branch:
        raise requests.RequestException(f"无法获取默认分支: {repo_full}")
    return branch


def _repo_tree_entries(repo_full: str, branch: str) -> Optional[List[Dict[str, Any]]]:
    """拿整仓 git tree（一次 API）。失败/被截断返回 None，由调用方决定回退。"""
    data = _api_json(
        f"https://api.github.com/repos/{repo_full}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if data.get("truncated"):
        return None
    entries = data.get("tree")
    return entries if isinstance(entries, list) else None


def _contents_walk(repo_full: str, branch: str, prefix: str) -> List[Dict[str, Any]]:
    """回退方案：用 Contents API 递归列出技能目录内全部条目（含 type/size/path）。"""
    out: List[Dict[str, Any]] = []

    def walk(path: str) -> None:
        data = _api_json(
            f"https://api.github.com/repos/{repo_full}/contents/{path}",
            params={"ref": branch},
        )
        if not isinstance(data, list):
            return
        for item in data:
            if item.get("type") == "dir":
                walk(item["path"])
            elif item.get("type") == "file":
                out.append(item)

    walk(prefix.rstrip("/"))
    return out


def _looks_ignored(path: str) -> bool:
    lower = path.lower()
    for part in _IGNORE_PATH_PARTS:
        if part.startswith("*"):
            if lower.endswith(part[1:]):
                return True
        elif part in lower.split("/"):
            return True
    return False


# ========== 引用检测（技能目录外的相对引用 → 警告） ==========
_EXT_REF_RE = re.compile(
    r"!?\[[^\]]*\]\(|(?<![`\w])[\w./-]*\.\./[\w./\-]+\.(?:md|py|sh|json|yaml|yml|txt|svg|png)"
    r"|\(([\w./\-]*\.\./[\w./\-]+\.(?:md|py|sh|json|yaml|yml|txt|svg|png))\)",
    re.IGNORECASE,
)


def detect_external_refs(skill_md_text: str) -> List[str]:
    """
    粗略检测 SKILL.md 中指向技能目录之外的相对引用（形如 ../../xxx）。
    返回去重后的引用路径列表。仅是提示，不阻止下载。
    """
    refs: List[str] = []
    for line in skill_md_text.splitlines():
        for m in _EXT_REF_RE.finditer(line):
            ref = m.group(0)
            ref = ref.strip("()[]!")  # 剥 markdown 外壳
            if ".." in ref and not ref.startswith(("http://", "https://")):
                if ref not in refs:
                    refs.append(ref)
    return refs


# ========== frontmatter source 溯源注入 ==========
_SOURCE_FIELD_RE = re.compile(r"^source\s*:", re.MULTILINE)


def _inject_source(skill_md: bytes, source_label: str) -> bytes:
    """在 SKILL.md frontmatter 中补 source 溯源字段（若已有则不动）。"""
    try:
        text = skill_md.decode("utf-8")
    except UnicodeDecodeError:
        return skill_md
    if not text.startswith("---") or _SOURCE_FIELD_RE.search(text):
        return skill_md

    lines = text.splitlines(keepends=True)
    # frontmatter 结束 = 第 2 行起第一个 "---"
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            lines.insert(i, f"source: {source_label}\n")
            break
    return "".join(lines).encode("utf-8")


# ========== 技能路径探测（上游仓库习惯把技能放在 skills/ 子目录） ==========
def _skill_path_candidates(skill_path: str) -> List[str]:
    """候选技能路径：原样 + 自动补 skills/ 前缀（若尚未带）。"""
    cands = [skill_path]
    if not skill_path.startswith("skills/") and not skill_path.startswith("."):
        cands.append(f"skills/{skill_path}")
    return cands


def _collect_files(
    repo_full: str,
    branch: str,
    skill_path: str,
    tree_entries: Optional[List[Dict[str, Any]]],
) -> Tuple[Optional[str], List[Tuple[str, int]]]:
    """
    解析技能目录并收集文件清单。

    Returns:
        (resolved_skill_path, files)：resolved 为实际存在且含 SKILL.md 的路径；
        files 为 [(相对路径, size)]；找不到返回 (None, [])。
    """
    candidates = _skill_path_candidates(skill_path)

    if tree_entries is not None:
        for cand in candidates:
            prefix = cand.rstrip("/") + "/"
            files: List[Tuple[str, int]] = []
            for item in tree_entries:
                p = item.get("path", "")
                if not p.startswith(prefix) or item.get("type") != "blob":
                    continue
                if item.get("mode") == "120000":  # symlink 拒绝
                    continue
                rel = p[len(prefix):]
                if not rel or _looks_ignored(rel):
                    continue
                files.append((rel, int(item.get("size") or 0)))
            if files and any(rel.lower() == "skill.md" for rel, _ in files):
                return cand, files
        return None, []

    # tree 不可用 → Contents API 对候选逐个递归探测（404 会抛，逐个容错）
    for cand in candidates:
        try:
            entries = _contents_walk(repo_full, branch, cand)
        except requests.RequestException:
            continue
        prefix = cand.rstrip("/") + "/"
        files = []
        for item in entries:
            rel = item["path"][len(prefix):]
            if not rel or _looks_ignored(rel):
                continue
            files.append((rel, int(item.get("size") or 0)))
        if any(rel.lower() == "skill.md" for rel, _ in files):
            return cand, files
    return None, []


# ========== 核心：下载一个技能目录 ==========

def _download_skill(
    parsed: Dict[str, str],
    branch: str,
    tree_entries: Optional[List[Dict[str, Any]]],
    target_dir: Path,
    force: bool,
    record_source: bool,
) -> Dict[str, Any]:
    """下载单个技能目录到 agent_skills/<目录名>/。返回结果 dict。"""
    repo_full = parsed["repo_full"]
    skill_path = parsed["skill_path"].rstrip("/")
    folder_name = skill_path.split("/")[-1]

    # 技能目录名合法性（与 skill_system 的 name==folder 规则对齐）
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", folder_name) or folder_name.startswith("."):
        return {"name": folder_name, "error": f"技能目录名不合法: {folder_name}"}

    # 解析候选路径 + 收集文件清单
    resolved_path, files = _collect_files(repo_full, branch, skill_path, tree_entries)
    if resolved_path is None:
        return {
            "name": folder_name,
            "error": (
                f"未找到含 SKILL.md 的技能目录（尝试了: "
                + ", ".join(_skill_path_candidates(skill_path)) + "）"
            ),
        }
    prefix = resolved_path.rstrip("/") + "/"

    dest = target_dir / folder_name
    backup = None
    if dest.exists():
        if not force:
            return {"name": folder_name, "error": "已存在（用 force=True 覆盖）", "exists": True}
        backup = dest.with_name(dest.name + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        shutil.move(str(dest), str(backup))
    dest.mkdir(parents=True, exist_ok=True)

    # 多线程并发拉 raw 文件
    installed_files: List[str] = []
    failed: List[Dict[str, str]] = []

    def _fetch_one(rel: str) -> Optional[bytes]:
        url = f"https://raw.githubusercontent.com/{repo_full}/{branch}/{prefix}{rel}"
        return _raw_get_bytes(url)

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(files)))) as pool:
        future_map = {pool.submit(_fetch_one, rel): rel for rel, _ in files}
        for future in as_completed(future_map):
            rel = future_map[future]
            try:
                content = future.result()
                out_path = dest / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                if rel.lower() == "skill.md" and record_source:
                    content = _inject_source(
                        content,
                        f"{parsed['source']}/{parsed['owner']}/{parsed['repo']}/{resolved_path}",
                    )
                out_path.write_bytes(content)
                installed_files.append(rel)
            except Exception as e:  # noqa: BLE001 - 单项失败不阻断整体
                failed.append({"file": rel, "error": str(e)})

    # ---- 失败判定与回滚（审计 B8）----
    # 旧代码这里只有一行注释写着"下载完但 SKILL.md 缺失 → 目录视为失败回滚"，
    # 实际**没有任何回滚或判失败逻辑**；而且单文件下载失败只被记进 failed_files，
    # 该技能的返回值里没有 error，调用方照样算进 installed、success 仍为 True
    # —— 技能缺一半文件也报"成功"。现在两道判据都补齐。
    def _rollback(reason: str) -> Dict[str, Any]:
        """收掉这次落盘的目录；若覆盖过旧版本，把备份移回原位。"""
        try:
            if dest.exists():
                shutil.rmtree(str(dest), ignore_errors=True)
        except Exception:
            pass
        if backup is not None and backup.exists():
            try:
                shutil.move(str(backup), str(dest))
            except Exception:
                pass
        return {"name": folder_name, "error": reason,
                "failed_files": failed, "dest": str(dest),
                "source": canonical_label(parsed)}

    skill_md_path = None
    for _cand in (dest / "SKILL.md", dest / "skill.md"):
        if _cand.exists():
            skill_md_path = _cand
            break
    if skill_md_path is None:
        return _rollback("下载结束但 SKILL.md 缺失（视为安装失败，已回滚）")
    if failed:
        names = "; ".join(str(f.get("file", "?")) for f in failed[:5])
        return _rollback("有 %d 个文件下载失败（视为安装失败，已回滚）：%s"
                         % (len(failed), names))

    warnings: List[str] = []
    try:
        text = skill_md_path.read_text(encoding="utf-8")
        for ref in detect_external_refs(text):
            warnings.append(f"正文引用技能目录外文件（需适配）: {ref}")
    except UnicodeDecodeError:
        pass

    return {
        "name": folder_name,
        "files": len(installed_files),
        "failed_files": failed,
        "warnings": warnings,
        "dest": str(dest.relative_to(PROJECT_ROOT)),
        "source": canonical_label(parsed),
    }


def skillhub_install(
    skills: List[str],
    force: bool = False,
    record_source: bool = True,
    target_dir: Optional[str] = None,
    logger=None,
) -> dict:
    """
    从 skillhub批量下载技能到 agent_skills/。

    Args:
        skills: 技能定位符列表，如 ["skills-sh/addyosmani/agent-skills/planning-and-task-breakdown"]
        force: 同名技能已存在时是否覆盖（默认 False=跳过；覆盖前会先备份为 .bak-时间戳）
        record_source: 是否在 SKILL.md frontmatter 记录 source 溯源字段（默认 True）
        target_dir: 技能库目录（默认项目 agent_skills/，一般不要改）
        logger: 日志实例（由编排器自动注入，可选）

    Returns:
        dict: {"success": bool, "installed": [...], "failed": [...], "message": str}
    """
    t0 = time.time()
    if logger:
        logger.info("skillhub_install 开始", count=len(skills), force=force)
    target = Path(target_dir).resolve() if target_dir else DEFAULT_TARGET_DIR
    target.mkdir(parents=True, exist_ok=True)

    parsed_list: List[Dict[str, str]] = []
    parse_failed: List[Dict[str, str]] = []
    for ident in skills:
        try:
            parsed_list.append(parse_identifier(ident))
        except ValueError as e:
            parse_failed.append({"identifier": ident, "error": str(e)})

    # 按 repo 分组：同一 repo 共享 repo meta + tree（省 API 配额，批量核心优化）
    by_repo: Dict[str, List[Dict[str, str]]] = {}
    for p in parsed_list:
        by_repo.setdefault(p["repo_full"], []).append(p)

    installed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = parse_failed
    all_warnings: List[str] = []

    for repo_full, skills_of_repo in by_repo.items():
        branch: Optional[str] = None
        tree: Optional[List[Dict[str, Any]]] = None
        try:
            branch = _repo_default_branch(repo_full)
        except Exception as e:  # noqa: BLE001
            failed.append({"identifier": repo_full, "error": f"获取仓库信息失败: {e}"})
            continue
        try:
            tree = _repo_tree_entries(repo_full, branch)
        except Exception:  # noqa: BLE001 - tree 失败回退 contents API
            tree = None

        for parsed in skills_of_repo:
            try:
                result = _download_skill(
                    parsed, branch, tree, target, force=force,
                    record_source=record_source,
                )
                if "error" in result:
                    result["identifier"] = canonical_label(parsed)
                    failed.append(result)
                else:
                    installed.append(result)
                    all_warnings.extend(result.get("warnings", []))
            except Exception as e:  # noqa: BLE001
                failed.append({"identifier": canonical_label(parsed), "error": str(e)})

    # 自动同步技能注册表（复用技能系统；失败不致命，提示手动同步）
    sync_hint = ""
    try:
        from skill_system import sync_registry  # noqa: F401

        sync = sync_registry(skills_dir=target)
        if sync.changed:
            sync_hint = f"；注册表已同步: {sync.summary()}"
        else:
            sync_hint = "；注册表无变化"
    except Exception as e:  # noqa: BLE001
        sync_hint = f"；⚠️ 注册表同步失败（可手动运行 python agent/skill_system.py）: {e}"

    elapsed = round(time.time() - t0, 1)
    msg_parts = [f"成功 {len(installed)} 个，失败 {len(failed)} 个，耗时 {elapsed}s{sync_hint}"]
    if installed:
        msg_parts.append("已装: " + ", ".join(i["name"] for i in installed))
    if failed:
        msg_parts.append("未装: " + ", ".join(str(f.get("identifier", f.get("name", "?"))) for f in failed))
    if all_warnings:
        msg_parts.append("引用警告 " + str(len(all_warnings)) + " 条")
    message = "；".join(msg_parts)

    if logger:
        logger.info("skillhub_install 完成", installed=len(installed), failed=len(failed))

    return {
        "success": bool(installed),
        "installed": installed,
        "failed": failed,
        "warnings": all_warnings,
        "message": message,
        "hint": "新技能已落入 agent_skills/；由于语境快照冻结，请开新对话让 agent 看到它们。",
    }


# ========== 工具 Schema（供 agent 直接调用） ==========
skillhub_install_schema = {
    "type": "function",
    "function": {
        "name": "skillhub_install",
        "description": (
            "从 Hermes skillhub 生态批量下载技能到本 agent 技能库 "
            "agent_skills/，自动同步注册表。定位符格式: "
            "[skills-sh/|github/]owner/repo/技能目录路径，例如 "
            "skills-sh/addyosmani/agent-skills/planning-and-task-breakdown。"
            "下载后需开新对话才能让 agent 使用新技能（语境快照冻结）。"
            "若技能源非Hermes生态,不建议使用"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "技能定位符列表（可多个，同一仓库自动共享 API 请求、文件多线程下载）",
                },
                "force": {
                    "type": "boolean",
                    "description": "同名技能已存在时覆盖（默认 false=跳过；覆盖前自动备份）",
                },
                "record_source": {
                    "type": "boolean",
                    "description": "在 SKILL.md frontmatter 写入 source 溯源（默认 true）",
                },
            },
            "required": ["skills"],
        },
    },
}

__all__ = ["skillhub_install", "skillhub_install_schema", "parse_identifier"]


# ========== CLI ==========
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AetherBreath 技能下载器：从 skillhub/GitHub 下载技能到 agent_skills/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
        "  venv/Scripts/python agent_tools/skillhub_download.py \\\n"
        "      skills-sh/addyosmani/agent-skills/code-review-and-quality \\\n"
        "      skills-sh/addyosmani/agent-skills/systematic-debugging",
    )
    parser.add_argument("identifiers", nargs="*", help="技能定位符，如 skills-sh/owner/repo/skill-path")
    parser.add_argument("--force", action="store_true", help="已存在同名技能时覆盖（先备份）")
    parser.add_argument("--no-source", action="store_true", help="不写 source 溯源字段")
    parser.add_argument("--dir", default=None, help="目标技能库目录（默认项目 agent_skills/）")
    parser.add_argument("--list", action="store_true", help="预览模式：只解析定位符（不访问网络、不写盘）")
    args = parser.parse_args(argv)

    if not args.identifiers:
        parser.print_help()
        return 1

    if args.list:
        for ident in args.identifiers:
            try:
                p = parse_identifier(ident)
                print(f"✓ {canonical_label(p)}  →  repo={p['repo_full']}  skill={p['skill_path']}")
            except ValueError as e:
                print(f"✗ {ident}: {e}")
        return 0

    result = skillhub_install(
        args.identifiers,
        force=args.force,
        record_source=not args.no_source,
        target_dir=args.dir,
    )
    print(f"📦 {result['message']}")
    for item in result["installed"]:
        print(f"  ✓ {item['name']} → {item['dest']}（{item['files']} 个文件）")
        for w in item.get("warnings", []):
            print(f"    ⚠ {w}")
    for item in result["failed"]:
        print(f"  ✗ {item.get('identifier', item.get('name', '?'))}: {item.get('error', '?')}")
    print("💡 " + result["hint"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
