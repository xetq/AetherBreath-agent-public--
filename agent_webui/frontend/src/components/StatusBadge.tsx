import type { AgentPhase, TurnPhase } from '../types'

const PHASE_META: Record<AgentPhase, { label: string; cls: string }> = {
  OFF: { label: 'OFF', cls: 'off' },
  STARTING: { label: 'STARTING', cls: 'wait' },
  ON: { label: 'ON', cls: 'on' },
  STOPPING: { label: 'STOPPING', cls: 'wait' },
}

const TURN_META: Record<TurnPhase, { label: string; cls: string }> = {
  IDLE: { label: '空闲', cls: 'idle' },
  THINKING: { label: '思考中', cls: 'run' },
  TOOL_RUNNING: { label: '工具执行中', cls: 'run' },
  RESPONDING: { label: '正在回复', cls: 'run' },
  ASK_WAIT: { label: '等待你的回答', cls: 'ask' },
  AUDIT_WAIT: { label: '等待你的授权裁决', cls: 'danger' },
  INTERRUPTED: { label: '中断中', cls: 'warn' },
  ERROR: { label: '出错', cls: 'err' },
}

export function PowerBadge({ phase }: { phase: AgentPhase }) {
  const m = PHASE_META[phase] || PHASE_META.OFF
  return <span className={`badge power ${m.cls}`}><i />{m.label}</span>
}

export function TurnBadge({ phase }: { phase: TurnPhase }) {
  const m = TURN_META[phase] || TURN_META.IDLE
  return <span className={`badge turn ${m.cls}`}>{m.label}</span>
}

export default TurnBadge
