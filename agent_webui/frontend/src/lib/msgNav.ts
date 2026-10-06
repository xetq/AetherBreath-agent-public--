// 消息导航栏的定位/联动逻辑（主人 2026-09-30 定的方案②：右侧常驻竖轨）
//
// 契约（来自需求原文）：
//   ① 小横杠 = **用户发送的每一条消息**（一条消息一条杠；「用户交代」/后台作业交付挂在
//      用户卡里，不算独立消息，所以不占杠）；
//   ② 悬停 → 弹出该条消息的**摘要预览**；
//   ③ 点击 → 对话区**平滑滚动**到那条消息，并**短暂高亮**；
//   ④ 用户上下滚动浏览时，轨道上对应横杠**自动高亮**（scroll-spy），实时指示阅读位置。
//
// 这里只放纯逻辑（预览摘要 / 定位 / scroll-spy 判据），React 壳在 components/MessageNav.tsx。
import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import type { TurnView } from './turns'

/** 用户消息卡的 DOM 锚点属性：用**回合序号**，与 React key 无关（历史重建也不飘）。 */
export const MSG_ANCHOR = 'data-msg-turn'
/** 点击跳转后短暂高亮的 class（CSS 里定义动画，到点由 JS 摘掉） */
export const MSG_FLASH_CLASS = 'msg-jump-flash'
/** 高亮停留时长（ms） */
export const FLASH_MS = 1500
/** 悬停预览的预览正文最多取多少字 */
const PREVIEW_MAX = 140

/** 导航栏要画的一条横杠 */
export interface NavItem {
  /** 锚点值 = 该用户消息在**原始消息流里的下标**（= 对应卡片上的 data-msg-turn） */
  idx: number
  /** 悬停预览用的一行摘要（已去 Markdown、已截断） */
  preview: string
  /** 该消息有多少字（0 = 只有附件、没有正文） */
  chars: number
  /** 附带几个附件 */
  atts: number
}

/** Markdown → 一行纯文本（只给预览用；渲染仍走 md.ts 的 marked，这里不许改原文）。 */
export function previewText(src: string): string {
  let s = (src || '').replace(/\r\n?/g, '\n')
  s = s.replace(/```[\s\S]*?```/g, ' ')            // 围栏代码块整体丢弃（预览里没有排版可言）
  s = s.replace(/~~~[\s\S]*?~~~/g, ' ')
  s = s.replace(/`([^`]*)`/g, '$1')                // 行内代码去反引号
  s = s.replace(/!\[[^\]]*\]\([^)]*\)/g, '')       // 图片整个丢
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')    // 链接只留文字
  s = s.replace(/^\s{0,3}#{1,6}\s*/gm, '')         // 标题号
  s = s.replace(/^\s{0,3}>\s?/gm, '')              // 引用号
  s = s.replace(/^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+/gm, '')   // 列表符号
  s = s.replace(/^\s{0,3}(?:[-*_]\s?){3,}\s*$/gm, ' ')      // 分隔线
  s = s.replace(/[*_~]{1,3}/g, '')                 // 强调记号
  s = s.replace(/\s+/g, ' ').trim()
  return s
}

/** 按「回合里有没有真实用户输入」挑出导航项 —— 交付/交代挂靠的回合不算。 */
export function navItems(turns: TurnView[]): NavItem[] {
  const out: NavItem[] = []
  for (const t of turns) {
    if (!t.user) continue
    const full = previewText(t.user.content || '')
    const preview = full.length > PREVIEW_MAX ? `${full.slice(0, PREVIEW_MAX)}…` : full
    out.push({ idx: t.anchor, preview, chars: full.length, atts: t.user.attachments?.length || 0 })
  }
  return out
}

const raf = (fn: () => void) => {
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fn)
  else setTimeout(fn, 16)
}

/** scroll-spy 的两个判据（纯函数，方便单独推敲）：
 *  - `atBottom`：真的滑到底（含 2px 容差）→ 直接判最后一条，不看判定线；
 *  - `probe`：**固定像素**判定线（视口 34%）。点阵是一屏一屏读的，
 *    比例阈在大屏上会钝到"滚了半屏还不动"，固定像素更跟手。 */
function readScroll(el: HTMLElement) {
  const max = el.scrollHeight - el.clientHeight
  return {
    atBottom: max > 0 && el.scrollTop >= max - 2,
    probe: el.scrollTop + el.clientHeight * 0.34,
  }
}

/**
 * 消息导航的定位与联动（scroll-spy + 点击跳转）。
 *
 * @param scrollRef 滚动容器（`.msgs`）的 ref —— **必须传真的那个 ref**：
 *        本 hook 不自己造 ref，否则 query 的是个永远为 null 的容器，
 *        表现就是"竖轨画出来了、点了没反应"（踩过）。
 * @param turnCount 回合数：只用来判「值不值得显示竖轨」（不足两条不显示）
 * @param sessionKey 当前会话 id：换会话 = 清空阅读位置 + 重挂监听
 * @returns 当前阅读位置序号、跳转函数、是否该显示
 *
 * 注意：依赖是**固定个数**的（sessionKey / turnCount），不接"外部依赖数组"——
 * React 要求 hook 依赖数组长度恒定，传数组进来等于把可变长度塞进 deps。
 */
export function useMsgNav(scrollRef: RefObject<HTMLDivElement>, turnCount: number, sessionKey: string) {
  const [active, setActive] = useState(-1)
  /** 最近一次用户点击跳转的目标 —— 平滑滚动途中由它压住 scroll-spy，防止中间态乱跳 */
  const target = useRef(-1)
  const flashTimer = useRef(0)

  /** 容器内已挂载的用户消息卡（按序号）。跳转与 scroll-spy 都只认它。 */
  const cards = () => {
    const el = scrollRef.current
    if (!el) return [] as HTMLElement[]
    return Array.from(el.querySelectorAll<HTMLElement>(`[${MSG_ANCHOR}]`))
  }

  /** 记下停在哪条 —— 仅作本地阅读位置线索（换会话后不自动跳，避免抢主人的滚动）。 */
  const remember = (idx: number) => {
    try { localStorage.setItem(`msgnav.${sessionKey || '_none'}`, String(idx)) } catch { /* 无痕等：忽略 */ }
  }

  const sync = () => {
    const el = scrollRef.current
    if (!el) return
    const list = cards()
    if (!list.length) { setActive(-1); return }
    if (target.current >= 0) {              // 跳转进行中：不做滚动推断，等定时器收口
      setActive(target.current)
      return
    }
    const top = el.getBoundingClientRect().top
    const { atBottom, probe } = readScroll(el)
    if (atBottom) {
      const last = Number(list[list.length - 1].dataset.msgTurn)
      setActive(last)
      remember(last)
      return
    }
    // 取「已越过判定线」的最靠下那条；都还没越过 → 高亮第一条
    let cur = Number(list[0].dataset.msgTurn)
    for (const c of list) {
      if (c.getBoundingClientRect().top - top <= probe) cur = Number(c.dataset.msgTurn)
      else break
    }
    setActive(cur)
    remember(cur)
  }

  const jump = (idx: number) => {
    const el = scrollRef.current
    if (!el) return
    const card = el.querySelector<HTMLElement>(`[${MSG_ANCHOR}="${idx}"]`)
    if (!card) { sync(); return }
    target.current = idx
    setActive(idx)
    card.scrollIntoView({ behavior: 'smooth', block: 'start' })
    if (flashTimer.current) window.clearTimeout(flashTimer.current)
    card.classList.remove(MSG_FLASH_CLASS)
    void card.offsetWidth                    // 强制重排：连续点同一条也能重播高亮动画
    card.classList.add(MSG_FLASH_CLASS)
    // 收口：闪一下就走，且**不留残余状态**（第二次点击同一条仍能再亮）
    flashTimer.current = window.setTimeout(() => {
      card.classList.remove(MSG_FLASH_CLASS)
      target.current = -1
      sync()
    }, FLASH_MS)
  }

  // 换会话：清空阅读位置（每条会话各记各的，localStorage key 用会话 id）
  useEffect(() => {
    setActive(-1)
    target.current = -1
  }, [sessionKey])

  // scroll-spy：滚动 + 尺寸变化都重算（窗口缩放、面板开合、图片/字体晚到都会改变几何）。
  // 回合数进依赖是有意的：**新增一条消息/一处追加都会改变几何**，必须重新量一次。
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    let pending = false
    const request = () => {
      if (pending) return
      pending = true
      raf(() => { pending = false; sync() })
    }
    el.addEventListener('scroll', request, { passive: true })
    const ro = typeof ResizeObserver === 'function'
      ? new ResizeObserver(() => request())
      : null
    if (ro) { ro.observe(el); for (const c of cards()) ro.observe(c) }
    window.addEventListener('resize', request)
    request()
    // 首次布局晚一拍再核一次：会话刚切完时消息高度可能还没定下来
    const late = window.setTimeout(sync, 90)
    return () => {
      el.removeEventListener('scroll', request)
      window.removeEventListener('resize', request)
      if (ro) ro.disconnect()
      window.clearTimeout(late)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionKey, turnCount, scrollRef])

  // 卸载：清掉可能还挂着的收口定时器（防止往已卸载的 DOM 上写）
  useEffect(() => () => { if (flashTimer.current) window.clearTimeout(flashTimer.current) }, [])

  return { active, jump, show: turnCount >= 2 }
}
