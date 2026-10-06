// 探针（前端运行时）：**真跑一遍 reducer**，验证橙点的三条前端判据。
//
// 为什么需要它：源码级断言（`tests/test_orch_orange_job.py` 里那几条）只能证明
// "代码里写了这个词"，证明不了"豁免条件没写反"。橙点熄灭正是这类错 ——
// 所以这里把 `store/appStore.tsx` 用 esbuild 打成 ESM、在 node 里直接喂事件。
//
// 跑法（node 在 PATH 里即可；esbuild/react 从 frontend/node_modules 取）：
//     node self_maintenance/packs/orch-orange-probe/probe_store_runtime.mjs
//
// 判据：
//   1) 回合 `done` 之后，**正在跑的跨回合作业**那一格必须还是 running（橙点不灭）
//   2) 同一时刻，普通（本回合）管道照旧被收成 idle（不能因为豁免把该收的也留下）
//   3) `merge_orch`：authoritative=false 时**一个字都不许改**（看不见 ≠ 不存在）
//   4) `merge_orch`：authoritative=true 时覆盖/补齐；作业结算后那格落回 idle
//   5) pipeline 事件必须把 `background/job_id/job_label` 带进 pipes（显式挑字段的老坑）
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
// `--store <路径>` 可以换成别的 appStore.tsx（例如快照里的 before 前像）跑对照
const argv = process.argv.slice(2)
const si = argv.indexOf('--store')
const STORE_PATH = si >= 0 && argv[si + 1]
  ? path.resolve(argv[si + 1])
  : path.join(FRONTEND, 'src', 'store', 'appStore.tsx')
const require = createRequire(path.join(FRONTEND, 'package.json'))
const esbuild = require('esbuild')

const STUB = path.join(os.tmpdir(), 'ab_persist_stub.mjs')
fs.writeFileSync(STUB, 'export const loadUsage = () => ({ bySid: {}, models: {} })\n' +
                       'export const saveUsage = () => {}\n' +
                       'export const loadText = () => ""\n' +
                       'export const saveText = () => {}\n' +
                       'export const usageSeq = () => 0\n' +
                       'export const debounce = (f) => f\n')

const built = await esbuild.build({
  // 可选 `--store <别的 appStore.tsx>`：跑"修前/修后"对照（快照的 before 前像）
  entryPoints: [STORE_PATH],
  bundle: true, format: 'esm', platform: 'node', write: false,
  absWorkingDir: FRONTEND,
  nodePaths: [path.join(FRONTEND, 'node_modules')],
  define: { 'process.env.NODE_ENV': '"development"' },
  plugins: [{
    name: 'stub-persist',
    setup(b) { b.onResolve({ filter: /lib\/persist$/ }, () => ({ path: STUB })) },
  }],
})
const tmp = path.join(os.tmpdir(), 'ab_store_probe_' + Date.now() + '.mjs')
fs.writeFileSync(tmp, built.outputFiles[0].text)
const { reducer, initialState } = await import(pathToFileURL(tmp).href)

const RESULTS = []
const check = (name, fn) => {
  try { fn(); RESULTS.push([true, name, '']) }
  catch (e) { RESULTS.push([false, name, String(e && e.message || e)]) }
}
const ev = (n, evt) => ({ type: 'event', evt: { hub_seq: n, ...evt } })

const PIPE_BG = { tc_id: 'tc-bg', tool: 'execute_shell', status: 'running', layer: 1,
                  thread: 'orch-parallel_3', background: true,
                  job_id: 'j1', job_label: 'execute_shell(sleep 30 && echo BG30_OK)' }
const PIPE_FG = { tc_id: 'tc-fg', tool: 'read_file', status: 'running', layer: 1,
                  thread: 'orch-serial_0', background: false }

// 先把"回合进行中"这一刻搭出来
let s = reducer(initialState, ev(1, { type: 'pipeline', ...PIPE_BG }))
s = reducer(s, ev(2, { type: 'pipeline', ...PIPE_FG }))

check('5 pipeline 事件把作业身份带进 pipes', () => {
  const p = s.pipes['tc-bg']
  if (!p) throw new Error('管道没进 state.pipes')
  if (p.background !== true) throw new Error('background 丢了（显式挑字段的老坑）')
  if (p.job_id !== 'j1') throw new Error('job_id 丢了')
  if (!p.job_label) throw new Error('job_label 丢了')
})

const afterDone = reducer(s, ev(3, { type: 'done', session_id: 's1', content: '已挂上 j1' }))

check('1 回合 done 之后跨回合作业那格仍然 running（橙点不灭）', () => {
  const p = afterDone.pipes['tc-bg']
  if (!p) throw new Error('整条管道被删了')
  if (p.status !== 'running') throw new Error('status=' + p.status + '（橙点会灭）')
  if (p.job_id !== 'j1') throw new Error('作业身份被 done 抹掉了')
})

check('2 同一时刻普通管道照旧被收成 idle（豁免没放宽）', () => {
  const p = afterDone.pipes['tc-fg']
  if (!p) throw new Error('普通管道不该消失')
  if (p.status !== 'idle') throw new Error('status=' + p.status + '（该收没收）')
})

check('3 merge_orch 非权威时一个字都不改', () => {
  const before = JSON.stringify(afterDone.pipes)
  const out = reducer(afterDone, { type: 'merge_orch', pipes: [], authoritative: false })
  if (JSON.stringify(out.pipes) !== before) throw new Error('非权威快照改动了本地状态')
})

check('4a merge_orch 权威：服务端条目覆盖（含 elapsed）', () => {
  const out = reducer(afterDone, { type: 'merge_orch', authoritative: true,
    pipes: [{ tc_id: 'tc-bg', tool: 'execute_shell', status: 'running', layer: 1,
              thread: 'orch-parallel_3', background: true, job_id: 'j1',
              job_label: 'L', elapsed: 12.4 }] })
  const p = out.pipes['tc-bg']
  if (p.status !== 'running' || p.elapsed !== 12.4) throw new Error('没覆盖：' + JSON.stringify(p))
  if (out.pipes['tc-fg'].status !== 'idle') throw new Error('服务端没报的条目该落回空闲')
})

check('4b merge_orch 权威：作业结算后那格落回 idle（橙点该灭就灭）', () => {
  const out = reducer(afterDone, { type: 'merge_orch', authoritative: true, pipes: [] })
  const p = out.pipes['tc-bg']
  if (!p) throw new Error('条目不该凭空消失（还要显示"上次 xx"）')
  if (p.status !== 'idle') throw new Error('status=' + p.status + '（橙点永远灭不掉）')
})

check('4c merge_orch 权威：认不出槽位的作业也能补进 pipes', () => {
  const out = reducer(afterDone, { type: 'merge_orch', authoritative: true,
    pipes: [{ tc_id: 'tc-live', tool: 'execute_shell', status: 'running', background: true,
              thread: null, job_id: 'j9', job_label: 'L9', elapsed: 3.1 }] })
  const p = out.pipes['tc-live']
  if (!p || p.background !== true || p.thread !== null) throw new Error('没补进来：' + JSON.stringify(p))
})

check('5 作业的终态帧（done + background）把状态收成 done', () => {
  const out = reducer(afterDone, ev(4, { type: 'pipeline', tc_id: 'tc-bg',
    tool: 'execute_shell', status: 'done', thread: 'orch-parallel_3',
    background: true, job_id: 'j1', job_label: 'L', elapsed: 30.2 }))
  const p = out.pipes['tc-bg']
  if (p.status !== 'done') throw new Error('status=' + p.status)
  if (p.background !== true) throw new Error('终态帧丢了 background（橙点的来源就断了）')
})

fs.rmSync(tmp, { force: true })
let ok = 0
for (const [good, name, why] of RESULTS) {
  console.log('  ' + (good ? '[PASS] ' : '[FAIL] ') + name + (good ? '' : '  <- ' + why))
  ok += good ? 1 : 0
}
console.log('\n探针结论：' + (ok === RESULTS.length ? '通过' : '未通过') +
            ' (' + ok + '/' + RESULTS.length + ')')
process.exit(ok === RESULTS.length ? 0 : 1)
