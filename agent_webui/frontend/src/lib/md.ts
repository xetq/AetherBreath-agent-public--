// Markdown 渲染：agent 输出以 markdown 为主，终端 UI 需要转成 HTML。
// 依赖仅 marked（零传递依赖）。输出用 sanitized 处理，避免工具返回内容注入脚本。
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: true })

const ESC: Record<string, string> = {
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}

/** 极简 HTML 清洗：剥掉脚本/事件属性，仅保留常用标签 */
function sanitize(html: string): string {
  return html
    .replace(/<\s*(script|iframe|object|embed|link|meta|style)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, '')
    .replace(/<\s*(script|iframe|object|embed|link|meta|style)[^>]*\/?>/gi, '')
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
    .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
    .replace(/\son\w+\s*=\s*[^\s>]+/gi, '')
    .replace(/javascript\s*:/gi, 'blocked:')
}

export function renderMarkdown(src: string): string {
  if (!src) return ''
  try {
    return sanitize(marked.parse(src, { async: false }) as string)
  } catch {
    return src.replace(/[&<>]/g, (c) => ESC[c] || c).replace(/\n/g, '<br>')
  }
}
