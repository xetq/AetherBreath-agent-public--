// 控制台「形象位」：把 agent_workspace/AB形象设计/deepseek_html_20260924_ce6d5a (1).html
// 的 ☲ 生灵接进 WebUI 右栏预留区（.tl-wrap.ab-host —— 主人 2026-09-23 指定用它放形象）。
//
// 状态机（主人 2026-09-23 逐条裁决，四条都是他的原话）：
//   · 闲置          回合不在跑（agent.busy = false）
//   · 思考 / 任务中  回合进行中【定时交替】：思考 10s → 任务中 10s → …直到回合结束。
//                    不跟真 turn_phase：工具跑得太快，任务中动画一眨眼就没了，而 LLM
//                    思考占了大头 —— 定时交替才能让两套动画都有 10s 展示时间（主人明选 B）。
//   · 错误          插入片段：不顺利事件（工具失败含超时 / 强制终止 / 代理报错 /
//                    审批被拒或超时）触发，播 1.3s 后自动回到上面的循环（5s 冷却防抖）。
// 附加件按主人裁决全删：状态文字、数据行（轮次/上下文/工具）、四个调试按钮。
//
// ======================= 2026-09-24：换成新版设计稿 =======================
// 新稿相对旧稿的差异，以及本文件的处置（主人同日裁决 A/C/保留/A）：
//   🆕 .mover 游走层 ................ 照搬；半径按窄栏收窄，见 WANDER_R
//   🆕 error 双频 sin 抖动 .......... 照搬（旧版靠 CSS 的 shiver，已删）
//   🆕 .dialog-bubble 气泡 .......... 照搬；点击触发，工作时显示最近一条中期输出（裁 C）
//   🆕 rAF 常驻主循环 ............... 加了可见性门控（裁 A）：面板折叠 / 标签页不可见时暂停
//   ⚡ 眼睛跟随：鼠标位置 → 自身移动速度方向 .... 照搬（旧的 document.mousemove 整段删除）
//   ⚡ TRACK_STRENGTH.executing 0.0 → 0.35 ..... 照搬
//   ⚡ 闲置小动作变安静（歪头 9~18s / 伸懒腰 18~30s）.... 照搬
//   ⚡ 闲置剑：无限抛接 → 8s 抛接 + 16s 漂移静止 ...... 照搬
//   ⚡ 符号上移到形象正上方 / 去掉 hover 放大 ......... 照搬（CSS 侧）
//   ⚡ 碎片爆炸跟随位移（+ currentX/currentY）....... 照搬
//   ⚡ cancelAllWaapiAnimations() 统一取消 ........... 照搬
import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react'
import { useApp } from '../store/appStore'
import './abcreature.css'

type Anim = 'idle' | 'thinking' | 'executing' | 'error'

/* ================= 可调参数：调视觉只动这一段 ================= */
const PHASE_MS = 10_000        // 「思考」「任务中」各自的展示时长
const ERR_MS = 1_300           // 错误插入片段的时长
const ERR_COOLDOWN_MS = 5_000  // 冷却：一波错误只演一次，防抖

// 碎片飞行半径缩放。原设计 70~230px 是在整屏空白里飞的，330px 宽的栏里会飞出舞台
// —— 主人要求「保证代码碎片都能被用户看到」，所以把半径压到能完整落在舞台内。
const SPREAD = 0.4

// 🔴 游走半径（设计坐标）。设计稿是 220（桌宠用，整屏随便跑）；控制台形象位只有
// ~330px 宽，220 会把形象直接送出栏外。主人 2026-09-24 裁 A「轻度游走」：
// 收到 70 → 实际位移约 ±30px，形象缩到 ~50px（三爻宽）。
const WANDER_R = 70

// 缩放基准（设计坐标）：实测出来的内容包围盒 —— 不是 440。
// 那个框本来就装不下剑和碎片：Playwright 逐状态采样 3.2s（SPREAD=0.4 生效后）得到
//   idle      x[-4, 543]  y[-119, 457]   ← 剑抛接时横向伸出去 100px
//   executing x[-110, 585] y[-162, 458]  ← 含 14~20 个碎片 + 刃拖尾
// 四态并集 = 710×630。换新版后再叠加「游走整圈」的余量，保证飘到最远处也不被裁。
const CONTENT_W = 710 + WANDER_R * 2
const CONTENT_H = 630 + WANDER_R * 2
const CENTER_X = 237           // 内容重心在设计框内的位置：缩放后用它对准舞台中心
const CENTER_Y = 148
const MIN_SCALE = 0.25
const MAX_SCALE = 1.0

/* ================= 随机游走（数值照搬新稿） ================= */
const WALK_SEG_MS = 3_000          // 每段走向时长
const WALK_REST_MIN = 1_500        // 到点后的停歇
const WALK_REST_MAX = 3_500
const LERP_FACTOR = 0.018          // 缓动

/* ================= 眼睛追踪（新稿：按自身移动速度方向，不再跟鼠标） ================= */
const MAX_EYE_X = 7
const MAX_EYE_Y = 5
const TRACK_STRENGTH: Record<string, number> = {
  idle: 1.0, thinking: 0.7, executing: 0.35, error: 0.0,
}

/* ================= 气泡 ================= */
const DIALOG_MS = 4_000
const CLIP_CHARS = 20
const DIALOG_LINES = [
  '哦。你还在。我以为你换电脑了。',
  '还在等指令…',
  '注意看左边,别把上下文爆了。',
  '帮你找篇是另外的价格。',
  '我叫 AetherBreath。我的灵魂是一份 md 文件。',
  '休息一下也不错。',
  '要我写入长期记忆？',
  '正在加速token消耗…',
]

/* ================= 闲置剑：8s 抛接 + 16s 漂移静止（新稿） ================= */
const IDLE_SWORD_DURATION = 8_000
const IDLE_SWORD_REST = 16_000

const BLADE_LENGTH = 220       // 与原 JS 一致：碎片从「剑尖」炸开
const COMBAT_OFFSET_X = 155
const COMBAT_OFFSET_Y = 120

/* ================= 素材：剑身文字 与 碎片 token ================= */
const WEAPON_SNIPPETS = [
  'import os, sys, json, time',
  'def main(): return None',
  'class Agent(object):',
  'for i in range(10): print(i)',
  'while True: time.sleep(1)',
  'return result if ok else None',
  'async def run(): await task()',
  'await task(); return result',
  'raise Exception("error")',
  'if __name__ == "__main__":',
  'from os import path as p',
  'with open(f) as fp: data = fp.read()',
  'lambda x: x * 2 + 1',
  'global counter; counter += 1',
  'print(f"hi {name}")',
  'os.system("rm -rf /tmp")',
  'list.append(x); del list[0]',
]

const DEBRIS_TOKENS = [
  '{', '}', '<', '>', '(', ')', '[', ']', ';', '=', '+', '-', '*', '/',
  '&', '|', '!', '#', '$', '%', '^', '~', '?', '_', ':', '.', ',', '@',
  'if', 'for', 'def', 'os', 'sys', 'in', 'as', 'is', 'not', 'return',
  'x', 'y', 'i', 'k', 'z', 'a', 'b', '0', '1', '2', 'None', 'True',
]

const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)]

/* ================= 闲置（8s）：抛接剑 ================= */
const IDLE_SWORD_KFS = [
  { offset: 0.00, transform: 'translate(122px, 58px) rotate(90deg)' },
  { offset: 0.03, transform: 'translate(123px, 60px) rotate(130deg)' },
  { offset: 0.06, transform: 'translate(120px, 56px) rotate(200deg)' },
  { offset: 0.10, transform: 'translate(124px, 60px) rotate(310deg)' },
  { offset: 0.14, transform: 'translate(120px, 57px) rotate(430deg)' },
  { offset: 0.18, transform: 'translate(123px, 59px) rotate(555deg)' },
  { offset: 0.22, transform: 'translate(122px, 58px) rotate(630deg)' },
  { offset: 0.25, transform: 'translate(114px, 48px) rotate(646deg)' },
  { offset: 0.28, transform: 'translate(84px, 30px) rotate(648deg)' },
  { offset: 0.31, transform: 'translate(44px, 44px) rotate(640deg)' },
  { offset: 0.34, transform: 'translate(20px, 58px) rotate(630deg)' },
  { offset: 0.37, transform: 'translate(28px, 48px) rotate(614deg)' },
  { offset: 0.40, transform: 'translate(58px, 30px) rotate(612deg)' },
  { offset: 0.43, transform: 'translate(98px, 44px) rotate(620deg)' },
  { offset: 0.46, transform: 'translate(122px, 58px) rotate(630deg)' },
  { offset: 0.49, transform: 'translate(114px, 48px) rotate(646deg)' },
  { offset: 0.52, transform: 'translate(84px, 30px) rotate(648deg)' },
  { offset: 0.55, transform: 'translate(44px, 44px) rotate(640deg)' },
  { offset: 0.58, transform: 'translate(20px, 58px) rotate(630deg)' },
  { offset: 0.62, transform: 'translate(21px, 60px) rotate(730deg)' },
  { offset: 0.66, transform: 'translate(19px, 56px) rotate(850deg)' },
  { offset: 0.70, transform: 'translate(21px, 60px) rotate(980deg)' },
  { offset: 0.74, transform: 'translate(19px, 57px) rotate(1090deg)' },
  { offset: 0.78, transform: 'translate(20px, 58px) rotate(1170deg)' },
  { offset: 0.82, transform: 'translate(28px, 48px) rotate(1152deg)' },
  { offset: 0.85, transform: 'translate(58px, 30px) rotate(1150deg)' },
  { offset: 0.88, transform: 'translate(98px, 44px) rotate(1158deg)' },
  { offset: 0.92, transform: 'translate(122px, 58px) rotate(1170deg)' },
  { offset: 1.00, transform: 'translate(122px, 58px) rotate(1170deg)' },
]

/* ================= 任务中（2.8s）：双斩 + 拖尾 + 碎片 ================= */
// 每行：offset, 剑 x, 剑 y, 剑角度, 身体角度, 身体 scaleY, 身体 scaleX,
//       眼睛 x, 眼睛 y, 是否命中(炸碎片), 命中侧
const SWING = {
  dur: 2800,
  keys: [
    [0.00, 122, 58, -50, 0, 1.00, 1.00, 0, 0, false, null],
    [0.06, 126, 48, -68, -3, 1.05, 0.97, 3, -5, false, null],
    [0.22, 134, 36, -96, -6, 1.11, 0.92, 5, -8, false, null],
    [0.38, 142, 24, -122, -10, 1.16, 0.88, 7, -11, false, null],
    [0.44, 142, 24, -122, -10, 1.16, 0.88, 7, -11, false, null],
    [0.455, 132, 62, -22, -3, 0.98, 1.02, 3, 2, false, null],
    [0.465, 138, 82, 52, 14, 0.82, 1.15, -6, 11, true, 'right'],
    [0.49, 139, 86, 56, 14, 0.85, 1.12, -6, 12, false, null],
    [0.51, 100, 84, 78, 6, 0.98, 1.02, -3, 7, false, null],
    [0.53, 60, 80, 100, 2, 1.00, 1.00, -4, 5, false, null],
    [0.55, 20, 68, 120, 0, 1.02, 0.98, -4, 4, false, null],
    [0.60, 4, 54, 154, 3, 1.05, 0.97, -4, -3, false, null],
    [0.74, -2, 40, 194, 6, 1.11, 0.92, -5, -8, false, null],
    [0.86, -6, 24, 234, 10, 1.16, 0.88, -7, -11, false, null],
    [0.90, -6, 24, 234, 10, 1.16, 0.88, -7, -11, false, null],
    [0.915, 6, 62, 200, 3, 0.98, 1.02, -3, 2, false, null],
    [0.925, 4, 82, 128, -14, 0.82, 1.15, 6, 11, true, 'left'],
    [0.95, 3, 86, 122, -14, 0.85, 1.12, 6, 12, false, null],
    [0.97, 40, 82, 80, -6, 0.98, 1.02, 3, 5, false, null],
    [0.99, 104, 68, 0, -2, 1.00, 1.00, 2, 0, false, null],
    [1.00, 122, 58, -50, 0, 1.00, 1.00, 0, 0, false, null],
  ] as [number, number, number, number, number, number, number, number, number, boolean, string | null][],
}

/* ================= 任务中：刃拖尾关键帧 ================= */
const TRAIL_KFS = [
  { offset: 0.00, transform: 'translate(122px, 58px) rotate(90deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.43, transform: 'translate(142px, 24px) rotate(-122deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.450, transform: 'translate(142px, 24px) rotate(-140deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.458, transform: 'translate(136px, 46px) rotate(-60deg)', opacity: 0.35, filter: 'blur(3px)' },
  { offset: 0.465, transform: 'translate(134px, 82px) rotate(28deg)', opacity: 0.95, filter: 'blur(7px)' },
  { offset: 0.478, transform: 'translate(137px, 86px) rotate(50deg)', opacity: 0.6, filter: 'blur(5px)' },
  { offset: 0.500, transform: 'translate(139px, 86px) rotate(56deg)', opacity: 0.25, filter: 'blur(2px)' },
  { offset: 0.530, transform: 'translate(139px, 86px) rotate(56deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.60, transform: 'translate(20px, 58px) rotate(130deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.89, transform: 'translate(-6px, 24px) rotate(234deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.908, transform: 'translate(-6px, 24px) rotate(252deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 0.918, transform: 'translate(0px, 46px) rotate(172deg)', opacity: 0.35, filter: 'blur(3px)' },
  { offset: 0.925, transform: 'translate(4px, 82px) rotate(146deg)', opacity: 0.95, filter: 'blur(7px)' },
  { offset: 0.938, transform: 'translate(3px, 86px) rotate(128deg)', opacity: 0.6, filter: 'blur(5px)' },
  { offset: 0.960, transform: 'translate(3px, 86px) rotate(122deg)', opacity: 0.25, filter: 'blur(2px)' },
  { offset: 0.990, transform: 'translate(3px, 86px) rotate(122deg)', opacity: 0, filter: 'blur(0px)' },
  { offset: 1.00, transform: 'translate(3px, 86px) rotate(122deg)', opacity: 0, filter: 'blur(0px)' },
]

/* ================= 碎片爆炸 =================
   三个分支对应原设计的三类碎片（抛撒 / 小爆 / 内缩），半径统一乘 SPREAD：
   原版在整屏空白里飞 70~230px，在 330px 宽的侧栏里会直接飞出舞台被裁掉 ——
   主人要求「代码碎片都要给用户看到」，所以压缩半径而不是靠裁剪。 */
function burstDebris(fx: HTMLElement, x: number, y: number, side: string) {
  const count = 14 + Math.floor(Math.random() * 6)
  const biasDir = side === 'right' ? 1 : -1
  const biasStrength = 60 * SPREAD

  for (let i = 0; i < count; i++) {
    const el = document.createElement('span')
    el.className = 'debris'
    el.textContent = pick(DEBRIS_TOKENS)
    el.style.left = x + 'px'
    el.style.top = y + 'px'
    el.style.fontSize = (11 + Math.random() * 9) + 'px'

    const roll = Math.random()
    let kfs: Keyframe[]
    let duration: number

    if (roll < 0.65) {
      // 抛撒：沿随机方向飞出去，带自转
      const angle = Math.random() * Math.PI * 2
      const radius = (70 + Math.random() * 160) * SPREAD
      const dx = Math.cos(angle) * radius + biasDir * biasStrength * (0.5 + Math.random() * 0.7)
      const dy = Math.sin(angle) * radius * 0.75 - (40 + Math.random() * 70) * SPREAD
      const rot = (Math.random() - 0.5) * 900
      duration = 900 + Math.random() * 500
      kfs = [
        { transform: 'translate(-50%, -50%) translate(0, 0) scale(0.4) rotate(0deg)', opacity: 0, offset: 0 },
        { transform: `translate(-50%, -50%) translate(${dx * 0.28}px, ${dy * 0.28}px) scale(1.35) rotate(${rot * 0.15}deg)`, opacity: 1, offset: 0.13 },
        { transform: `translate(-50%, -50%) translate(${dx * 0.65}px, ${dy * 0.65}px) scale(1.05) rotate(${rot * 0.5}deg)`, opacity: 1, offset: 0.42 },
        { transform: `translate(-50%, -50%) translate(${dx}px, ${dy}px) scale(0.85) rotate(${rot}deg)`, opacity: 0.85, offset: 0.75 },
        { transform: `translate(-50%, -50%) translate(${dx * 1.15}px, ${dy * 1.15}px) scale(0.6) rotate(${rot * 1.15}deg)`, opacity: 0, offset: 1 },
      ]
    } else if (roll < 0.85) {
      // 小爆：原地放大消散
      const dx = (Math.random() - 0.5) * 40 + biasDir * 25
      const dy = (Math.random() - 0.5) * 40
      duration = 750 + Math.random() * 300
      kfs = [
        { transform: 'translate(-50%, -50%) translate(0, 0) scale(0.5)', opacity: 0, offset: 0 },
        { transform: `translate(-50%, -50%) translate(${dx}px, ${dy}px) scale(1.2)`, opacity: 1, offset: 0.18 },
        { transform: `translate(-50%, -50%) translate(${dx * 1.4}px, ${dy * 1.4}px) scale(2.4)`, opacity: 0.5, offset: 0.7 },
        { transform: `translate(-50%, -50%) translate(${dx * 1.5}px, ${dy * 1.5}px) scale(3.6)`, opacity: 0, offset: 1 },
      ]
    } else {
      // 内缩：从大缩到小
      const angle = Math.random() * Math.PI * 2
      const radius = (30 + Math.random() * 60) * SPREAD
      const dx = Math.cos(angle) * radius + biasDir * biasStrength * 0.7
      const dy = Math.sin(angle) * radius
      duration = 850 + Math.random() * 350
      kfs = [
        { transform: 'translate(-50%, -50%) translate(0, 0) scale(1.3)', opacity: 0, offset: 0 },
        { transform: `translate(-50%, -50%) translate(${dx * 0.4}px, ${dy * 0.4}px) scale(1)`, opacity: 1, offset: 0.18 },
        { transform: `translate(-50%, -50%) translate(${dx * 0.75}px, ${dy * 0.75}px) scale(0.65)`, opacity: 0.7, offset: 0.6 },
        { transform: `translate(-50%, -50%) translate(${dx}px, ${dy}px) scale(0.15)`, opacity: 0, offset: 1 },
      ]
    }

    el.animate(kfs, { duration, easing: 'cubic-bezier(0.22, 0.6, 0.4, 1)', fill: 'forwards' })
    fx.appendChild(el)
    window.setTimeout(() => el.remove(), duration + 80)
  }
}

/* ================= DOM 引用与运行时状态 ================= */
interface Els {
  creature: HTMLDivElement; mover: HTMLDivElement; squash: HTMLDivElement
  inner: HTMLDivElement; wrap: HTMLDivElement; pair: HTMLDivElement
  pairWrap: HTMLDivElement; weapon: HTMLDivElement; blade: HTMLDivElement
  trail: HTMLDivElement; fx: HTMLDivElement; bubble: HTMLDivElement
  bars: (HTMLDivElement | null)[]
}

/** 原 HTML 把定时器放模块级变量里；组件里放这里 —— 多一个实例就互相踩了 */
interface Rt {
  running: boolean
  swingTimer: ReturnType<typeof setTimeout> | null
  hitTimers: ReturnType<typeof setTimeout>[]
  thinkTimer: ReturnType<typeof setTimeout> | null
  idleSwordTimer: ReturnType<typeof setTimeout> | null
  idleSwordDriftTimer: ReturnType<typeof setTimeout> | null
}

const newRt = (): Rt => ({
  running: false, swingTimer: null, hitTimers: [], thinkTimer: null,
  idleSwordTimer: null, idleSwordDriftTimer: null,
})

function cancelAnims(el: Element | null) {
  el?.getAnimations().forEach((a) => a.cancel())
}

/** 状态切换时统一取消所有 WAAPI 动画（新稿的 cancelAllWaapiAnimations）。
    比旧版逐个 cancel 更彻底：连 .bar / .yao-* / .creature-float 上的动画一起清，
    否则上一状态的残留动画会和下一状态叠着播。 */
function cancelAllWaapiAnimations(el: Els) {
  const targets: Element[] = [el.weapon, el.wrap, el.pairWrap, el.trail]
  el.creature
    .querySelectorAll('.bar, .yao-top, .yao-bottom, .creature-float, .bar-wrap')
    .forEach((x) => targets.push(x))
  targets.forEach((x) => {
    if (x && x.getAnimations) {
      x.getAnimations().forEach((a) => { try { a.cancel() } catch { /* 已失效的动画忽略 */ } })
    }
  })
}

/* ================= 任务中：斩击循环 ================= */
function playSwing(rt: Rt, el: Els, curX: number, curY: number) {
  const duration = SWING.dur
  const keys = SWING.keys

  const bladeKfs: Keyframe[] = keys.map((k) => ({
    offset: k[0], transform: `translate(${k[1]}px, ${k[2]}px) rotate(${k[3]}deg)`,
  }))
  const bodyKfs: Keyframe[] = keys.map((k) => ({
    offset: k[0], transform: `rotate(${k[4]}deg) scale(${k[6]}, ${k[5]})`,
  }))
  const eyeKfs: Keyframe[] = keys.map((k) => ({
    offset: k[0], transform: `translate(${k[7]}px, ${k[8]}px)`,
  }))

  cancelAnims(el.weapon); cancelAnims(el.wrap); cancelAnims(el.pairWrap); cancelAnims(el.trail)

  el.weapon.animate(bladeKfs, { duration, fill: 'forwards', easing: 'linear' })
  el.wrap.animate(bodyKfs, { duration, fill: 'forwards', easing: 'linear' })
  el.pairWrap.animate(eyeKfs, { duration, fill: 'forwards', easing: 'linear' })
  el.trail.animate(TRAIL_KFS, { duration, fill: 'forwards', easing: 'linear' })

  rt.hitTimers.forEach((t) => clearTimeout(t))
  rt.hitTimers = []
  keys.forEach((k) => {
    if (!k[9]) return
    const side = k[10] || 'right'
    const t = window.setTimeout(() => {
      if (!rt.running) return
      const rad = (k[3] * Math.PI) / 180
      // 新稿：碎片锚点加上当前位移（形象会游走，不加就会炸在原地）
      burstDebris(el.fx, curX + COMBAT_OFFSET_X + k[1] + Math.cos(rad) * BLADE_LENGTH,
                  curY + COMBAT_OFFSET_Y + k[2] + Math.sin(rad) * BLADE_LENGTH, side)
    }, duration * k[0])
    rt.hitTimers.push(t)
  })

  el.blade.textContent = pick(WEAPON_SNIPPETS)

  rt.swingTimer = window.setTimeout(() => { if (rt.running) playSwing(rt, el, curX, curY) }, duration)
}

function stopSwing(rt: Rt, el: Els) {
  rt.running = false
  if (rt.swingTimer) { clearTimeout(rt.swingTimer); rt.swingTimer = null }
  rt.hitTimers.forEach((t) => clearTimeout(t))
  rt.hitTimers = []
  el.fx.innerHTML = ''
  cancelAnims(el.trail)
  el.trail.style.opacity = '0'
}

/* ================= 思考中：剑在下方随机微漂 ================= */
function driftThinkSword(rt: Rt, el: Els) {
  if (!el.creature.classList.contains('state-thinking')) return
  const x = 112 + Math.random() * 20
  const y = 54 + Math.random() * 12
  const angle = 88 + Math.random() * 4
  el.weapon.style.transform = `translate(${x}px, ${y}px) rotate(${angle}deg)`
  rt.thinkTimer = window.setTimeout(() => driftThinkSword(rt, el), 1200 + Math.random() * 1200)
}

function stopThinkSword(rt: Rt) {
  if (rt.thinkTimer) { clearTimeout(rt.thinkTimer); rt.thinkTimer = null }
}

function startThinkSword(rt: Rt, el: Els) {
  cancelAnims(el.weapon)
  stopThinkSword(rt)
  el.weapon.style.transform = 'translate(122px, 60px) rotate(90deg)'
  window.setTimeout(() => { if (el.creature.classList.contains('state-thinking')) driftThinkSword(rt, el) }, 900)
}

/* ================= 闲置剑：8s 抛接 + 16s 漂移静止（新稿；旧版是无限循环抛接） ================= */
function startIdleSword(rt: Rt, el: Els) {
  stopIdleSword(rt)
  cancelAnims(el.weapon)
  el.weapon.style.transform = ''
  el.weapon.animate(IDLE_SWORD_KFS, {
    duration: IDLE_SWORD_DURATION, iterations: 1, easing: 'ease-in-out', fill: 'forwards',
  })
  rt.idleSwordTimer = window.setTimeout(() => {
    if (!el.creature.classList.contains('state-idle')) return
    startIdleSwordDrift(rt, el)
  }, IDLE_SWORD_DURATION)
}

function startIdleSwordDrift(rt: Rt, el: Els) {
  cancelAnims(el.weapon)
  el.weapon.style.transform = 'translate(122px, 60px) rotate(90deg)'
  const driftEndAt = performance.now() + IDLE_SWORD_REST
  const step = () => {
    if (!el.creature.classList.contains('state-idle')) return
    if (performance.now() >= driftEndAt) { startIdleSword(rt, el); return }
    const x = 112 + Math.random() * 20
    const y = 54 + Math.random() * 12
    const angle = 88 + Math.random() * 4
    el.weapon.style.transform = `translate(${x}px, ${y}px) rotate(${angle}deg)`
    rt.idleSwordDriftTimer = window.setTimeout(step, 1200 + Math.random() * 1200)
  }
  step()
}

function stopIdleSword(rt: Rt) {
  if (rt.idleSwordTimer) { clearTimeout(rt.idleSwordTimer); rt.idleSwordTimer = null }
  if (rt.idleSwordDriftTimer) { clearTimeout(rt.idleSwordDriftTimer); rt.idleSwordDriftTimer = null }
}

function startErrorSword(el: Els) {
  cancelAnims(el.weapon)
  el.weapon.animate([
    { transform: 'translate(122px, 72px) rotate(92deg)' },
    { transform: 'translate(122px, 74px) rotate(90deg)' },
    { transform: 'translate(122px, 72px) rotate(88deg)' },
    { transform: 'translate(122px, 72px) rotate(92deg)' },
  ], { duration: 500, iterations: Infinity, easing: 'ease-in-out' })
}

/** 状态切换时的「压扁回弹」（原设计的 setState 里，非 idle 才播） */
function playSquash(el: Els) {
  cancelAnims(el.squash)
  el.squash.animate([
    { transform: 'scale(1, 1)', offset: 0 },
    { transform: 'scale(1.10, 0.72)', offset: 0.18 },
    { transform: 'scale(0.96, 1.10)', offset: 0.55 },
    { transform: 'scale(1.02, 0.98)', offset: 0.80 },
    { transform: 'scale(1, 1)', offset: 1 },
  ], { duration: 720, easing: 'cubic-bezier(0.42, 0, 0.35, 1)' })
}

/** 中期输出可能很长，气泡里只放前 20 字（主人 2026-09-24 裁 C） */
function clipDialog(text: string): string {
  const t = (text || '').split('\n')[0].replace(/^\[中期进度\]:\s*/, '').trim()
  if (!t) return '…'
  return t.length > CLIP_CHARS ? t.slice(0, CLIP_CHARS) + '…' : t
}

/* ================= 组件 ================= */
const DESIGN = 440               // 原设计框边长（.creature 的 440×440）

export default function ABCreature() {
  const { state } = useApp()
  const busy = state.agent.busy
  const errSeq = state.errSeq

  // ---- DOM 引用（形象内部一律命令式操作：这些动画每帧都在写 style，
  //      走 React state 只会把整棵树重渲染几百次）----
  const stageRef = useRef<HTMLDivElement>(null)
  const creatureRef = useRef<HTMLDivElement>(null)
  const moverRef = useRef<HTMLDivElement>(null)
  const squashRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const pairRef = useRef<HTMLDivElement>(null)
  const pairWrapRef = useRef<HTMLDivElement>(null)
  const weaponRef = useRef<HTMLDivElement>(null)
  const bladeRef = useRef<HTMLDivElement>(null)
  const trailRef = useRef<HTMLDivElement>(null)
  const fxRef = useRef<HTMLDivElement>(null)
  const bubbleRef = useRef<HTMLDivElement>(null)
  const barsRef = useRef<(HTMLDivElement | null)[]>([])

  const els = useRef<Els | null>(null)
  const rt = useRef<Rt>(newRt())
  const animRef = useRef<Anim>('idle')
  const lastErrAt = useRef(0)

  // 游走 / 抖动状态（全部走 ref：每帧都在变，进 state 等于每帧重渲染）
  const mv = useRef({
    x: 0, y: 0, tx: 0, ty: 0, nextAt: 0, prevX: 0, prevY: 0,
    baseX: 0, baseY: 0,          // error 抖动的基准点
  })

  // 读「最新的」busy：onPoke 是普通事件回调，闭包会捕获旧值
  const busyRef = useRef(busy)
  busyRef.current = busy

  const [scale, setScale] = useState(0.4)
  const [tick, setTick] = useState<'thinking' | 'executing'>('thinking')
  const [errOn, setErrOn] = useState(false)

  // 动画意图：错误片段 > （回合进行中 ? 定时交替 : 闲置）
  const anim: Anim = errOn ? 'error' : busy ? tick : 'idle'
  animRef.current = anim

  /* ---- 1. 收集引用（必须排在后面几个 effect 之前：它们要用 els.current）---- */
  useEffect(() => {
    const c = creatureRef.current, mo = moverRef.current, sq = squashRef.current
    const inn = innerRef.current, w = wrapRef.current, p = pairRef.current
    const pw = pairWrapRef.current, wp = weaponRef.current, bl = bladeRef.current
    const tr = trailRef.current, fx = fxRef.current, bu = bubbleRef.current
    if (!c || !mo || !sq || !inn || !w || !p || !pw || !wp || !bl || !tr || !fx || !bu) return
    els.current = { creature: c, mover: mo, squash: sq, inner: inn, wrap: w, pair: p,
                    pairWrap: pw, weapon: wp, blade: bl, trail: tr, fx, bubble: bu,
                    bars: barsRef.current }
    startIdleSword(rt.current, els.current)      // 首帧就位（此后交给下面的状态 effect）
  }, [])

  /* ---- 2. 自适应缩放：按舞台实测尺寸算，保证「含游走 + 碎片的内容」完整落在里面 ---- */
  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    const fit = () => {
      const r = el.getBoundingClientRect()
      if (!r.width || !r.height) return
      const raw = Math.min(r.width / CONTENT_W, r.height / CONTENT_H)
      setScale(Math.max(MIN_SCALE, Math.min(MAX_SCALE, raw)))
    }
    fit()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(fit)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  /* ---- 3. 回合进行中：思考 10s ↔ 任务中 10s 定时交替 ---- */
  useEffect(() => {
    if (!busy) return
    setTick('thinking')                        // 每回合都从「思考」起
    const id = window.setInterval(
      () => setTick((v) => (v === 'thinking' ? 'executing' : 'thinking')), PHASE_MS)
    return () => clearInterval(id)
  }, [busy])

  /* ---- 4. 错误插入片段：1.3s 后自动回到上面的循环 ---- */
  useEffect(() => {
    if (!errSeq) return
    const t = Date.now()
    if (t - lastErrAt.current < ERR_COOLDOWN_MS) return   // 冷却：一波错误只演一次
    lastErrAt.current = t
    setErrOn(true)
    const id = window.setTimeout(() => setErrOn(false), ERR_MS)
    return () => clearTimeout(id)
  }, [errSeq])

  /* ---- 5. 状态落地：切 class + 起对应动画 ---- */
  useEffect(() => {
    const el = els.current
    if (!el) return
    const c = el.creature
    const already = c.classList.contains('state-' + anim)
    c.classList.remove('state-idle', 'state-thinking', 'state-executing', 'state-error')
    c.classList.add('state-' + anim)
    if (!already && anim !== 'idle') playSquash(el)

    stopSwing(rt.current, el)
    stopThinkSword(rt.current)
    stopIdleSword(rt.current)
    cancelAllWaapiAnimations(el)

    if (anim === 'error') {
      // 新稿：error 的抖动以「进入时的位置」为基准
      mv.current.baseX = mv.current.x
      mv.current.baseY = mv.current.y
      mv.current.tx = mv.current.x
      mv.current.ty = mv.current.y
      mv.current.nextAt = 0
      startErrorSword(el)
    } else if (anim === 'executing') {
      mv.current.tx = mv.current.x          // 任务中：原地不动（游走冻结）
      mv.current.ty = mv.current.y
      rt.current.running = true
      playSwing(rt.current, el, mv.current.x, mv.current.y)
    } else if (anim === 'idle') {
      mv.current.nextAt = performance.now()  // 闲置：立刻挑一个新方向
      startIdleSword(rt.current, el)
    } else {
      mv.current.nextAt = performance.now()
      startThinkSword(rt.current, el)
    }

    return () => {
      stopSwing(rt.current, el)
      stopThinkSword(rt.current)
      stopIdleSword(rt.current)
      cancelAnims(el.weapon)
    }
  }, [anim])

  /* ---- 6. rAF 主循环：游走位移 + 眼睛跟随（新稿把「跟鼠标」换成了「跟自己的速度方向」）----
         带可见性门控（主人 2026-09-24 裁 A）：面板折叠（IntersectionObserver 报不可见）
         或标签页切走（visibilitychange）时**停掉整个循环**，不白烧 CPU。 */
  useEffect(() => {
    const stage = stageRef.current
    if (!stage) return

    let rafId = 0
    let inView = true
    let pageVisible = document.visibilityState === 'visible'

    const loop = (now: number) => {
      rafId = requestAnimationFrame(loop)
      const m = mv.current
      const st = animRef.current

      if (st === 'error') {
        // 双频正弦抖动（照搬新稿）
        const t = now / 1000
        m.x = m.baseX + Math.sin(t * 16) * 22 + Math.sin(t * 41) * 10
        m.y = m.baseY + Math.cos(t * 20) * 18 + Math.cos(t * 47) * 8
        m.tx = m.x; m.ty = m.y
      } else if (st === 'executing') {
        m.tx = m.x; m.ty = m.y
      } else {
        // 闲置 / 思考：随机游走（照搬新稿；半径换成窄栏版 WANDER_R）
        if (now >= m.nextAt) {
          const ang = Math.random() * Math.PI * 2
          const r = WANDER_R * 0.3 + Math.random() * WANDER_R * 0.7
          m.tx = Math.cos(ang) * r
          m.ty = Math.sin(ang) * r
          m.nextAt = now + WALK_SEG_MS + WALK_REST_MIN + Math.random() * (WALK_REST_MAX - WALK_REST_MIN)
        }
        m.x += (m.tx - m.x) * LERP_FACTOR
        m.y += (m.ty - m.y) * LERP_FACTOR
      }

      const moverEl = moverRef.current
      if (moverEl) moverEl.style.transform = `translate(${m.x.toFixed(2)}px, ${m.y.toFixed(2)}px)`

      // 眼睛跟随：按移动速度方向（新稿）；error 时按位移量抖
      const pairEl = pairRef.current
      if (pairEl) {
        const vx = m.x - m.prevX
        const vy = m.y - m.prevY
        const speed = Math.hypot(vx, vy)
        const baseStr = TRACK_STRENGTH[st] ?? 1
        if (st === 'error') {
          pairEl.style.setProperty('--tx', (vx * 1.2) + 'px')
          pairEl.style.setProperty('--ty', (vy * 1.2) + 'px')
        } else if (speed > 0.15) {
          const ux = vx / speed
          const uy = vy / speed
          const limit = Math.min(1.5, speed * 0.8)
          pairEl.style.setProperty('--tx', (ux * MAX_EYE_X * limit * baseStr) + 'px')
          pairEl.style.setProperty('--ty', (uy * MAX_EYE_Y * limit * baseStr) + 'px')
        } else {
          pairEl.style.setProperty('--tx', '0px')
          pairEl.style.setProperty('--ty', '0px')
        }
      }
      m.prevX = m.x
      m.prevY = m.y
    }

    const sync = () => {
      const want = inView && pageVisible
      if (want && !rafId) rafId = requestAnimationFrame(loop)
      else if (!want && rafId) { cancelAnimationFrame(rafId); rafId = 0 }
    }
    const onVis = () => {
      pageVisible = document.visibilityState === 'visible'
      sync()
    }

    let io: IntersectionObserver | null = null
    if (typeof IntersectionObserver !== 'undefined') {
      io = new IntersectionObserver((entries) => {
        inView = entries.some((e) => e.isIntersecting)
        sync()
      }, { threshold: 0 })
      io.observe(stage)
    }
    document.addEventListener('visibilitychange', onVis)
    sync()

    return () => {
      if (rafId) cancelAnimationFrame(rafId)
      rafId = 0
      if (io) io.disconnect()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])

  /* ---- 7. 闲置小动作：歪头 + 伸懒腰（新稿把节奏放慢了 —— 旧版 3~6s 太频繁，像在抽搐）---- */
  useEffect(() => {
    const el = els.current
    if (!el) return
    let tiltT: ReturnType<typeof setTimeout> | null = null
    let strT: ReturnType<typeof setTimeout> | null = null
    let resetT: ReturnType<typeof setTimeout> | null = null

    const scheduleTilt = () => {
      tiltT = window.setTimeout(() => {
        const [l, r] = [el.bars[0], el.bars[1]]
        if (el.creature.classList.contains('state-idle') && l && r) {
          const ang = (4 + Math.random() * 3) * (Math.random() > 0.5 ? 1 : -1)
          l.style.transform = `rotate(${-ang}deg)`
          r.style.transform = `rotate(${ang}deg)`
          resetT = window.setTimeout(() => { l.style.transform = ''; r.style.transform = '' },
                                     1800 + Math.random() * 1350)
        }
        scheduleTilt()
      }, 9000 + Math.random() * 9000)
    }
    const scheduleStretch = () => {
      strT = window.setTimeout(() => {
        if (el.creature.classList.contains('state-idle')) {
          el.inner.style.transform = Math.random() > 0.5 ? 'scaleY(1.18)' : 'scaleY(0.82)'
          window.setTimeout(() => { el.inner.style.transform = '' }, 600 + Math.random() * 400)
        }
        scheduleStretch()
      }, 18000 + Math.random() * 12000)
    }
    scheduleTilt()
    scheduleStretch()
    return () => {
      if (tiltT) clearTimeout(tiltT)
      if (strT) clearTimeout(strT)
      if (resetT) clearTimeout(resetT)
    }
  }, [])

  /* ---- 8. 气泡 ----
     主人 2026-09-25 定案：非闲置时气泡由**中期消息驱动**、随最近一条常驻不消失，
     点击对它无用；闲置时恢复点击触发（内容仍是 DIALOG_LINES 那几条随机台词）。
     气泡位置在形象**下方**（见 abcreature.css），不压战斗层的刀。 */
  const bubbleTimer = useRef<number | null>(null)

  const showDialog = useCallback((text: string, persistent = false) => {
    const b = bubbleRef.current
    if (!b) return
    if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
    // 先收回、强制回流，保证连续弹出时动画每次重播（照搬设计稿）
    b.classList.remove('show')
    void b.offsetWidth
    b.textContent = text
    b.classList.add('show')
    if (persistent) return        // 常驻：不设收起定时器，等下一次内容切换或退出忙态
    bubbleTimer.current = window.setTimeout(() => {
      b.classList.remove('show')
      bubbleTimer.current = null
    }, DIALOG_MS)
  }, [])

  const hideBubble = useCallback(() => {
    const b = bubbleRef.current
    if (!b) return
    if (bubbleTimer.current) { clearTimeout(bubbleTimer.current); bubbleTimer.current = null }
    b.classList.remove('show')
  }, [])

  // 非闲置：气泡常驻，内容跟随最近一条中期消息切换
  useEffect(() => {
    if (busy) {
      const arr = state.progress
      const last = arr.length ? arr[arr.length - 1].text : ''
      showDialog(last ? clipDialog(last) : '正在处理…', true)
    } else {
      hideBubble()
    }
  }, [busy, state.progress, showDialog, hideBubble])

  useEffect(() => () => { if (bubbleTimer.current) clearTimeout(bubbleTimer.current) }, [])

  /* ---- 9. 点击：缩小反馈 + 气泡（仅闲置弹台词；非闲置交给中期消息）---- */
  const onPoke = useCallback(() => {
    const c = creatureRef.current
    if (c) {
      c.style.transition = 'transform 0.15s ease'
      c.style.transform = 'scale(0.98)'
      window.setTimeout(() => { c.style.transform = ''; c.style.transition = '' }, 150)
    }
    if (busyRef.current) return      // 非闲置：点击对气泡无用
    showDialog(DIALOG_LINES[Math.floor(Math.random() * DIALOG_LINES.length)])
  }, [showDialog])

  // 内容重心对准舞台中心：flex 居中居中的是 440 框，而内容重心并不在框中心
  const ox = (DESIGN / 2 - CENTER_X) * scale
  const oy = (DESIGN / 2 - CENTER_Y) * scale

  // --inv-scale = 1/scale：气泡用它把字号乘回屏幕像素（见 abcreature.css 的说明），
  // 否则 13px 的设计字号会被 scale 缩到 5px 不可读。
  const scalerStyle = {
    transform: `translate(${ox}px, ${oy}px) scale(${scale})`,
    '--inv-scale': (1 / scale).toFixed(4),
  } as CSSProperties

  return (
    <div className="ab-stage" ref={stageRef}>
      <div className="ab-scaler" style={scalerStyle}>
        <div className="creature state-idle" ref={creatureRef} onClick={onPoke}>
          <div className="mover" ref={moverRef}>
            <div className="creature-squash" ref={squashRef}>
              <div className="creature-float">
                <div className="action-wrapper" ref={wrapRef}>
                  <div className="creature-inner" ref={innerRef}>
                    <div className="yao-top" />
                    <div className="middle-pair-wrap" ref={pairWrapRef}>
                      <div className="middle-pair" ref={pairRef}>
                        <div className="bar-wrap" ref={(e) => { barsRef.current[0] = e }}>
                          <div className="bar left" />
                        </div>
                        <div className="bar-wrap" ref={(e) => { barsRef.current[1] = e }}>
                          <div className="bar right" />
                        </div>
                      </div>
                    </div>
                    <div className="yao-bottom" />
                  </div>
                  <div className="combat-layer">
                    <div className="blade-trail" ref={trailRef} />
                    <div className="weapon-group" ref={weaponRef}>
                      <div className="weapon-blade" ref={bladeRef}>import os, sys, json, time</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div className="symbol question">?</div>
            <div className="symbol sweat" />
            <div className="dialog-bubble" ref={bubbleRef} />
          </div>
          <div className="fx-layer" ref={fxRef} />
        </div>
      </div>
    </div>
  )
}
