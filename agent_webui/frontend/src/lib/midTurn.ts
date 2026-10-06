// 中期交互（「用户交代」）的前端侧约定。
//
// ⚠️ MID_PREFIX 必须与 agent/mid_turn.py 里的 PREFIX 逐字一致：会话历史里那条
// 注入消息只有 role/content，没有额外字段，前端全靠这一行字把它认出来。
// 两边各改一半 = 静默降级（历史里那几条交代会被当成主人的普通发言渲染），
// 构建与 tsc 都不会报错。所以 tests/test_mid_turn.py 里有一条断言把这段字符串钉死。

export const MID_MARK = '用户交代'
export const MID_PREFIX = '【用户交代 · 回合进行中追加】'

/** 交代的送达状态（纯前端视图态，后端不返回）：
 *  pending   = 已投递，等 AB 跑到下一次工具返回
 *  delivered = 已随工具返回注入模型（会话里也已落盘）
 *  dropped   = 回合结束仍没送到，已作废（bridge 明确回报） */
export type MidStatus = 'pending' | 'delivered' | 'dropped'

/**
 * 历史消息里的一条「用户交代」：剥掉标识行与引导行，只留主人的正文。
 * 不是交代就原样返回，调用方无需先判断。
 */
export function parseMidTurn(content: string): { isMid: boolean; text: string } {
  const s = content || ''
  if (!s.startsWith(MID_PREFIX)) return { isMid: false, text: s }
  const lines = s.split('\n')
  let i = 1
  // 第 2 行是引导行（形如「（…）」）；万一格式变了也不硬吃正文
  if (i < lines.length && /^（.*）$/.test(lines[i].trim())) i += 1
  return { isMid: true, text: lines.slice(i).join('\n').trim() }
}

/** 状态徽标文案：送达与否必须一眼可见，不能让人以为「发出去了就是送到了」 */
export const MID_BADGE: Record<MidStatus, string> = {
  pending: '⏳ 待送达 · 随下一批工具返回注入',
  delivered: '✅ 已随工具返回注入',
  dropped: '⚠️ 未送达 · 回合已结束',
}
