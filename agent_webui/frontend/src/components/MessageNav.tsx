// 消息导航栏：**常驻对话区右侧边缘的垂直轨道**（主人 2026-09-30）
//
//   小横杠 —— 你发送的每一条用户消息，一条一杠
//   悬停   —— 弹出该条消息的摘要预览（确认"是不是这条"）
//   点击   —— 对话区平滑滚到那条消息，并短暂高亮
//   滚动   —— 上下浏览时对应横杠自动高亮（scroll-spy），实时指示读到哪儿
//
// 两个定位决定（都是踩过界的，写在这儿免得后人再试一遍）：
//   1. 轨道本体 `position: fixed` + JS 按**滚动容器实时矩形**贴边。不用 absolute 的原因：
//      竖轨要相对**可视框**固定、不跟内容滚走，也不能被 `.msgs` 的滚动条挤压。
//   2. 悬停预览**渲染在轨道之外**（挂到滚动容器里的 fixed 层）。原因：消息多时轨道自身
//      要能滚，而 `overflow-y: auto` 会在**两个轴**上形成裁剪区 —— 预览框整个落在轨道左侧，
//      必被裁掉；这不是调 z-index 或 overflow-x 能救的（visible+auto 会被规范强制成 auto）。
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, RefObject } from 'react'
import { useMsgNav } from '../lib/msgNav'
import type { NavItem } from '../lib/msgNav'

/** 竖轨自身尺度（与样式表 .msgnav 对齐；JS 只用来算"贴哪儿"） */
const RAIL_W = 16          // 轨道宽度
const RAIL_EDGE = 6        // 与滚动容器右缘的留白（让开滚动条）
const RAIL_MAX_VH = 0.72   // 轨道最高占可视高度的比例（上下留白）
const TIP_W = 268          // 预览框宽度（与 CSS 的 .msgnav-tip 一致）
const TIP_GAP = 9          // 预览框与轨道之间的间隙
const TIP_PAD = 8          // 预览框离视口上下/左侧的最小留白

function Tip({ item, n, style, tipRef }: {
  item: NavItem; n: number; style: CSSProperties; tipRef: (el: HTMLDivElement | null) => void
}) {
  const atts = item.atts ? ` · ${item.atts} 个附件` : ''
  const size = item.chars > 0 ? `${item.chars} 字` : '无正文'
  return (
    <div className="msgnav-tip" style={style} ref={tipRef} role="tooltip">
      <div className="msgnav-tip-head">
        第 {n} 条消息<span className="dim"> · {size}{atts}</span>
      </div>
      {item.preview
        ? <div className="msgnav-tip-body">{item.preview}</div>
        : <div className="msgnav-tip-body dim">（本条只有附件，没有正文）</div>}
    </div>
  )
}

export default function MessageNav({ items, turnCount, sessionKey, scrollRef }: {
  /** 要画几条杠（= 用户消息条数），来自 lib/msgNav 的 navItems() */
  items: NavItem[]
  /** 回合数：只给"不足两条不显示竖轨"的判据用 */
  turnCount: number
  /** 当前会话 id：换会话时清空阅读位置并重挂监听 */
  sessionKey: string
  scrollRef: RefObject<HTMLDivElement>
}) {
  const { active, jump, show } = useMsgNav(scrollRef, turnCount, sessionKey)
  const railRef = useRef<HTMLDivElement>(null)
  /** 写成 `| null` 联合：这样才是可写的 MutableRefObject（回调 ref 要往里赋值） */
  const tipRef = useRef<HTMLDivElement | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const [box, setBox] = useState<{ top: number; right: number; height: number } | null>(null)
  /** 预览框最终落点（**视口坐标**）+ 落点方式；null = 还没量出来 */
  const [tipPos, setTipPos] = useState<{ left: number; top: number; below: boolean } | null>(null)
  /** 上一次量出来的落点：用来判断"夹紧结果有没有变"，避免无谓的再渲染 */
  const posRef = useRef<{ left: number; top: number; below: boolean } | null>(null)

  /** 预览框的垂直落点：让它的**中心**对齐到悬停那条横杠的中心。
   *  全部用 `getBoundingClientRect()` 的**视口坐标**，不碰 offsetTop/offsetParent ——
   *  预览框是 `position: fixed`，视口坐标是它唯一的坐标系（不依赖祖先有没有定位）。 */
  const centerOf = (idx: number): number => {
    const dot = railRef.current?.querySelector<HTMLElement>(`[data-nav-idx="${idx}"]`)
    if (!dot) return 0
    const r = dot.getBoundingClientRect()
    return r.top + r.height / 2
  }

  /** 那条横杠在视口里的上下边（用于判断"居中会不会顶到窗口边"） */
  const dotEdges = (idx: number): { top: number; bottom: number } | null => {
    const dot = railRef.current?.querySelector<HTMLElement>(`[data-nav-idx="${idx}"]`)
    if (!dot) return null
    const r = dot.getBoundingClientRect()
    return { top: r.top, bottom: r.bottom }
  }

  // 贴边：按滚动容器的实时矩形算。rect.right 减掉滚动条宽度，免得轨道压在滚动条上。
  useEffect(() => {
    if (!show) { setBox(null); return }
    const el = scrollRef.current
    if (!el) return
    let pending = false
    const apply = () => {
      pending = false
      const r = el.getBoundingClientRect()
      if (r.height < 120) { setBox(null); return }        // 太矮（窗口被压扁）就先不显示
      const sbw = Math.max(0, el.offsetWidth - el.clientWidth)   // 竖向滚动条宽度
      const height = r.height * RAIL_MAX_VH
      setBox({
        top: r.top + (r.height - height) / 2,
        right: Math.max(2, window.innerWidth - r.right + sbw + RAIL_EDGE),
        height,
      })
    }
    const request = () => {
      if (pending) return
      pending = true
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(apply)
      else setTimeout(apply, 16)
    }
    apply()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(request) : null
    if (ro) ro.observe(el)
    window.addEventListener('resize', request)
    el.addEventListener('scroll', request, { passive: true })   // 横向布局变化 / 滚动条出现
    return () => {
      if (ro) ro.disconnect()
      window.removeEventListener('resize', request)
      el.removeEventListener('scroll', request)
    }
  }, [show, scrollRef, items.length])

  // scroll-spy 跟着走：当前那条滚出轨道可视范围时把**轨道自己**挪一下。
  // ⚠️ 这里**不能用 `dot.scrollIntoView()`** —— 它会一路滚动**所有**可滚动祖先，
  //    连 document 一起滚，表现是整个页面（含顶栏与轨道）被往上顶。
  //    实测证据：900px 视口里悬停时整页上移约 70px、预览框贴到窗口顶边。
  //    所以只改轨道自己的 scrollTop（"拖到哪"由几何算，绝不动别人）。
  useEffect(() => {
    const rail = railRef.current
    if (!rail || active < 0) return
    const dot = rail.querySelector<HTMLElement>(`[data-nav-idx="${active}"]`)
    if (!dot) return
    // 相邻横杠要按 **DOM 顺序**找，不能拿 active±1 —— 锚点是"消息流下标"，是稀疏的
    // （实测 80 根杠的锚点是 0,6,11,21,… 不是 0,1,2,3…）。一个"相邻间距"只用来做
    // 边界留白，取不到就回落到杠高。
    const inner = (dot.previousElementSibling as HTMLElement | null)
      || (dot.nextElementSibling as HTMLElement | null)
    if (!inner) return
    const r = rail.getBoundingClientRect()
    const d = dot.getBoundingClientRect()
    const i = inner.getBoundingClientRect()
    const step = Math.abs(i.top - d.top) || d.height              // 相邻两条的轨内间距
    const maxTop = Math.max(0, rail.scrollHeight - rail.clientHeight)
    let want = rail.scrollTop
    if (d.top < r.top + step) want = Math.max(0, rail.scrollTop - (r.top + step - d.top) - 1)
    else if (d.bottom > r.bottom - step) want = Math.min(maxTop, rail.scrollTop + (d.bottom - (r.bottom - step)) + 1)
    else return
    if (Math.abs(want - rail.scrollTop) < 1) return
    rail.scrollTop = want
  }, [active, items.length, box])

  // 换悬停目标：先按"中心对齐那根横杠"落点，量完高度后夹进视口。
  // 竖直方向优先**居中**；只有横杠**真的贴到窗口上边**时才**翻到它下方**
  //（更接近普通 tooltip 的读法），而不是硬夹出一条半截的框。
  // ⚠️ 判据必须看横杠**自身的可见性**：轨道预滚到底时，靠前的横杠 dotTop 是负的，
  //    光看"夹紧后的 top 贴顶"会误判成需要翻转 —— 那会把预览框画到屏幕外（实测 y≈-632）。
  useLayoutEffect(() => {
    const tip = tipRef.current
    const rail = railRef.current
    if (!tip || !rail || hover === null) return
    const vw = window.innerWidth || document.documentElement.clientWidth
    const vh = window.innerHeight || document.documentElement.clientHeight
    const rr = rail.getBoundingClientRect()
    const h = tip.offsetHeight
    const left = Math.max(TIP_PAD, Math.min(rr.left - TIP_GAP - TIP_W, vw - TIP_W - TIP_PAD))
    const center = centerOf(hover)
    const half = h / 2
    let below = false
    let top = Math.min(Math.max(center, TIP_PAD + half), Math.max(TIP_PAD + half, vh - TIP_PAD - half))
    const dot = dotEdges(hover)
    if (dot && dot.top >= 0 && dot.top < TIP_PAD + h) {
      // 横杠在视口内且贴顶 → 预览翻到它下方
      below = true
      top = Math.min(dot.bottom + TIP_GAP, Math.max(TIP_PAD, vh - TIP_PAD - h))
    } else if (dot && dot.bottom > vh) {
      // 横杠在视口下方（轨道预滚时会遇到）→ 贴住窗口下沿，别飞到屏幕外
      top = Math.max(TIP_PAD, vh - TIP_PAD - h)
    }
    const cur = posRef.current
    if (!cur || Math.abs(cur.left - left) > 0.5 || Math.abs(cur.top - top) > 0.5 || cur.below !== below) {
      posRef.current = { left, top, below }
      setTipPos({ left, top, below })
    }
  }, [hover, box])

  const nOf = useMemo(() => {
    const m = new Map<number, number>()
    items.forEach((it, i) => m.set(it.idx, i + 1))
    return m
  }, [items])

  if (!show || !box || items.length === 0) return null

  const hoverIdx = hover !== null && items.some((it) => it.idx === hover) ? hover : null
  const tipItem = hoverIdx === null ? null : items.find((it) => it.idx === hoverIdx) || null
  const tipStyle: CSSProperties = {
    width: TIP_W,
    // 视口坐标；还没量出来就先放到视口外，免得闪一下错位（量完立刻回正）
    left: tipPos ? tipPos.left : -TIP_W * 2,
    top: tipPos ? tipPos.top : (hoverIdx === null ? -TIP_W * 2 : centerOf(hoverIdx)),
    transform: tipPos && tipPos.below ? 'none' : 'translateY(-50%)',
  }

  return (
    <>
      <div className="msgnav" ref={railRef}
           style={{ top: box.top, right: box.right, width: RAIL_W, maxHeight: box.height }}
           role="navigation" aria-label="消息导航">
        {items.map((it) => (
          <button key={it.idx} type="button" data-nav-idx={it.idx}
                  className={`msgnav-dot${active === it.idx ? ' on' : ''}`}
                  onClick={() => jump(it.idx)}
                  onMouseEnter={() => setHover(it.idx)}
                  onMouseLeave={() => setHover((h) => (h === it.idx ? null : h))}
                  onFocus={() => setHover(it.idx)}
                  onBlur={() => setHover((h) => (h === it.idx ? null : h))}
                  aria-label={`跳到第 ${nOf.get(it.idx) || 0} 条用户消息`}
                  aria-current={active === it.idx ? 'true' : undefined}
                  title={it.preview || '（无正文）'} />
        ))}
      </div>
      {tipItem ? <Tip item={tipItem} n={nOf.get(tipItem.idx) || 0} style={tipStyle}
                      tipRef={(el) => { tipRef.current = el }} /> : null}
    </>
  )
}
