// 后台作业「自动交付」的前端侧约定。
//
// ⚠️ JOB_DELIVERY_PREFIX 必须与 agent/task_orchestrator.py 里的 JOB_DELIVERY_PREFIX **逐字一致**：
// 会话历史里那条交付只有 role/content，前端全靠这一行字把它认出来（与「用户交代」同一套机制）。
// 两边各改一半 = 静默降级（交付被当成主人自己的发言渲染），构建与 tsc 都不会报错 ——
// 所以 tests/test_job_delivery.py 里有一条断言把这段字符串钉死。
//
// 交付长这样（`Job.delivery_text()` 生成）：
//   【后台作业交付 · 跨回合任务（不是你本回合发起的动作）】
//   作业 j3：execute_shell(sleep 5 && echo OK)（已完成，已跑 5.3s，归属会话 session_x）
//   输出（全文 12 字符）：
//   OK
//   · 这是自动交付，已写入本会话历史；该作业的内存已释放。

export const JOB_DELIVERY_PREFIX = '【后台作业交付 · 跨回合任务（不是你本回合发起的动作）】'
export const JOB_DELIVERY_MARK = '后台作业交付'
/** 卡片上的状态徽标：交付是**已经发生**的事（它落盘了才会出现在历史里） */
export const JOB_DELIVERY_BADGE = '✅ 全文已自动交付'

export interface JobDeliveryParts {
  /** 是不是一条交付（不是就原样返回 content，调用方无需先判断） */
  isJob: boolean
  /** 标题行：`作业 j3：execute_shell(...)（已完成，已跑 5.3s，归属会话 …）` */
  title: string
  /** 正文：交付的输出/日志全文（含 `输出（全文 N 字符）：` 这类标签行） */
  body: string
  /** 末尾的说明行（`· 这是自动交付…`）—— 单独放，不混进正文 */
  notes: string[]
}

/**
 * 剥掉交付的标识行，把正文与说明行分开。
 *
 * 为什么正文**原样保留**（不像「用户交代」那样剥引导行）：交付的正文就是作业的输出全文，
 * 剥掉任何一行都可能让粘贴代码/日志的人拿到错的东西。
 */
export function parseJobDelivery(content: string): JobDeliveryParts {
  const s = content || ''
  if (!s.startsWith(JOB_DELIVERY_PREFIX)) return { isJob: false, title: '', body: s, notes: [] }
  const lines = s.slice(JOB_DELIVERY_PREFIX.length).replace(/^\n+/, '').split('\n')
  const title = (lines.shift() || '').trim()
  const notes: string[] = []
  while (lines.length) {
    const last = (lines[lines.length - 1] || '').trim()
    if (!last.startsWith('·')) break
    notes.unshift(last)
    lines.pop()
  }
  return { isJob: true, title, body: lines.join('\n').trim(), notes }
}
