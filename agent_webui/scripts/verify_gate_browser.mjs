// 真机回归（浏览器侧）：卡片恢复的完整前端链路 —— 从 HTTP 响应到 DOM。
//
// 覆盖四个现象：切会话再回来 / 刷新 / 新建后返回 / 关页面再打开。
// 做法：在页面里把 /api/approval/pending 的响应替换成一张「服务端找回的卡」，
// 然后走真实的切会话与刷新流程，断言卡片真的出现在屏幕上。
// 这不是 mock 业务逻辑：React、store、reducer、组件、路由全是生产代码，
// 只换了那一条 HTTP 响应 —— 因为"服务端此刻真有一张卡"正是本回归要造的前置。
//
// 跑法（先起网关 webui.bat，无需 AB 开机、不花 LLM 额度）：
//   node agent_webui/scripts/verify_gate_browser.mjs
// 自己会拉起一个 headless Edge（用完关掉）。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const CDP_PORT = Number(process.env.AETHER_CDP_PORT || 9333)
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
  if (!exe) throw new Error('找不到 Edge/Chrome，无法跑浏览器回归：' + BROWSERS.join(' / '))
  const dir = path.join(os.tmpdir(), 'abw-verify-cdp')
  const child = spawn(exe, ['--headless=new', '--disable-gpu',
    `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${dir}`,
    '--no-first-run', '--no-default-browser-check', 'about:blank'],
    { stdio: 'ignore' })
  for (let i = 0; i < 40; i++) {
    if (await cdpReady()) return child
    await sleep(500)
  }
  child.kill()
  throw new Error('headless 浏览器 20s 内没就绪')
}

let browserChild = null
let ws = null
let bad = 0
try {
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
    setTimeout(() => { if (waiting.has(myId)) { waiting.delete(myId); ok({}) } }, 8000)
  })
  const evaluate = async (expr) => {
    const r = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true })
    if (r.result?.exceptionDetails) return { error: JSON.stringify(r.result.exceptionDetails) }
    return r.result?.result?.value
  }

  const results = []
  const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail })

  // 把恢复端点换成「有一张卡」的响应。卡片挂在当前会话名下，另配一张归属别的
  // 会话的对照卡由切换动作产生（本探针只造一张，归属提示靠切会话来验证）。
  const PATCH = (sid) => `(() => {
    if (window.__abwProbe) return 'already';
    window.__abwProbe = true;
    const real = window.fetch;
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const json = (o) => new Response(JSON.stringify(o),
        { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/approval/pending')) {
        return json({ ok: true, authoritative: true, error: '', cards: [{
          type: 'approval_batch', batch_id: 'PROBE_B1', ask_id: 'PROBE_B1',
          timeout: 300, remaining: 250, total: 1, session_id: ${JSON.stringify(sid)},
          scopes: [{ key: 'once', label: '仅批准本次', emoji: '1' }],
          accepts_note: false, note_hint: '', restored: true,
          items: [{ ask_id: 'PROBE_I1', kind: 'write_file', risk: 2, title: 'probe',
                    intent: 'PROBE-INTENT-MARK', reason: '由回归探针注入',
                    paths: ['C:/probe.txt'], notes: ['probe note'],
                    critical: false, source_code: 'open(...)' }]
        }] });
      }
      if (url.includes('/ask/pending')) {
        return json({ ok: true, authoritative: true, error: '', cards: [{
          type: 'ask_request', ask_id: 'PROBE_C1', question: 'PROBE-QUESTION-MARK',
          options: ['yes', 'no'], mode: 'choice', session_id: ${JSON.stringify(sid)},
          timeout: 120, remaining: 100, restored: true
        }] });
      }
      return real(input, init);
    };
    return 'patched';
  })()`

  const snapshot = `({
    approval: !!document.querySelector('.approval'),
    approvalText: (document.querySelector('.approval')?.innerText || '').replace(/\\s+/g, ' ').slice(0, 220),
    ask: !!document.querySelector('.ask'),
    askText: (document.querySelector('.ask')?.innerText || '').replace(/\\s+/g, ' ').slice(0, 160),
    foreign: !!document.querySelector('.approval-foreign'),
    restoreBadge: /由服务端找回/.test(document.querySelector('.approval')?.innerText || ''),
    countdown: (document.querySelector('.approval')?.innerText.match(/剩\\s*(\\d+)s/) || [])[1] || null,
    items: document.querySelectorAll('.side-item').length,
    current: (document.querySelector('.side-item.on .sid-id')?.innerText || '').trim(),
  })`

  await cdp('Runtime.enable')
  await cdp('Page.enable')
  await cdp('Page.navigate', { url: APP })
  await sleep(7000)

  const before = await evaluate(snapshot)
  if (before && before.error) throw new Error('页面没起来：' + before.error)
  check('网关可达且界面已加载', !!before && typeof before.items === 'number',
    JSON.stringify(before).slice(0, 100))
  check('加载瞬间没有残留卡片（干净基线）', !before?.approval && !before?.ask)
  check('侧栏有会话可切换', (before?.items || 0) >= 2, `items=${before?.items}`)
  const sid0 = before?.current
  check('已定位当前会话 id（探针把卡片挂在它名下）', !!sid0, String(sid0))

  // ---- 场景 A：服务端有卡 → 切走（现象1 的前半） ----
  const p1 = await evaluate(PATCH(sid0))
  check('探针已接管恢复端点', p1 === 'patched', String(p1))
  const clickOther = await evaluate(`(() => {
    const items = document.querySelectorAll('.side-item');
    for (const it of items) {
      const sid = (it.querySelector('.sid-id')?.innerText || '').trim();
      if (sid && sid !== ${JSON.stringify(sid0)}) { it.click(); return sid; }
    }
    return 'none';
  })()`)
  await sleep(3000)
  const a = await evaluate(snapshot)
  check('现象1/3：切会话后卡片由服务端找回并渲染', a.approval,
    `approval=${a.approval} switched=${clickOther}`)
  check('卡片内容完整（intent 渲染出来了）', a.approvalText.includes('PROBE-INTENT-MARK'),
    a.approvalText.slice(0, 90))
  check('卡面标注「由服务端找回」', a.restoreBadge, a.approvalText.slice(0, 90))
  check('恢复卡倒计时按剩余秒数续算（≈250 而不是满格 300）',
    Number(a.countdown) > 0 && Number(a.countdown) <= 255, `left=${a.countdown}`)
  check('ask_user 提问卡同样恢复', a.ask && a.askText.includes('PROBE-QUESTION-MARK'),
    a.askText.slice(0, 80))
  check('卡片归属别的会话时有明确提示（不靠删卡"理顺"视图）', a.foreign)

  // ---- 场景 B：切回卡片所属会话 → 卡片必须还在，归属提示消失 ----
  const back = await evaluate(`(() => {
    const items = document.querySelectorAll('.side-item');
    for (const it of items) {
      const sid = (it.querySelector('.sid-id')?.innerText || '').trim();
      if (sid === ${JSON.stringify(sid0)}) { it.click(); return sid; }
    }
    return 'none';
  })()`)
  await sleep(3000)
  const b = await evaluate(snapshot)
  check('现象1：切回去卡片仍在（不再被 set_current 删掉）', b.approval,
    `back=${back} approval=${b.approval}`)
  check('切回卡片所属会话后归属提示消失', !b.foreign, `foreign=${b.foreign}`)

  // ---- 场景 C：刷新页面（现象2）/ 重开页面（现象4） ----
  // patch 必须在「新文档的脚本执行之前」就位，否则页面挂载那一刻拿到的还是真实
  // （空）响应 —— 那就成了在测"注入太晚"，而不是在测恢复通道。
  await cdp('Page.addScriptToEvaluateOnNewDocument', { source: PATCH(sid0) })
  await cdp('Page.reload')
  await sleep(8000)
  const c = await evaluate(snapshot)
  check('现象2/4：刷新后卡片由服务端快照找回（页面重建 + 挂载即恢复）', c.approval,
    `approval=${c.approval} left=${c.countdown}`)
  check('现象2/4：刷新找回的卡仍带完整内容与倒计时',
    c.approvalText.includes('PROBE-INTENT-MARK') && Number(c.countdown) > 0,
    `left=${c.countdown}`)
  check('现象2/4：刷新后 ask_user 提问卡也找回',
    c.ask && c.askText.includes('PROBE-QUESTION-MARK'), c.askText.slice(0, 70))

  console.log('='.repeat(72))
  for (const r of results) {
    if (!r.ok) bad++
    console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? ' | ' + r.detail : ''}`)
  }
  console.log('='.repeat(72))
  console.log(`结论：${results.length - bad}/${results.length} 通过（真实浏览器 + 生产前端代码）`)
} catch (e) {
  bad = 1
  console.error('探针失败：', e && e.message ? e.message : e)
} finally {
  try { ws?.close() } catch { /* 忽略 */ }
  if (browserChild) { try { browserChild.kill() } catch { /* 忽略 */ } }
}
process.exit(bad ? 1 : 0)
