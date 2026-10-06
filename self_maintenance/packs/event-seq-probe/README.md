# event-seq-probe —— 事件序号并发探针

**用途**：并发压测 `EventBus.emit` 的序号分配，看会不会撞号/出现空洞。
（前端 store 有条硬规则：`evt.hub_seq <= lastSeq` 的事件静默丢弃 —— 序号一乱，
`done` 就可能被丢，表现为界面卡在"回合进行中"、点停止却说"没有活动回合"。）

**跑法**（必须用**网关解释器**：要 import bridge，根 venv 没有 fastapi）：

```bash
agent_webui/venv-gateway/Scripts/python.exe self_maintenance/packs/event-seq-probe/probe_seq.py
```

**2026-09-29 实测结论（重要，别重复踩）**：

1. 修复前的写法（`self.seq += 1` 后二次读、无锁）在 16 线程 x 500 的压力下**没有撞号**
   -> "序号竞态导致 done 丢帧"这个假设**未被实验支持**，据此做的加锁改动已回退。
2. 顺带量到一个真代价：给 seq 分配加锁会把 emit 吞吐压到约 1/3（2500/8000 vs 8000/8000），
   即"锁争用 -> 队列积压 -> 慢消费者丢事件"更多。**别用锁保护高频事件路径。**
3. 前端真正消费的序号是 **`sse.py` 的 `hub_seq`**（`evt["hub_seq"] = self._seq`），
   **不是** `bridge.EventBus.seq` —— 排查这条链别找错对象（我第一次就找错了）。

**留下的判断**：busy 卡死的根因更可能在 **SSE 断线续传窗口**（`sse.py` 只按
`hub_seq > since` 补发；`done` 一旦落到窗口外，重连就补不回来）。要现场（WS 事件流）才能定案。
