import { useState } from 'react'
import type { RegistryRow, RegistrySection } from '../types'

/** 「运行时」面板里的一块注册表区（技能 / 集成包）—— 视觉与「MCP 服务站」共用同一套 class
 *  （`.ctx-sect` + `.mcp-list`），所以样子天生一致，没另造风格。
 *
 *  数据由父组件**一次取好**传进来：`/api/registries` 一次给两块，没必要各发一次请求。
 *
 *  标题行可点折叠 —— **三区统一**（技能 / MCP 服务站 / 集成包），主人 2026-09-23 裁决 Q3 + Q5。
 *  折叠开关只包住「caret + 标题文字」，右边的刷新按钮不受影响。
 *
 *  底部那行小字是裁决 Q4：技能与集成包是**新会话启动时才冻结进提示词**的，
 *  而面板照的是**磁盘现状** —— 两者可以不一致，不说破就是骗人。
 */

/** 悬停提示：面板只露「名字 + 描述」（裁决 Q2），其余全收进 title，不占视觉空间。 */
function tip(r: RegistryRow): string {
  const bits = [r.description || '（没写描述）']
  if (r.version) bits.push('v' + r.version)
  if (r.tags?.length) bits.push('标签：' + r.tags.join(', '))
  if (r.doc) bits.push(r.doc)
  return bits.join('\n')
}

export default function RegistrySect(
  { kind, data, err }: { kind: 'skills' | 'packs'; data: RegistrySection | null; err: string },
) {
  // 默认展开（裁决 Q3）；刷新页面即复位 —— 不落 localStorage，先保持最小改动。
  const [open, setOpen] = useState(true)
  const isSkills = kind === 'skills'
  const noun = isSkills ? '技能' : '集成包'
  const label = isSkills ? '技能 SKILL' : '集成包 PACK'
  const rows = (isSkills ? data?.skills : data?.packs) ?? []
  const issues = data?.issues ?? []
  const issueText = !issues.length ? ''
    : issues.length > 2 ? `📌 ${issues.slice(0, 2).join('；')}　… 共 ${issues.length} 条`
    : `📌 ${issues.join('；')}`

  return (
    <div className="ctx-sect">
      <div className="tl-lbl">
        <button className="tl-fold" onClick={() => setOpen((v) => !v)}
                title={open ? '收起这一区' : '展开这一区'}>
          <span className="tl-caret">{open ? '▾' : '▸'}</span>
          <span>
            {label}
            {data?.ok ? `（${data.count ?? 0} 个${issues.length ? ` · ${issues.length} 条提醒` : ''}）` : '（—）'}
          </span>
        </button>
      </div>
      {open && (
        <>
          {err ? <div className="hint danger">读取失败：{err}</div> : null}
          {data && data.ok === false ? <div className="hint danger">{data.error}</div> : null}
          {data?.ok && rows.length === 0 ? (
            <div className="hint">
              还没有{noun}。
              {isSkills
                ? <>建 <code>agent_skills/&lt;名字&gt;/SKILL.md</code>，frontmatter 里的 name 要与文件夹同名。</>
                : <>用 <code>pack_manage</code> 建一个，或手建 <code>agent_integration_packs/&lt;名字&gt;/PACK.md</code>。</>}
            </div>
          ) : null}
          {rows.length > 0 && (
            <ul className="mcp-list">
              {rows.map((r) => (
                <li key={r.name} title={tip(r)}>
                  <div className="mcp-top">
                    <b className="mcp-name">{r.name}</b>
                  </div>
                  <div className="mcp-meta">{r.description || '（没写描述）'}</div>
                </li>
              ))}
            </ul>
          )}
          {issueText ? <div className="hint">{issueText}</div> : null}
          {data?.ok ? (
            <div className="hint">
              已注册 <b>{data.count ?? 0}</b> 个{noun}，<b>新会话启动才注入提示词</b>（本会话沿用启动时的快照）。
              {data.registry ? <>　注册表 <code>{data.registry}</code>
                {data.registry_mtime ? `（更新于 ${data.registry_mtime}）` : '（还没有）'}。</> : null}
            </div>
          ) : null}
        </>
      )}
    </div>
  )
}
