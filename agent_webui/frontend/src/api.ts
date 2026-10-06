// 后端 REST 封装。同源 /api（开发模式由 vite proxy 转发到网关）。
import type {
  AgentStatus, ApprovalRule, Attachment, AttachmentLimits, DeleteSessionResp, HistoryResp,
  McpStationsResp, OrchView, RegistriesResp, SearchResp, SessionsResp,
  ToolsResp, WsEvent, WorkspaceStatus,
} from './types'
import type { ModeOption } from './lib/permissionMode'

const BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch (e) {
    throw new ApiError(0, `网关不可达：${(e as Error).message}（后端是否已启动？）`)
  }
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = { raw: text } }
  }
  if (!res.ok) {
    const d = data as { detail?: string; error?: string } | null
    throw new ApiError(res.status, d?.detail || d?.error || `HTTP ${res.status}`)
  }
  return data as T
}

const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: 'POST', body: body === undefined ? '{}' : JSON.stringify(body) })

/** 电源类接口的返回：状态快照 + 动作结果 */
export type PowerResult = Partial<AgentStatus> & {
  ok?: boolean; error?: string; reason?: string; killed?: boolean
  already?: boolean; already_off?: boolean; model?: string; tools?: number
  injected?: string[]; mode?: string
}

export const api = {
  // ---- 会话 ----
  sessions: () => call<SessionsResp>('/sessions'),
  history: (sid: string) => call<HistoryResp>(`/sessions/${encodeURIComponent(sid)}/history`),
  createSession: (session_id?: string, prefix?: string, title?: string) =>
    post<{ session_id: string; created: boolean; resumed: boolean; title?: string }>(
      '/sessions', { session_id, prefix, title }),
  renameSession: (sid: string, title: string) =>
    post<{ ok: boolean; session_id: string; title: string }>(
      `/sessions/${encodeURIComponent(sid)}/title`, { title }),
  searchSessions: (q: string, scope = 'all', status = '') =>
    call<SearchResp>(`/sessions/search?q=${encodeURIComponent(q)}&scope=${scope}&status=${status}`),
  deleteSession: (sid: string) => call<DeleteSessionResp>(
    `/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' }),

  // ---- 对话 ----
  chat: (session_id: string, message: string, attachments?: string[]) =>
    post<{ ok: boolean; run_id: string; session_id: string }>(
      '/chat', { session_id, message, attachments }),
  /** 中期交互：回合运行中追加「用户交代」。**不起新回合**，投给正在跑的那个，
   *  由 AB 在下一批工具返回时注入。没有活动回合 / 回合或会话不符 → 409，回合已结束 → 404。 */
  midTurn: (session_id: string, text: string, run_id?: string | null,
            attachments?: string[]) =>
    post<{ ok: boolean; item_id?: string; pending?: number; run_id?: string;
           session_id?: string; error?: string }>(
      '/chat/mid_turn', { session_id, text, run_id, attachments }),
  stopChat: (run_id?: string | null) => post<{ ok: boolean; stopped?: boolean; note?: string | null; reason?: string }>(
    '/chat/stop', { run_id }),
  askAnswer: (ask_id: string, answer: string) =>
    post<{ ok: boolean; batch_live?: number; remaining?: number }>('/ask/answer', { ask_id, answer }),
  // 审批裁决：统一提交「勾选项 + 批准范围」。未勾的即视为拒绝（整批不放行）。
  // 审批已结束（超时/回合终止）时后端返 410，前端必须如实告知答复未被采纳。
  approvalAnswer: (ask_id: string, approved: string[], scope: string, stop = false,
                   note = '') =>
    post<{ ok: boolean; approved?: number }>(
      '/approval/answer', { ask_id, approved, scope, stop, note }),

  // ---- 附件 ----
  /** 上传附件：base64 JSON（网关没有 python-multipart，故不走 multipart）。
   *  后端只落盘 + 分类，**不执行、不解析**文件内容。 */
  uploadAttachment: (session_id: string, filename: string, data_b64: string) =>
    post<{ ok: boolean; attachment: Attachment }>(
      '/attachments', { session_id, filename, data_b64 }),
  attachmentLimits: () => call<AttachmentLimits>('/attachments/limits'),

  // ---- 电源 ----
  // 免审规则：查询与撤销都是整包 body，网关不做字段白名单
  /** 服务端当前真挂着的审批 / 提问：刷新、切会话、重开页面后靠它找回卡片。
   *  这两个端点是 GET —— 曾经这里误用 post()，于是每次恢复请求都被后端 405 掉，
   *  而调用点的 catch 把它吞了。结果是"恢复通道"自始至终一次都没生效过，
   *  四个"卡片会消失"的现象当然一个也没修好。方法写错就整条通道失效，
   *  所以网关侧现在同时收 GET/POST，两边都不许再靠单点正确性。 */
  pendingApprovals: () => call<{ ok: boolean; cards: WsEvent[]; error?: string }>('/approval/pending'),
  pendingAsk: () => call<{ ok: boolean; cards: WsEvent[]; error?: string }>('/ask/pending'),
  /** 编排器占用视图：跨回合作业占了哪些槽位。刷新/重连/定时对表都读它 ——
   *  前端内存里的 pipes 一刷新就没了，没有这条通道，跑着的作业就是黑箱。
   *  只有 authoritative === true 才代表"服务端真说了"；False 时不得据此熄灭橙点。 */
  orch: () => call<OrchView>('/orch'),
  approvalRules: (session_id = '') =>
    post<{ ok: boolean; rules?: ApprovalRule[]; error?: string }>(
      '/approval/rules', { session_id }),
  approvalRevoke: (path = '', scope = '', session_id = '') =>
    post<{ ok: boolean; removed?: number; rules?: ApprovalRule[]; error?: string }>(
      '/approval/rules/revoke', { path, scope, session_id }),

  agentStart: (mode = '') => post<PowerResult>('/agent/start', { mode }),
  agentStop: () => post<PowerResult>('/agent/stop'),
  agentKill: () => post<PowerResult>('/agent/kill'),
  agentStatus: () => call<AgentStatus>('/agent/status'),
  agentTools: () => call<ToolsResp>('/agent/tools'),
  agentLog: (lines = 80) => call<{ ok: boolean; tail: string }>(`/agent/log?lines=${lines}`),

  // ---- MCP 服务站（v2）----
  // 常驻快照：只读 station 文件夹 + 运行时表，不起进程，**不需要先发消息**。
  mcpStations: () => call<McpStationsResp>('/mcp/stations'),

  // ---- 技能 / 集成包（运行时面板）----
  // 与 mcpStations 同款常驻快照：只读盘、不起进程、**不需要先发消息**。
  // 一次给两块（技能 + 集成包），两块各自独立成败。
  registries: () => call<RegistriesResp>('/registries'),

  // ---- 工作区 ----
  workspace: () => call<WorkspaceStatus>('/workspace/status'),
  workspaceDir: (p: string) => call<Record<string, unknown>>(`/workspace/dir?path=${encodeURIComponent(p)}`),

  // ---- 权限模式（会话级四档，2026-10）----
  /** 带 mode = 切换，不带 = 只查。返回当前档 + 四档清单（catalog）。
   *  AB 没开机时网关直接写会话文件（offline: true），开机加载会话时生效。 */
  permissionMode: (session_id: string, mode?: string) =>
    post<{ ok: boolean; session_id: string; mode: string; label?: string; icon?: string;
           previous?: string; changed?: boolean; offline?: boolean; error?: string;
           valid?: string[]; catalog?: ModeOption[] }>(
      '/permission/mode', mode ? { session_id, mode } : { session_id }),

  health: () => call<Record<string, unknown>>('/health'),
}

export const EVENTS = [
  'agent_phase', 'turn_phase', 'stage', 'tool_begin', 'tool_end',
  // ⚠️ 'reasoning' 曾经**漏在这张表里**（2026-10-02 修）：openEvents 是按名字逐个
  //    addEventListener 的，不在表里的事件名浏览器端根本不会派发 ——
  //    bridge 一直在发 reasoning，前端却永远收不到，于是「思考过程」只有等回合收尾后
  //    刷新/切会话（走 history 里的 reasoning_content）才看得到。
  //    契约测试 test_api_ts_allowlist_matches_types_ts 一直在为这条红着。
  'progress', 'reasoning', 'delta', 'text',
  'ask_request', 'ask_resolved', 'approval_request', 'approval_expired',
  'approval_resolved', 'approval_batch', 'approval_batch_resolved',
  'mid_turn',
  'permission_mode',
  'done', 'error', 'heartbeat',
  'pipeline', 'usage',
] as const

/** 打开 SSE；返回 close()。onEvent 收到解析后的事件。 */
export function openEvents(
  since: number,
  onEvent: (e: WsEvent) => void,
  onState: (s: 'open' | 'error' | 'closed') => void,
  sessionId?: string | null,
): () => void {
  const q = new URLSearchParams({ since: String(since) })
  if (sessionId) q.set('session_id', sessionId)
  const es = new EventSource(`${BASE}/events?${q.toString()}`)
  es.onopen = () => onState('open')
  es.onerror = () => onState(es.readyState === EventSource.CLOSED ? 'closed' : 'error')
  for (const t of EVENTS) {
    es.addEventListener(t, (ev: MessageEvent) => {
      try { onEvent(JSON.parse(ev.data) as WsEvent) } catch { /* 忽略坏帧 */ }
    })
  }
  return () => es.close()
}
