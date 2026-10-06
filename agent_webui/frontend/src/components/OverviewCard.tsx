// 「概况」卡片：**本会话** token 消耗（按会话分桶常驻，切走再切回不重置）。
// 数据来自 bridge 直接读 API 返回值的 usage（见 backend/bridge.py:_norm_usage），
// 不是估算、不是数汉字；字段名做了多厂商归一。
// 统计范围只覆盖 WebUI 回合；CLI（python agent/agent.py）跑的对话不在这里。
import { useApp } from '../store/appStore'

const n0 = (v?: number) => (v ?? 0).toLocaleString('en-US')
const kfmt = (v?: number) => ((v ?? 0) >= 1000 ? `${((v as number) / 1000).toFixed(1)}k` : n0(v))

export default function OverviewCard() {
  const { state } = useApp()
  const u = state.usage
  // 模型名一律来自运行时：usage 事件的 response.model，回落 hello.model，都不是写死的
  const hello = state.agent.hello as { model?: string; usage_watch?: string;
    ctx?: { enabled?: boolean; window?: number; threshold?: number } } | undefined
  const model = u?.model || hello?.model || '?'
  const watchOff = hello && hello.usage_watch === 'off'
  const node = u?.node
  // 「当前上下文占用」：最近一次请求的输入 token，触发压缩后会回落（与累计值语义不同）
  const lastPrompt = node?.last_prompt || 0
  const win = hello?.ctx?.window || 0
  const pct = win > 0 ? Math.min(100, (lastPrompt / win) * 100) : 0

  return (
    <div className="ov">
      <div className="ov-head">
        <span className="orch-title">概况</span>
        <span className="ov-scope" title="自本会话开始累计；切换会话不会重置，切回来还是这个数">
          {u ? '本会话' : '待统计'}
        </span>
        {watchOff && <span className="ov-badge bad" title="bridge 未能包装 LLM client，token 数拿不到">计量未挂载</span>}
        <span className="orch-kv" title={`模型 ${model}`}>{model}</span>
      </div>

      {!u && (
        <div className="ov-empty">
          {watchOff
            ? 'bridge 未能挂载计量（详见 logs/bridge_*.err）。'
            : '本会话还没有计量。发一条消息后，这里显示本会话真实消耗的 token（取自 API 返回的 usage；改动之前发生的对话不计入）。'}
        </div>
      )}

      {u && node && (
        <div className="ov-grid">
          {/* 首位给「当前上下文」：它是会随压缩回落的实时量；累计输入只增不减，
              单列一格意义不大，故收进 tooltip（主人 2026-09-13 定） */}
          <div className="ov-cell" title={`当前上下文占用 = 最近一次请求的输入 token（真实值），触发压缩后会回落。自会话开始累计的输入为 ${n0(node.prompt)} token（只增不减，已并入「总计」）`}>
            <span className="ov-v acc">{kfmt(lastPrompt)}</span>
            <span className="ov-k">当前上下文{win ? ` · ${pct.toFixed(1)}% / ${kfmt(win)}` : ''}</span>
          </div>
          <div className="ov-cell" title="输出 token：模型生成的内容之和">
            <span className="ov-v">{kfmt(node.completion)}</span>
            <span className="ov-k">输出</span>
          </div>
          <div className="ov-cell" title="思考 token：推理链消耗（已含在输出内，单独列出便于看出思考花了多少）">
            <span className={`ov-v ${node.reasoning ? 'lit' : ''}`}>{n0(node.reasoning)}</span>
            <span className="ov-k">其中思考</span>
          </div>

          <div className="ov-cell wide" title="总 token = 输入 + 输出（API 返回的 total_tokens 累计）">
            <span className="ov-v acc">{n0(node.total)}</span>
            <span className="ov-k">总计 · {n0(node.calls)} 次调用</span>
          </div>
        </div>
      )}
    </div>
  )
}
