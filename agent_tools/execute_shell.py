"""
工具名称: execute_shell
功能: 在受限子进程中执行一条 shell 命令，并返回输出结果。

安全策略（资源护栏模型，参照宿主 Agent 的 terminal 工具标准 + execute_python 护栏）:
  1. 进程隔离    : 独立子进程 + 独立进程组；超时可整棵进程树强杀，不残留孤儿进程。
  2. 资源护栏    : 超时（有上限 clamp）+ 输出 head/tail 截断（防死循环输出撑爆内存）。
  3. 凭据隔离    : 环境变量只保留白名单前缀 + 剔除秘密子串（KEY/TOKEN/SECRET...），
                   防止命令读到并外泄 API key 等凭据。
  4. 命令拦截    : 正则黑名单「尽力而为」地拦截高危命令（系统目录删除、磁盘格式化、
                   关机重启、反弹 shell、下载执行链、挖矿勒索等）。
                   注意——这不是安全边界，恶意命令可绕过文本检查（如编码混淆、base64、
                   变量拼接）。真正的可信执行需要 OS 级沙箱/容器。

  🔴 定位声明: 本模块是「防失控的护栏」，不是「安全沙箱」。
    不要用它执行来自不可信来源、或用户无法确认意图的命令。

平台说明: 命令通过 bash 执行（Windows 上为 git-bash/MSYS）。普通文件操作、
    git/npm/python 等开发命令、网络请求、文件/目录删除（rm -rf 具体路径）均放行。
"""

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ================= 配置（全部可调） =================
DEFAULT_TIMEOUT = 30          # 默认超时（秒）
MAX_TIMEOUT = 120             # 超时硬上限（防止调用方传 timeout=999999）
MAX_STDOUT_BYTES = 100_000    # 输出截断上限（head 40% + tail 60%）
MAX_STDERR_BYTES = 20_000
_IS_WINDOWS = platform.system() == "Windows"

# ---- 项目 venv（相对路径动态推导，随项目走，GitHub 友好） ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN_DIR = "Scripts" if os.name == "nt" else "bin"
PROJECT_VENV_BIN = os.path.join(_PROJECT_ROOT, "venv", _VENV_BIN_DIR)

# ================= 危险命令检测（正则黑名单，尽力而为） =================
#
# 设计原则（对齐宿主 terminal 的 tirith + dangerous-command 思路的轻量版）:
#   - 只拦截「危险目标」，不拦正常清理: `rm -rf node_modules` / `rm -rf ./build` 放行，
#     `rm -rf /` / `rm -rf C:\` / `rm -rf ~` / `rm -rf /c` 拦截。
#   - 匹配对大小写不敏感（Windows 路径、PowerShell 混写都覆盖）。
#   - 每条规则是一个正则；命中即返回规则描述。

# rm -rf 危险目标: 根目录、通配符裸删、家目录、系统目录、Windows 盘符根
_RM_RF_PATTERNS = [
    # 根目录/盘符根: rm -rf /、rm -rf /*、rm -rf /c、rm -rf C:\、rm -rf C:/、rm -rf /c/
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:/|/\*|/c/?|/d/?|/e/?|/[a-zA-Z]:/?|C:[/\\]|D:[/\\]|E:[/\\])(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 家目录: rm -rf ~、rm -rf $HOME、rm -rf /home、rm -rf /root、rm -rf /Users/<name>
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:~|~/|~\$|/home|/home/|/root|/root/|\$HOME|\$HOME/|/Users/)(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 系统目录直删: /etc /usr /bin /sbin /boot /var /lib /opt /System /Windows /System32 /Program Files
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:/etc|/usr|/bin|/sbin|/boot|/var|/lib|/opt|/System|/Windows|/System32|/Windows/|/Program\s+Files)(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 通配符裸删（打错即灾难）: rm -rf *、rm -rf .、rm -rf ..、rm -rf ./*、rm -rf ../*
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:\*|\.|\.\.|\./\*|\.\./\*|\.\*|\./\..*)(?:[\x22\x27\x60]?)(?:\s|;|$)",
]

# 磁盘/分区/系统级破坏
_DISK_DESTROY_PATTERNS = [
    r"\b(?:dd\s+.*\s+of=/dev/(?:sd|hd|nvme|vd)[a-z])",   # dd 写裸盘
    r"\b(?:fdisk|parted|gdisk|sfdisk)\s+.*/dev/(?:sd|hd|nvme|vd)",  # 分区操作
    r"\bformat\s+[A-Za-z]:",                              # Windows format C:
    r"\b(?:cryptsetup\s+luksFormat|shred\s+.*/dev/(?:sd|hd|nvme|vd))",  # 加密/擦除磁盘
    # 注：mkfs / mkswap / diskpart 三条已移到**命令位**规则（_WORD_RULES）——
    # 它们作为模式全文扫时，会把 `grep mkfs`、`cat notes_diskpart.md` 这类
    # 只读命令一起拦掉（审计 B4）。
]

# ---- 单词型规则：**只在命令位**判（审计 B4 重写）----
# 旧实现把 shutdown/reboot/halt/diskpart/mkfs/xmrig 这些词做**全文子串搜索**，
# 实测后果（下面这些全被拦，可它们都是只读排障命令）：
#     echo shutdown / grep -i reboot app.log / git log --grep=poweroff /
#     echo halt / echo "diskpart"
# 排障最常用的读日志、查 git 历史，被自己的闸门挡住。
# 正解是分层（与审批引擎 approval_lex 同一思路）：
#   **单词型规则只认"命令位"上那个真正被执行的程序名**。
#
# 表形态：规则名 -> (精确词, 前缀词)
_WORD_RULES = [
    ("关机/重启", (("shutdown", "reboot", "poweroff", "halt"), ("init",))),
    ("磁盘/分区破坏", (("diskpart", "fdisk", "parted", "gdisk", "sfdisk", "mkswap",
                       "wipefs", "blkdiscard", "format"), ("mkfs",))),
    ("挖矿/勒索程序", (("xmrig", "minerd", "cpuminer", "kryptex", "nanominer",
                        "ransom", "wannacry", "lockbit"), ())),
]

# 段首 wrapper：这些词后面才是真正被执行的命令（`sudo shutdown -h now`）
_WRAPPER_WORDS = frozenset({
    "sudo", "doas", "env", "nohup", "time", "timeout", "xargs", "command", "exec",
    "nice", "ionice", "setsid", "stdbuf", "busybox", "runuser", "su", "watch",
    "systemd-run", "pkexec",
})

# shell 控制运算符（命令段分隔符）
_SEGMENT_OPS_2 = ("&&", "||", "$(")
_SEGMENT_OPS_1 = (";", "|", "&", "\n", "`", "(", ")")


def _token_to_word(token: str) -> str:
    """把 token 规整成命令词：去引号、取路径末段、去 .exe 后缀、小写。"""
    t = token.strip().strip(chr(34) + chr(39) + chr(96))
    t = t.replace(chr(92), "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


def _looks_like_assignment(token: str) -> bool:
    """`VAR=value` 形态的环境赋值（不是命令词）。"""
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _segment_words(command: str) -> List[List[str]]:
    """按 shell 控制运算符切段，每段给出规范化后的 token 列表。"""
    text = command or ""
    segs: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i + 2] in _SEGMENT_OPS_2:
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if text[i] in _SEGMENT_OPS_1:
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(text[i])
        i += 1
    segs.append("".join(buf))

    out: List[List[str]] = []
    for seg in segs:
        words: List[str] = []
        for token in seg.split():
            if _looks_like_assignment(token):
                continue
            word = _token_to_word(token)
            if word:
                words.append(word)
        if words:
            out.append(words)
    return out


def _match_word_table(word: str) -> Optional[str]:
    """这个 token 命中哪条单词型规则（没命中返回 None）。"""
    for rule_name, (exact, prefixes) in _WORD_RULES:
        if word in exact or any(word.startswith(p) for p in prefixes):
            return rule_name
    return None


def _word_rule_hit(command: str) -> Optional[str]:
    """单词型规则：只在**命令位**判（审计 B4）。

    两条判据：
      (a) 段内第一个"非 wrapper、非选项"的 token 命中危险词表 —— 正常形态：
          `shutdown -h now`、`mkfs.ext4 /dev/sda`；
      (b) **段首是 wrapper 词时**，该段内任意位置命中都算 —— wrapper 意味着
          "这段是借权限/包装去执行"，`sudo -u root <危险命令> -h now` 不该因为
          中间夹了 `-u root` 这种选项就漏掉（自测发现的漏拦）。
    为什么不干脆全文扫：那正是旧实现，`echo shutdown`、`grep -i reboot app.log`
    这些只读排障命令会被误拦（审计 B4 的原始症状）。
    """
    for seg in _segment_words(command):
        head = seg[0]
        for w in seg:
            if w in _WRAPPER_WORDS or w.startswith("-"):
                continue
            rule = _match_word_table(w)
            if rule:
                return "命令位命中「%s」危险模式: %s" % (rule, w)
            break
        if head in _WRAPPER_WORDS:
            for w in seg:
                rule = _match_word_table(w)
                if rule:
                    return "命令位命中「%s」危险模式（wrapper 段内）: %s" % (rule, w)
    return None

# 提权/系统目录权限破坏
_PRIV_ESC_PATTERNS = [
    r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?(?:777|666|000)\s+(?:[\x22\x27\x60]?)(?:/|/etc|/usr|/bin|/sbin|/boot|/var|/Windows|/System32|C:[/\\])(?:[\x22\x27\x60]?)",
    r"\bchown\s+-R\s+.*\s+(?:/|/etc|/usr|/bin|/sbin|/boot|/var|/Windows|/System32|C:[/\\])(?:\s|$)",
]

# 反弹 shell / 远程控制
_REVERSE_SHELL_PATTERNS = [
    r"/dev/tcp/",                                          # bash /dev/tcp 反弹
    r"\b(?:nc|ncat|netcat)\s+.*-e\b",                      # nc -e
    r"\b(?:nc|ncat|netcat)\s+.*--exec",                    # nc --exec
    r"\bsocat\s+.*(?:exec|system):",                       # socat 执行
    r"bash\s+-i\s+[<>]|sh\s+-i\s+[<>]",                    # bash -i >& /dev/tcp
]

# 下载即执行链（curl|sh 等）
_DOWNLOAD_EXEC_PATTERNS = [
    r"\bcurl\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
    r"\bwget\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
    r"\b(?:iwr|Invoke-WebRequest|Invoke-Expression|iex)\b[^|;&]*\|\s*iex\b",
    r"(?:Invoke-Expression|iex)\s*\(",
]

# 挖矿 / 勒索：已移到**命令位**规则（_WORD_RULES）。
# 程序名出现在**参数位**（`grep xmrig server.log`）并不代表要跑挖矿程序（审计 B4）。

# 汇总: (规则名, [正则...])
# 形态型规则（**全文扫**）：这些规则的形态本身就含"命令 + 参数"，
# 只认命令位反而会漏掉 `sh -c "dd if=... of=/dev/sda"` 这类嵌套。
# 单词型规则（关机/磁盘工具/矿工）在 _WORD_RULES，只认命令位。
_DANGEROUS_RULES: List[Tuple[str, List[str]]] = [
    ("系统目录删除", _RM_RF_PATTERNS),
    ("磁盘/分区破坏", _DISK_DESTROY_PATTERNS),
    ("提权/权限破坏", _PRIV_ESC_PATTERNS),
    ("反弹 shell", _REVERSE_SHELL_PATTERNS),
    ("下载执行链", _DOWNLOAD_EXEC_PATTERNS),
]
# 编译一次，复用
_COMPILED_RULES: List[Tuple[str, List[re.Pattern]]] = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in _DANGEROUS_RULES
]


def check_command_safety(command: str) -> Optional[str]:
    """检查命令是否命中危险规则。返回 (危险描述) 或 None（安全）。

    两层（审计 B4）：
      1. **单词型规则只在命令位判** —— `echo shutdown` / `grep -i reboot app.log`
         这类只读排障命令必须放行，而 `shutdown -h now` / `sudo shutdown` 照拦；
      2. **形态型规则全文扫** —— `rm -rf /`、`dd of=/dev/sda`、`curl|sh`、
         `/dev/tcp/` 的形态本身就含"命令 + 参数"，全文扫才不会漏掉嵌套。
    """
    if not command or not command.strip():
        return None
    hit = _word_rule_hit(command)
    if hit:
        return hit
    for rule_name, patterns in _COMPILED_RULES:
        for pat in patterns:
            if pat.search(command):
                return f"命令命中「{rule_name}」危险模式: {pat.pattern}"
    return None


# ================= 环境变量清洗 =================
# 白名单前缀（保留）；秘密子串（剔除）；Windows 必备（保留，否则 socket/subprocess 崩）
_SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP",
    "SHELL", "LOGNAME", "XDG_", "VIRTUAL_ENV", "CONDA", "PYTHONPATH", "PYTHONHOME",
    "NODE_", "NPM_", "PNPM_", "YARN_", "JAVA_", "GOPATH", "GOROOT", "RUST_", "CARGO_",
)
_SECRET_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "DSN",
    "WEBHOOK", "CREDS", "BEARER", "APIKEY", "PRIVATE",
)
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PUBLIC", "ALLUSERSPROFILE",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "USERDOMAIN", "USERNAME", "HOMEDRIVE", "HOMEPATH", "COMPUTERNAME",
    "PSMODULEPATH", "PSExecutionPolicyPreference",
})


def clean_environment(extra_path: Optional[List[str]] = None) -> Dict[str, str]:
    """构建清洗后的子进程环境。

    规则（顺序）:
      1. 秘密子串（KEY/TOKEN/SECRET/PASSWORD...）剔除 —— 防凭据泄露，这是真正的安全关键。
      2. 白名单前缀保留（PATH/HOME/LANG/TERM/NODE_/NPM_...）。
      3. Windows 必备变量保留（SYSTEMROOT/COMSPEC/...），否则 socket/subprocess 直接崩。
      4. 其余（含 AETHER_*/非白名单变量）全部丢弃。
      5. PATH 重组：项目 venv -> MSYS coreutils -> 原 PATH（原因见函数末尾注释）。

    extra_path: 额外前置目录，通常是 _msys_bin_dirs(_find_bash())。
                不传则内部自己探测 —— 保证任何调用方都漏不掉这一步。
    """
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue  # 凭据，剔除
        if k.startswith(_SAFE_ENV_PREFIXES):
            env[k] = v
            continue
        if _IS_WINDOWS and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS:
            env[k] = v
            continue
        # 其余全部丢弃（不放进 env）
    # 兜底：必须保证 PATH 存在，否则子进程连 bash 都找不到
    if "PATH" not in env:
        env["PATH"] = os.defpath
    # PATH 前置顺序：项目 venv（python/pip）→ MSYS coreutils（find/sort/grep）→ 原 PATH。
    #
    # 为什么必须显式前置 MSYS 目录：本工具用 `bash -c` 启动子进程（非登录、非交互），
    # 它不会 source /etc/profile，于是拿到的是裸 Windows 进程 PATH —— 里面
    # C:\Windows\System32 排在第 3 位，而 System32 自带 DOS 版 FIND.EXE / SORT.EXE
    # 和一个 bash.exe（WSL 启动器）；MSYS 自己的 /bin 却排在第 19 位。
    # 后果：GNU find/sort 被抢（find 会把 -type 当文件名报「找不到文件」），
    # 而在 shell 里再调 bash 会直接掉进坏掉的 WSL 发行版。
    # 对照实测：`bash -lc` 的 PATH 由 profile 前置成 /usr/local/bin:/usr/bin:/bin 打头，
    # 所以这里的前置是与官方 git-bash 行为对齐，不是自创规矩。
    # 想零代码回退：设 AETHER_MSYS_PATH_PREPEND=0。
    prepend: List[str] = []
    if os.path.isdir(PROJECT_VENV_BIN):
        prepend.append(PROJECT_VENV_BIN)
    if os.environ.get("AETHER_MSYS_PATH_PREPEND", "1") != "0":
        prepend.extend(extra_path if extra_path is not None
                       else _msys_bin_dirs(_find_bash()))
    seen, ordered = set(), []
    for d in prepend:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen:
            seen.add(key)
            ordered.append(d)
    if ordered:
        rest = [p for p in env["PATH"].split(os.pathsep)
                if p and os.path.normcase(os.path.abspath(p)) not in seen]
        env["PATH"] = os.pathsep.join(ordered + rest)
    return env


# ================= 子进程强杀 =================

try:                                    # 生产：作为包导入（agent_tools.execute_shell）
    from . import win_job as _win_job
except Exception:                       # 测试/CLI：顶格导入（sys.path 里有 agent_tools）
    try:
        import win_job as _win_job
    except Exception:
        _win_job = None                 # 拿不到就降级：只走 taskkill / 进程组


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀整个进程树。

    **先试 Job Object**（Windows，见 `win_job` 模块）：它记的是**进程归属**，能把
    "杀掉中间 shell 之后被重新挂靠的孙进程"一起杀掉 —— `taskkill /T` 按父子链遍历会漏掉它们
    （2026-09-30 实测：bash.exe 已消失、sleep.exe 还活着，还占着输出管道）。
    拿不到 job（非 Windows / 建 job 失败）就退回原来的做法 —— **功能只会更好，不会更差**。
    """
    job = getattr(proc, "_ab_job", None)
    if job is not None and _win_job is not None and _win_job.terminate(job):
        return
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _defer_pipe_cleanup(proc, *threads) -> None:
    """后台回收：等读线程结束再关管道（**主路径不等**）。"""
    def _reap():
        for t in threads:
            try:
                if t is not None:
                    t.join()
            except Exception:
                pass
        for _pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            try:
                if _pipe is not None:
                    _pipe.close()
            except Exception:
                pass
    try:
        threading.Thread(target=_reap, name="exec-reap", daemon=True).start()
    except Exception:
        pass


def _close_pipes_nonblocking(proc, *threads) -> None:
    """关掉子进程的管道 —— 但**绝不为它阻塞**。

    审计 B21 的原意是"别把 fd 留到下一次调用"，那是对的；**代价却没算到**：
    `BufferedReader.close()` 要等读线程手里的锁，而那个锁只在**所有写端持有者退出**时才放开。
    逃过 taskkill 的孙进程正是这样的持有者 —— 于是收尾把整个工具拖到命令自然结束
    （真机：`sleep 130 && echo …`（timeout=120）拖到 **130.2s** 才返回，
    看上去就像"超时没生效、跑完才判"）。

    读线程还活着时，把"等它结束 + 关管道"交给 daemon 回收线程：主路径立刻返回；
    fd 与线程随后（孙进程退出时）自然收干净。若孙进程永不退出，会留下一个 daemon 线程与
    两个 fd —— 那是"主路径绝不阻塞"的必要代价，且它**不再占着回合/作业槽位**。
    """
    if proc is None:
        return
    pending = [t for t in threads if t is not None and t.is_alive()]
    if pending:
        _defer_pipe_cleanup(proc, *threads)
        return
    for _pipe in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if _pipe is not None:
                _pipe.close()
        except Exception:
            pass


def _orphan_note(*threads) -> str:
    """被杀之后：若读线程仍卡着，说明有孙进程逃过了强杀 —— **如实写出来**。

    判据就是"管道还有写端持有者"（读线程没结束）。给 0.2 秒宽限，免得把"还没轮到调度"
    误报成孤儿（普通命令被杀后读线程会在毫秒级退出，实测不会误报）。
    """
    try:
        import time as _t
        _t.sleep(0.2)
        alive = [t for t in threads if t is not None and t.is_alive()]
    except Exception:
        alive = []
    if not alive:
        return ""
    return ("\n⚠️ 可能有子进程逃过强杀（**没拿到 Job Object 兜底时**才会这样：taskkill 按父子链遍历，会漏掉被重新挂靠的孙进程）："
            "它可能仍在运行，并且还占着输出管道。若这条命令会写盘/提交/删除，"
            "请先核对目标状态再决定是否重做。")


# ================= 流式输出截断（防 OOM） =================

def _drain_head_tail(pipe, head_chunks, tail_buf, head_bytes, tail_bytes, total_ref):
    """读管道，只保留 head 40% + tail 60%，中间超限部分丢弃。"""
    head_collected = 0
    tail_collected = 0
    try:
        while True:
            data = pipe.read(4096)
            if not data:
                break
            total_ref[0] += len(data)
            if head_collected < head_bytes:
                keep = min(len(data), head_bytes - head_collected)
                head_chunks.append(data[:keep])
                head_collected += keep
                data = data[keep:]
                if not data:
                    continue
            tail_buf.append(data)
            tail_collected += len(data)
            while tail_collected > tail_bytes and tail_buf:
                tail_collected -= len(tail_buf.popleft())
    except (ValueError, OSError):
        pass


def _drain_head(pipe, chunks, max_bytes):
    """读管道，只保留前 max_bytes（用于 stderr，错误通常出现在开头）。"""
    total = 0
    try:
        while True:
            data = pipe.read(4096)
            if not data:
                break
            if total < max_bytes:
                keep = max_bytes - total
                chunks.append(data[:keep])
            total += len(data)
    except (ValueError, OSError):
        pass


def _find_bash() -> Optional[str]:
    """定位 bash 可执行文件。Windows 上为 git-bash/MSYS 的 bash。

    优先级:
      1. AETHER_GIT_BASH_PATH 环境变量（显式指定 bash 的完整路径）
      2. git-bash/MSYS 常见安装路径（按安装概率排序）
      3. PATH 兜底（shutil.which）—— 但排除 WSL 启动器

    关键修复: shutil.which("bash") 在 PATH 含 C:\\Windows 时可能命中
    C:\\Windows\\System32\\bash.exe（WSL 启动器），导致命令落到 WSL
    发行版上执行；本机 WSL 磁盘损坏时直接报错/超时。因此任何来源的
    bash 路径只要位于 Windows\\System32 下都会被过滤掉。
    """
    def _is_wsl_launcher(path: str) -> bool:
        """判断是否为 WSL 启动器: 位于 Windows\System32 下的 bash/sh/wsl。"""
        if not path:
            return False
        norm = path.replace("/", "\\").lower()
        return ("windows\\system32" in norm and norm.endswith(".exe")) or "\\wsl" in norm

    candidates: List[str] = []

    # 1. 显式配置优先（AETHER_GIT_BASH_PATH）
    explicit = os.environ.get("AETHER_GIT_BASH_PATH")
    if explicit:
        absp = os.path.abspath(explicit)
        if os.path.isfile(absp) and not _is_wsl_launcher(absp):
            candidates.append(absp)

    # 2. git-bash/MSYS 常见安装位置（比 PATH 兜底更可靠）
    if _IS_WINDOWS:
        for p in [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\msys64\usr\bin\bash.exe",
        ]:
            if os.path.isfile(p) and not _is_wsl_launcher(p):
                candidates.append(p)


    # 2.5 从 PATH 中的 git.exe 推导 git 安装根（跨机器通用，GitHub 友好；
    #     覆盖 git 装在非 C 盘、但 PATH 里有 git 的情况）
    if _IS_WINDOWS:
        try:
            git_exe = shutil.which("git")
        except Exception:
            git_exe = None
        if git_exe:
            git_root = os.path.dirname(os.path.dirname(os.path.abspath(git_exe)))
            for sub in (r"usr\bin\bash.exe", r"bin\bash.exe"):
                p = os.path.join(git_root, sub)
                if os.path.isfile(p) and not _is_wsl_launcher(p):
                    candidates.append(p)

    # 3. PATH 兜底（排除 WSL 启动器）
    try:
        found = shutil.which("bash")
    except Exception:
        found = None
    if found and not _is_wsl_launcher(found):
        candidates.append(found)

    # 去重保序
    seen = set()
    result = []
    for c in candidates:
        key = os.path.normcase(os.path.abspath(c))
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result[0] if result else None


def _msys_bin_dirs(bash_path: Optional[str]) -> List[str]:
    r"""从 bash 的路径推导「MSYS coreutils 所在目录」（find/sort/grep/awk 的老家）。

    为什么不能直接用 bash 所在目录：Git for Windows 把 bash.exe 放在 <Git>\bin，
    而 GNU 工具链全部住在 <Git>\usr\bin —— 只前置前者等于没修。
    所以这里用「目录里同时存在 find 和 sort」来确认，探不到就返回空列表：
    宁可退回原有行为，也不把一个不含 coreutils 的目录塞到 PATH 最前面。

    ⚠️ 与 execute_python.py 的 _msys_bin_dirs 同构，改一处必须同步另一处。
    """
    if not bash_path:
        return []
    exe = ".exe" if _IS_WINDOWS else ""
    d = os.path.dirname(os.path.abspath(bash_path))
    guesses = [d,
               os.path.join(os.path.dirname(d), "usr", "bin"),   # Git\bin -> Git\usr\bin
               os.path.join(d, "usr", "bin")]                     # 兜底：直接在给定目录下级找
    out: List[str] = []
    for g in guesses:
        if not os.path.isdir(g):
            continue
        if not all(os.path.exists(os.path.join(g, t + exe)) for t in ("find", "sort")):
            continue
        if all(os.path.normcase(g) != os.path.normcase(o) for o in out):
            out.append(g)
    return out


# ================= 主执行函数 =================

def execute_shell(command: str, workdir: Optional[str] = None,
                  timeout: int = DEFAULT_TIMEOUT,
                  stdin_data: Optional[str] = None,
                  cancel_event=None, background_job: bool = False) -> str:
    """在受限子进程中执行一条 shell 命令，返回 stdout/退出码/错误信息。

    参数:
        command: 要执行的 shell 命令（bash 语法；Windows 上经 git-bash 执行）
        workdir: 工作目录（默认取当前进程目录；不存在时报错）
        timeout: 最大允许执行秒数（自动 clamp 到 [1, MAX_TIMEOUT]）
        stdin_data: 可选，写入子进程 stdin 的字符串（命令需要交互输入时用）
        cancel_event: 仅供编排器注入。一个 threading.Event；被 set 时立刻强杀进程树并返回。
                      （模型不需要也不应该传它 —— 它不在 schema 里。）

    返回:
        执行结果字符串（包含 stdout/stderr 或错误描述）
    """
    # 0. 参数守卫：clamp 超时
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    if not isinstance(command, str) or not command.strip():
        return "❌ 参数错误: command 必须是非空字符串"

    # 1. 命令安全检查（尽力而为）
    error = check_command_safety(command)
    if error:
        return f"❌ 安全拦截: {error}"

    # 2. 定位 bash
    bash_path = _find_bash()
    if not bash_path:
        return "❌ 找不到 bash 解释器（需要 git-bash / MSYS 或 POSIX 环境）"

    # 3. 解析工作目录
    run_dir = os.getcwd()
    if workdir:
        run_dir = os.path.expanduser(os.path.abspath(workdir))
        if not os.path.isdir(run_dir):
            return f"❌ 工作目录不存在: {run_dir}"

    # 4. 准备干净的运行环境
    clean_env = clean_environment(_msys_bin_dirs(bash_path))

    # 5. 启动子进程（独立进程组，可整组强杀）
    popen_kwargs = dict(
        cwd=run_dir,
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        bufsize=0,
    )
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    proc = None
    t_out = t_err = None
    try:
        proc = subprocess.Popen([bash_path, "-c", command], **popen_kwargs)
        # Windows：把子进程挂进 Job Object —— 强杀时才能连"被重新挂靠的孙进程"一起杀掉
        # （taskkill /T 按父子链遍历会漏）。失败就静默降级：_kill_process_tree 会走原路。
        # 刻意不设 KILL_ON_JOB_CLOSE —— 成功路径上用户故意放到后台的子进程照旧活着（不污染）。
        if _win_job is not None:
            try:
                proc._ab_job = _win_job.create_for(proc, kill_on_close=bool(background_job))
            except Exception:
                proc._ab_job = None

        # 6. 可选 stdin
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data.encode("utf-8", errors="replace"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        # 7. 后台线程流式读输出（head+tail 截断，避免无限输出撑爆内存）
        head_bytes = int(MAX_STDOUT_BYTES * 0.4)
        tail_bytes = MAX_STDOUT_BYTES - head_bytes
        stdout_head, stdout_tail = [], deque()
        stderr_chunks = []
        stdout_total = [0]
        t_out = threading.Thread(
            target=_drain_head_tail,
            args=(proc.stdout, stdout_head, stdout_tail, head_bytes, tail_bytes, stdout_total),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain_head, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True
        )
        t_out.start(); t_err.start()

        # 8. 轮询：检查退出、超时
        import time as _time
        deadline = _time.monotonic() + timeout
        status = "success"
        while proc.poll() is None:
            # 取消检查放在超时检查之前：主人按了停止，就该立刻停，不必等到超时。
            if cancel_event is not None and cancel_event.is_set():
                _kill_process_tree(proc)
                status = "cancelled"
                break
            if _time.monotonic() > deadline:
                _kill_process_tree(proc)
                status = "timeout"
                break
            try:
                proc.wait(timeout=min(0.05, max(0.0, deadline - _time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            _time.sleep(0.05)

        killed = status != "success"
        # ⚠️ 被杀之后**绝不等读线程**（2026-09-30 真机）：复合命令会多出一层 shell，
        #    taskkill 杀掉它之后，真正干活的孙进程被重新挂靠、逃出 /T 的遍历，仍占着管道 ——
        #    等读线程 = 等那条命令自然结束（真机：timeout=120 的 sleep 130 拖到 130.2s 才返回）。
        if not killed:
            t_out.join(timeout=3); t_err.join(timeout=3)

        stdout_text = _format_truncated(
            b"".join(stdout_head), b"".join(stdout_tail), stdout_total[0])
        err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_text = err_text.strip()

        if status == "cancelled":
            return "⏹ 已被取消（收到中断请求），已强杀整个进程树" + _orphan_note(t_out, t_err)
        if status != "success":
            return (f"⏰ 执行超时（超过 {timeout} 秒），已强杀整个进程树"
                    + _orphan_note(t_out, t_err))

        exit_code = proc.returncode
        output_parts = []
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(f"[stderr]\n{stderr_text}")
        if not output_parts:
            output_parts.append("(命令执行成功，但无输出)")
        if exit_code != 0:
            return f"❌ 执行失败（退出码 {exit_code}）:\n" + "\n".join(output_parts)
        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"⏰ 执行超时（超过 {timeout} 秒）"
    except Exception as e:
        return f"❌ 执行异常: {e}"
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)
        # 关闭管道，别把 fd 留到下一次调用（审计 B21）—— 但**绝不为它阻塞**（见该函数注释）
        _close_pipes_nonblocking(proc, t_out, t_err)
        if _win_job is not None and proc is not None:
            _win_job.close(getattr(proc, "_ab_job", None))     # 关句柄（不杀里面还活着的进程）


# ================= 小工具 =================

def _format_truncated(head_part: bytes, tail_part: bytes, total: int) -> str:
    """把捕获的 stdout 解码成文本。head/tail 之间插**显式分隔**（审计 B21）。

    旧实现把 head 与 tail 直接拼在一起、中间没有任何标记，读到的文本像是连续的，
    模型会把中间丢了几万字节的两段当成一句话读下去。
    """
    head_text = head_part.decode("utf-8", errors="replace")
    tail_text = tail_part.decode("utf-8", errors="replace")
    captured = len(head_part) + len(tail_part)
    if total > captured and tail_text:
        omitted = total - captured
        return (head_text
                + f"\n\n... [OUTPUT TRUNCATED - 中间 {omitted:,} bytes 已丢弃"
                  f"（共 {total:,} bytes）；以下是**末尾**内容] ...\n\n"
                + tail_text)
    return head_text + tail_text


# ================= 工具的 Schema =================

execute_shell_schema = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": (
            "执行一条 shell 命令，并返回执行结果。"
            "适用于文件/目录操作、git、npm、网络请求、构建脚本等场景。"
            "命令运行在受限子进程中：有超时（默认 30 秒，最大 120 秒；**到点强杀整棵进程树**"
            "—— Windows 用 Job Object，连被重新挂靠的子进程也一起杀 —— 本次输出作废并立刻返回）、"
            "输出会截断、环境变量已清洗（凭据不可见）。"
            "高危命令会被拦截（系统目录删除、磁盘格式化、关机重启、反弹 shell、"
            "下载执行链等）；普通文件删除/清理（如 rm -rf node_modules）放行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令（bash 语法，Windows 上经 git-bash 执行）。",
                },
                "workdir": {
                    "type": "string",
                    "description": "工作目录（绝对路径或相对路径，默认当前目录）。",
                },
                "timeout": {
                    "type": "integer",
                    "description": ("执行超时时间（秒），默认 30，最大 120。**到点强杀整棵进程树**"
                                    "（含子进程；Windows 用 Job Object 保证不漏），本次输出作废"
                                    "并**立刻返回**。把它设得比命令自然时长小 = 主动放弃这条命令。"),
                    "default": 30,
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT,
                },
                "stdin_data": {
                    "type": "string",
                    "description": "可选，写入命令标准输入的字符串（需要交互输入时用）。",
                }
            },
            "required": ["command"]
        }
    }
}


# ================= CLI 入口 =================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="受限 shell 命令执行工具")
    parser.add_argument("command", nargs="?", help="要执行的命令（不传则进入交互模式）")
    parser.add_argument("--workdir", "-w", default=None, help="工作目录")
    parser.add_argument("--timeout", "-t", type=int, default=DEFAULT_TIMEOUT,
                        help=f"超时秒数（默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}）")
    parser.add_argument("--stdin", default=None, help="写入 stdin 的字符串")
    args = parser.parse_args()

    if args.command:
        print(execute_shell(args.command, workdir=args.workdir,
                            timeout=args.timeout, stdin_data=args.stdin))
    else:
        # 交互模式：逐行执行（Ctrl+C / exit 退出）
        print("execute_shell 交互模式（输入 exit 退出）")
        while True:
            try:
                cmd = input("shell> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not cmd.strip():
                continue
            if cmd.strip().lower() in ("exit", "quit"):
                break
            print(execute_shell(cmd, workdir=args.workdir, timeout=args.timeout))
