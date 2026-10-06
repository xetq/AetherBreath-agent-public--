// 本地延续层：刷新页面后接得上——当前会话、每会话的输入草稿。
// 命名空间前缀 abw.（AetherBreath WebUI），不与其他站点冲突；全程容错，
// 隐私模式/配额写满时静默降级为"不持久化"，绝不让界面崩。
const NS = 'abw.'

export function loadText(key: string): string | null {
  try { return window.localStorage.getItem(NS + key) } catch { return null }
}

export function saveText(key: string, value: string): void {
  try {
    if (value) window.localStorage.setItem(NS + key, value)
    else window.localStorage.removeItem(NS + key)
  } catch { /* 存不下就算了 */ }
}

/** 防抖包装：尾部触发，用于"边打字边存草稿"，避免每敲一键写一次 localStorage */
export function debounce<A extends unknown[]>(fn: (...args: A) => void, ms: number) {
  let t: number | undefined
  const wrapped = (...args: A) => {
    if (t !== undefined) window.clearTimeout(t)
    t = window.setTimeout(() => { t = undefined; fn(...args) }, ms)
  }
  wrapped.flush = (...args: A) => {
    if (t !== undefined) { window.clearTimeout(t); t = undefined; fn(...args) }
  }
  return wrapped
}

// ---------- 概况（token 消耗）本地延续 ----------
// 目标：浏览器刷新不丢、只在网关换代时清零。判据用 hub_seq —— 网关的 SSEHub 每次
// 启动都从 0 重新计数（sse.py 里 _seq = 0），所以「本地存的 seq 比刚收到的帧大」
// 就等于网关换过代，此时本地桶全是上一代的数，必须作废。
const K_USAGE = 'usage_by_sid'
const K_MODEL = 'model_by_sid'
const K_SEQ = 'usage_seq'

export interface UsageStore {
  bySid: Record<string, unknown>
  models: Record<string, string>
  seq: number
  stale: boolean
}

/** 写。带 seq 单调保护：更旧的快照不许覆盖更新的（多标签页时不至于互相抹平）。 */
export function saveUsage(bySid: unknown, models: unknown, seq: number): void {
  const cur = Number(loadText(K_SEQ)) || 0
  if (seq < cur) return
  saveText(K_USAGE, JSON.stringify(bySid ?? {}))
  saveText(K_MODEL, JSON.stringify(models ?? {}))
  saveText(K_SEQ, String(seq))
}

/** 读盘。解析失败就当没有（并清盘），绝不让界面崩。 */
export function loadUsage(): UsageStore {
  try {
    const bySid = JSON.parse(loadText(K_USAGE) || '{}') as Record<string, unknown>
    const models = JSON.parse(loadText(K_MODEL) || '{}') as Record<string, string>
    const seq = Number(loadText(K_SEQ)) || 0
    if (!bySid || typeof bySid !== 'object') { clearUsage(); return { bySid: {}, models: {}, seq: 0, stale: false } }
    return { bySid, models: models && typeof models === 'object' ? models : {}, seq, stale: false }
  } catch {
    clearUsage()
    return { bySid: {}, models: {}, seq: 0, stale: false }
  }
}

/** 本地快照所属的 hub_seq（0 = 没有快照）。 */
export function usageSeq(): number {
  return Number(loadText(K_SEQ)) || 0
}

export function clearUsage(): void {
  saveText(K_USAGE, '')
  saveText(K_MODEL, '')
  saveText(K_SEQ, '')
}
