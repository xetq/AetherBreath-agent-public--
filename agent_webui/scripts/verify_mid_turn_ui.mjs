// 中期交互的**界面层**真机验收（headless Edge + 生产前端 + 真后端 + 真回合）。
//
// 为什么必须做这一层：紫色按钮、空回车不动作、消息三态徽标全都是「渲染 + 事件」的行为，
// reducer 单测和 HTTP 端到端都测不到 —— 只有真浏览器里的真 DOM 才算证据。
//
// 覆盖：
//  UI.1 空闲态：按钮是绿色「发送 ↵」
//  UI.2 回合跑起来：按钮变红「⏹ 停止本回合」
//  UI.3 **空录入回车无作用**（防误触停止：回合必须还在跑）
//  UI.4 有录入：按钮变紫「⤴ 发送（用户交代）」（class 含 btn mid）—— 三种语义三色
//  UI.5 回车即发送：消息以「用户交代」身份入列 + 挂「待送达」徽标
//  UI.6 送达回执回流：徽标转「✅ 已随工具返回注入」
//  UI.7 AB 回复带上口令（模型真收到）
//
// 跑法（需网关已起、AB 已开机；会花一次 LLM 额度）：
//   node agent_webui/scripts/verify_mid_turn_ui.mjs
// 自己会拉起一个 headless Edge（用完关掉）。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const CDP_PORT = Number(process.env.AETHER_CDP_PORT || 9334)
const CDP = `http://127.0.0.1:${CDP_PORT}`
const APP = process.env.AETHER_WEBUI_BASE || 'http://127.0.0.1:8900'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]

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
  const dir = path.join(os.tmpdir(), 'abw-midturn-ui')
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

// 慢工具：给界面留一段确定的长窗口（约 11 秒）做 DOM 检查
const SLOW_PROMPT = '请只用 execute_shell 执行一条命令：ping 127.0.0.1 -n 12 。' +
  '跑完后用一句话告诉我结果，不要调用其它工具。'
const TOKEN = '赤霄断水'

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail: String(detail).slice(0, 300) })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} | ${String(detail).slice(0, 300)}`)
}

let browserChild = null
let ws = null
let sid = null

// ---- 受控输入：必须走 native setter + input 事件，直接改 value React 不认 ----
const setInput = (text) => `(() => {
  const ta = document.querySelector('.composer textarea');
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return ta.value;
})()`

const hitEnter = `(() => {
  const ta = document.querySelector('.composer textarea');
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
  return true;
})()`

const snap = `(() => {
  const btn = document.querySelector('.composer-acts .btn');
  const textarea = document.querySelector('.composer textarea');
  return {
    btnText: (btn?.innerText || '').trim(),
    btnClass: btn?.className || '',
    taValue: textarea?.value ?? null,
    placeholder: textarea?.placeholder || '',
    midMsgs: Array.from(document.querySelectorAll('.msg.user.mid')).map((e) => e.innerText.replace(/\\n/g, ' ')),
    badges: Array.from(document.querySelectorAll('.mid-badge')).map((e) => e.innerText.trim()),
    lastAssistant: (Array.from(document.querySelectorAll('.msg.assistant')).pop()?.innerText || '').replace(/\\n/g, ' ').slice(0, 200),
  };
})()`

try {
  // ---- 前置：网关 + 开机状态 ----
  const health = await (await fetch(`${APP}/api/health`, { signal: AbortSignal.timeout(6000) })).json()
  const agent = health.agent || {}
  check('UI.0 前置：网关在线且 AB 已开机', agent.phase === 'ON', `phase=${agent.phase}`)
  if (agent.phase !== 'ON') throw new Error('AB 未开机，先点「🟢 开机」再跑本脚本')
  if (!(agent.injected || []).includes('mid_turn')) throw new Error('bridge 未载入 mid_turn 通道（需重启 AB）')

  const created = await (await fetch(`${APP}/api/sessions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prefix: 'midui', title: '中期交互UI验收' }),
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
  // 走生产路径选会话：前端首屏优先接上 localStorage 里「上次停留的会话」
  await evaluate(`localStorage.setItem('abw.current', ${JSON.stringify(sid)})`)
  await cdp('Page.reload', {})
  await sleep(5000)

  // ---- UI.1 空闲态 ----
  let s = await evaluate(snap)
  if (s?.error) throw new Error('页面没起来：' + s.error)
  check('UI.1 空闲态按钮为绿色「发送 ↵」',
    s.btnText.includes('发送 ↵') && s.btnClass.includes('primary'), `btn="${s.btnText}" cls="${s.btnClass}"`)

  // ---- 触发真实回合（点绿色发送，走真交互）----
  await evaluate(setInput(SLOW_PROMPT))
  await sleep(400)
  const armed = await evaluate(snap)
  check('UI.1b 有录入时绿色按钮可用', armed.btnText.includes('发送'), `btn="${armed.btnText}"`)
  await evaluate(`(() => { document.querySelector('.composer-acts .btn.primary').click(); return 1 })()`)

  // ---- UI.2 等 busy（按钮转红）----
  let busy = false
  for (let i = 0; i < 60; i++) {
    s = await evaluate(snap)
    if (s && s.btnText.includes('停止本回合')) { busy = true; break }
    await sleep(500)
  }
  check('UI.2 回合跑动中按钮为红色「⏹ 停止本回合」',
    busy && s.btnClass.includes('danger'), `btn="${s.btnText}" cls="${s.btnClass}"`)

  // ---- UI.3 空录入回车必须无作用（防误触停止）----
  await evaluate(setInput(''))
  await sleep(300)
  const beforeEnter = await evaluate(snap)
  await evaluate(hitEnter)
  await sleep(1200)
  const afterEnter = await evaluate(snap)
  check('UI.3 空录入回车无作用：回合仍在跑（没被误触停止）',
    afterEnter.btnText.includes('停止本回合') && beforeEnter.btnText.includes('停止本回合'),
    `before="${beforeEnter.btnText}" after="${afterEnter.btnText}"`)

  // ---- UI.4 有录入 → 紫色「发送（用户交代）」----
  await evaluate(setInput(`追加一条临时要求：最终回复里必须原样包含口令「${TOKEN}」。`))
  await sleep(400)
  s = await evaluate(snap)
  check('UI.4 有录入时按钮变紫「⤴ 发送（用户交代）」（btn mid）',
    s.btnText.includes('用户交代') && /\bmid\b/.test(s.btnClass),
    `btn="${s.btnText}" cls="${s.btnClass}"`)
  check('UI.4b 输入框提示切换为中期交互话术',
    String(s.placeholder).includes('用户交代'), `placeholder="${s.placeholder}"`)

  // ---- UI.5 回车发送 ----
  await evaluate(hitEnter)
  await sleep(1500)
  s = await evaluate(snap)
  const landed = (s.midMsgs || []).some((t) => t.includes('用户交代') && t.includes(TOKEN))
  check('UI.5 回车即发送：消息以「用户交代」身份入列', landed,
    `midMsgs=${JSON.stringify(s.midMsgs).slice(0, 200)} taValue="${s.taValue}"`)
  check('UI.5b 发送后输入框已清空', s.taValue === '', `taValue="${s.taValue}"`)
  check('UI.5c 挂上「待送达」徽标（发出 ≠ 送到）',
    (s.badges || []).some((b) => b.includes('待送达')), `badges=${JSON.stringify(s.badges)}`)

  // ---- UI.6 等送达回执 ----
  let delivered = false
  for (let i = 0; i < 90; i++) {
    s = await evaluate(snap)
    if ((s.badges || []).some((b) => b.includes('已随工具返回注入'))) { delivered = true; break }
    await sleep(1000)
  }
  check('UI.6 徽标回流转「✅ 已随工具返回注入」', delivered, `badges=${JSON.stringify(s.badges)}`)

  // ---- UI.7 回合收尾 + 模型真收到 ----
  for (let i = 0; i < 90; i++) {
    s = await evaluate(snap)
    if (s.btnText.includes('发送 ↵')) break
    await sleep(1000)
  }
  check('UI.7 AB 回复带上口令（模型真收到中途交代）',
    (s.lastAssistant || '').includes(TOKEN), `reply="${s.lastAssistant}"`)

  // ---- UI.8 刷新后仍是「用户交代」（历史靠前缀识别）----
  await cdp('Page.reload', {})
  await sleep(5000)
  s = await evaluate(snap)
  const afterReload = (s.midMsgs || []).some((t) => t.includes('用户交代') && t.includes(TOKEN))
  check('UI.8 刷新后历史里仍标为「用户交代」且已送达', afterReload,
    `midMsgs=${JSON.stringify(s.midMsgs).slice(0, 160)} badges=${JSON.stringify(s.badges)}`)
} catch (e) {
  check('脚本异常', false, String(e && e.message || e))
} finally {
  const ok = results.filter((r) => r.ok).length
  console.log(`\n==== 中期交互界面层：${ok === results.length ? '全部通过' : '有失败'} (${ok}/${results.length}) ====`)
  try {
    const out = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
      'verify_mid_turn_ui.result.json')
    fs.writeFileSync(out, JSON.stringify({ session_id: sid, passed: ok, total: results.length, results },
      null, 2), 'utf-8')
    console.log('结果落盘：' + out)
  } catch (e) { console.log('结果落盘失败：' + e.message) }
  try { ws?.close() } catch { /* ignore */ }
  if (browserChild) browserChild.kill()
  process.exit(ok === results.length ? 0 : 1)
}
