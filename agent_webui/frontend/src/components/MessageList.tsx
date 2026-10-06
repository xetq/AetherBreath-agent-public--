import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { renderMarkdown } from '../lib/md'
import { JOB_DELIVERY_BADGE, JOB_DELIVERY_MARK, parseJobDelivery } from '../lib/jobDelivery'
import { MID_BADGE, MID_MARK } from '../lib/midTurn'
import { modeIcon, modeLabel, modeStatus, type ModeOption } from '../lib/permissionMode'
import { MSG_ANCHOR, navItems } from '../lib/msgNav'
import { buildTurns, endedText } from '../lib/turns'
import type { TurnView } from '../lib/turns'
import { useApp } from '../store/appStore'
import MessageNav from './MessageNav'
import type { HistoryMessage, ToolCallView } from '../types'

/* 卡片结构与顺序（主人 2026-09-23 定）：
     [👤 用户卡]  正文 + 本回合的「用户交代」（中期交互）
     [☲ 最终输出卡]
        ├ 中期过程（嵌套在顶端；进行中默认展开、完成后强制收起）
        │   展开后 = 每轮一组：工具 → 思考 → 中期输出（中期输出正常白色，不折叠）
        └ 最终输出正文（没有最终输出的回合显示预设终止提示）
   刷新前 / 刷新后同一个结构 —— 不再有"一整张卡"和"一轮一张卡"的分裂。
   每轮内的顺序（主人 2026-09-23 追加交代定）：**思考 → 工具 → 中期输出**，逐轮交替，
   不许把工具、思考、中期输出各自堆成一大块。 */

function FoldBlock({ icon, title, meta, danger, children }: {
  icon: string; title: string; meta?: string; danger?: boolean; children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className={`fold${open ? ' open' : ''}${danger ? ' danger' : ''}`}>
      <button type="button" className="fold-head" onClick={() => setOpen((v) => !v)}
              title={open ? '收起' : '展开查看全部'}>
        <span className="fold-ico">{icon}</span>
        <span className="fold-title">{title}</span>
        {meta ? <span className="fold-meta">{meta}</span> : null}
        <span className="fold-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="fold-body">{children}</div>}
    </div>
  )
}

/** 思考过程：模型这一轮的 reasoning 原文（保留换行）。老会话被清过 -> 调用方不渲染本块。 */
function ThinkBlock({ text }: { text: string }) {
  return (
    <FoldBlock icon="💡" title="思考过程">
      <div className="think-body">{text}</div>
    </FoldBlock>
  )
}

/** 单条工具：一行摘要，点开看参数与返回全文。 */
function ToolLine({ t }: { t: ToolCallView }) {
  const [open, setOpen] = useState(false)
  const brief = (t.args || '').replace(/\s+/g, ' ').trim()
  return (
    <div className={`tline${t.failed ? ' err' : ''}${t.pending ? ' wait' : ''}`}>
      <div className="tline-head" onClick={() => setOpen((v) => !v)}
           title={open ? '收起' : '展开参数与返回'}>
        <span className="tline-ico">{t.pending ? '⏳' : t.failed ? '💥' : '🔧'}</span>
        <span className="tline-name">{t.name}</span>
        <span className="tline-args">{brief || '（无参数）'}</span>
        <span className="tline-caret">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="tline-body">
          <div className="tl-lbl">参数</div>
          <pre>{t.args || '（无）'}</pre>
          {t.failed
            ? <><div className="tl-lbl err">返回（失败）</div><pre className="err">{t.result ?? '（空）'}</pre></>
            : <><div className="tl-lbl">返回</div>
               <pre>{t.result ?? (t.pending ? '执行中…' : '（空）')}</pre></>}
        </div>
      )}
    </div>
  )
}

function ToolsBlock({ tools }: { tools: ToolCallView[] }) {
  const failed = tools.filter((t) => t.failed).length
  const pending = tools.filter((t) => t.pending).length
  const meta = failed ? `💥 ${failed} 失败` : pending ? `⏳ ${pending} 进行中` : ''
  return (
    <FoldBlock icon=">_" title={`已运行工具 · ${tools.length}`} meta={meta} danger={failed > 0}>
      <div className="tool-list">
        {tools.map((t, i) => <ToolLine key={t.id || i} t={t} />)}
      </div>
    </FoldBlock>
  )
}

/** 中期过程：进行中默认展开；一旦出现最终输出就**强制收起**（主人定）。
 *
 *  2026-10-02 修正：判据从 `t.live` 放宽到 `t.ended === 'live'` —— 刷新/切会话回来时
 *  实时通道还没接上（t.live=false），但磁盘快照已把"本回合进行到哪"标了出来。
 *  只认 t.live 的话，那种情况下过程块默认收起，主人以为"思考没显示"（实为折叠了）。 */
function ProcessBlock({ t }: { t: TurnView }) {
  const running = t.live || t.ended === 'live'
  const [open, setOpen] = useState(running)
  useEffect(() => { if (!running) setOpen(false) }, [running])
  const nTools = t.rounds.reduce((n, r) => n + r.tools.length, 0)
  return (
    <div className={`proc${open ? ' open' : ''}${t.live ? ' live' : ''}`}>
      <button type="button" className="proc-head" onClick={() => setOpen((v) => !v)}
              title={open ? '收起中期过程' : '展开中期过程（每轮：工具 → 思考 → 中期输出）'}>
        <span className="proc-title">中期过程</span>
        <span className="proc-meta">
          {t.rounds.length ? `${t.rounds.length} 轮` : '尚无进展'}
          {nTools ? ` · ${nTools} 工具` : ''}
        </span>
        <span className="fold-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="proc-body">
          {t.rounds.map((r, i) => (
            /* 每轮一组，顺序固定（主人 2026-09-23 追加交代）：**思考 → 工具 → 中期输出**。
               三种东西各占自己的块、按先后排开 —— 不许"工具堆一块、输出堆一块"。 */
            <div className="proc-round" key={i}>
              {r.think ? <ThinkBlock text={r.think} /> : null}
              {r.tools.length > 0 ? <ToolsBlock tools={r.tools} /> : null}
              {r.outs.map((o, oi) => <div className="proc-out" key={oi}>{o}</div>)}
            </div>
          ))}
          {t.rounds.length === 0 ? <div className="proc-out dim">（还没有进展）</div> : null}
        </div>
      )}
    </div>
  )
}

function Atts({ list }: { list: HistoryMessage['attachments'] }) {
  if (!list || list.length === 0) return null
  return (
    <div className="msg-atts">
      {list.map((a, i) => (
        <span key={`${a.path}-${i}`} className="msg-att" title={a.path}>
          <span className="att-tag">附件</span>{a.name}
        </span>
      ))}
    </div>
  )
}

/** 一条后台作业交付：正文超长时折叠（一份交付可能是几万字符的日志）。 */
const JOB_INLINE_MAX = 1500

function JobDeliveryBlock({ content }: { content: string }) {
  const p = parseJobDelivery(content)
  const body = <div className="body-text plain job-body">{p.body}</div>
  return (
    <div className="job-inline">
      <div className="who job-who">
        ☲ AB · <b>{JOB_DELIVERY_MARK}</b>
        <span className="job-badge">{JOB_DELIVERY_BADGE}</span>
      </div>
      {p.title ? <div className="job-title">{p.title}</div> : null}
      {p.body.length > JOB_INLINE_MAX
        ? <FoldBlock icon="📦" title={`交付全文 · ${p.body.length} 字符`}
                     meta="点开看全文">{body}</FoldBlock>
        : body}
      {p.notes.length ? <div className="job-notes">{p.notes.join(' ')}</div> : null}
    </div>
  )
}

/** 权限状态块：**一块，就地更新**（主人 2026-10 定）。
 *
 *  为什么不做成"每次切换追加一块"：那样切几次就堆几块，既占地方又是切换流水；
 *  主人要的是"此刻是什么状态"。所以它由**实时态**（store.permissionMode）驱动，
 *  钉在最新那张用户卡里：切一次就变一次，位置不动、数量恒为一。
 *  还没有发过 user 消息时不出现 —— 等第一条消息发出来，自然就贴在那张卡上。 */
function ModeStatusBlock({ mode, catalog }: { mode: string; catalog: ModeOption[] }) {
  const status = modeStatus(mode, catalog)
  return (
    <div className="mode-inline" title={status || undefined}>
      <span className="mode-inline-ico">{modeIcon(mode)}</span>
      <span className="mode-inline-name">权限模式 · {modeLabel(mode)}</span>
      {status ? <span className="mode-inline-status">{status}</span> : null}
    </div>
  )
}

/** 用户卡：本回合的输入 + 主人趁回合追加的「用户交代」+ 自动交付的后台作业结果。
 *
 *  后两者都**挂在用户卡里、不单独成卡**（主人 2026-09-23 / 2026-09-30 两次定），
 *  但样式必须分得开：**交代是"人说的话"（紫），交付是"机器送回来的结果"（橙）** ——
 *  一眼看错，就会把作业输出当成主人的指示。
 *  权限状态块（青蓝）也挂在这里，但它是**实时态**、不来自历史。 */
function UserCard({ t, anchorRef, mode, modeCatalog }: {
  t: TurnView
  /** 挂 DOM 锚点（消息导航栏点击跳转要用它 scrollIntoView）—— `data-msg-turn` = 回合序号 */
  anchorRef: (el: HTMLDivElement | null) => void
  /** 本会话**当前**权限模式：只有最新那张用户卡拿到值，其余传 null ——
   *  状态块因此恒为一块，且随切换就地更新（不是每次切换追加一块）。 */
  mode?: string | null
  modeCatalog: ModeOption[]
}) {
  const u = t.user
  return (
    <div className="msg user" ref={anchorRef}>
      <div className="who">👤 你</div>
      {u?.content ? <div className="body-text plain">{u.content}</div> : null}
      <Atts list={u?.attachments} />
      {t.mids.map((m, i) => (
        <div className="mid-inline" key={i}>
          <div className="who mid-who">
            👤 你 · <b>{MID_MARK}</b>
            <span className={`mid-badge ${m.mid}`}>{m.mid ? MID_BADGE[m.mid] : ''}</span>
          </div>
          <div className="body-text plain">{m.content}</div>
          <Atts list={m.attachments} />
        </div>
      ))}
      {t.jobs.map((d, i) => <JobDeliveryBlock key={`job-${i}`} content={d.content} />)}
      {mode ? <ModeStatusBlock mode={mode} catalog={modeCatalog} /> : null}
    </div>
  )
}

export default memo(function MessageList() {
  const { state } = useApp()
  const endRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  /** 子卡片挂载时挂锚点：`data-msg-turn` = 回合序号。
   *  导航栏跳转/滚动联动靠它 query 卡片，所以**不另存一份 DOM 表**（一份真相，不会发霉）。 */
  const anchorRef = useCallback((idx: number) => (el: HTMLDivElement | null) => {
    if (el) el.setAttribute(MSG_ANCHOR, String(idx))
  }, [])

  // 回合归属门控：busy/turn_phase 是进程级事实，只有当这个回合确实属于
  // 当前会话时才在本视图显示状态卡，否则切会话会串台（旧回合卡片挂在空会话下面）。
  const owns = state.turnOwner !== null && state.turnOwner === state.current
  const busy = state.agent.busy && owns

  const turns = useMemo(() => buildTurns(state.messages, state.turnOuts, {
    active: busy, rounds: state.liveTurns, timeline: state.timeline,
    streaming: state.streaming,
    // 进行中回合的磁盘快照：刷新/切会话时用它把已经产出的东西画回来
    partial: state.turnPartial,
  }), [state.messages, state.turnOuts, busy, state.liveTurns, state.timeline, state.streaming,
       state.turnPartial])

  /** 导航栏每一项 = 一条真实用户消息（「用户交代」/后台作业交付挂靠的回合不算） */
  const navs = useMemo(() => navItems(turns), [turns])

  /** 权限状态块钉在**最新一张有用户消息的卡**里（主人 2026-10 定：一块、就地更新）。
   *  还没有 user 消息时返回 null —— 不凭空造一张空卡；等第一条消息发出来自然就贴上了。 */
  const modeCardKey = useMemo(() => {
    for (let i = turns.length - 1; i >= 0; i -= 1) {
      if (turns[i].user) return turns[i].key
    }
    return null
  }, [turns])

  useEffect(() => {
    const onScroll = () => {
      const el = bottomRef.current
      if (!el) return
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
    }
    const el = bottomRef.current
    el?.addEventListener('scroll', onScroll)
    return () => el?.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (stick.current) endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [turns.length, state.messages.length, state.streaming, state.liveTurns.length])

  return (
    <div className="msgs" ref={bottomRef}>
      {/* 常驻右侧边缘的消息导航竖轨：一条用户消息一条横杠，悬停预览 / 点击跳转 / 滚动联动。
          它是 fixed 定位的覆盖层（见 MessageNav 的说明），不占对话区宽度、不随内容滚走。 */}
      <MessageNav items={navs} turnCount={turns.length}
                  sessionKey={state.current || '_none'} scrollRef={bottomRef} />

      {turns.length === 0 && !busy && (
        <div className="hero">
          <div className="hero-mark">☲</div>
          <h3>AetherBreath WebUI</h3>
          <p>左侧选会话或新建 → 顶部「🟢 开机」拉起 AB 子进程 → 直接开聊。<br />
             一个回合一张卡：卡片顶端是可展开的「中期过程」（工具 / 思考 / 中期输出），下面是最终输出。</p>
          <p className="hero-tip">⚠️ 同一会话请勿同时在 CLI 与 WebUI 操作：两端是独立进程，只共享磁盘。</p>
        </div>
      )}

      {turns.map((t) => (
        <Fragment key={t.key}>
          {t.user || t.mids.length ? (
            <UserCard t={t} anchorRef={anchorRef(t.anchor)}
                      mode={t.key === modeCardKey ? state.permissionMode : null}
                      modeCatalog={state.modeCatalog} />
          ) : null}
          <div className={`msg assistant${t.live ? ' live' : ''}`}>
            <div className="who">☲ AB {t.live ? <span className="typing">●●●</span> : null}</div>
            <ProcessBlock t={t} />
            {t.final ? (
              <div className="body-text md" dangerouslySetInnerHTML={{ __html: renderMarkdown(t.final) }} />
            ) : t.live ? (
              <div className="body-text waittext">
                {state.agent.turn_phase === 'ASK_WAIT' ? '等待你在下方回答…'
                  : state.agent.turn_phase === 'TOOL_RUNNING' ? '工具执行中…'
                  : '思考中…'}
              </div>
            ) : (
              /* 没有最终输出的回合（中断 / 出错 / 未知）：按主人要求给预设终止提示，
                 这样"最终输出卡片"在场，本回合的形态才算收口 */
              <div className="body-text waittext">{endedText(t.ended)}</div>
            )}
          </div>
        </Fragment>
      ))}

      {state.turnError && (
        <div className="msg error">
          <div className="who">❌ 错误</div>
          <div className="body-text plain">{state.turnError}</div>
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
})
