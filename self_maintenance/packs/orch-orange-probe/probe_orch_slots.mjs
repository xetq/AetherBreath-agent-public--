// 探针（编排器点阵 / 运行时）：**格子数恒定**，亮点只能把暗点点亮。
//
// 主人 2026-09-30 定的视觉契约：
//   "任务编排器亮起点，应该是在暗点的基础上点亮，而非多加橙色点。"
// 而代码里恰好有两处会**新增**格子：
//   ① `put()`：线程号超出上报容量时"补一个槽位"；
//   ② 认不出槽位的跨回合作业单独补一格（P5 加的 loose 分支）。
// 于是"16 格 + 10 个作业"有可能被画成"16 格 + 若干额外橙点"。
//
// 跑法（node 即可；esbuild/react 从 frontend/node_modules 取）：
//     node self_maintenance/packs/orch-orange-probe/probe_orch_slots.mjs
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const ROOT = (() => {
  let p = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))
  for (let i = 0; i < 8; i++) {
    if (fs.existsSync(path.join(p, 'agent_webui'))) return p
    p = path.dirname(p)
  }
  throw new Error('找不到项目根（agent_webui 在哪？）')
})()
const FRONTEND = path.join(ROOT, 'agent_webui', 'frontend')
const require = createRequire(path.join(FRONTEND, 'package.json'))
const esbuild = require('esbuild')

const STUB = path.join(os.tmpdir(), 'ab_persist_stub2.mjs')
fs.writeFileSync(STUB, 'export const loadUsage = () => ({ bySid: {}, models: {} })\n' +
                       'export const saveUsage = () => {}\n' +
                       'export const loadText = () => ""\n' +
                       'export const saveText = () => {}\n' +
                       'export const usageSeq = () => 0\n' +
                       'export const debounce = (f) => f\n')

const built = await esbuild.build({
  entryPoints: [path.join(FRONTEND, 'src', 'components', 'OrchStrip.tsx')],
  bundle: true, format: 'esm', platform: 'node', write: false,
  absWorkingDir: FRONTEND, nodePaths: [path.join(FRONTEND, 'node_modules')],
  define: { 'process.env.NODE_ENV': '"development"' },
  plugins: [{ name: 'stub-persist', setup(b) { b.onResolve({ filter: /lib\/persist$/ }, () => ({ path: STUB })) } }],
})
const tmp = path.join(os.tmpdir(), 'ab_orch_slots_' + Date.now() + '.mjs')
fs.writeFileSync(tmp, built.outputFiles[0].text)
const mod = await import(pathToFileURL(tmp).href)
const buildSlots = mod.buildSlots

const RESULTS = []
const check = (name, fn) => {
  try { fn(); RESULTS.push([true, name, '']) }
  catch (e) { RESULTS.push([false, name, String((e && e.message) || e)]) }
}

const N_SER = 1, N_PAR = 16
const GRID = N_SER + N_PAR                     // 17：1 串行 + 16 并行
const pipe = (o) => Object.assign({
  tool: 'execute_shell', status: 'running', layer: 1, at: Date.now() / 1000,
  thread: null, elapsed: null, background: false, job_id: null, job_label: null,
}, o)

// 真机那一屏：一次挂 10 个 15 秒作业（16 个并行槽位，线程 0~9）
const tenJobs = {}
for (let i = 0; i < 10; i++) {
  tenJobs['tc' + i] = pipe({ thread: 'orch-parallel_' + i, background: true,
                             job_id: 'j' + (i + 1), job_label: 'execute_shell(sleep 15)' })
}

check('1 空载：格子数 = 池容量（1 串 + 16 并），一个都不亮', () => {
  const { slots, unmapped } = buildSlots({}, [], true, N_SER, N_PAR)
  if (slots.length !== GRID) throw new Error('格子数=' + slots.length + '（应为 ' + GRID + '）')
  if (slots.some((s) => s.busy)) throw new Error('空载却有亮点')
  if (unmapped !== 0) throw new Error('空载却报了未映射')
})

check('2 十个后台作业：还是在 16 格里亮 10 格（不新增格子）', () => {
  const { slots, unmapped } = buildSlots(tenJobs, [], true, N_SER, N_PAR)
  if (slots.length !== GRID) throw new Error('格子数=' + slots.length + '（多了橙点）')
  const lit = slots.filter((s) => s.busy).length
  if (lit !== 10) throw new Error('亮点数=' + lit + '（应为 10）')
  if (slots.filter((s) => s.job).length !== 10) throw new Error('橙色格数不对')
  if (unmapped !== 0) throw new Error('不该有未映射')
})

check('3 认不出槽位的作业：占一个空闲暗格，绝不新增格子', () => {
  const pipes = Object.assign({}, tenJobs, {
    tcX: pipe({ thread: null, background: true, job_id: 'jX', job_label: '排队中' }),
  })
  const { slots, unmapped } = buildSlots(pipes, [], true, N_SER, N_PAR)
  if (slots.length !== GRID) throw new Error('格子数=' + slots.length + '（新增了橙点）')
  const lit = slots.filter((s) => s.busy).length
  if (lit !== 11) throw new Error('亮点数=' + lit + '（应为 11：10 个在册 + 1 个未映射占格）')
  if (unmapped !== 0) throw new Error('有空闲格时不该报未映射')
  if (!slots.some((s) => s.loose)) throw new Error('未映射的那格没被标成 loose（hover 说不清）')
})

check('4 线程号超出上报容量：照样不许新增格子', () => {
  const pipes = Object.assign({}, tenJobs, {
    tcOver: pipe({ thread: 'orch-parallel_20', background: true, job_id: 'jO' }),
  })
  const { slots } = buildSlots(pipes, [], true, N_SER, N_PAR)
  if (slots.length !== GRID) throw new Error('格子数=' + slots.length + '（超出容量就补格子 = 主人看到的多余橙点）')
  if (!slots.some((s) => s.loose)) throw new Error('超出容量的那个没被吸收进空闲格')
})

check('5 池满：16 格全占 + 8 个排队的，只报"未映射"，绝不画到格子外', () => {
  const pipes = {}
  for (let i = 0; i < N_PAR; i++) {                     // 16 个真占着槽位
    pipes['tc' + i] = pipe({ thread: 'orch-parallel_' + i, background: true, job_id: 'j' + i })
  }
  for (let i = 0; i < 8; i++) {                         // 8 个在排队（没有槽位 = thread 为空）
    pipes['q' + i] = pipe({ thread: null, background: true, job_id: 'q' + i })
  }
  const { slots, unmapped } = buildSlots(pipes, [], true, N_SER, N_PAR)
  if (slots.length !== GRID) throw new Error('格子数=' + slots.length)
  const litPar = slots.filter((s) => s.busy && s.pool === 'parallel').length
  if (litPar !== N_PAR) throw new Error('并行格应全亮，实际 ' + litPar)
  if (unmapped !== 8) throw new Error('排队中的作业没有被如实计数（应为 8，实际 ' + unmapped + '）')
})

check('6 已结束/空闲的管道不点亮任何格子', () => {
  const pipes = {
    a: pipe({ thread: 'orch-parallel_0', status: 'done' }),
    b: pipe({ thread: 'orch-parallel_1', status: 'idle' }),
    c: pipe({ thread: null, status: 'idle' }),
  }
  const { slots, unmapped } = buildSlots(pipes, [], true, N_SER, N_PAR)
  if (slots.some((s) => s.busy)) throw new Error('空闲/结束的被点亮了')
  if (unmapped !== 0) throw new Error('空闲的不该计成未映射')
})

check('7 串行池的作业落在最左边那格（不是并行格）', () => {
  const pipes = { s: pipe({ thread: 'orch-serial_0', background: true, job_id: 'jS' }) }
  const { slots } = buildSlots(pipes, [], true, N_SER, N_PAR)
  const first = slots[0]
  if (first.pool !== 'serial' || !first.busy) throw new Error('串行作业没落在串行格上')
})

check('8 恒定契约：任何输入下格子数都等于池容量', () => {
  const weird = {
    a: pipe({ thread: 'orch-parallel_99' }), b: pipe({ thread: null }),
    c: pipe({ thread: 'orch-parallel_-1' }), d: pipe({ thread: 'garbage' }),
    e: pipe({ thread: 'orch-serial_7', background: true }),
  }
  for (const [p, ser, par] of [[{}, 1, 16], [weird, 1, 16], [tenJobs, 1, 16], [tenJobs, 2, 4]]) {
    const { slots } = buildSlots(p, [], true, ser, par)
    if (slots.length !== ser + par) {
      throw new Error('输入 %o -> 格子数 %d（应为 %d）'.replace('%o', Object.keys(p).join(',') || '空')
        .replace('%d', String(slots.length)).replace('%d', String(ser + par)))
    }
  }
})

check('9 颜色污染：槽位跑过后台作业后，再跑前台并行任务必须回到绿色', () => {
  // 真机现象（主人 2026-10-02）：某一格跑过后台作业（橙）之后，**再在这个槽位跑并行任务，
  // 亮起的还是橙色**，绿色（并行任务）再也回不来。
  // 构造就是真机那一屏：陈旧的后台条目还留在视图里（store 会把它落回 idle，但保留 background），
  // 而同一个线程（= 同一个槽位）正在跑一条前台任务。
  const pipes = {
    old1: pipe({ thread: 'orch-parallel_3', background: true, job_id: 'j7',
                 job_label: 'execute_shell(sleep 15)', status: 'idle' }),
    cur: pipe({ thread: 'orch-parallel_3', background: false, status: 'running' }),
  }
  const { slots } = buildSlots(pipes, [], true, N_SER, N_PAR)
  const s = slots.find((x) => x.key === 'parallel#3')
  if (!s.busy) throw new Error('当前在跑的前台任务没点亮这一格')
  if (s.busy.status !== 'running') throw new Error('槽位没接到"当前在跑"的那一条')
  if (s.job) throw new Error('这一格被"以前跑过后台作业"污染成橙色了 —— 并行任务该是绿色')
})

check('10 顺序无关：陈旧的后台条目排在后面也不许污染', () => {
  const pipes = {
    cur: pipe({ thread: 'orch-parallel_4', background: false, status: 'running' }),
    old1: pipe({ thread: 'orch-parallel_4', background: true, job_id: 'j8', status: 'done' }),
  }
  const { slots } = buildSlots(pipes, [], true, N_SER, N_PAR)
  const s = slots.find((x) => x.key === 'parallel#4')
  if (s.job) throw new Error('陈旧的橙色条目排在后面照样污染了槽位（说明判据不是"当前那条"）')
})

check('11 反向判据：当前在跑的**就是**后台作业时，仍必须是橙色', () => {
  const pipes = {
    old1: pipe({ thread: 'orch-parallel_5', background: false, status: 'done' }),
    cur: pipe({ thread: 'orch-parallel_5', background: true, job_id: 'j9', status: 'running' }),
  }
  const { slots } = buildSlots(pipes, [], true, N_SER, N_PAR)
  const s = slots.find((x) => x.key === 'parallel#5')
  if (!s.busy || !s.job) throw new Error('正在跑的后台作业没有画成橙色（修过头了）')
})

fs.rmSync(tmp, { force: true })
let ok = 0
for (const [good, name, why] of RESULTS) {
  console.log('  ' + (good ? '[PASS] ' : '[FAIL] ') + name + (good ? '' : '  <- ' + why))
  ok += good ? 1 : 0
}
console.log('\n探针结论：' + (ok === RESULTS.length ? '通过' : '未通过') + ' (' + ok + '/' + RESULTS.length + ')')
process.exit(ok === RESULTS.length ? 0 : 1)
