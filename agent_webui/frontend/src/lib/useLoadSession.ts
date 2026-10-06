// 会话切换的唯一入口：加载历史 + 从 tool_calls 配对还原时间线。
import { useCallback } from 'react'
import { api } from '../api'
import { parseJobDelivery } from '../lib/jobDelivery'
import { parseMidTurn } from '../lib/midTurn'
import { parseModeNotice } from '../lib/permissionMode'
import { useApp, timelineFromHistory } from '../store/appStore'

export function useLoadSession() {
  const { dispatch } = useApp()
  return useCallback(async (sid: string) => {
    dispatch({ type: 'set_current', sid })
    try {
      const h = await api.history(sid)
      // 历史里注入过的「用户交代」只有 role/content，靠那行标识前缀认出来 ——
      // 标成 delivered（已注入必然已送达）并剥掉标识/引导行，界面才是它本来的样子。
      // 「后台作业交付」同理：也只剩 role/content，同样靠前缀认；但**正文原样保留**
      // （正文就是作业输出全文，剥了就没法看），只加一个标记让卡片知道该怎么画。
      //
      // 「权限模式切换通知」现在**直接丢弃**（2026-10 改版）：切换不再往历史里写通知，
      // 界面改为一块**就地更新**的状态块（反映"此刻是什么档"）。旧会话里已经落盘的
      // 那些通知若还画出来，就会与状态块重复、并堆成一条切换流水 —— 正是主人要废掉的。
      // 但仍然必须**认得出来**：不认就会被当成主人自己的发言（轮次与标题都会错）。
      const msgs = (h.exists ? h.messages : [])
        .filter((m) => m.role !== 'user' || !parseModeNotice(m.content || '').isNotice)
        .map((m) => {
          if (m.role !== 'user') return m
          const p = parseMidTurn(m.content || '')
          if (p.isMid) return { ...m, content: p.text, mid: 'delivered' as const }
          return parseJobDelivery(m.content || '').isJob ? { ...m, job: true } : m
        })

      // turns = 中期过程（bridge 落盘的 stdout 行，网关透出）：交给 store，
      // 与实时攒的内存版同结构，界面前后一致。
      // partial = **进行中**那个回合的半个快照（收尾前也已经在磁盘上）——
      // 少了它，"正在跑的回合已经产出的东西"会在刷新/切会话时当场消失。
      // permissionMode / modeCatalog：下拉与状态块要显示"现在哪一档、是什么意思"。
      dispatch({ type: 'set_messages', messages: msgs, timeline: timelineFromHistory(msgs),
                 turns: h.turns || [], partial: h.partial || null,
                 permissionMode: h.permission_mode || 'normal',
                 modeCatalog: h.permission_catalog || [] })
    } catch (e) {
      dispatch({ type: 'set_messages', messages: [], timeline: [], turns: [], partial: null })
      dispatch({ type: 'toast', kind: 'err', msg: `读取会话失败：${(e as Error).message}` })
    }
  }, [dispatch])
}
