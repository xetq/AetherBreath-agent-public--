// 「控制台」面板（原「工具状态机」，2026-09-23 改名）：概况卡 → 任务编排器点阵 → 形象位。
// 工具时间线已移除：思考过程与工具运行都收进了输出卡片（默认收起、点开看全文）。
//
// 原「预留区」占位（.slot-free）2026-09-23 起由 ☲ 生灵接管（主人指定）：
//   · .ab-host 提供裁剪与定位锚点；形象按容器实测尺寸等比缩放（见 ABCreature.tsx）。
//   · 四态全由 store 驱动：闲置 = 回合不在跑；回合进行中 = 思考/任务中 定时交替；
//     不顺利事件（工具失败含超时 / 强制终止 / 代理报错 / 审批被拒或超时）= 1.3s 错误片段。
import OverviewCard from './OverviewCard'
import OrchStrip from './OrchStrip'
import ABCreature from './ABCreature'

export default function ConsolePanel() {
  return (
    <div className="panel-body tl-root">
      <div className="orch-wrap"><OverviewCard /></div>
      <div className="orch-wrap"><OrchStrip /></div>
      <div className="tl-wrap ab-host">
        <ABCreature />
      </div>
    </div>
  )
}
