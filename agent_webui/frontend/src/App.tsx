import { useCallback, useEffect, useRef, useState } from 'react'
import { loadText, saveText, usageSeq } from './lib/persist'
import { api, openEvents } from './api'
import { useApp } from './store/appStore'
import { PANELS } from './panels/registry'
import PowerControl from './components/PowerControl'
import Sidebar from './components/Sidebar'
import ChatView from './components/ChatView'
import Toasts from './components/Toasts'
import type { WsEvent } from './types'

export default function App() {
  const { state, dispatch } = useApp()
  const [panel, setPanel] = useState<string>(PANELS[0].id)
  const [panelOpen, setPanelOpen] = useState(true)
  // [Fix] hub_seq 持久化：刷新后从断点续订，不再 since=0 全量回放 800 条历史
  // （全量回放会把旧会话的 interrupted/error 事件灌进 reducer，造成"回合已中断"类误报，
  // 也让浏览器白白多吞一整环事件）。网关重启后 seq 归零：since 偏大只会回放为空，安全。
  const seqRef = useRef<number>(Number(loadText('hub_seq')) || 0)
  const seqSaveAt = useRef(0)
  const lastEvtAt = useRef(Date.now())
  const [sseKey, setSseKey] = useState(0)
  // 概况的"代"判定：网关 SSEHub 每代从 hub_seq=0 重计（sse.py），
  // 所以收到比本地概况快照更早的帧 = 网关换过代，本地桶属于上一代，作废。
  const usageBase = useRef(usageSeq())

  // 以网关为准校正一次进程/回合状态（set_agent 里会顺带重算 turnOwner 归属）
  const syncStatus = useCallback(async () => {
    try {
      const st = await api.agentStatus()
      lastEvtAt.current = Date.now()
      dispatch({ type: 'set_agent', agent: st })
    } catch { /* 网关不可达时交给 SSE 重连逻辑，不打扰用户 */ }
  }, [dispatch])

  // 找回卡片：刷新 / 切会话 / 重开页面后，向服务端要一次「当前真挂着的」审批与提问。
  // 根因是卡片只活在前端内存里，而后端的 Event 还在等 —— 卡片一消失就没人能回投，
  // 整批工具等到超时、循环当场卡死（主人看到的现象是「工具全部返回空」）。
  //
  // 三条硬规矩，每条都是拿真机 bug 换来的：
  //  1) 两条通道分别成败，不用 Promise.all —— 一个失败会连另一个一起丢。
  //  2) 只有 authoritative === true（服务端确实回了权威快照）才允许剪枝。
  //     bridge 没开机时后端回的是"ok + 空列表"，拿它当"服务端说没有卡"去剪枝，
  //     会把屏幕上真挂着的卡删掉 —— 正是要修的那类 bug 的镜像。
  //  3) 失败必须留痕。这条通道上一版是用 post() 打 GET 端点，每个请求都吃 405，
  //     而调用点的 catch 一声不吭：四层链路全废却"验收通过"。静默即失控。
  const syncOkOnce = useRef(false)
  const syncErrAt = useRef<Record<string, number>>({})
  const syncPending = useCallback(async () => {
    const before = Date.now()
    const settled = await Promise.allSettled([api.pendingApprovals(), api.pendingAsk()])
    const cards: WsEvent[] = []
    const failed: string[] = []
    const authoritative = [false, false]
    settled.forEach((r, i) => {
      const what = i === 0 ? '审批' : 'ask_user 提问'
      if (r.status === 'rejected') {
        failed.push(`${what}：${(r.reason as Error)?.message || String(r.reason)}`)
        return
      }
      const val = r.value as { cards?: WsEvent[]; ok?: boolean; authoritative?: boolean
                               error?: string } | null
      if (!val || val.authoritative !== true) {
        failed.push(`${what}：${val?.error || '服务端未给出权威快照'}`)
        return
      }
      authoritative[i] = true
      syncOkOnce.current = true
      cards.push(...(val.cards || []))
    })
    for (const card of cards) dispatch({ type: 'event', evt: card })
    dispatch({
      type: 'prune_gates',
      ids: cards.map((c) => String((c as { ask_id?: string }).ask_id
        || (c as { batch_id?: string }).batch_id || '')).filter(Boolean),
      before, approvals: authoritative[0], ask: authoritative[1],
    })
    for (const msg of failed) {
      // eslint-disable-next-line no-console
      console.warn('[卡片恢复] ' + msg)
      // 通道曾经能用、现在不能 —— 这才是"坏了"。开机前拉不到属正常静默期，
      // 只进控制台不弹窗，免得每次打页面都吓人一跳。
      if (!syncOkOnce.current) continue
      const key = msg.slice(0, 60)
      if (Date.now() - (syncErrAt.current[key] || 0) < 60000) continue
      syncErrAt.current[key] = Date.now()
      dispatch({ type: 'toast', kind: 'warn', msg: `卡片恢复未能核对：${msg}` })
    }
  }, [dispatch])

  // 编排器占用对表：跨回合作业占着哪些槽位，服务端才是权威（`/api/orch`）。
  // 为什么要它：`state.pipes` 只活在浏览器内存里 —— 刷新、重连、丢帧之后，一个跑了
  // 十秒的后台作业在界面上会**一格都不亮**（退回"推导"模式），那就又成了黑箱。
  // 纪律与卡片恢复同源：只有 authoritative === true 的快照才允许改动本地状态。
  const syncOrch = useCallback(async () => {
    try {
      const r = await api.orch()
      dispatch({ type: 'merge_orch', pipes: r.pipes || [],
                 authoritative: r.authoritative === true })
    } catch { /* 对表失败不打扰用户：橙点由事件驱动，下一次成功对表会纠正 */ }
  }, [dispatch])

  // 记住当前会话：刷新后能回到原处（配合每会话草稿 = 延续输入状态）
  useEffect(() => { saveText('current', state.current ?? '') }, [state.current])

  // 切会话后再核对一次：卡片的真相在服务端。set_current 已不再删卡（那是"切回来
  // 就消失"的直接原因），这一条补的是另一头 —— 服务端早已结束、屏幕上却还留着的
  // 僵尸卡（bridge 换代、超时、回合终止）。拉不到时不剪枝，见 syncPending 的注释。
  const prevCur = useRef<string | null>(null)
  useEffect(() => {
    if (prevCur.current === state.current) return
    prevCur.current = state.current
    if (state.current) void syncPending()
  }, [state.current, syncPending])

  const refreshSessions = useCallback(async () => {
    try {
      const r = await api.sessions()
      dispatch({ type: 'set_sessions', sessions: r.sessions })
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `会话列表加载失败：${(e as Error).message}` })
    }
  }, [dispatch])

  // 初次装载：状态 + 会话列表
  useEffect(() => {
    let alive = true
    api.agentStatus()
      .then((s) => { if (alive) dispatch({ type: 'set_agent', agent: s }) })
      .catch((e) => { if (alive) dispatch({ type: 'toast', kind: 'err', msg: `网关状态获取失败：${(e as Error).message}` }) })
    void refreshSessions()
    void syncPending()          // 刷新/重开页面后第一件事就把挂着的卡找回来
    void syncOrch()             // 同一件事：把正在跑的跨回合作业占的槽位找回来
    return () => { alive = false }
  }, [dispatch, refreshSessions, syncPending, syncOrch])

  // SSE：断线后带 since 重连，历史回放补齐，刷新不丢时间线
  useEffect(() => {
    const onEvt = (e: WsEvent) => {
      if (usageBase.current > 0 && typeof e.hub_seq === 'number' && e.hub_seq < usageBase.current) {
        usageBase.current = 0
        // 注意：这里**不再作废计量桶**（主人 2026-09-13：概况/占比要任何情况下常驻）。
        // bridge 已把账本落盘，新代载入后接着累计；前端保留本地桶即可无缝显示。
        seqRef.current = e.hub_seq
        saveText('hub_seq', String(e.hub_seq))
        dispatch({ type: 'reset_usage', seq: e.hub_seq })
        window.setTimeout(() => setSseKey((k) => k + 1), 0)   // 重连：since 已打回本代，不再漏帧
        return
      }
      if (typeof e.hub_seq === 'number' && e.hub_seq > seqRef.current) seqRef.current = e.hub_seq
      lastEvtAt.current = Date.now()
      // 节流落盘：首开的 snapshot 帧（回放结束的边界）必存，直播期最多 1s 存一次
      const t = Date.now()
      if ((e.type === 'agent_phase' && e.reason === 'snapshot') || t - seqSaveAt.current > 1000) {
        seqSaveAt.current = t
        saveText('hub_seq', String(seqRef.current))
      }
      dispatch({ type: 'event', evt: e })
    }
    const close = openEvents(seqRef.current, onEvt, (s) => {
      dispatch({ type: 'set_sse', sse: s })
      if (s === 'error' || s === 'closed') {
        window.setTimeout(() => setSseKey((k) => k + 1), 2000)
      } else if (s === 'open') {
        void syncStatus()                 // 重连成功先对一次表，补上断线期间丢掉的那帧 done
        void syncPending()                // 再找回可能已经挂着的审批/提问卡片
        void syncOrch()                   // 以及可能还在跑的跨回合作业（橙点不能只靠内存）
      }
    }, state.current)
    return () => close()
  }, [sseKey, state.current, dispatch, syncStatus, syncPending, syncOrch])

  // busy 看门狗：busy 只靠 SSE 的 done 清零，丢一帧就永久卡在「回合进行中」
  // （表现：点停止后端说没有可终止的回合，界面却仍显示忙）。
  // 2026-09-29 修：原来要"静默 20 秒"才核对 —— 而**后台作业**在回合结束后仍会发事件，
  // 且主人往往几秒内就发现不对就动手了，20 秒的窗口根本等不到。
  // 改成：**只要 busy 就每 10 秒对一次表**（服务端是权威），最多 10 秒自愈。
  useEffect(() => {
    if (!state.agent.busy) return
    const t = window.setInterval(() => { void syncStatus() }, 10000)
    return () => window.clearInterval(t)
  }, [state.agent.busy, syncStatus])

  // 跨回合作业对表：橙点靠 SSE 事件点亮，但事件会晚、会丢。只要屏幕上还有"作业在跑"
  // 的槽位，就每 5 秒跟服务端对一次表 —— 作业真结束了立刻收灯，绝不让它亮着骗人。
  // 反向也守着：merge_orch 只认 authoritative，一次失败的对表不会把真在跑的作业灭掉。
  const hasLiveJob = Object.values(state.pipes).some(
    (p) => p.background && (p.status === 'running' || p.status === 'pending'))
  useEffect(() => {
    if (!hasLiveJob) return
    void syncOrch()
    const t = window.setInterval(() => { void syncOrch() }, 5000)
    return () => window.clearInterval(t)
  }, [hasLiveJob, syncOrch])

  // 回合结束 -> 刷新会话列表（消息数/摘要变了）
  const turn = state.agent.turn_phase
  useEffect(() => {
    if (turn === 'IDLE') void refreshSessions()
  }, [turn, refreshSessions])

  const ActivePanel = PANELS.find((p) => p.id === panel) ?? PANELS[0]

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">☲</span>
          <div className="brand-text">
            <b>AetherBreath</b>
            <span>WebUI · 前端 Vite+React ／ 网关 FastAPI ／ AB 子进程</span>
          </div>
        </div>
        <div className="topbar-right">
          <SseDot sse={state.sse} />
          <PowerControl />
        </div>
      </header>

      <div className="body">
        <Sidebar onNewDone={() => void refreshSessions()} />
        <ChatView />
        <aside className={`rpanel ${panelOpen ? '' : 'closed'}`}>
          <nav className="rpanel-tabs">
            {PANELS.map((p) => (
              <button key={p.id} className={p.id === panel ? 'on' : ''} onClick={() => { setPanel(p.id); setPanelOpen(true) }}>
                {p.icon} {p.title}
              </button>
            ))}
            <button className="fold" onClick={() => setPanelOpen((v) => !v)} title="折叠/展开面板">
              {panelOpen ? '⟩' : '⟨'}
            </button>
          </nav>
          {panelOpen && <ActivePanel.component />}
        </aside>
      </div>
      <Toasts />
    </div>
  )
}

function SseDot({ sse }: { sse: 'connecting' | 'open' | 'error' | 'closed' }) {
  const map = { open: ['●', '事件流已连接', 'ok'], connecting: ['●', '事件流连接中', 'wait'], error: ['●', '事件流断开，重连中', 'err'], closed: ['●', '事件流已关闭', 'err'] } as const
  const [dot, title, kind] = map[sse]
  return <span className={`sse-dot ${kind}`} title={title}>{dot}</span>
}
