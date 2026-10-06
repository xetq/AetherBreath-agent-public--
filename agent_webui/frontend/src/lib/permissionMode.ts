// 会话级权限模式（四档）与「切换通知」的前端侧约定。
//
// ⚠️ NOTICE_PREFIX 必须与 agent/permission_modes.py 里的 NOTICE_PREFIX **逐字一致**：
// 会话历史里那条切换通知只有 role/content，前端全靠这一行字把它认出来
// （与「用户交代」midTurn.ts、「后台作业交付」jobDelivery.ts 同一套机制）。
// 两边各改一半 = 静默降级（通知被当成主人自己的发言渲染），构建与 tsc 都不会报错 ——
// 所以 tests/test_permission_modes.py 里有一条断言把这段字符串钉死。

export const NOTICE_PREFIX = '🔐 [权限模式] '

/** 四档。顺序 = 从最紧到最松，与后端 permission_modes.MODES 一致。 */
export type PermissionMode = 'readonly' | 'workspace' | 'normal' | 'full'

export interface ModeOption {
  mode: PermissionMode
  label: string
  icon: string
  /** 一句话状态（**与模型每轮拿到的那行同源**，来自后端 permission_modes.STATUS） */
  status?: string
  description: string
}

/** 兜底清单：网关离线或后端是旧版时，下拉仍然画得出来（不可点切，只显示）。
 *  真源是后端 permission_modes.catalog() —— 这张表只保证"看得见"。 */
export const FALLBACK_MODES: ModeOption[] = [
  { mode: 'readonly', label: '仅读', icon: '🔒', description: '只能读取与了解信息；写入/删除/外发一律直接拒绝。' },
  { mode: 'workspace', label: '工作区', icon: '📁', description: 'agent_workspace/ 内自由读写；区外只读，越界直接拒绝。' },
  { mode: 'normal', label: '普通', icon: '🛡️', description: '工作区内外都能操作，按初始审批规则弹卡确认。' },
  { mode: 'full', label: '完全', icon: '🔓', description: '初始审批全部自动同意；绝对禁区仍然硬拦。' },
]

export const MODE_LABEL: Record<string, string> = {
  readonly: '仅读', workspace: '工作区', normal: '普通', full: '完全',
}

export const MODE_ICON: Record<string, string> = {
  readonly: '🔒', workspace: '📁', normal: '🛡️', full: '🔓',
}

export function modeLabel(mode?: string | null): string {
  return MODE_LABEL[String(mode || 'normal')] || '普通'
}

export function modeIcon(mode?: string | null): string {
  return MODE_ICON[String(mode || 'normal')] || '🛡️'
}

/** 这条历史消息是不是一条权限模式切换通知？是则返回剥掉前缀的正文。
 *
 * **现在只用于识别旧会话**：2026-10 改造后切换不再往历史里写通知（切几次堆几条，
 * 白占上下文），改成每轮请求瞬时带一行状态 + 界面一块就地更新的状态块。
 * 但旧会话里已经落盘的那些还得认得出来 —— 否则会被当成主人自己的发言。 */
export function parseModeNotice(s: string): { isNotice: boolean; text: string } {
  if (!s.startsWith(NOTICE_PREFIX)) return { isNotice: false, text: s }
  return { isNotice: true, text: s.slice(NOTICE_PREFIX.length) }
}

/** 某档的一句话状态；清单还没有时退回空串（**不编文案**，宁可少一行也不写第二份）。 */
export function modeStatus(mode: string | null | undefined, catalog: ModeOption[]): string {
  const m = String(mode || 'normal')
  return catalog.find((c) => c.mode === m)?.status || ''
}
