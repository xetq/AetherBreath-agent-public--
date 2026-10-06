// 任务编排器：**一排点**（2026-09-23 主人定：横排，最左串行，其后 8 个并行）。
//   · 用就亮，不用就灰；**不分成败**（成与败由输出卡片里的工具块体现）。
//   · 串行亮 = 蓝，并行亮 = 绿。
//   · 版面只留「任务编排器 / 真状态 / 并 N · 串 M · 跑 K」三样，细节收进 hover。
// 槽位定义取自 bridge 上报的池容量（hello.pools）：ThreadPoolExecutor 懒起线程，
// 没用过的槽位在任何事件里都不会出现，所以容量必须显式读，不能靠"见过哪些线程名"倒推。
//
// 跨回合作业（橙色）的特殊之处（2026-09-29 真机修）：它**活过它出生的那个回合**，
// 所以"它还在跑"这件事既不能随回合收尾熄灭（store 的 idleAll 已改），
// 也不能只靠浏览器内存（刷新就没了 -> App 的 syncOrch 对表兜住），
// 还得说得清"是哪个作业在跑"（bridge 把作业号/可读名带进管道事件）。
//
// 🔴 视觉契约（主人 2026-09-30 定）：**格子数恒定 = 池容量；亮点只能把暗点点亮。**
//    不许因为"线程号超出上报容量"或"认不出槽位"就**新增**一个橙点 ——
//    那会让点阵随着作业数量长出来，看起来像两套东西。多出来的占用改为：
//    ① 先占一个**空闲的暗格**（hover 里如实写明"未映射到具体槽位"）；
//    ② 一格都没有了，就只计入头部的「+N 未映射」（绝不画到格子外面去）。
import { useEffect, useMemo, useState } from 'react'
import { useApp } from '../store/appStore'
import type { PipeView, PoolCaps } from '../types'

type Busy = { tool: string; status: string; layer: number | null; elapsed?: number | null; sid?: string | null
              /** 跨回合作业身份 + 这一格的起点（本地秒针从它往后走） */
              jobId?: string | null; jobLabel?: string | null; at?: number }
type Slot = { key: string; pool: 'serial' | 'parallel'; no: number; busy: Busy | null; last: string; n: number; bad: number; job: boolean
              /** 这格是**吸收**来的（真身没映射到具体槽位）：hover 里必须说清 */
              loose?: boolean }
export type SlotsView = { slots: Slot[]; unmapped: number }

/** 把一条管道条目变成"这一格正在忙"的展示数据（作业身份也带上 —— hover 要说得清）。 */
function busyOf(v: PipeView): Busy {
  return { tool: v.tool, status: v.status, layer: v.layer ?? null, elapsed: v.elapsed ?? null,
           at: v.at, jobId: v.job_id ?? null, jobLabel: v.job_label ?? null }
}

const FALLBACK: PoolCaps = { parallel: { workers: 8 }, serial: { workers: 1 } }

// 线程名形如 orch-parallel_3 / orch-serial_0（Python 的 prefix_序号）
function parse(thread?: string | null): { pool: 'serial' | 'parallel'; no: number } | null {
  if (!thread) return null
  const m = /^(?:orch-)?(parallel|serial)_(\d+)$/.exec(thread)
  if (!m) return null
  return { pool: m[1] === 'serial' ? 'serial' : 'parallel', no: Number(m[2]) }
}

/**
 * 点阵的**纯函数**：谁亮、亮哪一格。抽出来是为了能被离线探针真跑
 * （`self_maintenance/packs/orch-orange-probe/probe_orch_slots.mjs`）——
 * "格子数恒定"这种视觉契约，只看源码里有某个词是证不出来的。
 */
export function buildSlots(
  pipes: Record<string, PipeView>,
  timeline: { tool: string; thread?: string | null; status: string; ok: boolean }[],
  real: boolean,
  nSer: number,
  nPar: number,
): SlotsView {
  const slots: Slot[] = []
  const byKey = new Map<string, Slot>()
  const add = (pool: 'serial' | 'parallel', no: number) => {
    const s: Slot = { key: `${pool}#${no}`, pool, no, busy: null, last: '', n: 0, bad: 0, job: false }
    slots.push(s); byKey.set(s.key, s); return s
  }
  for (let i = 0; i < nSer; i++) add('serial', i)
  for (let i = 0; i < nPar; i++) add('parallel', i)

  const occupy = (s: Slot, v: PipeView) => {
    s.n += 1
    s.job = !!v.background                         // 颜色只认**当前这条**（见 place 里的注释）
    s.busy = busyOf(v)
  }

  // 认不出槽位 / 槽位号超出容量的**忙**管道：先攒着，最后吸收进空闲格
  const orphans: PipeView[] = []
  const place = (v: PipeView) => {
    const p = parse(v.thread)
    const s = p ? byKey.get(`${p.pool}#${p.no}`) : undefined
    const busy = v.status === 'running' || v.status === 'pending'
    if (!s) { if (busy) orphans.push(v); return }  // 没有这一格：交给吸收，绝不新建
    s.n += 1
    if (busy) {
      s.busy = busyOf(v)
      // ★ 颜色**只由"当前在跑的这条"决定**（2026-10-02 真机修）：
      //   线程会被复用，所以同一个槽位先跑后台作业、后跑并行任务是常态。
      //   旧写法是粘性的 `if (v.background) s.job = true` —— 只要这一格**历史上**出现过
      //   background 条目（store 会把已结束的后台条目落回 idle 但保留这个标记），
      //   这一格就永久是橙色："以后再在这个管道运行并行任务，亮起来的还是橙色"。
      //   改成赋值（不是 |=）：当前这条是前台的，就把橙色摘掉、回到绿色。
      s.job = !!v.background
    } else {
      // 非忙条目（已结束/空闲/陈旧）只贡献"上次是谁"与失败数 —— **绝不参与颜色判定**
      s.last = v.tool
      if (v.status === 'failed' || v.status === 'cancelled') s.bad += 1
    }
  }

  if (real) {
    for (const tc of Object.keys(pipes)) place(pipes[tc])
  } else {
    for (const t of timeline) {
      const p = parse(t.thread)
      const s = p ? byKey.get(`${p.pool}#${p.no}`) : undefined
      if (!s) continue
      if (t.status === 'running') s.busy = { tool: t.tool, status: 'running', layer: null, elapsed: null }
      else { s.n += 1; s.last = t.tool; if (!t.ok) s.bad += 1 }
    }
  }

  let unmapped = 0
  for (const v of orphans) {
    const free = slots.find((s) => s.pool === 'parallel' && !s.busy)
    if (!free) { unmapped += 1; continue }         // 没有空闲格：只如实计数，不画格子外
    occupy(free, v)
    free.loose = true
  }
  return { slots, unmapped }
}

export default function OrchStrip() {
  const { state } = useApp()
  const caps = state.agent.hello?.pools || FALLBACK
  const known = !!state.agent.hello?.pools
  const nPar = Math.max(1, Math.min(64, caps.parallel?.workers ?? 8))
  const nSer = Math.max(1, Math.min(8, caps.serial?.workers ?? 1))
  const real = Object.keys(state.pipes).length > 0

  // 作业的"已跑时长"必须自己会走：跨回合作业能跑几十秒到十几分钟，静止的数字
  // 等于在说"它卡住了"。只在真的有作业在跑时才起秒针（空闲时零开销）。
  const [nowS, setNowS] = useState(() => Date.now() / 1000)

  const { slots, unmapped } = useMemo<SlotsView>(
    () => buildSlots(state.pipes, state.timeline, real, nSer, nPar),
    [state.pipes, state.timeline, real, nSer, nPar])

  const anyJob = slots.some((s) => s.job && !!s.busy)
  useEffect(() => {
    if (!anyJob) return
    setNowS(Date.now() / 1000)
    const t = window.setInterval(() => setNowS(Date.now() / 1000), 1000)
    return () => window.clearInterval(t)
  }, [anyJob])

  const running = slots.filter((s) => s.busy?.status === 'running').length

  return (
    <div className="orch">
      <div className="orch-head">
        <span className="orch-title">任务编排器</span>
        <span className={`orch-mode ${real ? 'real' : 'derived'}`}
              title={real ? 'bridge 上报的 ToolPipeline 真状态' : '未收到 pipeline 事件：按工具事件的线程名推导（重启 AB 可得真状态）'}>
          {real ? '真状态' : '推导'}
        </span>
        <span className="orch-kv" title={known ? `池容量由 bridge 上报；已创建线程 ${(caps.parallel?.started ?? 0) + (caps.serial?.started ?? 0)} 个（懒起）` : '未拿到池容量，按默认 8+1 画格子'}>
          并 {nPar} · 串 {nSer} · 跑 {running}{unmapped ? ` · +${unmapped} 未映射` : ''}
        </span>
      </div>

      {/* 一排点：最左串行，其后并行。亮 = 有活（执行中或排队中），灰 = 空闲。 */}
      <div className="orch-dots">
        {slots.map((s) => {
          const on = !!s.busy
          const cls = on ? (s.job ? 'job' : (s.pool === 'serial' ? 'ser' : 'par')) : 'idle'
          // 跨回合作业：把"是谁在跑、跑了多久"直接写进 hover —— 一个没有解释的色块还是黑箱。
          // 秒针只在真有作业在跑时走（见上面 anyJob），所以这里算出来的数字是活的。
          const age = s.job && s.busy && s.busy.status === 'running'
            ? Math.max(0, Math.round(nowS - (s.busy.at ?? nowS) + (s.busy.elapsed ?? 0)))
            : null
          const who = s.busy?.jobLabel || s.busy?.jobId || ''
          const where = s.loose
            ? `未映射到具体槽位（占一格空闲格显示）· ${s.pool === 'serial' ? '串行池' : '并行池'} #${s.no}`
            : `${s.pool === 'serial' ? '串行池' : '并行池'} #${s.no} · 线程 orch-${s.pool}_${s.no}`
          const detail = s.busy
            ? `${s.busy.status === 'running' ? '执行中' : '排队中'}：${s.busy.tool}`
              + (s.busy.layer != null ? `（L${s.busy.layer}）` : '')
              + (s.job ? ` · 跨回合作业${s.busy.jobId ? ` ${s.busy.jobId}` : ''}` : '')
              + (who ? ` · ${who}` : '')
              + (age != null ? ` · 已跑 ${age}s` : '')
            : s.last ? `空闲 · 上次 ${s.last}` : '空闲 · 从未使用'
          return (
            <span key={s.key} className={`odot ${cls}`}
                  title={`${where} · ${detail}${s.n ? ` · 累计 ${s.n} 次` : ''}${s.bad ? ` · 失败 ${s.bad}` : ''}`} />
          )
        })}
      </div>
    </div>
  )
}
