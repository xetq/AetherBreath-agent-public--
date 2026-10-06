import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { WorkspaceStatus } from '../types'

function fmtSize(n: number) {
  if (n < 1024) return `${n}B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}K`
  return `${(n / 1024 / 1024).toFixed(1)}M`
}

export default function WorkspacePanel() {
  const [ws, setWs] = useState<WorkspaceStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [auto, setAuto] = useState(true)

  const refresh = useCallback(async () => {
    try { setWs(await api.workspace()); setErr(null) }
    catch (e) { setErr((e as Error).message) }
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!auto) return
    const t = window.setInterval(() => void refresh(), 10000)
    return () => window.clearInterval(t)
  }, [auto, refresh])

  const toggle = (name: string) => setOpen(open === name ? null : name)

  if (err) return <div className="panel-body"><div className="err">工作区读取失败：{err}</div></div>
  if (!ws) return <div className="panel-body"><div className="empty">加载中…</div></div>

  return (
    <div className="panel-body">
      <div className="panel-sub">
        <span>{ws.task_dir_count} 个任务目录</span>
        <span className="mini">{fmtSize(ws.task_dirs.reduce((a, b) => a + (b.size || 0), 0))}</span>
        <label className="chk right"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />10s 自动刷新</label>
      </div>

      <div className="ws-list">
        {ws.task_dirs.map((d) => (
          <div key={d.name} className="ws-dir">
            <div className="ws-head" onClick={() => toggle(d.name)}>
              <span>{open === d.name ? '▾' : '▸'}</span>
              <b>{d.name}</b>
              <span className="mini">{d.file_count} 文件 · {fmtSize(d.size)}</span>
              <span className="when">{d.mtime.slice(5, 16)}</span>
            </div>
            {open === d.name && (
              <div className="ws-files">
                {d.files.length === 0 && <div className="empty">（空目录）</div>}
                {d.files.map((f) => (
                  <div key={f.name} className="ws-file">
                    <span className="fname">{f.name}</span>
                    <span className="mini">{fmtSize(f.size)}</span>
                    <span className="when">{f.mtime.slice(5, 16)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {ws.task_dirs.length === 0 && <div className="empty">工作区还没有任务子目录</div>}
      </div>

      <div className="ws-sect">
        <div className="tl-lbl">最近活动文件</div>
        {ws.recent_files.slice(0, 12).map((f) => (
          <div key={f.path} className="ws-file">
            <span className="fname" title={f.path}>{f.path}</span>
            <span className="mini">{fmtSize(f.size)}</span>
            <span className="when">{f.mtime.slice(5, 16)}</span>
          </div>
        ))}
        {ws.recent_files.length === 0 && <div className="empty">（无）</div>}
      </div>

      <div className="ws-sect">
        <div className="tl-lbl">会话落盘</div>
        <div className="kv"><span>数量</span><b>{ws.sessions.count}</b></div>
        {Object.entries(ws.sessions.by_status).map(([k, v]) => (
          <div className="kv" key={k}><span>{k}</span><b>{v}</b></div>
        ))}
        {ws.sessions.tmp_files ? <div className="kv warn"><span>残留 .tmp</span><b>{ws.sessions.tmp_files}</b></div> : null}
        {ws.skills && <div className="kv"><span>技能目录</span><b>{ws.skills.count}</b></div>}
      </div>
    </div>
  )
}
