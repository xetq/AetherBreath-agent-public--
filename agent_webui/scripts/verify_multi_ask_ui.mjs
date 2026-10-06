// 提问多卡的**界面层**真机验收（headless Edge + 生产前端 + 真回合）。
//
// 交付形态是**逐张**：AB 一次问 N 个独立问题，但屏幕上**永远只有一张卡** ——
// 答完这张，下一张自动顶上来（后端仍把这一批的答案一起收回）。
// 所以这里最关键的断言是「任何时刻都不超过 1 张卡」和「答完自动换下一张」。
//
// 覆盖：
//  UI.1 屏幕上只有一张卡（不是把三个问题一起挤上来）
//  UI.2 卡片提示还剩几个问题
//  UI.3 答完 → 下一张自动顶上（内容变了，张数仍是 1）
//  UI.4 再答 → 第三张顶上
//  UI.5 答完最后一张 → 队列清空（卡消失）
//  UI.6 整批答案一起回到模型（回复里三个答案都在）
//  UI.7 刷新后无残留卡
//
// 跑法（需网关已起、AB 已开机；会花 LLM 额度）：
//   node agent_webui/scripts/verify_multi_ask_ui.mjs
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const CDP_PORT = Number(process.env.AETHER_CDP_PORT || 9335)
const CDP = `http://127.0.0.1:${CDP_PORT}`
const APP = process.env.AETHER_WEBUI_BASE || 'http://127.0.0.1:8900'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]

const PROMPT = '请立刻并行调用 ask_user 三次 —— 在同一条消息里发出三个工具调用，分别问：' +
  '1) 更想喝哪种咖啡？（选项：美式、拿铁）2) 今天想听什么音乐？（选项：民谣、摇滚）' +
  '3) 周末更想去哪？（选项：图书馆、公园）三个问题同时问、不要等回答。' +
  '收齐三个答案后，用一行把三个答案都复述出来。'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} | ${String(detail).slice(0, 300)}`)
}

async function cdpReady() {
  try {
    const r = await fetch(`${CDP}/json/version`, { signal: AbortSignal.timeout(1500) })
    return r.ok
  } catch { return false }
}

async function ensureBrowser() {
  if (await cdpReady()) return null
  const exe = BROWSERS.find((p) => fs.existsSync(p))
  if (!exe) throw new Error('找不到 Edge/Chrome：' + BROWSERS.join(' / '))
  const dir = path.join(os.tmpdir(), 'abw-multiclar-ui')
  const child = spawn(exe, ['--headless=new', '--disable-gpu',
    `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${dir}`,
    '--no-first-run', '--no-default-browser-check', 'about:blank'], { stdio: 'ignore' })
  for (let i = 0; i < 40; i++) {
    if (await cdpReady()) return child
    await sleep(500)
  }
  child.kill()
  throw new Error('headless 浏览器 20s 内没就绪')
}

const setInput = (text) => `(() => {
  const ta = document.querySelector('.composer textarea');
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return ta.value.length;
})()`

// 只看屏幕上**当前那一张**卡 —— 逐张交付的判据就靠这个
const snap = `(() => {
  const cards = Array.from(document.querySelectorAll('.ask'));
  const c0 = cards[0];
  return {
    count: cards.length,
    head: (c0?.querySelector('.ask-head')?.innerText || '').replace(/\\n/g, ' ').trim(),
    question: (c0?.querySelector('.ask-q')?.innerText || '').trim().slice(0, 30),
    countdown: (c0?.querySelector('.ask-count')?.innerText || '').trim(),
    options: Array.from(c0?.querySelectorAll('.opt') || []).map((e) => e.innerText.trim()),
    busy: !!document.querySelector('.msg.assistant.live'),
    lastAssistant: (Array.from(document.querySelectorAll('.msg.assistant:not(.live)')).pop()?.innerText || '').replace(/\\n/g, ' ').slice(0, 400),
  };
})()`

const clickOpt = (label) => `(() => {
  const card = document.querySelector('.ask');
  if (!card) return 'no-card';
  const opts = Array.from(card.querySelectorAll('.opt'));
  const hit = ${JSON.stringify(label)} ? opts.find((o) => o.innerText.trim() === ${JSON.stringify(label)}) : opts[0];
  if (!hit) return 'no-opt';
  const t = hit.innerText.trim();
  hit.click();
  return t;
})()`

let browserChild = null
let ws = null
let sid = null

try {
  const health = await (await fetch(`${APP}/api/health`, { signal: AbortSignal.timeout(6000) })).json()
  const agent = health.agent || {}
  check('UI.0 前置：网关在线且 AB 已开机', agent.phase === 'ON', `phase=${agent.phase}`)
  if (agent.phase !== 'ON') throw new Error('AB 未开机')

  const created = await (await fetch(`${APP}/api/sessions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prefix: 'multiui', title: '提问逐张界面验收' }),
  })).json()
  sid = created.session_id
  check('UI.0b 新建验收会话', !!sid, `sid=${sid}`)

  browserChild = await ensureBrowser()
  const res = await fetch(`${CDP}/json/new?about:blank`, { method: 'PUT' })
  const target = await res.json()
  ws = new WebSocket(target.webSocketDebuggerUrl)
  await new Promise((ok, err) => { ws.onopen = ok; ws.onerror = err })

  let id = 0
  const waiting = new Map()
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data)
    if (m.id && waiting.has(m.id)) { waiting.get(m.id)(m); waiting.delete(m.id) }
  }
  const cdp = (method, params = {}) => new Promise((ok) => {
    const myId = ++id
    waiting.set(myId, ok)
    ws.send(JSON.stringify({ id: myId, method, params }))
    setTimeout(() => { if (waiting.has(myId)) { waiting.delete(myId); ok({}) } }, 10000)
  })
  const evaluate = async (expr) => {
    const r = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
    if (r.result?.exceptionDetails) return { error: JSON.stringify(r.result.exceptionDetails).slice(0, 300) }
    return r.result?.result?.value
  }

  await cdp('Runtime.enable')
  await cdp('Page.enable')
  await cdp('Page.navigate', { url: APP })
  await sleep(4000)
  await evaluate(`localStorage.setItem('abw.current', ${JSON.stringify(sid)})`)
  await cdp('Page.reload', {})
  await sleep(5000)

  await evaluate(setInput(PROMPT))
  await sleep(400)
  await evaluate(`(() => { document.querySelector('.composer-acts .btn.primary').click(); return 1 })()`)

  // ---- 轮询期间一直盯着"同时最多几张"：逐张交付就不该出现 2 张 ----
  let s = { count: 0, head: '' }
  let peak = 0
  for (let i = 0; i < 150; i++) {
    s = await evaluate(snap)
    if (s && typeof s.count === 'number') {
      peak = Math.max(peak, s.count)
      if (s.count === 1 && /还有 2 个问题/.test(String(s.head))) break
    }
    await sleep(1000)
  }
  check('UI.1 屏幕上只有一张卡（三个问题没有一起挤上来）', peak === 1 && s.count === 1,
    `peak=${peak} now=${s.count} head="${s.head}"`)
  check('UI.2 卡片告诉你还剩几个问题', /还有 2 个问题/.test(String(s.head)), `head="${s.head}"`)
  const q1 = s.question
  const t1 = s.countdown
  check('UI.2b 卡上有倒计时（共享窗口的真值，不是满格重置）', /\d+s/.test(String(t1)) || t1.includes('超出窗口'),
    `countdown="${t1}"`)

  // ---- 逐个答：每答一次，下一张顶上 ----
  const a1 = await evaluate(clickOpt(''))
  await sleep(2500)
  s = await evaluate(snap)
  check('UI.3 答完第一张 → 下一张自动顶上（张数仍是 1，问题换了）',
    s.count === 1 && s.question && s.question !== q1,
    `clicked=${a1} count=${s.count} 上题="${q1}" 现题="${s.question}"`)
  check('UI.3b 顶部提示改为「还有 1 个问题」', /还有 1 个问题/.test(String(s.head)), `head="${s.head}"`)
  const q2 = s.question

  const a2 = await evaluate(clickOpt(''))
  await sleep(2500)
  s = await evaluate(snap)
  check('UI.4 答完第二张 → 第三张顶上',
    s.count === 1 && s.question && s.question !== q2,
    `clicked=${a2} count=${s.count} 现题="${s.question}"`)
  check('UI.4b 只剩一个时不再显示「还有」（不啰嗦）',
    !/还有/.test(String(s.head)) && /需要你确认/.test(String(s.head)), `head="${s.head}"`)

  const a3 = await evaluate(clickOpt(''))
  await sleep(2500)
  s = await evaluate(snap)
  check('UI.5 答完最后一张 → 卡消失、队列清空', s.count === 0,
    `clicked=${a3} count=${s.count}`)

  // ---- 等回合真正收尾（live 卡消失）再读最终回复 ----
  for (let i = 0; i < 150; i++) {
    s = await evaluate(snap)
    if (s.count === 0 && !s.busy) break
    await sleep(1000)
  }
  await sleep(2500)
  s = await evaluate(snap)
  const reply = String(s.lastAssistant || '')
  check('UI.6 整批答案一起回到模型（回复里三个答案都在）',
    (reply.includes('美式') || reply.includes('拿铁'))
    && (reply.includes('民谣') || reply.includes('摇滚'))
    && (reply.includes('图书馆') || reply.includes('公园')), reply.slice(0, 260))
  check('UI.6b 全程没有任何时刻出现超过一张卡', peak === 1, `peak=${peak}`)

  // ---- 刷新后无残留 ----
  await cdp('Page.reload', {})
  await sleep(6000)
  s = await evaluate(snap)
  check('UI.7 刷新后无残留提问卡（回合收尾已清场）', s.count === 0, `count=${s.count}`)
} catch (e) {
  check('脚本异常', false, String((e && e.message) || e))
} finally {
  const ok = results.filter((r) => r.ok).length
  console.log(`\n==== 提问逐张界面层：${ok === results.length ? '全部通过' : '有失败'} (${ok}/${results.length}) ====`)
  try {
    const out = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
      'verify_multi_ask_ui.result.json')
    fs.writeFileSync(out, JSON.stringify({ session_id: sid, passed: ok, total: results.length, results }, null, 2), 'utf-8')
    console.log('结果落盘：' + out)
  } catch (e) { console.log('结果落盘失败：' + e.message) }
  try { ws?.close() } catch { /* ignore */ }
  if (browserChild) browserChild.kill()
  process.exit(ok === results.length ? 0 : 1)
}
