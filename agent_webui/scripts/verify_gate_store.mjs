// 真机回归：审批 / ask_user 卡片在「切会话、刷新、重开页面」后的状态语义。
//
// 为什么测 reducer 而不是只测接口：四个现象的现场都在这里 —— 卡片是前端内存里的
// 东西，接口通了不等于屏幕上还在。这份测试直接 bundle 生产文件
// frontend/src/store/appStore.tsx（只在 bundle 时给它补两个 export，源码一字不改），
// 因此断言的就是生产行为。
//
// 跑法：  node agent_webui/scripts/verify_gate_store.mjs
// 依赖：  frontend/node_modules（esbuild + react），由 vite 工程自带
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'
import os from 'node:os'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WEBUI = path.dirname(HERE)
const FE = path.join(WEBUI, 'frontend')
const requireFE = createRequire(path.join(FE, 'package.json'))
const esbuild = requireFE('esbuild')

// ---------- 1. bundle 生产 appStore（补 export 以便调用内部纯函数） ----------
const expose = {
  name: 'expose-internals',
  setup(b) {
    b.onLoad({ filter: /appStore\.tsx$/ }, async (args) => {
      let src = await fs.promises.readFile(args.path, 'utf8')
      const before = src
      src = src.replace('function reducer(state: State, a: Action): State {',
                        'export function reducer(state: State, a: Action): State {')
      src = src.replace('function onEvent(state: State, evt: WsEvent): State {',
                        'export function onEvent(state: State, evt: WsEvent): State {')
      if (src === before) throw new Error('appStore.tsx 结构变了：补 export 的锚点没命中')
      return { contents: src, loader: 'tsx' }
    })
  },
}

const out = path.join(os.tmpdir(), `abw_gate_store_${Date.now()}.cjs`)
await esbuild.build({
  entryPoints: [path.join(FE, 'src', 'store', 'appStore.tsx')],
  bundle: true, format: 'cjs', platform: 'node', outfile: out, plugins: [expose],
  logLevel: 'error',
})

// ---------- 2. 装一个最小的浏览器替身（persist.ts 会读 localStorage） ----------
globalThis.window = {
  localStorage: {
    _d: {}, getItem(k) { return k in this._d ? this._d[k] : null },
    setItem(k, v) { this._d[k] = String(v) }, removeItem(k) { delete this._d[k] },
  },
  setTimeout: () => 0, clearTimeout: () => {},
}

const mod = requireFE(out)
const { reducer, initialState } = mod
fs.unlinkSync(out)

// ---------- 3. 断言工具 ----------
const checks = []
const check = (name, ok, detail = '') => checks.push({ name, ok: !!ok, detail })
const count = (s) => s.approvals.length
const ev = (type, extra) => ({ type, ...extra })

// 一张合并审批卡的 SSE 事件（形状与 approval_adapter 发出的完全一致）
const batchEvt = (over = {}) => ev('approval_batch', {
  batch_id: 'B1', timeout: 300, total: 2,
  session_id: 'sessA', ts: 1,
  scopes: [{ key: 'once', label: '仅批准本次', emoji: '1' }],
  accepts_note: true, note_hint: 'hint',
  items: [
    { ask_id: 'i1', kind: 'write_file', risk: 2, title: 't1', intent: '写 C:/x',
      reason: 'r', paths: ['C:/x'], notes: ['n1'], critical: false, source_code: 'code' },
    { ask_id: 'i2', kind: 'write_file', risk: 3, title: 't2', intent: '写 C:/y',
      reason: 'r', paths: ['C:/y'], notes: [], critical: true, source_code: '' },
  ],
  ...over,
})

let s = reducer(initialState, { type: 'set_sessions', sessions: [] })

// ---------- 现象 1 / 3：切会话、新建后返回，卡片必须还在 ----------
s = reducer(s, { type: 'event', evt: batchEvt() })
check('卡片弹出后在队列里', count(s) === 1 && s.approvals[0].ask_id === 'B1', `count=${count(s)}`)
const withCard = s
s = reducer(s, { type: 'set_current', sid: 'sessB' })
check('现象1/3：切到别的会话，卡片不被删（后端还在等）', count(s) === 1, `count=${count(s)}`)
s = reducer(s, { type: 'set_current', sid: null })
check('现象3：新建会话（sid=null）时卡片不被删', count(s) === 1, `count=${count(s)}`)
s = reducer(s, { type: 'set_current', sid: 'sessA' })
check('现象1：切回来，卡片仍在', count(s) === 1, `count=${count(s)}`)
check('卡片归属仍标着原会话（好让界面提示"属于别的会话"）',
  s.approvals[0]?.session_id === 'sessA', String(s.approvals[0]?.session_id))

// ---------- 现象 2 / 4：刷新 / 重开页面 —— 全新 store 靠服务端快照找回 ----------
let fresh = reducer(initialState, { type: 'set_current', sid: 'sessA' })
check('现象2/4 前提：全新 store 里确实没有卡片', count(fresh) === 0, `count=${count(fresh)}`)
const restored = batchEvt({ restored: true, remaining: 240, batch_id: 'B1' })
fresh = reducer(fresh, { type: 'event', evt: restored })
check('现象2/4：服务端快照能重建卡片', count(fresh) === 1, `count=${count(fresh)}`)
check('恢复卡带 restored 标记', fresh.approvals[0]?.restored === true)
check('恢复卡的条目字段完整（intent/paths/critical 都在）',
  fresh.approvals[0]?.items?.[1]?.critical === true
  && fresh.approvals[0]?.items?.[1]?.paths?.[0] === 'C:/y')
check('恢复卡显式显示"由服务端找回"', (fresh.approvals[0]?.items?.[0]?.notes || [])
  .some((n) => n.includes('由服务端找回')), JSON.stringify(fresh.approvals[0]?.items?.[0]?.notes))
const bornAge = Date.now() - (fresh.approvals[0]?.born || 0)
check('恢复卡倒计时按 remaining 续算（不重置成满格）',
  bornAge >= 55000 && bornAge <= 75000, `age=${Math.round(bornAge / 1000)}s (期望≈60s)`)
check('恢复卡的 accepts_note / note_hint 未被丢掉（白名单漏抄的旧病）',
  fresh.approvals[0]?.accepts_note === true && fresh.approvals[0]?.note_hint === 'hint')

// ---------- 剪枝守卫：拉不到 ≠ 没有卡 ----------
let g = reducer(withCard, { type: 'prune_gates', ids: [], before: Date.now(),
                            approvals: false, ask: false })
check('守卫生效：通道拉取失败时不剪枝（卡片保留）', count(g) === 1, `count=${count(g)}`)
g = reducer(withCard, { type: 'prune_gates', ids: [], before: Date.now() + 10000,
                        approvals: true, ask: true })
check('权威快照说没有卡时，僵尸卡被剪掉', count(g) === 0, `count=${count(g)}`)
g = reducer(withCard, { type: 'prune_gates', ids: ['B1'], before: Date.now(),
                        approvals: true, ask: true })
check('权威快照说卡还在时，卡片保留', count(g) === 1, `count=${count(g)}`)

// ---------- 提问卡（ask_user）同一条病，同一条修法 ----------
const clEvt = (over = {}) => ev('ask_request', {
  ask_id: 'C1', question: '选哪个？', options: ['A', 'B'], mode: 'choice',
  session_id: 'sessA', timeout: 120, run_id: 'r1', ...over,
})
let c = reducer(initialState, { type: 'event', evt: clEvt() })
check('提问卡弹出后存在', c.clarifies.length === 1 && c.clarifies[0].ask_id === 'C1')
c = reducer(c, { type: 'set_current', sid: 'sessB' })
check('提问卡：切会话不被删', c.clarifies.length === 1, JSON.stringify(c.clarifies[0]?.ask_id))
const clFresh = reducer(reducer(initialState, { type: 'set_current', sid: 'sessB' }),
                        { type: 'event', evt: clEvt({ restored: true, remaining: 100 }) })
check('提问卡：刷新后能从服务端快照找回',
  clFresh.clarifies.length === 1 && clFresh.clarifies[0].restored === true)
const clAge = Date.now() - (clFresh.clarifies[0]?.born || 0)
check('提问卡：恢复后倒计时按 remaining 续算',
  clAge >= 15000 && clAge <= 25000, `age=${Math.round(clAge / 1000)}s (期望≈20s)`)
let cg = reducer(reducer(initialState, { type: 'event', evt: clEvt() }),
                 { type: 'prune_gates', ids: [], before: Date.now(), approvals: false, ask: false })
check('提问卡：通道失败时不剪枝', cg.clarifies.length === 1)
cg = reducer(reducer(initialState, { type: 'event', evt: clEvt() }),
             { type: 'prune_gates', ids: [], before: Date.now() + 10000,
               approvals: true, ask: true })
check('提问卡：权威快照说没有时剪掉', cg.clarifies.length === 0)

// ---------- 提问多卡：一个回合里可以同时挂着好几张（ask_user 支持并行问） ----------
// 单槽时代这里只会剩一张 —— 三张卡互相覆盖，主人答完一张就再也看不见其余的。
const clA = clEvt({ ask_id: 'C1', question: '问一' })
const clB = clEvt({ ask_id: 'C2', question: '问二', batch_size: 3, batch_live: 2 })
const clC = clEvt({ ask_id: 'C3', question: '问三', batch_size: 3, batch_live: 3 })
let m = reducer(initialState, { type: 'event', evt: clA })
m = reducer(m, { type: 'event', evt: clB })
m = reducer(m, { type: 'event', evt: clC })
check('三张提问卡同时挂着（单槽时代只会剩最后一张）', m.clarifies.length === 3,
  `len=${m.clarifies.length}`)
check('批次信息透传到卡上（共几问 / 还剩几问）',
  m.clarifies[2].batch_size === 3 && m.clarifies[2].batch_live === 3,
  JSON.stringify(m.clarifies[2]))
check('同 ask_id 补发不堆叠（SSE 重连）',
  reducer(m, { type: 'event', evt: clC }).clarifies.length === 3)
m = reducer(m, { type: 'resolve_ask', ask_id: 'C2', remaining: 118 })
check('答一张只摘一张，其余仍在等', m.clarifies.length === 2
  && !m.clarifies.some((x) => x.ask_id === 'C2'), `len=${m.clarifies.length}`)
check('作答回执的真值把共享窗口续上（其余卡倒计时按 118s 重算）',
  m.askWindow?.left === 118 && m.askWindow.at > 0, JSON.stringify(m.askWindow))
m = reducer(m, { type: 'resolve_ask', ask_id: 'C1' })
m = reducer(m, { type: 'resolve_ask', ask_id: 'C3' })
check('全部答完后队列清空、共享窗口一并关闭',
  m.clarifies.length === 0 && m.askWindow === null)
let rp2 = reducer(initialState, { type: 'event', evt: clA })
rp2 = reducer(rp2, { type: 'event', evt: clB })
rp2 = reducer(rp2, { type: 'prune_gates', ids: ['C2'], before: Date.now() + 10000,
                     approvals: true, ask: true })
check('剪枝逐张判定：服务端只留 C2 时，C1 被剪、C2 保留',
  rp2.clarifies.length === 1 && rp2.clarifies[0].ask_id === 'C2',
  JSON.stringify(rp2.clarifies.map((x) => x.ask_id)))
rp2 = reducer(rp2, { type: 'event', evt: ev('ask_resolved', { reason: 'stopped', count: 2 }) })
check('ask_resolved（回合被终止）一次清空所有提问卡 + 窗口',
  rp2.clarifies.length === 0 && rp2.askWindow === null)

// ---------- 回放帧：只补时间线，不许清掉/伪造卡片 ----------
// 初次打开时 since=0 会整环回放，上一回合的 done 就在里面。少了这条守卫，
// 它会把这会儿正挂着的卡清掉 —— 表现正是"刷新后卡片消失，且刷几次才复现"。
const sA = reducer(initialState, { type: 'set_current', sid: 'sessA' })
const withCard2 = reducer(sA, { type: 'event', evt: batchEvt() })
const rp = reducer(withCard2, { type: 'event', evt: ev('done',
  { session_id: 'sessA', content: '上一回合的回复', replayed: true }) })
check('回放帧的旧 done 不清掉正挂着的卡', count(rp) === 1, `count=${count(rp)}`)
check('回放帧的旧 done 仍补时间线（消息进来了）', rp.messages.length >= 1,
  `messages=${rp.messages.length}`)
check('回放帧不改写"此刻"忙闲状态', rp.agent.busy === true, `busy=${rp.agent.busy}`)
const rpErr = reducer(withCard2, { type: 'event', evt: ev('error',
  { session_id: 'sessA', message: '上一回合的错', replayed: true }) })
check('回放帧的旧 error 不清卡、不弹告警、不改忙闲', count(rpErr) === 1, `count=${count(rpErr)}`)
const rpCard = reducer(initialState, { type: 'event', evt: batchEvt({ replayed: true }) })
check('回放帧不建卡（真相在 REST pending，避免僵尸卡复活）', count(rpCard) === 0,
  `count=${count(rpCard)}`)
check('回放帧不建提问卡', reducer(initialState,
  { type: 'event', evt: clEvt({ replayed: true }) }).clarifies.length === 0)
check('直播帧的 done 照旧清场（守卫没把正常路径一起挡掉）',
  count(reducer(withCard2, { type: 'event', evt: ev('done',
    { session_id: 'sessA', content: 'x' }) })) === 0)

// ---------- 回合收尾必须清场（否则卡片永久僵尸） ----------
const done = reducer(withCard, { type: 'event', evt: ev('done', { session_id: 'sessA', content: 'ok' }) })
check('done：回合结束清空审批卡', count(done) === 0, `count=${count(done)}`)
check('done：回合结束清空提问卡', reducer(
  reducer(initialState, { type: 'event', evt: clEvt() }),
  { type: 'event', evt: ev('done', { session_id: 'sessA', content: 'ok' }) }).clarifies.length === 0)
const resolved = reducer(withCard, { type: 'event', evt: ev('approval_batch_resolved',
  { batch_id: 'B1', via: 'answered', approved: ['i1'] }) })
check('裁决事件摘卡', count(resolved) === 0, `count=${count(resolved)}`)

// ---------- 输出 ----------
let bad = 0
console.log('='.repeat(70))
for (const c2 of checks) {
  if (!c2.ok) bad++
  console.log(`${c2.ok ? 'PASS' : 'FAIL'}  ${c2.name}${c2.detail ? ' | ' + c2.detail : ''}`)
}
console.log('='.repeat(70))
console.log(`结论：${checks.length - bad}/${checks.length} 通过`)
process.exit(bad ? 1 : 0)
