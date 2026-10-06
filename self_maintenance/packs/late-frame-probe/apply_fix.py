# -*- coding: utf-8 -*-
"""P4 补丁：后台作业的迟到帧不许再改写「回合/忙碌」状态。

三处生产改动（都在 workbench 副本上先跑通，再按字节复制到生产）：

  A. agent_webui/backend/agent_proc.py
     - _forward：只让**当前活动回合**的事件改写回合状态机；每一帧都带上服务端权威
       的 busy/can_send（前端改读它，不再靠本地推断"收到 tool_begin 就置忙"）。
     - _note_dead_run：记住已收口的 run_id（迟到帧的身份判据）。
     - stop_run：bridge 说 no-active-run 时就地校正网关忙碌态。
     - status：自愈核对（忙了 >=5 秒且 bridge 说它 IDLE -> 校正为 IDLE）。
  B. agent_webui/backend/bridge.py
     - RunContext.finished + _execute_turn 收尾置位；
     - _wrap_tool：回合已收口的工具（后台作业）不再发 turn_phase（断源）。
  C. agent_webui/frontend/src/store/appStore.tsx
     - tool_begin 读服务端 busy；tool_end 只在仍忙时改 turn_phase。

用法：
    python apply_fix.py --root <树根> --check     # 只检查锚点命中数，不写盘
    python apply_fix.py --root <树根> --apply
"""
import argparse
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# A. agent_proc.py
# --------------------------------------------------------------------------

A_INIT_OLD = '''        self._last_tools: Dict[str, Any] = {}
        config.WEBUI_LOG_DIR.mkdir(parents=True, exist_ok=True)
'''

A_INIT_NEW = '''        self._last_tools: Dict[str, Any] = {}
        # 已收口回合的 run_id（有界）：用来认出"死回合发来的迟到帧"，见 _forward。
        self._dead_runs: List[str] = []
        # 最近一次把回合状态置为"忙"的时刻：status 的自愈核对拿它避开 done 在途的那几毫秒。
        self._turn_armed_at = 0.0
        config.WEBUI_LOG_DIR.mkdir(parents=True, exist_ok=True)
'''

A_FORWARD_OLD = '''    def _forward(self, evt: Dict[str, Any]) -> None:
        """bridge 事件 -> 前端事件，同时同步回合级状态机。"""
        etype = evt.get("type")
        payload = {k: v for k, v in evt.items() if k not in ("seq", "ts")}
        if etype == "turn_phase":
            phase = payload.get("phase") or TURN_IDLE
            self.sm.set_turn(phase)
            payload["can_send"] = self.sm.snapshot()["can_send"]
            payload["busy"] = self.sm.snapshot()["busy"]
        elif etype in ("done", "error"):
            self.sm.set_turn(TURN_IDLE if etype == "done" else TURN_ERROR)
            self.sm.set_run(None)
            payload["can_send"] = True
            payload["busy"] = False
        elif etype == "tool_begin":
            self.sm.set_turn("TOOL_RUNNING")
        elif etype == "ask_request":
            self.sm.set_turn("ASK_WAIT")
        elif etype in ("approval_request",):
            self.sm.set_turn("AUDIT_WAIT")
        elif etype in ("approval_resolved", "approval_expired"):
            # 门禁结束（批准/拒绝/超时）：交还控制权给主循环
            self.sm.set_turn("THINKING")
        elif etype in ("approval_request", "approval_expired"):
            # 审批门禁挂起：回合仍在跑，但明确停在"等人裁决"
            if etype == "approval_request":
                self.sm.set_turn("AUDIT_WAIT")
        self.hub.publish(etype or Ev.STAGE, payload)
'''

A_FORWARD_NEW = '''    def _is_stale_frame(self, run_id: Any) -> bool:
        """这一帧是不是"已经死掉的回合"发来的迟到帧？

        判据不猜，只用两种确凿情况：
          ① 该 run_id 已被本网关见证收口（done/error）—— 后台作业跨回合收尾时的典型；
          ② 网关正跑着**另一个**回合，run_id 对不上。
        两条都不成立时一律放行 —— 免得网关重启、外部起回合这类"网关不认识这个 run_id"
        的场景被误判，把整条忙碌链路静默掐断（那是比卡忙更糟的故障）。
        """
        if not run_id:
            return False
        if run_id in self._dead_runs:
            return True
        return self.sm.snapshot()["run_id"] not in (None, run_id)

    def _note_dead_run(self, run_id: Any) -> None:
        """记下"这个回合已经收口"（有界，只服务于迟到帧判定）。"""
        if not run_id or run_id in self._dead_runs:
            return
        self._dead_runs.append(run_id)
        del self._dead_runs[:-32]

    def _forward(self, evt: Dict[str, Any]) -> None:
        """bridge 事件 -> 前端事件，同时同步回合级状态机。

        2026-09-29 真机事故：后台作业跑在编排器的 worker 线程里，会在**回合收口之后**
        才收尾；bridge 的 _wrap_tool 那时仍会发一条 `turn_phase: THINKING`（run_id 属于
        那个已经结束的回合）。旧写法无条件 set_turn —— 于是 30 秒后界面自己变回
        「回合进行中」（草稿变中期交互），而 bridge 侧早已没有活动回合：
        点「停止本回合」只能得到 no-active-run，且 /agent/status 也是忙的（看门狗救不了）。
        现在只让**当前活动回合**的事件改写状态机，并且每一帧都带上服务端权威的
        busy/can_send —— 前端改读它，不再靠"收到 tool_begin 就置忙"这种本地推断。
        """
        etype = evt.get("type")
        payload = {k: v for k, v in evt.items() if k not in ("seq", "ts")}
        was_busy = self.sm.snapshot()["busy"]
        stale = self._is_stale_frame(payload.get("run_id"))
        if etype == "turn_phase":
            if not stale:
                self.sm.set_turn(payload.get("phase") or TURN_IDLE)
        elif etype in ("done", "error"):
            if not stale:
                self.sm.set_turn(TURN_IDLE if etype == "done" else TURN_ERROR)
                self.sm.set_run(None)
                self._note_dead_run(payload.get("run_id"))
        elif etype == "tool_begin":
            if not stale:
                self.sm.set_turn("TOOL_RUNNING")
        elif etype == "ask_request":
            if not stale:
                self.sm.set_turn("ASK_WAIT")
        elif etype in ("approval_request",):
            if not stale:
                self.sm.set_turn("AUDIT_WAIT")
        elif etype in ("approval_resolved", "approval_expired"):
            # 门禁结束（批准/拒绝/超时）：交还控制权给主循环
            if not stale:
                self.sm.set_turn("THINKING")
        # 统一收尾：每一帧都带**服务端权威**的 busy/can_send。
        # 前端只认这两个字段，于是"迟到帧骗过本地推断"这条路被彻底堵死。
        snap = self.sm.snapshot()
        # 「置忙时刻」只在"不忙 -> 忙"的**跃迁**上打点。若每帧都刷新它，
        # 一个卡住的忙碌态只要有帧在流（后台作业的 progress 也算），
        # status 的自愈核对就会被无限期推迟 —— 那正是要修的场景。
        if snap["busy"] and not was_busy:
            self._turn_armed_at = time.time()
        payload["can_send"] = snap["can_send"]
        payload["busy"] = snap["busy"]
        self.hub.publish(etype or Ev.STAGE, payload)
'''

A_STOP_OLD = '''    def stop_run(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        res = self.client.stop(run_id)
        if res.get("stopped"):
            self.sm.set_turn(TURN_INTERRUPTED)
            self._emit_turn(TURN_INTERRUPTED, "已请求中断，将在工具/请求边界保存退出")
        return res
'''

A_STOP_NEW = '''    def stop_run(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        res = self.client.stop(run_id)
        if res.get("stopped"):
            self.sm.set_turn(TURN_INTERRUPTED)
            self._emit_turn(TURN_INTERRUPTED, "已请求中断，将在工具/请求边界保存退出")
        elif res.get("reason") == "no-active-run" and self.sm.snapshot()["busy"]:
            # bridge 说"没有活动回合"= 它对回合的认知已经收口，而网关可能仍停在忙碌态
            # （后台作业收尾的迟到帧会把状态机又置忙）。这里**就地校正**，
            # 让前端那句「界面状态已校正」变成真的 —— 否则它刷到的状态仍然是忙。
            self.sm.set_turn(TURN_IDLE)
            self.sm.set_run(None)
            self._emit_turn(TURN_IDLE, "bridge 已无活动回合：网关忙碌态就地校正")
        return res
'''

A_STATUS_OLD = '''    def status(self) -> Dict[str, Any]:
        snap = self.sm.snapshot()
        snap["alive"] = bool(self._proc and self._proc.poll() is None)
'''

A_STATUS_NEW = '''    def _reconcile_turn_with_bridge(self, min_age: float = 5.0) -> bool:
        """自愈：网关自认在忙、bridge 却说自己没有活动回合 -> 就地校正为 IDLE。

        为什么必须有这一层：网关的忙碌态是前端的**唯一权威**（前端看门狗刷的正是本接口），
        所以只要有一条路径把它错误地置忙，界面就会永久卡在「回合进行中」，而
        「停止本回合」只会得到 no-active-run —— 这个死循环 2026-09-29 真机撞过两次。
        只在"忙了至少 min_age 秒"时才核对，避开 done 正在路上时的那几毫秒。
        """
        snap = self.sm.snapshot()
        if not snap["busy"]:
            return False
        if time.time() - self._turn_armed_at < min_age:
            return False
        client = self._client
        if client is None:
            return False
        try:
            h = client.health(timeout=2.5)
        except Exception:
            return False
        if not (isinstance(h, dict) and h.get("ok") and h.get("phase") == "IDLE"):
            return False
        self.sm.set_turn(TURN_IDLE)
        self.sm.set_run(None)
        self._emit_turn(TURN_IDLE, "bridge 已无活动回合：网关忙碌态自愈")
        return True

    def status(self) -> Dict[str, Any]:
        self._reconcile_turn_with_bridge()
        snap = self.sm.snapshot()
        snap["alive"] = bool(self._proc and self._proc.poll() is None)
'''

# --------------------------------------------------------------------------
# B. bridge.py
# --------------------------------------------------------------------------

B_CTX_OLD = '''        self.lock = threading.Lock()
        self.result: Optional[Dict[str, Any]] = None
'''

B_CTX_NEW = '''        self.lock = threading.Lock()
        self.result: Optional[Dict[str, Any]] = None
        # 回合是否已收口（_execute_turn 的 finally 置位）。
        # 后台作业会活过它所属的回合，收尾时**不许**再改回合状态 —— 见 _wrap_tool。
        self.finished = False
'''

B_FINALLY_OLD = '''        _REASONING_SEEN.pop(ctx.session_id, None)   # 本回合的"已发过"记录只对本回合有效
        _set_current(None)
'''

B_FINALLY_NEW = '''        _REASONING_SEEN.pop(ctx.session_id, None)   # 本回合的"已发过"记录只对本回合有效
        ctx.finished = True                         # 回合收口：此后任何收尾都不许再改回合状态
        _set_current(None)
'''

B_WRAP_OLD = '''                if still == 0:
                    _set_turn(ctx, "THINKING")
'''
B_WRAP_NEW = '''                # 回合已经收口的（后台作业在 worker 线程里跑完的那种）**绝不再改回合状态**：
                # 这一条就是"挂完 30 秒作业后界面自己变回回合进行中"的断源点 ——
                # 旧写法在这里把网关状态机从"空闲"又推回了"忙"。
                if still == 0 and not getattr(ctx, "finished", False):
                    _set_turn(ctx, "THINKING")
'''

B_START_OLD = '''            _set_turn(ctx, "TOOL_RUNNING")
'''

B_START_NEW = '''            if not getattr(ctx, "finished", False):
                _set_turn(ctx, "TOOL_RUNNING")
'''

# --------------------------------------------------------------------------
# B2. agent_client.py —— health 支持短超时（status 的自愈核对要用，别把状态接口拖住）
# --------------------------------------------------------------------------

B2_HEALTH_OLD = '''    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health", timeout=8)
'''

B2_HEALTH_NEW = '''    def health(self, timeout: float = 8) -> Dict[str, Any]:
        # timeout 可短化：网关 status 的忙碌态自愈核对用它（默认值不变，其它调用方零改动）。
        return self._request("GET", "/health", timeout=timeout)
'''

# --------------------------------------------------------------------------
# C. appStore.tsx
# --------------------------------------------------------------------------

C_BEGIN_OLD = '''      const agent = { ...base.agent, busy: true, can_send: false, turn_phase: 'TOOL_RUNNING' as const }
      const r = typeof evt.round === 'number' && evt.round >= 0
'''

C_BEGIN_NEW = '''      // busy 以**服务端权威**为准（网关现在每一帧都带 busy/can_send）：后台作业在回合
      // 收口之后才收尾，迟到的 tool_begin 并不代表"此刻有回合在跑"。网关没给 busy 时
      // 才退回本地推断（保守置忙，与老行为一致）。
      const running = evt.busy ?? true
      const agent = running
        ? { ...base.agent, busy: true, can_send: false, turn_phase: 'TOOL_RUNNING' as const }
        : { ...base.agent, busy: false, can_send: evt.can_send ?? base.agent.can_send }
      const r = typeof evt.round === 'number' && evt.round >= 0
'''

C_END_OLD = '''      const agent = { ...base.agent, turn_phase: 'THINKING' as const }
      const out = { ...base, timeline, agent }
'''

C_END_NEW = '''      // 只在"确实还有回合在跑"时把徽标交还思考中：后台作业在回合结束后才收尾，
      // 迟到的 tool_end 不该把空闲态画成"思考中"（那正是"又自动起了一个回合"的错觉来源）。
      const stillBusy = evt.busy ?? base.agent.busy
      const agent = stillBusy ? { ...base.agent, turn_phase: 'THINKING' as const } : base.agent
      const out = { ...base, timeline, agent }
'''

PATCHES = [
    ("agent_webui/backend/agent_proc.py", [
        ("__init__ 记死回合 + 置忙时刻", A_INIT_OLD, A_INIT_NEW),
        ("_forward 迟到帧防护 + 权威 busy", A_FORWARD_OLD, A_FORWARD_NEW),
        ("stop_run 就地校正", A_STOP_OLD, A_STOP_NEW),
        ("status 自愈核对", A_STATUS_OLD, A_STATUS_NEW),
    ]),
    ("agent_webui/backend/bridge.py", [
        ("RunContext.finished", B_CTX_OLD, B_CTX_NEW),
        ("_execute_turn 收尾置位", B_FINALLY_OLD, B_FINALLY_NEW),
        ("_wrap_tool 断源（收尾）", B_WRAP_OLD, B_WRAP_NEW),
        ("_wrap_tool 断源（起始）", B_START_OLD, B_START_NEW),
    ]),
    ("agent_webui/backend/agent_client.py", [
        ("health 支持短超时", B2_HEALTH_OLD, B2_HEALTH_NEW),
    ]),
    ("agent_webui/frontend/src/store/appStore.tsx", [
        ("tool_begin 读服务端 busy", C_BEGIN_OLD, C_BEGIN_NEW),
        ("tool_end 只在仍忙时改 phase", C_END_OLD, C_END_NEW),
    ]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="要打补丁的树根")
    ap.add_argument("--apply", action="store_true", help="写盘（默认只检查锚点）")
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
                compile(out, str(path), "exec")      # 语法不过就整块不写盘
            except SyntaxError as e:
                print("[FAIL] %s 语法检查不过：%s" % (rel, e))
                bad += 1
                continue
        if args.apply:
            path.write_text(out, encoding="utf-8", newline="\n")
            changed.append(rel)
    print("\n锚点失败 %d 处；%s" % (
        bad, ("已写盘: " + ", ".join(changed)) if args.apply else "未写盘（--check）"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
