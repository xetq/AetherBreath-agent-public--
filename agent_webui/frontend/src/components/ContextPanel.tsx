import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import RegistrySect from './RegistrySect'
import { useApp } from '../store/appStore'
import type { ApprovalRule, McpStationsResp, RegistriesResp, ToolsResp } from '../types'

/** 紫色 chip 的含义：**运行时注入** —— WebUI 进程在启动后挂上去的（不在 agent_tools/ 源码里）。
 *  鼠标悬停能看到这句；看着有规律，比只标一个 ask_user 强。 */
const INJ_TIP = '运行时注入：WebUI 进程在启动后挂上去的（不在 agent_tools/ 源码里）'

/** 大数字缩写（与概览卡片同一口径：1.2k） */
const kfmt = (v?: number) => ((v ?? 0) >= 1000 ? `${((v as number) / 1000).toFixed(1)}k` : String(v ?? 0))

export default function ContextPanel() {
  const { state, dispatch } = useApp()
  const [tools, setTools] = useState<ToolsResp | null>(null)
  const [log, setLog] = useState<string>('')
  // MCP 服务站：这些值**常驻**（只读 station 文件夹 + 运行时表，不需要先发一条消息）
  const [mcp, setMcp] = useState<McpStationsResp | null>(null)
  // 技能 / 集成包：与 MCP 同款**常驻**快照（只读盘，不需要先发一条消息）
  const [reg, setReg] = useState<RegistriesResp | null>(null)
  const [regErr, setRegErr] = useState('')
  // 三区折叠状态（技能 / MCP / 集成包）—— 默认都展开，裁决 Q3 + Q5
  const [mcpOpen, setMcpOpen] = useState(true)
  const [mcpErr, setMcpErr] = useState('')
  // 免审规则：这些是「点过一次就永久不再问」的授权，必须看得见、也删得掉
  const [rules, setRules] = useState<ApprovalRule[]>([])
  const [ruleErr, setRuleErr] = useState('')
  const [armed, setArmed] = useState('')
  const a = state.agent
  const mcpStations = mcp?.stations ?? []
  const mcpIssues = mcp?.issues ?? []
  // 「工具集」= 常驻工具 + 运行时注入项，**合在一起按字母序排**（原来注入项单独占一行灰字，
  // 主人说"站在外面太突兀、增加阅读成本"）。
  // 紫色 = 这一项是**运行时注入**进来的（按来源判定，不按名字）。
  // **同名去重**：同一个名字只留一条，保留"运行时注入"那条（紫）—— 主人明确要求去掉灰色的
  // 重复项（ask_user 两边都有：留紫的那条，它带 tooltip 说明含义）。
  const merged = new Map<string, boolean>()
  for (const n of tools?.tools || []) merged.set(n, false)
  for (const n of tools?.injected || []) merged.set(n, true)   // 注入项覆盖同名工具
  const dupMerged = (tools?.tools || []).filter((n) => (tools?.injected || []).includes(n)).length
  const chips = [...merged.entries()]
    .map(([n, inj]) => ({ key: `${inj ? 'i' : 't'}:${n}`, n, inj }))
    .sort((a, b) => (a.n < b.n ? -1 : a.n > b.n ? 1 : 0))

  const load = useCallback(async () => {
    if (a.phase !== 'ON') { setTools(null); return }
    try { setTools(await api.agentTools()) } catch (e) { setTools({ ok: false, error: (e as Error).message }) }
    try { setLog((await api.agentLog(120)).tail || '') } catch { setLog('') }
  }, [a.phase])

  const loadRules = useCallback(async () => {
    if (a.phase !== 'ON') { setRules([]); return }
    try {
      const r = await api.approvalRules(state.current || '')
      setRules(r.rules || [])
      setRuleErr(r.error || '')
    } catch (e) { setRuleErr((e as Error).message) }
  }, [a.phase, state.current])

  async function revokeRule(r: ApprovalRule) {
    setArmed('')
    try {
      const res = await api.approvalRevoke(r.path, r.scope, state.current || '')
      setRules(res.rules || [])
      dispatch({ type: 'toast', kind: 'ok', msg: '已撤销 ' + (res.removed ?? 0) + ' 条免审规则' })
    } catch (e) { setRuleErr((e as Error).message) }
  }

  async function revokeAll() {
    if (armed !== 'all') { setArmed('all'); return }        // 两段式，防误点
    setArmed('')
    try {
      const res = await api.approvalRevoke('', '', state.current || '')
      setRules(res.rules || [])
      dispatch({ type: 'toast', kind: 'ok', msg: '已撤销全部免审规则（' + (res.removed ?? 0) + ' 条）' })
    } catch (e) { setRuleErr((e as Error).message) }
  }

  const loadMcp = useCallback(async () => {
    // 刻意**不看** a.phase：station 状态是磁盘 + 运行时表的快照，AB 关着也该看得见
    try {
      setMcp(await api.mcpStations())
      setMcpErr('')
    } catch (e) { setMcpErr((e as Error).message) }
  }, [])

  const loadReg = useCallback(async () => {
    // 同上：技能库与包目录也是磁盘现状，AB 开没开都该看得见
    try {
      setReg(await api.registries())
      setRegErr('')
    } catch (e) { setRegErr((e as Error).message) }
  }, [])

  useEffect(() => { void load() }, [load])
  useEffect(() => { void loadRules() }, [loadRules])
  useEffect(() => {
    void loadMcp(); void loadReg()
    // 常驻看板：5s 自己刷（两个请求共用同一个定时器）
    const t = window.setInterval(() => { void loadMcp(); void loadReg() }, 5000)
    return () => window.clearInterval(t)
  }, [loadMcp, loadReg])

  return (
    <div className="panel-body">
      <div className="ctx-sect">
        <div className="tl-lbl">进程</div>
        <div className="kv"><span>phase</span><b>{a.phase}</b></div>
        <div className="kv"><span>turn</span><b>{a.turn_phase}</b></div>
        <div className="kv"><span>pid</span><b>{a.pid ?? '—'}</b></div>
        <div className="kv"><span>bridge</span><b className="mono">{a.bridge_url ?? '—'}</b></div>
        <div className="kv"><span>mode</span><b>{String(a.mode ?? a.hello?.mode ?? 'default')}</b></div>
        <div className="kv"><span>model</span><b>{String(a.hello?.model ?? '—')}</b></div>
        <div className="kv"><span>事件订阅</span><b>{a.subscribers ?? 0}</b></div>
        <div className="kv"><span>优雅超时</span><b>{a.graceful_timeout ?? '—'}s</b></div>
        {a.last_error && <div className="kv err"><span>last_error</span><b>{String(a.last_error).slice(0, 160)}</b></div>}
      </div>

      <RegistrySect kind="skills" data={reg?.skills ?? null} err={regErr} />

      <div className="ctx-sect">
        <div className="tl-lbl">
          <button className="tl-fold" onClick={() => setMcpOpen((v) => !v)}
                  title={mcpOpen ? '收起这一区' : '展开这一区'}>
            <span className="tl-caret">{mcpOpen ? '▾' : '▸'}</span>
            <span>MCP 服务站{mcp?.ok ? `（${mcp.on_switch ?? 0}/${mcp.count ?? 0} 开 · ${mcp.online ?? 0} 在线 · ${mcp.total_tools ?? 0} 工具）` : '（—）'}</span>
          </button>
          <button className="btn tiny ghost right" onClick={() => void loadMcp()}>刷新</button>
        </div>
        {mcpOpen && (<>
        {mcpErr ? <div className="hint danger">读取失败：{mcpErr}</div> : null}
        {mcp && mcp.ok === false ? <div className="hint danger">{mcp.error}</div> : null}
        {mcp?.ok && mcp.enabled === false ? (
          <div className="hint">总闸关着（<code>config.yaml</code> 的 <code>mcp.enabled: false</code>）：注册表与两个元工具都没给模型。</div>
        ) : null}
        {mcp?.ok && (mcp.count ?? 0) === 0 ? (
          <div className="hint">还没有 station。建 <code>agent_MCP/&lt;名字&gt;/STATION.md</code>，或让 AB 用 mcp_manage 自己装一个。</div>
        ) : null}
        {mcpStations.length > 0 && (
          <ul className="mcp-list">
            {mcpStations.map((s) => (
              <li key={s.name} className={s.enabled && s.usable ? '' : 'off'}>
                <div className="mcp-top">
                  <span className={'mcp-dot ' + (s.alive ? 'on' : 'idle')}
                        title={s.alive ? 'server 进程在跑' : '进程未起（懒启动：第一次调用才起）'} />
                  <b className="mcp-name">{s.name}</b>
                  <span className={'mcp-tag ' + (s.origin === 'hand' ? 'hand' : 'auto')}
                        title={s.origin === 'hand' ? '你手写的：机器不改它' : 'AB 自己集成的'}>
                    {s.origin === 'hand' ? '手写' : '自集成'}
                  </span>
                  {!s.enabled && <span className="mcp-tag shut">已关</span>}
                  {!s.usable && <span className="mcp-tag bad" title={(s.problems || []).join('；')}>有毛病</span>}
                  <span className="mcp-calls">{kfmt(s.calls)} 次</span>
                </div>
                <div className="mcp-meta" title={s.description || ''}>
                  {s.tools} 工具 · {!mcp?.snapshot_at
                    ? '运行态未知（agent 还没跑过）'
                    : `${s.alive ? '在线' : '未起'} · 调用 ${kfmt(s.calls)} 次 · 最近${s.last_tool ? ` ${s.last_tool} ${String(s.last_call_at || '').slice(11)}` : '还没调过'}`}
                </div>
                {(s.problems || []).length > 0 && <div className="mcp-prob">⚠ {s.problems[0]}</div>}
                {(s.missing_env || []).length > 0 && <div className="mcp-prob warn">缺环境变量：{s.missing_env.join('、')}</div>}
              </li>
            ))}
          </ul>
        )}
        {mcp?.ok && mcpIssues.length > 0 && (
          <div className="hint">📌 {mcpIssues.slice(0, 2).join('；')}</div>
        )}
        <div className="hint">工具 schema <b>不进上下文</b>（按需 <code>mcp_search</code> 取）；调用走 <code>mcp_call</code>，每个 station 第一次会弹批准卡。{mcp?.snapshot_at
          ? <>　运行态来自 agent 快照（{mcp.snapshot_at}{mcp.runner_pid ? ` · pid ${mcp.runner_pid}` : ''}）。</>
          : <>　<b>运行态未知</b>：agent 还没写过快照（面板在网关进程里，看不到 agent 的连接表）。</>}</div>
        </>)}
      </div>

      <RegistrySect kind="packs" data={reg?.packs ?? null} err={regErr} />

      <div className="ctx-sect">
        <div className="tl-lbl">
          <span title={tools ? `常驻工具 ${tools.tools?.length ?? 0} + 运行时注入 ${tools.injected?.length ?? 0}${dupMerged ? `（同名合并 ${dupMerged}）` : ''}` : undefined}>
            工具集（{chips.length || '—'}）
          </span>
          <button className="btn tiny ghost right" onClick={() => void load()}>刷新</button>
        </div>
        {a.phase !== 'ON' ? <div className="empty">开机后可见</div>
          : <div className="chips">{chips.map((c) => (
            <span key={c.key}
                  className={`chip ${c.inj ? 'inj' : ''}`}
                  title={c.inj ? INJ_TIP : undefined}>{c.n}</span>))}</div>}
      </div>

      <div className="ctx-sect">
        <div className="tl-lbl">免审规则（{rules.length} 条）
          <button className="btn tiny ghost right" onClick={() => void loadRules()}>重载</button>
        </div>
        {ruleErr ? <div className="hint danger">读取失败：{ruleErr}</div> : null}
        {rules.length === 0 ? (
          <div className="hint">当前没有免审规则：系统盘的写入/删除/移动，每一次都会问你。</div>
        ) : (
          <ul className="rule-list">
            {rules.map((r, i) => (
              <li key={r.scope + '|' + r.path + '|' + i}>
                <span className={'rule-scope ' + r.scope}>
                  {r.scope === 'persistent' ? '永久' : '本会话'}
                </span>
                <code title={r.kind || ''}>{r.path}</code>
                <button className="btn tiny ghost" onClick={() => void revokeRule(r)}>撤销</button>
              </li>
            ))}
          </ul>
        )}
        {rules.length > 0 && (
          <button className={'btn small ghost danger' + (armed === 'all' ? ' armed' : '')}
                  onClick={() => void revokeAll()}>
            {armed === 'all' ? '再点一次确认：撤销全部' : '撤销全部'}
          </button>
        )}
        <div className="hint">撤销同时改内存与磁盘 —— 只清文件会残留到进程里，重启后诈尸。</div>
      </div>

      <div className="ctx-sect">
        <div className="tl-lbl">bridge 日志尾部（stderr）
          <button className="btn tiny ghost right" onClick={() => void load()}>重载</button>
        </div>
        <pre className="logbox">{log || '（空）'}</pre>
      </div>

      <div className="ctx-sect">
        <div className="tl-lbl">提示</div>
        <div className="hint">改 SOUL/AGENTS/MEMORY/技能需「新建会话」才生效（语境快照冻结）；改工具需重新开机。思考过程与工具运行已收进输出卡片（右栏原「工具时间线」位置已预留）。</div>
        <button className="btn small ghost" onClick={() => { dispatch({ type: 'reset_timeline' }); dispatch({ type: 'toast', kind: 'info', msg: '进度与工具缓存已清空' }) }}>
          清空进度与工具缓存
        </button>
      </div>
    </div>
  )
}
