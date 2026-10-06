// 验收脚本：进行中回合的"磁盘快照补洞"（2026-10-02 修两个持久化 bug）
//
// 修的是什么：
//   1. 刷新 / 切会话 / 关掉再开 → 本回合**已经产出**的中期输出与思考当场消失，
//      要等下一个回合收尾才回来；
//   2. 回合**进行中**看不到思考内容，只有收尾后刷新/切会话才看得到。
//
// 两条根因：
//   · bridge 的 sidecar 只在**回合收尾**写盘 → 进行中那半个回合磁盘上没有；
//   · api.ts 的 SSE 监听表漏了 'reasoning' → 思考事件浏览器端根本不派发。
//   （第 2 条由 tests/test_frontend_contracts.py 守着；本脚本守第 1 条 + turns.ts 的合并口径。）
//
// 用法：node agent_webui/scripts/check_turns.mjs
// 判据：退出码 0 = 全过。
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const FRONTEND = join(HERE, '..', 'frontend')
const TSC = join(FRONTEND, 'node_modules', 'typescript', 'bin', 'tsc')

let pass = 0, fail = 0
const ok = (cond, msg) => {
  if (cond) { pass++; console.log('  ✅ ' + msg) }
  else { fail++; console.log('  ❌ ' + msg) }
}

// turns.ts 只 import type（编译后被完全剥离），所以单文件编译即可直接跑。
const out = mkdtempSync(join(tmpdir(), 'ab-turns-'))
let buildTurns, cleanOuts
try {
  execFileSync(process.execPath, [TSC, join(FRONTEND, 'src', 'lib', 'turns.ts'),
    '--target', 'ES2020', '--module', 'ES2020', '--moduleResolution', 'bundler',
    // rootDir 钉在 src/：turns.ts 会 import type '../../types'（types.ts），
    // rootDir 必须把它们都罩住，否则 tsc 报 TS6059。产物落在 out/lib/turns.js。
    '--rootDir', join(FRONTEND, 'src'),
    '--outDir', out, '--skipLibCheck'], { stdio: 'inherit' })
  const mod = await import(pathToFileURL(join(out, 'lib', 'turns.js')).href)
  buildTurns = mod.buildTurns
  cleanOuts = mod.cleanOuts
} catch (e) {
  console.error('编译 turns.ts 失败：' + e.message)
  process.exit(2)
}

// 会话文件里本回合的两轮：第 1 轮的 reasoning 被上下文管理器清空了（真机实测有这个形态）
const msgs = [
  { role: 'user', content: '做一个 MV' },
  { role: 'assistant', content: '先读交接报告', reasoning: '第一轮思考',
    tool_calls: [{ id: 'c1', name: 'read_file', args: '{}', result: 'ok', failed: false, pending: false }] },
  { role: 'assistant', content: '', reasoning: '',
    tool_calls: [{ id: 'c2', name: 'execute_python', args: '{}', result: 'ok', failed: false, pending: false }] },
]
const NO_PARTIAL = { active: false, rounds: [], timeline: [], streaming: null }

console.log('【1】刷新后进行中回合：只有磁盘快照、没有实时事件（bug 1+2 的主场景）')
{
  const partial = {
    user_seq: 0, run_id: 'r1', ended: 'live', partial: true,
    reasoning: { '0': '第一轮思考', '1': '第二轮思考（会话文件里已被清空）' },
    outs: [
      { round: 0, text: '[中期进度]: 先读交接报告' },
      { round: 1, text: '[中期进度]: 开始写场景' },
      { round: 1, text: '续行也要在' },
    ],
  }
  const t = buildTurns(msgs, [], { ...NO_PARTIAL, partial: [partial] }).at(-1)
  ok(!!t, '切出了回合')
  ok(t.rounds.length === 2, '铺回 2 轮，快照没凭空多加空轮')
  ok(t.rounds[0].think === '第一轮思考', '第 0 轮思考来自快照')
  ok(t.rounds[1].think === '第二轮思考（会话文件里已被清空）',
     '第 1 轮思考由快照补齐（消息里那条是空的 —— 这正是"思考消失"的洞）')
  ok(t.rounds[1].outs.length === 1 && t.rounds[1].outs[0] === '开始写场景\n续行也要在',
     '中期输出剥 [中期进度] 前缀 + 续行并回一段')
  ok(t.rounds[0].outs.length === 1 && t.rounds[0].outs[0] === '先读交接报告', '输出归到了正确的轮')
  ok(t.ended === 'live', "ended='live'（界面据此知道本回合还在跑，过程块默认展开）")
  // live 的语义是"**这个回合还在跑**"（不是"实时通道已连上"）：快照能匹配上本回合
  // 就说明它确实在跑 —— 界面该显示"思考中…"，而不是 endedText 那种中断口的文案。
  ok(t.live === true, "live=true（本回合在跑 → 显示'思考中…'，不是'没有最终输出'）")
}

console.log('【2】实时正在进行：不能被旧快照盖住')
{
  const partial = { user_seq: 0, run_id: 'r1', ended: 'live', partial: true,
    reasoning: { '1': '旧快照的思考' },
    outs: [{ round: 1, text: '[中期进度]: 旧快照的输出' }] }
  const live = { active: true, timeline: [], streaming: null, partial: [partial],
    rounds: [
      { reasoning: '实时第0轮', toolIds: [], outs: ['[中期进度]: 实时输出0'] },
      { reasoning: '实时第1轮', toolIds: [], outs: ['[中期进度]: 实时输出1'] },
    ] }
  const t = buildTurns(msgs, [], live).at(-1)
  ok(t.rounds[1].think === '实时第1轮', '实时思考未被旧快照覆盖')
  ok(t.rounds[1].outs[0] === '实时输出1', '实时输出未被旧快照覆盖')
}

console.log('【3】实时只到了一半：空的那轮用快照补')
{
  const partial = { user_seq: 0, run_id: 'r1', ended: 'live', partial: true,
    reasoning: { '0': '快照第0轮' },
    outs: [{ round: 0, text: '[中期进度]: 快照输出0' }] }
  const live = { active: true, timeline: [], streaming: null, partial: [partial],
    rounds: [
      { reasoning: '', toolIds: [], outs: [] },                      // 刷新后重连，前面的实时事件已丢
      { reasoning: '实时第1轮', toolIds: [], outs: ['[中期进度]: 实时输出1'] },
    ] }
  const t = buildTurns(msgs, [], live)[0]
  // 优先级口径（实施就是按这个来的）：**这一轮 live > messages > snapshot**。
  // 第 0 轮 messages 里有（"第一轮思考"/"先读交接报告"），所以轮不到快照 —— 这是对的：
  // 快照只在 messages **空**的时候才需要顶上（见【1】第 1 轮那种被压缩清空的形态）。
  ok(t.rounds[0].think === '第一轮思考', '第 0 轮用 messages 的（快照只在 messages 空时顶）')
  ok(t.rounds[0].outs[0] === '先读交接报告', '第 0 轮中期输出用 messages 的')
  ok(t.rounds[1].think === '实时第1轮', '有实时数据的第 1 轮用实时的')
}

console.log('【4】没有 partial（老 bridge / 回合已收尾）→ 行为必须与从前逐字一致')
{
  const t = buildTurns(msgs, [], NO_PARTIAL)[0]
  ok(t.rounds.length === 2, '仍是 2 轮')
  ok(t.rounds[0].think === '第一轮思考', '思考仍来自 messages')
  ok(t.ended === 'unknown', 'ended 仍是 unknown（无 sidecar）')
  ok(t.live === false, 'live=false')
}

console.log('【5】★ 真实故障态：刷新后 busy 已恢复（active=true）但 liveTurns 还是空的')
{
  // 这是主人实际遇到的形态：网关先把 busy/turnOwner 恢复回来，实时事件要等重连后才有。
  // 旧写法在这一步把 rounds 整个换成空 liveTurns，再"补空轮"已无从补起 → 中期进度消失。
  const partial = {
    user_seq: 0, run_id: 'r1', ended: 'live', partial: true,
    reasoning: { '0': '第一轮思考', '1': '第二轮思考（会话文件里已被清空）' },
    outs: [
      { round: 0, text: '[中期进度]: 先读交接报告' },
      { round: 1, text: '[中期进度]: 开始写场景' },
    ],
  }
  const t = buildTurns(msgs, [], { active: true, rounds: [], timeline: [], streaming: null,
                                   partial: [partial] }).at(-1)
  const outs = t.rounds.reduce((n, r) => n + r.outs.length, 0)
  ok(t.rounds.length === 2, 'active=true + liveTurns 空 → 仍然铺回 2 轮（不是 0 轮）')
  ok(outs === 2, `中期进度段 = ${outs}（旧实现在这里是 0，就是主人看到的消失）`)
  ok(t.rounds[1].think === '第二轮思考（会话文件里已被清空）', '思考也一并补回')
  ok(t.ended === 'live', "ended='live'（过程块默认展开）")
  ok(t.live === true, 'live=true（实时在跑，界面该显示"思考中/工具执行中"）')
}

console.log('【6】错位护栏：快照属于别的回合时，宁可不补也不缝错')
{
  const partial = {
    user_seq: 0, run_id: 'r-old', ended: 'live', partial: true,   // 上一条消息的快照
    reasoning: { '0': '上一条的思考' },
    outs: [{ round: 0, text: '[中期进度]: 上一条的进度' }],
  }
  // 消息里已经有 2 条用户输入 → 末段序号 = 1，而快照 user_seq=0 → 不匹配
  const twoTurns = [...msgs, { role: 'user', content: '再改一版' }]
  const live = { active: true, rounds: [{ reasoning: '本回合思考', toolIds: [], outs: [] }],
                 timeline: [], streaming: null, partial: [partial] }
  const t = buildTurns(twoTurns, [], live).at(-1)
  ok(t.rounds.length === 1 && t.rounds[0].think === '本回合思考',
     '不匹配的快照被丢弃，只保留本回合的实时内容（不把上一条的进度缝进来）')
}

console.log('【8】★ active=true 但没有实时轮（切到别的会话/turnOwner 还指着别人）→ 一个字都不许改')
{
  // 这个 fixture 的**最后一条 assistant 有正文**（= 最终输出），才能验证"不会被清掉"
  const withFinal = [...msgs, { role: 'assistant', content: '这是已经产出的最终输出',
                                reasoning: '', tool_calls: [] }]
  const t = buildTurns(withFinal, [], { active: true, rounds: [], timeline: [], streaming: null }).at(-1)
  const tools = t.rounds.reduce((n, r) => n + r.tools.length, 0)
  ok(t.rounds.length === 3, `消息里的 3 轮原样保留（旧实现会变成 0 轮，实际 ${t.rounds.length}）`)
  ok(tools === 2, `工具调用原样保留：${tools} 个（旧实现会变成 0 —— 主人看到的"工具不展现"）`)
  ok(t.rounds[0].think === '第一轮思考', '思考原样保留')
  ok(t.final === '这是已经产出的最终输出',
     '最终输出**没有被清成 null**（旧实现会清掉它并画成"思考中…"）')
  ok(t.live === false && t.ended === 'unknown',
     '没有实时数据就不许谎报 live（旧的会置 live=true）')
}

console.log('【9】★ active=true + 实时有轮但工具事件还没到（timeline 空）→ 工具要落在那一轮上')
{
  const withTools = [
    { role: 'user', content: '查两件事' },
    { role: 'assistant', content: '', reasoning: '',
      tool_calls: [
        { id: 'k1', name: 'time_weather', args: '{}', result: '晴', failed: false, pending: false },
        { id: 'k2', name: 'search', args: '{}', result: 'ok', failed: false, pending: false },
      ] },
  ]
  // 实时只攒到第 0 轮的思考（工具事件还没来 → timeline 里没有 k1/k2）
  const live = { active: true, timeline: [], streaming: null,
                 rounds: [{ reasoning: '实时思考', toolIds: [], outs: ['[中期进度]: 我先查天气'] }] }
  const t = buildTurns(withTools, [], live)[0]
  ok(t.rounds.length === 1, '只有 1 轮')
  ok(t.rounds[0].tools.length === 2, `工具没丢：${t.rounds[0].tools.length} 个（旧实现这里会变 0）`)
  ok(t.rounds[0].think === '实时思考', '思考用实时的')
  ok(t.rounds[0].outs[0] === '我先查天气', '中期输出用实时的')
}

console.log('【10】流式草稿：还没有正式事件时，思考与正文要先看得见')
{
  // 这是"边想边看"的核心：新一轮（消息里还没有的那一轮）只有 delta 攒的草稿，
  // 正式事件还没来。优先级 = 实时正式 > 消息里的 > 流式草稿。
  const live = {
    active: true, timeline: [], streaming: null,
    rounds: [
      // 第 0 轮：消息里有正式的，应该赢过草稿
      { reasoning: '', toolIds: [], outs: [], thinkDraft: '草稿不该赢' },
      // 第 1 轮：消息里没有（新一轮），只有草稿 —— 这就是"边想边看"要显示的东西
      { reasoning: '', toolIds: [], outs: [],
        thinkDraft: '正在想：先看天气再定行程',
        textDraft: '我去查一下青岛今天的天气…' },
    ],
  }
  const t = buildTurns(msgs, [], live).at(-1)
  ok(t.rounds[0].think === '第一轮思考', '消息里有思考时，草稿不许覆盖它')
  ok(t.rounds[1].think === '正在想：先看天气再定行程',
     '新一轮的思考用流式草稿顶上（不用等整轮生成完）')
  const last = t.rounds[1].outs[t.rounds[1].outs.length - 1] || ''
  ok(last === '我去查一下青岛今天的天气…', '正在生成的正文显示为最后一段中期输出')

  // 正式事件到达后，草稿必须让位（否则会在界面上显示两遍）
  const live2 = {
    active: true, timeline: [], streaming: null,
    rounds: [{
      reasoning: '正式思考全文', toolIds: [], outs: ['[中期进度]: 正式进度'],
      thinkDraft: '正在想：先看天气再定行程', textDraft: '我去查一下青岛今天的天气…',
    }],
  }
  const t2 = buildTurns(msgs, [], live2).at(-1)
  ok(t2.rounds[0].think === '正式思考全文', '正式思考覆盖草稿')
  ok(!t2.rounds[0].outs.includes('我去查一下青岛今天的天气…'),
     '有正式 outs 时草稿不再重复追加')
}

console.log('【11】cleanOuts 仍是唯一清洗口径（实时与磁盘必须逐字一致）')
{
  const got = cleanOuts(['噪音行', '[中期进度]: A', 'B', '[中期进度]: A'], '')
  ok(got.length === 2 && got[0] === 'A\nB' && got[1] === 'A',
     '段前噪音丢弃 / 剥前缀 / 续行并回一段')
  ok(cleanOuts(['[中期进度]: 同一段思考'], '同一段思考').length === 0,
     '与同轮思考相同的段被去掉（避免"思考块 + 白字"重复）')
}

try { rmSync(out, { recursive: true, force: true }) } catch { /* 临时目录清不掉不影响结论 */ }
console.log(`\n结果：${pass} 通过 / ${fail} 失败`)
process.exit(fail ? 1 : 0)
