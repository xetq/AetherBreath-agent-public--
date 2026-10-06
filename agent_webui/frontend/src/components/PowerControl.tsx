// AB 子进程电源控制：开机 / 优雅关机 / 强制关闭。
// 语义差异（README 也写了）：优雅=在边界保存退出；强杀=立即终止，最多丢当前半轮。
// 两个关机动作都必须二次确认：误点代价一个是"打断正在跑的回合"，一个是"丢掉当前半轮"。
import { useState } from 'react'
import { api, type PowerResult } from '../api'
import { useApp } from '../store/appStore'
import { PowerBadge } from './StatusBadge'

// 回合级阶段 -> 人话。后端枚举见 backend/events.py（TURN_*）。
const TURN_LABEL: Record<string, string> = {
  IDLE: '空闲',
  THINKING: '思考中',
  TOOL_RUNNING: '执行工具',
  RESPONDING: '生成回复',
  ASK_WAIT: '等你回答提问',
  INTERRUPTED: '已中断',
  ERROR: '出错',
}

export default function PowerControl() {
  const { state, dispatch } = useApp()
  const [busy, setBusy] = useState(false)
  const phase = state.agent.phase

  const run = async (label: string, fn: () => Promise<PowerResult>) => {
    setBusy(true)
    try {
      const r = await fn()
      dispatch({ type: 'set_agent', agent: r })
      if (r.ok === false) dispatch({ type: 'toast', kind: 'err', msg: `${label}失败：${r.error || '未知原因'}` })
      else if (r.reason) dispatch({ type: 'toast', kind: r.killed ? 'warn' : 'ok', msg: String(r.reason) })
      if (r.ok !== false && !r.reason) dispatch({ type: 'toast', kind: 'ok', msg: label })
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `${label}异常：${(e as Error).message}` })
      try { dispatch({ type: 'set_agent', agent: await api.agentStatus() }) } catch { /* 网关也没起来 */ }
    } finally { setBusy(false) }
  }

  // 优雅关机：等当前回合在工具边界保存后退出。有回合在跑时会明显更慢，
  // 且超过 graceful_timeout 会被后端转成强制终止 —— 提示里说清楚，别让人以为卡死。
  const gracefulStop = () => {
    const tp = state.agent.turn_phase || 'IDLE'
    const what = TURN_LABEL[tp] || tp
    const tip = tp === 'IDLE'
      ? '当前空闲，会立即保存会话并退出。'
      : tp === 'ASK_WAIT'
        ? `AB 正在等你回答提问（${what}），关机后这次提问会失效，需重开后再问。`
        : `当前回合正在「${what}」，会等它在工具边界保存后退出（可能要一会儿，超时将自动转为强制终止）。`
    if (window.confirm(`优雅关机：${tip}\n\n确认关机？`)) void run('优雅关机中…', api.agentStop)
  }

  const forceKill = () => {
    if (window.confirm('强制关闭会立即终止 AB 进程。最多丢失当前半轮（此前的工具结果已自动落盘），重开后可续聊。确认？'))
      void run('已强制关闭', api.agentKill)
  }

  return (
    <div className="power">
      <PowerBadge phase={phase} />
      {phase === 'ON' ? (
        <>
          <button className="btn ghost" disabled={busy} onClick={gracefulStop} title="等待当前回合在工具边界保存后退出（需二次确认）">⏻ 优雅关机</button>
          <button className="btn danger" disabled={busy} onClick={forceKill} title="立即终止子进程（卡死时用，需二次确认）">🛑 强制关闭</button>
        </>
      ) : (
        <button className="btn primary" disabled={busy || phase !== 'OFF'} onClick={() => void run('AB 已开机', () => api.agentStart())} title="拉起 AB 子进程">
          {busy || phase === 'STARTING' ? '⏳ 开机中…' : '🟢 开机'}
        </button>
      )}
      {phase === 'STOPPING' && <span className="mini">处理中…</span>}
    </div>
  )
}
