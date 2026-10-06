"""
工具名称: execute_python
功能: 执行一段 Python 代码，并返回输出结果。

安全策略（资源护栏模型，参照宿主 Agent 的 execute_code 标准）:
  1. 进程隔离    : 独立子进程 + 独立进程组；超时可整棵进程树强杀，不残留孤儿进程。
  2. 资源护栏    : 超时（有上限 clamp）+ 输出 head/tail 截断（防死循环 print 撑爆内存）。
  3. 凭据隔离    : 环境变量只保留白名单前缀 + 剔除秘密子串（KEY/TOKEN/SECRET...），
                   防止脚本读到并外泄 API key 等凭据。
  4. 能力拦截    : AST 黑名单「尽力而为」地拦截危险能力调用。注意——这不是安全边界，
                   恶意代码可绕过 AST 文本检查（如 __class__.__base__.__subclasses__、
                   getattr(__builtins__,'__import__')）。真正的可信执行需要 OS 级沙箱/容器。

  🔴 定位声明: 本模块是「防失控的护栏」，不是「安全沙箱」。
    不要用它执行来自不可信来源、或用户无法确认意图的代码。
"""

import ast
import logging
import os
import platform
import signal
import subprocess
import sys
import tempfile
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ================= 配置（全部可调） =================
DEFAULT_TIMEOUT = 30          # 默认超时（秒）
MAX_TIMEOUT = 120             # 超时硬上限（防止调用方传 timeout=999999）
MAX_STDOUT_BYTES = 50_000     # 输出截断上限（head 40% + tail 60%）
MAX_STDERR_BYTES = 10_000
_IS_WINDOWS = platform.system() == "Windows"

# ---- 工作区临时目录（替代系统 Temp，避免占用 C 盘） ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_TMP_DIR = os.path.join(_PROJECT_ROOT, "agent_workspace", ".tmp_exec")

# 项目 venv 的 bin 目录（审计 B18：与 execute_shell 对齐，前置进子进程 PATH）
_VENV_BIN_DIR = "Scripts" if os.name == "nt" else "bin"
PROJECT_VENV_BIN = os.path.join(_PROJECT_ROOT, "venv", _VENV_BIN_DIR)

# ---- 危险能力黑名单（AST 层，尽力而为） ----
# 危险模块导入：凡 import 到这些模块直接拦截（importlib 等是逃逸工具）
DANGEROUS_IMPORTS = {
    "ctypes", "cffi", "pickle", "marshal", "importlib", "pty", "telnetlib",
    "pty", "_thread", "threading",  # threading 可用于脱离主流程
}
# ---------------------------------------------------------------
# 危险调用判定：**分档**匹配（审计 B5 重写）
# ---------------------------------------------------------------
# 旧实现是一张「函数名黑名单」，判定时**不看接收者是谁**：
#   · 实测 `x.replace("a","b")`、`obj.run()`、`d.call()` 全被拦 —— 字符串替换
#     这种最高频操作在沙箱里直接不可用；
#   · 而 `os.replace()` 因为白名单反而放行；
#   · 同一个方法名，判定取决于接收者是「变量」还是「os」，语义自相矛盾。
# 现分三档：
#   · _BARE_DANGER    裸名调用一律拦（`eval(...)` / `__import__(...)` / `system(...)`）
#   · _KNOWN_DANGER_OBJECTS 上的属性调用：走该对象白名单，白名单外全拦
#   · _ALWAYS_DANGER  名字足够独特（命令执行入口），**任何**接收者上都拦
#
# ⚠️ 边界声明（别把这里当安全边界）：AST 文本判定永远能被人绕过
#    （`f = os; f.system(...)`、`getattr(__builtins__, "__import__")`…）。
#    这一层挡的是「手滑与明显高危」，不是「攻击者」。
_BARE_DANGER = {
    "eval", "exec", "compile", "__import__",
    # 命令执行入口：裸名出现几乎只能是 from os import system / from subprocess import Popen
    "system", "popen", "Popen", "getoutput", "check_output", "check_call",
    "execl", "execle", "execlp", "execv", "execve", "execvp",
    "spawn", "spawnl", "spawnv", "spawnvp", "spawnlp", "fork",
}
# 名字足够独特 —— 在普通对象上几乎不出现，任何接收者都拦
_ALWAYS_DANGER = {
    "system", "popen", "Popen", "getoutput", "check_output", "check_call",
    "execl", "execle", "execlp", "execv", "execve", "execvp",
    "spawnl", "spawnv", "spawnvp", "spawnlp", "fork",
}
# 判定时按"已知危险对象"走白名单的模块名
_KNOWN_DANGER_OBJECTS = ("os", "sys", "shutil", "subprocess", "pickle", "ctypes")
# 兼容别名：旧名保留，供外部脚本/测试按老习惯引用
DANGEROUS_FUNCS = _BARE_DANGER | _ALWAYS_DANGER
# 对象 -> 允许的属性白名单：白名单内显式放行（优先于黑名单），
# 白名单之外且命中高危对象的属性调用才被拦截。文件/目录删除已放行，
# 否则只能建不能删。
# ⚠️ 审计 B1：`check_output` 曾被放进来 —— 而它同时也在黑名单里，白名单优先
#    导致黑名单那条**永久失效**，实测 `subprocess.check_output([...])` 真跑起来了
#    （等同于给任意命令执行留正门）。它和 PIPE/STDOUT/DEVNULL 不是一类东西，已移除。
ALLOWED_ATTRIBUTES = {
    "os": {"path", "getcwd", "chdir", "listdir", "walk", "scandir", "stat", "getenv", "environ", "name",
           "sep", "linesep", "pathsep", "curdir", "pardir", "mkdir", "makedirs",
           # 目录树只读遍历（2026-09-22 主人点名解除）：walk / scandir 只读、不删不改，
           # 能力不超出同在白名单的 listdir；旧实现一律拦属过严，与 listdir 放行自相矛盾。
           # 文件/目录删除与重命名（agent 需要清理能力）
           "remove", "unlink", "rmdir", "removedirs", "replace", "rename"},
    "sys": {"path", "version", "version_info", "platform", "executable", "stdout",
            "stderr", "stdin", "getrecursionlimit", "setrecursionlimit"},
    "shutil": {"which", "get_terminal_size", "rmtree",
               # rmtree 递归删除非空目录树（仅空目录时 os.rmdir 不够用）
               "move", "copy", "copyfile"},
    "subprocess": {"PIPE", "STDOUT", "DEVNULL"},
}

# ---- 环境变量清洗规则 ----
# 白名单前缀（保留）；秘密子串（剔除）；Windows 必备（保留，否则 socket/subprocess 崩）
_SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP",
    "SHELL", "LOGNAME", "XDG_", "VIRTUAL_ENV", "CONDA", "PYTHONPATH", "PYTHONHOME",
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
})

# ================= AST 安全检查（尽力而为） =================

def _is_dangerous_call(node: ast.Call) -> Optional[str]:
    """判断一个调用节点是否命中危险能力（分档，见上方常量注释）。"""
    func = node.func

    # 1) 裸名调用: eval(...) / __import__(...) / system(...)
    if isinstance(func, ast.Name):
        if func.id in _BARE_DANGER:
            return f"禁止调用危险函数: {func.id}()"
        return None

    # 2) 属性调用: os.system(...) / shutil.rmtree(...) / x.replace(...)
    if isinstance(func, ast.Attribute):
        base = func.value
        attr = func.attr
        if isinstance(base, ast.Name):
            obj = base.id
            allowed = ALLOWED_ATTRIBUTES.get(obj, set())
            # 2a. 已声明对象的白名单：显式放行（os.remove 等要能用）
            if attr in allowed:
                return None
            # 2b. 已知危险对象：白名单之外的一切都拦
            if obj in _KNOWN_DANGER_OBJECTS:
                return f"禁止调用: {obj}.{attr}()"
        # 2c. 接收者未知（变量/表达式/字面量）：**只按"名字足够独特"的执行入口拦**，
        #     不再对 run / call / replace / remove 这类通用名一刀切 ——
        #     那些名字在普通对象上到处都是（实测 x.replace 曾被误拦）。
        if attr in _ALWAYS_DANGER:
            return f"禁止调用危险方法: {attr}()"
        # 动态导入：getattr(x, "__import__")() 之类
        if isinstance(base, ast.Call) and attr == "__import__":
            return "禁止通过动态调用导入模块"
    return None


def _is_dangerous_node(node: ast.AST) -> Optional[str]:
    """递归遍历 AST，返回首个危险描述；无危险则返回 None。"""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] in DANGEROUS_IMPORTS:
                return f"禁止导入危险模块: {alias.name}"
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in DANGEROUS_IMPORTS:
            return f"禁止从危险模块导入: {node.module}"
    elif isinstance(node, ast.Call):
        danger = _is_dangerous_call(node)
        if danger:
            return danger
    for child in ast.iter_child_nodes(node):
        result = _is_dangerous_node(child)
        if result:
            return result
    return None


def validate_code(code: str) -> Optional[str]:
    """验证代码安全性。返回错误信息或 None。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"代码语法错误: {e}"
    return _is_dangerous_node(tree)


# ================= 环境变量清洗 =================

def clean_environment() -> Dict[str, str]:
    """构建清洗后的子进程环境。

    规则（顺序）:
      1. 秘密子串（KEY/TOKEN/SECRET/PASSWORD...）剔除 —— 防凭据泄露，这是真正的安全关键。
      2. 白名单前缀保留（PATH/HOME/LANG/TERM/...）。
      3. Windows 必备变量保留（SYSTEMROOT/COMSPEC/...），否则 socket/subprocess 直接崩。
      4. 其余（含 AETHER_*/非白名单变量）全部丢弃。
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
    # 兜底：必须保证 PATH 存在，否则子进程连解释器都找不到
    if "PATH" not in env:
        env["PATH"] = os.defpath
    # PATH 前置顺序与 execute_shell.clean_environment **保持一致**（审计 B18）：
    #   项目 venv（python/pip）→ MSYS coreutils（find/sort/grep）→ 原 PATH。
    #
    # 为什么 venv 要前置：子进程里再调 `python` / `pip` 时，落到项目 venv 才是
    # 用户期待的那个（旧实现没做这一步，与 execute_shell 漂移 —— 而两处注释
    # 都写着自己"同构"，属于注释骗人）。
    #
    # 为什么必须显式前置 MSYS 目录：本工具的子进程同样不 source /etc/profile，
    # 拿到的是裸 Windows PATH —— System32 里 DOS 版 FIND.EXE / SORT.EXE 会抢在
    # GNU 版之前，用户代码里再起 shell 就会拿到错的那一个。
    # 零代码回退：设 AETHER_MSYS_PATH_PREPEND=0。
    prepend: List[str] = []
    if os.path.isdir(PROJECT_VENV_BIN):
        prepend.append(PROJECT_VENV_BIN)
    if os.environ.get("AETHER_MSYS_PATH_PREPEND", "1") != "0":
        prepend.extend(_msys_bin_dirs())
    if prepend:
        seen = {os.path.normcase(os.path.abspath(g)) for g in prepend}
        rest = [p for p in env["PATH"].split(os.pathsep)
                if p and os.path.normcase(os.path.abspath(p)) not in seen]
        env["PATH"] = os.pathsep.join(prepend + rest)
    return env


def _msys_bin_dirs() -> List[str]:
    r"""定位 MSYS coreutils 目录（find/sort/grep/awk 的老家）。

    与 execute_shell.py 的同名函数同构，改一处必须同步另一处。
    为什么宁可复制一遍也不抽公共模块：两个工具都带 `if __name__ == "__main__"`
    入口、能被直接当脚本跑，一旦引入包内相对导入那种用法就会炸。
    判据与 execute_shell 版一致：目录里必须同时有 find 和 sort 才算数，
    探不到就返回空列表 —— 宁可退回原有行为，也不把不含 coreutils 的目录插到 PATH 最前。
    """
    exe = ".exe" if _IS_WINDOWS else ""
    cands: List[str] = []
    explicit = os.environ.get("AETHER_GIT_BASH_PATH")
    if explicit:
        cands.append(os.path.dirname(os.path.abspath(explicit)))
    # 从 PATH 里的 git.exe 推导安装根：通常是 Git\cmd\git.exe，要的是同级 usr\bin
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d and os.path.isfile(os.path.join(d, "git" + exe)):
            cands.append(os.path.join(os.path.dirname(os.path.abspath(d)), "usr", "bin"))
            break
    for guess in (r"C:\Program Files\Git\usr\bin", r"C:\msys64\usr\bin"):
        cands.append(guess)
    out: List[str] = []
    for g in cands:
        if not os.path.isdir(g):
            continue
        if not all(os.path.exists(os.path.join(g, t + exe)) for t in ("find", "sort")):
            continue
        if all(os.path.normcase(g) != os.path.normcase(o) for o in out):
            out.append(g)
    return out


# ================= 子进程强杀 =================

try:                                    # 生产：作为包导入（agent_tools.execute_python）
    from . import win_job as _win_job
except Exception:                       # 测试/CLI：顶格导入（sys.path 里有 agent_tools）
    try:
        import win_job as _win_job
    except Exception:
        _win_job = None                 # 拿不到就降级：只走 taskkill / 进程组


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀整个进程树（**先试 Job Object**，拿不到再退回 taskkill / 进程组）。

    与 execute_shell 同一套判据：`taskkill /T` 按父子链遍历，漏得掉被重新挂靠的孙进程
    （用户代码里起的子进程就可能这样逃掉，并一直占着输出管道）。
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

    审计 B21 的原意（"别把 fd 留到下一次调用"）没错，代价却没算到：
    `BufferedReader.close()` 要等读线程手里的锁，而那个锁只在**所有写端持有者退出**时才放开。
    逃过 taskkill 的孙进程正是这样的持有者 —— 于是收尾把工具拖到命令自然结束
    （真机：timeout=120 的 `sleep 130` 拖到 130.2s 才返回，看着像"超时没生效"）。

    读线程还活着时，把"等它结束 + 关管道"交给 daemon 回收线程：主路径立刻返回。
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
    """被杀之后：若读线程仍卡着，说明有子进程逃过了强杀 —— **如实写出来**。"""
    try:
        import time as _t
        _t.sleep(0.2)
        alive = [t for t in threads if t is not None and t.is_alive()]
    except Exception:
        alive = []
    if not alive:
        return ""
    return ("\n⚠️ 可能有子进程逃过强杀（**没拿到 Job Object 兜底时**才会这样：taskkill 按父子链遍历，会漏掉被重新挂靠的孙进程）："
            "它可能仍在运行，并且还占着输出管道。若这段代码会写盘/提交/删除，"
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


# ================= 主执行函数 =================

def _write_script_with_retry(code: str, tmp_dir: str, retries: int = 3):
    """写入脚本文件并做写后确认（stat 校验字节数），失败带退避重试。

    针对 Windows 环境 I/O 抖动（"写文件偶发超时但实际成功"）：
    与其让调用方猜，不如在写入后立刻 stat 二次确认；偶发失败自动重试。

    注意: newline="\\n" 禁止 Windows 换行转换，否则 UTF-8 字节数无法精确预测。

    返回 (tmp_path, attempts, elapsed_sec)。
    全部重试仍失败则抛出最后一次 OSError。
    """
    expected_size = len(code.encode("utf-8"))
    last_exc: Optional[OSError] = None
    attempts = 0
    t0 = _time.perf_counter()
    for attempt in range(1, retries + 1):
        tmp_path = None
        attempts = attempt
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False,
                encoding="utf-8", newline="\n", dir=tmp_dir,
            ) as f:
                f.write(code)
                f.flush()
                os.fsync(f.fileno())
                tmp_path = f.name
            # 写后二次确认：stat 校验存在性与字节数
            st = os.stat(tmp_path)
            if st.st_size != expected_size:
                raise OSError(
                    f"写后确认失败: 期望 {expected_size} bytes, 实际 {st.st_size} bytes"
                )
            return tmp_path, attempts, _time.perf_counter() - t0
        except OSError as e:
            last_exc = e
            # 清理可能残留的半成品文件
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if attempt < retries:
                _time.sleep(0.2 * attempt)  # 退避 0.2s / 0.4s / 0.6s
    assert last_exc is not None
    raise last_exc


def execute_python(code: str, timeout: int = DEFAULT_TIMEOUT, cancel_event=None,
                   background_job: bool = False) -> str:
    """在受限子进程中执行 Python 代码，返回 stdout 或错误信息。

    参数:
        code: 要执行的 Python 代码字符串
        timeout: 最大允许执行秒数（自动 clamp 到 [1, MAX_TIMEOUT]）

    返回:
        执行结果字符串（包含 stdout/stderr 或错误描述）
    """
    t_total0 = _time.perf_counter()

    # 0. 参数守卫：clamp 超时
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    # 1. 代码安全检查（尽力而为）
    error = validate_code(code)
    if error:
        return f"❌ 安全拦截: {error}"

    # 2. 写入临时文件（重试 + 写后确认，规避环境 I/O 抖动）
    write_attempts = 0
    try:
        os.makedirs(WORKSPACE_TMP_DIR, exist_ok=True)
        tmp_path, write_attempts, write_sec = _write_script_with_retry(
            code, WORKSPACE_TMP_DIR
        )
    except Exception as e:
        return f"❌ 创建临时文件失败（尝试 {write_attempts} 次）: {e}"

    # 3. 准备干净的运行环境
    clean_env = clean_environment()
    python_executable = sys.executable
    run_dir = WORKSPACE_TMP_DIR

    # 4. 启动子进程（独立进程组，可整组强杀）
    popen_kwargs = dict(
        cwd=run_dir,
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,          # 输入交给 /dev/null，防 input() 挂死
        bufsize=0,
    )
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    proc = None
    t_out = t_err = None        # finally 要引用它们（Popen 失败时也得能安全收尾）
    try:
        t_start0 = _time.perf_counter()
        proc = subprocess.Popen([python_executable, tmp_path], **popen_kwargs)
        start_sec = _time.perf_counter() - t_start0
        # Windows：挂进 Job Object（强杀时才能连被重新挂靠的孙进程一起杀掉）；失败静默降级。
        # 不设 KILL_ON_JOB_CLOSE —— 成功路径上用户故意放到后台的子进程照旧活着（不污染）。
        if _win_job is not None:
            try:
                proc._ab_job = _win_job.create_for(proc, kill_on_close=bool(background_job))
            except Exception:
                proc._ab_job = None

        # 5. 后台线程流式读输出（head+tail 截断，避免死循环 print 撑爆内存）
        head_bytes = int(MAX_STDOUT_BYTES * 0.4)
        tail_bytes = MAX_STDOUT_BYTES - head_bytes
        stdout_head, stdout_tail = [], deque()
        stderr_chunks = []
        stdout_total = [0]
        import threading
        t_out = threading.Thread(
            target=_drain_head_tail,
            args=(proc.stdout, stdout_head, stdout_tail, head_bytes, tail_bytes, stdout_total),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain_head, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True
        )
        t_out.start(); t_err.start()

        # 6. 轮询：检查退出、超时、输出总量
        deadline = time_now() + timeout
        status = "success"
        while proc.poll() is None:
            # 取消检查先于超时检查（same 语义：收到中断请求就立刻停）
            if cancel_event is not None and cancel_event.is_set():
                _kill_process_tree(proc)
                status = "cancelled"
                break
            if time_now() > deadline:
                _kill_process_tree(proc)
                status = "timeout"
                break
            try:
                proc.wait(timeout=min(0.05, max(0.0, deadline - time_now())))
            except subprocess.TimeoutExpired:
                pass
            time_sleep(0.05)
        exec_sec = _time.perf_counter() - t_start0

        killed = status != "success"
        # ⚠️ 被杀之后**绝不等读线程**（与 execute_shell 同一条判据，2026-09-30 真机）：
        #    用户代码里起的子进程若被重新挂靠、逃出 taskkill 的 /T 遍历，它会一直占着管道，
        #    等读线程 = 等那个子进程自然结束 —— 工具就会"超时之后又跑了很久"。
        if not killed:
            t_out.join(timeout=3); t_err.join(timeout=3)

        stdout_text = _format_truncated(
            b"".join(stdout_head), b"".join(stdout_tail), stdout_total[0])
        err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stderr_text = err_text.strip()

        if status == "cancelled":
            return "⏹ 已被取消（收到中断请求），已强杀整个进程树" + _orphan_note(t_out, t_err)
        if status != "success":
            return (
                f"⏰ 执行超时（超过 {timeout} 秒），已强杀整个进程树" + _orphan_note(t_out, t_err) + "\n"
                f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
                f"启动 {start_sec:.2f}s / 执行 {exec_sec:.2f}s / "
                f"总计 {_time.perf_counter() - t_total0:.2f}s"
            )

        exit_code = proc.returncode
        output_parts = []
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(f"[stderr]\n{stderr_text}")
        if not output_parts:
            output_parts.append("(代码执行成功，但无输出)")
        if exit_code != 0:
            return f"❌ 执行失败（退出码 {exit_code}）:\n" + "\n".join(output_parts)
        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return (
            f"⏰ 执行超时（超过 {timeout} 秒）\n"
            f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
            f"总计 {_time.perf_counter() - t_total0:.2f}s"
        )
    except Exception as e:
        return (
            f"❌ 执行异常: {e}\n"
            f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
            f"总计 {_time.perf_counter() - t_total0:.2f}s"
        )
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)
        # 关闭管道，别把 fd 留到下一次调用（审计 B21）—— 但**绝不为它阻塞**（见该函数注释）
        _close_pipes_nonblocking(proc, t_out, t_err)
        if _win_job is not None and proc is not None:
            _win_job.close(getattr(proc, "_ab_job", None))     # 关句柄（不杀里面还活着的进程）
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---- 小工具 ----
import time as _time


def time_now() -> float:
    return _time.monotonic()


def time_sleep(sec: float) -> None:
    _time.sleep(sec)


def _format_truncated(head_part: bytes, tail_part: bytes, total: int) -> str:
    """把捕获的 stdout 解码成文本。head/tail 之间插**显式分隔**（审计 B21）。

    旧实现把 head 与 tail 直接拼接，中间没有任何标记 —— 读到的文本像连续的，
    模型会把"被丢掉几万字节"的两段当成一句话读下去。现在明写"中间省略"。
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

execute_python_schema = {
    "type": "function",
    "function": {
        "name": "execute_python",
        "description": (
            "执行一段 Python 代码，并返回执行结果。"
            "适用于需要多步计算、数据处理、文件操作等场景。"
            "代码运行在受限子进程中：有超时（默认 30 秒，最大 120 秒；**到点强杀整棵进程树**"
            "—— Windows 用 Job Object，连代码里起的子进程也一起杀 —— 本次输出作废并立刻返回）、"
            "输出会截断、环境变量已清洗（凭据不可见）。"
            "代码禁止调用系统级危险能力（system/eval/importlib 等）；"
            "文件/目录删除（remove/rmdir/rmtree）和复制移动已放行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码（纯文本，不含输入交互）。",
                },
                "timeout": {
                    "type": "integer",
                    "description": ("执行超时时间（秒），默认 30，最大 120。**到点强杀整棵进程树**"
                                    "（含代码里起的子进程；Windows 用 Job Object 保证不漏），"
                                    "本次输出作废并**立刻返回**。"),
                    "default": 30,
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT,
                }
            },
            "required": ["code"]
        }
    }
}
