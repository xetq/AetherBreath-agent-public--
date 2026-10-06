# -*- coding: utf-8 -*-
"""P12 双向补丁（交付挂在用户卡里 + 交付当场可见 + task_list 只当仪表盘）。

**为什么有它**：P12 的快照是在动手**之前**建的（这一点做对了），但我**漏声明**了 4 个
"中途才决定要改"的文件（`agent/agent.py`、`agent_webui/backend/bridge.py`、
`frontend/src/store/appStore.tsx`、`frontend/src/components/ChatView.tsx`），
它们没有前像 —— 所以**只靠那次快照回滚不完整**。这里给出双向补丁，
并用"回退 -> 再应用 -> 与生产**逐字节一致**"证明它是精确逆操作。

用法：
    python revert_or_apply_p12.py --root <树根> --to pre  --apply   # 回退到 P12 之前
    python revert_or_apply_p12.py --root <树根> --to post --apply   # 应用 P12（与生产逐字节一致）

注意：前端回退后要重建 dist（`cd agent_webui/frontend && npm run build`）并硬刷新。
"""
import argparse
import sys
from pathlib import Path

PATCHES = {
    "agent_webui/backend/bridge.py": [
        ("done 事件带交付回执", '''            _set_turn(ctx, "INTERRUPTED" if interrupted else "IDLE")
            # 本回合开头自动交付了哪些后台作业 —— 从 agent 侧取**真实发生**的事件
            # （交付是落盘的消息，不是实时事件；界面收到这个才去以磁盘为准重放一次历史，
            #  否则那条【后台作业交付】要等切会话/刷新才看得见）。取不到就为空，不加噪音。
            try:
                delivered_jobs = list(getattr(_agent, "take_delivered_notices", lambda: [])())
            except Exception:
                delivered_jobs = []
            payload = {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "content": content,
                "iterations": result.get("iterations"),
                "tool_calls": ctx.tool_count,
                "elapsed": round(time.time() - ctx.started_at, 2),
                "interrupted": interrupted,
            }
            if delivered_jobs:
                payload["delivered_jobs"] = delivered_jobs
            BUS.emit("done", payload)
''', '''            _set_turn(ctx, "INTERRUPTED" if interrupted else "IDLE")
            BUS.emit("done", {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "content": content,
                "iterations": result.get("iterations"),
                "tool_calls": ctx.tool_count,
                "elapsed": round(time.time() - ctx.started_at, 2),
                "interrupted": interrupted,
            })
'''),
    ],
    "agent/agent.py": [
        ("交付回执（take_delivered_notices）", '''    for jid in delivered:
        try:
            # 再释放内存 + **从登记册出册**（P12：交付过的不用再留着 —— 内容已在会话历史里）
            orch.jobs.mark_delivered(jid)
        except Exception as e:
            log.warning(f"作业 {jid} 标记交付失败: {type(e).__name__}: {e}")
    try:
        # 记一笔"刚交付了谁"，供 bridge 放进 `done` 事件 —— 界面据此**当场**重放历史，
    # 那条交付才不用等你切会话/刷新才出现（交付本身是落盘消息，实时通道里没有它）。
        _DELIVERED_NOTICES.extend(delivered)
        del _DELIVERED_NOTICES[:-50]                    # 只留最近 50 条，别无限长
    except Exception:
        pass
    log.info("[后台作业交付] %d 个作业的全文已写入会话: %s"
             % (len(delivered), ", ".join(delivered)))
    return len(delivered)


# 最近自动交付过的作业号（agent 侧的事实，bridge 取走后清空）—— 见 take_delivered_notices
_DELIVERED_NOTICES: List[str] = []


def take_delivered_notices() -> List[str]:
    """取走并清空"刚交付过"的作业号（bridge 用来给 `done` 事件带上 `delivered_jobs`）。

    为什么不让 bridge 自己去看会话文件：交付这件事只有 agent 侧**知道确切时刻**
    （它就是在这里 append + 落盘的）。取走即清空 = 同一批交付不会被报两次。
    """
    out = list(_DELIVERED_NOTICES)
    _DELIVERED_NOTICES.clear()
    return out
''', '''    for jid in delivered:
        try:
            orch.jobs.mark_delivered(jid)               # 再释放内存（列表里留"已交付"墓碑）
        except Exception as e:
            log.warning(f"作业 {jid} 标记交付失败: {type(e).__name__}: {e}")
    log.info("[后台作业交付] %d 个作业的全文已写入会话: %s"
             % (len(delivered), ", ".join(delivered)))
    return len(delivered)
'''),
    ],
    "agent_webui/frontend/src/store/appStore.tsx": [
        ("State 字段 jobDeliveredAt", '''  turnOwner: string | null
  turnError: string | null
  /** 本回合 `done` 报来的"刚自动交付了后台作业"的时刻（0 = 没有）。
   *  交付是**落盘的消息**、不是实时事件，所以界面拿到这个信号后要以磁盘为准重放一次历史 ——
   *  否则那条【后台作业交付】要等切会话/刷新才看得见（主人 P12 的诉求就是"当场出现在卡里"）。 */
  jobDeliveredAt: number
''', '''  turnOwner: string | null
  turnError: string | null
'''),
        ("初始值", '''  turnOwner: null,
  turnError: null,
  jobDeliveredAt: 0,
''', '''  turnOwner: null,
  turnError: null,
'''),
        ("done 分支接住回执", '''      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      // 本回合开头自动交付过后台作业 -> 记一个时刻，让视图层去"以磁盘为准"重放一次历史
      // （交付是落盘消息，实时通道里没有它；这条信号是 bridge 从 agent 侧取的真事件）
      const delivered = Array.isArray(evt.delivered_jobs) ? evt.delivered_jobs.length : 0
      const next: State = {
        ...base, agent, messages, turnOuts, clarifies: [], askWindow: null, approvals: [],
        turnOwner: null, pipes: idleAll(base.pipes), liveTurns: [],
        streaming: here ? null : base.streaming,
        progress: here ? [] : base.progress,
        turnError: here ? null : base.turnError,
        jobDeliveredAt: delivered && here ? Date.now() : base.jobDeliveredAt,
      }
''', '''      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      const next: State = {
        ...base, agent, messages, turnOuts, clarifies: [], askWindow: null, approvals: [],
        turnOwner: null, pipes: idleAll(base.pipes), liveTurns: [],
        streaming: here ? null : base.streaming,
        progress: here ? [] : base.progress,
        turnError: here ? null : base.turnError,
      }
'''),
    ],
    "agent_webui/frontend/src/components/ChatView.tsx": [
        ("收到回执后重放历史", '''  }, [state.current, state.sessions, load])

  // 后台作业**自动交付**是"落盘的消息"，实时通道里没有它 —— 回合结束时 bridge 会在 done 里
  // 告诉我们刚交付了谁（jobDeliveredAt），这里就以磁盘为准重放一次历史，让那条交付
  // **当场**出现在用户卡里（与「用户交代」同一个位置、不同样式）。没有交付则什么都不做。
  useEffect(() => {
    if (!state.jobDeliveredAt || !state.current) return
    void load(state.current)
  }, [state.jobDeliveredAt, state.current, load])
''', '''  }, [state.current, state.sessions, load])
'''),
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--to", choices=["pre", "post"], required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    bad, changed = 0, []
    for rel, hunks in PATCHES.items():
        path = root / rel
        if not path.exists():
            print("[FAIL] 找不到 %s" % path)
            bad += 1
            continue
        text = path.read_text(encoding="utf-8")
        out = text
        for label, post, pre in hunks:
            src, dst = (pre, post) if args.to == "post" else (post, pre)
            n = out.count(src)
            if n != 1:
                print("[FAIL] %s :: %s 锚点命中 %d 次（要求 1）" % (rel, label, n))
                bad += 1
                continue
            out = out.replace(src, dst)
            print("[OK]   %s :: %s -> %s" % (rel, label, args.to))
        if out != text:
            if str(path).endswith(".py"):          # 只对 Python 做语法校验（TSX 交给 tsc）
                try:
                    compile(out, str(path), "exec")
                except SyntaxError as e:
                    print("[FAIL] %s 改完语法不过：%s" % (rel, e))
                    bad += 1
                    continue
            if args.apply:
                path.write_text(out, encoding="utf-8", newline="\n")
                changed.append(rel)
    print("\n锚点失败 %d 处；%s" % (bad, ("已写盘: " + ", ".join(changed)) if args.apply else "未写盘"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
