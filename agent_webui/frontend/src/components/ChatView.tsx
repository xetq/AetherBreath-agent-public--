import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store/appStore'
import { useLoadSession } from '../lib/useLoadSession'
import { debounce, loadText, saveText } from '../lib/persist'
import type { Attachment, AttachmentLimits } from '../types'
import MessageList from './MessageList'
import AskCard from './AskCard'
import ApprovalCard from './ApprovalCard'
import ModeSelect from './ModeSelect'
import TurnBadge from './StatusBadge'

const draftKey = (sid: string | null) => `draft.${sid || '_none'}`

function fmtSize(n: number): string {
  if (!n || n < 0) return '?'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

export default function ChatView() {
  const { state, dispatch } = useApp()
  const load = useLoadSession()
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  // 附件：已选中（已上传落盘）的一批。发送/交代时把它们的 path 一起递上去。
  // 二进制内容不留在前端状态里 —— 上传完只留元数据，避免大对象挂在 React 树上。
  const [atts, setAtts] = useState<Attachment[]>([])
  const [dragging, setDragging] = useState(false)
  const [limits, setLimits] = useState<AttachmentLimits | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    api.attachmentLimits().then(setLimits).catch(() => { /* 拿不到就用本地默认值 */ })
  }, [])

  /** 读文件 → base64 → 上传落盘 → 进附件列表。逐个体检大小与数量上限。 */
  const uploadFiles = async (files: File[]) => {
    const list = (files || []).filter(Boolean)
    if (!list.length) return
    const cap = limits?.max_per_message ?? 10
    const maxMb = limits?.max_file_mb ?? 20
    const room = Math.max(0, cap - atts.length)
    if (room <= 0) {
      dispatch({ type: 'toast', kind: 'warn', msg: `附件已达上限（${cap} 个）` })
      return
    }
    if (list.length > room) {
      dispatch({ type: 'toast', kind: 'warn', msg: `超出上限，本次只收前 ${room} 个` })
    }
    for (const f of list.slice(0, room)) {
      if (f.size > maxMb * 1048576) {
        dispatch({ type: 'toast', kind: 'err', msg: `「${f.name}」${fmtSize(f.size)} 超过上限 ${maxMb} MB，已跳过` })
        continue
      }
      try {
        const b64 = await new Promise<string>((res, rej) => {
          const rd = new FileReader()
          rd.onload = () => res(String(rd.result || ''))
          rd.onerror = () => rej(new Error('本地读取失败'))
          rd.readAsDataURL(f)
        })
        const r = await api.uploadAttachment(state.current || '', f.name, b64)
        setAtts((prev) => [...prev, r.attachment])
      } catch (e) {
        dispatch({ type: 'toast', kind: 'err', msg: `「${f.name}」上传失败：${(e as Error).message}` })
      }
    }
  }
  // 草稿：400ms 防抖落盘（刷新/误关页面回来还在），且不会每敲一键写一次 localStorage。
  // 单一真相源 draftRef —— 切会话时先立即落盘上一份，避免防抖尾巴写错槽位。
  const draftRef = useRef<{ sid: string | null; text: string }>({ sid: null, text: '' })
  const saveDraft = useMemo(() => debounce(() => {
    saveText(draftKey(draftRef.current.sid), draftRef.current.text)
  }, 400), [])
  useEffect(() => {
    saveText(draftKey(draftRef.current.sid), draftRef.current.text)      // 先把手上这份结清
    const mine = loadText(draftKey(state.current)) || ''
    draftRef.current = { sid: state.current, text: mine }
    setInput(mine)
  }, [state.current])

  const on = state.agent.phase === 'ON'
  const busy = state.agent.busy
  const canSend = on && !busy && !state.clarifies.length
  // 中期交互（「用户交代」）只在一种情形下可用：**AB 正在跑的正是本会话**。
  // 它在跑别的会话时这里没有可投递的目标 —— 按钮保持红色「停止本回合」，
  // 回车也不做任何事：绝不把话投进别人的语境，更不误触停止。
  const busyHere = on && busy && state.turnOwner === state.current
  const attPaths = atts.map((a) => a.path)
  const hasAny = !!input.trim() || attPaths.length > 0
  const midMode = busyHere && hasAny

  // 首屏：优先接上"上次停留的会话"（刷新延续），否则退回最近一个
  useEffect(() => {
    if (state.current || state.sessions.length === 0) return
    const saved = loadText('current')
    const hit = !!saved && state.sessions.some((x) => x.session_id === saved)
    void load(hit ? (saved as string) : state.sessions[0].session_id)
  }, [state.current, state.sessions, load])

  // 后台作业**自动交付**是"落盘的消息"，实时通道里没有它 —— 回合结束时 bridge 会在 done 里
  // 告诉我们刚交付了谁（jobDeliveredAt），这里就以磁盘为准重放一次历史，让那条交付
  // **当场**出现在用户卡里（与「用户交代」同一个位置、不同样式）。没有交付则什么都不做。
  // 注：权限模式**不在这里**重放 —— 它已经改成实时态（store.permissionMode 驱动那块
  // 就地更新的状态块），不再往历史里写通知，也就不存在"以磁盘为准补一条"的问题。
  useEffect(() => {
    if (!state.jobDeliveredAt || !state.current) return
    void load(state.current)
  }, [state.jobDeliveredAt, state.current, load])

  const send = async (e?: FormEvent) => {
    e?.preventDefault()
    const text = input.trim()
    const paths = atts.map((a) => a.path)
    if ((!text && paths.length === 0) || !canSend) return
    let sid = state.current
    if (!sid) {
      try {
        const r = await api.createSession(undefined, 'session')
        sid = r.session_id
        await load(sid)
      } catch (err) {
        dispatch({ type: 'toast', kind: 'err', msg: `新建会话失败：${(err as Error).message}` })
        return
      }
    }
    setSending(true)
    dispatch({ type: 'append_user', text, atts: atts.map((a) => ({ name: a.name, path: a.path })) })
    setInput('')
    setAtts([])
    draftRef.current = { sid, text: '' }
    saveText(draftKey(sid), '')        // 已发出，草稿清掉
    try {
      await api.chat(sid, text, paths)
    } catch (err) {
      dispatch({ type: 'toast', kind: 'err', msg: `发送失败：${(err as Error).message}` })
    } finally {
      setSending(false)
      setTimeout(() => taRef.current?.focus(), 50)
    }
  }

  // 中期交互：把输入框里的字作为「用户交代」投递给正在跑的这个回合。
  // 它不起新回合、不打断工具执行 —— AB 会在下一批工具返回时收到。
  const sendMid = async () => {
    const text = input.trim()
    const sid = state.current
    const paths = atts.map((a) => a.path)
    if ((!text && paths.length === 0) || !sid || !midMode || sending) return
    setSending(true)
    try {
      const r = await api.midTurn(sid, text, state.agent.run_id, paths)
      // 先拿到投递回执再清输入框：失败时字还留着，主人重发即可（不丢话）
      setInput('')
      setAtts([])
      draftRef.current = { sid, text: '' }
      saveText(draftKey(sid), '')
      dispatch({ type: 'append_mid_turn', text, itemId: r.item_id || '',
                 atts: atts.map((a) => ({ name: a.name, path: a.path })) })
    } catch (err) {
      dispatch({ type: 'toast', kind: 'err', msg: `「用户交代」未送达：${(err as Error).message}` })
    } finally {
      setSending(false)
      setTimeout(() => taRef.current?.focus(), 50)
    }
  }

  const stop = async () => {
    try {
      const r = await api.stopChat(state.agent.run_id)
      // stopped=false 有两种完全不同的含义，必须分开说，别混成一句：
      //   ① 有 note  → 中断**已经注入**，只是回合线程此刻阻塞在原生调用（subprocess/recv/sleep），
      //                异常要等它回到下一个字节码边界才生效 —— 这是「已请求停止」，不是「没停成」
      //   ② 没 note  → 压根没有活动回合（前端状态过期），立刻以网关为准校正
      // 两种情况都要说清：**已经在跑的工具线程不会被中断**（它跑在编排器的 worker 里），
      // 会自行跑完 —— 所以界面只说「已请求停止」，不说「已停止」。
      if (r.ok !== false && r.stopped === false) {
        if (r.note) {
          dispatch({ type: 'toast', kind: 'warn', msg: '已请求停止：回合线程正阻塞在原生调用中，收尾会稍晚发生（已在跑的工具会自行跑完）' })
        } else {
          try { dispatch({ type: 'set_agent', agent: await api.agentStatus() }) } catch { /* 忽略 */ }
          dispatch({ type: 'toast', kind: 'info', msg: r.reason === 'no-active-run' ? '当前没有活动回合，无需中断（界面状态已校正）' : `未执行中断：${r.reason || '原因未知'}` })
        }
      } else {
        dispatch({ type: 'toast', kind: 'warn', msg: '已请求停止：回合将在下一个边界收尾（已在跑的工具会自行跑完）' })
      }
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `中断失败：${(e as Error).message}` })
    }
  }

  return (
    <main className="chat">
      <div className="chat-head">
        <div className="chat-title">
          <b>{state.current || '未选择会话'}</b>
          {state.agent.session_id && state.agent.session_id !== state.current && (
            <span className="hint" title="AB 进程当前正在处理的会话">AB 正在跑：{state.agent.session_id}</span>
          )}
        </div>
        <div className="chat-state">
          <TurnBadge phase={state.agent.turn_phase} />
          {state.agent.pid ? <span className="mini">pid {state.agent.pid}</span> : null}
          {state.agent.run_id && busy ? <span className="mini">run {state.agent.run_id}</span> : null}
        </div>
      </div>

      {state.agent.busy && state.turnOwner && state.turnOwner !== state.current ? (
        <div className="elsewhere">
          <span>⏳ 会话 <b>{state.turnOwner}</b> 的回合正在执行，本会话只是旁观位</span>
          <button className="btn tiny ghost" onClick={() => void load(state.turnOwner as string)}>切过去看</button>
        </div>
      ) : null}

      <MessageList />
      <ApprovalCard />
      <AskCard />

      <form className={`composer${dragging ? ' dragging' : ''}`} onSubmit={send}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault(); setDragging(false)
              void uploadFiles(Array.from(e.dataTransfer.files || []))
            }}>
        {atts.length > 0 && (
          <div className="att-chips">
            {atts.map((a) => (
              <span key={a.id} className="att-chip" title={a.path}>
                <span className="att-tag">附件</span>
                <span className="att-name">{a.name}</span>
                <span className="att-size">{fmtSize(a.size)}</span>
                <button type="button" className="att-x" title="取消选择"
                        onClick={() => setAtts((prev) => prev.filter((x) => x.id !== a.id))}>×</button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          value={input}
          rows={input.split('\n').length > 6 ? 8 : Math.max(2, input.split('\n').length)}
          placeholder={!on ? 'AB 未开机：先点右上角「🟢 开机」'
            : busyHere ? '回合进行中：输入后点紫色「发送」= 用户交代（AB 在下一批工具返回时收到，不打断本回合）'
            : busy ? `AB 正在跑会话 ${state.turnOwner || '?'}，本会话只能旁观`
            : '消息…（Enter 发送 / Shift+Enter 换行）'}
          disabled={!on}
          onPaste={(e) => {
            /* Ctrl+V 粘图/粘文件：剪贴板里带文件才算，纯文本仍然走正常粘贴 */
            const files = Array.from(e.clipboardData?.files || [])
            if (files.length) { e.preventDefault(); void uploadFiles(files) }
          }}
          onChange={(e) => {
            const v = e.target.value
            setInput(v); draftRef.current = { sid: state.current, text: v }; saveDraft()
          }}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || e.shiftKey) return
            e.preventDefault()
            // 回合运行中：有内容 → 发交代；输入框是空的 → 什么都不做。
            // 这一条是刻意的：空回车绝不能穿透到下面的「⏹ 停止本回合」上去。
            // AB 在跑别的会话时同理什么都不做 —— 本会话没有可投递的目标。
            if (busyHere) { void sendMid(); return }
            if (!busy) { void send() }
          }}
        />
        <div className="composer-acts">
          {/* 上传按钮与「发送」同在右列，垂直排列 —— 它正好落在发送按钮上方（主人指定的位置）。
              放进 composer-acts 还有一层原因：那一列本来就是 column 布局，
              不会再跟 textarea 抢横向宽度（上一版把它当独立 flex item，实测把输入框挤成一条）。 */}
          <input ref={fileRef} type="file" multiple style={{ display: 'none' }}
                 onChange={(e) => {
                   void uploadFiles(Array.from(e.target.files || []))
                   e.target.value = ''      /* 清空 value，允许再次选同一个文件 */
                 }} />
          <button type="button" className="btn icon att-upload"
                  title="上传附件（点此选文件；也可拖拽到输入栏、或 Ctrl+V 粘贴）"
                  onClick={() => fileRef.current?.click()}
                  disabled={!on}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          {/* 权限模式与「发送」同排：**权限在左、发送在右**（主人 2026-10 指定的位置）。
              这一排本身还是一列里的第二个元素，所以上传按钮仍稳稳落在它上方。 */}
          <div className="composer-send-row">
            <ModeSelect />
            {busy ? (
              midMode ? (
                /* 紫色 = 中期交互：这一步不打断回合，只是把话塞进模型的下一步动作里。
                   输入框空着时它就退回红色「停止本回合」—— 停止是危险动作，不该常驻在
                   「我刚打完字要发送」的那个位置上。 */
                <button type="button" className="btn mid" disabled={sending}
                        onClick={() => void sendMid()}
                        title="作为「用户交代」送出：不打断当前回合，AB 在下一批工具返回时收到">
                  ⤴ 发送（用户交代）
                </button>
              ) : (
                <button type="button" className="btn danger" onClick={() => void stop()}>⏹ 停止本回合</button>
              )
            ) : (
              <button type="submit" className="btn primary" disabled={!canSend || !hasAny || sending}>
                {on ? '发送 ↵' : '需先开机'}
              </button>
            )}
          </div>
        </div>
      </form>
    </main>
  )
}
