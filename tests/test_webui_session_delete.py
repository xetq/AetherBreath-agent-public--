# -*- coding: utf-8 -*-
"""
WebUI 会话删除的血缘一致性测试（agent_webui/backend/sessions.py）
=================================================================
主人 2026-09-13 要求：压缩视图与对话原文有血缘，删除会话时必须一起管——
不许出现"原文没了、视图还留着"的孤儿。

覆盖：
  1. 三件套（原文 + 视图 + 事件流）一起移入 .trash，原位置全空
  2. 没压缩过的会话（无视图）照常删，不报错
  3. 只压过、没事件流的会话也能删
  4. 会话不存在 → 明确失败，不误动视图
  5. 移动失败（占用/权限）→ 会话仍删成功，但 warnings 如实报告（不许静默留孤儿）

跑法（项目根）：venv/Scripts/python -m pytest tests/test_webui_session_delete.py -q
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent_webui" / "backend"))

import sessions as S  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把三个目录都指到 tmp：working_memory / 视图目录 / .trash 根。"""
    wm = tmp_path / "agent_memory" / "working_memory"
    views = tmp_path / "agent_memory" / ".condensed_sessions"
    wm.mkdir(parents=True, exist_ok=True)
    views.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(S.config, "WORKING_MEMORY_DIR", wm)
    monkeypatch.setattr(S.config, "WEBUI_DIR", tmp_path)
    monkeypatch.setattr(S, "VIEW_DIR", views)
    return {"wm": wm, "views": views, "root": tmp_path}


def _mk_session(env, sid="s1", msgs=None):
    (env["wm"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "messages": msgs or [{"role": "user", "content": "hi"}]},
                   ensure_ascii=False), encoding="utf-8")


def _mk_view(env, sid="s1", with_events=True):
    (env["views"] / f"{sid}.json").write_text(
        json.dumps({"session_id": sid, "version": 1, "source_len": 1, "messages": []}), encoding="utf-8")
    if with_events:
        (env["views"] / f"{sid}.events.jsonl").write_text(
            json.dumps({"event": "compact", "version": 1}) + "\n", encoding="utf-8")


def test_delete_moves_all_three(env):
    _mk_session(env)
    _mk_view(env)
    r = S.delete_session("s1")
    assert r["ok"] is True
    assert {m["kind"] for m in r["moved"]} == {"session", "view", "view_events"}
    # 原位置必须全空（不留孤儿）
    assert not (env["wm"] / "s1.json").exists()
    assert not (env["views"] / "s1.json").exists()
    assert not (env["views"] / "s1.events.jsonl").exists()
    # 备份里三件都在
    assert list((env["root"] / ".trash" / "working_memory").glob("s1.*.json"))
    assert list((env["root"] / ".trash" / "condensed_sessions").glob("s1.*.json"))
    assert list((env["root"] / ".trash" / "condensed_sessions").glob("s1.events.*.jsonl"))
    assert "warnings" not in r


def test_delete_without_view_is_fine(env):
    """没压缩过的会话：只有原文，照常删。"""
    _mk_session(env, "plain")
    r = S.delete_session("plain")
    assert r["ok"] and [m["kind"] for m in r["moved"]] == ["session"]
    assert not (env["wm"] / "plain.json").exists()


def test_delete_view_without_events(env):
    _mk_session(env, "s2")
    _mk_view(env, "s2", with_events=False)
    r = S.delete_session("s2")
    assert {m["kind"] for m in r["moved"]} == {"session", "view"}
    assert not (env["views"] / "s2.json").exists()


def test_delete_missing_session_leaves_view_alone(env):
    """会话不存在 → 失败退出，且不许顺手把别人的视图挪走。"""
    _mk_view(env, "s3")
    r = S.delete_session("s3")
    assert r["ok"] is False and "不存在" in r["error"]
    assert (env["views"] / "s3.json").exists()


def test_move_failure_is_reported_not_silent(env, monkeypatch):
    """视图移不动时：会话仍删成功，但必须如实报 warnings（不许静默留孤儿）。"""
    _mk_session(env, "s4")
    _mk_view(env, "s4")

    real_move = S.shutil.move

    def fake_move(src, dst):
        if str(src).endswith("s4.json") and ".condensed_sessions" in str(src):
            raise PermissionError("文件被占用")
        return real_move(src, dst)

    monkeypatch.setattr(S.shutil, "move", fake_move)
    r = S.delete_session("s4")
    assert r["ok"] is True
    kinds = [m["kind"] for m in r["moved"]]
    assert kinds[0] == "session" and "view" not in kinds      # 视图没走成
    assert "view_events" in kinds                             # 事件流照常走（只坏了那一个）
    assert r.get("warnings") and "view" in r["warnings"][0]
    assert not (env["wm"] / "s4.json").exists()          # 会话确实走了
    assert (env["views"] / "s4.json").exists()           # 视图还在（但已被警告标出）


def test_events_file_suffix_preserved(env):
    """备份文件名要能一眼认出是哪个文件，别把 .events 吞掉。"""
    _mk_session(env, "s5")
    _mk_view(env, "s5")
    S.delete_session("s5")
    names = [p.name for p in (env["root"] / ".trash" / "condensed_sessions").iterdir()]
    assert any(n.startswith("s5.") and n.endswith(".json") and ".events" not in n for n in names)
    assert any(n.startswith("s5.events.") and n.endswith(".jsonl") for n in names)
