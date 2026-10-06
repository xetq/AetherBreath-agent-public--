// 消息导航栏的**界面层**真机验收（headless Edge + 生产前端 + 真网关历史）。
//
// 为什么必须做这一层：竖轨的定位、悬停预览、点击跳转、滚动联动全都是「几何 + 事件」行为，
// tsc 只能证明它编译得过 —— 只有真浏览器里的真 DOM 才算证据。
//
// 覆盖：
//  NAV.1 竖轨存在，且**横杠数 = 界面里真实存在的用户消息卡数**
//        （注意：历史按 limit=400 条截断，「用户消息总条数」可能比界面里的多 ——
//          没渲染出来的消息不该有横杠，点了也没得跳。所以权威是 DOM，不是会话统计。）
//  NAV.2 竖轨贴在对话区右侧边缘（不压住消息文本、纵向不出可视区）
//  NAV.3 悬停弹出**摘要预览**（纯文本、去掉 Markdown、够长会截断、长文不超 4 行）
//  NAV.4 点击 → 平滑滚动到对应消息（滚过了才有效）+ 短暂高亮（1.5s 内会自己消失）
//  NAV.5 滚动联动（scroll-spy）：滚到哪条，对应横杠就高亮
//  NAV.6 短会话（只有 1 条用户消息）不显示竖轨（避免一条杠孤零零挂着）
//
// 跑法（只需网关在线，**不需要 AB 开机/LLM**）：
//   node agent_webui/scripts/verify_msgnav_ui.mjs
//   AETHER_WEBUI_BASE=http://127.0.0.1:8900 可覆盖地址
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const CDP_PORT = Number(process.env.AETHER_CDP_PORT || 9336)
const CDP = `http://127.0.0.1:${CDP_PORT}`
const APP = process.env.AETHER_WEBUI_BASE || 'http://127.0.0.1:8900'
/** 要选的历史会话：默认挑「用户消息最多」的那个（点了才有东西可滚） */
const WANT_SID = process.env.AETHER_SID || ''
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const BROWSERS = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
]

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail: String(detail).slice(0, 400) })
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name} | ${String(detail).slice(0, 400)}`)
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
  const dir = path.join(os.tmpdir(), 'abw-msgnav-ui')
  const child = spawn(exe, ['--headless=new', '--disable-gpu', '--window-size=1280,900',
    `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${dir}`,
    '--no-first-run', '--no-default-browser-check', 'about:blank'], { stdio: 'ignore' })
  for (let i = 0; i < 40; i++) {
    if (await cdpReady()) return child
    await sleep(500)
  }
  child.kill()
  throw new Error('headless 浏览器 20s 内没就绪')
}

/** 给横杠派发"真"鼠标事件（坐标取它自己的中心）—— 比 JS 直接 dispatch 更接近真机 */
const hoverExpr = (idx) => `(() => {
  const d = document.querySelector('[data-nav-idx="${idx}"]');
  if (!d) return { err: 'no-dash' };
  const r = d.getBoundingClientRect();
  const opt = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
  d.dispatchEvent(new MouseEvent('mouseover', opt));
  d.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false, clientX: opt.clientX, clientY: opt.clientY }));
  d.dispatchEvent(new MouseEvent('mousemove', opt));
  return { x: opt.clientX, y: opt.clientY };
})()`

const unhoverExpr = (idx) => `(() => {
  const d = document.querySelector('[data-nav-idx="${idx}"]');
  if (!d) return 1;
  const r = d.getBoundingClientRect();
  const out = { bubbles: true, cancelable: true, clientX: 2, clientY: 2 };
  d.dispatchEvent(new MouseEvent('mouseout', { ...out, relatedTarget: document.body }));
  d.dispatchEvent(new MouseEvent('mouseleave', { bubbles: false, clientX: 2, clientY: 2 }));
  return 1;
})()`

const TIP_SNAP = `(() => {
  const tips = Array.from(document.querySelectorAll('.msgnav-tip'));
  return tips.map((t) => {
    const r = t.getBoundingClientRect();
    const body = t.querySelector('.msgnav-tip-body');
    const cs = body ? getComputedStyle(body) : null;
    return {
      head: (t.querySelector('.msgnav-tip-head')?.innerText || '').trim(),
      body: (body?.innerText || '').trim(),
      bodyH: body ? body.getBoundingClientRect().height : 0,
      lineH: cs ? parseFloat(cs.lineHeight) || 0 : 0,
      rect: { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) },
      right: Math.round(r.right),
    };
  });
})()`

const GEO = `(() => {
  const msgs = document.querySelector('.msgs');
  const nav = document.querySelector('.msgnav');
  if (!msgs) return { err: 'no-msgs' };
  const mr = msgs.getBoundingClientRect();
  const cards = Array.from(document.querySelectorAll('.msg.user[data-msg-turn]'));
  let maxRight = 0, widest = null;
  for (const c of cards) {
    const r = c.getBoundingClientRect();
    if (r.right > maxRight) { maxRight = r.right; widest = c; }
  }
  const nar = nav ? nav.getBoundingClientRect() : null;
  const anchors = cards.map((c) => Number(c.getAttribute('data-msg-turn')));
  return {
    msgs: { left: Math.round(mr.left), right: Math.round(mr.right), top: Math.round(mr.top), bottom: Math.round(mr.bottom), h: Math.round(mr.height) },
    nav: nar ? { left: Math.round(nar.left), right: Math.round(nar.right), top: Math.round(nar.top), bottom: Math.round(nar.bottom), w: Math.round(nar.width), h: Math.round(nar.height) } : null,
    dashes: document.querySelectorAll('.msgnav-dot').length,
    userCards: cards.length,
    anchors: anchors.slice(0, 8).concat(anchors.length > 8 ? ['…', anchors[anchors.length - 1]] : []),
    anchorOk: anchors.length > 0 && anchors.every((v, i) => Number.isInteger(v) && (i === 0 || v > anchors[i - 1])),
    cardsOverflowMaxRight: Math.round(maxRight) > Math.round(mr.right) + 1,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    scroll: { top: Math.round(msgs.scrollTop), max: Math.round(msgs.scrollHeight - msgs.clientHeight) },
  };
})()`

const ACTIVE = `(() => {
  const on = Array.from(document.querySelectorAll('.msgnav-dot.on')).map((d) => d.getAttribute('data-nav-idx'));
  return { on, first: on[0] ?? null, count: on.length };
})()`

const FLASH = `(() => ({
  flashed: Array.from(document.querySelectorAll('.msg-jump-flash')).map((e) => e.getAttribute('data-msg-turn')),
  scrollTop: Math.round(document.querySelector('.msgs')?.scrollTop ?? -1),
}))()`

/** 把某张卡**正对齐到阅读判定线**（视口 34% 处）——这样就能唯一确定"该高亮哪一条"。
 *  用法：想验第 i 条，就把它**后面紧跟的那条**卡对齐到判定线，结论必然是那条。 */
const alignToReadingLine = (idx) => `(() => {
  const msgs = document.querySelector('.msgs');
  const cards = Array.from(document.querySelectorAll('[data-msg-turn]'));
  const pos = cards.findIndex((c) => Number(c.getAttribute('data-msg-turn')) === ${idx});
  const next = cards[pos + 1];
  if (!msgs || !next) return null;
  const line = msgs.getBoundingClientRect().top + msgs.clientHeight * 0.34;
  msgs.scrollTop = msgs.scrollTop + (next.getBoundingClientRect().top - line) + 1;
  return { want: Number(next.getAttribute('data-msg-turn')), scrollTop: Math.round(msgs.scrollTop) };
})()`

/** 独立复算"判定线之上最靠下的那张卡"——与竖轨的实现**各算各的**，用来对表 */
const EXPECT_LIT = `(() => {
  const msgs = document.querySelector('.msgs');
  const top = msgs.getBoundingClientRect().top;
  const probe = msgs.scrollTop + msgs.clientHeight * 0.34;
  const max = msgs.scrollHeight - msgs.clientHeight;
  const cards = Array.from(document.querySelectorAll('[data-msg-turn]'));
  if (msgs.scrollTop >= max - 2) return String(Number(cards[cards.length - 1].getAttribute('data-msg-turn')));
  let cur = Number(cards[0].getAttribute('data-msg-turn'));
  for (const c of cards) {
    if (c.getBoundingClientRect().top - top <= probe) cur = Number(c.getAttribute('data-msg-turn'));
    else break;
  }
  return String(cur);
})()`

let browserChild = null
let ws = null
let sid = null

try {
  // ---- 前置：网关在线（**不需要 AB 开机**：导航栏只依赖历史消息）----
  const health = await (await fetch(`${APP}/api/health`, { signal: AbortSignal.timeout(6000) })).json()
  check('NAV.0 前置：网关在线', !!health.ok || !!health.agent, `health=${JSON.stringify(health).slice(0, 120)}`)

  const list = await (await fetch(`${APP}/api/sessions`, { signal: AbortSignal.timeout(20000) })).json()
  const sessions = list.sessions || []
  const byUser = [...sessions].sort((a, b) => (b.user_turns || 0) - (a.user_turns || 0))
  const rich = WANT_SID ? sessions.find((s) => s.session_id === WANT_SID) : byUser[0]
  const short = [...sessions].sort((a, b) => (a.user_turns || 0) - (b.user_turns || 0))[0]
  if (!rich) throw new Error('网关没有任何会话，无法验收')
  sid = rich.session_id
  check('NAV.0b 选中历史会话（用户消息最多）', (rich.user_turns || 0) >= 2,
    `sid=${sid} user_turns=${rich.user_turns} msgs=${rich.message_count}`)

  browserChild = await ensureBrowser()
  const target = await (await fetch(`${CDP}/json/new?about:blank`, { method: 'PUT' })).json()
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
    setTimeout(() => { if (waiting.has(myId)) { waiting.delete(myId); ok({}) } }, 15000)
  })
  const evaluate = async (expr) => {
    const r = await cdp('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
    if (r.result?.exceptionDetails) return { error: JSON.stringify(r.result.exceptionDetails).slice(0, 300) }
    return r.result?.result?.value
  }

  await cdp('Runtime.enable')
  await cdp('Page.enable')
  await cdp('Page.navigate', { url: APP })
  await sleep(3000)
  // 走生产路径选会话：前端首屏优先接上 localStorage 里「上次停留的会话」（key = abw.current）
  await evaluate(`localStorage.setItem('abw.current', ${JSON.stringify(sid)})`)
  await cdp('Page.reload', {})
  await sleep(6000)

  // ---- NAV.1 竖轨与横杠数 ----
  let geo = await evaluate(GEO)
  if (geo?.error) throw new Error('页面没起来：' + geo.error)
  if (geo.err) throw new Error('页面结构不对：' + geo.err)
  check('NAV.1 竖轨存在', !!geo.nav, `nav=${JSON.stringify(geo.nav)}`)
  // 权威 = 界面里真实渲染出来的用户卡（历史 limit=400 会截断，统计值可能更大）
  check('NAV.1b 横杠数 = 界面里的用户消息卡数（一条消息一杠，不多不少）',
    geo.dashes > 0 && geo.dashes === geo.userCards,
    `dashes=${geo.dashes} userCards=${geo.userCards} api.user_turns=${rich.user_turns}（历史 limit=400，可能大于界面数）`)
  // 反向取证：界面里的用户卡数不能超过会话统计 —— 超了说明凭空多画了
  check('NAV.1b2 界面用户卡数不超过会话统计（没有凭空多画）',
    geo.userCards <= (rich.user_turns || 0),
    `userCards=${geo.userCards} api.user_turns=${rich.user_turns}`)
  check('NAV.1c 每张用户卡都有 DOM 锚点（data-msg-turn 严格递增、各不相同）', geo.anchorOk,
    `anchorOk=${geo.anchorOk} userCards=${geo.userCards} anchors=${JSON.stringify(geo.anchors)}`)

  // ---- NAV.2 贴边几何 ----
  check('NAV.2 竖轨在对话区右侧边缘（右缘对齐、不与消息重叠）',
    !!geo.nav && geo.nav.right > geo.msgs.right - 40 && geo.nav.right <= geo.msgs.right + 4 &&
    geo.nav.left > geo.msgs.left + (geo.msgs.right - geo.msgs.left) * 0.6,
    `nav.right=${geo.nav?.right} msgs.right=${geo.msgs.right} nav.left=${geo.nav?.left}`)
  check('NAV.2b 竖轨纵向不出可视区',
    !!geo.nav && geo.nav.top >= geo.msgs.top - 1 && geo.nav.bottom <= geo.msgs.bottom + 1,
    `nav.top=${geo.nav?.top} nav.bottom=${geo.nav?.bottom} msgs.top=${geo.msgs.top} msgs.bottom=${geo.msgs.bottom}`)
  check('NAV.2c 没有把消息卡挤出容器（布局没有被撑破）', !geo.cardsOverflowMaxRight,
    `overflow=${geo.cardsOverflowMaxRight} viewport=${JSON.stringify(geo.viewport)}`)

  // ---- NAV.3 悬停预览 ----
  // 先拿一个"长消息"和"短消息"的序号，验证截断与长度上限
  const lens = await evaluate(`(() => {
    const out = [];
    document.querySelectorAll('.msgnav-dot').forEach((d) => {
      const t = d.getAttribute('title') || '';
      out.push({ idx: Number(d.getAttribute('data-nav-idx')), len: t.length, hasMark: /[*\`#>]/.test(t) });
    });
    return out;
  })()`)
  const longest = [...(lens || [])].sort((a, b) => b.len - a.len)[0] || { idx: 0 }
  const shortest = [...(lens || [])].sort((a, b) => a.len - b.len)[0] || { idx: 0 }

  await evaluate(hoverExpr(longest.idx))
  await sleep(600)
  let tips = await evaluate(TIP_SNAP)
  check('NAV.3 悬停弹出摘要预览（且只弹一个）',
    Array.isArray(tips) && tips.length === 1,
    `tips=${JSON.stringify(tips).slice(0, 260)}`)
  const tip = (tips || [])[0] || {}
  check('NAV.3b 预览是纯文本（Markdown 记号已去掉）',
    !!tip.body && !/[*\`#>]/.test(tip.body),
    `body="${String(tip.body).slice(0, 120)}"`)
  check('NAV.3c 长消息预览被截断（不超过上限 140 字）',
    (tip.body || '').length <= 141 && (longest.len > 141 ? /…$/.test(tip.body || '') : true),
    `len=${(tip.body || '').length} titleLen=${longest.len} tail="${String(tip.body).slice(-12)}"`)
  check('NAV.3d 预览框不超 4 行正文、不越出视口',
    !!tip.rect && tip.bodyH <= (tip.lineH || 18) * 4 + 2 && tip.rect.x >= 0 && tip.rect.y >= 0 &&
    tip.rect.x + tip.rect.w <= geo.viewport.w,
    `bodyH=${tip.bodyH} lineH=${tip.lineH} x=${tip.rect?.x} w=${tip.rect?.w} viewportW=${geo.viewport.w}`)

  // 换一条短的：预览应该跟着换（不是卡在第一条）
  await evaluate(unhoverExpr(longest.idx))
  await sleep(200)
  await evaluate(hoverExpr(shortest.idx))
  await sleep(600)
  const tips2 = await evaluate(TIP_SNAP)
  check('NAV.3e 换一条悬停 → 预览跟着换（内容与位置都对）',
    Array.isArray(tips2) && tips2.length === 1 && (tips2[0].body || '') !== (tip.body || ''),
    `first="${String(tip.body).slice(0, 40)}" second="${String((tips2[0] || {}).body).slice(0, 40)}"`)
  await evaluate(unhoverExpr(shortest.idx))
  await sleep(400)
  const tips3 = await evaluate(TIP_SNAP)
  check('NAV.3f 移开鼠标 → 预览消失（不留残影）',
    Array.isArray(tips3) && tips3.length === 0, `tips=${JSON.stringify(tips3)}`)

  // ---- NAV.4 点击跳转 ----
  // 先滚到底：这样第 0 条一定在视口外，点它才能看出"真的滚过去了"
  await evaluate(`(() => { const m = document.querySelector('.msgs'); m.scrollTop = m.scrollHeight; return 1 })()`)
  await sleep(700)
  const before = await evaluate(GEO)
  const activeAtBottom = await evaluate(ACTIVE)
  const lastAnchor = await evaluate(`(() => {
    const c = Array.from(document.querySelectorAll('[data-msg-turn]'));
    return String(Number(c[c.length - 1].getAttribute('data-msg-turn')));
  })()`)
  check('NAV.4 滚到底时高亮最后一条（scroll-spy 的边界）',
    activeAtBottom.count === 1 && String(activeAtBottom.first) === lastAnchor,
    `on=${JSON.stringify(activeAtBottom)} lastAnchor=${lastAnchor}`)

  const c0 = await evaluate(`(() => {
    const d = document.querySelector('[data-nav-idx="0"]');
    if (!d) return null;
    const r = d.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  })()`)
  const clickRes = await evaluate(`(() => {
    const d = document.querySelector('[data-nav-idx="0"]');
    if (!d) return { err: 'no-dash' };
    const r = d.getBoundingClientRect();
    const o = { bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    d.dispatchEvent(new MouseEvent('mousedown', o));
    d.dispatchEvent(new MouseEvent('mouseup', o));
    d.click();
    return { ok: true };
  })()`)
  const flashNow = await evaluate(FLASH)
  check('NAV.4b 点击立即高亮对应消息卡（不需要等滚动结束）',
    (flashNow.flashed || []).includes('0'),
    `click=${JSON.stringify(clickRes)} flashed=${JSON.stringify(flashNow.flashed)}`)
  const activeAfterClick = await evaluate(ACTIVE)
  check('NAV.4c 点击后横杠高亮跟到被点的那条',
    String(activeAfterClick.first) === '0', `on=${JSON.stringify(activeAfterClick.on)}`)

  let arrived = null
  for (let i = 0; i < 30; i++) {
    await sleep(300)
    const g = await evaluate(GEO)
    if (g && g.scroll.top < before.scroll.top - 100) { arrived = g; break }
  }
  check('NAV.4d 对话区平滑滚动到第 0 条（scrollTop 显著减小）',
    !!arrived, `before=${before.scroll.top} after=${arrived ? arrived.scroll.top : '未下降'}`)
  // 高亮是"短暂"的：要等过 FLASH_MS(1500ms) 再看。注意别用 click 后紧接着的时刻去判 ——
  // 那会儿高亮**本该还在**（这正是它要显示的窗口）。
  await sleep(2200)
  const flashLater = await evaluate(FLASH)
  check('NAV.4e 高亮是"短暂"的：1.5 秒后自己消失（不留残余状态）',
    !(flashLater.flashed || []).includes('0'),
    `flashed=${JSON.stringify(flashLater.flashed)}`)
  const activeSettled = await evaluate(ACTIVE)
  const expectAfterJump = await evaluate(EXPECT_LIT)
  check('NAV.4f 跳转收口后横杠高亮与"判定线"一致（没有回弹、没有乱跳）',
    String(activeSettled.first) === String(expectAfterJump),
    `on=${JSON.stringify(activeSettled.on)} expected=${expectAfterJump}`)

  // ---- NAV.5 滚动联动（scroll-spy）----
  // 判据不写死序号：把某张卡**正对齐到阅读判定线**，那条就必须被点亮 ——
  // 这样"该亮哪条"是由几何唯一决定的，不是拿"我以为是第几条"去套。
  const anchors = await evaluate(`Array.from(document.querySelectorAll('[data-msg-turn]')).map((c) => Number(c.getAttribute('data-msg-turn')))`)
  const t5 = anchors[Math.min(3, anchors.length - 2)]
  const align = await evaluate(alignToReadingLine(t5))
  let spy5 = null
  let spyMid = null
  let expected5 = null
  for (let i = 0; i < 20; i++) {
    await sleep(300)
    spy5 = await evaluate(ACTIVE)
    spyMid = await evaluate(GEO)
    expected5 = await evaluate(EXPECT_LIT)
    if (spy5 && spy5.on.length === 1 && String(spy5.first) === String(expected5)) break
  }
  check('NAV.5 滚到"第 N 条之上" → 竖轨点亮的正是判定线上那条（与独立复算一致）',
    !!spy5 && spy5.on.length === 1 && String(spy5.first) === String(expected5),
    `align=${JSON.stringify(align)} on=${JSON.stringify(spy5 && spy5.on)} expected=${expected5} scrollTop=${spyMid && spyMid.scroll.top}`)
  // 再换一条对齐，确认"高亮确实跟着滚动走"（而不是碰巧对上一次）
  const t5b = anchors[Math.min(6, anchors.length - 2)]
  await evaluate(alignToReadingLine(t5b))
  let spy5b = null
  let expected5b = null
  for (let i = 0; i < 20; i++) {
    await sleep(300)
    spy5b = await evaluate(ACTIVE)
    expected5b = await evaluate(EXPECT_LIT)
    if (spy5b && String(spy5b.first) === String(expected5b)) break
  }
  check('NAV.5b 换一个滚动位置 → 高亮跟着变（连续两次都对，不是碰巧）',
    !!spy5b && spy5b.on.length === 1 && String(spy5b.first) === String(expected5b) &&
    String(expected5b) !== String(expected5),
    `on=${JSON.stringify(spy5b && spy5b.on)} expected=${expected5b} 上一处=${expected5} scrollTop=${spyMid && spyMid.scroll.top}`)

  // ---- NAV.5c 轨道自动滚动**不许把整页一起滚**（scrollIntoView 会滚所有可滚动祖先）----
  // 判据：把对话滚到"高亮跑出轨道可视范围"的位置，轨道该自己挪，而**页面/顶栏/轨道框都不许动**。
  const railBefore = await evaluate(`(() => {
    const n = document.querySelector('.msgnav');
    const r = n.getBoundingClientRect();
    window.scrollTo(0, 0);
    return { top: Math.round(r.top), right: Math.round(r.right), h: Math.round(r.height),
             pageY: Math.round(window.scrollY), docTop: Math.round(document.querySelector('.chat-head').getBoundingClientRect().top) };
  })()`)
  // 跳到很靠后的一条，足以让高亮条远离轨道当前可视窗口
  await evaluate(alignToReadingLine(anchors[Math.max(0, anchors.length - 3)]))
  await sleep(1200)
  const railAfter = await evaluate(`(() => {
    const n = document.querySelector('.msgnav');
    const r = n.getBoundingClientRect();
    return { top: Math.round(r.top), right: Math.round(r.right), h: Math.round(r.height),
             pageY: Math.round(window.scrollY), docTop: Math.round(document.querySelector('.chat-head').getBoundingClientRect().top),
             railScrollTop: Math.round(n.scrollTop), railMax: Math.round(n.scrollHeight - n.clientHeight),
             activeInView: (() => {
               const d = n.querySelector('.msgnav-dot.on');
               if (!d) return null;
               const dr = d.getBoundingClientRect();
               return dr.top >= r.top - 2 && dr.bottom <= r.bottom + 2;
             })() };
  })()`)
  check('NAV.5c 轨道自动滚动只动轨道：整页/顶栏/轨道框都纹丝不动',
    railAfter.pageY === railBefore.pageY && railAfter.top === railBefore.top &&
    railAfter.docTop === railBefore.docTop && railAfter.right === railBefore.right,
    `before=${JSON.stringify(railBefore)} after=${JSON.stringify(railAfter)}`)
  check('NAV.5d 高亮条始终在轨道可视范围内（自动带入视野真的生效）',
    railAfter.activeInView === true && railAfter.railScrollTop > 0,
    `activeInView=${railAfter.activeInView} railScrollTop=${railAfter.railScrollTop} max=${railAfter.railMax}`)

  // ---- NAV.6 短会话不显示 ----
  if (short && (short.user_turns || 0) <= 1) {
    await evaluate(`localStorage.setItem('abw.current', ${JSON.stringify(short.session_id)})`)
    await cdp('Page.reload', {})
    // 等它真的把会话换过来（用户卡数变了 / 变 0 了）再判，别拿旧 DOM 当结论
    let g6 = null
    for (let i = 0; i < 25; i++) {
      await sleep(400)
      g6 = await evaluate(GEO)
      if (g6 && !g6.err && g6.userCards <= 1) break
    }
    check('NAV.6 只有 1 条用户消息的会话不显示竖轨（避免一条杠孤零零）',
      !!g6 && !g6.err && g6.nav === null && g6.userCards <= 1,
      `sid=${short.session_id} nav=${JSON.stringify(g6 && g6.nav)} userCards=${g6 && g6.userCards} user_turns=${short.user_turns}`)
  } else {
    check('NAV.6 短会话判据（当前没有 ≤1 条用户消息的会话，跳过）', true,
      `sessions=${sessions.length} 最少 user_turns=${short ? short.user_turns : 'n/a'}`)
  }
} catch (e) {
  check('脚本异常', false, String((e && e.message) || e))
} finally {
  const ok = results.filter((r) => r.ok).length
  console.log(`\n==== 消息导航栏界面层：${ok === results.length ? '全部通过' : '有失败'} (${ok}/${results.length}) ====`)
  try {
    const out = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')),
      'verify_msgnav_ui.result.json')
    fs.writeFileSync(out, JSON.stringify({ session_id: sid, passed: ok, total: results.length, results }, null, 2), 'utf-8')
    console.log('结果落盘：' + out)
  } catch (e) { console.log('结果落盘失败：' + e.message) }
  try { ws?.close() } catch { /* ignore */ }
  if (browserChild) browserChild.kill()
  process.exit(ok === results.length ? 0 : 1)
}
