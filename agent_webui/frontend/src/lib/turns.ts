// 把「平铺的消息流」重建成「一轮一轮的交互回合」——界面只认这个结构（2026-09-23 主人定）。
//
// 为什么要重建：会话文件里一个回合是**多条** assistant 消息（模型每轮一条），
// 而界面上一个回合应该是**一张最终输出卡片**（内含"中期过程"）。刷新前后必须一致，
// 所以实时（liveTurns）与历史（messages + sidecar）都走同一个重建函数。
import type { HistoryMessage, LiveTurn, SidecarTurn, TimelineEntry, ToolCallView } from '../types'

/** 一轮 = 模型的一次往返：它调了哪些工具、想了什么、期间说了什么（中期输出）。 */
export interface RoundView {
  tools: ToolCallView[]
  think: string
  outs: string[]
}

export interface TurnView {
  key: string
  /** 本回合用户消息在**原始消息流里的下标**：DOM 锚点用它（消息导航栏跳转/联动），
   *  与 sidecar 的 user_seq（会话全局序号）也不是一回事 —— 历史被 limit 截断过，
   *  两种序号会错开。用它做锚点，就不会因为"第几个回合"理解不同而错位。 */
  anchor: number
  /** 本回合的用户输入（没有则 null —— 例如历史从 assistant 开始） */
  user: HistoryMessage | null
  /** 本回合用户追加的「用户交代」（中期交互）：挂在用户卡里，不单独成卡 */
  mids: HistoryMessage[]
  /** 本回合开头**自动交付**的后台作业结果（也挂在用户卡里；样式与交代区分开） */
  jobs: HistoryMessage[]
  rounds: RoundView[]
  /** 最终输出；null = 还没有（进行中，或中断了 → 用 endedText 兜底） */
  final: string | null
  /** done / interrupted / error / live / unknown */
  ended: string
  live: boolean
}

/** 没有最终输出时的预设终止提示（主人 2026-09-23：这种回合也要有"最终输出卡片"） */
export function endedText(ended: string): string {
  if (ended === 'interrupted') return '（本回合已中断 —— 未产生最终输出）'
  if (ended === 'error') return '（本回合出错终止 —— 未产生最终输出）'
  return '（本回合没有最终输出）'
}

/** agent 打印中期进度用的前缀（与 agent_webui/backend/bridge.py 的 MID_PROGRESS_TAG 一致） */
const MID_TAG = '[中期进度]: '

/** 中期输出清洗：sidecar 里存的是 agent 的**原始 stdout 行**（保真，不丢数据），
 *  但界面只该显示「中期进度」那部分 —— 于是：
 *    1. 剥掉 `[中期进度]: ` 前缀；
 *    2. 丢掉不属于该段的行（agent 启动信息 📜/📂、工具自身的 print）；
 *    3. 同一段的续行并回一段（Markdown 段落是按行 print 出来的）；
 *    4. 与同轮思考完全相同的段去掉 —— 模型没写正文的那些轮，agent 打印的就是思考原文，
 *       不删会在界面上"思考块 + 白字"重复一遍。 */
export function cleanOuts(lines: string[], think: string): string[] {
  const segs: string[] = []
  let cur: string | null = null
  for (const raw of lines) {
    const line = (raw || '').replace(/\s+$/, '')
    if (line.startsWith(MID_TAG)) {
      if (cur !== null) segs.push(cur)
      cur = line.slice(MID_TAG.length)
    } else if (cur !== null && line.trim()) {
      cur += '\n' + line
    }
    // 其余：不在任何 [中期进度] 段里的行 -> 丢弃（噪音）
  }
  if (cur !== null) segs.push(cur)
  const th = (think || '').trim()
  return segs.map((s) => s.trim()).filter((s) => s && s !== th)
}

export interface LiveInput {
  active: boolean
  /** 实时攒的轮。**直接用 store 的 LiveTurn**（不要在这里抄一份内联结构：
   *  抄一份就必然漏字段 —— 加流式草稿 thinkDraft/textDraft 时就这么漏过一次）。 */
  rounds: LiveTurn[]
  timeline: TimelineEntry[]
  streaming: string | null
  /** **进行中**回合的半个快照（磁盘版）：本回合还没收尾、实时事件也接不到时
   *  （刷新 / 切会话回来）用它把"已经产出的中期输出 + 思考"画回来。
   *  形状与 turns.ts 的 sidecar 完全一致，所以两条路能共用同一套清洗。 */
  partial?: SidecarTurn[]
}

function toCallView(t: TimelineEntry): ToolCallView {
  return {
    id: t.call_id, name: t.tool, args: t.args, result: t.result,
    failed: t.status === 'done' && !t.ok, pending: t.status === 'running',
  }
}

/** 消息流（+ sidecar + 实时态）→ 回合列表。纯函数，无副作用。 */
export function buildTurns(
  messages: HistoryMessage[], sidecar: SidecarTurn[], live: LiveInput,
): TurnView[] {
  // ① 先按"真实用户输入"切段；用户交代（mid）与后台作业交付（job）都挂进当前段
  //    —— 它们**不是主人说的话**，另起一段就会在界面上凭空多出没有回复的回合
  //    （bridge 的 _user_seq 同样把它们排除在轮次外）。
  //    注：权限模式切换通知**不在这里** —— 它已不再进历史，界面用一块就地更新的
  //    状态块表示"此刻是什么档"（见 MessageList 的 ModeStatusBlock）。
  type Seg = { user: HistoryMessage | null; at: number; mids: HistoryMessage[]; jobs: HistoryMessage[]; asst: HistoryMessage[] }
  const segs: Seg[] = []
  const ensure = () => {
    if (!segs.length) segs.push({ user: null, at: -1, mids: [], jobs: [], asst: [] })
    return segs[segs.length - 1]
  }
  messages.forEach((m, mi) => {
    if (m.role === 'user') {
      if (m.mid) {
        ensure().mids.push(m)
      } else if (m.job) {
        // 交付是"本回合开始前注入的结果"，位置就在本轮用户输入之后 —— 挂进同一张用户卡
        ensure().jobs.push(m)
      } else {
        segs.push({ user: m, at: mi, mids: [], jobs: [], asst: [] })
      }
    } else {
      ensure().asst.push(m)
    }
  })

  // sidecar 的 user_seq 是**会话全局**序号；历史被 limit 截断时它就对不上了，
  // 所以再备一条"尾部对齐"（sidecar 与段数相同才启用，宁可不显示也不显示错位的）。
  const bySeq = new Map(sidecar.map((t) => [t.user_seq, t]))
  const tail = sidecar.length === segs.length ? sidecar : []
  const turns: TurnView[] = segs.map((seg, si) => {
    const rounds: RoundView[] = seg.asst.map((a) => ({
      tools: a.tool_calls || [], think: (a.reasoning || '').trim(), outs: [],
    }))
    let final: string | null = null
    // 最后一条 assistant 的正文 = 最终输出；中间轮的正文 = 中期输出（agent 打印的就是它，
    // 所以没有 sidecar 的老回合也能把中期输出近似还原出来）
    seg.asst.forEach((a, i) => {
      const c = (a.content || '').trim()
      if (i === seg.asst.length - 1) { if (c) final = c }
      else if (c) rounds[i].outs.push(c)
    })
    // sidecar 权威（有就覆盖近似值）：把 stdout 行按轮塞回
    const sc = bySeq.get(si) || tail[si]
    let ended = 'unknown'
    if (sc) {
      ended = sc.ended || 'done'
      rounds.forEach((r) => { r.outs = [] })
      // 先按轮分组原始行，再逐轮清洗（剥前缀/去噪/合并/与思考去重）
      const byRound = new Map<number, string[]>()
      for (const o of sc.outs) {
        const r = Math.min(Math.max(o.round, 0), Math.max(rounds.length - 1, 0))
        const arr = byRound.get(r) || []
        arr.push(o.text)
        byRound.set(r, arr)
      }
      rounds.forEach((r, i) => { r.outs = cleanOuts(byRound.get(i) || [], r.think) })
    }
    return { key: `turn-${si}`, anchor: seg.at >= 0 ? seg.at : si, user: seg.user, mids: seg.mids,
             jobs: seg.jobs, rounds, final, ended, live: false }
  })

  // ② 进行中的回合：把实时数据**逐轮合并**进去。
  //
  //    ⚠️ 这里踩过两个坑，都是同一个错法（"先整个替换、再想补回来"）：
  //      · 2026-10-02-a：`live.rounds` 为空时把 t.rounds 换成 []，再"补空轮"无从补起
  //        → 刷新后中期进度消失（主人报的）；
  //      · 2026-10-02-b：同一个替换还会把**工具调用**一起清掉 —— 消息里明明有工具，
  //        界面上却一个都不显示（主人报的第二个现象）。
  //    正确口径：**轮数取"实时 / 消息"的较大值，逐轮按优先级填**；
  //    实时一个轮都没有时，消息里那份（含工具）原样保留，一个字都不动。
  if (live.active && turns.length) {
    const t = turns[turns.length - 1]
    if (live.rounds.length) {
      const byId = new Map(live.timeline.map((x) => [x.call_id, x]))
      const n = Math.max(t.rounds.length, live.rounds.length)
      const merged: RoundView[] = []
      for (let r = 0; r < n; r++) {
        const base = t.rounds[r]                         // 消息里那份（含历史 tool_calls）
        const rt = live.rounds[r]                        // 实时那份
        if (!rt) { merged.push(base || { tools: [], think: '', outs: [] }); continue }
        const liveTools = rt.toolIds.map((id) => byId.get(id))
          .filter((x): x is TimelineEntry => !!x).map(toCallView)
        // 思考优先级：实时正式 → 消息里的 → 流式草稿。
        // （消息那一层不能跳：跳过它就会把"消息里明明有思考"的轮让给草稿/快照，
        //   离线用例抓到过这个漏层。）
        const think = (rt.reasoning || '').trim()
          || (base?.think || '').trim()
          || (rt.thinkDraft || '').trim()
        // 中期输出：正式 stdout 行优先；只有草稿时把草稿作为**最后一段**追加
        // （追加而不是替换：前面几轮的进度不该被正在写的这一段挤掉）。
        const outs = rt.outs.length
          ? cleanOuts(rt.outs, think)
          : (base?.outs || [])
        // ⚠️ 草稿只在"还没有正式 outs"时追加：有正式 outs 说明这一轮的输出已经落定，
        // 再追加草稿就是在界面上把同一段字显示两遍（离线用例抓到过）。
        const draft = (rt.outs.length || !rt.textDraft) ? '' : rt.textDraft.trim()
        merged.push({
          // 实时认得的工具优先；实时还没收到那几帧时，保留消息里的工具，别把工具弄丢
          tools: liveTools.length ? liveTools : (base?.tools || []),
          think,
          outs: draft ? [...outs, draft] : outs,
        })
      }
      t.rounds = merged
    }
    // 只有**确实拿到了实时轮**才把这个回合标成"进行中"。
    // 否则（例如切到了别的会话、而 turnOwner 还指着原来那个会话）会把一个已经
    // 收尾的回合画成 live，还会顺手把最终输出清成 null —— 那是拿"进程级忙"
    // 去谎报"这个回合在跑"。宁可什么都不改。
    if (live.rounds.length) {
      t.live = true
      t.ended = 'live'
      t.final = null
      if (live.streaming && live.streaming.trim()) t.final = live.streaming
    }
  }

  // ③ 进行中回合的**磁盘版**补洞（2026-10-02）。
  //    为什么需要它：会话数据有两条路 —— 实时走 liveTurns（内存事件），
  //    刷新/切会话走 messages + sidecar。而本回合**还没收尾**，sidecar 的 turns 里
  //    没有它，liveTurns 又被清空 → 屏幕上"已经产出的东西"凭空消失，
  //    要等下一个回合收尾才回来（主人报的正是这个）。
  //    修法：bridge 把本回合节流落盘成 partial 快照，这里把它铺回去。
  //
  //    ⚠️ 这里踩过一个坑（2026-10-02 实测）：刷新后网关会先把 busy 恢复回来
  //    （`agent.busy` + turnOwner 都对了）→ `active=true`，**而 liveTurns 还是空的**
  //    （实时事件要等重连后才有）。旧写法在这一步把 t.rounds 整个换成空的 liveTurns，
  //    再想"补空轮"已经无从补起 → 中期进度照旧消失。所以：
  //    **轮数取"实时 / 消息 / 快照"三者的最大值，再逐轮按优先级填**，
  //    而不是先清空再修补。这是主人那个场景（B 态）的正面修法。
  const partial = live.partial && live.partial.length
    ? (live.partial[live.partial.length - 1] || null)
    : null
  if (partial && turns.length) {
    const t = turns[turns.length - 1]
    // 错位护栏：快照记录的是"第几次真实用户输入"（user_seq）。它与末段序号不一致，
    // 就说明这是**别的回合**留下的快照（例如刚发新消息、bridge 还没写新快照）——
    // 宁可不补，也绝不把上一条的进度缝到新回合头上。
    const okSeq = partial.user_seq === (segs.length - 1)
    // 快照的轮数是"直到此刻已产出的轮数"，可能与实时/消息里的轮数不一致
    // （刷新时机不同）—— 取三者较大值，宁多不少。
    const n = Math.max(t.rounds.length,
                       ...partial.outs.map((o) => o.round + 1),
                       ...Object.keys(partial.reasoning || {}).map((k) => Number(k) + 1),
                       0)
    // 快照按轮整理（思考用快照原文、输出走同一个 cleanOuts）
    const snapReason = partial.reasoning || {}
    const byRound = new Map<number, string[]>()
    for (const o of partial.outs) {
      const r = Math.min(Math.max(o.round, 0), Math.max(n - 1, 0))
      const arr = byRound.get(r) || []
      arr.push(o.text)
      byRound.set(r, arr)
    }
    const merged: RoundView[] = []
    for (let r = 0; r < n; r++) {
      const liveR = t.rounds[r]                       // 实时/消息那一份（可能不存在或为空）
      const snapThink = (snapReason[String(r)] || '').trim()
      const think = (liveR?.think || '').trim() || snapThink
      const snapOuts = cleanOuts(byRound.get(r) || [], think)
      merged.push({
        tools: liveR?.tools || [],
        think,
        // 实时有输出就用实时的；没有才用快照的（实时是权威，快照是兜底）
        outs: (liveR?.outs && liveR.outs.length) ? liveR.outs : snapOuts,
      })
    }
    if (!live.active || okSeq) {
      // active=false：实时通道确实没接上 → 整轮以"实时/消息 + 快照合并"为准铺回来。
      // active=true：实时在跑，但重连后 liveTurns 可能大面积为空 —— 也按合并结果铺，
      //   因为合并规则本身就保证了"实时有的优先、实时没有的才用快照"。
      t.rounds = merged
      t.ended = 'live'
      // 走到这里说明快照**就是本回合的**（okSeq 已确认，active 或非 active 都算），
      // 所以这个回合确实还在跑：标 live 让界面说"思考中/工具执行中"，
      // 而不是拿 endedText 那种"没有最终输出"的中断口吻（那会让人以为回合已经黄了）。
      t.live = true
    }
  }
  return turns
}
