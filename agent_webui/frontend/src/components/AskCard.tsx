// ask 提问卡：AB 可以一次问多个独立问题，但**一次只挂一张卡** ——
// 答完这张，下一张自动顶上来。（后端仍把这一批的答案一起收回，所以逐个答不损失并行收益。）
// 三种模式都允许「自己写答案」——choice 只给按钮会把想自由回答的主人逼到超时。
// multi 用「、」拼接；__CANCEL__ 前缀由 bridge 翻译成「停止收尾」指令。
import { useEffect, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store/appStore'
import type { AskRequest } from '../types'

const FALLBACK_WINDOW = 120

export default function AskCards() {
  const { state } = useApp()
  const list = state.clarifies
  if (!list.length) return null
  // 卡片以 ask_id 为 key：换下一张时组件重建，输入框与倒计时自然重置
  return <AskCard key={list[0].ask_id} c={list[0]} waiting={list.length - 1} />
}

function AskCard({ c, waiting }: { c: AskRequest; waiting: number }) {
  const { state, dispatch } = useApp()
  const [free, setFree] = useState('')
  const [multiSel, setMultiSel] = useState<string[]>([])
  const [sending, setSending] = useState(false)
  const [, forceTick] = useState(0)
  useEffect(() => {                       // 每秒重算一次倒计时（真值来自 store 的共享窗口）
    const t = window.setInterval(() => forceTick((x) => x + 1), 1000)
    return () => window.clearInterval(t)
  }, [])

  // 一批问题共享同一条 deadline：你每答一个，剩下的都会自动续期 → 所有卡显示同一剩余秒数
  const win = state.askWindow
  const left = win
    ? Math.max(0, Math.round(win.left - (Date.now() - win.at) / 1000))
    : Math.max(5, Math.round(Number(c.timeout ?? FALLBACK_WINDOW)))
  const expired = left <= 0
  const CANCEL = '__CANCEL__ 我取消了这个提问'

  const answer = async (text: string) => {
    if (!c.ask_id || sending) return
    setSending(true)
    try {
      const r = await api.askAnswer(c.ask_id, text)
      dispatch({ type: 'resolve_ask', ask_id: c.ask_id, remaining: r.remaining })
      if (expired) {
        dispatch({ type: 'toast', kind: 'warn',
                   msg: '已提交，但本轮很可能已被判超时：若 AB 没继续，请把答案直接发给我' })
      }
    } catch (e) {
      dispatch({ type: 'toast', kind: 'warn', msg: `提交失败：${(e as Error).message}` })
      dispatch({ type: 'resolve_ask', ask_id: c.ask_id })
    } finally { setSending(false) }
  }

  const toggle = (o: string) =>
    setMultiSel((prev) => (prev.includes(o) ? prev.filter((x) => x !== o) : [...prev, o]))

  // 勾选项与手写内容合并提交（两者都填就一起给，不互相丢弃）
  const merged = (parts: string[]) => [...parts, free.trim()].filter(Boolean).join('；')
  const canSend = free.trim().length > 0 || multiSel.length > 0

  return (
    <div className={`ask ${expired ? 'expired' : ''}`}>
      <div className="ask-head">
        <span>
          🙋 {waiting > 0 ? `还有 ${waiting} 个问题` : 'AB 需要你确认'}
          {' · '}{c.kind === 'multi' ? '多选' : c.kind === 'freeform' ? '自由回答' : '单选'}
        </span>
        <span className={`ask-count ${left <= 10 ? 'bad' : ''}`}>
          {expired ? '已超出窗口' : `${left}s`}
        </span>
      </div>
      {!!c.restored && (
        <div className="ask-q" style={{ opacity: 0.75, fontSize: 12 }}>
          🔄 刷新后找回的提问
        </div>
      )}
      {!!c.session_id && !!state.current && c.session_id !== state.current && (
        <div className="ask-foreign">
          ⚠️ 这个提问属于会话 <b>{c.session_id}</b> —— AB 正在那边等你回答，本会话只是旁观位
        </div>
      )}
      <div className="ask-q">{c.question}</div>

      {(c.options.length > 0 && c.kind !== 'freeform') && (
        <div className="ask-opts">
          {c.kind === 'multi' ? (
            c.options.map((o) => (
              <button key={o} className={`opt ${multiSel.includes(o) ? 'on' : ''}`} disabled={sending}
                      onClick={() => toggle(o)}>{multiSel.includes(o) ? '☑' : '☐'} {o}</button>
            ))
          ) : (
            c.options.map((o) => (
              <button key={o} className="opt" disabled={sending} onClick={() => void answer(o)} title="点击即以此答案提交">{o}</button>
            ))
          )}
        </div>
      )}

      <div className="ask-text">
        <input value={free} maxLength={400} disabled={sending}
               placeholder={c.options.length ? '或自己写一个答案…' : '输入你的回答…'}
               onChange={(e) => setFree(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter' && free.trim()) void answer(merged(c.kind === 'multi' ? multiSel : [])) }} />
      </div>

      <div className="ask-acts">
        <button className="btn primary small" disabled={!canSend || sending}
                onClick={() => void answer(merged(c.kind === 'multi' ? multiSel : []))}>
          {c.kind === 'multi' && multiSel.length ? `提交 ${multiSel.length} 项 + 文本` : '提交'}
        </button>
        <button className="btn ghost small" disabled={sending} onClick={() => void answer(CANCEL)}>取消</button>
      </div>

      {expired && (
        <div className="ask-tip">
          ⚠️ AB 那边已按「主人没答」继续跑了，此时提交可能不被接收 —— 建议直接把答案发给我。
        </div>
      )}
    </div>
  )
}
