// 与 backend 对齐的类型（单一来源：agent_webui/backend/{events,api,sessions,workspace}.py）
// 改后端字段时同步这里；增强的类型生成方案见 README「扩展指南」。

export type AgentPhase = 'OFF' | 'STARTING' | 'ON' | 'STOPPING'

export type TurnPhase =
  | 'IDLE' | 'THINKING' | 'TOOL_RUNNING' | 'RESPONDING'
  | 'ASK_WAIT' | 'AUDIT_WAIT' | 'INTERRUPTED' | 'ERROR'

export interface AgentStatus {
  phase: AgentPhase
  turn_phase: TurnPhase
  pid: number | null
  bridge_url: string | null
  session_id: string | null
  run_id: string | null
  last_error: string | null
  health_at: string | null
  can_send: boolean
  busy: boolean
  alive?: boolean
  hello?: PoolHello
  mode?: string
  injected?: string[]
  subscribers?: number
  graceful_timeout?: number
  ask_timeout?: number
  gateway?: { host: string; port: number; pid: number }
}

export interface SessionMeta {
  session_id: string
  /** 上下文视图元数据（agent/context_manager.py 落盘；未压缩过 = undefined/{}） */
  ctx?: CtxMeta
  title?: string
  display?: string
  file?: string
  message_count: number
  user_turns?: number
  tool_calls?: number
  status: 'active' | 'complete' | 'interrupted' | 'unknown' | string
  status_label?: string
  has_snapshot?: boolean
  summary: string
  last_activity?: string
  saved_at?: string
  size?: number
  unreadable?: boolean
}

/** 上下文管理器：视图元数据（backend/sessions.py:_ctx_meta 读出） */
export interface Attachment {
  id: string
  name: string
  /** 相对项目根的路径 —— 发送时回传的就是它（后端会重新校验合法性） */
  path: string
  abs?: string
  size: number
  kind: 'image' | 'text' | 'doc'
  ext: string
  session_id?: string
}

/** 附件开关/上限/白名单（GET /api/attachments/limits） */
export interface AttachmentLimits {
  ok: boolean
  enabled: boolean
  max_file_mb: number
  max_per_message: number
  max_text_chars: number
  accepted: { image: string[]; text: string[]; doc: string[] }
}

export interface CtxMeta {
  version?: number
  source_len?: number
  rounds?: number
  saved_ratio?: number
  est_before?: number
  est_after?: number
  kept_rounds?: number
  compactions?: number
  last_compact_at?: string
  /** 原文占用估算（本地口径 1 字符≈0.5 token）：没压缩过、也没发过消息时的圆圈兜底 */
  est_original?: number
}

// ---------- 会话搜索 ----------
export interface SearchHit {
  field: 'title' | 'content'
  role?: string
  at?: number
  snippet: string
}

export interface SearchMatch {
  session_id: string
  title: string
  display?: string
  summary?: string
  status?: string
  status_label?: string
  message_count?: number
  last_activity?: string
  title_matched: boolean
  hit_count: number
  hits: SearchHit[]
}

export interface SearchResp {
  ok: boolean
  query: string
  scope?: string
  status?: string
  scanned: number
  matched: number
  matches: SearchMatch[]
}

export interface SessionsResp {
  sessions: SessionMeta[]
  total: number
  dir: string
}

/** 删除会话的返回：血缘三件套（原文 + 视图 + 事件流）一起移入 .trash */
export interface DeleteSessionResp {
  ok: boolean
  session_id?: string
  moved_to: string
  moved?: { kind: 'session' | 'view' | 'view_events' | string; to: string }[]
  warnings?: string[]
}

export interface ToolCallView {
  id: string
  name: string
  args: string
  result: string | null
  failed: boolean
  pending: boolean
}

/** 一条消息携带的附件（历史由后端从【附件】标记行剥出；实时由前端上传后直接带上） */
export interface MsgAttachment {
  name: string
  path: string
}

import type { ModeOption } from './lib/permissionMode'

export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
  /** 这一轮模型的思考原文（agent 存 reasoning_content；上下文管理器只保留最近 N 轮，
   *  更早的被清掉 —— 那时没有这个键，界面就不渲染「思考过程」块，不做空壳）。 */
  reasoning?: string
  /** 这条消息带了哪些附件 —— 已发送的卡片靠它回显（主人要求） */
  attachments?: MsgAttachment[]
  tool_calls?: ToolCallView[]
  timeline?: TimelineEntry[]
  /** 仅前端视图态：这条是「用户交代」（历史靠前缀识别，实时靠 mid_turn 事件）。
   *  pending=已投递待注入 / delivered=已随工具返回注入 / dropped=回合结束未送达 */
  mid?: 'pending' | 'delivered' | 'dropped'
  /** 投递回执 id：injected/dropped 事件按它精确更新状态（乱序到达也不会错配） */
  mid_id?: string
  /** 仅前端视图态：这条是**后台作业自动交付**（跨回合作业跑完后送进来的结果）。
   *  和「用户交代」一样靠前缀识别、一样挂在用户卡里 —— 但样式必须区分开：
   *  交代是"人说的话"，交付是"机器送回来的结果"（主人 2026-09-30 定）。 */
  job?: boolean
}

export interface HistoryResp {
  session_id: string
  exists: boolean
  status?: string
  status_label?: string
  message_count?: number
  saved_at?: string
  has_snapshot?: boolean
  snapshot_head?: string | null
  /** 本会话当前的权限模式（四档：readonly/workspace/normal/full）。
   *  缺省 = 旧会话，按「普通」显示（后端也不返回旧字段）。 */
  permission_mode?: string
  /** 四档清单（含每档一句话状态）：界面那块状态块用它，与模型那行同源。 */
  permission_catalog?: ModeOption[]
  messages: HistoryMessage[]
  /** 中期过程（按 user_seq 对齐到回合）；老会话/CLI 回合没有 → 空/缺省 */
  turns?: SidecarTurn[]
  /** **进行中**回合的半个快照（bridge 节流落盘）；没有进行中回合时为 null/缺省。
   *  与 turns 里那条同名回合互斥：它没写进 turns 正是因为那个回合还没收尾。 */
  partial?: SidecarTurn | null
}

// ---------- 时间线 ----------
export interface TimelineEntry {
  key: string
  tool: string
  args: string
  result: string | null
  ok: boolean
  error: string | null
  elapsed: number | null
  call_id: string
  thread?: string
  run_id?: string
  session_id?: string | null
  source: 'live' | 'history'
  at?: number
  status: 'running' | 'done'
}

/** 实时回合内按「模型轮」攒的块：思考 + 该轮的工具（存 call_id，条目本体在 timeline 里）
 *  + 该轮的中期输出行（agent stdout）。回合结束后转成消息，刷新后由 sidecar 还原，
 *  两条路同源，所以界面前后一致。 */
export interface LiveTurn {
  at: number
  reasoning: string
  toolIds: string[]
  outs: string[]
  /** 本轮的思考**增量草稿**（流式 delta 攒的）。真实事件到达后 reasoning 会覆盖它 ——
   *  有它才能"边想边看"，而不是等整轮生成完才第一次看到思考（2026-10-02 流式）。 */
  thinkDraft?: string
  /** 本轮的正文**增量草稿**：模型边写边显示，轮末转成正式的中期输出/最终输出。 */
  textDraft?: string
}

/** WebUI 中期过程的一个回合（bridge 落盘 → 网关透出）。
 *  outs 是 agent 的 stdout 行，按"第几轮"分组 —— 会话文件里没有它们，
 *  不落盘的话刷新后这一段就没了（主人 2026-09-23 要求刷新前后逐字一致）。 */
export interface SidecarTurn {
  user_seq: number
  run_id: string
  ended: 'done' | 'interrupted' | 'error' | string
  at?: number | null
  outs: { round: number; text: string }[]
  /** 每轮思考原文（round → 文本）。**只有进行中快照带**：它是"思考在收尾前也在
   *  磁盘上"的唯一来源 —— 会话文件里的 reasoning_content 会被上下文管理清空，
   *  刷新后靠它才不至于让本回合的思考块变成空壳。 */
  reasoning?: Record<string, string>
  /** true = 这条是**没收尾**的进行中快照（收尾后由 turns 里的正式条目接管） */
  partial?: boolean
}

// ---------- SSE 事件 ----------
export type WsEventType =
  | 'agent_phase' | 'turn_phase' | 'stage' | 'tool_begin' | 'tool_end'
  | 'progress' | 'reasoning' | 'delta' | 'text' | 'ask_request' | 'ask_resolved' | 'approval_request' | 'approval_expired'
  | 'approval_resolved' | 'approval_batch' | 'approval_batch_resolved'
  | 'mid_turn'
  | 'permission_mode'
  | 'done' | 'error' | 'heartbeat'
  | 'pipeline' | 'usage'

export interface WsEvent {
  type: WsEventType
  hub_seq?: number
  /** SSE 回放帧（刷新/重连时补历史）。只补时间线，不许改写"此刻谁在等人"。 */
  replayed?: boolean
  ts?: number | string
  seq?: number
  run_id?: string | null
  session_id?: string | null
  phase?: TurnPhase | AgentPhase
  turn_phase?: TurnPhase | AgentPhase
  reason?: string
  ok?: boolean
  timeout?: number
  // 审批卡：引擎决定是否收集主人的补充说明（逐字段搬运见 appStore reducer）
  accepts_note?: boolean
  note_hint?: string
  already?: boolean
  killed?: boolean
  stage?: string
  /** 本回合第几轮模型往返（0 起）：bridge 给 reasoning / progress / tool_begin 都带它，
   *  前端照它归位 —— 光靠到达顺序会错位（中期进度 print 早于工具执行）。 */
  round?: number
  tool?: string
  args?: string
  result?: string | null
  error?: string | null
  elapsed?: number
  call_id?: string
  thread?: string
  /** 该管道是**跨回合后台作业**（bridge 上报；前端据此把槽位画成橙色） */
  background?: boolean
  /** 跨回合作业的身份（bridge 从作业登记册带出来，不是入参）。
   *  只有颜色没有身份 = 黑箱：橙点的 hover 要靠它说清"是哪个作业在跑"。 */
  job_id?: string | null
  job_label?: string | null
  index?: number
  text?: string
  content?: string
  message?: string
  iterations?: number
  tool_calls?: number
  interrupted?: boolean
  /** 本回合开头**自动交付**了哪些后台作业（bridge 从 agent 侧取的真实事件）。
   *  交付是落盘的消息、不是实时事件，所以界面靠它触发一次"以磁盘为准"的历史重放 ——
   *  否则那条交付要等你切会话/刷新才看得见。 */
  delivered_jobs?: string[]
  /** 权限模式（permission_mode 事件带）：主人切了档，界面据此更新下拉与状态块。
   *  `mode` / `label` / `icon` / `status` 见下方统一声明区（`status` 那里已有）。 */
  previous?: string
  label?: string
  icon?: string
  can_send?: boolean
  busy?: boolean
  pid?: number | null
  bridge_url?: string | null
  // ask
  ask_id?: string
  question?: string
  title?: string
  kind?: string
  flag?: string
  risk?: number
  paths?: string[]
  lines?: string[]
  source_code?: string
  channel?: string
  batch_id?: string
  batch_size?: number
  /** 审批裁决（approval_resolved 带）：A 本次 / B 本会话路径 / D 永久 / E 拒 / F 拒并中止。
   *  形象动画要判「被拒了」（E/F）就必须显式声明 —— WsEvent 没有索引签名，
   *  漏声明的字段会被静默丢掉（这类病在本项目犯过三次）。 */
  choice?: string
  /** 合并卡裁决（approval_batch_resolved 带）：被批准的 ask_id 列表；空数组 = 整批都没批。 */
  approved?: string[]
  batch_live?: number
  items?: unknown[]
  scopes?: unknown[]
  intent?: string
  notes?: string[]
  total?: number
  critical?: boolean
  // 恢复通道：服务端算出的剩余秒数 + 「这是找回来的卡」标记。
  // WsEvent 没有索引签名，新字段必须在此声明 —— 否则又是静默丢值那一类病。
  remaining?: number
  restored?: boolean
  user_request?: string
  options?: string[]
  mode?: string
  model?: string
  history_count?: number
  snapshot?: string
  tools?: string[] | number
  injected?: string[]
  error_detail?: string
  // 编排器管道状态机
  tc_id?: string
  status?: string
  live?: number
  layer?: number | null
  size?: number
  pool?: string
  // usage（token 计量，bridge 从 API 返回值直接读）
  call?: UsageNode
  session?: UsageNode
  global?: UsageNode
  // 中期交互（mid_turn 事件）：
  //   mid = accepted（已投递）| injected（已随工具返回注入）| dropped（回合结束未送达）
  mid?: string
  item_id?: string
  ids?: string[]
  texts?: string[]
  count?: number
  pending?: number
}

// ---------- token 计量 ----------
export interface UsageNode {
  calls: number; prompt: number; completion: number; reasoning: number; total: number
  /** 命中前缀缓存的输入 token（厂商不返回则恒为 0） */
  cached?: number
  /** 最近一次请求的输入 token = 当前上下文占用（压缩后会回落） */
  last_prompt?: number
}
export interface UsageSnapshot {
  /** 只表示本会话累计；本会话尚无计量时为 null（不再用全局数冒充） */
  scope: 'session'
  node: UsageNode
  session: UsageNode | null
  global: UsageNode
  model?: string | null
  at: number
}

// ---------- 编排器管道 ----------
export interface PipeView {
  tool: string
  status: 'pending' | 'running' | 'idle' | 'failed' | 'cancelled' | string
  layer: number | null
  at: number
  thread?: string | null
  elapsed?: number | null
  /** 该管道是**跨回合后台作业**（不随本回合结束）：编排器槽位画橙色 */
  background?: boolean
  /** 作业身份（作业号 + 可读名）：橙点的 hover 靠它说清"是哪个作业在跑" */
  job_id?: string | null
  job_label?: string | null
}

/** 编排器**占用视图**的一条（bridge `/orch` → 网关 `/api/orch`）。
 *  这是"刷新/重连之后仍然看得见谁占着槽位"的唯一通道 —— 只活在前端内存里的
 *  `state.pipes` 一刷新就空了，那时界面会退回"推导"模式，跑着的作业一格都不亮。 */
export interface OrchPipe {
  tc_id: string
  tool: string
  status: string
  layer?: number | null
  thread?: string | null
  /** 跨回合作业占用的槽位（画橙色）。作业还活着就一定带它。 */
  background?: boolean
  /** 服务端算好的已跑秒数（前端拿到后自己起秒针接着走） */
  elapsed?: number | null
  job_id?: string | null
  job_label?: string | null
}

export interface OrchView {
  ok?: boolean
  pipes: OrchPipe[]
  /** 服务端确实回了权威快照吗？False（没问到 / bridge 出错）时前端**不得**据此
   *  熄灭橙点 —— "看不见"不等于"没有作业在跑"，这条纪律与卡片恢复同源。 */
  authoritative?: boolean
  error?: string
}

// bridge 首行协议在网关 status.hello 里整包回显
export interface PoolHello {
  pools?: PoolCaps
  port?: number
  pid?: number
  mode?: string
  model?: string
  tools?: number
  injected?: string[]
  usage_watch?: string
  /** 上下文管理器配置（窗口/阈值），前端画占比圆圈用；由 bridge 从 CM_PARAMS 原样下发 */
  ctx?: CtxConfig
  /** 计量账本：上次退出前的每会话用量（bridge 落盘，重启后原样带回来） */
  usage_ledger?: { total?: UsageNode; by_sid?: Record<string, UsageNode> }
}

export interface CtxConfig {
  enabled?: boolean
  window?: number
  threshold?: number
  keep_recent_rounds?: number
}

// 编排器池容量（bridge 在 hello 里上报；懒起线程推不出来，必须显式给）
export interface PoolCap { workers: number; prefix?: string; started?: number }
export interface PoolCaps { parallel?: PoolCap; serial?: PoolCap }

// ---------- 审批（系统盘/高危门禁；与 ask 提问是不同语义，故不同通道）----------
export interface ApprovalItem {
  ask_id: string
  kind: string
  risk: number
  title?: string
  intent?: string
  reason?: string
  paths?: string[]
  notes?: string[]
  critical?: boolean
  source_code?: string
}

export interface ApprovalScope { key: string; label: string; emoji: string }

/** 一条已签发的免审规则。撤销必须同时改内存与磁盘，见 ContextPanel 的提示。 */
export interface ApprovalRule {
  path: string
  kind?: string
  scope: string          // persistent | session
  session_id?: string
  ask_id?: string
  ts?: number
  drive_root?: string
}

export interface ApprovalRequest {
  ask_id: string         // 回投时的 ask_id / batch_id
  batch: boolean         // true=合并卡；单条按 1 项的批处理，共用一套渲染
  items: ApprovalItem[]
  scopes: ApprovalScope[]
  total: number
  timeout?: number
  session_id?: string | null
  born?: number
  user_request?: string
  // 引擎决定是否收集主人的补充说明；缺字段（旧 bridge）就不显示输入框
  accepts_note?: boolean
  note_hint?: string
  /** 从服务端恢复回来的卡：倒计时按 remaining 续算，不许重置成满格 */
  remaining?: number
  restored?: boolean
}

export interface AskRequest {
  ask_id: string
  question: string
  options: string[]
  kind: 'choice' | 'multi' | 'freeform'
  run_id?: string | null
  session_id?: string | null
  timeout?: number
  answered?: string | null
  remaining?: number
  restored?: boolean
  /** 到达时刻（恢复的卡会被折算成"过去的时刻"，倒计时才连得上） */
  born?: number
  /** 同批一共几问 / 此刻还剩几个待答 —— ask_user 允许一条消息里并行问多个独立问题，
   *  这些卡共享一条等待窗口（有人作答就往后续）。 */
  batch_size?: number
  batch_live?: number
}

// ---------- 工作区 ----------
export interface WorkspaceFile { name: string; mtime: string; size: number }
export interface WorkspaceDir {
  name: string
  mtime: string
  file_count: number
  size: number
  files: WorkspaceFile[]
}
export interface WorkspaceStatus {
  workspace: string
  exists: boolean
  task_dirs: WorkspaceDir[]
  task_dir_count: number
  recent_files: { path: string; mtime: string; size: number }[]
  sessions: {
    dir: string; count: number; by_status: Record<string, number>
    latest: { session_id: string; mtime: string } | null; tmp_files?: number
  }
  skills?: { dir: string; count: number; registry: string | null }
  loose_files?: { name: string; mtime: string }[]
  generated_at: string
}

export interface ToolsResp {
  ok: boolean
  tools?: string[]
  injected?: string[]
  error?: string
}

/** MCP 服务站（v2）：一个 station = 一个 server 文件夹（agent_MCP/<name>/） */
export interface McpStation {
  name: string
  description: string
  /** hand = 主人手写（机器不改）；auto = AB 自己集成的 */
  origin: 'hand' | 'auto' | string
  enabled: boolean
  usable: boolean
  tools: number
  problems: string[]
  alive: boolean          // 进程是否在跑（懒启动：没起过 = false，属正常）
  calls: number           // 累计调用次数（来自 agent 侧快照；agent 没跑过 = 0）
  missing_env: string[]
  last_tool: string | null
  last_call_at: string | null
  last_call_ok: boolean | null
  server_version: string
}

export interface McpStationsResp {
  ok: boolean
  enabled?: boolean       // 总闸（config.yaml 的 mcp.enabled）
  stations_dir?: string
  registry?: string
  count?: number
  online?: number
  on_switch?: number
  total_tools?: number
  issues?: string[]
  stations?: McpStation[]
  /** agent 侧运行时快照的时间；null/缺省 = agent 还没跑过 → 运行态未知 */
  snapshot_at?: string | null
  /** 写快照的那个 agent 进程 PID */
  runner_pid?: number | null
  error?: string
}

/** 技能 / 集成包注册表（「运行时」面板的两块）—— 与 McpStation 同口径：常驻、只读盘、不起进程。
 *
 *  数据**来自扫文件夹**（agent_skills/<名字>/、agent_integration_packs/<名字>/），
 *  **不是**读 SKILL_REGISTRY.md / PACK_REGISTRY.md —— 那两份 .md 是「新会话启动时冻结、
 *  注入给模型看」的注入快照，与磁盘现状可以不一致；注册表在这里只报路径 + 更新时间。
 */
export interface RegistryRow {
  name: string
  description: string
  /** 正文 / 说明书相对项目根的 POSIX 路径（技能 = SKILL.md，包 = PACK.md） */
  doc: string
  /** 附赠字段：不占视觉，收进悬停提示里（主人 2026-09-23 裁决 Q2） */
  version?: string
  tags?: string[]
}

export interface RegistrySection {
  ok: boolean
  /** 被扫描的目录绝对路径 */
  dir?: string
  /** 注册表 .md 的文件名（只作对照，不是数据源） */
  registry?: string
  /** 注册表最后修改时间；null / 缺省 = 文件不存在（如实说没有） */
  registry_mtime?: string | null
  count?: number
  issues?: string[]
  /** 技能区用 skills、包区用 packs（同一个形状，前端一套渲染） */
  skills?: RegistryRow[]
  packs?: RegistryRow[]
  error?: string
}

export interface RegistriesResp {
  skills: RegistrySection
  packs: RegistrySection
}
