// 审批卡（合并式 + 五结算方式）。三条硬规矩：
//  1. 回车不绑"允许"，Esc = 拒绝整批；防肌肉记忆放行高危操作。
//  2. critical（系统关键目录）默认不勾选，必须主动勾。
//  3. 超时不是"没回答"，是"拒绝"，文案照实写。
// 「批准」与「批准并返回AB」的区别是语义级的：前者纯放行（全勾才可用），
// 后者把主人的取舍回传给模型重规划（部分勾选才可用）—— 因为勾选本身就意味着拆分。
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store/appStore'
import type { ApprovalRequest } from '../types'

const FALLBACK = 300
const windowOf = (t?: number) => Math.max(5, Math.min(3600, t && t > 0 ? t : FALLBACK))
const BADGE = ['🟢', '🟡', '🟠', '🔴']
const NEEDS_CONFIRM = ['always', 'denyAll', 'denyStop']
const HKEY = 'approval.height'            // 与发送框一样可拖，且记住主人选的高度
const HMIN = 168, HMAX = 920, HDEF = 300
const clampH = (v: number) => Math.max(HMIN, Math.min(HMAX, v))

export default function ApprovalCard() {
  const { state, dispatch } = useApp()
  const cur: ApprovalRequest | undefined = state.approvals[0]
  const [on, setOn] = useState<Record<string, boolean>>({})
  const [openId, setOpenId] = useState<string | null>(null)
  const [left, setLeft] = useState(FALLBACK)
  const [busy, setBusy] = useState(false)
  const [armed, setArmed] = useState<string | null>(null)
  const [h, setH] = useState(() => {
    const v = Number(localStorage.getItem(HKEY) || 0)
    return v >= HMIN ? clampH(v) : HDEF
  })
  // 主人裁决时手打的话：只负责收集与提交，语义全归审批引擎
  const [note, setNote] = useState('')
  const drag = useRef<{ y: number; h0: number } | null>(null)
  useEffect(() => {
    const mv = (e: PointerEvent) => {
      if (!drag.current) return
      // 手柄在卡片顶部：向上拖（dy 为负）= 变高；顶部上限由消息区 min-height 兜住
      const nh = clampH(drag.current.h0 - (e.clientY - drag.current.y))
      setH(nh)
      localStorage.setItem(HKEY, String(nh))
    }
    const up = () => { drag.current = null }
    window.addEventListener('pointermove', mv)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', mv)
      window.removeEventListener('pointerup', up)
    }
  }, [])
  const startDrag = (e: React.PointerEvent) => {
    drag.current = { y: e.clientY, h0: h }
    e.preventDefault()
  }

  useEffect(() => {
    if (!cur) return
    const m: Record<string, boolean> = {}
    cur.items.forEach((it) => { m[it.ask_id] = !it.critical })
    setOn(m)
    setArmed(null)
    setOpenId(cur.items.length === 1 ? cur.items[0].ask_id : null)
    setNote('')                                            // 换卡必须清空，不能把上张卡的话带过去
    setLeft(windowOf(cur.timeout))
  }, [cur?.ask_id])                                        // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!cur) return
    const w = windowOf(cur.timeout)
    const born = cur.born || Date.now()
    const tick = () => setLeft(Math.max(0, w - Math.floor((Date.now() - born) / 1000)))
    tick()
    const t = window.setInterval(tick, 1000)
    return () => window.clearInterval(t)
  }, [cur])

  const allIds = cur ? cur.items.map((i) => i.ask_id) : []
  const chosen = useMemo(
    () => (cur ? cur.items.filter((i) => on[i.ask_id]).map((i) => i.ask_id) : []),
    [cur, on])
  const allChecked = allIds.length > 0 && chosen.length === allIds.length

  useEffect(() => {
    if (!cur) return
    const onKey = (e: KeyboardEvent) => {
      // 打字时绝不裁决：焦点在输入控件里，数字键与 Esc 属于内容，不属于按钮。
      // 没有这一行，主人在补充说明里打「ProgramData 1」就会当场把整批改掉。
      const el = e.target as HTMLElement | null
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                 || el.isContentEditable)) return
      if (e.key === 'Escape') { e.preventDefault(); act('denyAll'); return }
      // 键盘兜底：内容再多、按钮被挤到任何位置，都能用 1~5 直接裁决
      const KEYMAP: Record<string, string> = { 1: 'always', 2: 'approve', 3: 'return',
        4: 'denyAll', 5: 'denyStop' }
      const m = KEYMAP[e.key]
      if (m) { e.preventDefault(); act(m) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  if (!cur) return null
  const expired = left <= 0
  const maxRisk = cur.items.reduce((a, b) => Math.max(a, b.risk), 0)

  async function send(mode: string) {
    if (!cur || busy) return
    setBusy(true)
    // 三种作用域就够表达全部语义：approved + scope + stop，引擎与通道协议不变
    const approved = mode === 'approve' ? allIds
      : (mode === 'return' || mode === 'always') ? chosen : []
    const scope = mode === 'always' ? 'persistent' : 'once'
    const stop = mode === 'denyStop'
    try {
      await api.approvalAnswer(cur.ask_id, approved, scope, stop, note.trim())
      dispatch({ type: 'resolve_approval', ask_id: cur.ask_id })
      const txt = mode === 'approve' ? '已全部批准，本批直接送执行（不把裁决细节回传 AB）'
        : mode === 'return' ? `已批准 ${chosen.length}/${allIds.length} 项，并把主人的取舍交回 AB 重新规划`
          : mode === 'always' ? `已永久允许这 ${chosen.length} 项所在路径（写入规则文件）`
            : stop ? '已整批拒绝并中止本回合' : '已整批拒绝，各条原因已回传 AB'
      dispatch({ type: 'toast', kind: approved.length ? 'ok' : 'warn',
                 msg: expired ? txt + ' —— 但窗口已过，期间系统按拒绝处理过' : txt })
    } catch (e) {
      dispatch({ type: 'resolve_approval', ask_id: cur.ask_id })
      dispatch({ type: 'toast', kind: 'err', msg: '裁决未被采纳：' + (e as Error).message })
    } finally {
      setBusy(false)
      setArmed(null)
    }
  }
  // 可用性判定必须与按钮 disabled 同源：否则键盘 1~5 会绕过限制，
  // 等于留一条「没全勾也能按批准」的后门——那正好废掉批次原子性的意义。
  const usable = (mode: string) =>
    mode === 'approve' ? allChecked
      : mode === 'return' || mode === 'always' ? chosen.length > 0
      : true

  function act(mode: string) {
    if (!cur || !usable(mode)) return
    if (NEEDS_CONFIRM.indexOf(mode) >= 0 && armed !== mode) { setArmed(mode); return }
    void send(mode)
  }

  return (
    <div className={`approval r${Math.min(3, maxRisk)} ${expired ? 'expired' : ''}`}
         style={{ height: `${h}px` }}>
      <div className="approval-grip" onPointerDown={startDrag}
           title="按住上下拖动可调整审批卡高度（自动记住）" />
      <div className="approval-inner">
      <div className="approval-head">
        <span className="approval-title">
          {BADGE[Math.min(3, maxRisk)]} 安全审计 ·{' '}
          {cur.items.length > 1 ? `本批 ${cur.items.length} 项待授权操作` : '一项待授权操作'}
        </span>
        <span className={`approval-count ${left <= 20 ? 'bad' : ''}`}>
          {expired ? '已超时=拒绝' : `剩 ${left}s`}
        </span>
      </div>

      <div className="approval-batchnote">
        批次原子：勾选=批准、未勾=拒绝；只要有一项没批准，本批一个工具都不会运行
      </div>

      {!!cur.session_id && !!state.current && cur.session_id !== state.current && (
        <div className="approval-foreign">
          ⚠️ 这张卡属于会话 <b>{cur.session_id}</b> —— AB 正在那边等着裁决，本会话只是旁观位
        </div>
      )}

      {cur.items.length > 1 && (
        <div className="approval-scrolldots">
          ⤓ 列表可滚动（共 {cur.items.length} 项），裁决按钮固定在底部；也可按
          1=始终批准 2=批准 3=批准并返回AB 4=全部拒绝 5=终止 操作
        </div>
      )}
      <div className="approval-list">
        {cur.items.map((it, idx) => (
          <div key={it.ask_id} className={`approval-item ${it.critical ? 'crit' : ''}`}>
            <label className="approval-check">
              <input type="checkbox" disabled={busy || expired} checked={!!on[it.ask_id]}
                     onChange={(e) => setOn((m) => ({ ...m, [it.ask_id]: e.target.checked }))} />
              <span className="approval-idx">{idx + 1}</span>
            </label>
            <div className="approval-body">
              <div className="approval-intent">
                <span className="approval-k">AB 想要</span>
                <span className="approval-v">{it.intent || '改动系统盘文件'}</span>
                {it.critical && <span className="approval-crit">关键目录</span>}
              </div>
              {!!it.reason && (
                <div className="approval-reason">
                  <span className="approval-k">依据</span>
                  <span className="approval-v">{it.reason}</span>
                </div>
              )}
              {!!(it.notes && it.notes.length) && (
                <ul className="approval-lines">{it.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
              )}
              {!!(it.paths && it.paths.length) && (
                <div className="approval-paths">
                  {it.paths.slice(0, 2).map((pth, i) => <code key={i}>{pth}</code>)}
                  {it.paths.length > 2 && (
                    <code className="approval-more">+{it.paths.length - 2} 项</code>
                  )}
                </div>
              )}
              {!!it.source_code && (
                <div className="approval-src">
                  <button className="approval-toggle" disabled={busy}
                          onClick={() => setOpenId((v) => (v === it.ask_id ? null : it.ask_id))}>
                    {openId === it.ask_id ? '▾ 收起' : '▸ 展开'}操作源代码
                  </button>
                  {openId === it.ask_id && <pre>{it.source_code}</pre>}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {!!cur.accepts_note && (
        <div className="approval-note">
          <textarea rows={2} value={note} maxLength={400} disabled={busy}
                    placeholder={cur.note_hint || '可补充说明（原样送回 AB 与账本）'}
                    onChange={(e) => setNote(e.target.value)} />
          <span className="approval-note-n">
            {note.trim() ? `原样送回：${note.trim().length} 字` : '可不填'}
          </span>
        </div>
      )}
      <div className="approval-acts">
        <button className="btn ghost small" disabled={busy} onClick={() => act('always')}
                title="把勾选这些路径写入永久规则（跨会话生效）">
          {armed === 'always' ? `⚪ 再点一次确认永久允许（${chosen.length}）` : `⚪ 始终批准（${chosen.length}）`}
        </button>
        <button className="btn primary small" disabled={busy || !allChecked}
                onClick={() => act('approve')}
                title="全部勾选后可用：纯放行，不把裁决细节回传给 AB">
          🟢 批准{allChecked ? `（${allIds.length}）` : '（需全部勾选）'}
        </button>
        <button className="btn primary small" disabled={busy || !chosen.length || allChecked}
                onClick={() => act('return')}
                title="部分勾选时可用：本批不执行，但把哪些允许、哪些拒绝回传 AB 重新规划">
          🟠 批准并返回AB（{chosen.length}/{allIds.length}）
        </button>
        <button className="btn ghost small danger" disabled={busy} onClick={() => act('denyAll')}>
          {armed === 'denyAll' ? '🔴 再点一次确认全部拒绝' : '🔴 全部拒绝'}
        </button>
        <button className="btn ghost small danger" disabled={busy} onClick={() => act('denyStop')}>
          {armed === 'denyStop' ? '⛔ 再点一次确认并终止回合' : '⛔ 全部拒绝并终止'}
        </button>
      </div>

      <div className="approval-tip">
        {expired
          ? '⛔ 窗口已过：系统已按拒绝往下走了，此卡提交不再被采纳；要放行请让 AB 重新发起。'
          : 'Esc 或 4 = 拒绝整批；1/2/3/5 对应其余四个动作；超时同样按拒绝处理，不会自动放行。' +
          ' ⚪🔴⛔ 需按两次确认。'}
        {' '}<span className="approval-scope">批准=纯放行 · 批准并返回AB=把取舍交还模型重规划</span>
      </div>
      </div>
    </div>
  )
}
