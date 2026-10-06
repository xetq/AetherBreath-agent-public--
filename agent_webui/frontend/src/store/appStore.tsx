// 全局状态：Context + useReducer（够用，不引额外状态库）
// SSE 事件是唯一的状态来源，reducer 必须对乱序/重复帧幂等。
import React, { createContext, Dispatch, useContext, useEffect, useMemo, useReducer } from 'react'
import { loadUsage, saveUsage } from '../lib/persist'
import type { ModeOption } from '../lib/permissionMode'
import type {
  AgentStatus, ApprovalItem, ApprovalRequest, ApprovalScope, AskRequest, HistoryMessage,
  LiveTurn, MsgAttachment, OrchPipe, PipeView, SessionMeta, SidecarTurn, TimelineEntry, ToolCallView,
  UsageNode, UsageSnapshot, WsEvent,
} from '../types'

export interface Toast { id: number; kind: 'info' | 'ok' | 'warn' | 'err'; msg: string }

export interface State {
  agent: AgentStatus
  sessions: SessionMeta[]
  current: string | null
  messages: HistoryMessage[]
  timeline: TimelineEntry[]
  /** 实时回合按「模型轮」攒的块（思考 + 该轮工具 + 该轮中期输出）。回合结束后转成消息追加。 */
  liveTurns: LiveTurn[]
  /** 内存版 sidecar：本会话每个回合的中期输出（刷新后由 history 的 turns 字段接管）。
   *  没有它的话，done 一清 liveTurns，屏幕上的中期输出就没了 —— 而刷新后反而有。 */
  turnOuts: SidecarTurn[]
  /** **进行中**回合的半个快照（磁盘版，bridge 节流落盘 / 网关在 history 里透出）。
   *  为什么要单独存：turns 只装已收尾的回合，而刷新/切会话时主人正在跑的那个回合
   *  恰恰不在里面 —— 旧写法 set_messages 把 liveTurns 清空又不补，于是"已经产出的
   *  中期输出与思考"当场消失，得等下一个回合收尾才回来。
   *  与 turnOuts 同一条口径：实时（liveTurns）与磁盘（turnPartial）都走 buildTurns。 */
  turnPartial: SidecarTurn[]
  progress: { run_id?: string; text: string; at: number; sid?: string | null }[]
  streaming: string | null
  /** 正在显示的这个回合属于哪个会话。
   *  busy/turn_phase 是【进程级】事实，而界面是【会话级】视图 —— 不记归属
   *  就会出现"切到别的会话，上一会话的思考中卡片还挂在下面"的串台。 */
  turnOwner: string | null
  turnError: string | null
  /** 本回合 `done` 报来的"刚自动交付了后台作业"的时刻（0 = 没有）。
   *  交付是**落盘的消息**、不是实时事件，所以界面拿到这个信号后要以磁盘为准重放一次历史 ——
   *  否则那条【后台作业交付】要等切会话/刷新才看得见（主人 P12 的诉求就是"当场出现在卡里"）。 */
  jobDeliveredAt: number
  /** 本会话当前的**权限模式**（四档：readonly/workspace/normal/full，2026-10）。
   *  真源是会话文件 + bridge 进程里的引擎缓存；这里只是界面要显示的那一份，
   *  由 history 加载与 permission_mode 事件两处更新。 */
  permissionMode: string
  /** 四档清单（含每档一句话状态）。状态块的文案取自它 —— 与模型每轮拿到的那行同源。 */
  modeCatalog: ModeOption[]
  /** 提问队列：**同批并行提问会同时挂多张卡**（ask_user 允许一条消息里问多个独立问题）。
   *  单槽时代三张卡会互相覆盖 —— 屏幕上只看得见最后一张，答完即消失，其余的还在
   *  服务端干等（这正是"问三个问题却只能答一个"的成因）。 */
  clarifies: AskRequest[]
  /** 提问窗口的**共享剩余秒数** + 它到达本地的时刻。多卡共享一条 deadline，有人作答
   *  会续窗 —— 倒计时必须统一读它，否则答完一张后其余卡还按各自的老出生时刻倒数，
   *  屏幕上会显示一个比真实值小的数字（主人以为来不及了，其实时间才刚续上）。 */
  askWindow: { left: number; at: number } | null
  /** 审批队列：当前主循环串行审批，同时最多 1 条挂起；用队列是为将来的
   *  执行层第二道闸门预留——那时并发请求若挤在单槽里会互相覆盖。 */
  approvals: ApprovalRequest[]
  sse: 'connecting' | 'open' | 'error' | 'closed'
  /** 编排器管道实时表（tc_id → 状态）；done 后统一转空闲灰 */
  pipes: Record<string, PipeView>
  pipeSid: string | null
  toasts: Toast[]
  lastSeq: number
  /** token 概况：按会话分桶常驻（bridge 从 API 返回值读；只覆盖 WebUI 回合，不含 CLI） */
  usageBySid: Record<string, UsageNode>
  /** 每个会话最近一次上报的模型名（切会话时要跟着换，不能沿用上一个是哪个模型） */
  modelBySid: Record<string, string>
  usage: UsageSnapshot | null
  /** 错误脉冲：每次「不顺利」事件 +1（工具失败 / 强制终止 / 代理报错 / 审批被拒或超时）。
   *  形象动画靠它演 1.3s 的错误插入片段。用计数器而不是布尔量：连发两次错误时
   *  布尔量会被 React 合并成一次渲染，动画只演一遍 —— 计数器每次都变，跑不掉。 */
  errSeq: number
  /** 最近一次错误脉冲的时刻（秒，同 now()）。形象侧据此做 5s 冷却防抖。 */
  errAt: number
}

export const initialAgent: AgentStatus = {
  phase: 'OFF', turn_phase: 'IDLE', pid: null, bridge_url: null, session_id: null,
  run_id: null, last_error: null, health_at: null, can_send: false, busy: false,
}

export const initialState: State = {
  agent: initialAgent,
  sessions: [],
  current: null,
  messages: [],
  timeline: [],
  liveTurns: [],
  turnOuts: [],
  turnPartial: [],
  progress: [],
  streaming: null,
  turnOwner: null,
  turnError: null,
  jobDeliveredAt: 0,
  permissionMode: 'normal',
  modeCatalog: [],
  clarifies: [],
  askWindow: null,
  approvals: [],
  sse: 'connecting',
  pipes: {},
  pipeSid: null,
  toasts: [],
  lastSeq: 0,
  usageBySid: {},
  modelBySid: {},
  usage: null,
  errSeq: 0,
  errAt: 0,
}

export type Action =
  | { type: 'set_agent'; agent: Partial<AgentStatus> }
  | { type: 'set_sessions'; sessions: SessionMeta[] }
  | { type: 'set_current'; sid: string | null }
  | { type: 'set_messages'; messages: HistoryMessage[]; timeline: TimelineEntry[]
      turns?: SidecarTurn[]; partial?: SidecarTurn | null; permissionMode?: string
      modeCatalog?: ModeOption[] }
  | { type: 'set_permission_mode'; mode: string }
  | { type: 'set_mode_catalog'; catalog: ModeOption[] }
  | { type: 'append_user'; text: string; atts?: MsgAttachment[] }
  | { type: 'append_mid_turn'; text: string; itemId: string; atts?: MsgAttachment[] }
  | { type: 'set_sse'; sse: State['sse'] }
  | { type: 'toast'; kind: Toast['kind']; msg: string }
  | { type: 'drop_toast'; id: number }
  | { type: 'resolve_ask'; ask_id: string; remaining?: number }
  | { type: 'resolve_approval'; ask_id: string }
  | { type: 'clear_approvals' }
  | { type: 'prune_gates'; ids: string[]; before: number;
      approvals?: boolean; ask?: boolean }
  | { type: 'reset_timeline' }
  | { type: 'reset_usage'; seq?: number }
  | { type: 'merge_orch'; pipes: OrchPipe[]; authoritative: boolean }
  | { type: 'event'; evt: WsEvent }

const DEFAULT_SCOPES: ApprovalScope[] = [
  { key: 'once', label: '仅批准本次', emoji: '🟢' },
  { key: 'session', label: '本会话允许这些路径', emoji: '🟡' },
  { key: 'persistent', label: '永久允许这些路径', emoji: '⚪' },
]

let toastSeq = 1
const now = () => Date.now() / 1000

function toast(state: State, kind: Toast['kind'], msg: string): State {
  const t: Toast = { id: toastSeq++, kind, msg }
  return { ...state, toasts: [...state.toasts.slice(-4), t] }
}

/** 「不顺利」→ 形象错误脉冲（给控制台里的 ☲ 生灵演 1.3s 错误动画）。
 *  回放帧一律不算：重连/刷新会把旧事件重发一遍，照它演错误等于每次刷新都吓主人一跳
 *  —— 与"卡片恢复不能靠环形回放"是同一条哲学。 */
function pulse(state: State): State {
  return { ...state, errSeq: state.errSeq + 1, errAt: now() }
}

/** 历史消息 -> 时间线条目（与实时事件复用同一渲染器） */
export function timelineFromHistory(messages: HistoryMessage[]): TimelineEntry[] {
  const out: TimelineEntry[] = []
  messages.forEach((m, mi) => {
    ;(m.timeline || []).forEach((t, ti) => {
      out.push({ ...t, key: `h${mi}-${t.call_id || ti}`, status: 'done', source: 'history' })
    })
  })
  return out
}

/** 回合收尾后所有管道转空闲：满足"关闭或运行完成后呈灰色空闲态"的语义 */
/** 保证 liveTurns 至少有 round+1 项（中间缺的补空轮）：事件带轮号，前端的归位就与
 *  到达顺序无关 —— agent 的「中期进度」print 比 reasong 事件早，顺序对齐本来就是错的。 */
function ensureTurn(turns: LiveTurn[], round: number): LiveTurn[] {
  const out = [...turns]
  while (out.length <= round) {
    out.push({ at: Date.now() / 1000, reasoning: '', toolIds: [], outs: [] })
  }
  return out
}

function attachTool(turns: LiveTurn[], callId: string, round: number): LiveTurn[] {
  const r = Math.max(0, round)
  const out = ensureTurn(turns, r)
  out[r] = { ...out[r], toolIds: [...out[r].toolIds, callId] }
  return out
}

/** 把实时攒的轮转成一条 sidecar 回合（内存版）：与 bridge 落盘的结构**逐字段一致**，
 *  这样 done 之后不刷新也看得到中期输出，刷新后由磁盘版接管、内容不会跳变。 */
function sidecarOf(turns: LiveTurn[], seq: number, ended: string): SidecarTurn {
  const outs: { round: number; text: string }[] = []
  turns.forEach((t, i) => t.outs.forEach((o) => outs.push({ round: i, text: o })))
  return { user_seq: seq, run_id: '', ended, outs }
}

/** 回合结束：把实时攒的「每轮思考 + 该轮工具」转成消息追加。
 *  形态与刷新后 sessions.py 还原的**完全一致**（每轮一条 assistant 消息）——
 *  这样不刷新也能看到思考块/工具块，刷新之后也不会跳变。 */
function finishTurns(turns: LiveTurn[], timeline: TimelineEntry[], content: string): HistoryMessage[] {
  if (turns.length === 0) return content ? [{ role: 'assistant', content }] : []
  const byId = new Map(timeline.map((t) => [t.call_id, t]))
  return turns.map((tn, i) => {
    const calls: ToolCallView[] = tn.toolIds
      .map((id) => byId.get(id))
      .filter((t): t is TimelineEntry => !!t)
      .map((t) => ({
        id: t.call_id, name: t.tool, args: t.args, result: t.result,
        failed: t.status === 'done' && !t.ok, pending: t.status === 'running',
      }))
    const m: HistoryMessage = { role: 'assistant', content: i === turns.length - 1 ? content : '' }
    if (tn.reasoning) m.reasoning = tn.reasoning
    if (calls.length) m.tool_calls = calls
    return m
  })
}

function idleAll(pipes: Record<string, PipeView>): Record<string, PipeView> {
  const out: Record<string, PipeView> = {}
  for (const [k, v] of Object.entries(pipes)) {
    // ⚠️ 落回空闲/失败/取消的条目**顺手摘掉 background 标记**（2026-10-02 真机修）：
    //    那个标记的语义是「这一格**此刻**被跨回合作业占着」。条目都结束了它就不成立，
    //    留着它，任何"看历史条目决定颜色"的写法都会把槽位永久染橙
    //    （主人看到的现象：这一格跑过后台作业后，再跑并行任务亮起来还是橙色）。
    if (v.status === 'failed' || v.status === 'cancelled') {
      out[k] = { ...v, background: false }; continue
    }
    // 跨回合作业占用的槽位**不随回合收尾熄灭**：作业的定义就是"活过它出生的那个回合"。
    // 少了这一条，挂完后台作业一汇报，那盏橙灯就当场变灰（真机 2026-09-29 的现象）——
    // 而作业其实还在跑，界面等于在撒谎。
    if (v.background && (v.status === 'running' || v.status === 'pending')) { out[k] = v; continue }
    out[k] = { ...v, status: 'idle', background: false }
  }
  return out
}

function onEvent(state: State, evt: WsEvent): State {
  if (typeof evt.hub_seq === 'number' && evt.hub_seq <= state.lastSeq) return state
  const lastSeq = typeof evt.hub_seq === 'number' ? evt.hub_seq : state.lastSeq
  // 带 session_id 的事件会更新归属；不带的（agent_phase 等进程级事件）沿用上一次
  const owner = typeof evt.session_id === 'string' && evt.session_id ? evt.session_id : state.turnOwner
  const base: State = { ...state, lastSeq, turnOwner: owner }

  switch (evt.type) {
    case 'agent_phase': {
      const agent: AgentStatus = {
        ...base.agent,
        phase: (evt.phase as AgentStatus['phase']) || base.agent.phase,
        turn_phase: (evt.turn_phase as AgentStatus['turn_phase']) || base.agent.turn_phase,
        pid: evt.pid !== undefined ? evt.pid : base.agent.pid,
        bridge_url: evt.bridge_url !== undefined ? evt.bridge_url : base.agent.bridge_url,
        run_id: evt.run_id !== undefined ? evt.run_id : base.agent.run_id,
        can_send: evt.can_send ?? base.agent.can_send,
        busy: evt.busy ?? base.agent.busy,
      }
      let next: State = { ...base, agent }
      // AB 关机/停机 = **编排器整个没了**（作业登记册是内存态，进程一没就全没）。
      // 此时任何"还在跑的橙点"都是残留：关机链路上已经强杀并等落地（P13），
      // 但浏览器这边没人告诉它 —— 于是点阵会一直亮着，看着像"关机了活儿还在跑"。
      // 这里以**进程级事实**为准把它们放回灰色（比"等 /api/orch 请求失败"精确得多：
      // 后者连一次网络抖动都会误伤）。
      if (agent.phase === 'OFF' || agent.phase === 'STOPPING') {
        next = { ...next, pipes: idleAll(next.pipes) }
      }
      if (evt.reason && evt.reason !== 'snapshot' && evt.error) {
        next = toast(next, 'err', `进程状态：${evt.error}`.slice(0, 240))
      }
      return next
    }
    case 'turn_phase': {
      const agent = {
        ...base.agent,
        turn_phase: evt.phase as AgentStatus['turn_phase'],
        can_send: evt.can_send ?? base.agent.can_send,
        busy: evt.busy ?? base.agent.busy,
      }
      return { ...base, agent }
    }
    case 'stage': {
      const agent = { ...base.agent, turn_phase: 'THINKING' as const }
      return { ...base, agent }
    }
    case 'tool_begin': {
      const e: TimelineEntry = {
        key: `${evt.run_id || 'r'}-${evt.call_id || now()}`,
        tool: evt.tool || '?', args: evt.args || '', result: null, ok: true,
        error: null, elapsed: null, call_id: evt.call_id || '', thread: evt.thread,
        run_id: evt.run_id || undefined, session_id: evt.session_id ?? null,
        source: 'live', at: evt.ts as number, status: 'running',
      }
      // busy 以**服务端权威**为准（网关现在每一帧都带 busy/can_send）：后台作业在回合
      // 收口之后才收尾，迟到的 tool_begin 并不代表"此刻有回合在跑"。网关没给 busy 时
      // 才退回本地推断（保守置忙，与老行为一致）。
      const running = evt.busy ?? true
      const agent = running
        ? { ...base.agent, busy: true, can_send: false, turn_phase: 'TOOL_RUNNING' as const }
        : { ...base.agent, busy: false, can_send: evt.can_send ?? base.agent.can_send }
      const r = typeof evt.round === 'number' && evt.round >= 0
        ? evt.round : Math.max(0, base.liveTurns.length - 1)
      return { ...base, agent, timeline: [...base.timeline, e],
               liveTurns: attachTool(base.liveTurns, e.call_id, r) }
    }
    case 'tool_end': {
      const idx = base.timeline.findIndex((t) => t.call_id === evt.call_id && t.status === 'running')
      const merged: TimelineEntry = {
        key: idx >= 0 ? base.timeline[idx].key : `${evt.run_id || 'r'}-${evt.call_id || now()}`,
        tool: evt.tool || '?', args: base.timeline[idx]?.args || evt.args || '',
        result: evt.result ?? null, ok: evt.ok !== false, error: evt.error || null,
        elapsed: evt.elapsed ?? null, call_id: evt.call_id || '', thread: evt.thread,
        run_id: evt.run_id || undefined, session_id: evt.session_id ?? base.timeline[idx]?.session_id ?? null,
        source: 'live', at: base.timeline[idx]?.at ?? (evt.ts as number), status: 'done',
      }
      const timeline = idx >= 0
        ? base.timeline.map((t, i) => (i === idx ? merged : t))
        : [...base.timeline, merged]
      // 只在"确实还有回合在跑"时把徽标交还思考中：后台作业在回合结束后才收尾，
      // 迟到的 tool_end 不该把空闲态画成"思考中"（那正是"又自动起了一个回合"的错觉来源）。
      const stillBusy = evt.busy ?? base.agent.busy
      const agent = stillBusy ? { ...base.agent, turn_phase: 'THINKING' as const } : base.agent
      const out = { ...base, timeline, agent }
      // 工具失败 = 不顺利（主人 2026-09-23 裁决：工具超时也算）→ 形象错误脉冲。
      // 回放帧跳过：刷新/重连会把旧事件重发一遍，照它演错误等于每次刷新都吓主人一跳。
      return (evt.ok === false && !evt.replayed) ? pulse(out) : out
    }
    case 'reasoning': {
      // 模型这一轮的思考原文（bridge 读会话文件后发出），按它给的轮号归位
      const text = (evt.text || '').trim()
      if (!text) return base
      const r = typeof evt.round === 'number' && evt.round >= 0 ? evt.round : 0
      const turns = ensureTurn(base.liveTurns, r)
      turns[r] = { ...turns[r], reasoning: text }
      return { ...base, liveTurns: turns }
    }
    case 'delta': {
      // 流式增量（2026-10-02）：模型**正在生成**的思考/正文碎片。攒进本轮草稿，
      // 让「中期过程」在生成期间就有东西可看 —— 这是"边想边看"的全部实现。
      // 正式事件（reasoning / progress / done）到达后会覆盖草稿，所以草稿只当预览。
      const text = evt.text || ''
      if (!text) return base
      const r = typeof evt.round === 'number' && evt.round >= 0 ? evt.round : 0
      const turns = ensureTurn(base.liveTurns, r)
      const cur = turns[r]
      // 上限护栏：万一服务端发疯猛灌，也绝不让内存无限涨（超出就丢弃后续碎片，
      // 反正整轮结束时的正式事件会带来完整文本）。
      const CAP = 20000
      if (evt.kind === 'reasoning') {
        if ((cur.thinkDraft || '').length >= CAP) return base
        turns[r] = { ...cur, thinkDraft: (cur.thinkDraft || '') + text }
      } else {
        if ((cur.textDraft || '').length >= CAP) return base
        turns[r] = { ...cur, textDraft: (cur.textDraft || '') + text }
      }
      return { ...base, liveTurns: turns }
    }
    case 'progress': {
      if (!evt.text) return base
      const p = { run_id: evt.run_id || undefined, text: evt.text, at: now(), sid: evt.session_id ?? null }
      // 同时挂进对应轮（中期过程卡要按轮显示它）。轮号由 bridge 给 —— 这行 print
      // 发生在工具执行之前，靠到达顺序归位必然错。
      const r = typeof evt.round === 'number' && evt.round >= 0 ? evt.round : 0
      const turns = ensureTurn(base.liveTurns, r)
      turns[r] = { ...turns[r], outs: [...turns[r].outs, evt.text] }
      return { ...base, progress: [...base.progress.slice(-59), p], liveTurns: turns }
    }
    case 'text':
      return { ...base, streaming: evt.text || '' }
    case 'approval_request':
    case 'approval_batch': {
      // 回放帧不建卡：卡片"此刻在不在等人"的真相只在 REST pending 通道里
      // （App.tsx 的 syncPending）。靠环形回放复活卡片，会把早已裁决/超时的
      // 卡重新显示出来，比看不见更糟 —— 主人会对着一个不存在的 ask_id 点允许。
      if (evt.replayed) return base
      // 单条与合并卡归一成一个容器：单条 = 只有 1 项的批，渲染同一套代码。
      const isBatch = evt.type === 'approval_batch'
      const raw = (isBatch ? evt.items : [evt]) as unknown as Partial<ApprovalItem>[]
      const id = String((isBatch ? evt.batch_id : evt.ask_id) || '')
      if (!id || !raw.length) return base
      const a: ApprovalRequest = {
        ask_id: id, batch: isBatch || raw.length > 1,
        items: raw.map((it) => ({
          ask_id: String(it.ask_id || id), kind: it.kind || '',
          risk: Number(it.risk ?? 2), title: it.title || '', intent: it.intent || '',
          reason: it.reason || '', paths: it.paths || [],
          critical: !!it.critical, source_code: it.source_code || '',
          // 恢复卡要在卡上说明白：主人可能以为这是刚弹的，其实已经等掉了一半窗口
          notes: evt.restored
            ? ['🔄 本卡在你刷新／切走之后由服务端找回（后端仍在等你裁决）', ...(it.notes || [])]
            : (it.notes || []),
        })),
        scopes: (evt.scopes as unknown as ApprovalScope[]) || DEFAULT_SCOPES,
        total: Number(evt.total ?? raw.length), timeout: evt.timeout,
        session_id: evt.session_id,
        // 恢复的卡不许把倒计时重置成满格，那是在骗主人：服务端给了剩余秒数，
        // 就折算回一个「过去的出生时刻」，让秒数继续往下走。
        born: Date.now() - Math.max(0,
          (Number(evt.timeout ?? 300) - Number(evt.remaining ?? evt.timeout ?? 300)) * 1000),
        restored: !!evt.restored,
        user_request: evt.user_request || '',
        // 这两个字段必须在这里显式搬一遍：本 reducer 是逐字段手工白名单构造，
        // types.ts 里加了可选字段而这里漏抄，值就会静默变 undefined ——
        // tsc 不报、构建不报，只有真机看一眼界面才发现输入框根本没渲染。
        accepts_note: !!evt.accepts_note,
        note_hint: String(evt.note_hint || ''),
      }
      // 同 id 重复到达（SSE 重连补发）时替换而非堆叠
      const rest = base.approvals.filter((x) => x.ask_id !== a.ask_id)
      const agent = { ...base.agent, turn_phase: 'AUDIT_WAIT' as const, busy: true, can_send: false }
      return { ...base, approvals: [...rest, a], agent }
    }
    case 'approval_resolved':
    case 'approval_batch_resolved':
    case 'approval_expired': {
      // 门禁结束（批准 / 拒绝 / 超时 / 回合终止）：摘卡并把徽标交还主循环。
      // 只处理仍在队列里的 id —— 晚到的帧不许改写已经 done 的回合状态。
      const gone = String(evt.ask_id || evt.batch_id || '')
      const hit = base.approvals.find((x) => x.ask_id === gone)
      const next: State = { ...base, approvals: base.approvals.filter((x) => x.ask_id !== gone) }
      if (!hit) return next
      next.agent = { ...base.agent, turn_phase: 'THINKING' as const }
      // 主人裁决（2026-09-23）：审批被拒、审批超时都算「不顺利」→ 形象错误脉冲。
      // choice：A 本次 / B 本会话路径 / D 永久 / E 拒 / F 拒并中止（见 agent/approval.py）。
      // 合并卡没有 choice，用「approved 是空数组」表示整批一项都没批 = 拒绝。
      const denied = evt.type === 'approval_expired'
        || evt.choice === 'E' || evt.choice === 'F'
        || (evt.type === 'approval_batch_resolved'
            && Array.isArray(evt.approved) && evt.approved.length === 0)
      // 能走到这里说明卡还在队列里（上面 hit 命中）→ 回放帧天然进不来，不必再判 replayed
      const marked = denied ? pulse(next) : next
      if (evt.type === 'approval_expired') {
        return toast(marked, 'warn', '审批超时：该操作已按「拒绝」处理，未被执行')
      }
      return marked
    }
    case 'permission_mode': {
      // 权限模式切换（bridge 广播）：更新下拉与那块**就地更新**的状态块。
      // 切换通知不再进历史（主人 2026-10 定：只报"此刻是什么状态"，不做流水），
      // 所以这里**不需要**重放历史 —— 状态块是实时态，SSE 一到就变。
      const m = String(evt.mode || '')
      if (!m) return state
      return { ...state, permissionMode: m }
    }
    case 'mid_turn': {
      // 中期交互（用户交代）：三个阶段都走事件 —— accepted 落座、injected 送到、
      // dropped 作废。回放帧一律不动消息列表：它与卡片恢复同一条哲学 ——
      // 重连/刷新时的环形回放不是"此刻正在发生什么"的证据，照它写会把早已
      // 注入落盘的旧交代再显示一遍（历史里本来就有一条）。
      if (evt.replayed) return base
      const sid = typeof evt.session_id === 'string' ? evt.session_id : null
      if (!sid) return base
      const here = sid === state.current
      if (evt.mid === 'accepted') {
        if (!here) return base
        const id = evt.item_id || ''
        if (id && base.messages.some((m) => m.mid_id === id)) return base   // 本地已落座 → 不堆叠
        return { ...base, messages: [...base.messages, {
          role: 'user' as const, content: evt.text || '',
          mid: 'pending' as const, mid_id: id,
        }] }
      }
      const ids = new Set((evt.ids || []).map(String))
      if (!ids.size) return base
      const status: HistoryMessage['mid'] = evt.mid === 'dropped' ? 'dropped' : 'delivered'
      let hit = false
      const messages = base.messages.map((m) => {
        if (m.mid_id && ids.has(m.mid_id) && m.mid !== status) {
          hit = true
          return { ...m, mid: status }
        }
        return m
      })
      if (!hit) return base
      const next: State = { ...base, messages }
      // 没送到就必须当面说清：那条交代不会生效，别让人以为 AB 看到了
      if (evt.mid === 'dropped' && here) {
        const first = (evt.texts && evt.texts[0]) || ''
        const brief = first.length > 24 ? first.slice(0, 24) + '…' : first
        return toast(next, 'warn',
          `「用户交代」未送达（回合已结束）：${brief || (evt.count || 1) + ' 条'} —— 需要就重发一次`)
      }
      return next
    }
    case 'ask_request': {
      if (evt.replayed) return base          // 同审批：回放不建卡，真相在 REST pending
      const c: AskRequest = {
        ask_id: evt.ask_id || '', question: evt.question || '',
        options: evt.options || [], kind: (evt.mode as AskRequest['kind']) || 'choice',
        run_id: evt.run_id, session_id: evt.session_id, timeout: evt.timeout,
        remaining: typeof evt.remaining === 'number' ? evt.remaining : undefined,
        restored: !!evt.restored,
        // 批次信息（新增）：同批一共几问、此刻还剩几个待答 —— 卡片上要如实显示
        batch_size: typeof evt.batch_size === 'number' ? evt.batch_size : undefined,
        batch_live: typeof evt.batch_live === 'number' ? evt.batch_live : undefined,
        born: Date.now() - Math.max(0, ((Number(evt.timeout ?? 120))
          - Number(evt.remaining ?? evt.timeout ?? 120)) * 1000),
      }
      const agent = { ...base.agent, turn_phase: 'ASK_WAIT' as const, busy: true, can_send: false }
      // 同 ask_id 重复到达（SSE 重连补发）→ 替换；不同 ask_id = 同批的另一个问题 → 追加。
      // 这里**绝不能**覆盖整个槽位：一个回合里可以同时挂着好几张卡。
      const rest = base.clarifies.filter((x) => x.ask_id !== c.ask_id)
      return { ...base, clarifies: [...rest, c],
               askWindow: { left: Number(evt.remaining ?? evt.timeout ?? 120), at: Date.now() },
               agent }
    }
    case 'ask_resolved': {
      // 挂着的提问整批作废（回合被终止等）→ 卡立即从屏幕上摘掉，不留僵尸
      if (evt.replayed) return base
      if (!base.clarifies.length) return base
      return { ...base, clarifies: [], askWindow: null }
    }
    case 'done': {
      // 归属判定必须覆盖"写数据"的路径：否则 A 会话的回复会被 append 进你正看着的
      // B 会话消息列表（切回去才发现串了台）。
      const sid = typeof evt.session_id === 'string' ? evt.session_id : null
      const here = sid === null || sid === state.current
      const content = evt.content || base.streaming || ''
      const messages = here
        ? [...base.messages, ...finishTurns(base.liveTurns, base.timeline, content)]
        : base.messages
      // 中途输出的内存版：user_seq 与会话里"第几次真实用户输入"对齐（与 bridge 同一算法）
      const userSeq = Math.max(0, base.messages.filter(
        (m) => m.role === 'user' && !m.mid).length - 1)
      const endedKind = evt.interrupted ? 'interrupted' : 'done'
      const turnOuts = here && base.liveTurns.length
        ? [...base.turnOuts, sidecarOf(base.liveTurns, userSeq, endedKind)]
        : base.turnOuts
      // 回放帧只补时间线 —— 它不是"此刻正在发生什么"的证据。少了这一条，初次打开
      // （since=0，整环回放）时上一回合的 done 会把正挂着的审批卡一起清掉，
      // 表现是"刷新后卡片消失，且要刷几次才复现"（取决于回放与恢复请求谁先到）。
      if (evt.replayed) return { ...base, messages }
      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      // 本回合开头自动交付过后台作业 -> 记一个时刻，让视图层去"以磁盘为准"重放一次历史
      // （交付是落盘消息，实时通道里没有它；这条信号是 bridge 从 agent 侧取的真事件）
      const delivered = Array.isArray(evt.delivered_jobs) ? evt.delivered_jobs.length : 0
      const next: State = {
        ...base, agent, messages, turnOuts, clarifies: [], askWindow: null, approvals: [],
        turnOwner: null, pipes: idleAll(base.pipes), liveTurns: [],
        turnPartial: [],
        streaming: here ? null : base.streaming,
        progress: here ? [] : base.progress,
        turnError: here ? null : base.turnError,
        jobDeliveredAt: delivered && here ? Date.now() : base.jobDeliveredAt,
      }
      // [Fix] 刷新会全量回放 hub 历史：其它/旧会话的 interrupted done 若无条件弹 toast，
      // 用户会误以为当前对话被刷新中断。仅当事件归属当前正在看的会话才提示。
      // 强制终止（主人原话里的「强制终止」）= 不顺利 → 形象错误脉冲
      const stopped = evt.interrupted ? pulse(next) : next
      if (evt.interrupted && here) return toast(stopped, 'warn', '回合已中断（进度已保存到会话）')
      return stopped
    }
    case 'error': {
      const sid2 = typeof evt.session_id === 'string' ? evt.session_id : null
      const here2 = sid2 === null || sid2 === state.current
      const msg = evt.message || evt.error || '未知错误'
      if (evt.replayed) return base          // 回放不写"此刻状态"，也不弹告警
      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      const nextErr: State = { ...base, agent, turnOwner: null, clarifies: [], askWindow: null, liveTurns: [],
        turnPartial: [],
        approvals: [], pipes: idleAll(base.pipes),
                               turnError: here2 ? msg : base.turnError,
                               streaming: here2 ? null : base.streaming }
      // 同理：回放来的非当前会话 error 事件只校正状态，不弹 toast
      // 代理报错 = 不顺利 → 形象错误脉冲（回放帧在上面已提前 return，走不到这里）
      const pulsed = pulse(nextErr)
      return here2 ? toast(pulsed, 'err', msg) : pulsed
    }
    case 'pipeline': {
      if (!evt.tc_id) return base
      const prev = base.pipes[evt.tc_id]
      const pipes: Record<string, PipeView> = { ...base.pipes, [evt.tc_id]: {
        tool: evt.tool || prev?.tool || '?',
        status: evt.status || 'idle',
        layer: evt.layer ?? prev?.layer ?? null,
        at: now(),
        thread: evt.thread ?? prev?.thread ?? null,
        elapsed: evt.elapsed ?? prev?.elapsed ?? null,
        // ⚠️ 这里是**显式挑字段**：新增字段忘了加进来，它就在这一层被丢掉。
        // 2026-09-29 真机事故：bridge 发了 background、OrchStrip 也读它、types.ts 也声明了，
        // 唯独漏了这一行 -> 橙色槽位永远不亮（三层全绿，就是看不见）。
        background: evt.background ?? prev?.background ?? false,
        // 作业身份同理：少了它，橙点只能画一个说不出"是谁在跑"的色块。
        job_id: evt.job_id ?? prev?.job_id ?? null,
        job_label: evt.job_label ?? prev?.job_label ?? null,
      } }
      return { ...base, pipes, pipeSid: evt.session_id ?? base.pipeSid }
    }
    case 'usage': {
      const esid = typeof evt.session_id === 'string' && evt.session_id ? evt.session_id : null
      const sess = evt.session as UsageNode | undefined
      if (!esid || !sess) return base            // 拿不到归属就不记，避免串会话
      // 按会话分桶常驻：切走再切回沿用同一桶，不重置（用户要求"来回看本会话消耗"）。
      const bySid = { ...base.usageBySid, [esid]: sess }
      const mdl = evt.model ? String(evt.model) : null
      const models = mdl ? { ...base.modelBySid, [esid]: mdl } : base.modelBySid
      if (esid !== state.current) return { ...base, usageBySid: bySid, modelBySid: models }
      const snap: UsageSnapshot = {
        scope: 'session',
        node: sess,
        session: sess,
        global: (evt.global as UsageNode) || sess,
        model: mdl ?? base.modelBySid[esid] ?? null,
        at: now(),
      }
      return { ...base, usageBySid: bySid, modelBySid: models, usage: snap }
    }
    case 'heartbeat':
    default:
      return base
  }
}

// 导出 reducer 只为**离线行为探针**（`self_maintenance/packs/orch-orange-probe/`）：
// 源码级断言看不出"豁免条件写反了"这类错，只有真跑一遍才认账。
// 它不进任何生产调用路径 —— 生产仍然只经 AppProvider。
export function reducer(state: State, a: Action): State {
  switch (a.type) {
    case 'set_agent': {
      const agent = { ...state.agent, ...a.agent }
      // 刷新后延续思考中：status 里 busy 且带 session_id，说明那会话有回合在跑
      const turnOwner = agent.busy ? (a.agent.session_id ?? state.turnOwner) : null
      // 计量账本校准：bridge 把上次退出的每会话用量落了盘，重启后随 hello 带回来。
      // 本地桶优先（更实时），账本只补本地没有的会话 —— 于是「没发过消息也有数」。
      const led = (a.agent.hello as { usage_ledger?: { by_sid?: Record<string, UsageNode> } } | undefined)
        ?.usage_ledger?.by_sid
      const bySid = led ? { ...led, ...state.usageBySid } : state.usageBySid
      const usage = state.usage ?? (state.current && bySid[state.current] ? {
        scope: 'session' as const, node: bySid[state.current],
        session: bySid[state.current], global: bySid[state.current],
        model: state.modelBySid[state.current] ?? null, at: now(),
      } : state.usage)
      return { ...state, agent, turnOwner, usageBySid: bySid, usage }
    }
    case 'set_sessions': return { ...state, sessions: a.sessions }
    case 'set_current': {
      // 切会话只换显示口径，不清 usageBySid：切回来还能看到该会话的累计
      const usage = a.sid && state.usageBySid[a.sid] ? {
        scope: 'session' as const, node: state.usageBySid[a.sid],
        session: state.usageBySid[a.sid], global: state.usageBySid[a.sid],
        model: state.modelBySid[a.sid] ?? null, at: now(),
      } : null
      // 待裁决卡片一律保留，不按会话过滤 —— 这一条是四个现象里"切会话/新建后
      // 卡片消失"的正面修法。审批与提问是【进程级】闸门（主循环串行，同一时刻
      // 最多一批），后端 Event 还在死等；卡片的归属只是它顺带带的标签。
      // 上一版写成"只保留属于目标会话的"：切走时把卡删了，切回来自然什么都没有，
      // 而后端仍在等 —— 于是整批工具等到超时、循环当场卡死。
      // 归属不同的卡由卡片自己显示"属于别的会话"的提示，不靠删卡来"理顺"视图。
      return { ...state, current: a.sid, streaming: null, turnError: null, usage, liveTurns: [],
               turnPartial: [] }
    }
    case 'set_messages': return { ...state, messages: a.messages, timeline: a.timeline,
                                  liveTurns: [], turnOuts: a.turns || [],
                                  turnPartial: a.partial ? [a.partial] : [],
                                  permissionMode: a.permissionMode || state.permissionMode,
                                  modeCatalog: a.modeCatalog?.length ? a.modeCatalog
                                                                     : state.modeCatalog }
    case 'set_permission_mode': return { ...state, permissionMode: a.mode }
    case 'set_mode_catalog': return { ...state, modeCatalog: a.catalog }
    case 'append_user':
      return {
        ...state,
        messages: [...state.messages, { role: 'user', content: a.text,
          attachments: a.atts && a.atts.length ? a.atts : undefined }],
        streaming: null, turnError: null, progress: [],
      }
    case 'append_mid_turn': {
      // 中期交互「用户交代」：投递回执（HTTP 200）一到就本地落座，不等 SSE。
      // 与 SSE 的 accepted 事件用 item_id 判重 —— 两条通道谁先到都不会堆两条。
      // 注意这里**不清** streaming/progress：交代不打断正在跑的回合视图。
      const id = a.itemId
      if (id && state.messages.some((m) => m.mid_id === id)) return state
      return { ...state, messages: [...state.messages, {
        role: 'user', content: a.text, mid: 'pending', mid_id: id,
        attachments: a.atts && a.atts.length ? a.atts : undefined }] }
    }
    case 'set_sse': return { ...state, sse: a.sse }
    case 'toast': return toast(state, a.kind, a.msg)
    case 'drop_toast': return { ...state, toasts: state.toasts.filter((t) => t.id !== a.id) }
    case 'resolve_ask': {
      // 按 ask_id 摘掉这一张：同批的其它问题还在等，绝不清空整个队列
      const rest = state.clarifies.filter((x) => x.ask_id !== a.ask_id)
      // 有人作答 → 后端把本批共享窗口续期了。用回执里的真值续算，剩下的卡不会被误判超时。
      const win = typeof a.remaining === 'number' && a.remaining > 0
        ? { left: a.remaining, at: Date.now() }
        : state.askWindow
      return { ...state, clarifies: rest, askWindow: rest.length ? win : null }
    }
    case 'resolve_approval':
      return { ...state, approvals: state.approvals.filter((x) => x.ask_id !== a.ask_id) }
    case 'clear_approvals': return { ...state, approvals: [] }
    case 'prune_gates': {
      // 服务端说「这些才是真挂着的」。只剪掉在快照起点之前就已存在的卡：
      // 拉取期间新到达的卡 born 晚于起点，不在剪枝范围内（否则会把刚弹的摘掉）。
      // 这一条防的是 bridge 换代后屏幕上留着永远点不动的僵尸卡。
      //
      // approvals / ask 可分别关闭：某条通道拉取失败时，我们对它的真实状态
      // 一无所知 —— 此时"没看见卡"绝不等于"服务端没有卡"。拿看不见当不存在去剪枝，
      // 会把屏幕上真挂着的卡删掉，正是要修的那类 bug 的镜像。默认开启以保持旧语义。
      const live = new Set(a.ids)
      const approvals = a.approvals === false ? state.approvals
        : state.approvals.filter((x) => live.has(x.ask_id) || (x.born ?? 0) >= a.before)
      // 提问是多卡的：逐张判定，留下"服务端确认还在"或"快照起点之后才出现的"那些
      const clarPrev = state.clarifies
      const clarifies = a.ask === false ? clarPrev
        : clarPrev.filter((x) => live.has(x.ask_id) || (x.born ?? 0) >= a.before)
      if (approvals.length === state.approvals.length
          && clarifies.length === clarPrev.length) return state
      return { ...state, approvals, clarifies }
    }
    case 'reset_timeline': return { ...state, timeline: [], progress: [], liveTurns: [],
                                   turnPartial: [] }
    // 网关换代：**不再清桶**（主人 2026-09-13：任何情况下都要常驻）。
    // bridge 侧已把计量账本落盘并在新代载入，新代发来的桶是接着上次累计的，
    // 所以这里只需把 seq 打回本代起点，避免落盘用旧的高 lastSeq 覆盖。
    case 'reset_usage': return { ...state, lastSeq: a.seq ?? 0 }
    case 'merge_orch': {
      // 编排器占用视图对表（装载 / 重连 / 定时）。**只有 authoritative 才动状态**：
      // 没问到 ≠ 没有作业在跑。拿"看不见"去熄灭橙点，正是这次要修的 bug 的镜像。
      if (!a.authoritative) return state
      const live = new Set(a.pipes.map((p) => p.tc_id).filter(Boolean))
      const pipes: Record<string, PipeView> = {}
      // 1) 本地已有的先留下；但"服务端说它不在跑了"的运行中条目要落回空闲
      for (const [k, v] of Object.entries(state.pipes)) {
        if (live.has(k)) continue
        // 服务端权威快照里没有它 = 它已经不在跑（结算/被一起停/出册）——
        // 落回空闲的同时**摘掉 background**：那标记描述"此刻"，别留成历史条目的永久属性
        // （旧写法留着它，于是这一格再跑前台任务时仍被判成橙色）。
        pipes[k] = (v.status === 'running' || v.status === 'pending')
          ? { ...v, status: 'idle', background: false }
          : { ...v, background: false }
      }
      // 2) 服务端条目覆盖：状态/槽位/作业身份一律以它为准（它就是权威快照）
      for (const p of a.pipes) {
        if (!p.tc_id) continue
        const old = pipes[p.tc_id]
        pipes[p.tc_id] = {
          tool: p.tool || old?.tool || '?',
          status: p.status || 'idle',
          layer: p.layer ?? old?.layer ?? null,
          at: now(),
          thread: p.thread ?? old?.thread ?? null,
          elapsed: p.elapsed ?? null,
          background: !!p.background,
          job_id: p.job_id ?? old?.job_id ?? null,
          job_label: p.job_label ?? old?.job_label ?? null,
        }
      }
      return { ...state, pipes }
    }
    case 'event': return onEvent(state, a.evt)
    default: return state
  }
}

const Ctx = createContext<{ state: State; dispatch: Dispatch<Action> } | null>(null)

export function AppProvider({ children }: { children: React.ReactNode }) {
  // 初始态从本地装载：刷新不丢；网关换代时由 App.tsx 派发 reset_usage 作废
  const [state, dispatch] = useReducer(reducer, undefined, bootState)
  // 概况桶落盘：存的是每个会话最新累计值，体量很小
  useEffect(() => {
    saveUsage(state.usageBySid, state.modelBySid, state.lastSeq)
  }, [state.usageBySid, state.modelBySid, state.lastSeq])

  const value = useMemo(() => ({ state, dispatch }), [state])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useApp() {
  const c = useContext(Ctx)
  if (!c) throw new Error('useApp 必须在 AppProvider 内使用')
  return c
}

/**
 * 初始态：把上一次的概况桶从 localStorage 捞回来。
 * 桶只在「本地快照的 hub_seq 落在已保存的订阅点之后」才有效，换代判定在 App.tsx 里
 * 用「收到的帧 hub_seq < 本地 usage_seq」触发 reset_usage（网关的 hub_seq 每代从 0 起）。
 */
function bootState(): State {
  const stored = loadUsage()
  return { ...initialState, usageBySid: stored.bySid as State['usageBySid'],
           modelBySid: stored.models as State['modelBySid'] }
}
