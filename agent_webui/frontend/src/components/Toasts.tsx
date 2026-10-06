import { useEffect } from 'react'
import { useApp } from '../store/appStore'

const LIFE = 6000

export default function Toasts() {
  const { state, dispatch } = useApp()
  useEffect(() => {
    if (state.toasts.length === 0) return
    const timers = state.toasts.map((t) =>
      window.setTimeout(() => dispatch({ type: 'drop_toast', id: t.id }), LIFE))
    return () => timers.forEach((x) => window.clearTimeout(x))
  }, [state.toasts, dispatch])

  if (state.toasts.length === 0) return null
  return (
    <div className="toasts">
      {state.toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <span>{t.msg}</span>
          <button className="x" onClick={() => dispatch({ type: 'drop_toast', id: t.id })}>✕</button>
        </div>
      ))}
    </div>
  )
}
