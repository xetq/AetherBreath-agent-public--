// 侧栏：搜索/筛选 + 会话列表（用户命名标题）+ 新建（可填标题，留空用默认摘要）。
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store/appStore'
import { useLoadSession } from '../lib/useLoadSession'
import type { CtxConfig, SearchResp, SessionMeta, UsageNode } from '../types'

const STATUS_ICON: Record<string, string> = {
  active: '🔄', complete: '✅', interrupted: '⚠️', unknown: '❓',
}

const n0 = (v?: number) => (v ?? 0).toLocaleString('en-US')

/** 会话卡片上的上下文占比圆圈。
 *  环 = 当前上下文占用 / 模型窗口（真实 usage，压缩后回落）；圈内数字 = 消息条数；
 *  悬停 = 窗口用量 · 触发阈值 · 压缩次数 · 缓存命中率。 */
function CtxRing({ meta, usage, cfg }: {
  meta: SessionMeta; usage?: UsageNode; cfg?: CtxConfig
}) {
  const win = cfg?.window || 0
  // 占用优先级：真实 usage → 视图估算（压缩后）→ 原文估算（没压缩过也有数）
  const used = usage?.last_prompt || meta.ctx?.est_after || meta.ctx?.est_original || 0
  const pct = win > 0 ? Math.min(1, used / win) : 0
  const short = meta.message_count >= 1000 ? `${(meta.message_count / 1000).toFixed(1)}k` : String(meta.message_count)
  const hit = usage && usage.prompt > 0 ? (usage.cached || 0) / usage.prompt : null
  const compactions = meta.ctx?.compactions ?? 0
  const R = 15.5                     // viewBox 半径；实际显示尺寸由 CSS 控制在 18px
  const C = 2 * Math.PI * R
  const color = pct >= 0.9 ? 'var(--err)' : pct >= 0.5 ? 'var(--warn)' : 'var(--acc)'
  const tip = [
    win ? `上下文 ${n0(used)} / 窗口 ${n0(win)}（${(pct * 100).toFixed(1)}%）` : `上下文 ${n0(used)}`,
    cfg?.threshold ? `压缩阈值 ${n0(cfg.threshold)}` : '',
    `压缩次数 ${compactions}`,
    hit === null ? '缓存命中：厂商未上报' : `缓存命中率 ${(hit * 100).toFixed(1)}%`,
    cfg && !cfg.enabled ? '上下文管理器当前关闭' : '',
    `${n0(meta.message_count)} 条消息`,
  ].filter(Boolean).join(' · ')
  return (
    <span className="sid-ring" title={tip}>
      <svg viewBox="0 0 36 36" width="18" height="18">
        <circle cx="18" cy="18" r={R} fill="none" stroke="rgba(255,255,255,.14)" strokeWidth="3" />
        <circle cx="18" cy="18" r={R} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round"
                strokeDasharray={`${(pct * C).toFixed(2)} ${C.toFixed(2)}`}
                transform="rotate(-90 18 18)" />
      </svg>
      <b>{short}</b>
    </span>
  )
}

export default function Sidebar({ onNewDone }: { onNewDone: () => void }) {
  const { state, dispatch } = useApp()
  const load = useLoadSession()
  const [creating, setCreating] = useState(false)
  const [confirmDel, setConfirmDel] = useState<string | null>(null)
  const [titleDraft, setTitleDraft] = useState('')
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameVal, setRenameVal] = useState('')
  const [q, setQ] = useState('')
  const [scope, setScope] = useState('all')
  const [status, setStatus] = useState('')
  const [res, setRes] = useState<SearchResp | null>(null)
  const [busy, setBusy] = useState(false)
  const seq = useRef(0)
  const searching = q.trim().length > 0

  useEffect(() => {                                 // 350ms 防抖：别每敲一键就扫一遍磁盘
    const mine = ++seq.current
    if (!q.trim()) { setRes(null); setBusy(false); return }
    setBusy(true)
    const t = window.setTimeout(async () => {
      try {
        const r = await api.searchSessions(q.trim(), scope, status)
        if (seq.current === mine) setRes(r)
      } catch (e) {
        if (seq.current === mine) dispatch({ type: 'toast', kind: 'err', msg: `搜索失败：${(e as Error).message}` })
      } finally { if (seq.current === mine) setBusy(false) }
    }, 350)
    return () => window.clearTimeout(t)
  }, [q, scope, status, dispatch])

  const createNew = async () => {
    const t = titleDraft.trim()
    setCreating(true)
    try {
      const r = await api.createSession(undefined, 'session', t || undefined)
      await load(r.session_id)
      setTitleDraft('')
      onNewDone()
      dispatch({ type: 'toast', kind: 'ok', msg: t ? `已新建会话「${t}」` : `已新建会话 ${r.session_id}（未命名，用首条消息作摘要）` })
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `新建失败：${(e as Error).message}` })
    } finally { setCreating(false) }
  }

  const rename = async (sid: string) => {
    try {
      const r = await api.renameSession(sid, renameVal.trim())
      dispatch({ type: 'toast', kind: 'ok', msg: r.title ? `已改名为「${r.title}」` : '已清除自定义标题' })
      setRenaming(null); onNewDone()
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `改名失败：${(e as Error).message}` })
    }
  }

  const remove = async (sid: string) => {
    try {
      const r = await api.deleteSession(sid)
      // 视图与事件流随原文一起进备份（血缘不分家）；有哪些没跟上要看得见
      const extra = (r.moved || []).filter((m) => m.kind !== 'session').length
      const warn = (r.warnings || []).length ? ` ⚠️${(r.warnings || []).length} 项未跟上` : ''
      dispatch({ type: 'toast', kind: r.warnings ? 'warn' : 'ok',
                 msg: `已移入备份 ${r.moved_to}${extra ? `（含视图 ${extra} 件）` : ''}${warn}` })
      if (state.current === sid) {
        dispatch({ type: 'set_current', sid: null })
        dispatch({ type: 'set_messages', messages: [], timeline: [] })
      }
      onNewDone()
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `删除失败：${(e as Error).message}` })
    } finally { setConfirmDel(null) }
  }

  return (
    <aside className="sidebar">
      <div className="side-head">
        <span>{searching ? `搜索 · ${res?.matched ?? 0}/${res?.scanned ?? '?'}` : `会话 · ${state.sessions.length}`}</span>
        <button className="btn small primary" disabled={creating || searching} onClick={() => void createNew()}>
          {creating ? '…' : '＋ 新建'}
        </button>
      </div>

      <div className="search-box">
        <div className="search-row">
          <input className="search-input" value={q} placeholder="🔍 搜标题 + 对话记录…"
                 onChange={(e) => setQ(e.target.value)} />
          {q ? <button className="btn tiny ghost" onClick={() => setQ('')} title="清空">✕</button> : null}
        </div>
        <div className="search-row filt">
          <select value={scope} onChange={(e) => setScope(e.target.value)}>
            <option value="all">标题+记录</option><option value="title">仅标题</option><option value="content">仅记录</option>
          </select>
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">全部状态</option><option value="active">🔄 进行中</option>
            <option value="complete">✅ 已完成</option><option value="interrupted">⚠️ 中断</option>
          </select>
          {busy ? <span className="search-busy">搜索中…</span> : null}
        </div>
        {!searching ? (
          <input className="title-input" value={titleDraft} maxLength={60}
                 placeholder="新会话标题（留空＝用首条消息作默认摘要）"
                 onChange={(e) => setTitleDraft(e.target.value)}
                 onKeyDown={(e) => { if (e.key === 'Enter') void createNew() }} />
        ) : null}
      </div>

      <div className="side-list">
        {searching ? (
          <>
            {res && res.matched === 0 && !busy ? <div className="empty">没有匹配「{q}」的会话。</div> : null}
            {(res?.matches || []).map((m) => (
              <div key={m.session_id} className="side-item sr-item" onClick={() => void load(m.session_id)}>
                <div className="side-item-top">
                  <span className="sid-title">{STATUS_ICON[m.status || 'unknown'] || '•'} {m.display || m.title || m.summary}</span>
                  <span className="sr-badge">{m.title_matched ? '标题命中' : `记录 ${m.hit_count} 处`}</span>
                </div>
                {m.hits.slice(0, 2).map((h, i) => (
                  <div key={i} className="sr-snip">
                    <em>{h.role === 'title' ? '标题' : h.role === 'user' ? '你' : h.role === 'assistant' ? 'AB' : h.role}</em> {h.snippet}
                  </div>
                ))}
                <div className="side-meta"><span className="sid-id">{m.session_id}</span></div>
              </div>
            ))}
          </>
        ) : (
          <>
            {state.sessions.length === 0 ? <div className="empty">暂无会话，填个标题点「新建」开始</div> : null}
            {state.sessions.map((s) => (
              <div key={s.session_id} className={`side-item ${state.current === s.session_id ? 'on' : ''}`}
                   onClick={() => { if (renaming !== s.session_id) void load(s.session_id) }}>
                <div className="side-item-top">
                  {renaming === s.session_id ? (
                    <span className="rename-box" onClick={(e) => e.stopPropagation()}>
                      <input autoFocus value={renameVal} maxLength={60} placeholder="新标题，留空＝清除"
                             onChange={(e) => setRenameVal(e.target.value)}
                             onKeyDown={(e) => { if (e.key === 'Enter') void rename(s.session_id); if (e.key === 'Escape') setRenaming(null) }} />
                      <button className="btn tiny primary" onClick={() => void rename(s.session_id)}>存</button>
                      <button className="btn tiny ghost" onClick={() => setRenaming(null)}>取</button>
                    </span>
                  ) : (
                    <>
                      {/* 标题 = 用户自定义标题 → 自动摘要（首条用户消息）；永不拿 session_id 当标题，
                          会话 ID 的查看路径是「对话上方」（ChatView 头部），卡片里只在圆圈悬停能看到 */}
                      <span className="sid-title">{STATUS_ICON[s.status] || '•'} {s.title || s.summary || '（空会话）'}</span>
                      {confirmDel === s.session_id ? (
                        <span className="del-confirm" onClick={(e) => e.stopPropagation()}>
                          <button className="btn tiny danger" onClick={() => void remove(s.session_id)}>确认删</button>
                          <button className="btn tiny ghost" onClick={() => setConfirmDel(null)}>取消</button>
                        </span>
                      ) : (
                        <span className="side-acts">
                          <button className="ren" title="重命名会话"
                                  onClick={(e) => { e.stopPropagation(); setRenaming(s.session_id); setRenameVal(s.title || '') }}>✎</button>
                          <button className="del" title="删除（移入 .trash 备份）"
                                  onClick={(e) => { e.stopPropagation(); setConfirmDel(s.session_id) }}>✕</button>
                        </span>
                      )}
                    </>
                  )}
                </div>
                {/* 摘要作副标题：只在有自定义标题时才有意义（否则标题行已经就是摘要，显示两遍是重复）。
                    卡片高度一致性交给 .side-item 的 min-height，不靠这行占位。 */}
                {s.title ? <div className="side-sum" title={s.summary}>{s.summary || '（空）'}</div> : null}
                {/* 会话 ID 独占一行（等宽小字）：信息给足，但不与 meta 行的圆圈/工具数/状态/日期抢宽度 */}
                <div className="side-sid" title={`会话 ID：${s.session_id}（与对话上方显示的一致，续聊时用它）`}>
                  {s.session_id}
                </div>
                <div className="side-meta">
                  <CtxRing meta={s} usage={state.usageBySid[s.session_id]} cfg={state.agent.hello?.ctx} />
                  {s.tool_calls ? <span>🔧{s.tool_calls}</span> : null}
                  <span className={s.status}>{s.status_label || s.status}</span>
                  {s.has_snapshot ? <span title="已冻结语境快照">🧊</span> : null}
                  <span className="when">{(s.last_activity || '').slice(5, 16)}</span>
                </div>
              </div>
            ))}
          </>
        )}
      </div>

      <div className="side-foot">
        <span>共 {state.sessions.reduce((a, b) => a + (b.message_count || 0), 0)} 条消息</span>
        <a href="/docs" target="_blank" rel="noreferrer">API 文档</a>
      </div>
    </aside>
  )
}
