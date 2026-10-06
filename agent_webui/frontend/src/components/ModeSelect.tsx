// 权限模式下拉（发送框左侧，2026-10 主人指定位置）。
//
// 四档：🔒 仅读 / 📁 工作区 / 🛡️ 普通 / 🔓 完全。**切换即生效，不弹二次确认**
// （主人 2026-10 定：下拉本身就是明确操作）。引擎侧的语义是"审批时点判定"——
// 已经发出去的调用不受影响，只约束之后的新调用。
//
// 它**只是开关**：不解释策略、不代替审批卡。四档各自允许什么由
// agent/permission_modes.py 定义，这里是它的一面镜子（清单从后端 catalog 拉，
// 拉不到就用本地兜底清单，至少让人看得见四档）。
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { FALLBACK_MODES, modeIcon, modeLabel, type ModeOption } from '../lib/permissionMode'
import { useApp } from '../store/appStore'

export default function ModeSelect() {
  const { state, dispatch } = useApp()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [catalog, setCatalog] = useState<ModeOption[]>(FALLBACK_MODES)
  const boxRef = useRef<HTMLDivElement>(null)
  const sid = state.current
  const mode = state.permissionMode || 'normal'

  // 点面板外面就收起。不做"必须再点一次按钮才关"——那种下拉在工具栏上很难用。
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  // 打开时对一次表（真源在后端）：顺带把"此刻真实的档"读回来 —— 会话可能在
  // 别处被切过（另一个标签页 / 命令行），不能只信本地那份。
  // 拿到的清单也**存进 store**：那块状态块的文案取自它（与模型每轮那行同源）。
  useEffect(() => {
    if (!open || !sid) return
    let alive = true
    void api.permissionMode(sid).then((r) => {
      if (!alive) return
      if (r.catalog && r.catalog.length) {
        setCatalog(r.catalog)
        dispatch({ type: 'set_mode_catalog', catalog: r.catalog })
      }
      if (r.mode) dispatch({ type: 'set_permission_mode', mode: r.mode })
    }).catch(() => { /* 网关不可达：保留兜底清单，不改当前档 */ })
    return () => { alive = false }
  }, [open, sid, dispatch])

  const pick = async (m: string) => {
    if (!sid || busy) return
    setBusy(true)
    try {
      const r = await api.permissionMode(sid, m)
      if (!r.mode) throw new Error(r.error || '后端没有确认新模式')
      dispatch({ type: 'set_permission_mode', mode: r.mode })
      const opt = catalog.find((c) => c.mode === r.mode)
      dispatch({
        type: 'toast', kind: 'ok',
        msg: `权限模式：${opt?.label || modeLabel(r.mode)}`
          + (r.offline ? '（AB 未开机，已存入会话，开机后生效）' : ''),
      })
      setOpen(false)
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `切换权限模式失败：${(e as Error).message}` })
    } finally { setBusy(false) }
  }

  return (
    <div className="mode-select" ref={boxRef}>
      <button type="button"
              className={`btn ghost mode-btn mode-${mode}`}
              disabled={!sid || busy}
              title={sid
                ? '本会话权限模式：点开切换（立即生效；已发出的工具调用不受影响）'
                : '先开会话/选一个会话，再设权限模式'}
              onClick={() => setOpen((v) => !v)}>
        <span className="mode-ico">{modeIcon(mode)}</span>
        <span className="mode-name">{modeLabel(mode)}</span>
      </button>
      {open ? (
        <div className="mode-menu" role="menu">
          <div className="mode-menu-head">本会话权限模式</div>
          {catalog.map((o) => (
            <button type="button" key={o.mode} role="menuitem"
                    className={`mode-item${o.mode === mode ? ' on' : ''}`}
                    disabled={busy}
                    onClick={() => void pick(o.mode)}>
              <span className="mode-item-label">
                {o.icon} {o.label}
                {o.mode === mode ? <span className="mode-item-cur">当前</span> : null}
              </span>
              <span className="mode-item-desc">{o.description}</span>
            </button>
          ))}
          <div className="mode-menu-foot">切换立即生效；已发出的工具调用不受影响。</div>
        </div>
      ) : null}
    </div>
  )
}
