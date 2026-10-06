"""
read_file 的文本类型适配（2026-09-23 主人要求支持 .tsx / .ts）

钉住四件事：
  1. 代码后缀（含 ts/tsx/js/jsx/vue…）走文本读取，offset/limit 照常可用；
  2. **无扩展名**的文本文件（Dockerfile / Makefile / LICENSE）靠内容探测也能读；
  3. 编码兜底：UTF-8 失败试 GBK，并在输出里**标明编码**（免得模型把 GBK 当乱码去猜）；
  4. 二进制（含 NUL）必须被拒 —— 探测不能把可执行文件当文本读出来。

历史背景：这几个后缀原本不在白名单里，落在"不支持该格式"分支上，
于是我读前端源码只能绕道 execute_python（主人 2026-09-23 点名的就是这事）。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra in (str(PROJECT_ROOT), str(PROJECT_ROOT / "agent")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from agent_tools.file_read import read_file, read_file_schema  # noqa: E402


def _w(tmp_path: Path, name: str, text: str, enc: str = "utf-8") -> str:
    p = tmp_path / name
    p.write_text(text, encoding=enc)
    return str(p)


def test_code_extensions_are_text(tmp_path):
    """代码后缀（本任务主角：ts / tsx）+ 旧后缀，都要能读，且 offset/limit 生效。"""
    for name in ("a.ts", "b.tsx", "c.js", "d.jsx", "e.vue", "f.scss", "g.go", "h.md", "i.py", "j.json"):
        fp = _w(tmp_path, name, "line1\nline2\nline3\n")
        out = read_file(fp)
        assert out.startswith("line1"), f"{name} 未按文本读取：{out[:80]}"
        win = read_file(fp, offset=2, limit=1)
        assert "line2" in win and "line1" not in win, f"{name} 行窗口失效：{win[:80]}"


def test_no_extension_text_is_probed(tmp_path):
    """无扩展名文本：探测通过就该读出来（Dockerfile / Makefile 这类）。"""
    fp = _w(tmp_path, "Dockerfile", "FROM python:3.11\nRUN echo hi\n")
    out = read_file(fp)
    assert "FROM python:3.11" in out
    fp2 = _w(tmp_path, "Makefile", "all:\n\techo ok\n")
    assert "echo ok" in read_file(fp2)


def test_gbk_fallback_marks_encoding(tmp_path):
    """GBK 文本要能读，并且**明确标出编码**（中文 Windows 上的 CSV 常年是 GBK）。"""
    fp = _w(tmp_path, "gbk.csv", "学号,姓名\n2023000001,张三\n", enc="gbk")
    out = read_file(fp)
    assert "2023000001" in out and "张三" in out, out[:120]
    assert out.splitlines()[0].startswith("（编码"), "未标明实际编码"
    assert "gbk" in out.splitlines()[0].lower()


def test_binary_is_rejected(tmp_path):
    """二进制（含 NUL）不能被当文本读出来 —— 探测要挡住它，且给出可读的提示。"""
    p = tmp_path / "blob.bin"
    p.write_bytes(b"\x7fELF\x02\x01\x00\x00" + bytes(range(256)))
    out = read_file(str(p))
    assert out.startswith("❌"), out[:80]
    assert "不支持" in out


def test_missing_and_dir_and_docs(tmp_path):
    """回归：不存在路径 / 目录列表 / 文档格式分派都不能被这次改动带坏。"""
    assert read_file(str(tmp_path / "nope.tsx")).startswith("❌")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.ts").write_text("ok", encoding="utf-8")
    listing = read_file(str(tmp_path / "sub"))
    assert "x.ts" in listing
    assert read_file(str(tmp_path / "sub" / "x.ts")) == "ok"


def test_schema_mentions_code_extensions():
    """说明书必须与实现一致 —— 否则模型不知道能用它读 .tsx（这次的需求就白做）。"""
    desc = read_file_schema["function"]["description"]
    for token in (".tsx", ".ts ", ".jsx", "探测"):
        assert token in desc, f"schema 说明缺少 {token!r}"
