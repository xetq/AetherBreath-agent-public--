# -*- coding: utf-8 -*-
"""P5 补丁（后端 4 文件）：让编排器**如实**反映跨回合后台作业占用的槽位。

背景（真机）：挂 30 秒后台作业 + 立即汇报 → 橙点亮了，**回复一落地就灭**，
而作业还在跑。三个断点：

  1. `bridge._execute_turn` 回合起点 `_PIPES.clear()` —— 连跨回合作业的条目一起清；
  2. 管道事件只带 `background` 布尔，**没有作业身份**（"是谁在跑"说不清 = 黑箱）；
  3. 没有"占用视图"通道：`state.pipes` 只活在浏览器内存，F5 之后全空。

改动清单：
  A. bridge.py
     - `_job_info(tc)`：作业身份**以登记册为准**（超时收编的作业没有 background 入参）
     - `_pipe_emit(..., job_id, job_label)`：事件与占用条目都带作业身份；
       作业身份一旦拿到就不再被后续帧抹掉
     - `_reset_pipes_for_turn()`：回合起点清场**保留**跨回合作业的占用
     - `_orch_view()` + `GET /orch`：权威占用视图（管道 ∪ 仍活着的作业）
     - `_wrap_tool` 包装里的三处 emit 全部带上 `_job_info`
  B. agent_client.py  `orch()`：调 bridge 的 /orch（短超时）
  C. agent_proc.py    `AgentManager.orch()`：authoritative 纪律与卡片恢复同源
  D. api.py           `GET/POST /api/orch`

用法：
    python apply_p5.py --root <树根>            # 只检查锚点
    python apply_p5.py --root <树根> --apply
"""
import argparse
import sys
from pathlib import Path

# ==========================================================================
# A. bridge.py
# ==========================================================================

A1_OLD = '''def _pipe_emit(status: str, tc_id: str, tool: str, elapsed=None, layer=None,
               background=None) -> None:
'''

A1_NEW = '''def _job_info(tc) -> "tuple":
    """(是不是跨回合作业, 作业号, 可读名) —— **以作业登记册为准**，不看入参。

    为什么读 `metadata["job"]` 而不是 `metadata["background"]`：超时被收编成作业的调用
    （`_adopt_background`）从来没有 background 入参，但它确实是跨回合作业 ——
    只看入参会把这种作业漏成"普通管道"，橙点自然亮不起来。
    """
    md = getattr(tc, "metadata", None) or {}
    job = md.get("job")
    if job is not None:
        return True, getattr(job, "job_id", None), getattr(job, "label", None)
    return bool(md.get("background")), None, None


def _pipe_emit(status: str, tc_id: str, tool: str, elapsed=None, layer=None,
               background=None, job_id=None, job_label=None) -> None:
'''

A2_OLD = '''                _PIPES[tc_id] = {"tool": tool, "status": status, "layer": layer,
                                 "background": bool(background)}
'''

A2_NEW = '''                _prev = _PIPES.get(tc_id) or {}
                _PIPES[tc_id] = {"tool": tool, "status": status, "layer": layer,
                                 # 作业身份一旦拿到就不许被后续帧抹掉（pending 帧还没有它）
                                 "background": bool(background) or bool(_prev.get("background")),
                                 "job_id": job_id or _prev.get("job_id"),
                                 "job_label": job_label or _prev.get("job_label")}
'''

A3_OLD = '''            "background": bool(background),   # 跨回合作业占用的槽位（前端画橙色）
            "live": len(_PIPE_ORDER),
        })
    except Exception:
        pass
'''

A3_NEW = '''            "background": bool(background),   # 跨回合作业占用的槽位（前端画橙色）
            "job_id": job_id or None,         # 作业身份：让橙点能说出"是哪个作业在跑"
            "job_label": job_label or None,   # 可读名（工具名+参数摘要）
            "live": len(_PIPE_ORDER),
        })
    except Exception:
        pass


def _reset_pipes_for_turn() -> int:
    """回合起点清场：清掉上一回合的管道视图，**但跨回合作业的占用不清**。

    作业的定义就是"活过它出生的那个回合"。回合一起就把它的条目抹掉，等于
    前端唯一的真状态来源先瞎了 —— 橙点会在**下一个回合开始时**熄灭
    （真机现象：汇报完那盏灯立马灭了）。返回保住了几个槽位，供日志/诊断用。
    """
    kept = 0
    with _PIPE_LOCK:
        for tc_id in list(_PIPES.keys()):
            v = _PIPES.get(tc_id) or {}
            if v.get("background") and v.get("status") in ("running", "pending"):
                kept += 1
                continue
            _PIPES.pop(tc_id, None)
            if tc_id in _PIPE_ORDER:
                _PIPE_ORDER.remove(tc_id)
    return kept


def _orch_view() -> Dict[str, Any]:
    """编排器的**权威占用视图**（前端刷新/重连/定时对表都读它）。

    两个来源合并：
      · `_PIPES`    —— 谁占着哪个槽（线程名就是槽位身份）；
      · 作业登记册  —— 跨回合作业是否**还活着**（它活过回合边界，所以 `_PIPES`
        里没有它时也必须出现在这里）。

    合成规则一句话：**每一个还活着的作业，都必须在这个视图里占一格。**
    反向也成立：作业已结算 -> 不再算占用（否则橙点永远灭不掉）。

    纯读操作，任何异常都不许炸 —— 它是刷新路径上的常驻调用。
    """
    try:
        orch = getattr(_agent, "_ORCHESTRATOR", None)
        reg = getattr(orch, "jobs", None)
        jobs = list(reg.list_jobs(include_finished=True)) if reg is not None else []
    except Exception:
        jobs = []
    by_tc: Dict[str, Any] = {}
    for j in jobs:
        tc_id = getattr(j, "tool_call_id", "") or ""
        if tc_id:
            by_tc[tc_id] = j
    out: List[Dict[str, Any]] = []
    seen = set()
    with _PIPE_LOCK:
        raw = [(k, dict(v)) for k, v in _PIPES.items()]
    for tc_id, v in raw:
        seen.add(tc_id)
        job = by_tc.get(tc_id) if v.get("background") else None
        if job is not None:
            # 作业登记册是权威：它还活着 -> 把它说成"占用中"，并补上身份与时长
            if not job.alive:
                continue                      # 已结算：不再算占用（前端据此把那格放回灰）
            v["job_id"] = job.job_id
            v["job_label"] = job.label
            v["elapsed"] = round(job.elapsed, 1)
        v["tc_id"] = tc_id
        out.append(v)
    for tc_id, j in by_tc.items():
        if tc_id in seen or not j.alive:
            continue
        # 活着的作业却没有管道条目（池满排队 / 那一帧丢了）：补一格，**绝不让它消失**
        out.append({"tc_id": tc_id, "tool": j.tool, "status": "running", "layer": None,
                    "background": True, "thread": None, "elapsed": round(j.elapsed, 1),
                    "job_id": j.job_id, "job_label": j.label})
    return {"ok": True, "pipes": out}
'''

A4_OLD = '''                def create_pipeline(self, tool_call):
                    p = orig_cp(self, tool_call)
                    _pipe_emit("pending", getattr(tool_call, "id", "?"), getattr(self, "name", "?"))
                    return p
'''

A4_NEW = '''                def create_pipeline(self, tool_call):
                    p = orig_cp(self, tool_call)
                    _bg0, _jid0, _jlab0 = _job_info(tool_call)
                    _pipe_emit("pending", getattr(tool_call, "id", "?"),
                               getattr(self, "name", "?"), background=_bg0,
                               job_id=_jid0, job_label=_jlab0)
                    return p
'''

A5_OLD = '''                    _bg = bool((getattr(tc, "metadata", None) or {}).get("background"))
                    _pipe_emit("running", tc_id, tool, layer=lay, background=_bg)
                    try:
                        r = orig_run(self)
                        st = getattr(self, "status", None) or ("done" if getattr(r, "success", True) else "failed")
                        # 新版 orchestrator 会标 biz_failed；_pipe_emit 白名单只认 done|failed|cancelled，
                        # 未知状态会被当作活跃槽位挂住不放，所以必须映射（getattr 兜住旧版无此字段）
                        if st == "biz_failed" or getattr(r, "biz_fail", False):
                            st = "failed"
                        _pipe_emit(st, tc_id, tool, elapsed=round(self.elapsed, 3), layer=lay)
                        return r
                    except BaseException as e:
                        _pipe_emit("failed", tc_id, tool, layer=lay)
                        raise
'''

A5_NEW = '''                    # 作业身份以**登记册**为准（超时收编的作业没有 background 入参，
                    # 但它确实是跨回合作业）。终态帧也必须带上：前端靠它把那格橙色收掉。
                    _bg, _jid, _jlab = _job_info(tc)
                    _pipe_emit("running", tc_id, tool, layer=lay, background=_bg,
                               job_id=_jid, job_label=_jlab)
                    try:
                        r = orig_run(self)
                        st = getattr(self, "status", None) or ("done" if getattr(r, "success", True) else "failed")
                        # 新版 orchestrator 会标 biz_failed；_pipe_emit 白名单只认 done|failed|cancelled，
                        # 未知状态会被当作活跃槽位挂住不放，所以必须映射（getattr 兜住旧版无此字段）
                        if st == "biz_failed" or getattr(r, "biz_fail", False):
                            st = "failed"
                        _pipe_emit(st, tc_id, tool, elapsed=round(self.elapsed, 3), layer=lay,
                                   background=_bg, job_id=_jid, job_label=_jlab)
                        return r
                    except BaseException as e:
                        _pipe_emit("failed", tc_id, tool, layer=lay,
                                   background=_bg, job_id=_jid, job_label=_jlab)
                        raise
'''

A6_OLD = '''    with _PIPE_LOCK:
        _PIPES.clear()
        _PIPE_ORDER.clear()
'''

A6_NEW = '''    _kept_pipes = _reset_pipes_for_turn()   # 跨回合作业占的槽位不清（见该函数）
    if _kept_pipes:
        print("[bridge] 本回合起点保留 %d 个跨回合作业占用的槽位" % _kept_pipes,
              file=sys.__stderr__, flush=True)
'''

A7_OLD = '''        if path == "/tools":
            return self._json(200, {"ok": True, "tools": _tool_names(),
                                    "injected": list(_injected)})
'''

A7_NEW = '''        if path == "/tools":
            return self._json(200, {"ok": True, "tools": _tool_names(),
                                    "injected": list(_injected)})
        if path == "/orch":
            # 编排器占用视图：前端刷新/重连/定时对表用。
            # 观测面绝不允许 500（刷新路径上的一次异常就会让橙点全灭）。
            try:
                return self._json(200, _orch_view())
            except Exception as e:
                return self._json(200, {"ok": False, "pipes": [],
                                        "error": "%s: %s" % (e.__class__.__name__, e)})
'''

# ==========================================================================
# B. agent_client.py
# ==========================================================================

B_OLD = '''    def ask_pending(self) -> Dict[str, Any]:
        return self._request("GET", "/ask/pending", timeout=8)
'''

B_NEW = '''    def ask_pending(self) -> Dict[str, Any]:
        return self._request("GET", "/ask/pending", timeout=8)

    def orch(self) -> Dict[str, Any]:
        """编排器占用视图（跨回合作业占着哪些槽位）—— 前端刷新/重连后对表用。"""
        return self._request("GET", "/orch", timeout=5)
'''

# ==========================================================================
# C. agent_proc.py
# ==========================================================================

C_OLD = '''    def health(self) -> Dict[str, Any]:
        try:
            return {"ok": True, **self.client.health()}
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
'''

C_NEW = '''    def orch(self) -> Dict[str, Any]:
        """编排器占用视图 —— 与卡片恢复同一条纪律：**"没问到"不等于"没有作业"**。

        拉不到时必须如实标 authoritative=False。前端只认它是 True 的快照；
        否则一次失败的对表就会把屏幕上正亮着的橙点灭掉 ——
        那正是本次要修的那个 bug 的镜像（"看不见"被当成"不存在"）。
        """
        try:
            c = self.client
        except BridgeError as e:
            return {"ok": True, "pipes": [], "authoritative": False,
                    "error": "bridge 未就绪：%s" % e}
        try:
            res = c.orch()
        except Exception as e:
            return {"ok": True, "pipes": [], "authoritative": False,
                    "error": "拉取失败：%s: %s" % (type(e).__name__, e)}
        if not isinstance(res, dict) or not res.get("ok"):
            return {"ok": True, "pipes": [], "authoritative": False,
                    "error": (res or {}).get("error") or "bridge 未给出权威快照"}
        return {"ok": True, "pipes": res.get("pipes") or [], "authoritative": True}

    def health(self) -> Dict[str, Any]:
        try:
            return {"ok": True, **self.client.health()}
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
'''

# ==========================================================================
# D. api.py
# ==========================================================================

D_OLD = '''# ---------------- SSE：唯一事件出口 ----------------
'''

D_NEW = '''@router.api_route("/orch", methods=["GET", "POST"],
                  summary="编排器占用视图（跨回合作业占了哪些槽位）")
def api_orch() -> Dict[str, Any]:
    # 与卡片恢复同源：这是"刷新后仍然看得见作业在跑"的通道，绝不允许它的失败把页面带崩。
    # authoritative=False 时前端**不得**据此剪枝 —— 没问到 ≠ 没有作业在跑。
    # 同时收 GET/POST：方法写错就整条通道失效，这个坑在 /approval/pending 上踩过。
    try:
        res = _mgr().orch()
    except BridgeError as e:
        return {"ok": True, "pipes": [], "authoritative": False,
                "error": "bridge 未就绪：%s" % e}
    except Exception as e:
        return {"ok": True, "pipes": [], "authoritative": False,
                "error": "拉取失败：%s: %s" % (e.__class__.__name__, e)}
    return {"ok": bool(res.get("ok", True)), "pipes": res.get("pipes") or [],
            "authoritative": bool(res.get("authoritative")),
            "error": res.get("error") or ""}


# ---------------- SSE：唯一事件出口 ----------------
'''

PATCHES = [
    ("agent_webui/backend/bridge.py", [
        ("_job_info + _pipe_emit 签名", A1_OLD, A1_NEW),
        ("占用条目带作业身份", A2_OLD, A2_NEW),
        ("事件带作业身份 + 新函数", A3_OLD, A3_NEW),
        ("create_pipeline pending 带身份", A4_OLD, A4_NEW),
        ("pipe.run 三处 emit 带身份", A5_OLD, A5_NEW),
        ("回合起点保留跨回合作业", A6_OLD, A6_NEW),
        ("/orch 路由", A7_OLD, A7_NEW),
    ]),
    ("agent_webui/backend/agent_client.py", [
        ("BridgeClient.orch", B_OLD, B_NEW),
    ]),
    ("agent_webui/backend/agent_proc.py", [
        ("AgentManager.orch", C_OLD, C_NEW),
    ]),
    ("agent_webui/backend/api.py", [
        ("GET/POST /api/orch", D_OLD, D_NEW),
    ]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    bad = 0
    changed = []
    for rel, items in PATCHES:
        path = root / rel
        if not path.exists():
            print("[FAIL] 找不到 %s" % path)
            bad += 1
            continue
        text = path.read_text(encoding="utf-8")
        out = text
        for label, old, new in items:
            n = out.count(old)
            if n != 1:
                print("[FAIL] %s :: %s 锚点命中 %d 次（要求恰好 1 次）" % (rel, label, n))
                bad += 1
                continue
            out = out.replace(old, new)
            print("[OK]   %s :: %s" % (rel, label))
        if out == text:
            continue
        if rel.endswith(".py"):
            try:
                compile(out, str(path), "exec")
            except SyntaxError as e:
                print("[FAIL] %s 语法不过：%s" % (rel, e))
                bad += 1
                continue
        if args.apply:
            path.write_text(out, encoding="utf-8", newline="\n")
            changed.append(rel)
    print("\n锚点失败 %d 处；%s" % (
        bad, ("已写盘: " + ", ".join(changed)) if args.apply else "未写盘"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
