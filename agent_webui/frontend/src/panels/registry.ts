// 可插拔面板注册表：加面板 = 写一个组件 + 在这里加一行（右栏自动生成标签）。
import type { ComponentType } from 'react'
import ConsolePanel from '../components/ConsolePanel'
import WorkspacePanel from '../components/WorkspacePanel'
import ContextPanel from '../components/ContextPanel'

export interface PanelDef {
  id: string
  title: string
  icon: string
  component: ComponentType
}

export const PANELS: PanelDef[] = [
  { id: 'console', title: '控制台', icon: '🖥️', component: ConsolePanel },
  { id: 'workspace', title: '工作区', icon: '📁', component: WorkspacePanel },
  { id: 'context', title: '运行时', icon: '⚙️', component: ContextPanel },
]
